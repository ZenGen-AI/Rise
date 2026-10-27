"""六个处理器构造器(任务 107):每个键都接到计划 3、5、7 的真实处理器;开关值只有一个来源。"""
import subprocess
import sys

import pytest

from controlplane.config import write_config
from controlplane.paths import backends_config_file, scheduler_config_file
from controlplane.reconcile import CLOSED_LOOP_ACTIONS
from factory import handlers as fh
from factory.agent import AGENT_CONFIG, AgentRuntime, CannedGuidedBackend, read_agent_settings
from factory.defaults import DEFAULT_AGENT_CONFIG, install_defaults
from factory.errors import FactoryConfigError
from factory.wire import CODE_ROOT, FactoryOptions, assemble, resolve_repo_commit
from factory_fixtures import closed_loop_repo
from mindagent.admit import handle_admit_proposal
from mindagent.config import AgentConfig
from recipestore.admit import handle_admit_recipe
from recipestore.record import handle_record_track
from simulator.backends import ScriptedBackend
from trainscheduler.config import load_scheduler_config

ALL = ("plan", "nominate", "track", "agent", "admit")


@pytest.fixture
def store(tmp_path):
    return closed_loop_repo(str(tmp_path / "cp"), run_id=None)


def _planner_backend(store):
    write_config(store, backends_config_file(), "backends", {"planner": {
        "backend": "fake", "invocation_form": "cli", "model_name": "m", "prompt_template_version": "v1",
        "token_env": None, "command": [sys.executable, "-c", "print('{}')"],
    }}, "test: backends")


def test_the_table_covers_exactly_the_six_keys():
    assert set(fh.DEFAULT_BUILDERS) == set(CLOSED_LOOP_ACTIONS)


def test_three_keys_are_the_plan_3_and_plan_7_handlers_themselves(store):
    asm = assemble(store, FactoryOptions(switches=("track", "admit")), env={})
    assert asm.handlers == {"record_track": handle_record_track, "admit_recipe": handle_admit_recipe,
                            "admit_proposal": handle_admit_proposal}


def test_every_switch_builds_a_callable_handler_with_the_heuristic_backend(store):
    asm = assemble(store, FactoryOptions(switches=ALL, planner_backend="heuristic"), env={})
    try:
        assert set(asm.handlers) == set(CLOSED_LOOP_ACTIONS)
        assert all(callable(h) for h in asm.handlers.values())
    finally:
        asm.close()


def test_the_bridge_planner_needs_a_planner_backend_entry(store, capsys):
    from factory.cli import main
    assert main(["reconcile", str(store.repo_root), "--once", "--enable", "plan"], env={}) == 2
    assert "backends.yaml is missing" in capsys.readouterr().err
    _planner_backend(store)
    assert main(["reconcile", str(store.repo_root), "--once", "--enable", "plan"], env={}) == 0


def test_the_mock_evidence_switch_reaches_the_nominate_handler(store, monkeypatch):
    seen = []
    monkeypatch.setattr(fh, "make_nominate_handler", lambda **kw: seen.append(kw) or (lambda s, a: None))
    for flag in (False, True):
        assemble(store, FactoryOptions(switches=("nominate",), allow_mock_evidence=flag, repo_commit="abc"), env={})
    assert [(kw["accept_mock_evidence"], kw["repo_commit"]) for kw in seen] == [(False, "abc"), (True, "abc")]


def test_confirm_override_only_changes_the_loaded_value(store):
    write_config(store, scheduler_config_file(), "scheduler",
                 dict(load_scheduler_config(store).to_dict(), confirm_directives=True), "human: confirm on")
    assert fh.scheduler_config_loader(None)(store).confirm_directives is True
    assert fh.scheduler_config_loader(False)(store).confirm_directives is False
    assert load_scheduler_config(store).confirm_directives is True  # 配置文件没被改


def test_repo_commit_prefers_the_override_then_git_then_none(tmp_path):
    assert resolve_repo_commit("deadbeef") == "deadbeef"
    head = subprocess.run(["git", "-C", str(CODE_ROOT), "rev-parse", "HEAD"], capture_output=True, text=True)
    expected = head.stdout.strip() if head.returncode == 0 else None
    assert resolve_repo_commit(None) == expected
    assert resolve_repo_commit(None, code_root=tmp_path) is None


def test_the_cli_flag_sets_repo_commit(store, monkeypatch):
    from factory.cli import main
    seen = []
    monkeypatch.setattr(fh, "make_nominate_handler", lambda **kw: seen.append(kw) or (lambda s, a: None))
    assert main(["reconcile", str(store.repo_root), "--once", "--enable", "nominate", "--repo-commit", "c0ffee"],
                env={}) == 0
    assert seen[0]["repo_commit"] == "c0ffee"


def test_agent_settings_come_from_agent_yaml(store):
    config, lines = read_agent_settings(store)
    assert isinstance(config, AgentConfig)
    assert config.scenario_ids == tuple(DEFAULT_AGENT_CONFIG["scenario_ids"])
    assert (config.agent_slot, config.max_turns, config.min_support) == ("agent", 4, 3)
    assert lines == tuple(DEFAULT_AGENT_CONFIG["opponent_lines"])


