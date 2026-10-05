from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Protocol

from controlplane.envelope import BigRef
from mindtrainer.datamodel import RewardedTrajectory, StepResult
from mindtrainer.grouping import count_degenerate_groups, group_by_id
from simulator.dataset import episode_digest
from simulator.datamodel import Episode

_URI_PREFIX = "mock://ckpt/"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ExecutorNotLoadedError(RuntimeError):
    """没有 load 就训练或存点。"""


class CheckpointCorruptError(Exception):
    """checkpoint 引用无法还原(格式不对、哈希对不上)。"""


class SimulatedCrashError(RuntimeError):
    """mock 执行器按 crash_at_step 注入的崩溃,用来测试续训。"""


class TrainingExecutor(Protocol):
    """有状态的训练执行器,对应 ROLL 里持有权重的训练进程。

    GRPO 与 OPD 只有一个方法:spec §3.2 明说两者作用在同一个训练 step 里,不是先后阶段。
    StepResult.step 是执行器自己的全局 step 计数,从 0 起;load 一个中间 checkpoint
    会把计数恢复到存点时的值。

    checkpoint_intact 是对设计文档协议的补充:decide_resume 需要"能否重建该 checkpoint"
    这个事实,而只有执行器知道怎么重建。
    """

    def load(self, checkpoint_ref: BigRef) -> None: ...

    def sft_step(self, episodes: list[Episode], *, trained_slot: str) -> StepResult: ...

    def grpo_opd_step(
        self,
        rewarded: list[RewardedTrajectory],
        *,
        opd_coefficient: float,
        teacher_model_ref: BigRef,
    ) -> StepResult: ...

    def save_checkpoint(self, tag: str) -> BigRef: ...

    def checkpoint_intact(self, checkpoint_ref: BigRef) -> bool: ...


def _sha256(obj) -> str:
    text = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_inference_format(ref: BigRef) -> bool:
    """MockConverter 产出的推理格式(HF)引用。晋级链路里候选带的就是它,下一轮训练的
    基座又从它解析而来(设计 §6.1)。"""
    return bool(ref.fetch_hint) and ref.fetch_hint.get("format") == "hf"


def _parse_mock_ref(ref: BigRef) -> tuple[str, int]:
    """mock://ckpt/{state}@{steps}。state 必须等于 ref.sha256,否则视为损坏。"""
    body = ref.uri[len(_URI_PREFIX):]
    state, sep, steps = body.partition("@")
    if not sep or not steps.isdigit() or not _SHA256.match(state) or state != ref.sha256:
        raise CheckpointCorruptError(f"cannot restore from {ref.uri!r} (sha256 {ref.sha256!r})")
    return state, int(steps)


