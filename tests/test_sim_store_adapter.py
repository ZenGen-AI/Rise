from controlplane.gitstore import GitStore
from simulator.datamodel import (
    Persona,
    Scenario,
    RoleSlot,
    RoleBinding,
    RoleAssignment,
    Episode,
    TurnRecord,
)
from simulator.store_adapter import (
    write_persona,
    write_scenario,
    write_episode,
    read_persona,
    read_scenario,
    read_episode,
    list_scenarios,
    list_personas,
)


def _store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    return s


def test_write_read_persona_round_trip(tmp_path):
    store = _store(tmp_path)
    p = Persona(id="pers-1", traits=["谨慎"], motivation="m", speech_style="s", values=["v"])
    write_persona(store, p)
    assert read_persona(store, "pers-1") == p


def test_write_read_scenario_round_trip(tmp_path):
    store = _store(tmp_path)
    s = Scenario(
        id="scen-1",
        background="bg",
        role_slots=[RoleSlot(slot_id="a", description="d")],
        conflict="c",
        norms=["n"],
        purpose="training",
    )
    write_scenario(store, s)
    assert read_scenario(store, "scen-1") == s


def test_write_read_episode_round_trip_and_links(tmp_path):
    store = _store(tmp_path)
    ra = RoleAssignment(
        scenario_id="scen-1",
        bindings=[RoleBinding(slot_id="a", persona_id="pers-1", model_backend="self_play")],
        max_turns=1,
    )
    ep = Episode(
        id="ep-1",
        scenario_id="scen-1",
        role_assignment=ra,
        turns=[TurnRecord(turn_index=0, speaker_slot="a", utterance="hi")],
        terminated_reason="max_turns",
    )
    write_episode(store, "run-1", ep)
    assert read_episode(store, "run-1", "ep-1") == ep


def test_list_pools(tmp_path):
    store = _store(tmp_path)
    write_persona(store, Persona(id="pers-1", traits=[], motivation="", speech_style="", values=[]))
    write_scenario(
        store,
        Scenario(id="scen-1", background="", role_slots=[], conflict="", norms=[], purpose="training"),
    )
    personas = list_personas(store)
    scenarios = list_scenarios(store)
    assert any("pers-1.yaml" in p for p in personas)
    assert any("scen-1.yaml" in s for s in scenarios)


def test_write_persona_idempotent(tmp_path):
    store = _store(tmp_path)
    p = Persona(id="pers-1", traits=[], motivation="", speech_style="", values=[])
    write_persona(store, p)
    write_persona(store, p)  # 逐字节相同：不应抛异常（复用计划 1 幂等 write_product）
    assert read_persona(store, "pers-1") == p
