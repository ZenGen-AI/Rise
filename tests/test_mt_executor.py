import dataclasses

import pytest

from controlplane.envelope import BigRef
from mindtrainer.datamodel import RewardedTrajectory
from mindtrainer.executor import (
    CheckpointCorruptError, ExecutorNotLoadedError, MockExecutor, SimulatedCrashError,
)
from simulator.datamodel import Episode, RoleAssignment, RoleBinding, TurnRecord

BASE = BigRef("file:///ckpt/base", "a" * 64, 1)
TEACHER = BigRef("file:///ckpt/teacher", "b" * 64, 1)
GRPO_KEYS = {
    "grpo_loss", "opd_loss", "opd_weight", "total_loss", "mean_reward",
    "n_groups", "n_degenerate_groups",
}


def _episode(ep_id="e0", utterance="u0"):
    assignment = RoleAssignment(
        "s1",
        [RoleBinding("a", "pa", "self_play"),
         RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1")],
        max_turns=2,
    )
    return Episode(ep_id, "s1", assignment, [TurnRecord(0, "a", utterance), TurnRecord(1, "b", "v")], "max_turns")


def _rt(group, reward, ep_id):
    return RewardedTrajectory(_episode(ep_id), group, [reward, None], reward)


def _loaded(**kw):
    ex = MockExecutor(**kw)
    ex.load(BASE)
    return ex


def _sft(ex, i):
    """第 i 步的数据是 i 的纯函数——续训等价靠这一点。"""
    return ex.sft_step([_episode(f"e{i}", f"u{i}")], trained_slot="a")


def _grpo(ex, rewards=(("g1", 1.0), ("g1", 0.0)), coef=0.1):
    batch = [_rt(g, r, f"e{i}") for i, (g, r) in enumerate(rewards)]
    return ex.grpo_opd_step(batch, opd_coefficient=coef, teacher_model_ref=TEACHER)


# ---------------- 未加载 ----------------

@pytest.mark.parametrize(
    "call",
    [
        lambda ex: ex.sft_step([_episode()], trained_slot="a"),
        lambda ex: ex.grpo_opd_step([_rt("g", 0.5, "e")], opd_coefficient=0.1, teacher_model_ref=TEACHER),
        lambda ex: ex.save_checkpoint("t"),
    ],
    ids=["sft", "grpo_opd", "save"],
)
def test_nothing_works_before_load(call):
    with pytest.raises(ExecutorNotLoadedError):
        call(MockExecutor())


# ---------------- 指标契约 ----------------

def test_sft_step_reports_the_phase_the_global_step_and_only_sft_loss():
    ex = _loaded()
    first, second = _sft(ex, 0), _sft(ex, 1)
    assert (first.phase, first.step, second.step) == ("sft", 0, 1)
    assert set(first.metrics) == {"sft_loss"}


def test_grpo_step_reports_exactly_the_contract_metrics():
    result = _grpo(_loaded())
    assert result.phase == "grpo_opd"
    assert set(result.metrics) == GRPO_KEYS
    assert all(isinstance(v, float) for v in result.metrics.values())


def test_input_derived_metrics_are_real():
    rewards = (("g1", 1.0), ("g1", 0.0), ("g2", 0.5), ("g2", 0.5))
    m = _grpo(_loaded(), rewards, coef=0.3).metrics
    assert m["mean_reward"] == 0.5
    assert m["n_groups"] == 2.0
    assert m["n_degenerate_groups"] == 1.0  # g2 奖励全相同
    assert m["opd_weight"] == 0.3


def test_total_loss_combines_grpo_and_the_weighted_opd_loss():
    m = _grpo(_loaded(), coef=0.4).metrics
    assert m["total_loss"] == pytest.approx(m["grpo_loss"] + 0.4 * m["opd_loss"])


def test_zero_coefficient_reports_zero_weight_but_still_reports_the_opd_loss():
    m = _grpo(_loaded(), coef=0.0).metrics
    assert m["opd_weight"] == 0.0
    assert m["opd_loss"] > 0
    assert m["total_loss"] == m["grpo_loss"]


# ---------------- 哈希链 ----------------

def test_same_data_same_state_across_independent_executors():
    a, b = _loaded(), _loaded()
    for i in range(3):
        _sft(a, i), _sft(b, i)
    assert a.state_hash == b.state_hash


@pytest.mark.parametrize(
    "tamper",
    [
        lambda ex: ex.sft_step([_episode("e0", "DIFFERENT")], trained_slot="a"),
        lambda ex: ex.sft_step([_episode("e0", "u0")], trained_slot="b"),
    ],
    ids=["episode_content", "trained_slot"],
)
def test_different_data_gives_a_different_state(tamper):
    baseline, other = _loaded(), _loaded()
    _sft(baseline, 0)
    tamper(other)
    assert baseline.state_hash != other.state_hash


def test_step_order_matters():
    ab, ba = _loaded(), _loaded()
    _sft(ab, 0), _sft(ab, 1)
    ba.sft_step([_episode("e1", "u1")], trained_slot="a")
    ba.sft_step([_episode("e0", "u0")], trained_slot="a")
    assert ab.state_hash != ba.state_hash


def test_rewards_and_opd_settings_feed_the_chain():
    states = set()
    for kwargs in (
        dict(), dict(rewards=(("g1", 1.0), ("g1", 0.25))), dict(coef=0.2),
    ):
        ex = _loaded()
        _grpo(ex, **kwargs)
        states.add(ex.state_hash)
    assert len(states) == 3


# ---------------- checkpoint ----------------

def test_saved_checkpoint_carries_the_state_hash_and_the_step_count():
    ex = _loaded()
    _sft(ex, 0), _sft(ex, 1)
    ref = ex.save_checkpoint("mid")
    assert ref.sha256 == ex.state_hash and len(ref.sha256) == 64
    assert ref.uri.endswith("@2")
    assert ref.fetch_hint == {"type": "mock", "tag": "mid"}


def test_the_tag_does_not_change_the_hash():
    ex = _loaded()
    _sft(ex, 0)
    assert ex.save_checkpoint("one").sha256 == ex.save_checkpoint("two").sha256


def test_loading_a_checkpoint_restores_state_and_step_counter_exactly():
    original = _loaded()
    _sft(original, 0), _sft(original, 1)
    ref = original.save_checkpoint("mid")

    restored = MockExecutor()
    restored.load(ref)
    assert (restored.state_hash, restored.steps_done) == (original.state_hash, 2)

    assert _sft(original, 2) == _sft(restored, 2)  # 下一步的 StepResult 也一致,含 step=2
    assert original.state_hash == restored.state_hash


def test_loading_a_foreign_base_checkpoint_starts_from_step_zero():
    ex = MockExecutor()
    ex.load(BASE)
    assert (ex.state_hash, ex.steps_done) == (BASE.sha256, 0)


def test_an_inference_format_ref_is_accepted_as_a_starting_point_like_any_foreign_base():
    # 晋级后下一轮的基座是转换后的推理格式引用。mock 不区分格式,把它当外部起点;
    # 真实 ROLL 能否这样加载未核实(设计 §10 第 9 条)。
    hf = BigRef(
        "mock://ckpt/" + "c" * 64 + "@3.hf", "d" * 64, 0,
        {"type": "mock", "format": "hf", "source_sha256": "c" * 64},
    )
    ex = MockExecutor()
    ex.load(hf)
    assert (ex.state_hash, ex.steps_done) == (hf.sha256, 0)
    assert ex.checkpoint_intact(hf) is False  # 无法当作中间存点来确认
    assert _sft(ex, 0).step == 0  # 训练可以从它继续


def test_resume_equivalence_at_the_executor_level():
    straight = _loaded()
    for i in range(5):
        _sft(straight, i)

    first = _loaded()
    for i in range(3):
        _sft(first, i)
    ref = first.save_checkpoint("mid")
    second = MockExecutor()
    second.load(ref)
    for i in range(3, 5):
        _sft(second, i)
    assert second.state_hash == straight.state_hash


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: dataclasses.replace(r, sha256="f" * 64),
        lambda r: dataclasses.replace(r, uri=r.uri.rsplit("@", 1)[0]),
        lambda r: dataclasses.replace(r, uri=r.uri.rsplit("@", 1)[0] + "@x"),
        lambda r: dataclasses.replace(r, uri="mock://ckpt/garbage"),
    ],
    ids=["sha_mismatch", "no_step_suffix", "bad_step", "garbage"],
)
def test_a_corrupt_mock_checkpoint_is_rejected_and_leaves_the_executor_untouched(mutate):
    ex = _loaded()
    _sft(ex, 0)
    good = ex.save_checkpoint("t")
    before = (ex.state_hash, ex.steps_done)
    bad = mutate(good)
    assert ex.checkpoint_intact(bad) is False
    with pytest.raises(CheckpointCorruptError):
        ex.load(bad)
    assert (ex.state_hash, ex.steps_done) == before


