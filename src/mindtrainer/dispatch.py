from __future__ import annotations

import copy
import dataclasses
from collections.abc import Callable
from contextlib import AbstractContextManager, ExitStack, nullcontext

from controlplane.daemon import DirectiveFailedError, Dispatcher
from controlplane.deferred import DeferredError
from controlplane.envelope import BigRef, Envelope
from controlplane.gitstore import UNREADABLE_PRODUCT_ERRORS, GitStore
from controlplane.paths import dataset_manifest_file, run_dir
from mindtrainer.checkpoint import CheckpointConverter, MockConverter
from mindtrainer.data_spec import (
    DataProducer, DataSynthesisError, InvalidDataSpecError, check_data_spec_against_config,
    data_spec_from_directive, manifest_data_run, required_stages, stages_to_synthesize,
)
from mindtrainer.executor import MockExecutor, TrainingExecutor
from mindtrainer.roll_executor import TrainingDataRejectedError
from mindtrainer.grouping import DegenerateGroupError
from mindtrainer.orchestrator import NotEnoughGroupsError, resume_or_run
from mindtrainer.resume import NeedsHumanError
from mindtrainer.roll_converter import EXPORT_FORMAT
from mindtrainer.reward_pipeline import (
    AttributionError, AttributionBackend, build_attribution_backend,
)
from mindtrainer.store_adapter import read_training_run
from mindtrainer.training_config import (
    InvalidTrainingConfigError, MISSING_MEMORY_REASON, TrainingConfig, is_pending_interact,
    load_config_from_directive, validate_training_config,
)
from mindtrainer.trajectory_source import (
    BatchTrajectorySource, InteractTrajectorySource, PolicyForRollout, TrajectorySource,
)
from simulator.backends import ModelBackend
from simulator.dataset import DatasetIntegrityError, DatasetManifest, read_manifest
from simulator.store_adapter import write_persona, write_scenario

# 确定性地做不成的失败:重试同样的指令还会再失败,所以映射成 directive 的 failed。
# 其余异常原样上抛,directive 保持 pending,下一轮重新派发(瞬态恢复)。
_DETERMINISTIC = (
    DatasetIntegrityError, DegenerateGroupError, AttributionError,
    NotEnoughGroupsError, InvalidTrainingConfigError, TrainingDataRejectedError, NeedsHumanError,
)


# 真正会用到的阶段只在 data_spec.required_stages 一处实现:合成与读清单必须对"要哪几个阶段"
# 意见一致,否则会合成用不到的阶段,或漏合成要用的阶段。
_required_stages = required_stages


def _data_run_id(run_id: str, ref: str, stage: str) -> str:
    """清单 id 的格式是 dsm-{数据所在的 run}-{阶段}(build_manifest 是唯一的构造点)。后一轮训练
    常常复用前一轮的数据集,所以数据所在的 run 从 id 里解出来,不假设它等于指令自己的 run。"""
    data_run = manifest_data_run(ref, stage)
    if data_run is None:
        raise DirectiveFailedError(
            f"directive {run_id}: stage_manifest_refs[{stage!r}]={ref!r} is not a manifest id "
            f"of the form 'dsm-<run>-{stage}'"
        )
    return data_run


