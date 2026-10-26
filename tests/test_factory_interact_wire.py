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
    write_config(store, training_defaults_file(), "training-defaults", _interact_defaults(), "test: defaults")
    _seed_world(store)
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
from compute.errors import ComputeBusyError, ComputeError, HelperError, ServeStartupError, ServeStopError
from compute.service import ServiceHandle
from compute.vllm_service import BASE_MODEL_NAME
from controlplane.config import write_config
from controlplane.daemon import DirectiveFailedError, reconcile_once
from controlplane.envelope import BigRef, Envelope, OwnerRef
from controlplane.paths import (
    candidate_checkpoint_file, episodes_dir, model_file, run_dir, scheduler_config_file, training_defaults_file,
)
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

    def __init__(self, base_url: str, *, enter_raises: Exception | None = None,
                 exit_raises: Exception | None = None) -> None:
        self.base_url = base_url
        self.enter_raises = enter_raises
        self.exit_raises = exit_raises                   # 退出(停服务)时抛,像 compute.service.serve 的 finally
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
            if self.exit_raises is not None:
                raise self.exit_raises


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


def _seed_world(store, *, scenarios=("t1", "t2"), personas=("pa", "pb"), purpose="training"):
    for sid in scenarios:
        write_scenario(store, Scenario(sid, "bg", [RoleSlot("a", "a"), RoleSlot("b", "b")], "c", ["n"], purpose))
    for pid in personas:
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


def test_without_the_interact_switch_an_interact_directive_fails_and_names_the_switch(world, tmp_path, fake_serve):
    """不带 --interact 时装配出的是无参派发器(与冻结入口同一个),interact 指令判 failed(T2),
    原因点名开关;没起过服务。"""
    _write_directive(world)
    _run(world, tmp_path, FactoryOptions())
    directive = _read(world, "directive")
    assert directive.status == "failed" and directive.payload["failure"]["category"] == "T2"
    reason = directive.payload["failure"]["reason"]
    assert "--interact" in reason and "factory run" in reason and "factory reconcile" in reason
    assert fake_serve.calls == []


STUCK = "job serve-job-a1 is still running after stop; the GPU lease was kept"


def test_a_service_that_will_not_stop_after_a_finished_training_fails_the_directive_as_t2(
        world, tmp_path, fake_serve):
    """训练已完成、候选已落树,收尾时服务停不下来:重试只会撞上同一个租约,所以判 T2 要人去停作业,
    而不是延后重试(重试会在已完成的训练上反复起服务)。"""
    fake_serve.exit_raises = ServeStopError(STUCK)
    _write_directive(world)
    _run(world, tmp_path)
    directive = _read(world, "directive")
    assert directive.status == "failed" and directive.payload["failure"]["category"] == "T2"
    reason = directive.payload["failure"]["reason"]
    assert "serve-job-a1" in reason and "stop" in reason and "manually" in reason
    assert _read(world, "training_run").status == "executed"          # 训练本身是成功的
    assert world.read_product(candidate_checkpoint_file(RUN)).status == "pending_review"   # 候选已落树
    assert "already failed" not in reason                             # 训练没失败,不该补那半句


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


def test_a_stop_error_after_a_training_exception_still_unregisters_restores_and_unbinds(world, tmp_path, stub):
    """训练体抛错、退出时服务又停不下来:登记注销、deadline 还原、对手开关解绑都不能被跳过,
    指令判 failed(T2),而不是被训练体的异常带走。"""
    serve = FakeServe(stub.base_url, exit_raises=ServeStopError(STUCK))
    runtime, switch, factory, _ = _unit(world, tmp_path, stub, serve=serve)
    before = runtime.interact_deadline_s
    binding = _opponent_binding()
    scenario = Scenario("t1", "bg", [RoleSlot("a", "a"), RoleSlot("b", "b")], "c", ["n"], "training")
    with pytest.raises(DirectiveFailedError) as caught:
        with factory(RUN, _cfg()):
            assert runtime._sessions == {"t1": RUN, "t2": RUN}
            raise RuntimeError("boom")
    assert "serve-job-a1" in str(caught.value) and caught.value.category == "T2"
    # 被顶掉的训练异常只留类名,不带它的文字
    assert "the training had already failed: RuntimeError" in str(caught.value)
    assert "boom" not in str(caught.value)
    assert runtime._sessions == {} and runtime.interact_deadline_s == before
    assert serve.trace == ["enter", "exit"]
    from simulator.backends import ScriptedBackend

    assert isinstance(switch(binding, scenario), ScriptedBackend)      # 开关已解绑