@pytest.mark.parametrize("change,match", [
    ({"drop": "opponent_lines"}, "missing"), ({"set": ("colour", "red")}, "unknown"),
    ({"set": ("scenario_ids", [])}, "scenario_ids"), ({"set": ("min_ratio", 2.0)}, "min_ratio"),
    ({"set": ("bindings", [{"slot_id": "a"}])}, "KeyError"),
])
def test_bad_agent_yaml_is_an_assembly_error(store, change, match):
    payload = dict(DEFAULT_AGENT_CONFIG)
    if "drop" in change:
        del payload[change["drop"]]
    else:
        payload[change["set"][0]] = change["set"][1]
    write_config(store, AGENT_CONFIG, "agent", payload, "human: break agent.yaml")
    with pytest.raises(FactoryConfigError, match=match):
        assemble(store, FactoryOptions(switches=("agent",)), env={})


def test_the_agent_runtime_starts_lazily_and_closes(store):
    runtime = AgentRuntime(store, ("hi",))
    assert not runtime.started
    runtime.client_factory()
    assert runtime.started
    runtime.close()
    assert not runtime.started


def test_channels_of_finished_clients_are_closed_before_a_new_one_is_handed_out(store, monkeypatch):
    """守护进程长跑时每个轮次都要几个客户端。会话结束(收完或 abort)的客户端,通道在下一次
    发客户端时就关掉,在途通道数不随轮次增长;close() 仍关掉剩下的全部。"""
    import grpc

    class FakeChannel:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    made = []

    def fake_channel(target):
        made.append(FakeChannel())
        return made[-1]

    class FakeStub:
        def __init__(self, channel):
            pass

        def Interact(self, request_iterator, **kw):
            return iter(())

    monkeypatch.setattr(grpc, "insecure_channel", fake_channel)
    runtime = AgentRuntime(store, ("hi",), stub_factory=FakeStub)
    try:
        for _ in range(5):
            runtime.client_factory().abort()          # 一条 rollout 结束:驱动方在 finally 里 abort
        in_flight = runtime.client_factory()          # 第六个仍在用
        assert [c.closed for c in made] == [True] * 5 + [False]
        assert not in_flight.closed
    finally:
        runtime.close()
    assert all(c.closed for c in made)


def test_every_interact_stream_carries_a_deadline(store):
    """计划 7 已知限制 4:生产用的 EnvironmentServiceClient 没有 deadline,组合根在构造 stub 时包一层。
    用假 stub 记下调用参数,不依赖真实超时。"""
    from factory.agent import INTERACT_DEADLINE_S, DeadlineStub

    calls = []

    class FakeStub:
        def __init__(self, channel):
            pass

        def Interact(self, request_iterator, **kw):
            calls.append(kw)
            return iter(())

    default = AgentRuntime(store, ("hi",), stub_factory=FakeStub)
    short = AgentRuntime(store, ("hi",), stub_factory=FakeStub, interact_deadline_s=2.5)
    try:
        client = default.client_factory()
        assert isinstance(client._stub, DeadlineStub)
        assert calls == [{"timeout": INTERACT_DEADLINE_S}] and INTERACT_DEADLINE_S == 15.0
        short.client_factory()
        assert calls[-1] == {"timeout": 2.5}
    finally:
        default.close()
        short.close()
    assert not default.started and not short.started


def test_a_caller_given_deadline_overrides_the_runtime_value_for_that_client_only(store):
    calls = []

    class FakeStub:
        def __init__(self, channel):
            pass

        def Interact(self, request_iterator, **kw):
            calls.append(kw)
            return iter(())

    runtime = AgentRuntime(store, ("hi",), stub_factory=FakeStub, interact_deadline_s=2.5)
    try:
        # 客户端构造时就开流(EnvironmentServiceClient.__init__ 调一次 stub.Interact),所以每个客户端恰好记一条
        runtime.client_factory(deadline_s=360.0)
        runtime.client_factory()
        assert calls == [{"timeout": 360.0}, {"timeout": 2.5}]
        assert runtime.interact_deadline_s == 2.5            # 调用方给的值不回写运行时
    finally:
        runtime.close()


def test_the_message_timeout_reaches_every_client_and_defaults_to_none(store):
    with_limit = AgentRuntime(store, ("hi",), message_timeout_s=90.0)
    plain = AgentRuntime(store, ("hi",))
    try:
        assert with_limit.client_factory()._message_timeout_s == 90.0
        assert plain.client_factory()._message_timeout_s is None
    finally:
        with_limit.close()
        plain.close()


def test_the_canned_backend_echoes_guidance():
    assert CannedGuidedBackend().respond_guided(None, None, [], "agent", ["[skill:x] a", "b"]) == \
        ("[skill:x] a|b", "saw 0")
    assert CannedGuidedBackend().respond_guided(None, None, [1, 2], "agent", []) == ("plain", "saw 2")


def test_install_defaults_writes_scheduler_and_agent_material_and_is_idempotent(tmp_path):
    from factory_fixtures import git_head
    from controlplane.gitstore import GitStore
    s = GitStore(str(tmp_path / "x"))
    s.init()
    paths = install_defaults(s)
    assert {scheduler_config_file(), AGENT_CONFIG} <= set(paths)
    assert all(s.exists(p) for p in paths)
    head = git_head(s)
    assert install_defaults(s) == paths
    assert git_head(s) == head


# ---------------- 对手位按驱动分派(AgentRuntime._backends) ----------------

def _scenario_with(script=None):
    from simulator.datamodel import RoleSlot, Scenario
    return Scenario("s1", "bg", [RoleSlot("a", "a"), RoleSlot("b", "b")], "c", ["n"], "training",
                    opponent_script=script or {})