def test_checkpoint_intact_is_true_only_for_restorable_mock_refs():
    ex = _loaded()
    _sft(ex, 0)
    assert ex.checkpoint_intact(ex.save_checkpoint("t")) is True
    assert ex.checkpoint_intact(BASE) is False  # 非 mock 方案:无法确认


# ---------------- 崩溃注入与参数校验 ----------------

def test_a_crash_is_raised_before_any_state_changes_and_can_be_cleared():
    ex = _loaded(crash_at_step=1)
    _sft(ex, 0)
    before = (ex.state_hash, ex.steps_done)
    with pytest.raises(SimulatedCrashError, match="step 1"):
        _sft(ex, 1)
    assert (ex.state_hash, ex.steps_done) == before
    ex.crash_at_step = None
    assert _sft(ex, 1).step == 1


def test_bad_arguments_are_rejected():
    ex = _loaded()
    with pytest.raises(ValueError, match="at least one episode"):
        ex.sft_step([], trained_slot="a")
    with pytest.raises(ValueError, match="at least one trajectory"):
        ex.grpo_opd_step([], opd_coefficient=0.1, teacher_model_ref=TEACHER)
    for bad in (-0.1, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="opd_coefficient"):
            _grpo(ex, coef=bad)
    with pytest.raises(ValueError, match="tag"):
        ex.save_checkpoint("")
    assert ex.steps_done == 0  # 被拒绝的调用不推进状态