def test_a_stop_error_while_the_service_is_still_starting_up_is_also_t2_and_registers_nothing(
        world, tmp_path, stub):
    """起服务失败后收尾停不下来:compute.service 的 finally 里抛出的 ServeStopError 会顶掉原来的启动错误。"""
    serve = FakeServe(stub.base_url, enter_raises=ServeStopError(STUCK))
    runtime, _, factory, _ = _unit(world, tmp_path, stub, serve=serve)
    with pytest.raises(DirectiveFailedError) as caught:
        with factory(RUN, _cfg()):
            pytest.fail("the body must not run")
    assert caught.value.category == "T2" and "serve-job-a1" in str(caught.value)
    assert runtime._sessions == {}


def test_a_stop_error_on_a_clean_exit_is_translated_to_a_t2_failure(world, tmp_path, stub):
    serve = FakeServe(stub.base_url, exit_raises=ServeStopError(STUCK))
    runtime, _, factory, _ = _unit(world, tmp_path, stub, serve=serve)
    before = runtime.interact_deadline_s
    with pytest.raises(DirectiveFailedError) as caught:
        with factory(RUN, _cfg()):
            pass
    assert caught.value.category == "T2" and "serve-job-a1" in str(caught.value)
    assert runtime._sessions == {} and runtime.interact_deadline_s == before


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


# ---- 真推理的 rollout 失败不逃出两条通道(Ruling 27) ----

import socket

from controlplane.daemon import DeferralBook
from controlplane.deferred import DeferredError
from factory.opponents import OpponentApiError
from mindtrainer.trajectory_source import RolloutFailedError

DISPATCH_KEY = ("dispatch_directive", RUN)