def _assignment(*bindings):
    from simulator.datamodel import RoleAssignment
    return RoleAssignment("s1", list(bindings), 4)


def _backends_of(runtime, scenario, assignment, personas, client_slot):
    """用一份固定的会话标识调运行时的后端工厂,取回各角色位的后端。"""
    from simulator.realtime.resources import SessionInfo
    info = SessionInfo(scenario, assignment, personas, client_slot, "sess-test", "ep-sess-test")
    return runtime._backends(info).backends


def _says(backend, slot, times):
    return [backend.respond(None, None, [], slot)[0] for _ in range(times)]


def test_a_scripted_driver_loops_the_scenario_script_for_its_slot(store):
    from simulator.datamodel import RoleBinding
    runtime = AgentRuntime(store, ("全局",))
    opponent = RoleBinding("b", "p", "frozen_reference", model_ref="zengen-v1", driver="scripted")
    backends = _backends_of(runtime, _scenario_with({"b": ["一", "二"]}),
                             _assignment(RoleBinding("a", "p", "self_play"), opponent), {}, "a")
    assert set(backends) == {"b"}
    assert _says(backends["b"], "b", 5) == ["一", "二", "一", "二", "一"]


@pytest.mark.parametrize("script", [None, {}, {"b": []}, {"c": ["别人的"]}])
def test_a_scripted_driver_falls_back_to_the_global_lines_when_the_slot_has_no_script(store, script):
    from simulator.datamodel import RoleBinding
    runtime = AgentRuntime(store, ("全局甲", "全局乙"))
    opponent = RoleBinding("b", "p", "external_api", model_ref="gpt-x", driver="scripted")
    backends = _backends_of(runtime, _scenario_with(script),
                             _assignment(RoleBinding("a", "p", "self_play"), opponent), {}, "a")
    assert _says(backends["b"], "b", 3) == ["全局甲", "全局乙", "全局甲"]


def test_without_a_factory_every_opponent_is_the_same_unlooped_scripted_backend_as_before(store):
    """MindAgent 部署会话的绑定多是 frozen_reference:没传工厂时一个字不能变,剧本也不能偷偷生效。"""
    from simulator.datamodel import RoleBinding
    runtime = AgentRuntime(store, ("全局甲", "全局乙"))
    bindings = [RoleBinding("a", "p", "self_play"),
                RoleBinding("b", "p", "frozen_reference", model_ref="zengen-v1"),
                RoleBinding("c", "p", "external_api", model_ref="gpt-x")]
    backends = _backends_of(runtime, _scenario_with({"b": ["剧本"]}), _assignment(*bindings), {}, "a")
    assert set(backends) == {"b", "c"}
    for slot in ("b", "c"):
        assert isinstance(backends[slot].instance.backend.inner, ScriptedBackend)
        assert _says(backends[slot], slot, 3) == ["全局甲", "全局乙", "(无更多发言)"]


def test_every_call_builds_fresh_backends(store):
    from simulator.datamodel import RoleBinding
    runtime = AgentRuntime(store, ("甲", "乙"))
    assignment = _assignment(RoleBinding("a", "p", "self_play"), RoleBinding("b", "p", "frozen_reference", model_ref="m"))
    first = _backends_of(runtime, _scenario_with(), assignment, {}, "a")
    assert _says(first["b"], "b", 1) == ["甲"]
    assert _says(_backends_of(runtime, _scenario_with(), assignment, {}, "a")["b"], "b", 1) == ["甲"]


def test_a_factory_supplies_the_non_scripted_drivers_and_never_the_scripted_ones(store):
    from simulator.datamodel import RoleBinding
    made = []

    def factory(binding, scenario):
        made.append((binding.slot_id, scenario.id))
        return ScriptedBackend({binding.slot_id: [f"来自工厂-{binding.slot_id}"]})

    runtime = AgentRuntime(store, ("全局",), opponent_backend_factory=factory)
    runtime.register_session("s1", "run-1")             # 工厂只服务训练登记过的会话(Ruling 28)
    bindings = [RoleBinding("a", "p", "self_play"),
                RoleBinding("b", "p", "frozen_reference", model_ref="zengen-v1"),
                RoleBinding("c", "p", "external_api", model_ref="gpt-x"),
                RoleBinding("d", "p", "frozen_reference", model_ref="zengen-v1", driver="scripted")]
    backends = _backends_of(runtime, _scenario_with({"d": ["剧本丁"]}), _assignment(*bindings), {}, "a")
    assert sorted(made) == [("b", "s1"), ("c", "s1")]   # 剧本驱动的 d 不经工厂
    assert _says(backends["b"], "b", 1) == ["来自工厂-b"] and _says(backends["c"], "c", 1) == ["来自工厂-c"]
    assert _says(backends["d"], "d", 3) == ["剧本丁"] * 3


def test_a_factory_error_surfaces_instead_of_being_swallowed(store):
    from simulator.datamodel import RoleBinding

    def factory(binding, scenario):
        raise RuntimeError("model not served")

    runtime = AgentRuntime(store, ("全局",), opponent_backend_factory=factory)
    runtime.register_session("s1", "run-1")
    assignment = _assignment(RoleBinding("a", "p", "self_play"), RoleBinding("b", "p", "frozen_reference", model_ref="m"))
    with pytest.raises(RuntimeError, match="model not served"):
        _backends_of(runtime, _scenario_with(), assignment, {}, "a")


