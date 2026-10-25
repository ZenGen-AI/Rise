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


def _says(backend, slot, times):
    return [backend.respond(None, None, [], slot)[0] for _ in range(times)]


def test_a_scripted_driver_loops_the_scenario_script_for_its_slot(store):
    from simulator.datamodel import RoleBinding
    runtime = AgentRuntime(store, ("全局",))
    opponent = RoleBinding("b", "p", "frozen_reference", model_ref="zengen-v1", driver="scripted")
    backends = runtime._backends(_scenario_with({"b": ["一", "二"]}),
                                 _assignment(RoleBinding("a", "p", "self_play"), opponent), {}, "a")
    assert set(backends) == {"b"}
    assert _says(backends["b"], "b", 5) == ["一", "二", "一", "二", "一"]


@pytest.mark.parametrize("script", [None, {}, {"b": []}, {"c": ["别人的"]}])
def test_a_scripted_driver_falls_back_to_the_global_lines_when_the_slot_has_no_script(store, script):
    from simulator.datamodel import RoleBinding
    runtime = AgentRuntime(store, ("全局甲", "全局乙"))
    opponent = RoleBinding("b", "p", "external_api", model_ref="gpt-x", driver="scripted")
    backends = runtime._backends(_scenario_with(script),
                                 _assignment(RoleBinding("a", "p", "self_play"), opponent), {}, "a")
    assert _says(backends["b"], "b", 3) == ["全局甲", "全局乙", "全局甲"]


def test_without_a_factory_every_opponent_is_the_same_unlooped_scripted_backend_as_before(store):
    """MindAgent 部署会话的绑定多是 frozen_reference:没传工厂时一个字不能变,剧本也不能偷偷生效。"""
    from simulator.datamodel import RoleBinding
    runtime = AgentRuntime(store, ("全局甲", "全局乙"))
    bindings = [RoleBinding("a", "p", "self_play"),
                RoleBinding("b", "p", "frozen_reference", model_ref="zengen-v1"),
                RoleBinding("c", "p", "external_api", model_ref="gpt-x")]
    backends = runtime._backends(_scenario_with({"b": ["剧本"]}), _assignment(*bindings), {}, "a")
    assert set(backends) == {"b", "c"}
    for slot in ("b", "c"):
        assert isinstance(backends[slot], ScriptedBackend)
        assert _says(backends[slot], slot, 3) == ["全局甲", "全局乙", "(无更多发言)"]


def test_every_call_builds_fresh_backends(store):
    from simulator.datamodel import RoleBinding
    runtime = AgentRuntime(store, ("甲", "乙"))
    assignment = _assignment(RoleBinding("a", "p", "self_play"), RoleBinding("b", "p", "frozen_reference", model_ref="m"))
    first = runtime._backends(_scenario_with(), assignment, {}, "a")
    assert _says(first["b"], "b", 1) == ["甲"]
    assert _says(runtime._backends(_scenario_with(), assignment, {}, "a")["b"], "b", 1) == ["甲"]


def test_a_factory_supplies_the_non_scripted_drivers_and_never_the_scripted_ones(store):
    from simulator.datamodel import RoleBinding
    made = []

    def factory(binding, scenario):
        made.append((binding.slot_id, scenario.id))
        return ScriptedBackend({binding.slot_id: [f"来自工厂-{binding.slot_id}"]})

    runtime = AgentRuntime(store, ("全局",), opponent_backend_factory=factory)
    bindings = [RoleBinding("a", "p", "self_play"),
                RoleBinding("b", "p", "frozen_reference", model_ref="zengen-v1"),
                RoleBinding("c", "p", "external_api", model_ref="gpt-x"),
                RoleBinding("d", "p", "frozen_reference", model_ref="zengen-v1", driver="scripted")]
    backends = runtime._backends(_scenario_with({"d": ["剧本丁"]}), _assignment(*bindings), {}, "a")
    assert sorted(made) == [("b", "s1"), ("c", "s1")]   # 剧本驱动的 d 不经工厂
    assert _says(backends["b"], "b", 1) == ["来自工厂-b"] and _says(backends["c"], "c", 1) == ["来自工厂-c"]
    assert _says(backends["d"], "d", 3) == ["剧本丁"] * 3


def test_a_factory_error_surfaces_instead_of_being_swallowed(store):
    from simulator.datamodel import RoleBinding

    def factory(binding, scenario):
        raise RuntimeError("model not served")

    runtime = AgentRuntime(store, ("全局",), opponent_backend_factory=factory)
    assignment = _assignment(RoleBinding("a", "p", "self_play"), RoleBinding("b", "p", "frozen_reference", model_ref="m"))
    with pytest.raises(RuntimeError, match="model not served"):
        runtime._backends(_scenario_with(), assignment, {}, "a")


def test_without_a_factory_the_legacy_path_does_not_judge_the_binding_at_all(store):
    """MindAgent 的 agent.yaml 不校验 model_backend 的取值,非客户端位也可能是 self_play:
    没传工厂又不是剧本驱动时,和过去一样给罐头后端,不能因为驱动判定抛 ValueError。"""
    from simulator.datamodel import RoleBinding
    runtime = AgentRuntime(store, ("全局甲", "全局乙"))
    bindings = [RoleBinding("a", "p", "self_play"),
                RoleBinding("b", "p", "self_play"),
                RoleBinding("c", "p", "scripted_user", model_ref="x")]
    backends = runtime._backends(_scenario_with({"b": ["剧本"]}), _assignment(*bindings), {}, "a")
    assert set(backends) == {"b", "c"}
    for slot in ("b", "c"):
        assert _says(backends[slot], slot, 3) == ["全局甲", "全局乙", "(无更多发言)"]


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