def _closed_port_url() -> str:
    """一个刚释放、没人监听的本机端口:连上去立刻被拒,不等超时。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{s.getsockname()[1]}"


def _ticks(store, tmp_path, *, ticks=1, until=None, **env) -> DeferralBook:
    """装配一次,调和 ticks 轮(退避设为 0,每轮都到期)。reconcile_once 只接 DeferredError,
    别的异常会从这里直接逃出来,所以调用本函数本身就是"守护进程没被杀死"的断言。"""
    book = DeferralBook(default_backoff_s=0.0)
    asm = assemble(store, FactoryOptions(interact=True, endpoint="gpu-2"), env=_env(tmp_path, **env))
    try:
        for _ in range(ticks):
            reconcile_once(store, dispatcher=asm.dispatcher, deferrals=book)
            if until is not None and until():
                break
    finally:
        asm.close()
    return book


def _external_opponent_binding():
    return [RoleBinding("a", "pa", "self_play"),
            _opponent_binding(model_ref="gpt-x", driver="external_api", backend="external_api")]


def test_an_opponent_that_cannot_be_reached_defers_the_directive_instead_of_killing_the_daemon(
        world, tmp_path, stub, fake_serve):
    """对手的 HTTP 失败 -> 服务端判 role_backend_failure -> 轨迹源抛 RolloutFailedError。过去它一路逃出调和循环。"""
    write_config(world, training_defaults_file(), "training-defaults",
                 _interact_defaults(base_url=_closed_port_url(), model="ext-model", api_key_env=OPP_KEY_ENV),
                 "test: unreachable opponent")
    _write_directive(world, bindings=_external_opponent_binding())
    book = _ticks(world, tmp_path, **{OPP_KEY_ENV: OPP_KEY})
    assert _read(world, "directive").status == "pending"
    assert _read(world, "training_run").status == "pending"
    assert book.streak(DISPATCH_KEY) == 1 and book.next_try(DISPATCH_KEY) is not None
    assert fake_serve.trace == ["enter", "exit"]                      # 服务照常停了、租约照常放了


class ScriptedChatStub:
    """外部 API 对手的本机桩:按到达顺序取 script 里的下一项((状态码, 延迟秒数)),用完后回 200。
    正常回复循环用 CYCLE,所以训练位与对手位的发言口径与 ChatStub 一致。"""

    def __init__(self, script) -> None:
        self.script = list(script)
        self.count = 0
        self._lock = threading.Lock()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                with stub._lock:
                    stub.count += 1
                    status, delay = stub.script.pop(0) if stub.script else (200, 0.0)
                time.sleep(delay)
                raw = json.dumps({"choices": [{"message": {"role": "assistant", "content": "好的"}}]}).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except OSError:
                    pass          # 客户端已超时离开

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def external_api(monkeypatch):
    """造一个外部 API 桩,并把对手的单次 HTTP 上限从 25 秒收到 0.3 秒,让超时用例几百毫秒就走完。"""
    import factory.interact_policy as ip

    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    monkeypatch.setattr(ip, "OPPONENT_TIMEOUT_S", 0.3)
    made: list[ScriptedChatStub] = []

    def _make(script):
        server = ScriptedChatStub(script)
        made.append(server)
        return server

    yield _make
    for server in made:
        server.close()


def _use_external_opponent(world, server) -> None:
    write_config(world, training_defaults_file(), "training-defaults",
                 _interact_defaults(base_url=server.base_url, model="ext-model", api_key_env=OPP_KEY_ENV),
                 "test: scripted external opponent")
    _write_directive(world, bindings=_external_opponent_binding())


def test_an_external_opponent_that_times_out_once_is_retried_and_the_directive_executes(
        world, tmp_path, stub, fake_serve, external_api):
    """对手位外部 API 第一次调用超时 -> 服务端 role_backend_timeout(T1) -> 轨迹源整条重跑 -> 第二次正常。
    指令照常 executed,episode 数是预期的那几条(超时的半截 episode 不进树)。"""
    server = external_api([(200, 5.0)])                       # 第一次回应拖得比 0.3 秒上限久,之后都正常
    _use_external_opponent(world, server)
    book = _ticks(world, tmp_path, **{OPP_KEY_ENV: OPP_KEY})
    assert _read(world, "directive").status == "executed"
    assert _read(world, "training_run").status == "executed"
    assert book.streak(DISPATCH_KEY) == 0
    expected = 1 * 1 * 2                                       # grpo_steps * groups_per_step * group_size
    assert _wait_until(lambda: len(_episode_ids(world)) >= expected)
    assert len(_episode_ids(world)) == expected
    # 判别点是上面的 executed:按旧的归类(超时算 role_backend_failure)这条指令会停在 pending。
    # 这里只确认那次超时的调用确实发生过,不据此推断重跑了几次。
    assert server.count > expected


def test_an_external_opponent_answering_500_defers_the_directive_with_role_backend_failure(
        world, tmp_path, stub, fake_serve, external_api, caplog):
    """非 200 不重跑单条(协议上分不出 T1 与 T2):RolloutFailedError -> RolloutDeferredError,
    指令保持 pending 退避,延后原因点名 role_backend_failure。续训次数耗尽后升 T3(见上面那条)。"""
    import logging

    server = external_api([(500, 0.0)] * 50)
    _use_external_opponent(world, server)
    with caplog.at_level(logging.WARNING, logger="controlplane.daemon"):
        book = _ticks(world, tmp_path, **{OPP_KEY_ENV: OPP_KEY})
    assert _read(world, "directive").status == "pending"
    assert _read(world, "training_run").status == "pending"
    assert book.streak(DISPATCH_KEY) == 1
    assert "RolloutDeferredError" in caplog.text and "role_backend_failure" in caplog.text
    assert OPP_KEY not in caplog.text and server.base_url not in caplog.text


def test_an_external_opponent_answering_404_fails_the_directive_with_role_backend_rejected(
        world, tmp_path, stub, fake_serve, external_api):
    """对手 API 明确拒绝(4xx,429 除外)是确定性的:服务端 role_backend_rejected -> 轨迹源不重跑 -> 编排器把
    training_run 写 failed -> 策略上下文原样放行 -> 派发器判指令 failed(T2),不进延后通道。"""
    server = external_api([(404, 0.0)] * 50)
    _use_external_opponent(world, server)
    book = _ticks(world, tmp_path, **{OPP_KEY_ENV: OPP_KEY})
    directive = _read(world, "directive")
    assert directive.status == "failed" and directive.payload["failure"]["category"] == "T2"
    assert "role_backend_rejected" in directive.payload["failure"]["reason"]
    assert OPP_KEY not in directive.payload["failure"]["reason"]
    assert server.base_url not in directive.payload["failure"]["reason"]
    run = _read(world, "training_run")
    assert run.status == "failed" and run.payload["failure"]["category"] == "T2"
    assert "role_backend_rejected" in run.payload["failure"]["detail"]
    assert book.streak(DISPATCH_KEY) == 0                      # 没进延后通道
    assert fake_serve.trace == ["enter", "exit"]               # 服务照常停了、租约照常放了
    assert server.count == 1                                   # 不重跑:只打了一次对手


def test_a_policy_that_cannot_reach_its_service_defers_the_directive(world, tmp_path, stub, monkeypatch):
    """训练位自己的 respond 抛 OpponentApiError(轨迹源不包装策略异常),同样不该逃出去。"""
    import factory.interact_policy as ip

    monkeypatch.setattr(ip, "real_serve", FakeServe(_closed_port_url()))
    _write_directive(world)
    book = _ticks(world, tmp_path)
    assert _read(world, "directive").status == "pending"
    assert _read(world, "training_run").status == "pending"
    assert book.streak(DISPATCH_KEY) == 1


def test_an_invalid_session_still_fails_the_directive_this_tick(world, tmp_path, stub, fake_serve):
    """invalid_session 是请求本身有问题:订单写 failed,派发器译成 T2,不进延后通道。"""
    write_scenario(world, Scenario("t1", "bg", [RoleSlot("a", "a")], "c", ["n"], "training"))   # 缺 b 位
    _write_directive(world)
    book = _ticks(world, tmp_path)
    directive = _read(world, "directive")
    assert directive.status == "failed" and directive.payload["failure"]["category"] == "T2"
    assert "invalid_session" in directive.payload["failure"]["reason"]
    assert _read(world, "training_run").status == "failed"
    assert book.streak(DISPATCH_KEY) == 0


def test_a_run_that_keeps_failing_ends_in_t3_through_the_resume_budget(world, tmp_path, stub, fake_serve):
    """延后通道不会让它无限转:每次重派 resume_attempts 加一,到 max_resume_attempts 由 decide_resume 升 T3。"""
    write_config(world, training_defaults_file(), "training-defaults",
                 _interact_defaults(base_url=_closed_port_url(), model="ext-model", api_key_env=OPP_KEY_ENV),
                 "test: unreachable opponent")
    _write_directive(world, bindings=_external_opponent_binding())
    _ticks(world, tmp_path, ticks=10, until=lambda: _read(world, "directive").status == "failed",
           **{OPP_KEY_ENV: OPP_KEY})
    directive = _read(world, "directive")
    assert directive.status == "failed" and directive.payload["failure"]["category"] == "T3"
    assert "resume attempts exhausted" in directive.payload["failure"]["reason"]


def _failed_rollout(reason: str, detail: str = "") -> RolloutFailedError:
    return RolloutFailedError(scenario_id="t1", rollout=0, reason=reason, detail=detail, attempts=1, failed_attempts=[])


@pytest.mark.parametrize("raised, expected", [
    (_failed_rollout("role_backend_failure", "http://10.9.8.7:8000 refused"), "RolloutFailedError: role_backend_failure"),
    (_failed_rollout("timeout"), "RolloutFailedError: timeout"),
    (_failed_rollout("persistence_failed"), "RolloutFailedError: persistence_failed"),
    (OpponentApiError("HTTP 500 from 10.9.8.7:8000"), "OpponentApiError"),
])
def test_the_deferral_reason_names_the_class_and_reason_but_not_the_message(world, tmp_path, stub, raised, expected):
    from factory.interact_policy import RolloutDeferredError

    _, _, factory, serve = _unit(world, tmp_path, stub)
    with pytest.raises(RolloutDeferredError) as caught:
        with factory(RUN, _cfg()):
            raise raised
    assert isinstance(caught.value, DeferredError)
    assert expected in caught.value.reason and "10.9.8.7" not in caught.value.reason
    assert caught.value.__cause__ is None and caught.value.__suppress_context__      # 原异常的文字不跟着回溯走
    assert serve.trace == ["enter", "exit"]


@pytest.mark.parametrize("reason", ["invalid_session", "role_backend_rejected"])
def test_a_deterministic_rollout_failure_passes_through_the_policy_scope_unchanged(world, tmp_path, stub, reason):
    _, _, factory, _ = _unit(world, tmp_path, stub)
    original = _failed_rollout(reason, "bad persona")
    with pytest.raises(RolloutFailedError) as caught:
        with factory(RUN, _cfg()):
            raise original
    assert caught.value is original


# ---------------- Part 4:装配期拒绝项(设计 90 §6.1 的十一条) ----------------
# 每条:退出码 2、一行 `factory: error: ...` 点名缺什么、树里不写东西(没有新提交)。

import subprocess

from factory_fixtures import tree_files
from trainscheduler.config import DEFAULT_SCHEDULER_CONFIG


def _git(store, *args) -> str:
    return subprocess.run(["git", "-C", str(store.repo_root), *args], capture_output=True, text=True,
                          check=True).stdout


def _configured(tmp_path, mutate=None, *, seed=None, endpoints=True):
    """装好默认资产的仓库,training_defaults.yaml 写成合法的点名路 interact 配置,再由 mutate 改坏一处。"""
    from factory_fixtures import closed_loop_repo

    store = closed_loop_repo(str(tmp_path / "cp"), run_id=None)
    if endpoints:
        write_endpoints(store, "gpu-1")
    payload = _interact_defaults()
    if mutate is not None:
        mutate(payload)
    write_config(store, training_defaults_file(), "training-defaults", payload, "test: defaults")
    _seed_world(store, **(seed or {}))
    return store


def _refused(store, tmp_path, capsys, fragment, *argv, env=None):
    """跑 `factory reconcile --once --interact`,断言被拒:退出码 2、标准错误恰好一行、含 fragment、树没动。"""
    from factory.cli import main

    head, status, files = _git(store, "rev-parse", "HEAD"), _git(store, "status", "--porcelain",
                                                                  "--untracked-files=all"), tree_files(store)
    code = main(["reconcile", str(store.repo_root), "--once", *(argv or ("--interact",))],
                env=hosts_env(tmp_path) if env is None else env)
    err = capsys.readouterr().err
    assert code == 2, err
    assert err.startswith("factory: error: ") and len(err.strip().splitlines()) == 1, err
    assert fragment in err, err
    assert (_git(store, "rev-parse", "HEAD"), _git(store, "status", "--porcelain", "--untracked-files=all"),
            tree_files(store)) == (head, status, files)
    return err


def test_a_valid_interact_configuration_is_accepted(tmp_path):
    store = _configured(tmp_path)
    asm = assemble(store, FactoryOptions(interact=True), env=hosts_env(tmp_path))
    asm.close()


def test_item_2_a_missing_interact_section_is_refused(tmp_path, capsys):
    store = _configured(tmp_path, lambda p: p.pop("interact"))
    _refused(store, tmp_path, capsys, "interact section")


def test_item_2_a_missing_training_defaults_file_is_refused_by_name(tmp_path, capsys):
    from factory_fixtures import closed_loop_repo

    store = closed_loop_repo(str(tmp_path / "bare"), run_id=None)
    (store.repo_root / training_defaults_file()).unlink()
    subprocess.run(["git", "-C", str(store.repo_root), "commit", "-qam", "test: drop defaults"], check=True)
    write_endpoints(store, "gpu-1")
    _refused(store, tmp_path, capsys, "training_defaults.yaml")


def test_item_3_and_9_an_external_api_opponent_needs_its_key_variable(tmp_path, capsys):
    def external(p):
        p["interact"]["slots"][1] = {"slot_id": "b", "model_backend": "external_api", "model_ref": "gpt-x"}
        p["opponent"] = {"base_url": "https://api.example.com", "model": "m", "api_key_env": OPP_KEY_ENV}

    store = _configured(tmp_path, external)
    err = _refused(store, tmp_path, capsys, OPP_KEY_ENV)                         # 环境里没有这个变量
    assert "sk-" not in err
    _refused(store, tmp_path, capsys, OPP_KEY_ENV, env=hosts_env(tmp_path) | {OPP_KEY_ENV: "   "})   # 空白也算没有


def test_item_3_and_9_the_key_value_never_reaches_the_error(tmp_path, capsys):
    def external(p):
        p["interact"]["slots"][1] = {"slot_id": "b", "model_backend": "external_api", "model_ref": "gpt-x"}
        p["opponent"] = {"base_url": "https://user:hunter2@api.example.com", "model": "m",
                         "api_key_env": OPP_KEY_ENV}

    store = _configured(tmp_path, external)
    err = _refused(store, tmp_path, capsys, "base_url",
                   env=hosts_env(tmp_path) | {OPP_KEY_ENV: OPP_KEY})
    # 信息里的 "(user:password@)" 是写死的提示,不是配置里的取值;配置里的 userinfo 与地址都不能出现
    assert "hunter2" not in err and "user:hunter2" not in err and OPP_KEY not in err
    assert "api.example.com" not in err


def test_item_3_and_9_an_external_slot_without_an_opponent_section_is_refused(tmp_path, capsys):
    def external(p):
        p["interact"]["slots"][1] = {"slot_id": "b", "model_backend": "external_api", "model_ref": "gpt-x"}

    store = _configured(tmp_path, external)
    _refused(store, tmp_path, capsys, "opponent section")


def test_item_3_and_9_the_key_variable_present_lets_assembly_through(tmp_path):
    def external(p):
        p["interact"]["slots"][1] = {"slot_id": "b", "model_backend": "external_api", "model_ref": "gpt-x"}
        p["opponent"] = {"base_url": "https://api.example.com", "model": "m", "api_key_env": OPP_KEY_ENV}

    store = _configured(tmp_path, external)
    assemble(store, FactoryOptions(interact=True), env=hosts_env(tmp_path) | {OPP_KEY_ENV: OPP_KEY}).close()


def test_a_userinfo_base_url_is_refused_even_when_no_slot_uses_it(tmp_path, capsys):
    def with_userinfo(p):
        p["opponent"] = {"base_url": "https://user:hunter2@api.example.com", "model": "m",
                         "api_key_env": OPP_KEY_ENV}

    store = _configured(tmp_path, with_userinfo)
    err = _refused(store, tmp_path, capsys, "base_url")
    assert "hunter2" not in err and "api.example.com" not in err


def test_item_4_scenarios_without_personas_are_refused(tmp_path, capsys):
    store = _configured(tmp_path, lambda p: p["interact"].pop("persona_ids"))
    _refused(store, tmp_path, capsys, "together")


def test_item_4_personas_without_scenarios_are_refused(tmp_path, capsys):
    store = _configured(tmp_path, lambda p: p["interact"].pop("scenario_ids"))
    _refused(store, tmp_path, capsys, "together")


def test_item_5_a_named_scenario_that_is_not_in_the_tree_is_refused(tmp_path, capsys):
    store = _configured(tmp_path, seed={"scenarios": ("t1",)})
    err = _refused(store, tmp_path, capsys, "t2")
    assert "scenario" in err


def test_item_5_a_named_persona_that_is_not_in_the_tree_is_refused(tmp_path, capsys):
    store = _configured(tmp_path, seed={"personas": ("pa",)})
    err = _refused(store, tmp_path, capsys, "pb")
    assert "persona" in err


def test_item_6_a_deployment_scenario_cannot_be_trained_on(tmp_path, capsys):
    store = _configured(tmp_path, seed={"purpose": "deployment_simulation"})
    err = _refused(store, tmp_path, capsys, "deployment_simulation")
    assert "t1" in err and "t2" in err


def test_item_6_the_scenarios_install_defaults_writes_are_deployment_scenarios(tmp_path, capsys):
    def named_defaults(p):
        p["interact"]["scenario_ids"] = ["factory-deploy-s1", "factory-deploy-s2"]
        p["interact"]["persona_ids"] = {"a": "factory-agent-persona", "b": "factory-user-persona"}

    store = _configured(tmp_path, named_defaults)
    _refused(store, tmp_path, capsys, "factory-deploy-s1")


def test_item_7_the_named_path_does_not_take_sft_steps(tmp_path, capsys):
    def with_sft(p):
        p["training_config"]["sft_steps"] = 2

    store = _configured(tmp_path, with_sft)
    err = _refused(store, tmp_path, capsys, "sft_steps")
    assert "data_spec" in err


def test_item_7_does_not_apply_to_the_generated_path(tmp_path):
    def generated(p):
        p["training_config"]["sft_steps"] = 2
        p["interact"].pop("scenario_ids")
        p["interact"].pop("persona_ids")
        p["interact"]["data_spec"] = {"scenario_count": 4, "seed": 0, "purpose": "training",
                                      "stages": {"sft": {"group_size": 1}, "grpo_opd": {"group_size": 2}}}

    store = _configured(tmp_path, generated)
    assemble(store, FactoryOptions(interact=True), env=hosts_env(tmp_path)).close()


def test_item_8_fewer_scenarios_than_groups_per_step_is_refused(tmp_path, capsys):
    def one_scenario(p):
        p["interact"]["scenario_ids"] = ["t1"]

    store = _configured(tmp_path, one_scenario)
    _refused(store, tmp_path, capsys, "groups_per_step")


def test_the_config_checks_come_before_the_endpoint_checks(tmp_path, capsys):
    """没有端点配置也没有主机映射,但 interact 段缺了:报的是缺段,而不是缺端点。"""
    store = _configured(tmp_path, lambda p: p.pop("interact"), endpoints=False)
    _refused(store, tmp_path, capsys, "interact section", env={})


def test_item_10_is_not_reachable_from_the_cli():
    """策略与策略工厂同给,派发器自己抛 ValueError;组合根从不给具体的 policy,所以装配期没有这一条可检查。"""
    from mindtrainer.dispatch import make_mindtrainer_dispatcher

    with pytest.raises(ValueError, match="either policy or policy_factory"):
        make_mindtrainer_dispatcher(policy=object(), policy_factory=lambda run_id, cfg: None)


# ---- 第 11 条:没传 --interact 时 ----

def _allow_interact(store, value=True):
    write_config(store, scheduler_config_file(), "scheduler-config",
                 {**DEFAULT_SCHEDULER_CONFIG.to_dict(), "allow_interact": value}, "test: scheduler")


def test_item_11_plan_with_allow_interact_but_no_interact_flag_is_refused(tmp_path, capsys):
    store = _configured(tmp_path)
    _allow_interact(store)
    err = _refused(store, tmp_path, capsys, "--interact", "--enable", "plan", "--planner-backend", "heuristic")
    assert "allow_interact" in err                                       # 说明二选一:加 --interact,或关掉 allow_interact


def test_item_11_does_not_fire_when_allow_interact_is_off(tmp_path):
    store = _configured(tmp_path)
    assemble(store, FactoryOptions(switches=("plan",), planner_backend="heuristic"), env={}).close()


def test_item_11_does_not_fire_without_the_plan_switch(tmp_path):
    store = _configured(tmp_path)
    _allow_interact(store)
    assemble(store, FactoryOptions(switches=("track",)), env={}).close()


def test_item_11_is_satisfied_by_passing_interact(tmp_path):
    store = _configured(tmp_path)
    _allow_interact(store)
    assemble(store, FactoryOptions(switches=("plan",), planner_backend="heuristic", interact=True),
             env=hosts_env(tmp_path)).close()
