"""`--interact` 在真实入口的端到端用例(计划 Task 9)。

走的是真入口:调度器的 resolve_action + construct_directive 写出一条生成路的 pending interact 指令
(情境与人格都还没有),`factory reconcile --once --interact` 的命令行入口装配并跑一个 tick。
不碰 SSH、GPU 主机与 vLLM:`compute.service.serve` 换成指向本机 http.server 桩的假替身
(桩与假 serve 复用 test_factory_interact_wire 里那一份,不再抄第三份)。
"""
from __future__ import annotations

import dataclasses
import subprocess

import pytest
from test_factory_interact_wire import fake_serve, hosts_env, stub, write_endpoints  # noqa: F401 (两个是夹具)

from compute.vllm_service import BASE_MODEL_NAME
from controlplane.config import write_config
from controlplane.envelope import BigRef
from controlplane.paths import (
    candidate_checkpoint_file,
    episodes_dir,
    persona_file,
    run_dir,
    scenario_file,
    training_defaults_file,
)
from factory.agent import AgentRuntime
from factory.cli import main
from factory.opponents import EXPORT_FORMAT
from simulator.store_adapter import list_personas, list_scenarios, read_episode, read_persona, read_scenario
from trainplanner.decision import PlannerDecision
from trainscheduler.config import DEFAULT_SCHEDULER_CONFIG, default_training_defaults_payload, load_training_defaults
from trainscheduler.directive import construct_directive
from trainscheduler.resolve import resolve_action

RUN = "run-e2e-1"
BASE = BigRef("file:///ckpt/base", "a" * 64, 1)
EXPORT = BigRef("ssh://gpu-1/srv/rise/work/exports/t1", "d" * 64, 10,
                fetch_hint={"type": "roll_ssh", "format": EXPORT_FORMAT})

# 训练步数、每步组数、组大小:本用例的会话总数 = 2 * 2 * 2
GRPO_STEPS, GROUPS_PER_STEP, GROUP_SIZE = 2, 2, 2
SESSIONS = GRPO_STEPS * GROUPS_PER_STEP * GROUP_SIZE
MAX_TURNS = 4
TRAINED_TURNS_PER_SESSION = MAX_TURNS // 2          # 训练位先手,四回合里说两句,每句一次策略请求


def _generated_defaults(base_ref: BigRef) -> dict:
    """生成路的 training_defaults.yaml:interact 段带 data_spec、不点名情境与人格;sft_steps 为 0,
    所以树里的 episode 全是实时会话产出的,没有合成的 SFT 数据混进来(合成路径由 test_mt_dispatch_synthesis 管)。"""
    payload = default_training_defaults_payload()
    cfg = payload["training_config"]
    cfg.update(sft_steps=0, grpo_steps=GRPO_STEPS, groups_per_step=GROUPS_PER_STEP, group_size=GROUP_SIZE,
               base_checkpoint_ref=base_ref.to_dict())
    payload["interact"] = {
        "trained_slot": "a",
        "slots": [{"slot_id": "a", "model_backend": "self_play"},
                  {"slot_id": "b", "model_backend": "frozen_reference", "model_ref": "zengen-v1",
                   "driver": "scripted"}],
        "max_turns_per_episode": MAX_TURNS,
        "data_spec": {"scenario_count": 2, "seed": 0, "purpose": "training",
                      "stages": {"sft": {"group_size": 1}, "grpo_opd": {"group_size": 2}}},
    }
    return payload


def _repo(tmp_path, base_ref: BigRef):
    from factory_fixtures import closed_loop_repo

    store = closed_loop_repo(str(tmp_path / "cp"), run_id=None)
    write_endpoints(store, "gpu-1")
    write_config(store, training_defaults_file(), "training-defaults", _generated_defaults(base_ref),
                 "test: generated-path defaults")
    return store


def _schedule_interact_directive(store) -> None:
    """调度器怎么写,指令就怎么在树里:allow_interact 打开,新一轮 interact,不确认(直接 pending)。"""
    config = dataclasses.replace(DEFAULT_SCHEDULER_CONFIG, allow_interact=True)
    decision = PlannerDecision("tom", "new", "interact", None, {}, "", "e2e")
    resolved = resolve_action(store, decision, config=config, defaults=load_training_defaults(store))
    construct_directive(store, resolved, new_run_id=RUN, confirm=False, planner_decision_id="planner-e2e")


@pytest.fixture
def sessions(monkeypatch):
    """记下真运行时上的两类调用:登记(情境 id, run id)与落盘钩子收到的 episode id。
    都是转手给原方法,只为得到一个与树无关的会话计数。"""
    seen = {"registered": [], "closed": []}
    real_register, real_hook = AgentRuntime.register_session, AgentRuntime._on_session_closed

    def register(self, scenario_id, run_id):
        seen["registered"].append((scenario_id, run_id))
        return real_register(self, scenario_id, run_id)

    def hook(self, episode):
        seen["closed"].append(episode.id)
        return real_hook(self, episode)

    monkeypatch.setattr(AgentRuntime, "register_session", register)
    monkeypatch.setattr(AgentRuntime, "_on_session_closed", hook)
    return seen


