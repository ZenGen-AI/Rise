from __future__ import annotations

from collections.abc import Callable

from controlplane.daemon import DirectiveFailedError, Dispatcher
from controlplane.envelope import Envelope
from controlplane.gitstore import GitStore
from controlplane.paths import dataset_manifest_file
from mindtrainer.checkpoint import CheckpointConverter, MockConverter
from mindtrainer.executor import MockExecutor, TrainingExecutor
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
    NotEnoughGroupsError, InvalidTrainingConfigError, NeedsHumanError,
)


def _required_stages(cfg: TrainingConfig) -> list[str]:
    """真正会用到的阶段:配了步数的才算。stage_manifest_refs 里多写的阶段不读。"""
    wanted = []
    if cfg.sft_steps > 0:
        wanted.append("sft")
    if cfg.grpo_steps > 0:
        wanted.append("grpo_opd")
    return wanted


def _data_run_id(run_id: str, ref: str, stage: str) -> str:
    """清单 id 的格式是 dsm-{数据所在的 run}-{阶段}(build_manifest 是唯一的构造点)。后一轮训练
    常常复用前一轮的数据集,所以数据所在的 run 从 id 里解出来,不假设它等于指令自己的 run。"""
    head, tail = "dsm-", f"-{stage}"
    if not (ref.startswith(head) and ref.endswith(tail) and len(ref) > len(head) + len(tail)):
        raise DirectiveFailedError(
            f"directive {run_id}: stage_manifest_refs[{stage!r}]={ref!r} is not a manifest id "
            f"of the form 'dsm-<run>-{stage}'"
        )
    return ref[len(head):-len(tail)]


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
        manifest = read_manifest(store, data_run, name)
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
    executor_factory: Callable[[], TrainingExecutor] = MockExecutor,
    converter: CheckpointConverter | None = None,
    client_factory: Callable[[], object] | None = None,
    policy: ModelBackend | None = None,
) -> Dispatcher:
    """构造 MindTrainer 派发器。它只做一件事:把一条 pending 的 directive 跑成候选。

    起点与续训交给 resume_or_run:指令若在上一轮崩在中途,directive 仍是 pending,
    下一轮重新派发时从 training_run 的存点接着跑,不从头再来。

    - batch:按 stage_manifest_refs 读数据集清单。
    - interact:另需 client_factory 与 policy,注入后 grpo_opd 走实时会话。
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
            make_sources = _interact_sources(store, run_id, cfg, client_factory, policy)
        else:
            make_sources = _batch_sources(store, run_id, cfg)
        try:
            resume_or_run(
                cfg, store=store, run_id=run_id, make_sources=make_sources,
                attribution=attribution, executor=executor_factory(), converter=converter,
            )
        except _DETERMINISTIC as exc:
            raise DirectiveFailedError(f"directive {run_id}: {type(exc).__name__}: {exc}") from exc
        except Exception as exc:
            # 编排器对 invalid_session 这类确定性的 rollout 拒绝,会先把 training_run 写成 failed 再重抛。
            # 记录已是终态,下一轮重派只会换来 NeedsHumanError,而这一轮的异常会一路逃出守护循环,
            # 所以当轮就把指令判 failed。记录仍是 pending 的瞬态异常照旧上抛。
            record = read_training_run(store, run_id)
            if record is not None and record.status == "failed":
                raise DirectiveFailedError(f"directive {run_id}: {type(exc).__name__}: {exc}") from exc
            raise

    return dispatch
