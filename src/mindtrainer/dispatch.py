from __future__ import annotations

from collections.abc import Callable

import yaml

from controlplane.daemon import DirectiveFailedError, Dispatcher
from controlplane.deferred import DeferredError
from controlplane.envelope import Envelope
from controlplane.gitstore import GitStore
from controlplane.migration import UnknownSchemaVersionError
from controlplane.paths import dataset_manifest_file
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
from mindtrainer.reward_pipeline import (
    AttributionError, AttributionBackend, build_attribution_backend,
)
from mindtrainer.store_adapter import read_training_run
from mindtrainer.training_config import (
    InvalidTrainingConfigError, TrainingConfig, load_config_from_directive, validate_training_config,
)
from mindtrainer.trajectory_source import (
    BatchTrajectorySource, InteractTrajectorySource, TrajectorySource,
)
from simulator.backends import ModelBackend
from simulator.dataset import DatasetIntegrityError, DatasetManifest, read_manifest

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
        except (KeyError, TypeError, AttributeError, yaml.YAMLError, UnknownSchemaVersionError) as exc:
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
    run_id: str,
    cfg: TrainingConfig,
    client_factory: Callable[[], object],
    policy: ModelBackend,
) -> Callable[[int], dict[str, TrajectorySource]]:
    """interact 的 grpo_opd 来自实时会话,只有 sft 用清单;清单缺失同样在这里先判。"""
    manifests = _batch_manifests(store, run_id, cfg)

    def make_sources(start: int) -> dict[str, TrajectorySource]:
        sources: dict[str, TrajectorySource] = {
            name: BatchTrajectorySource(store, m) for name, m in manifests.items()
        }
        if cfg.grpo_steps > 0:
            sources["grpo_opd"] = InteractTrajectorySource(
                client_factory, policy, store, cfg, start_step=start
            )
        return sources

    return make_sources


def make_mindtrainer_dispatcher(
    *,
    executor_factory: Callable[[str], TrainingExecutor] = lambda run_id: MockExecutor(),
    converter: CheckpointConverter | None = None,
    client_factory: Callable[[], object] | None = None,
    policy: ModelBackend | None = None,
    data_producer: DataProducer | None = None,
) -> Dispatcher:
    """构造 MindTrainer 派发器。它只做一件事:把一条 pending 的 directive 跑成候选。

    起点与续训交给 resume_or_run:指令若在上一轮崩在中途,directive 仍是 pending,
    下一轮重新派发时从 training_run 的存点接着跑,不从头再来。

    - batch:按 stage_manifest_refs 读数据集清单。
    - interact:另需 client_factory 与 policy,注入后 grpo_opd 走实时会话。
    - data_producer:指令带 data_spec 且本 run 名下的清单缺失时,用它先合成数据(两种模式都可,
      interact 只合成 sft)。不带 data_spec 的指令行为与改动前完全相同;带 data_spec 的指令总是
      先严格解析,写错的判 failed;需要合成而没有注入生产器时判 failed 并写明缺什么。
    - 确定性失败(数据不对、配置不合法、该人工介入)抛 DirectiveFailedError,调和循环把
      directive 置 failed,不再重试;其余异常原样上抛,保持 pending 等下一轮。
    """
    converter = converter or MockConverter()

    def dispatch(store: GitStore, directive: Envelope) -> None:
        run_id = directive.id
        try:
            cfg = load_config_from_directive(directive)
        except InvalidTrainingConfigError as exc:
            raise DirectiveFailedError(f"directive {run_id}: {exc}") from exc
        reason = validate_training_config(cfg)
        if reason is not None:
            raise DirectiveFailedError(f"directive {run_id}: {reason}")
        try:
            # 本入口不提供 judge:judge 归因的指令做不成,重试无益,所以判失败而不是让 ValueError 逃出去
            attribution: AttributionBackend = build_attribution_backend(cfg.reward)
        except ValueError as exc:
            raise DirectiveFailedError(f"directive {run_id}: {exc}") from exc
        if cfg.mode == "interact":
            if client_factory is None or policy is None:
                raise DirectiveFailedError(
                    f"directive {run_id}: interact mode needs a client_factory and a policy"
                )
            _synthesize_missing(store, run_id, cfg, directive, data_producer)
            make_sources = _interact_sources(store, run_id, cfg, client_factory, policy)
        else:
            _synthesize_missing(store, run_id, cfg, directive, data_producer)
            make_sources = _batch_sources(store, run_id, cfg)
        try:
            resume_or_run(
                cfg, store=store, run_id=run_id, make_sources=make_sources,
                attribution=attribution, executor=executor_factory(run_id), converter=converter,
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
