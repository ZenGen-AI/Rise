from __future__ import annotations

from dataclasses import dataclass

from controlplane.envelope import BigRef
from mindtrainer.datamodel import TrainingRun
from mindtrainer.training_config import TrainingConfig

ACTIONS = ("noop", "resume", "restart", "escalate")


class NeedsHumanError(Exception):
    """escalate 的落点(T3,需人工)。reason 说明为什么不能自动续训。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ResumeDecision:
    action: str  # ACTIONS 之一
    reason: str
    checkpoint_ref: BigRef | None = None  # 仅 resume 有值
    from_step: int = 0  # resume 时为 checkpoint_step;restart 为 0


def decide_resume(
    run: TrainingRun, config: TrainingConfig, *, checkpoint_intact: bool | None
) -> ResumeDecision:
    """续训判定,纯函数。按下面的顺序,先命中者生效:

    1. executed/consumed -> noop
    2. failed -> escalate(失败已落盘,不自动重跑;补充 E.3 机制三)
    3. resume_attempts >= max_resume_attempts -> escalate(否则每次续训都在同一步崩溃会无限循环)
    4. allow_resume 为假 -> escalate
    5. 有 last_checkpoint_ref:checkpoint_intact 为 True -> resume;False 或 None(无法确认)
       -> escalate,按中间态损坏处理
    6. 没有任何 checkpoint(崩在第一次存点之前) -> restart,从 base_checkpoint_ref 的 step 0 重来

    第 6 条是设计里的解读:spec 没覆盖"还没存过点",这时什么都没丢,从头跑等价于新运行,
    判 T3 反而让人去处理一件没坏的事。
    checkpoint_intact 由调用方算(来自执行器能否重建该 checkpoint):计算层算好传进来,
    控制层只判"该不该续、从哪续"。没有 checkpoint 时它被忽略。
    调用方每次 resume 或 restart 之后要把 resume_attempts 加一,第 3 条才会生效。"""
    if run.status in ("executed", "consumed"):
        return ResumeDecision("noop", f"run {run.run_id} is already {run.status}")
    if run.status == "failed":
        return ResumeDecision(
            "escalate",
            f"run {run.run_id} failed deterministically and is kept; it is not rerun automatically",
        )
    if run.status != "pending":
        raise ValueError(f"unknown training_run status {run.status!r}")
    if run.resume_attempts >= config.max_resume_attempts:
        return ResumeDecision(
            "escalate",
            f"resume attempts exhausted ({run.resume_attempts} >= {config.max_resume_attempts})",
        )
    if not config.allow_resume:
        return ResumeDecision("escalate", "resume is not allowed by the config (allow_resume=false)")
    if run.last_checkpoint_ref is not None:
        if checkpoint_intact is True:
            return ResumeDecision(
                "resume",
                f"resume from the checkpoint at step {run.checkpoint_step}",
                checkpoint_ref=run.last_checkpoint_ref,
                from_step=run.checkpoint_step,
            )
        if checkpoint_intact is False:
            return ResumeDecision(
                "escalate",
                f"the checkpoint at step {run.checkpoint_step} failed its integrity check",
            )
        return ResumeDecision(
            "escalate",
            f"cannot confirm the checkpoint at step {run.checkpoint_step} is intact",
        )
    return ResumeDecision("restart", "no checkpoint was ever saved; restart from the base checkpoint")