def _prepare_interact(
    store: GitStore,
    run_id: str,
    cfg: TrainingConfig,
    directive: Envelope,
    data_producer: DataProducer | None,
) -> TrainingConfig:
    """生成路 interact 指令的回写:情境与人格到派发时才生成,落树后写回指令,返回回写后的配置。

    只有"待回写"(is_pending_interact:interact、scenario_ids 为空、bindings 非空且 persona_id 全是空串)
    才动手。其余配置原样返回,交给紧随其后的严格校验:点名路本来就完整;生成路已回写过的指令重入时
    也不再是待回写,所以回写只做一次;半成品(部分 persona_id 空、scenario_ids 空而人格齐、bindings 为空)
    不回写,由严格校验判 T2。

    写入顺序:情境与人格先落树,指令最后回写。回写前崩溃,重入时 world_for_interact 生成同样的 id 与
    同样的内容,重写是空操作(GitStore 对相同内容不提交),随后回写。
    直接改指令里的原始字典,而不是 TrainingConfig.to_dict():调度器写进 binding 的、TrainingConfig
    不认的键要原样留下(driver 现在是 RoleBinding 的字段,往返不丢,但这条规矩对以后新增的键仍然成立)。
    确定性做不成判 failed(T2);瞬态错误(OSError 等)原样上抛,指令保持 pending。"""
    if not is_pending_interact(cfg):
        return cfg
    try:
        spec = data_spec_from_directive(directive)
    except InvalidDataSpecError as exc:
        raise DirectiveFailedError(f"directive {run_id}: {exc}") from exc
    missing = []
    if spec is None:
        missing.append("a data_spec in the directive")
    if data_producer is None:
        missing.append("a data_producer")
    if missing:
        raise DirectiveFailedError(
            f"directive {run_id}: the interact config has no scenario_ids and no persona_id, so they must "
            f"be generated at dispatch time, which needs {' and '.join(missing)}"
        )
    make_world = getattr(data_producer, "world_for_interact", None)
    if make_world is None:
        raise DirectiveFailedError(
            f"directive {run_id}: the interact config must be generated at dispatch time, but the "
            f"data_producer {type(data_producer).__name__} has no world_for_interact"
        )
    try:
        scenarios, personas = make_world(cfg, spec)
    except (DataSynthesisError, *_DETERMINISTIC) as exc:
        raise DirectiveFailedError(
            f"directive {run_id}: data synthesis failed ({type(exc).__name__}: {exc})"
        ) from exc
    slots = [b.slot_id for b in cfg.bindings]
    absent = [slot for slot in slots if slot not in personas]
    if absent:
        # 生产器违约:缺人格会在下面变成 KeyError,被当成瞬态失败每轮重派
        raise DirectiveFailedError(
            f"directive {run_id}: data synthesis failed (world_for_interact returned no persona "
            f"for slot(s) {absent})"
        )
    for scenario in scenarios:
        write_scenario(store, scenario)
    for slot in slots:
        write_persona(store, personas[slot])
    path = f"{run_dir(run_id)}/directive.yaml"
    env = store.read_product(path)
    raw = env.payload["training_config"]
    raw["scenario_ids"] = [scenario.id for scenario in scenarios]
    for binding in raw["bindings"]:
        binding["persona_id"] = personas[binding["slot_id"]].id
    store.write_product(path, env, "mindtrainer: write back interact scenarios")
    try:
        return load_config_from_directive(env)
    except InvalidTrainingConfigError as exc:
        raise DirectiveFailedError(f"directive {run_id}: {exc}") from exc


def _synthesize_missing(
    store: GitStore,
    run_id: str,
    cfg: TrainingConfig,
    directive: Envelope,
    data_producer: DataProducer | None,
) -> None:
    """缺清单、指令带 data_spec、清单的数据所在 run 就是本 run:三者同时成立才合成。
    其余情况什么都不做,交给 _batch_manifests 按原样判(缺清单仍是 failed)。

    走到这里时配置校验、归因与 interact 客户端检查都已通过;此后 data_spec 只要在 payload 里就先严格解析:
    写错的 data_spec 是指令本身的错,不等用到才暴露。不合成的其余阶段先查清单在场,再调生产器。
    生产器抛的确定性错误判 failed(T2);瞬态错误(OSError 等)原样上抛,指令保持 pending,
    下一轮重入时已在树里的清单不再合成,episode 写完而清单未写的阶段整段重跑覆盖。"""
    try:
        spec = data_spec_from_directive(directive)
    except InvalidDataSpecError as exc:
        raise DirectiveFailedError(f"directive {run_id}: {exc}") from exc
    if spec is None:
        return
    stages = stages_to_synthesize(store, run_id, cfg)
    if not stages:
        return
    if data_producer is None:
        raise DirectiveFailedError(
            f"directive {run_id}: dataset manifest(s) for stage(s) {stages} are not in the tree and "
            f"must be synthesized from data_spec, but no data_producer was supplied"
        )
    # 其余阶段的清单(不合成的那些)先查 id 形状与文件是否在场:有一份缺了,指令反正要判 failed,
    # 先合成只会在树里留下一批不属于任何训练的 episode。内容是否读得出、与 episode 是否一致,
    # 仍由 _batch_manifests 在合成之后完整校验。
    for name in _required_stages(cfg):
        ref = cfg.stage_manifest_refs.get(name)
        if name in stages or not ref:
            continue
        if not store.exists(dataset_manifest_file(_data_run_id(run_id, ref, name), name)):
            raise DirectiveFailedError(
                f"directive {run_id}: dataset manifest {ref!r} for stage {name!r} is not in the tree"
            )
    reason = check_data_spec_against_config(spec, cfg, stages)
    if reason is not None:
        raise DirectiveFailedError(f"directive {run_id}: {reason}")
    try:
        data_producer.produce(store, run_id, cfg, spec)
    except (DataSynthesisError, *_DETERMINISTIC) as exc:
        raise DirectiveFailedError(
            f"directive {run_id}: data synthesis failed ({type(exc).__name__}: {exc})"
        ) from exc


