from __future__ import annotations

import copy
import dataclasses
from typing import Callable

from controlplane.envelope import BigRef
from controlplane.gitstore import GitStore
from mindtrainer.checkpoint import CheckpointConverter, register_candidate
from mindtrainer.datamodel import StepResult, TrainingRun
from mindtrainer.executor import TrainingExecutor
from mindtrainer.grouping import DegenerateGroupError, group_by_id, validate_groups
from mindtrainer.resume import NeedsHumanError, decide_resume
from mindtrainer.reward_pipeline import (
    AttributionBackend, AttributionError, run_reward_pipeline, summarize_rewards,
)
from mindtrainer.store_adapter import read_training_run, write_training_run
from mindtrainer.training_config import (
    STAGES, InvalidTrainingConfigError, TrainingConfig, config_hash, phase_at,
    total_steps, validate_training_config,
)
from mindtrainer.trajectory_source import TrajectorySource, batch_for_step
from simulator.dataset import DatasetIntegrityError


class NotEnoughGroupsError(ValueError):
    """Batch 数据集的组数少于 groups_per_step。继续的话 batch_for_step 回绕,同一个组会在
    一步里出现两次,成员被重复计入——静默地扭曲组内基线,所以是错误。"""


# 这几类是确定性失败(补充 E.2 的 T2):同样的输入还会再失败,写 failed 并保留,不进续训。
# 其余异常一律不吞,run 保持 pending,交给 decide_resume 判定。
_DETERMINISTIC = (
    DatasetIntegrityError, DegenerateGroupError, AttributionError,
    NotEnoughGroupsError, InvalidTrainingConfigError,
)

# start_grpo_step -> {"sft": 源, "grpo_opd": 源}。用工厂而不是现成的源,因为续训时
# Interact 源要从正确的 scenario 轮转位置起步(start_step),而这取决于续训点。
SourcesFactory = Callable[[int], dict[str, TrajectorySource]]


def _failed(base: TrainingRun, point: str, exc: Exception) -> TrainingRun:
    return dataclasses.replace(
        base,
        status="failed",
        failure={"category": "T2", "point": point, "detail": f"{type(exc).__name__}: {exc}"},
    )


def _prepare_batch_grpo(cfg, source, attribution):
    """Batch 的 GRPO 数据是固定的,所以整份数据集只算一次奖励、只校验一次。
    在训练开始前就做(而不是等 SFT 跑完):数据有问题时一步都不浪费。"""
    batch = source.collect()
    rewarded = run_reward_pipeline(batch, cfg.reward, attribution, trained_slot=cfg.trained_slot)
    groups = group_by_id(rewarded)
    validate_groups(groups, cfg.min_group_size)
    if cfg.groups_per_step > len(groups):
        raise NotEnoughGroupsError(
            f"groups_per_step {cfg.groups_per_step} exceeds the {len(groups)} group(s) in the dataset"
        )
    return groups


def _update_summary(summary: dict, result: StepResult, reward_mean: float | None, failed: int) -> None:
    summary["failed_rollout_attempts"] = summary.get("failed_rollout_attempts", 0) + failed
    if result.phase == "sft":
        summary["sft_steps_done"] = summary.get("sft_steps_done", 0) + 1
        summary["final_sft_loss"] = result.metrics["sft_loss"]
        return
    summary["grpo_steps_done"] = summary.get("grpo_steps_done", 0) + 1
    summary.setdefault("first_mean_reward", reward_mean)
    summary["final_mean_reward"] = reward_mean
    summary["final_grpo_loss"] = result.metrics["grpo_loss"]
    summary["final_opd_weight"] = result.metrics["opd_weight"]
    summary["degenerate_groups_total"] = (
        summary.get("degenerate_groups_total", 0) + int(result.metrics["n_degenerate_groups"])
    )


