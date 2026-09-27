from controlplane.gitstore import GitStore
from controlplane.paths import run_dir
from simulator.datamodel import (
    RoleSlot,
    RoleBinding,
    RoleAssignment,
    Persona,
)
from simulator.backends import TemplateGenerationBackend, ScriptedBackend
from simulator.generators import generate_scenario, generate_persona
from simulator.runner import run_episode
from simulator.store_adapter import write_scenario, write_persona, write_episode, read_episode


def test_full_batch_loop_generate_run_persist(tmp_path):
    """端到端:生成情境+人格 → 组装角色分配 → Runner 产出 Episode → 落盘可回读。"""
    store = GitStore(str(tmp_path))
    store.init()
    gen = TemplateGenerationBackend()

    scenario = generate_scenario(gen, seed=1, purpose="training")
    write_scenario(store, scenario)
    persona_a = generate_persona(gen, seed=1)
    persona_b = generate_persona(gen, seed=2)
    write_persona(store, persona_a)
    write_persona(store, persona_b)

    slot_ids = [r.slot_id for r in scenario.role_slots]
    assignment = RoleAssignment(
        scenario_id=scenario.id,
        bindings=[
            RoleBinding(slot_id=slot_ids[0], persona_id=persona_a.id, model_backend="self_play"),
            RoleBinding(slot_id=slot_ids[1], persona_id=persona_b.id, model_backend="frozen_reference"),
        ],
        max_turns=4,
    )
    backends = {
        slot_ids[0]: ScriptedBackend(scripted={slot_ids[0]: ["A1", "A2"]}),
        slot_ids[1]: ScriptedBackend(scripted={slot_ids[1]: ["B1", "B2"]}),
    }
    personas = {persona_a.id: persona_a, persona_b.id: persona_b}

    ep = run_episode(scenario, assignment, personas, backends, "ep-int-1")
    write_episode(store, "run-int", ep)

    assert read_episode(store, "run-int", "ep-int-1") == ep
    assert len(ep.turns) == 4


def test_social_simulator_stub_now_generates_real_scenario(tmp_path):
    from simulator.stubs import social_simulator_stub

    env = social_simulator_stub("run-x", "high_order_belief")
    # 计划 1 兼容断言点不变
    assert env.type == "scenario_pool"
    assert env.status == "executed"
    assert env.links == ["run-x"]
    assert env.payload["target_dimension"] == "high_order_belief"
    assert env.payload["episode_count"] == 0
    # 新增:指向真实生成的 Scenario 产物
    assert "scenario_id" in env.payload
