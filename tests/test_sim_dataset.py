import pytest

from controlplane.gitstore import GitStore
from controlplane.paths import dataset_manifest_file, episode_file
from simulator.backends import ScriptedBackend
from simulator.dataset import (
    DatasetIntegrityError, DatasetManifest, build_manifest, episode_digest,
    episode_group_id, produce_grouped_dataset, read_manifest, verify_manifest,
    write_manifest,
)
from simulator.datamodel import (
    Episode, Persona, RoleAssignment, RoleBinding, RoleSlot, Scenario, TurnRecord,
)
from simulator.store_adapter import write_episode


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    return s


def _bindings(opp_ref="zengen-v1", opp_persona="pb"):
    return [
        RoleBinding("a", "pa", "self_play"),
        RoleBinding("b", opp_persona, "frozen_reference", model_ref=opp_ref),
    ]


def _ep(bindings=None, scenario_id="s1", max_turns=4, ep_id="e", utterance="x"):
    ra = RoleAssignment(scenario_id, bindings or _bindings(), max_turns)
    return Episode(ep_id, scenario_id, ra, [TurnRecord(0, "a", utterance)], "max_turns")


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


# ---------------- 摘要 ----------------

def test_digest_is_stable_across_serialization_round_trip():
    ep = _ep()
    assert episode_digest(Episode.from_dict(ep.to_dict())) == episode_digest(ep)


def test_digest_changes_when_an_utterance_changes():
    assert episode_digest(_ep(utterance="x")) != episode_digest(_ep(utterance="y"))


# ---------------- group_id ----------------

def test_group_id_is_shared_by_episodes_of_the_same_context():
    assert episode_group_id(_ep(ep_id="e1", utterance="x")) == episode_group_id(
        _ep(ep_id="e2", utterance="y")
    )


def test_group_id_ignores_max_turns():
    assert episode_group_id(_ep(max_turns=4)) == episode_group_id(_ep(max_turns=40))


@pytest.mark.parametrize(
    "other",
    [
        _ep(scenario_id="s2"),
        _ep(bindings=_bindings(opp_persona="pc")),
        _ep(bindings=_bindings(opp_ref="zengen-v2")),  # 换了对手模型就不是同一上下文
        _ep(bindings=_bindings(opp_ref=None)),
        _ep(bindings=list(reversed(_bindings()))),  # 发言轮转顺序不同
    ],
    ids=["scenario", "persona", "opponent_model_ref", "no_model_ref", "binding_order"],
)
def test_group_id_differs_when_the_context_differs(other):
    assert episode_group_id(_ep()) != episode_group_id(other)


# ---------------- 清单 ----------------

def _seed(store, run_id="run-1", ids=("e1", "e2")):
    for eid in ids:
        write_episode(store, run_id, _ep(ep_id=eid, utterance=f"say-{eid}"))
    return list(ids)


def test_manifest_hash_is_independent_of_input_order(store):
    _seed(store)
    m1 = build_manifest(store, "run-1", "sft", ["e1", "e2"])
    m2 = build_manifest(store, "run-1", "sft", ["e2", "e1"])
    assert m1 == m2
    assert [r["id"] for r in m1.episode_refs] == ["e1", "e2"]


@pytest.mark.parametrize(
    "name,ids",
    [("sft", []), ("sft", ["e1", "e1"]), ("Bad Name", ["e1"]), ("a/b", ["e1"])],
    ids=["empty", "duplicate", "bad_name_space", "bad_name_slash"],
)
def test_build_manifest_rejects_bad_input(store, name, ids):
    _seed(store)
    with pytest.raises(ValueError):
        build_manifest(store, "run-1", name, ids)


def test_manifest_is_stored_as_a_controlplane_product(store):
    _seed(store)
    m = build_manifest(store, "run-1", "sft", ["e1", "e2"])
    path = write_manifest(store, m)
    assert path == dataset_manifest_file("run-1", "sft")
    env = store.read_product(path)
    assert env.type == "dataset_manifest" and env.status == "executed"
    assert read_manifest(store, "run-1", "sft") == m