def _commit_subjects(store) -> list[str]:
    out = subprocess.run(["git", "-C", str(store.repo_root), "log", "--format=%s"],
                         capture_output=True, text=True, check=True).stdout
    return out.splitlines()


@pytest.mark.parametrize(("base_ref", "kind", "adapters", "model"), [
    (BASE, "base", (), BASE_MODEL_NAME),
    (EXPORT, "adapter", (("trained", "/srv/rise/work/exports/t1"),), "trained"),
], ids=["base-checkpoint", "adapter-export"])
def test_an_interact_directive_runs_through_the_real_entry(tmp_path, capsys, stub, fake_serve, sessions, base_ref,
                                                           kind, adapters, model):
    store = _repo(tmp_path, base_ref)
    _schedule_interact_directive(store)
    directive_path = f"{run_dir(RUN)}/directive.yaml"

    # 起点:调度器写出的生成路指令。情境为空,角色位布局已展开,但人格还是空串
    pending = store.read_product(directive_path)
    assert pending.status == "pending"
    assert pending.payload["training_config"]["scenario_ids"] == []
    assert [(b["slot_id"], b["persona_id"]) for b in pending.payload["training_config"]["bindings"]] == [
        ("a", ""), ("b", "")]
    scenarios_before, personas_before = list_scenarios(store), list_personas(store)

    # 真入口:命令行解析 -> 装配 -> 一个 tick。没有 --enable,只有 --interact
    code = main(["reconcile", str(store.repo_root), "--once", "--interact"], env=hosts_env(tmp_path))
    out = capsys.readouterr()
    assert code == 0, out.err
    assert f"dispatch_directive {RUN}" in out.out

    # 指令与训练记录都 executed;候选等人审
    directive = store.read_product(directive_path)
    assert directive.status == "executed"
    training_run = store.read_product(f"{run_dir(RUN)}/training_run.yaml")
    assert training_run.status == "executed"
    assert store.read_product(candidate_checkpoint_file(RUN)).status == "pending_review"

    # policy_backend 在场,训练位的 kind 与用的基座一致,端点就是服务起在的端点
    backend = training_run.payload["policy_backend"]
    assert backend["trained"] == {"ref": base_ref.uri, "kind": kind, "endpoint": "gpu-1"}
    assert backend["opponents"] == {"b": {"driver": "scripted", "source": "zengen-v1"}}
    assert fake_serve.calls == [("gpu-1", adapters)]
    assert fake_serve.trace == ["enter", "exit"]
    assert stub.models() == {model}

    # 情境与人格是派发时生成的,落了树,并写回了指令
    cfg = directive.payload["training_config"]
    new_scenarios = sorted(set(list_scenarios(store)) - set(scenarios_before))
    new_personas = sorted(set(list_personas(store)) - set(personas_before))
    assert cfg["scenario_ids"] == ["scen-auto-0", "scen-auto-1"]
    assert new_scenarios == [scenario_file(sid) for sid in cfg["scenario_ids"]]
    persona_ids = [b["persona_id"] for b in cfg["bindings"]]
    assert persona_ids == ["pers-auto-0", "pers-auto-1"]
    assert new_personas == [persona_file(pid) for pid in persona_ids]
    for pid in persona_ids:
        assert read_persona(store, pid).id == pid
    for sid in cfg["scenario_ids"]:
        assert [s.slot_id for s in read_scenario(store, sid).role_slots] == ["a", "b"]

    # episode:会话产出的每一条都在本 run 的目录里,读得回来,个数就是会话数
    paths = store.list_products(episodes_dir(RUN))
    episodes = [read_episode(store, RUN, path.rsplit("/", 1)[-1].removesuffix(".yaml")) for path in paths]
    assert len(episodes) == SESSIONS == cfg["grpo_steps"] * cfg["groups_per_step"] * cfg["group_size"]
    assert len({e.id for e in episodes}) == SESSIONS
    assert {e.scenario_id for e in episodes} == set(cfg["scenario_ids"])
    assert all(e.terminated_reason in ("max_turns", "goal_reached") for e in episodes)
    assert all([b.persona_id for b in e.role_assignment.bindings] == persona_ids for e in episodes)
    # 会话数的独立计数:本 run 的两个情境都登记过、都登记给本 run;钩子收到的会话数、策略桩收到的请求数,
    # 与树里的 episode 个数各自对得上,所以不是"同一个文件被写了两遍"凑出来的数
    assert sessions["registered"] == [(sid, RUN) for sid in cfg["scenario_ids"]]
    assert sorted(sessions["closed"]) == sorted(e.id for e in episodes)
    assert len(stub.requests) == SESSIONS * TRAINED_TURNS_PER_SESSION
    # 每个 episode 只落盘一次:提交说明里每个 episode 恰好一条
    written = [s for s in _commit_subjects(store) if s.startswith("episode: ") and s.endswith(f" for {RUN}")]
    assert sorted(written) == sorted(f"episode: {e.id} for {RUN}" for e in episodes)