def _batch_manifests(store: GitStore, run_id: str, cfg: TrainingConfig) -> dict[str, DatasetManifest]:
    """指令点名的清单必须在场,且就是点名的那一份。缺文件是配置问题(确定性),而 read_product
    缺文件抛的是 FileNotFoundError、read_manifest 又只会因内容对不上抛 DatasetIntegrityError,
    所以这里显式判一次,免得缺清单被当成瞬态失败反复重派。"""
    manifests: dict[str, DatasetManifest] = {}
    for name in _required_stages(cfg):
        ref = cfg.stage_manifest_refs.get(name)
        if not ref:
            continue
        data_run = _data_run_id(run_id, ref, name)
        if not store.exists(dataset_manifest_file(data_run, name)):
            raise DirectiveFailedError(
                f"directive {run_id}: dataset manifest {ref!r} for stage {name!r} is not in the tree"
            )
        try:
            manifest = read_manifest(store, data_run, name)
        except UNREADABLE_PRODUCT_ERRORS as exc:
            # 文件在场但读不出来(缺键、YAML 语法错、schema_version 没有迁移器):这同样是
            # 确定性的配置/数据问题,重试不会变好,不能让它逃出 dispatch() 变成每轮重派的
            # "瞬态失败"(那样会在同一处卡死 run_daemon,排在它后面的指令永远轮不到)。
            # 真正瞬态的异常(例如 OSError)不在这个元组里,原样上抛,指令保持 pending。
            raise DirectiveFailedError(
                f"directive {run_id}: dataset manifest {ref!r} for stage {name!r} cannot be "
                f"read ({type(exc).__name__}: {exc})"
            ) from exc
        if manifest.id != ref:
            raise DirectiveFailedError(
                f"directive {run_id}: the manifest at {dataset_manifest_file(data_run, name)} "
                f"is {manifest.id!r}, not the {ref!r} the config names"
            )
        manifests[name] = manifest
    return manifests


def _batch_sources(store: GitStore, run_id: str, cfg: TrainingConfig) -> Callable[[int], dict[str, TrajectorySource]]:
    """数据集清单在指令里点名(stage_manifest_refs),按 id 读回;清单内容与 episode 对不上时
    verify_manifest 抛 DatasetIntegrityError(确定性失败,映射见 _DETERMINISTIC)。"""
    manifests = _batch_manifests(store, run_id, cfg)

    def make_sources(_start: int) -> dict[str, TrajectorySource]:
        return {name: BatchTrajectorySource(store, m) for name, m in manifests.items()}

    return make_sources


def _interact_sources(
    store: GitStore,
    cfg: TrainingConfig,
    manifests: dict[str, DatasetManifest],
    client_factory: Callable[..., object],
    policy_for_rollout: PolicyForRollout,
    run_id: str,
) -> Callable[[int], dict[str, TrajectorySource]]:
    """interact 的 grpo_opd 来自实时会话,只有 sft 用清单。清单由调用方先读好(_batch_manifests):
    缺失、读不出这类确定性的错要在占用推理服务之前就判,不能等策略上下文进入之后。"""

    def make_sources(start: int) -> dict[str, TrajectorySource]:
        sources: dict[str, TrajectorySource] = {
            name: BatchTrajectorySource(store, m) for name, m in manifests.items()
        }
        if cfg.grpo_steps > 0:
            sources["grpo_opd"] = InteractTrajectorySource(
                client_factory, policy_for_rollout, store, cfg, run_id=run_id, start_step=start
            )
        return sources

    return make_sources


# 对手位没写 driver 时按 model_backend 取的默认驱动。与 factory.opponents.driver_for 必须一致:
# mindtrainer 不得导入 factory,所以这里复制一份;改一处要改另一处。
_DEFAULT_OPPONENT_DRIVER = {"frozen_reference": "vllm", "external_api": "external_api"}