def run_training(
    cfg: TrainingConfig,
    *,
    store: GitStore,
    run_id: str,
    make_sources: SourcesFactory,
    attribution: AttributionBackend,
    executor: TrainingExecutor,
    converter: CheckpointConverter,
    resume_from: TrainingRun | None = None,
    metric_stream_refs: list[BigRef] | None = None,
    on_step: Callable[[StepResult], None] | None = None,
) -> TrainingRun:
    """薄编排:不含判定逻辑,只调下层纯函数和协议(沿用 run_diagnostic 的做法)。

    - 新运行从 base_checkpoint_ref 的 step 0 起;resume_from 带中间 checkpoint 则从
      checkpoint_step 起,不带(崩在第一次存点之前)则从头重来。两种情况都先把
      resume_attempts 加一并落盘,再动执行器——这样即便 load 本身反复失败也会触顶升级。
    - 数据是 step 的纯函数(Batch):SFT 取 batch_for_step(episodes, step, sft_batch_size);
      GRPO 取 batch_for_step(组 id, grpo 序号, groups_per_step) 个完整组。所以"一次跑完"
      与"崩溃后从存点续"逐字节等价。Interact 的 rollout 是实时的,不声称等价。
    - 每 checkpoint_every_steps 步存点,并把引用、checkpoint_step 与当时的曲线摘要一起回写。
      曲线摘要只在存点时落盘,所以续训后它与存点时一致,不会重复计入崩溃前多跑的步。
    - 收尾:存最终 checkpoint(先回写 pending,再转换),注册候选,置 executed。
    - 确定性失败写 failed(category T2)并原样重抛;其余异常不吞,run 保持 pending。
    - metric_stream_refs 与 on_step:逐步指标不进 git(补充 D.2),on_step 是调用方观察
      每一步的唯一出口;指标流文件本身由调用方写,只把引用交给这里记录。
    """
    chash = config_hash(cfg)
    if resume_from is not None:
        if resume_from.run_id != run_id:
            raise ValueError(f"resume_from belongs to {resume_from.run_id!r}, not {run_id!r}")
        if resume_from.status != "pending":
            raise ValueError(f"only a pending run can be resumed, got {resume_from.status!r}")
        if resume_from.config_hash != chash:
            raise NeedsHumanError(
                f"config changed since run {run_id} started "
                f"({resume_from.config_hash[:12]} -> {chash[:12]}); resuming would not be reproducible"
            )
    base = resume_from or TrainingRun(run_id, chash)

    reason = validate_training_config(cfg)
    if reason is not None:
        write_training_run(store, _failed(base, "config", InvalidTrainingConfigError(reason)))
        raise InvalidTrainingConfigError(reason)

    # 起点:纯计算,先不写任何东西
    if resume_from is None:
        run, start, from_base = base, 0, True
    elif base.last_checkpoint_ref is not None:
        run = dataclasses.replace(base, resume_attempts=base.resume_attempts + 1)
        start, from_base = base.checkpoint_step, False
    else:
        run = dataclasses.replace(
            base, resume_attempts=base.resume_attempts + 1, checkpoint_step=0, curve_summary={}
        )
        start, from_base = 0, True
    if metric_stream_refs is not None:
        run = dataclasses.replace(run, metric_stream_refs=list(metric_stream_refs))

    sources = make_sources(max(0, start - cfg.sft_steps))
    for stage, steps in (("sft", cfg.sft_steps), ("grpo_opd", cfg.grpo_steps)):
        if steps > 0 and stage not in sources:
            raise ValueError(f"make_sources() returned no {stage!r} source but {steps} {stage} step(s) are configured")

    write_training_run(store, run)  # 先落盘本次尝试,再动执行器
    executor.load(cfg.base_checkpoint_ref if from_base else run.last_checkpoint_ref)

    summary = copy.deepcopy(run.curve_summary)
    total = total_steps(cfg)
    sft_episodes = None
    batch_groups = None
    point = "start"
    try:
        if cfg.mode == "batch" and cfg.grpo_steps > 0:
            point = "grpo precheck"
            batch_groups = _prepare_batch_grpo(cfg, sources["grpo_opd"], attribution)
        for step in range(start, total):
            phase = phase_at(step, cfg)
            point = f"step {step} ({phase})"
            reward_mean, failed = None, 0
            if phase == "sft":
                if sft_episodes is None:
                    sft_episodes = sources["sft"].collect().episodes
                chosen = batch_for_step(sft_episodes, step, cfg.sft_batch_size)
                result = executor.sft_step(chosen, trained_slot=cfg.trained_slot)
            else:
                grpo_index = step - cfg.sft_steps
                if cfg.mode == "batch":
                    gids = batch_for_step(list(batch_groups), grpo_index, cfg.groups_per_step)
                    rewarded = [r for gid in gids for r in batch_groups[gid]]
                else:
                    batch = sources["grpo_opd"].collect()
                    rewarded = run_reward_pipeline(
                        batch, cfg.reward, attribution, trained_slot=cfg.trained_slot
                    )
                    validate_groups(group_by_id(rewarded), cfg.min_group_size)
                    failed = len(batch.metadata.get("failed_attempts", []))
                reward_mean = summarize_rewards(rewarded)["mean"]
                result = executor.grpo_opd_step(
                    rewarded, opd_coefficient=cfg.opd_coefficient,
                    teacher_model_ref=cfg.teacher_model_ref,
                )
            if result.step != step:
                raise RuntimeError(
                    f"executor reports step {result.step} but the orchestrator is at step {step}: "
                    f"executor and run record are out of sync"
                )
            _update_summary(summary, result, reward_mean, failed)
            if on_step is not None:
                on_step(result)
            done = step + 1
            if done < total and done % cfg.checkpoint_every_steps == 0:
                ref = executor.save_checkpoint(f"step-{done}")
                run = dataclasses.replace(
                    run, checkpoint_step=done, last_checkpoint_ref=ref,
                    curve_summary=copy.deepcopy(summary),
                )
                write_training_run(store, run)
    except _DETERMINISTIC as exc:
        write_training_run(store, _failed(run, point, exc))
        raise

    final_ref = executor.save_checkpoint("final")
    run = dataclasses.replace(
        run, checkpoint_step=total, last_checkpoint_ref=final_ref,
        curve_summary=copy.deepcopy(summary),
    )
    write_training_run(store, run)  # 先记下最终存点:转换或注册崩了,续训直接回到这里收尾
    register_candidate(
        store,
        run_id=run_id,
        checkpoint_ref=converter.convert(final_ref),
        base_checkpoint_ref=cfg.base_checkpoint_ref,
        config_hash=chash,
        curve_summary=summary,
        manifest_ids=[cfg.stage_manifest_refs[s] for s in STAGES if cfg.stage_manifest_refs.get(s)],
    )
    run = dataclasses.replace(run, status="executed")
    write_training_run(store, run)
    return run


