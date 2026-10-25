"""`--interact` 装配(设计 90 §6.1 的 4c,十一条拒绝项)。"""
from __future__ import annotations

import pytest

from controlplane.gitstore import GitStore
from factory.cli import _options, build_parser, main
from factory.errors import FactoryConfigError
from factory.wire import FactoryOptions, assemble


@pytest.fixture
def repo(tmp_path):
    s = GitStore(str(tmp_path / "cp"))
    s.init()
    return s


def _commit_count(store) -> int:
    """空仓库安全:还没有提交时 rev-list HEAD 会抛;--all 返回 0。"""
    return int(store._git("rev-list", "--all", "--count").strip())


# ---------------- Part 1:选项与命令行 ----------------

def test_interact_defaults_to_off():
    assert FactoryOptions().interact is False
    args = build_parser().parse_args(["run", "/tmp/x"])
    assert args.interact is False


@pytest.mark.parametrize("command", [["run"], ["reconcile", "--once"]])
def test_the_flag_parses_on_run_and_reconcile_and_reaches_the_options(command):
    args = build_parser().parse_args([command[0], "/tmp/x", "--interact", *command[1:]])
    assert args.interact is True
    assert _options(args).interact is True


def test_interact_with_the_roll_executor_is_refused_at_option_level(repo):
    with pytest.raises(FactoryConfigError, match=r"--interact.*--executor roll"):
        assemble(repo, FactoryOptions(interact=True, executor="roll"))


def test_interact_with_roll_exits_2_with_one_line_and_writes_nothing(repo, capsys):
    before = _commit_count(repo)
    code = main(["run", str(repo.repo_root), "--interact", "--executor", "roll", "--max-ticks", "1"], env={})
    err = capsys.readouterr().err
    assert code == 2
    assert err.startswith("factory: error:") and "--executor roll" in err
    assert len(err.strip().splitlines()) == 1
    assert _commit_count(repo) == before


def test_an_endpoint_name_is_now_allowed_with_interact_alone(repo):
    """放宽后,点名端点的判断不再在选项层挡住 --interact;后续装配步骤才读端点配置。"""
    with pytest.raises(FactoryConfigError) as caught:
        assemble(repo, FactoryOptions(endpoint="gpu-1"))
    assert "--interact" in str(caught.value)             # 报错同时点名两种允许的用法
    try:
        assemble(repo, FactoryOptions(endpoint="gpu-1", interact=True))
    except FactoryConfigError as exc:
        assert "only makes sense" not in str(exc)      # 不再是选项层的用法错误



# ---------------- 共用:端点配置与主机映射 ----------------

ENDPOINT = {
    "endpoint_id": "gpu-1", "work_root": "/srv/rise/work", "roll_root": "/srv/roll", "launch_prefix": [],
    "base_model_path": "/srv/models/qwen-4b", "heldout_jsonl": "/srv/roll/data/heldout.jsonl",
    "sft_template": "/srv/roll/configs/sft_lora.yaml", "min_free_host_mem_gib": 1.0,
    "min_free_disk_gib": 1.0, "poll_seconds": 1.0, "job_timeout_seconds": 60.0,
}


def write_endpoints(store, *ids):
    from controlplane.envelope import Envelope, OwnerRef
    from controlplane.paths import compute_endpoints_file

    store.write_product(compute_endpoints_file(), Envelope(
        id="cfg-compute-endpoints", type="config", version=1, created_at="2026-10-17T00:00:00Z",
        created_by=OwnerRef(), status="effective", schema_version=1,
        payload={"endpoints": [{**ENDPOINT, "endpoint_id": i} for i in ids]}), "test: endpoints")


def hosts_env(tmp_path, text="gpu-1: gpu-181\n"):
    path = tmp_path / "hosts.yaml"
    path.write_text(text, encoding="utf-8")
    return {"RISMFORGE_COMPUTE_HOSTS": str(path)}


# ---------------- Part 2:一个共用的运行时 ----------------

@pytest.fixture
def runtimes(monkeypatch):
    """记下每个被构造的 AgentRuntime(按构造次数数,而不是按持有者数)。"""
    from factory.agent import AgentRuntime

    made = []
    real_init = AgentRuntime.__init__

    def init(self, *args, **kwargs):
        made.append(self)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(AgentRuntime, "__init__", init)
    return made