def _is_adapter_export(ref: BigRef) -> bool:
    """起点是不是适配器导出:fetch_hint 的 type 是 roll_ssh 且 format 是 EXPORT_FORMAT。
    与 factory.opponents.resolve_model_ref、SomevalRunner 同一个两条件判法。只看 type 不够:
    roll_executor 写的训练存点 type 也是 roll_ssh(format 是 roll_lora_legacy),它不是适配器导出,
    记成 adapter 会让读记录的人以为训练位挂着 LoRA 在说话。EXPORT_FORMAT 与本模块同包,可以直接导入。"""
    hint = ref.fetch_hint or {}
    return hint.get("type") == "roll_ssh" and hint.get("format") == EXPORT_FORMAT


# 注入的记忆解析器:(种子, 情境 id 元组) -> (情境 id -> 起点世界摘要, 运行时身份)。
# 派发器不知道记忆库长什么样,也不得导入 simple_actio、factory;由装配方(组合根)注入。
MemoryResolver = Callable[[str | None, tuple[str, ...]], tuple[dict[str, str], dict]]


def _resolve_memory(run_id: str, cfg: TrainingConfig, memory_resolver: MemoryResolver) -> TrainingConfig:
    """解出 cfg.memory 里的起点摘要与运行时身份,返回带解出值的新配置。解出值只进这次派发的配置
    (于是进 config_hash、policy_factory、policy_backend),不写回 directive.yaml:续训时要重新解。

    入参情境按配置顺序去重。解析器抛 DirectiveFailedError 就按它的原因重抛(组合根给出可读原因);
    抛 DeferredError(记忆服务连不上、超时,30 篇 MT-32)原样上抛,指令留 pending,守护进程退避重试;
    抛其余任何异常只报类名,不带异常文本(起点世界打不开、摘要算不出,文本里可能有端点地址)。
    回写后再过一次严格校验:解析器给的摘要缺情境、身份缺键,都在这里判 T2。"""
    scenario_ids = tuple(dict.fromkeys(cfg.scenario_ids))
    try:
        digests, runtime = memory_resolver(cfg.memory["seed"], scenario_ids)
        # 返回值形状不对(摘要不是映射、身份拷不动)是确定性的错,与解析器自己抛错同样只报类名;
        # 转换放在 try 里,免得 TypeError 逃出 dispatch,让指令留在 pending 里每轮重派
        memory = {"seed": cfg.memory["seed"], "digests": dict(digests), "runtime": copy.deepcopy(runtime)}
    except DeferredError:
        raise
    except DirectiveFailedError as exc:
        raise DirectiveFailedError(f"directive {run_id}: {exc.reason}") from exc
    except Exception as exc:
        raise DirectiveFailedError(f"directive {run_id}: memory resolver failed: {type(exc).__name__}") from exc
    resolved = dataclasses.replace(cfg, memory=memory)
    reason = validate_training_config(resolved)
    if reason is not None:
        raise DirectiveFailedError(f"directive {run_id}: {reason}")
    return resolved


def _policy_backend(cfg: TrainingConfig, endpoint: str | None) -> dict:
    """interact 训练的后端说明,写进 training_run.policy_backend,让读记录的人不必从配置重新推导。
    只含 JSON 安全的类型(要写进产物树)。cfg 是回写后、严格校验过的配置;对手位 = 除训练位以外的绑定。
    memory 是解析之后的那份(含 digests 与 runtime):读记录的人从这里就能看到记忆从哪来。"""
    return {
        "trained": {
            "ref": cfg.base_checkpoint_ref.uri,
            "kind": "adapter" if _is_adapter_export(cfg.base_checkpoint_ref) else "base",
            "endpoint": endpoint,
        },
        "opponents": {
            b.slot_id: {
                "driver": b.driver if b.driver is not None else _DEFAULT_OPPONENT_DRIVER.get(b.model_backend),
                "source": b.model_ref,
            }
            for b in cfg.bindings if b.slot_id != cfg.trained_slot
        },
        "real_inference_with_mock_training": True,
        "memory": copy.deepcopy(cfg.memory),
    }


