import dataclasses

import pytest

from controlplane.gitstore import GitStore
from controlplane.paths import episode_file
from mindtrainer.trajectory_source import BatchTrajectorySource, batch_for_step
from simulator.backends import ScriptedBackend
from simulator.dataset import (
    DatasetIntegrityError, DatasetManifest, episode_group_id, produce_grouped_dataset,
    read_manifest,
)
from simulator.datamodel import Persona, RoleAssignment, RoleBinding, RoleSlot, Scenario
from simulator.store_adapter import read_episode, write_episode


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    return s


def _bindings():
    return [
        RoleBinding("a", "pa", "self_play"),
        RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1"),
    ]


def _scenario(sid):
    return Scenario(sid, "bg", [RoleSlot("a", "a"), RoleSlot("b", "b")], "c", ["n"], "training")


def _personas():
    return {pid: Persona(pid, ["t"], "m", "s", ["v"]) for pid in ("pa", "pb")}


def _scenarios(*ids):
    return [(_scenario(s), RoleAssignment(s, _bindings(), max_turns=4)) for s in ids]


def _varied(scenario, k):
    return {
        "a": ScriptedBackend({"a": [f"{scenario.id}-a{k}-1", f"{scenario.id}-a{k}-2"]}),
        "b": ScriptedBackend({"b": [f"{scenario.id}-b{k}-1", f"{scenario.id}-b{k}-2"]}),
    }


def _produce(store, name="grpo_opd", group_size=2, ids=("s1", "s2")):
    return produce_grouped_dataset(
        store, "run-1", name, scenarios=_scenarios(*ids), personas=_personas(),
        backends_for_rollout=_varied, group_size=group_size,
    )


# ---------------- 正常路径 ----------------

def test_collect_returns_manifest_episodes_in_manifest_order_with_provenance(store):
    manifest = _produce(store)
    batch = BatchTrajectorySource(store, manifest).collect()
    assert [e.id for e in batch.episodes] == [r["id"] for r in manifest.episode_refs]
    assert batch.metadata == {
        "manifest_id": manifest.id,
        "manifest_hash": manifest.manifest_hash,
        "episode_count": 4,
    }


def test_collected_episodes_fall_into_one_group_per_scenario(store):
    batch = BatchTrajectorySource(store, _produce(store)).collect()
    groups = {}
    for episode in batch.episodes:
        groups.setdefault(episode_group_id(episode), []).append(episode.id)
    assert sorted(len(members) for members in groups.values()) == [2, 2]


def test_a_manifest_read_back_from_the_store_works_the_same(store):
    manifest = _produce(store)
    from_disk = read_manifest(store, "run-1", "grpo_opd")
    assert BatchTrajectorySource(store, from_disk).collect() == BatchTrajectorySource(store, manifest).collect()


def test_a_single_episode_per_group_is_fine_here_group_size_is_checked_elsewhere(store):
    manifest = _produce(store, name="sft", group_size=1)
    assert len(BatchTrajectorySource(store, manifest).collect().episodes) == 2


def test_each_collect_reads_afresh_so_batches_are_independent(store):
    source = BatchTrajectorySource(store, _produce(store))
    first, second = source.collect(), source.collect()
    first.episodes[0].turns[0].utterance = "tampered by the caller"
    assert second.episodes[0].turns[0].utterance != "tampered by the caller"
    assert source.collect().episodes[0].turns[0].utterance != "tampered by the caller"


# ---------------- 完整性:失败是显式的 ----------------

def test_a_modified_episode_is_detected(store):
    manifest = _produce(store)
    target = manifest.episode_refs[0]["id"]
    episode = read_episode(store, "run-1", target)
    episode.turns[0].utterance = "rewritten after the manifest was built"
    write_episode(store, "run-1", episode)
    with pytest.raises(DatasetIntegrityError, match=f"episode {target} sha256 mismatch"):
        BatchTrajectorySource(store, manifest).collect()


def test_a_missing_episode_is_detected(store, tmp_path):
    manifest = _produce(store)
    target = manifest.episode_refs[1]["id"]
    (tmp_path / episode_file("run-1", target)).unlink()
    with pytest.raises(DatasetIntegrityError, match=f"episode {target} is missing"):
        BatchTrajectorySource(store, manifest).collect()


def test_a_forged_manifest_hash_is_detected(store):
    manifest = _produce(store)
    forged = dataclasses.replace(manifest, manifest_hash="0" * 64)
    with pytest.raises(DatasetIntegrityError, match="manifest_hash mismatch"):
        BatchTrajectorySource(store, forged).collect()


def test_dropping_a_ref_without_recomputing_the_hash_is_detected(store):
    manifest = _produce(store)
    shrunk = dataclasses.replace(manifest, episode_refs=manifest.episode_refs[:-1])
    with pytest.raises(DatasetIntegrityError, match="manifest_hash mismatch"):
        BatchTrajectorySource(store, shrunk).collect()


def test_an_empty_manifest_is_refused(store):
    empty = DatasetManifest("dsm-run-1-sft", "run-1", "sft", [], "0" * 64)
    with pytest.raises(DatasetIntegrityError, match="is empty"):
        BatchTrajectorySource(store, empty).collect()


# ====================== batch_for_step ======================

def test_batch_for_step_rotates_in_fixed_order_and_wraps():
    items = ["a", "b", "c"]
    assert batch_for_step(items, 0, 2) == ["a", "b"]
    assert batch_for_step(items, 1, 2) == ["c", "a"]
    assert batch_for_step(items, 2, 2) == ["b", "c"]


def test_batch_for_step_may_repeat_items_when_size_exceeds_the_pool():
    assert batch_for_step(["a", "b"], 0, 5) == ["a", "b", "a", "b", "a"]


def test_batch_for_step_is_a_pure_function_of_its_arguments():
    items = ["a", "b", "c"]
    assert batch_for_step(items, 7, 2) == batch_for_step(items, 7, 2)
    assert items == ["a", "b", "c"]


def test_batch_for_step_returns_a_new_list():
    items = ["a", "b", "c"]
    batch_for_step(items, 0, 2).append("zzz")
    assert items == ["a", "b", "c"]


@pytest.mark.parametrize(
    "items,step,size,needle",
    [([], 0, 1, "non-empty"), (["a"], 0, 0, "size must be >= 1"), (["a"], -1, 1, "step must be >= 0")],
    ids=["empty", "size_zero", "negative_step"],
)
def test_batch_for_step_rejects_bad_arguments(items, step, size, needle):
    with pytest.raises(ValueError, match=needle):
        batch_for_step(items, step, size)