@pytest.fixture
def dispatcher_kwargs(monkeypatch):
    import factory.wire as wire

    seen = []
    real = wire.make_mindtrainer_dispatcher

    def spy(**kwargs):
        seen.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(wire, "make_mindtrainer_dispatcher", spy)
    return seen


@pytest.fixture
def handler_kwargs(monkeypatch):
    import factory.handlers as handlers

    seen = []

    def spy(**kwargs):
        seen.append(kwargs)
        return lambda store, action: None

    monkeypatch.setattr(handlers, "make_start_round_handler", spy)
    return seen


@pytest.fixture
def defaults_repo(tmp_path):
    from factory_fixtures import closed_loop_repo

    store = closed_loop_repo(str(tmp_path / "cp"), run_id=None)
    write_endpoints(store, "gpu-1")
    return store


@pytest.fixture
def env(tmp_path):
    return hosts_env(tmp_path)


def test_interact_and_the_agent_switch_share_one_runtime(
        defaults_repo, env, runtimes, dispatcher_kwargs, handler_kwargs):
    asm = assemble(defaults_repo, FactoryOptions(switches=("agent",), interact=True), env=env)
    try:
        assert len(runtimes) == 1
        from_dispatcher = dispatcher_kwargs[-1]["client_factory"].__self__
        from_handler = handler_kwargs[-1]["client_factory"].__self__
        assert from_dispatcher is from_handler is runtimes[0]
        assert [close.__self__ for close in asm.closers] == [runtimes[0]]      # 只登记一次收尾
    finally:
        asm.close()


def test_interact_alone_builds_one_runtime_and_a_longer_turn_timeout(
        defaults_repo, env, runtimes, dispatcher_kwargs):
    asm = assemble(defaults_repo, FactoryOptions(interact=True), env=env)
    try:
        assert len(runtimes) == 1
        assert dispatcher_kwargs[-1]["client_factory"].__self__ is runtimes[0]
        assert runtimes[0]._timeout == 30.0
        assert type(dispatcher_kwargs[-1]["data_producer"]).__name__ == "TemplateDataProducer"
    finally:
        asm.close()


def test_the_agent_switch_alone_keeps_the_default_turn_timeout_and_builds_one_runtime(
        defaults_repo, runtimes, handler_kwargs):
    asm = assemble(defaults_repo, FactoryOptions(switches=("agent",)), env={})
    try:
        assert len(runtimes) == 1
        assert handler_kwargs[-1]["client_factory"].__self__ is runtimes[0]
        assert runtimes[0]._timeout == 5.0
        assert [close.__self__ for close in asm.closers] == [runtimes[0]]
    finally:
        asm.close()


def test_without_either_there_is_no_runtime(defaults_repo, runtimes):
    asm = assemble(defaults_repo, FactoryOptions(switches=("track",)), env={})
    assert runtimes == [] and asm.closers == ()


def test_the_context_carries_the_runtime_to_every_builder(defaults_repo, env, runtimes):
    from factory_fixtures import Recorder, recording_builders

    seen: list = []
    asm = assemble(defaults_repo, FactoryOptions(switches=("agent", "track"), interact=True), env=env,
                   builders=recording_builders(Recorder(), seen_contexts=seen))
    try:
        assert seen and all(ctx.runtime is runtimes[0] for ctx in seen)
    finally:
        asm.close()


def test_the_runtime_is_closed_when_interact_runs_without_any_switch(defaults_repo, env, runtimes, monkeypatch):
    """--interact 不带 --enable 时没有处理器,但运行时仍要收尾:reconcile 与 run 两条路径都得关它。"""
    from factory.agent import AgentRuntime
    from factory.wire import reconcile_one_tick, run

    closed: list = []
    real_close = AgentRuntime.close
    monkeypatch.setattr(AgentRuntime, "close", lambda self: (closed.append(self), real_close(self)))
    reconcile_one_tick(str(defaults_repo.repo_root), FactoryOptions(interact=True), env=env)
    assert len(runtimes) == 1 and closed == runtimes
    run(str(defaults_repo.repo_root), FactoryOptions(interact=True), max_ticks=1, interval_s=0.0, env=env)
    assert len(runtimes) == 2 and closed == runtimes


