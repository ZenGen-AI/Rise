import pytest

from controlplane.gitstore import GitStore
from controlplane.paths import CONTROL_PLANE_ROOT, proposal_file
from mindagent.analysis import BehaviorFinding
from mindagent.packaging import PROPOSAL_TYPE, Thresholds, package, proposal_id_for, screen, stale_proposals
from mindagent.proposals import propose
from mindagent.verify import Verification

ROUND = "agent-round-zengen-v3"
TH = Thresholds(min_fired_on_motivating=0.8, max_fired_elsewhere=0.2)


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    return s


def _finding(signal):
    return BehaviorFinding(f"finding-{signal}-x", signal, f"p-{signal}", 3, 0.75, ("e0", "e1", "e2"))


def _v(on, elsewhere):
    return Verification("replay_behavior", ("e0", "e1", "e2"), on, elsewhere, "notes")


def _pack(store, findings, verifications):
    drafts = propose(findings)
    return package(store, ROUND, findings, drafts, verifications, thresholds=TH,
                   run_id="run-003", bound_model_id="zengen-v3")


def test_a_passing_draft_becomes_a_pending_review_proposal_with_the_spec_payload(store):
    f = _finding("emotion_high")
    (env,) = _pack(store, [f], [_v(1.0, 0.0)])
    assert env.id == "proposal-agent-round-zengen-v3-1" == proposal_id_for(ROUND, 1)
    assert (env.type, env.status) == (PROPOSAL_TYPE, "pending_review")
    assert env.links == [ROUND, f.id]
    on_disk = store.read_product(proposal_file(env.id))
    assert on_disk.to_dict() == env.to_dict()
    p = on_disk.payload
    assert p["round_id"] == ROUND and p["run_id"] == "run-003" and p["bound_model_id"] == "zengen-v3"
    assert p["finding"] == {"id": f.id, "pattern": "p-emotion_high", "support": 3, "ratio": 0.75,
                            "episode_refs": ["e0", "e1", "e2"]}
    (inc,) = p["increments"]
    assert inc["kind"] == "skill" and inc["definition"]["trigger"] == {"signal": "emotion_high"}
    assert p["verification"] == {"method": "replay_behavior", "episodes": ["e0", "e1", "e2"],
                                 "fired_on_motivating": 1.0, "fired_elsewhere": 0.0, "notes": "notes"}


def test_failing_drafts_are_not_packaged_and_numbering_stays_dense(store):
    fs = [_finding("a_sig"), _finding("b_sig"), _finding("c_sig")]
    vs = [_v(0.5, 0.0), _v(1.0, 0.0), _v(1.0, 0.5)]
    envs = _pack(store, fs, vs)
    assert [e.id for e in envs] == ["proposal-agent-round-zengen-v3-1"]
    assert envs[0].payload["finding"]["id"] == "finding-b_sig-x"
    kept, dropped = screen(propose(fs), vs, TH)
    assert kept == [1]
    assert [(d["finding_id"], d["reason"]) for d in dropped] == [
        ("finding-a_sig-x", "fired_on_motivating 0.500 < 0.8"),
        ("finding-c_sig-x", "fired_elsewhere 0.500 > 0.2"),
    ]


def test_the_thresholds_are_inclusive(store):
    kept, dropped = screen(propose([_finding("a_sig")]), [_v(0.8, 0.2)], TH)
    assert kept == [0] and dropped == []


def test_repackaging_the_same_drafts_does_not_rewrite_or_duplicate(store):
    f = _finding("emotion_high")
    first = _pack(store, [f], [_v(1.0, 0.0)])
    head = store._git("rev-parse", "HEAD")
    again = _pack(store, [f], [_v(1.0, 0.0)])
    assert [e.to_dict() for e in again] == [e.to_dict() for e in first]
    assert store._git("rev-parse", "HEAD") == head
    assert len(list((store.repo_root / f"{CONTROL_PLANE_ROOT}/skill_memory_proposals").iterdir())) == 1


def test_a_reviewed_proposal_is_never_overwritten(store):
    f = _finding("emotion_high")
    (env,) = _pack(store, [f], [_v(1.0, 0.0)])
    env.status = "approved"
    store.write_product(proposal_file(env.id), env, "human: approve")
    (again,) = _pack(store, [f], [_v(0.9, 0.0)])
    assert again.status == "approved"
    assert store.read_product(proposal_file(env.id)).payload["verification"]["fired_on_motivating"] == 1.0


def test_a_pending_proposal_with_a_changed_payload_is_rewritten_in_place(store):
    f = _finding("emotion_high")
    _pack(store, [f], [_v(1.0, 0.0)])
    (again,) = _pack(store, [f], [_v(0.9, 0.0)])
    assert store.read_product(proposal_file(again.id)).payload["verification"]["fired_on_motivating"] == 0.9


def test_mismatched_lengths_are_rejected(store):
    with pytest.raises(ValueError, match="1 drafts but 0 verifications"):
        screen(propose([_finding("a_sig")]), [], TH)


def test_nothing_to_package_writes_nothing(store):
    assert package(store, ROUND, [], [], [], thresholds=TH, run_id="run-003", bound_model_id="zengen-v3") == []
    assert list((store.repo_root / f"{CONTROL_PLANE_ROOT}/skill_memory_proposals").iterdir()) == []


def test_stale_proposals_reports_pending_ones_numbered_past_kept_and_skips_reviewed_ones(store):
    assert stale_proposals(store, ROUND, 0) == []                      # 还没有提案目录
    fs = [_finding("a_sig"), _finding("b_sig"), _finding("c_sig")]
    envs = _pack(store, fs, [_v(1.0, 0.0)] * 3)
    assert [e.id for e in envs] == [proposal_id_for(ROUND, n) for n in (1, 2, 3)]
    reviewed = store.read_product(proposal_file(proposal_id_for(ROUND, 3)))
    reviewed.status = "approved"
    store.write_product(proposal_file(reviewed.id), reviewed, "review")
    other = [_finding("other")]
    package(store, f"{ROUND}0", other, propose(other), [_v(1.0, 0.0)], thresholds=TH,
            run_id="run-003", bound_model_id="zengen-v3")              # 另一轮(id 以本轮 id 起头)的提案不算
    assert store.exists(proposal_file(f"proposal-{ROUND}0-1"))
    assert stale_proposals(store, ROUND, 3) == []
    stale = stale_proposals(store, ROUND, 1)
    assert [d["proposal_id"] for d in stale] == [proposal_id_for(ROUND, 2)]   # 3 号已审过,不报
    assert "stale" in stale[0]["reason"]