def test_verify_returns_episodes_in_manifest_order(store):
    _seed(store)
    m = build_manifest(store, "run-1", "sft", ["e2", "e1"])
    assert [e.id for e in verify_manifest(store, m)] == ["e1", "e2"]


def test_tampered_episode_is_detected(store):
    _seed(store)
    m = build_manifest(store, "run-1", "sft", ["e1", "e2"])
    path = episode_file("run-1", "e1")
    env = store.read_product(path)
    env.payload["turns"][0]["utterance"] = "TAMPERED"
    store.write_product(path, env, "tamper")
    with pytest.raises(DatasetIntegrityError, match="e1"):
        verify_manifest(store, m)


def test_forged_manifest_entry_is_detected_by_manifest_hash(store):
    _seed(store)
    m = build_manifest(store, "run-1", "sft", ["e1"])
    forged = DatasetManifest(
        m.id, m.run_id, m.name, [{"id": "e1", "sha256": "0" * 64}], m.manifest_hash,
    )
    with pytest.raises(DatasetIntegrityError, match="manifest_hash"):
        verify_manifest(store, forged)


def test_missing_episode_is_detected(store):
    _seed(store)
    m = build_manifest(store, "run-1", "sft", ["e1", "e2"])
    (store.repo_root / episode_file("run-1", "e2")).unlink()
    with pytest.raises(DatasetIntegrityError, match="e2"):
        verify_manifest(store, m)


def test_empty_manifest_is_rejected(store):
    with pytest.raises(DatasetIntegrityError, match="empty"):
        verify_manifest(store, DatasetManifest("dsm", "run-1", "sft", [], "whatever"))


# ---------------- 生产方 ----------------

def test_producer_makes_group_size_varied_rollouts_per_scenario(store):
    manifest = produce_grouped_dataset(
        store, "run-1", "grpo_opd", scenarios=_scenarios("s1", "s2"),
        personas=_personas(), backends_for_rollout=_varied, group_size=3,
    )
    episodes = verify_manifest(store, manifest)
    assert len(episodes) == 6
    groups: dict[str, list[Episode]] = {}
    for ep in episodes:
        groups.setdefault(episode_group_id(ep), []).append(ep)
    assert sorted(len(v) for v in groups.values()) == [3, 3]
    for members in groups.values():
        assert len({m.scenario_id for m in members}) == 1
        assert len({m.turns[0].utterance for m in members}) == 3  # 各 rollout 确有差异


def test_two_stage_datasets_in_one_run_do_not_collide(store):
    sft = produce_grouped_dataset(
        store, "run-1", "sft", scenarios=_scenarios("s1"),
        personas=_personas(), backends_for_rollout=_varied, group_size=1,
    )
    grpo = produce_grouped_dataset(
        store, "run-1", "grpo_opd", scenarios=_scenarios("s1"),
        personas=_personas(), backends_for_rollout=_varied, group_size=2,
    )
    assert sft.id != grpo.id
    assert {r["id"] for r in sft.episode_refs}.isdisjoint({r["id"] for r in grpo.episode_refs})
    assert dataset_manifest_file("run-1", "sft") != dataset_manifest_file("run-1", "grpo_opd")
    verify_manifest(store, sft)
    verify_manifest(store, grpo)
    assert read_manifest(store, "run-1", "sft") == sft


@pytest.mark.parametrize("kwargs", [{"group_size": 0}, {"scenarios": []}], ids=["size_zero", "no_scenarios"])
def test_producer_rejects_degenerate_requests(store, kwargs):
    args = dict(
        scenarios=_scenarios("s1"), personas=_personas(),
        backends_for_rollout=_varied, group_size=1,
    )
    args.update(kwargs)
    with pytest.raises(ValueError):
        produce_grouped_dataset(store, "run-1", "sft", **args)