def test_a_failed_handler_build_closes_the_runtime(defaults_repo, env, runtimes, monkeypatch):
    from factory.agent import AgentRuntime

    closed: list = []
    monkeypatch.setattr(AgentRuntime, "close", lambda self: closed.append(self))

    def boom(ctx):
        raise FactoryConfigError("builder exploded")

    with pytest.raises(FactoryConfigError, match="builder exploded"):
        assemble(defaults_repo, FactoryOptions(switches=("track",), interact=True), env=env,
                 builders={"record_track": boom})
    assert closed == runtimes and len(runtimes) == 1


# ---------------- Part 3:策略工厂与对手 ----------------
# serve 是注入的假替身(不连 SSH、不起 vLLM),HTTP 对着本机 http.server 桩;不 mock urllib。

import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

from compute.endpoint import ComputeEndpoint
from compute.errors import ComputeBusyError, ComputeError, HelperError, ServeStartupError
from compute.service import ServiceHandle
from compute.vllm_service import BASE_MODEL_NAME
from controlplane.config import write_config
from controlplane.daemon import DirectiveFailedError, reconcile_once
from controlplane.envelope import BigRef, Envelope, OwnerRef
from controlplane.paths import episodes_dir, model_file, run_dir, training_defaults_file
from factory.agent import AgentRuntime
from factory.interact_policy import OpponentSwitch, make_policy_factory
from factory.opponents import EXPORT_FORMAT
from mindtrainer.training_config import RewardConfig, TrainingConfig
from simulator.datamodel import Persona, RoleBinding, RoleSlot, Scenario
from simulator.store_adapter import write_persona, write_scenario
from trainscheduler.config import default_training_defaults_payload

RUN = "run-int-1"
SERVE_KEY = "vllm-key-must-not-leak"
OPP_KEY_ENV = "OPP_API_KEY"
OPP_KEY = "sk-opponent-secret"
BASE = BigRef("file:///ckpt/base", "a" * 64, 1)
TEACHER = BigRef("file:///ckpt/teacher", "b" * 64, 1)
REWARD = RewardConfig(["goal"], "outcome_split", "sum", {"goal": ["agreed"]})
CYCLE = ["agreed", "x", "no", "x"]                       # 训练位每个 episode 说两句:一条拿奖励,一条不拿