def test_without_a_factory_the_legacy_path_does_not_judge_the_binding_at_all(store):
    """MindAgent 的 agent.yaml 不校验 model_backend 的取值,非客户端位也可能是 self_play:
    没传工厂又不是剧本驱动时,和过去一样给罐头后端,不能因为驱动判定抛 ValueError。"""
    from simulator.datamodel import RoleBinding
    runtime = AgentRuntime(store, ("全局甲", "全局乙"))
    bindings = [RoleBinding("a", "p", "self_play"),
                RoleBinding("b", "p", "self_play"),
                RoleBinding("c", "p", "scripted_user", model_ref="x")]
    backends = _backends_of(runtime, _scenario_with({"b": ["剧本"]}), _assignment(*bindings), {}, "a")
    assert set(backends) == {"b", "c"}
    for slot in ("b", "c"):
        assert _says(backends[slot], slot, 3) == ["全局甲", "全局乙", "(无更多发言)"]


def _never_called(binding, scenario):
    raise AssertionError(f"the opponent factory must not be asked about slot {binding.slot_id!r}")


@pytest.mark.parametrize("backend", ["self_play", "scripted_user", "magic"])
def test_with_the_factory_set_an_unregistered_session_never_judges_its_bindings(store, backend):
    """Ruling 28:--interact 下运行时的工厂一直是 OpponentSwitch,部署会话(没登记)仍按 Ruling 15 不判绑定:
    agent.yaml 不校验 model_backend,self_play 或未知后端名的非客户端位不能让部署轮次整轮失败。"""
    from simulator.datamodel import RoleBinding
    runtime = AgentRuntime(store, ("全局甲", "全局乙"), opponent_backend_factory=_never_called)
    bindings = [RoleBinding("a", "p", "self_play"),
                RoleBinding("b", "p", backend, model_ref=None if backend == "self_play" else "x")]
    backends = _backends_of(runtime, _scenario_with({"b": ["剧本"]}), _assignment(*bindings), {}, "a")
    assert set(backends) == {"b"} and isinstance(backends["b"].instance.backend.inner, ScriptedBackend)
    assert _says(backends["b"], "b", 3) == ["全局甲", "全局乙", "(无更多发言)"]     # 不循环,剧本也不生效


def test_with_the_factory_set_an_unregistered_scripted_binding_still_loops_its_script(store):
    from simulator.datamodel import RoleBinding
    runtime = AgentRuntime(store, ("全局",), opponent_backend_factory=_never_called)
    opponent = RoleBinding("b", "p", "frozen_reference", model_ref="zengen-v1", driver="scripted")
    backends = _backends_of(runtime, _scenario_with({"b": ["一", "二"]}),
                             _assignment(RoleBinding("a", "p", "self_play"), opponent), {}, "a")
    assert _says(backends["b"], "b", 5) == ["一", "二", "一", "二", "一"]


@pytest.mark.parametrize("backend", ["self_play", "magic"])
def test_with_the_factory_set_an_unregistered_explicit_scripted_driver_is_not_judged_either(store, backend):
    """显式 scripted 给循环的剧本后端,但不经 driver_for:它对 self_play 与未知后端名会抛。"""
    from simulator.datamodel import RoleBinding
    runtime = AgentRuntime(store, ("全局",), opponent_backend_factory=_never_called)
    opponent = RoleBinding("b", "p", backend, model_ref=None if backend == "self_play" else "x", driver="scripted")
    backends = _backends_of(runtime, _scenario_with({"b": ["一", "二"]}),
                             _assignment(RoleBinding("a", "p", "self_play"), opponent), {}, "a")
    assert _says(backends["b"], "b", 3) == ["一", "二", "一"]


@pytest.mark.parametrize("backend, driver", [("self_play", None), ("magic", None), ("frozen_reference", "magic")])
def test_a_registered_session_with_an_invalid_driver_still_fails(store, backend, driver):
    from simulator.datamodel import RoleBinding
    runtime = AgentRuntime(store, ("全局",), opponent_backend_factory=_never_called)
    runtime.register_session("s1", "run-1")
    bad = RoleBinding("b", "p", backend, model_ref=None if backend == "self_play" else "x", driver=driver)
    with pytest.raises(ValueError, match="slot 'b'"):
        _backends_of(runtime, _scenario_with(), _assignment(RoleBinding("a", "p", "self_play"), bad), {}, "a")


def _open_and_finish(runtime, scenario_id, bindings):
    """经运行时的 loopback 服务端跑完一条两轮会话;会话开不了时 create_session 抛 SessionRejectedError。"""
    from simulator.datamodel import Persona, RoleAssignment, RoleSlot, Scenario
    from simulator.store_adapter import write_scenario
    write_scenario(runtime._store, Scenario(scenario_id, "bg", [RoleSlot("b", "b"), RoleSlot("a", "a")],
                                            "c", ["n"], "training"))
    personas = {pid: Persona(pid, ["t"], "m", "s", ["v"]) for pid in ("pa", "pb")}
    client = runtime.client_factory()
    client.create_session(scenario_id=scenario_id, role_assignment=RoleAssignment(scenario_id, bindings, 2),
                          personas=personas, purpose="training", client_driven_slot="a")
    while not client.closed:
        client.submit_turn("嗯")
    return client.drain_until_end()