def make_mindtrainer_dispatcher(
    *,
    executor_factory: Callable[[str], TrainingExecutor] = lambda run_id: MockExecutor(),
    converter: CheckpointConverter | None = None,
    client_factory: Callable[..., object] | None = None,
    policy: ModelBackend | None = None,
    policy_factory: Callable[[str, TrainingConfig], AbstractContextManager[PolicyForRollout]] | None = None,
    data_producer: DataProducer | None = None,
    policy_endpoint: str | None = None,
    memory_resolver: MemoryResolver | None = None,
    repo_commit: str | None = None,
) -> Dispatcher:
    """构造 MindTrainer 派发器。它只做一件事:把一条 pending 的 directive 跑成候选。

    起点与续训交给 resume_or_run:指令若在上一轮崩在中途,directive 仍是 pending,
    下一轮重新派发时从 training_run 的存点接着跑,不从头再来。

    - batch:按 stage_manifest_refs 读数据集清单。
    - interact:另需 client_factory 与策略,注入后 grpo_opd 走实时会话。策略二选一:
      policy 是现成的固定后端(每条 rollout 都用它);policy_factory(run_id, cfg) 每次派发调用一次,
      返回一个上下文管理器,进入时交出 policy_for_rollout(episode_id, scenario_id),每条 rollout 进出一次,
      交出该条 rollout 的后端(组合根按情境 id 建这条 rollout 的记忆世界);整个上下文退出时收尾(起停推理服务、登记会话都放在这一进一出里)。
      client_factory 必须收 keyword-only deadline_s。cfg 是回写后、
      严格校验过的配置。上下文覆盖 _interact_sources 与整个 resume_or_run,且在分类异常的 try 里:
      进入时抛的延后异常(租约被占、服务起不来)照常走延后通道,此时训练记录一个字节都没写;
      进入时抛的确定性错误照常判 failed。两者同给是调用方的错,构造时 ValueError。
      另需 memory_resolver,进策略上下文之前解出起点摘要与运行时身份。
    - policy_endpoint:策略所用计算端点的名字,只用来写进 interact 运行的 training_run.policy_backend。
      由装配方给(派发器不得导入 factory,自己查不到);没给就记 None。batch 路径不写 policy_backend。
    - data_producer:指令带 data_spec 且本 run 名下的清单缺失时,用它先合成数据(两种模式都可,
      interact 只合成 sft)。不带 data_spec 的指令行为与改动前完全相同;带 data_spec 的指令总是
      先严格解析,写错的判 failed;需要合成而没有注入生产器时判 failed 并写明缺什么。
    - 生成路的 interact 指令(scenario_ids 为空、persona_id 全空)另需生产器的 world_for_interact
      (DataProducer 协议之外的可选方法):派发时生成情境与人格,落树后回写进指令,再按严格口径复校
      (见 _prepare_interact)。点名路与已回写的指令不经过它。
    - 确定性失败(数据不对、配置不合法、该人工介入)抛 DirectiveFailedError,调和循环把
      directive 置 failed,不再重试;其余异常原样上抛,保持 pending 等下一轮。
    - repo_commit:本次派发推进训练所用的 rise 代码版本,写进 training_run.code_versions(新建时写入,
      续训或重启时追加;同值只记一次)。只记不拦:config_hash 续训判定与 T3 规则一概不动。
      派发器不得导入 factory 自己查,由装配方给;取不到就是 None,记录不改。
    """
    if policy is not None and policy_factory is not None:
        raise ValueError("pass either policy or policy_factory to make_mindtrainer_dispatcher, not both")
    converter = converter or MockConverter()

    def dispatch(store: GitStore, directive: Envelope) -> None:
        run_id = directive.id
        try:
            cfg = load_config_from_directive(directive)
        except InvalidTrainingConfigError as exc:
            raise DirectiveFailedError(f"directive {run_id}: {exc}") from exc
        if cfg.mode == "interact" and cfg.memory is None:
            # 落地前写下的 interact 指令:早判,不让生成路的旧指令先合成、落树、回写情境与人格再判失败
            # (紧随其后的 validate_training_config 给同一原因,早判只是让它不留副作用)
            raise DirectiveFailedError(f"directive {run_id}: {MISSING_MEMORY_REASON}")
        # 生成路的 interact 指令此刻 scenario_ids 与 persona_id 还空着:先回写,再让严格校验看回写后的配置
        cfg = _prepare_interact(store, run_id, cfg, directive, data_producer)
        reason = validate_training_config(cfg)
        if reason is not None:
            raise DirectiveFailedError(f"directive {run_id}: {reason}")
        try:
            # 本入口不提供 judge:judge 归因的指令做不成,重试无益,所以判失败而不是让 ValueError 逃出去
            attribution: AttributionBackend = build_attribution_backend(cfg.reward)
        except ValueError as exc:
            raise DirectiveFailedError(f"directive {run_id}: {exc}") from exc
        if cfg.mode == "interact":
            if client_factory is None or (policy is None and policy_factory is None):
                raise DirectiveFailedError(
                    f"directive {run_id}: interact mode needs a client_factory and a policy or policy_factory"
                    "; start factory run or factory reconcile with --interact to run interact directives"
                )
            if memory_resolver is None:
                raise DirectiveFailedError(
                    f"directive {run_id}: interact mode needs a memory_resolver"
                    "; start factory run or factory reconcile with --interact to run interact directives"
                )
            _synthesize_missing(store, run_id, cfg, directive, data_producer)
            # 清单缺失在进策略上下文(起 vLLM、占租约)之前就判
            manifests = _batch_manifests(store, run_id, cfg)
            # 解析在清单与客户端检查之后(缺了就不白跑解析器)、策略上下文之前(上下文要起服务、占租约);
            # resume_or_run 的 config_hash 在此之后算,续训时重新解出的摘要或身份一变就是 T3
            cfg = _resolve_memory(run_id, cfg, memory_resolver)
        else:
            _synthesize_missing(store, run_id, cfg, directive, data_producer)
            make_sources = _batch_sources(store, run_id, cfg)
        try:
            # ExitStack 等价于 `with scope as pol:`。interact 的上下文在 try 之内进入,所以进入时抛的异常
            # 与训练中抛的一样被下面的分支分类;退出发生在 resume_or_run 返回或抛出之后,无论哪种。
            with ExitStack() as stack:
                extra: dict = {}
                if cfg.mode == "interact":
                    scope = (
                        nullcontext(lambda episode_id, scenario_id: nullcontext(policy)) if policy is not None
                        else policy_factory(run_id, cfg)
                    )
                    make_sources = _interact_sources(
                        store, cfg, manifests, client_factory, stack.enter_context(scope), run_id
                    )
                    # 只有 interact 记推理后端;batch 不传,由执行器自己的后端记账补上(见 resume_or_run)
                    extra["policy_backend"] = _policy_backend(cfg, policy_endpoint)
                resume_or_run(
                    cfg, store=store, run_id=run_id, make_sources=make_sources,
                    attribution=attribution, executor=executor_factory(run_id), converter=converter,
                    repo_commit=repo_commit,
                    **extra,
                )
        except _DETERMINISTIC as exc:
            # NeedsHumanError 的含义是"该人工介入"(设计 §6.2、resume.py),是 T3,不是 T2;
            # _DETERMINISTIC 里其余几种都是数据/配置层面的确定性失败,仍是 T2。
            category = "T3" if isinstance(exc, NeedsHumanError) else "T2"
            raise DirectiveFailedError(
                f"directive {run_id}: {type(exc).__name__}: {exc}", category=category
            ) from exc
        except DeferredError:
            # 资源暂时不可用(真实后端设计 §7.3):不是指令的错,不论记录当时是什么状态都原样上抛,
            # 交给守护进程的延后通道;不能落进下面的兜底分支被包成终态的 DirectiveFailedError。
            raise
        except Exception as exc:
            # 编排器对 invalid_session 这类确定性的 rollout 拒绝,会先把 training_run 写成 failed 再重抛。
            # 记录已是终态,下一轮重派只会换来 NeedsHumanError,而这一轮的异常会一路逃出守护循环,
            # 所以当轮就把指令判 failed。记录仍是 pending 的瞬态异常照旧上抛。
            try:
                record = read_training_run(store, run_id)
            except NeedsHumanError as unreadable:
                # 记录在而读不出(I8):这里抛出的异常兄弟分支接不住,会一路逃出调和循环。
                # 下一轮重派也只会在入口读到同一份坏记录,所以当轮判 failed,两个原因都留下。
                raise DirectiveFailedError(
                    f"directive {run_id}: {type(exc).__name__}: {exc}; and {unreadable}", category="T3",
                ) from exc
            if record is not None and record.status == "failed":
                raise DirectiveFailedError(f"directive {run_id}: {type(exc).__name__}: {exc}") from exc
            raise

    return dispatch