class ChatStub:
    """本机 OpenAI 兼容桩:按到达顺序循环回 CYCLE;记下每次请求的 model 与 Authorization。"""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self._lock = threading.Lock()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                with stub._lock:
                    stub.requests.append({"model": body["model"], "auth": self.headers.get("Authorization")})
                    reply = CYCLE[(len(stub.requests) - 1) % len(CYCLE)]
                raw = json.dumps({"choices": [{"message": {"role": "assistant", "content": reply}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def models(self) -> set[str]:
        with self._lock:
            return {r["model"] for r in self.requests}

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class FakeServe:
    """照 compute.service.serve 的签名;记下被要求起服务的端点与适配器,产出指向桩的 ServiceHandle。"""

    def __init__(self, base_url: str, *, enter_raises: Exception | None = None) -> None:
        self.base_url = base_url
        self.enter_raises = enter_raises
        self.calls: list[tuple[str, tuple]] = []        # (端点 id, 适配器列表)
        self.trace: list[str] = []

    def __call__(self, client, cfg, **kw):
        self.calls.append((client.endpoint.endpoint_id, cfg.adapters))
        return self._context(cfg)

    @contextmanager
    def _context(self, cfg):
        self.trace.append("enter")
        if self.enter_raises is not None:
            raise self.enter_raises
        try:
            yield ServiceHandle("serve-job-a1", self.base_url, SERVE_KEY,
                                (BASE_MODEL_NAME, *(name for name, _ in cfg.adapters)))
        finally:
            self.trace.append("exit")


@pytest.fixture
def stub(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")           # 本机若配了代理,别让桩的请求绕出去
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    s = ChatStub()
    yield s
    s.close()


@pytest.fixture
def fake_serve(stub, monkeypatch):
    import factory.interact_policy as ip

    serve = FakeServe(stub.base_url)
    monkeypatch.setattr(ip, "real_serve", serve)
    return serve


def _interact_defaults(**opponent):
    """training_defaults.yaml:点名路的 interact 段(sft_steps 为 0,点名路不做冷启动),可带 opponent 段。"""
    payload = default_training_defaults_payload()
    payload["training_config"]["sft_steps"] = 0
    payload["interact"] = {
        "trained_slot": "a",
        "slots": [{"slot_id": "a", "model_backend": "self_play"},
                  {"slot_id": "b", "model_backend": "frozen_reference", "model_ref": "zengen-v1",
                   "driver": "scripted"}],
        "max_turns_per_episode": 4,
        "scenario_ids": ["t1", "t2"],
        "persona_ids": {"a": "pa", "b": "pb"},
    }
    if opponent:
        payload["opponent"] = opponent
    return payload


def _seed_world(store):
    for sid in ("t1", "t2"):
        write_scenario(store, Scenario(sid, "bg", [RoleSlot("a", "a"), RoleSlot("b", "b")], "c", ["n"], "training"))
    for pid in ("pa", "pb"):
        write_persona(store, Persona(pid, ["t"], "m", "s", ["v"]))


def _seed_model(store, n: int, uri: str):
    ref = BigRef(uri, "c" * 64, 10, fetch_hint={"type": "roll_ssh", "format": EXPORT_FORMAT})
    store.write_product(model_file(n), Envelope(
        id=f"zengen-v{n}", type="zengen_model", version=1, created_at="t", created_by=OwnerRef(),
        status="effective", schema_version=1, payload={"checkpoint_ref": ref.to_dict()}), f"seed zengen-v{n}")


@pytest.fixture
def world(tmp_path):
    from factory_fixtures import closed_loop_repo

    store = closed_loop_repo(str(tmp_path / "cp"), run_id=None)
    write_endpoints(store, "gpu-1", "gpu-2")
    write_config(store, training_defaults_file(), "training-defaults", _interact_defaults(), "test: defaults")
    _seed_world(store)
    return store


def _opponent_binding(model_ref="zengen-v1", driver="scripted", backend="frozen_reference"):
    return RoleBinding("b", "pb", backend, model_ref=model_ref, driver=driver)


def _cfg_dict(*, bindings=None, **overrides) -> dict:
    cfg = dict(
        mode="interact", trained_slot="a", base_checkpoint_ref=BASE.to_dict(), teacher_model_ref=TEACHER.to_dict(),
        reward=REWARD.to_dict(), sft_steps=0, grpo_steps=1, sft_batch_size=1, groups_per_step=1, group_size=2,
        grpo_config={}, opd_coefficient=0.1, checkpoint_every_steps=1, allow_resume=True,
        scenario_ids=["t1", "t2"], max_turns_per_episode=4,
        bindings=[b.to_dict() for b in (bindings or [RoleBinding("a", "pa", "self_play"), _opponent_binding()])],
    )
    cfg.update(overrides)
    return cfg


def _write_directive(store, **cfg_overrides):
    store.write_product(f"{run_dir(RUN)}/directive.yaml", Envelope(
        id=RUN, type="directive", version=1, created_at="2026-10-26T00:00:00Z", created_by=OwnerRef(),
        status="pending", schema_version=1,
        payload={"target_dimension": "tom", "recipe_ref": "", "training_config": _cfg_dict(**cfg_overrides)}),
        "human: write directive")


def _read(store, name):
    return store.read_product(f"{run_dir(RUN)}/{name}.yaml")


def _episode_ids(store):
    return [p for p in store.list_products(episodes_dir(RUN))]


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _env(tmp_path, **extra):
    return hosts_env(tmp_path, "gpu-1: gpu-181\ngpu-2: gpu-182\n") | extra


def _run(store, tmp_path, options=None, **extra_env):
    asm = assemble(store, options or FactoryOptions(interact=True, endpoint="gpu-2"), env=_env(tmp_path, **extra_env))
    try:
        reconcile_once(store, dispatcher=asm.dispatcher)
    finally:
        asm.close()
    return asm


# ---- 端到端:装配出的派发器真的经策略工厂跑完一条 interact 指令 ----

def test_an_interact_directive_runs_through_the_assembled_dispatcher(world, tmp_path, stub, fake_serve):
    _write_directive(world)
    _run(world, tmp_path)
    assert _read(world, "directive").status == "executed"
    assert _read(world, "training_run").status == "executed"
    assert fake_serve.trace == ["enter", "exit"]
    assert stub.models() == {BASE_MODEL_NAME}                       # 训练位没有适配器,用基座说话
    assert fake_serve.calls == [("gpu-2", ())]
    assert _wait_until(lambda: len(_episode_ids(world)) >= 2)       # 落盘钩子把 episode 写进了这个 run


def test_the_recorded_endpoint_is_the_endpoint_the_service_was_started_on(world, tmp_path, fake_serve):
    """端点只选一次:点名的 gpu-2 既是 policy_context 起服务的端点,也是写进记录的端点。"""
    _write_directive(world)
    _run(world, tmp_path)
    started_on = fake_serve.calls[0][0]
    assert started_on == "gpu-2"
    assert _read(world, "training_run").payload["policy_backend"]["trained"]["endpoint"] == started_on


def test_several_endpoints_without_a_name_is_refused_at_assembly(world, tmp_path):
    with pytest.raises(FactoryConfigError, match="gpu-1.*gpu-2|several|ambiguous|--endpoint"):
        assemble(world, FactoryOptions(interact=True), env=_env(tmp_path))


def test_a_trained_adapter_export_is_mounted_and_spoken_with(world, tmp_path, stub, fake_serve):
    ref = BigRef("ssh://gpu-2/srv/rise/work/exports/t1", "d" * 64, 10,
                 fetch_hint={"type": "roll_ssh", "format": EXPORT_FORMAT})
    _write_directive(world, base_checkpoint_ref=ref.to_dict())
    _run(world, tmp_path)
    assert fake_serve.calls == [("gpu-2", (("trained", "/srv/rise/work/exports/t1"),))]
    assert stub.models() == {"trained"}
    assert _read(world, "training_run").payload["policy_backend"]["trained"]["kind"] == "adapter"


def test_a_trained_export_on_another_endpoint_fails_the_directive(world, tmp_path, fake_serve):
    ref = BigRef("ssh://gpu-1/srv/rise/work/exports/t1", "d" * 64, 10,
                 fetch_hint={"type": "roll_ssh", "format": EXPORT_FORMAT})
    _write_directive(world, base_checkpoint_ref=ref.to_dict())
    _run(world, tmp_path)                                             # 策略连的是 gpu-2
    assert _read(world, "directive").status == "failed"
    assert "another endpoint" in _read(world, "directive").payload["failure"]["reason"]
    assert fake_serve.calls == []                                     # 在占租约之前就判了


def test_a_vllm_opponent_shares_the_service_through_its_own_adapter(world, tmp_path, stub, fake_serve):
    _seed_model(world, 1, "ssh://gpu-2/srv/rise/work/exports/o1")
    _write_directive(world, bindings=[RoleBinding("a", "pa", "self_play"), _opponent_binding(driver=None)])
    _run(world, tmp_path)
    assert fake_serve.calls == [("gpu-2", (("zengen-v1", "/srv/rise/work/exports/o1"),))]
    assert stub.models() == {BASE_MODEL_NAME, "zengen-v1"}            # 训练位用基座,对手用自己的适配器
    assert _read(world, "directive").status == "executed"


def test_an_unresolvable_opponent_model_ref_fails_the_directive_before_serving(world, tmp_path, fake_serve):
    _write_directive(world, bindings=[RoleBinding("a", "pa", "self_play"),
                                      _opponent_binding(model_ref="zengen-v9", driver=None)])
    _run(world, tmp_path)
    directive = _read(world, "directive")
    assert directive.status == "failed" and "zengen-v9" in directive.payload["failure"]["reason"]
    assert fake_serve.calls == []


def test_an_external_api_opponent_uses_the_configured_endpoint_and_the_key_from_the_environment(
        world, tmp_path, stub, fake_serve):
    write_config(world, training_defaults_file(), "training-defaults",
                 _interact_defaults(base_url=stub.base_url, model="ext-model", api_key_env=OPP_KEY_ENV),
                 "test: defaults with opponent")
    _write_directive(world, bindings=[RoleBinding("a", "pa", "self_play"),
                                      _opponent_binding(model_ref="gpt-x", driver="external_api",
                                                        backend="external_api")])
    _run(world, tmp_path, **{OPP_KEY_ENV: OPP_KEY})
    assert _read(world, "directive").status == "executed"
    assert {"ext-model", BASE_MODEL_NAME} <= stub.models()
    assert any(r["model"] == "ext-model" and r["auth"] == f"Bearer {OPP_KEY}" for r in stub.requests)
    assert fake_serve.calls == [("gpu-2", ())]                       # 外部对手不占服务里的适配器位


# ---- 错误分类:延后类原样上抛,其余译成 failed ----

@pytest.mark.parametrize("busy", [
    ComputeBusyError("the GPU lease is held by someone else"),
    ServeStartupError("the vLLM service did not become ready"),
])
def test_a_deferred_error_leaves_the_directive_pending(world, tmp_path, fake_serve, busy):
    fake_serve.enter_raises = busy
    _write_directive(world)
    _run(world, tmp_path)
    directive = _read(world, "directive")
    assert directive.status == "pending" and "failure" not in directive.payload
    assert not world.exists(f"{run_dir(RUN)}/training_run.yaml")


@pytest.mark.parametrize("error", [ComputeError("adapters must be a tuple"), HelperError("bad_request", "no such path")])
def test_a_deterministic_compute_error_fails_the_directive(world, tmp_path, fake_serve, error):
    fake_serve.enter_raises = error
    _write_directive(world)
    _run(world, tmp_path)
    directive = _read(world, "directive")
    assert directive.status == "failed"
    assert type(error).__name__ in directive.payload["failure"]["reason"]
    assert not world.exists(f"{run_dir(RUN)}/training_run.yaml")


# ---- 策略上下文本身:登记、deadline、清理、撞车 ----

def _unit(store, tmp_path, stub, *, serve=None, opponent=None, env=None, runtime=None):
    """直接造策略工厂(不经装配),用真的 AgentRuntime 与注入的 serve。"""
    runtime = runtime or AgentRuntime(store, ("hi",))
    switch = OpponentSwitch(("hi",))
    endpoint = ComputeEndpoint(**{**ENDPOINT, "launch_prefix": (), "endpoint_id": "gpu-1"})
    serve = serve or FakeServe(stub.base_url)
    factory = make_policy_factory(
        store=store, runtime=runtime, switch=switch, client=SimpleNamespace(alias="gpu-181", endpoint=endpoint),
        endpoint=endpoint, opponent=opponent, env=env or {}, serve=serve)
    return runtime, switch, factory, serve


def _cfg(**overrides) -> TrainingConfig:
    return TrainingConfig.from_dict(_cfg_dict(**overrides))


def test_the_sessions_are_registered_before_the_body_and_dropped_after(world, tmp_path, stub):
    runtime, _, factory, _ = _unit(world, tmp_path, stub)
    cfg = _cfg()
    before = runtime.interact_deadline_s
    with factory(RUN, cfg):
        assert runtime._sessions == {"t1": RUN, "t2": RUN}
        assert runtime.interact_deadline_s == cfg.max_turns_per_episode * 30.0
    assert runtime._sessions == {} and runtime.interact_deadline_s == before


def test_a_training_exception_still_unregisters_and_restores_the_deadline(world, tmp_path, stub):
    runtime, _, factory, serve = _unit(world, tmp_path, stub)
    before = runtime.interact_deadline_s
    with pytest.raises(RuntimeError, match="boom"):
        with factory(RUN, _cfg()):
            raise RuntimeError("boom")
    assert runtime._sessions == {} and runtime.interact_deadline_s == before
    assert serve.trace == ["enter", "exit"]


@pytest.mark.parametrize("error", [ComputeBusyError("busy"), ComputeError("bad config")])
def test_a_failed_context_entry_registers_nothing(world, tmp_path, stub, error):
    runtime, _, factory, _ = _unit(world, tmp_path, stub, serve=FakeServe(stub.base_url, enter_raises=error))
    before = runtime.interact_deadline_s
    with pytest.raises((type(error), DirectiveFailedError)):
        with factory(RUN, _cfg()):
            pytest.fail("the body must not run")
    assert runtime._sessions == {} and runtime.interact_deadline_s == before


def test_a_later_run_does_not_inherit_an_earlier_runs_registration(world, tmp_path, stub):
    runtime, _, factory, _ = _unit(world, tmp_path, stub)
    with pytest.raises(RuntimeError):
        with factory("run-old", _cfg()):
            raise RuntimeError("first run died")
    with factory("run-new", _cfg(scenario_ids=["t2"])):
        assert runtime._sessions == {"t2": "run-new"}
    assert runtime._sessions == {}


def test_a_scenario_that_is_also_a_deployment_scenario_fails_the_directive_and_registers_nothing(
        world, tmp_path, stub):
    """Ruling 24:钩子只按情境 id 判,撞车就会把部署 episode 写进训练 run,MindAgent 驱动器又写一遍。"""
    runtime, _, factory, serve = _unit(world, tmp_path, stub)
    with pytest.raises(DirectiveFailedError) as caught:
        with factory(RUN, _cfg(scenario_ids=["factory-deploy-s1", "t2", "factory-deploy-s3"])):
            pytest.fail("the body must not run")
    reason = str(caught.value)
    assert "factory-deploy-s1" in reason and "factory-deploy-s3" in reason and "t2" not in reason
    assert runtime._sessions == {} and serve.calls == []             # 什么都没登记,服务也没起


def test_without_an_agent_yaml_nothing_can_overlap(tmp_path, stub):
    bare = GitStore(str(tmp_path / "bare"))
    bare.init()
    runtime, _, factory, _ = _unit(bare, tmp_path, stub)
    with factory(RUN, _cfg(scenario_ids=["factory-deploy-s1", "t2"])):
        assert set(runtime._sessions) == {"factory-deploy-s1", "t2"}
    assert runtime._sessions == {}


def test_the_opponent_switch_is_bound_only_inside_the_context(world, tmp_path, stub):
    _seed_model(world, 1, "ssh://gpu-1/srv/rise/work/exports/o1")
    _, switch, factory, _ = _unit(world, tmp_path, stub)
    binding = _opponent_binding(driver=None)
    scenario = Scenario("t1", "bg", [RoleSlot("a", "a"), RoleSlot("b", "b")], "c", ["n"], "training")
    with factory(RUN, _cfg(bindings=[RoleBinding("a", "pa", "self_play"), binding])):
        from factory.vllm_policy import VllmPolicy

        assert isinstance(switch(binding, scenario), VllmPolicy)
    from simulator.backends import ScriptedBackend

    after = switch(binding, scenario)
    assert isinstance(after, ScriptedBackend) and after.respond(None, scenario, [], "b")[0] == "hi"


def test_a_missing_opponent_key_names_only_the_variable(world, tmp_path, stub):
    opponent = {"base_url": stub.base_url, "model": "ext-model", "api_key_env": OPP_KEY_ENV}
    _, _, factory, serve = _unit(world, tmp_path, stub, opponent=opponent, env={})
    bindings = [RoleBinding("a", "pa", "self_play"),
                _opponent_binding(model_ref="gpt-x", driver="external_api", backend="external_api")]
    with pytest.raises(DirectiveFailedError) as caught:
        with factory(RUN, _cfg(bindings=bindings)):
            pytest.fail("the body must not run")
    assert OPP_KEY_ENV in str(caught.value) and serve.calls == []    # 凭证缺失在占租约之前就判了


def test_an_external_opponent_without_an_opponent_section_fails_the_directive(world, tmp_path, stub):
    _, _, factory, _ = _unit(world, tmp_path, stub, opponent=None)
    bindings = [RoleBinding("a", "pa", "self_play"),
                _opponent_binding(model_ref="gpt-x", driver="external_api", backend="external_api")]
    with pytest.raises(DirectiveFailedError, match="opponent"):
        with factory(RUN, _cfg(bindings=bindings)):
            pytest.fail("the body must not run")