def test_a_deployment_round_with_a_self_play_opponent_slot_opens_and_finishes_under_the_switch(store):
    """真实 loopback + 真的 OpponentSwitch(没装工厂时它给罐头后端):部署会话的 self_play 对手位照常开得起来。"""
    from factory.interact_policy import OpponentSwitch
    from simulator.datamodel import RoleBinding
    runtime = AgentRuntime(store, ("罐头",), opponent_backend_factory=OpponentSwitch(("罐头",)))
    try:
        episode = _open_and_finish(runtime, "s-deploy", [RoleBinding("a", "pa", "self_play"),
                                                         RoleBinding("b", "pb", "self_play")])
    finally:
        runtime.close()
    assert episode.terminated_reason == "max_turns"


def test_a_registered_session_with_an_invalid_driver_is_still_refused_by_the_server(store):
    from factory.interact_policy import OpponentSwitch
    from simulator.datamodel import RoleBinding
    from simulator.realtime.client import SessionRejectedError
    runtime = AgentRuntime(store, ("罐头",), opponent_backend_factory=OpponentSwitch(("罐头",)))
    runtime.register_session("s-train", "run-1")
    try:
        with pytest.raises(SessionRejectedError) as caught:
            _open_and_finish(runtime, "s-train", [RoleBinding("a", "pa", "self_play"),
                                                  RoleBinding("b", "pb", "self_play")])
    finally:
        runtime.close()
    assert caught.value.reason == "role_backend_failure"