def resume_or_run(
    cfg: TrainingConfig,
    *,
    store: GitStore,
    run_id: str,
    make_sources: SourcesFactory,
    attribution: AttributionBackend,
    executor: TrainingExecutor,
    converter: CheckpointConverter,
    metric_stream_refs: list[BigRef] | None = None,
    on_step: Callable[[StepResult], None] | None = None,
) -> TrainingRun:
    """入口:读已有的 training_run,交给 decide_resume 判定。
    noop 直接返回已有记录;resume / restart 带记录进入 run_training;
    escalate 抛 NeedsHumanError(带原因)。没有记录就是新运行。"""
    existing = read_training_run(store, run_id)
    kwargs = dict(
        store=store, run_id=run_id, make_sources=make_sources, attribution=attribution,
        executor=executor, converter=converter,
        metric_stream_refs=metric_stream_refs, on_step=on_step,
    )
    if existing is None:
        return run_training(cfg, **kwargs)
    intact = (
        executor.checkpoint_intact(existing.last_checkpoint_ref)
        if existing.last_checkpoint_ref is not None else None
    )
    decision = decide_resume(existing, cfg, checkpoint_intact=intact)
    if decision.action == "noop":
        return existing
    if decision.action == "escalate":
        raise NeedsHumanError(decision.reason)
    return run_training(cfg, resume_from=existing, **kwargs)
