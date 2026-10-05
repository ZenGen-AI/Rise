import dataclasses

import pytest

from controlplane.envelope import BigRef
from mindtrainer.datamodel import TrainingRun
from mindtrainer.resume import ACTIONS, NeedsHumanError, ResumeDecision, decide_resume
from mindtrainer.training_config import RewardConfig, TrainingConfig

CKPT = BigRef("mock://ckpt/" + "9" * 64 + "@4", "9" * 64, 0)


def _cfg(**kw):
    cfg = TrainingConfig(
        mode="batch", trained_slot="a",
        base_checkpoint_ref=BigRef("file:///ckpt/a", "a" * 64, 1),
        teacher_model_ref=BigRef("file:///ckpt/b", "b" * 64, 1),
        reward=RewardConfig(["goal"], "outcome_split", "sum", {"goal": ["agreed"]}),
        sft_steps=2, grpo_steps=4, sft_batch_size=2, groups_per_step=2, group_size=2,
        grpo_config={}, opd_coefficient=0.1, checkpoint_every_steps=2, allow_resume=True,
        stage_manifest_refs={"sft": "m1", "grpo_opd": "m2"},
    )
    return dataclasses.replace(cfg, **kw)


def _run(**kw):
    base = dict(run_id="run-1", config_hash="c" * 64)
    base.update(kw)
    return TrainingRun(**base)


def _checkpointed(**kw):
    return _run(checkpoint_step=4, last_checkpoint_ref=CKPT, **kw)


def _decide(run, cfg=None, intact=True):
    return decide_resume(run, cfg or _cfg(), checkpoint_intact=intact)


# ---------------- 1. 已结束 ----------------

@pytest.mark.parametrize("status", ["executed", "consumed"])
def test_a_finished_run_is_left_alone_even_if_everything_else_looks_bad(status):
    run = _checkpointed(status=status, resume_attempts=99)
    decision = _decide(run, _cfg(allow_resume=False), intact=False)
    assert decision.action == "noop" and "already" in decision.reason


# ---------------- 2. 失败 ----------------

def test_a_failed_run_escalates_and_is_never_rerun_automatically():
    run = _checkpointed(status="failed", failure={"category": "T2"})
    decision = _decide(run)
    assert decision.action == "escalate" and "failed deterministically" in decision.reason
    assert decision.checkpoint_ref is None


# ---------------- 3. 尝试次数 ----------------

def test_attempts_exactly_at_the_limit_escalate_and_one_below_still_resumes():
    cfg = _cfg(max_resume_attempts=3)
    at_limit = _decide(_checkpointed(resume_attempts=3), cfg)
    below = _decide(_checkpointed(resume_attempts=2), cfg)
    assert (at_limit.action, below.action) == ("escalate", "resume")
    assert "exhausted" in at_limit.reason and "3 >= 3" in at_limit.reason


def test_zero_allowed_attempts_escalates_even_a_run_with_no_checkpoint():
    decision = _decide(_run(), _cfg(max_resume_attempts=0))
    assert decision.action == "escalate" and "exhausted" in decision.reason


def test_exhausted_attempts_are_reported_before_the_resume_ban():
    decision = _decide(_checkpointed(resume_attempts=3), _cfg(max_resume_attempts=3, allow_resume=False))
    assert "exhausted" in decision.reason


# ---------------- 4. 不允许续训 ----------------

@pytest.mark.parametrize("run", [_checkpointed(), _run()], ids=["with_checkpoint", "no_checkpoint"])
def test_resume_ban_escalates_even_when_a_restart_would_otherwise_be_possible(run):
    decision = _decide(run, _cfg(allow_resume=False))
    assert decision.action == "escalate" and "allow_resume" in decision.reason


# ---------------- 5. 有 checkpoint ----------------

def test_an_intact_checkpoint_resumes_from_its_step_with_its_ref():
    decision = _decide(_checkpointed(), intact=True)
    assert decision == ResumeDecision(
        "resume", "resume from the checkpoint at step 4", checkpoint_ref=CKPT, from_step=4
    )
    assert decision.checkpoint_ref is not None and decision.checkpoint_ref == CKPT


def test_a_checkpoint_that_fails_the_check_escalates_as_corruption():
    decision = _decide(_checkpointed(), intact=False)
    assert decision.action == "escalate" and "integrity" in decision.reason


def test_an_unconfirmable_checkpoint_escalates_instead_of_guessing():
    decision = _decide(_checkpointed(), intact=None)
    assert decision.action == "escalate" and "cannot confirm" in decision.reason


# ---------------- 6. 没有 checkpoint ----------------

@pytest.mark.parametrize("intact", [True, False, None])
def test_a_run_that_never_saved_restarts_from_the_base_whatever_the_flag_says(intact):
    decision = _decide(_run(), intact=intact)
    assert decision.action == "restart"
    assert (decision.checkpoint_ref, decision.from_step) == (None, 0)


# ---------------- 其余 ----------------

def test_an_unknown_status_is_an_error_not_a_guess():
    with pytest.raises(ValueError, match="unknown training_run status 'weird'"):
        _decide(_run(status="weird"))


def test_every_action_returned_is_a_declared_action():
    runs = [_run(status="executed"), _run(status="failed", failure={"category": "T2"}),
            _run(), _checkpointed(), _checkpointed(resume_attempts=9)]
    for run in runs:
        for intact in (True, False, None):
            assert _decide(run, intact=intact).action in ACTIONS


def test_the_decision_is_pure_and_frozen():
    run = _checkpointed(resume_attempts=1)
    snapshot = run.to_dict()
    first, second = _decide(run), _decide(run)
    assert first == second and run.to_dict() == snapshot
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.action = "noop"


def test_the_intactness_flag_cannot_be_left_out():
    with pytest.raises(TypeError):
        decide_resume(_run(), _cfg())


def test_needs_human_error_carries_the_reason():
    err = NeedsHumanError("attempts exhausted")
    assert err.reason == "attempts exhausted" and str(err) == "attempts exhausted"