class MockExecutor:
    """确定性,不训练任何东西。

    - 来自输入的量是真的:组统计、mean_reward、opd_weight。
    - 损失值是 step 的合成函数,**不是学习证据**,只为让指标名与形状可测。
    - checkpoint 是哈希链:每步 state = sha256(上一步 state + 本步数据),BigRef 的
      sha256 就是当前 state,uri 里带步数。所以 load(ref) 无需任何文件就能还原状态,
      续训可以精确测试。tag 不进哈希,只放在 fetch_hint 里——否则两条不同路径存出的
      同一状态会因 tag 不同而哈希不同,续训等价就没法比。
    - load 一个非 mock 方案的引用(例如配置里的 base_checkpoint_ref)当作起点权重:
      state = ref.sha256,步数 0。
    - **推理格式(HF)的引用同样当作外部起点**:晋级后下一轮训练的基座正是转换后的那份
      (设计 §6.1)。真实的 ROLL 能否直接加载推理格式作为训练起点**没有核实**(设计 §10
      第 9 条);mock 在这里不区分格式,所以暴露不出该问题——这一行只是把假设写明。
    - crash_at_step:到该 step 时在改动任何状态之前抛 SimulatedCrashError。
    """

    def __init__(self, *, crash_at_step: int | None = None) -> None:
        self._state: str | None = None
        self._steps = 0
        self.crash_at_step = crash_at_step

    @property
    def state_hash(self) -> str | None:
        return self._state

    @property
    def steps_done(self) -> int:
        return self._steps

    def load(self, checkpoint_ref: BigRef) -> None:
        if checkpoint_ref.uri.startswith(_URI_PREFIX) and not _is_inference_format(checkpoint_ref):
            state, steps = _parse_mock_ref(checkpoint_ref)  # 失败时不改动当前状态
        else:
            state, steps = checkpoint_ref.sha256, 0
        self._state, self._steps = state, steps

    def checkpoint_intact(self, checkpoint_ref: BigRef) -> bool:
        """mock 方案的引用能否还原。非 mock 方案无法确认,返回 False。"""
        if not checkpoint_ref.uri.startswith(_URI_PREFIX) or _is_inference_format(checkpoint_ref):
            return False
        try:
            _parse_mock_ref(checkpoint_ref)
        except CheckpointCorruptError:
            return False
        return True

    def _advance(self, phase: str, data: dict) -> int:
        if self._state is None:
            raise ExecutorNotLoadedError("call load() before training")
        step = self._steps
        if self.crash_at_step is not None and step == self.crash_at_step:
            raise SimulatedCrashError(f"simulated crash at step {step}")
        self._state = _sha256({"prev": self._state, "phase": phase, "step": step, "data": data})
        self._steps += 1
        return step

    def sft_step(self, episodes: list[Episode], *, trained_slot: str) -> StepResult:
        if self._state is None:
            raise ExecutorNotLoadedError("call load() before training")
        if not episodes:
            raise ValueError("sft_step needs at least one episode")
        step = self._advance(
            "sft", {"episodes": [episode_digest(e) for e in episodes], "slot": trained_slot}
        )
        return StepResult(step, "sft", {"sft_loss": 1.0 / (1 + step)})

    def grpo_opd_step(
        self,
        rewarded: list[RewardedTrajectory],
        *,
        opd_coefficient: float,
        teacher_model_ref: BigRef,
    ) -> StepResult:
        if self._state is None:
            raise ExecutorNotLoadedError("call load() before training")
        if not rewarded:
            raise ValueError("grpo_opd_step needs at least one trajectory")
        if not (math.isfinite(opd_coefficient) and opd_coefficient >= 0):
            raise ValueError(f"opd_coefficient must be a finite number >= 0, got {opd_coefficient}")
        groups = {
            gid: [r.combined_reward for r in members]
            for gid, members in group_by_id(rewarded).items()
        }
        step = self._advance(
            "grpo_opd",
            {
                "rewards": [[r.episode.id, r.group_id, r.combined_reward] for r in rewarded],
                "opd": opd_coefficient,
                "teacher": teacher_model_ref.sha256,
            },
        )
        grpo_loss = 1.0 / (1 + step)
        opd_loss = 0.5 / (1 + step)  # 系数为 0 时仍然报告:opd_weight 才表示是否生效
        return StepResult(
            step,
            "grpo_opd",
            {
                "grpo_loss": grpo_loss,
                "opd_loss": opd_loss,
                "opd_weight": float(opd_coefficient),
                "total_loss": grpo_loss + opd_coefficient * opd_loss,
                "mean_reward": math.fsum(r.combined_reward for r in rewarded) / len(rewarded),
                "n_groups": float(len(groups)),
                "n_degenerate_groups": float(count_degenerate_groups(groups)),
            },
        )

    def save_checkpoint(self, tag: str) -> BigRef:
        if self._state is None:
            raise ExecutorNotLoadedError("call load() before saving")
        if not tag:
            raise ValueError("tag must be non-empty")
        return BigRef(
            uri=f"{_URI_PREFIX}{self._state}@{self._steps}",
            sha256=self._state,
            size_bytes=0,
            fetch_hint={"type": "mock", "tag": tag},
        )