def test_a_scripted_driver_survives_the_grpc_hop_and_loops_the_scenario_script(store):
    """真实 loopback:driver 经 CreateSession 过 gRPC 到服务端,服务端按它给对手位挂循环的剧本后端。
    直接调 _backends 的用例看不见 codec 丢字段。"""
    from simulator.datamodel import Persona, RoleBinding, RoleSlot, Scenario
    from simulator.store_adapter import write_scenario
    write_scenario(store, Scenario("s-script", "bg", [RoleSlot("b", "b"), RoleSlot("a", "a")], "c", ["n"], "training",
                                   opponent_script={"b": ["剧本一", "剧本二"]}))
    personas = {pid: Persona(pid, ["t"], "m", "s", ["v"]) for pid in ("pa", "pb")}
    assignment = _assignment(RoleBinding("a", "pa", "self_play"),
                             RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1", driver="scripted"))
    assignment.max_turns = 8
    runtime = AgentRuntime(store, ("全局",))
    try:
        client = runtime.client_factory()
        client.create_session(scenario_id="s-script", role_assignment=assignment, personas=personas,
                              purpose="training", client_driven_slot="a")
        heard = [t.utterance for t in client.opening_turns]
        while not client.closed:
            heard += [t.utterance for t in client.submit_turn("嗯")]
        client.drain_until_end()
    finally:
        runtime.close()
    assert heard == ["剧本一", "剧本二", "剧本一", "剧本二"]


def _slot_info(scenario, assignment, *, session_id="sess-1", episode_id="run-1-grpo0-s1-r0", client_slot="a"):
    from simulator.realtime.resources import SessionInfo
    return SessionInfo(scenario, assignment, {}, client_slot, session_id, episode_id)


def _two_slots():
    from simulator.datamodel import RoleBinding
    return _assignment(RoleBinding("a", "pa", "self_play"),
                       RoleBinding("b", "pb", "frozen_reference", model_ref="zengen-v1", driver="scripted"))


def test_every_opponent_slot_is_an_actio_instance_with_its_own_empty_memory(store):
    from actio.memory import TupleMemory
    from factory.memory_translate import MemoryTranslatingBackend
    runtime = AgentRuntime(store, ("全局",))
    runtime.register_session("s1", "run-1")
    first = runtime._backends(_slot_info(_scenario_with(), _two_slots()))
    second = runtime._backends(_slot_info(_scenario_with(), _two_slots(), session_id="sess-2"))
    backend = first.backends["b"]
    assert isinstance(backend, MemoryTranslatingBackend)
    inst = backend.instance
    assert inst.episode_id == "run-1-grpo0-s1-r0"
    assert inst.interlocutors == {"a": "pa", "b": "pb"}
    assert isinstance(inst.memory, TupleMemory) and inst.memory.digest() == TupleMemory().digest()
    assert inst.memory is not second.backends["b"].instance.memory
    assert inst.skills == () and inst.library_version == {"skill": "0", "memory": "0"}
    first.close()
    second.close()


def test_the_opponent_lines_are_unchanged_and_each_turn_carries_a_v2_trace(store):
    runtime = AgentRuntime(store, ("全局",))
    resources = runtime._backends(_slot_info(_scenario_with({"b": ["剧本乙"]}), _two_slots()))
    utterance, thought, trace = resources.backends["b"].respond(None, _scenario_with(), [], "b")
    assert (utterance, thought) == ("剧本乙", None)
    assert trace["schema"] == "actio.trace.v2" and trace["memory_hits"] == []
    resources.close()


def test_a_deployment_session_gets_actio_opponents_too_and_no_route(store):
    """部署会话(没登记)的对手位也换成 Actio 实例,底座仍是原来的 ScriptedBackend(设计源 7.4 节)。"""
    from simulator.datamodel import RoleBinding
    runtime = AgentRuntime(store, ("全局甲", "全局乙"))
    bindings = [RoleBinding("a", "p", "self_play"), RoleBinding("b", "p", "frozen_reference", model_ref="x")]
    resources = runtime._backends(_slot_info(_scenario_with(), _assignment(*bindings), episode_id="deploy-m-s1-0"))
    assert isinstance(resources.backends["b"].instance.backend.inner, ScriptedBackend)
    assert runtime._session_runs == {}
    resources.close()


def test_closing_the_session_closes_every_opponent_instance(store):
    from factory.memory_translate import FactoryMemoryFailureError
    runtime = AgentRuntime(store, ("全局",))
    runtime.register_session("s1", "run-1")
    resources = runtime._backends(_slot_info(_scenario_with(), _two_slots()))
    resources.close()
    with pytest.raises(FactoryMemoryFailureError, match="MemoryClosedError"):
        resources.backends["b"].respond(None, _scenario_with(), [], "b")
    assert runtime._session_runs == {}


def test_a_factory_failure_closes_the_instances_already_built(store, monkeypatch):
    import factory.agent as agent_mod
    from factory.memory_translate import FactoryMemoryFailureError
    from simulator.datamodel import RoleBinding
    made = []
    real = agent_mod.memory_role_backend

    def recording(*args, **kwargs):
        made.append(real(*args, **kwargs))
        return made[-1]

    def factory(binding, scenario):
        raise RuntimeError("factory exploded")

    monkeypatch.setattr(agent_mod, "memory_role_backend", recording)
    runtime = AgentRuntime(store, ("全局",), opponent_backend_factory=factory)
    runtime.register_session("s1", "run-1")
    bindings = [RoleBinding("a", "p", "self_play"),
                RoleBinding("b", "p", "frozen_reference", model_ref="x", driver="scripted"),
                RoleBinding("c", "p", "frozen_reference", model_ref="x")]
    with pytest.raises(RuntimeError, match="factory exploded"):
        runtime._backends(_slot_info(_scenario_with(), _assignment(*bindings)))
    assert len(made) == 1
    with pytest.raises(FactoryMemoryFailureError):
        made[0].respond(None, _scenario_with(), [], "b")
    assert runtime._session_runs == {}


# ---------------- AgentRuntime 的会话登记与 episode 落盘(--interact 与部署共用一个运行时) ----------------

def _play_session(runtime, scenario_id):
    """经运行时的真实 loopback 服务端跑完一条两轮会话,返回客户端重建的 episode。"""
    from simulator.datamodel import Persona, RoleAssignment, RoleBinding, RoleSlot, Scenario
    from simulator.store_adapter import write_scenario
    if not runtime._store.exists(f"control-plane/scenarios/{scenario_id}.yaml"):
        write_scenario(runtime._store, Scenario(scenario_id, "bg", [RoleSlot("b", "b"), RoleSlot("a", "a")],
                                                "c", ["n"], "training"))
    personas = {pid: Persona(pid, ["t"], "m", "s", ["v"]) for pid in ("pa", "pb")}
    assignment = RoleAssignment(scenario_id, [RoleBinding("a", "pa", "self_play"),
                                              RoleBinding("b", "pb", "frozen_reference")], 2)
    client = runtime.client_factory()
    client.create_session(scenario_id=scenario_id, role_assignment=assignment, personas=personas,
                          purpose="training", client_driven_slot="a")
    while not client.closed:
        client.submit_turn("嗯")
    return client.drain_until_end()


def _stored_episode_ids(store, run_id):
    from controlplane.paths import episodes_dir
    return [p.rsplit("/", 1)[-1].removesuffix(".yaml") for p in store.list_products(episodes_dir(run_id))]


def test_a_registered_session_is_written_to_its_runs_directory(store):
    from simulator.store_adapter import read_episode
    runtime = AgentRuntime(store, ("hi",))
    try:
        runtime.register_session("s-int", "run-int")
        episode = _play_session(runtime, "s-int")
        assert _stored_episode_ids(store, "run-int") == [episode.id]
        assert read_episode(store, "run-int", episode.id).scenario_id == "s-int"
        assert list(runtime.episode_write_failures) == []
    finally:
        runtime.close()


def test_an_unregistered_session_is_not_written_so_deployment_keeps_its_own_driver_in_charge(store):
    """部署会话没有训练登记,由 MindAgent 驱动器自己落盘;钩子再写一遍就是同一条 episode 被两边各写一次。"""
    runtime = AgentRuntime(store, ("hi",))
    try:
        runtime.register_session("s-int", "run-int")
        _play_session(runtime, "s-deploy")
        assert _stored_episode_ids(store, "run-int") == []
        assert not any(store.list_products(f"control-plane/runs/{d}/episodes")
                       for d in ("run-int", "s-deploy", "run-deploy"))
        assert list(runtime.episode_write_failures) == []
    finally:
        runtime.close()


def test_unregistering_stops_later_episodes_of_that_scenario_from_being_written(store):
    runtime = AgentRuntime(store, ("hi",))
    try:
        runtime.register_session("s-int", "run-int")
        first = _play_session(runtime, "s-int")
        runtime.unregister_session("s-int")
        _play_session(runtime, "s-int")
        assert _stored_episode_ids(store, "run-int") == [first.id]
        runtime.unregister_session("s-int")           # 重复注销是空操作
        runtime.unregister_session("never-registered")
    finally:
        runtime.close()


def test_a_session_started_before_unregistering_is_still_written_to_its_run(store):
    """落盘按 session_id 路由:策略上下文注销情境时,已经在跑的会话照样落到自己的 run(设计源 6.1 节)。"""
    from simulator.datamodel import Persona, RoleAssignment, RoleBinding, RoleSlot, Scenario
    from simulator.store_adapter import write_scenario
    write_scenario(store, Scenario("s-int", "bg", [RoleSlot("b", "b"), RoleSlot("a", "a")], "c", ["n"], "training"))
    runtime = AgentRuntime(store, ("hi",))
    try:
        runtime.register_session("s-int", "run-int")
        client = runtime.client_factory()
        client.create_session(
            scenario_id="s-int", personas={pid: Persona(pid, ["t"], "m", "s", ["v"]) for pid in ("pa", "pb")},
            role_assignment=RoleAssignment("s-int", [RoleBinding("a", "pa", "self_play"),
                                                     RoleBinding("b", "pb", "frozen_reference")], 2),
            purpose="training", client_driven_slot="a")
        runtime.unregister_session("s-int")                  # 会话已经开了
        while not client.closed:
            client.submit_turn("嗯")
        episode = client.drain_until_end()
        assert _stored_episode_ids(store, "run-int") == [episode.id]
    finally:
        runtime.close()


def _info(session_id):
    from simulator.datamodel import RoleBinding
    from simulator.realtime.resources import SessionInfo
    assignment = _assignment(RoleBinding("a", "p", "self_play"), RoleBinding("b", "q", "frozen_reference"))
    return SessionInfo(_scenario_with(), assignment, {}, "a", session_id, f"ep-{session_id}")


def test_the_session_route_is_dropped_when_the_resources_close(store):
    runtime = AgentRuntime(store, ("hi",))
    runtime.register_session("s1", "run-1")
    resources = runtime._backends(_info("sess-route"))
    assert runtime._session_runs == {"sess-route": "run-1"}
    resources.close()
    assert runtime._session_runs == {}


def test_no_route_is_recorded_for_a_scenario_that_is_not_registered(store):
    """没登记的(部署会话)不记路由,也就没有收尾要做的事。"""
    runtime = AgentRuntime(store, ("hi",))
    resources = runtime._backends(_info("sess-deploy"))
    assert runtime._session_runs == {}
    resources.close()
    assert runtime._session_runs == {}


def test_a_failure_while_building_the_backends_leaves_no_route_behind(store):
    """工厂不留半开状态(设计源 6.2 节):造后端抛异常时,已记的路由要先撤掉再把原异常抛出去。"""
    def boom(binding, scenario):
        raise RuntimeError("no model")

    runtime = AgentRuntime(store, ("hi",), opponent_backend_factory=boom)
    runtime.register_session("s1", "run-1")
    with pytest.raises(RuntimeError, match="no model"):
        runtime._backends(_info("sess-fail"))
    assert runtime._session_runs == {}


def test_each_session_keeps_its_own_route_even_when_the_scenario_is_registered_to_a_new_run(store):
    """同一个情境 id 先后登记给两个 run:先开的会话仍按开会话时的 run 落盘。"""
    runtime = AgentRuntime(store, ("hi",))
    runtime.register_session("s1", "run-old")
    old = runtime._backends(_info("sess-old"))
    runtime.register_session("s1", "run-new")
    new = runtime._backends(_info("sess-new"))
    assert runtime._session_runs == {"sess-old": "run-old", "sess-new": "run-new"}
    old.close()
    assert runtime._session_runs == {"sess-new": "run-new"}
    new.close()
    assert runtime._session_runs == {}


def test_a_failing_write_is_recorded_and_surfaces_to_the_client_as_persistence_failed(store, monkeypatch):
    """钩子必须把异常再抛出去:servicer 看到钩子抛错,才会把结束原因改报 persistence_failed 并清空 episode_id,
    轨迹源据此把它当作可重跑的瞬时失败。钩子若吞掉异常,客户端会收到正常的结束原因与 episode id,
    把一条树里没有的 episode 当成已落盘。清单里只留类名、episode id 与 run id,不留异常文字(可能带凭证)。"""
    import factory.agent as agent_mod

    def boom(store_, run_id, episode):
        raise RuntimeError("push to https://user:sk-secret-123@git.example failed")

    class RecordingStub:
        """包住真实 stub,记下服务端发来的每条 end_session(客户端自己不读 end_session.episode_id)。"""
        ends: list = []

        def __init__(self, channel):
            self._stub = pb2_grpc.EnvironmentServiceStub(channel)

        def Interact(self, request_iterator, **kw):
            for msg in self._stub.Interact(request_iterator, **kw):
                if msg.WhichOneof("payload") == "end_session":
                    RecordingStub.ends.append(msg.end_session)
                yield msg

    from simulator.realtime.proto import realtime_env_pb2_grpc as pb2_grpc
    monkeypatch.setattr(agent_mod, "write_episode", boom)
    runtime = AgentRuntime(store, ("hi",), stub_factory=RecordingStub)
    try:
        runtime.register_session("s-int", "run-int")
        episode = _play_session(runtime, "s-int")      # 会话照常收尾,客户端不抛
        # (a) 失败记进清单
        assert len(runtime.episode_write_failures) == 1
        (line,) = runtime.episode_write_failures
        assert episode.id in line and "run-int" in line and "RuntimeError" in line
        assert "sk-secret-123" not in line and "git.example" not in line
        # (b) 客户端看到的是 persistence_failed,线上的 episode_id 为空
        assert episode.terminated_reason == "persistence_failed"
        (end,) = RecordingStub.ends
        assert end.reason == "persistence_failed" and end.episode_id == ""
        # (c) run 目录里什么都没有
        assert _stored_episode_ids(store, "run-int") == []
    finally:
        runtime.close()


def test_the_write_failure_list_is_bounded_and_keeps_the_latest(store, monkeypatch):
    """守护进程长跑时每次落盘失败追加一条;不设上限就是慢性泄漏。只留最近的 100 条。"""
    import factory.agent as agent_mod
    from types import SimpleNamespace

    def boom(store_, run_id, episode):
        raise OSError("disk full")

    monkeypatch.setattr(agent_mod, "write_episode", boom)
    runtime = AgentRuntime(store, ("hi",))
    try:
        for i in range(150):
            runtime._session_runs[f"sess-{i}"] = "run-int"       # 钩子只查 session_id 的路由表
            with pytest.raises(OSError):
                runtime._on_session_closed(f"sess-{i}", SimpleNamespace(scenario_id="s-int", id=f"ep-{i}"))
        kept = list(runtime.episode_write_failures)
        assert len(kept) == 100
        assert "ep-149" in kept[-1] and "ep-50" in kept[0]          # 丢的是最早的 50 条
        assert all("OSError" in line and "disk full" not in line for line in kept)
    finally:
        runtime.close()


def test_reading_the_deadline_does_not_take_the_runtime_lock(store):
    """getter 只读一个 float(原子),不取锁:锁不可重入,持锁的代码一旦碰到 getter 就是自死锁。
    在另一个线程里读,主线程持着锁;getter 若取锁,线程会一直挂着,join 超时。"""
    import threading

    runtime = AgentRuntime(store, ("hi",), interact_deadline_s=7.0)
    seen = []
    try:
        with runtime._lock:
            reader = threading.Thread(target=lambda: seen.append(runtime.interact_deadline_s), daemon=True)
            reader.start()
            reader.join(timeout=2.0)
            assert not reader.is_alive(), "reading interact_deadline_s blocked on the runtime lock"
        assert seen == [7.0]
    finally:
        runtime.close()


def test_the_episode_is_written_outside_the_runtime_lock(store, monkeypatch):
    """close() 持锁停服务端并等它停完;钩子若持锁写文件,两边会互相等。"""
    import factory.agent as agent_mod
    held = []
    real = agent_mod.write_episode

    def spy(store_, run_id, episode):
        held.append(runtime._lock.locked())
        return real(store_, run_id, episode)

    monkeypatch.setattr(agent_mod, "write_episode", spy)
    runtime = AgentRuntime(store, ("hi",))
    try:
        runtime.register_session("s-int", "run-int")
        _play_session(runtime, "s-int")
        assert held == [False]
    finally:
        runtime.close()


def test_closing_a_session_and_routing_its_episode_never_wait_for_the_runtime_lock(store, monkeypatch):
    """runtime.close() 持 _lock 停服务端并等在途处理器退出;处理器收尾时跑的 drop 与钩子里的路由查询若也取 _lock,
    两边互相等,关停就挂住。路由表有自己的锁:主线程持着 _lock,另一个线程里关资源、查路由都要在限时内做完。"""
    import threading
    from types import SimpleNamespace
    import factory.agent as agent_mod

    written = []
    monkeypatch.setattr(agent_mod, "write_episode", lambda store_, run_id, episode: written.append((run_id, episode.id)))
    runtime = AgentRuntime(store, ("hi",))
    runtime.register_session("s1", "run-1")
    resources = runtime._backends(_info("sess-lock"))
    assert runtime._session_runs == {"sess-lock": "run-1"}

    def wind_down():
        runtime._on_session_closed("sess-lock", SimpleNamespace(scenario_id="s1", id="ep-lock"))   # 路由查询
        resources.close()                                                                           # 跑 drop

    worker = threading.Thread(target=wind_down, daemon=True)
    with runtime._lock:                                   # 模拟 runtime.close() 正持着锁等处理器退出
        worker.start()
        worker.join(timeout=2.0)
        finished = not worker.is_alive()
    worker.join(timeout=5.0)                              # 放锁后无论如何让线程收尾,不把线程留给后面的用例
    assert finished, "session wind-down blocked on the runtime lock"
    assert written == [("run-1", "ep-lock")]
    assert runtime._session_runs == {}


def test_the_interact_deadline_is_settable_and_read_when_a_client_is_built(store):
    """调和循环一次只派一条指令,所以一个可变值够用。设值之后新造的客户端用新值;已造好的 stub 不回头改。"""
    from factory.agent import INTERACT_DEADLINE_S
    calls = []

    class FakeStub:
        def __init__(self, channel):
            pass

        def Interact(self, request_iterator, **kw):
            calls.append(kw)
            return iter(())

    runtime = AgentRuntime(store, ("hi",), stub_factory=FakeStub)
    try:
        assert runtime.interact_deadline_s == INTERACT_DEADLINE_S
        runtime.interact_deadline_s = 7.0
        assert runtime.interact_deadline_s == 7.0
        built_before = runtime.client_factory()
        assert calls[-1] == {"timeout": 7.0}
        runtime.interact_deadline_s = 3.0
        assert built_before._stub.timeout_s == 7.0     # 已造好的不变
        runtime.client_factory()
        assert calls[-1] == {"timeout": 3.0}
    finally:
        runtime.close()
