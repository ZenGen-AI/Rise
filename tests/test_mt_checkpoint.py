import hashlib
import subprocess

import pytest

from controlplane.daemon import read_tree_state, reconcile_once
from controlplane.envelope import BigRef
from controlplane.gitstore import GitStore
from controlplane.paths import candidate_checkpoint_file, diagnostic_report_file
from controlplane.reconcile import CandidateView
from mindtrainer.checkpoint import CandidateAlreadyReviewedError, MockConverter, register_candidate


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    return s


def _src():
    return BigRef("mock://ckpt/" + "1" * 64 + "@6", "1" * 64, 7, {"type": "mock", "tag": "final"})


def _register(store, run_id="run-1", **kw):
    args = dict(
        checkpoint_ref=MockConverter().convert(_src()),
        base_checkpoint_ref=BigRef("file:///ckpt/base", "a" * 64, 1),
        config_hash="c" * 64,
        curve_summary={"final_mean_reward": 0.5},
        manifest_ids=["dsm-run-1-sft", "dsm-run-1-grpo_opd"],
    )
    args.update(kw)
    return register_candidate(store, run_id=run_id, **args)


def _commits(root):
    out = subprocess.run(["git", "-C", str(root), "rev-list", "--count", "HEAD"],
                         capture_output=True, text=True, check=True)
    return int(out.stdout)


# ---------------- 转换器 ----------------

def test_conversion_yields_a_different_artifact_with_a_derived_hash():
    src, out = _src(), MockConverter().convert(_src())
    assert out.uri == src.uri + ".hf"
    assert out.sha256 == hashlib.sha256((src.sha256 + "hf").encode()).hexdigest()
    assert out.sha256 != src.sha256 and out.size_bytes == src.size_bytes
    assert out.fetch_hint == {"type": "mock", "format": "hf", "source_sha256": src.sha256}


def test_conversion_is_deterministic_and_input_dependent():
    other = BigRef("mock://ckpt/" + "2" * 64 + "@6", "2" * 64, 7)
    converter = MockConverter()
    assert converter.convert(_src()) == converter.convert(_src())
    assert converter.convert(_src()).sha256 != converter.convert(other).sha256


def test_converting_twice_is_refused_rather_than_stacking_suffixes():
    once = MockConverter().convert(_src())
    with pytest.raises(ValueError, match="already converted"):
        MockConverter().convert(once)


# ---------------- 注册 ----------------

def test_registered_candidate_has_the_shape_the_daemon_and_cli_expect(store):
    path = _register(store)
    assert path == candidate_checkpoint_file("run-1")
    env = store.read_product(path)
    assert (env.type, env.id, env.status) == ("candidate_checkpoint", "cand-run-1", "pending_review")
    assert env.links == ["run-1", "dsm-run-1-sft", "dsm-run-1-grpo_opd"]
    assert set(env.payload) == {
        "checkpoint_ref", "train_curve_summary", "training_run_ref",
        "base_checkpoint_ref", "config_hash",
    }
    assert env.payload["training_run_ref"] == "trainrun-run-1"
    assert env.payload["train_curve_summary"] == {"final_mean_reward": 0.5}


def test_the_candidate_carries_the_converted_ref_not_the_training_format(store):
    converted = MockConverter().convert(_src())
    _register(store, checkpoint_ref=converted)
    stored = store.read_product(candidate_checkpoint_file("run-1")).payload["checkpoint_ref"]
    assert BigRef.from_dict(stored) == converted
    assert stored["uri"].endswith(".hf") and stored["sha256"] != _src().sha256


def test_the_daemon_reads_it_as_a_candidate_awaiting_review(store):
    _register(store)
    assert read_tree_state(store).candidates == [
        CandidateView(id="cand-run-1", status="pending_review", run_id="run-1", has_diagnostic=False)
    ]


def test_reconcile_asks_for_a_diagnostic_without_any_change_to_the_loop(store):
    _register(store)
    actions = reconcile_once(store)
    assert [(a.kind, a.target_id) for a in actions] == [("request_diagnostic", "cand-run-1")]
    assert store.exists(diagnostic_report_file("run-1"))


def test_replaying_the_same_registration_adds_no_commit(store, tmp_path):
    _register(store)
    before = _commits(tmp_path)
    _register(store)
    assert _commits(tmp_path) == before


def test_re_registering_while_still_pending_review_updates_the_candidate(store):
    _register(store, curve_summary={"final_mean_reward": 0.5})
    _register(store, curve_summary={"final_mean_reward": 0.9})
    env = store.read_product(candidate_checkpoint_file("run-1"))
    assert env.payload["train_curve_summary"] == {"final_mean_reward": 0.9}


@pytest.mark.parametrize("reviewed", ["approved", "rejected", "effective"])
def test_a_replay_after_review_refuses_and_keeps_the_human_decision(store, reviewed):
    _register(store)
    path = candidate_checkpoint_file("run-1")
    cand = store.read_product(path)
    cand.status = reviewed
    store.write_product(path, cand, "human: decide")
    with pytest.raises(CandidateAlreadyReviewedError, match=reviewed):
        _register(store)
    assert store.read_product(path).status == reviewed


def test_the_stored_summary_keeps_the_values_from_registration_time(store):
    summary = {"curve": {"mean": 0.5}}
    _register(store, curve_summary=summary)
    summary["curve"]["mean"] = 99
    stored = store.read_product(candidate_checkpoint_file("run-1")).payload["train_curve_summary"]
    assert stored == {"curve": {"mean": 0.5}}


def test_empty_run_id_is_rejected(store):
    with pytest.raises(ValueError, match="run_id"):
        _register(store, run_id="")
