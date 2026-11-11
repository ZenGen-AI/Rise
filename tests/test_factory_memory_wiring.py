"""组合根的记忆后端选择(设计二 5.1、6.2、6.4 节,第 10 节拒绝项;设计四 90 篇 4c 前一行、第 12 至 23 条)。

Starling 路径的真服务用例用拉起模式:`root` 在 tmp_path 下,rise 自己拉起 starling-serve(数据目录与 HOME 都在
`root/service` 下),抽取与嵌入走本机回环的假端点,只放占位密钥。"""
from __future__ import annotations

import json
import logging
import os
import sys

import pytest
import yaml

from controlplane.lock import hold_file_lock
from factory import wire
from factory.cli import main
from factory.errors import FactoryConfigError
from factory.memory_config import MEMORY_CONFIG_ENV
from factory.memory_runtime import (
    MemoryWiring,
    StandInTrainingWorlds,
    memory_wiring,
    stand_in_deployment_memory,
    stand_in_memory_resolver,
)
from factory.wire import FactoryOptions, assemble, deploy_turn_timeout_s
from factory_fixtures import closed_loop_repo

needs_service = pytest.mark.skipif(
    __import__("importlib.util").util.find_spec("starling_client") is None
    or __import__("importlib.util").util.find_spec("starling") is None,
    reason="starling-client 或 starling-memory[dashboard] 未安装")


@pytest.fixture
def store(tmp_path):
    return closed_loop_repo(str(tmp_path / "cp"), run_id=None)


@pytest.fixture(scope="module")
def fake():
    from starling_fakes import FakeOpenAI
    with FakeOpenAI(dim=256) as f:
        yield f


def _memory_env(tmp_path, root=None, *, base_url="http://127.0.0.1:9/v1", **extra):
    """记忆配置:缺省是拉起模式(不给 url 与 token_file)。`extra` 里的配置键写进文件,其余当环境变量。
    给了 `url` 就是外部模式,端点键不写(它们跟 Starling 走)。"""
    keys = {k: extra.pop(k) for k in list(extra) if k in ("embedding_model", "timeout_ms", "max_retries",
                                                          "url", "token_file", "world_prefix")}
    path = tmp_path / "memory.yaml"
    data = {"root": str(root or tmp_path / "mem")}
    if "url" not in keys:
        data.update({"extraction_base_url": base_url, "extraction_model": "fake-extract",
                     "embedding_base_url": base_url, "embedding_model": keys.pop("embedding_model", "fake-embed"),
                     "embedding_dim": 256})
    path.write_text(yaml.safe_dump({**data, **keys}), encoding="utf-8")
    os.chmod(path, 0o600)
    return {MEMORY_CONFIG_ENV: str(path), "OPENAI_API_KEY": "offline-test-key", "PATH": os.environ["PATH"], **extra}


def _service_env(tmp_path, fake, **extra):
    return _memory_env(tmp_path, base_url=fake.base_url, **extra)


def _fake_wiring(repo_root, on_locked):
    return MemoryWiring(backend="stand-in", deployment=stand_in_deployment_memory(repo_root),
                        memory_resolver=stand_in_memory_resolver, training_worlds=StandInTrainingWorlds(),
                        on_locked=on_locked)


def _pass_install_checks(monkeypatch, commit="c" * 40):
    """两个包的安装来源打桩成同一提交:装没装都能走到后面的检查。"""
    import factory.memory_config as mc
    monkeypatch.setattr(mc, "client_install_commit", lambda name, **kw: commit)


class _FakeDist:
    def __init__(self, *, direct_url=None, record=None):
        self._files = {"RECORD": record}
        if direct_url is not None:
            self._files["direct_url.json"] = json.dumps(direct_url)

    def read_text(self, name):
        return self._files.get(name)


def test_turn_timeouts_follow_section_6_4():
    assert deploy_turn_timeout_s(interact=False, starling=False) is None     # 替身:保持 5 秒
    assert deploy_turn_timeout_s(interact=False, starling=True) == 40.0
    assert deploy_turn_timeout_s(interact=True, starling=True) == 150.0      # 同开取较大的 150
    assert deploy_turn_timeout_s(interact=True, starling=False) == 150.0


def test_without_the_variable_the_wiring_is_the_stand_in_and_logs_it_once(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="factory.memory_runtime"):
        w = memory_wiring(tmp_path / "repo", {}, deployment=True)
    assert w.backend == "stand-in" and isinstance(w.training_worlds, StandInTrainingWorlds) and w.on_locked is None
    assert w.deployment is not None and w.memory_resolver is stand_in_memory_resolver
    assert caplog.text.count("memory backend: stand-in") == 1


def test_the_stand_in_builds_no_deployment_memory_unless_asked(tmp_path):
    assert memory_wiring(tmp_path / "repo", {}, deployment=False).deployment is None


def test_without_the_variable_no_client_is_imported(store, monkeypatch):
    monkeypatch.delitem(sys.modules, "starling_client", raising=False)
    monkeypatch.delitem(sys.modules, "simple_actio.memory_starling", raising=False)
    asm = assemble(store, FactoryOptions(switches=("agent",)), env={})
    asm.close()
    assert "starling_client" not in sys.modules and asm.on_locked is None


def test_a_set_config_without_the_client_installed_is_exit_code_2(tmp_path, monkeypatch, store, capsys):
    """Review Focus:客户端未安装但设了记忆配置。"""
    import importlib.metadata as md
    monkeypatch.setattr(md, "distribution", lambda name: (_ for _ in ()).throw(md.PackageNotFoundError(name)))
    code = main(["reconcile", str(store.repo_root), "--once", "--enable", "agent"], env=_memory_env(tmp_path))
    assert code == 2
    err = capsys.readouterr().err
    assert "pip install '.[memory]'" in err and "offline-test-key" not in err
    assert str(tmp_path / "mem") not in err and "127.0.0.1" not in err
    assert not (tmp_path / "mem" / ".owner").exists()        # 拒绝在碰根目录之前


def test_a_record_install_source_is_refused(tmp_path, store, monkeypatch, capsys):
    monkeypatch.setattr("factory.memory_config.importlib.metadata.distribution",
                        lambda name: _FakeDist(direct_url=None, record="starling_client/__init__.py,sha256=x,1\n"))
    assert main(["reconcile", str(store.repo_root), "--once", "--interact"], env=_memory_env(tmp_path)) == 2
    assert "git commit" in capsys.readouterr().err
    assert not (tmp_path / "mem").exists()


def test_a_client_and_server_from_different_commits_are_refused_in_spawn_mode(tmp_path, store, monkeypatch):
    import factory.memory_config as mc
    monkeypatch.setattr(mc, "client_install_commit", lambda name, **kw: ("a" if name == "starling-client" else "b") * 40)
    with pytest.raises(FactoryConfigError, match="starling-client is at a+ but starling-memory is at b+"):
        memory_wiring(store.repo_root, _memory_env(tmp_path), deployment=True)
    assert not (tmp_path / "mem").exists()


def test_an_empty_api_key_is_refused_by_name_in_spawn_mode(tmp_path, monkeypatch, store):
    """拉起模式下密钥要照传给服务进程,所以查;排在认领根目录之前。"""
    _pass_install_checks(monkeypatch)
    with pytest.raises(FactoryConfigError, match="^OPENAI_API_KEY "):
        memory_wiring(store.repo_root, _memory_env(tmp_path, OPENAI_API_KEY=""), deployment=True)
    assert not (tmp_path / "mem").exists()


def test_external_mode_does_not_ask_for_the_api_key_and_reads_the_token_file(tmp_path, monkeypatch, store):
    """外部模式下密钥在看板服务的进程里,rise 查不到也不查;连不上的服务在装配期退出码 2,文字不带地址与 token。"""
    _pass_install_checks(monkeypatch)
    token = tmp_path / "dash.token"
    token.write_text("s3cr3t-token\n", encoding="utf-8"); os.chmod(token, 0o600)
    env = _memory_env(tmp_path, OPENAI_API_KEY="", url="http://127.0.0.1:9", token_file=str(token))
    with pytest.raises(FactoryConfigError, match="memory service connection refused") as info:
        memory_wiring(store.repo_root, env, deployment=True)
    assert "s3cr3t" not in str(info.value) and "127.0.0.1" not in str(info.value)
    assert (tmp_path / "mem" / ".owner").exists()                 # 认领在连接之前


def test_a_client_that_cannot_be_loaded_is_the_same_install_hint(tmp_path, monkeypatch, store):
    """装了包但导入失败:译成同一条安装提示,不带根目录与端点。"""
    _pass_install_checks(monkeypatch)
    monkeypatch.setitem(sys.modules, "simple_actio.memory_starling", None)          # `from ... import` 抛 ImportError
    with pytest.raises(FactoryConfigError) as caught:
        memory_wiring(store.repo_root, _memory_env(tmp_path), deployment=True)
    text = str(caught.value)
    assert "pip install '.[memory]'" in text and "cannot be loaded" in text
    assert str(tmp_path / "mem") not in text and "127.0.0.1" not in text and "offline-test-key" not in text


def test_a_root_owned_by_another_repo_is_exit_code_2(tmp_path, store, monkeypatch, capsys):
    """Review Focus:记忆根目录被另一个仓库占用(两个子进程并发的用例在 test_factory_memory_config)。"""
    _pass_install_checks(monkeypatch)
    root = tmp_path / "mem"
    root.mkdir()
    (root / ".owner").write_text("/somewhere/else/repo\n", encoding="utf-8")
    assert main(["reconcile", str(store.repo_root), "--once", "--enable", "agent"], env=_memory_env(tmp_path)) == 2
    err = capsys.readouterr().err
    assert "/somewhere/else/repo" in err and str(store.repo_root.resolve()) in err
    assert not (root / "service.pid").exists()                   # 拒绝在拉起服务之前


@needs_service
def test_the_starling_wiring_logs_only_its_backend_name(tmp_path, store, fake, caplog):
    with caplog.at_level(logging.INFO, logger="factory.memory_runtime"):
        w = memory_wiring(store.repo_root, _service_env(tmp_path, fake), deployment=True)
    try:
        assert w.backend == "starling" and w.deployment is not None and callable(w.on_locked)
        assert type(w.training_worlds).__name__ == "StarlingTrainingWorlds" and w.session is not None
        assert caplog.text.count("memory backend: starling") == 1 and "stand-in" not in caplog.text
        assert str(tmp_path / "mem") not in caplog.text and fake.base_url not in caplog.text
        assert w.deployment.runtime["backend"] == "starling"
        assert w.deployment.runtime["starling_version"].endswith("+protocol:1")
        assert "world_settings" in w.deployment.runtime["extraction"]
    finally:
        for close in reversed(w.closers):
            close()


@needs_service
def test_with_starling_the_round_handler_gets_starling_memory_and_the_derived_spawn_budget(
        tmp_path, store, fake, monkeypatch):
    """拉起模式缺省 timeout_ms 4000、max_retries 0:一轮记忆最坏 76 秒,部署等提交 96、后端 146、收尾 join 156、
    等端口全关 166、准入退避 161、句柄超时 58(FC-51,数字的推法见 test_factory_memory_budget)。"""
    from simple_actio.memory_starling import StarlingDeploymentStore
    import factory.handlers as fh
    seen = {}
    monkeypatch.setattr(fh, "make_start_round_handler", lambda **kw: seen.setdefault("round", kw) and None or (lambda s, a: None))
    asm = assemble(store, FactoryOptions(switches=("agent", "admit")), env=_service_env(tmp_path, fake))
    try:
        r = seen["round"]
        assert isinstance(r["memory_store"], StarlingDeploymentStore)
        assert r["memory_layout"].root == tmp_path / "mem"
        assert r["memory_runtime"]["backend"] == "starling"
        runtime = r["client_factory"].__self__
        assert r["stream_deadline_s"] == r["config"].max_turns * 146.0
        assert (runtime._timeout, runtime._backend_timeout, runtime._join_timeout) == (96.0, 146.0, 156.0)
        assert (r["close_wait_s"], r["admission_wait_s"]) == (166.0, 161.0)
        assert r["memory_store"]._session.handle_timeout_s == 58.0     # 装配期经 retune 换上
        assert (tmp_path / "mem" / ".owner").read_text(encoding="utf-8").strip() == str(store.repo_root.resolve())
        assert callable(asm.on_locked)
    finally:
        asm.close()


@needs_service
def test_assembly_opens_no_deploy_world_handle_and_the_check_runs_under_the_lock(store, tmp_path, fake, monkeypatch):
    """Review Focus:设置核对在持仓库锁之后。第二个 run 撞锁之前不开任何句柄;核对开的是只读句柄。"""
    import factory.starling_session as ss
    opened = []
    real = ss.ServiceSession.open_port

    def spy(self, world, holder, *, read_only, **kw):
        opened.append((world, read_only))
        return real(self, world, holder, read_only=read_only, **kw)

    monkeypatch.setattr(ss.ServiceSession, "open_port", spy)
    asm = assemble(store, FactoryOptions(switches=("agent",)), env=_service_env(tmp_path, fake))
    try:
        assert opened == []                                              # 装配期不开句柄
        asm.on_locked()                                                  # 世界还没建:没有句柄可开
        assert opened == []
        mem = _round_store(asm)
        mem.remember_event("factory-deploy-s1", "pa", "k#0@pa", "pb：你好", interlocutor="pb")   # 第一次用时建
        opened.clear()
        asm.on_locked()
        assert opened == [("rise.deploy.factory-deploy-s1", True)]      # 只读,开完就关
        assert mem.wait_closed("factory-deploy-s1", 0.0) is True
    finally:
        asm.close()


_ROUND_MEMORY: list = []
_ROUND_RUNTIME: list = []


@pytest.fixture(autouse=True)
def _remember_round_memory(monkeypatch):
    """记下装配交给轮次处理器的部署记忆与运行时身份(`_round_memory` 用);处理器本身照常造。"""
    import factory.handlers as fh
    real = fh.make_start_round_handler

    def spy(**kw):
        _ROUND_MEMORY.append(kw["memory_store"])
        _ROUND_RUNTIME.append(kw["memory_runtime"])
        return real(**kw)

    monkeypatch.setattr(fh, "make_start_round_handler", spy)
    yield
    _ROUND_MEMORY.clear()
    _ROUND_RUNTIME.clear()


def _round_store(asm):
    """装配里那一份部署管理面:轮次处理器与门② 共用。"""
    return _ROUND_MEMORY[-1]


def _closer_owner(asm, cls_name):
    """`asm.closers` 里绑定方法所属的对象(按类名找):服务会话、拉起的服务。"""
    (owner,) = [c.__self__ for c in asm.closers if type(getattr(c, "__self__", None)).__name__ == cls_name]
    return owner


@needs_service
def test_a_deploy_world_with_other_settings_exits_2_only_after_the_lock(store, tmp_path, fake, capsys):
    asm = assemble(store, FactoryOptions(switches=("agent",)), env=_service_env(tmp_path, fake))
    try:
        _round_store(asm).remember_event("factory-deploy-s1", "pa", "k#0@pa", "pb：你好", interlocutor="pb")
    finally:
        asm.close()
    env2 = _service_env(tmp_path, fake, embedding_model="m-other")      # 改了嵌入配置
    assert main(["reconcile", str(store.repo_root), "--once", "--enable", "agent"], env=env2) == 2
    err = capsys.readouterr().err
    assert "world_settings_mismatch" in err and "rise.deploy.factory-deploy-s1" in err
    assert fake.base_url not in err and str(tmp_path) not in err


@needs_service
def test_a_deleted_deploy_world_exits_2_after_the_lock(store, tmp_path, fake, capsys):
    asm = assemble(store, FactoryOptions(switches=("agent",)), env=_service_env(tmp_path, fake))
    try:
        _round_store(asm).remember_event("factory-deploy-s1", "pa", "k#0@pa", "pb：你好", interlocutor="pb")
        _closer_owner(asm, "ServiceSession").admin("close_world", {"world": "rise.deploy.factory-deploy-s1"})
        from starling_service import delete_world
        delete_world(_closer_owner(asm, "SpawnedService"), "rise.deploy.factory-deploy-s1")
    finally:
        asm.close()
    assert main(["reconcile", str(store.repo_root), "--once", "--enable", "agent"], env=_service_env(tmp_path, fake)) == 2
    err = capsys.readouterr().err
    assert "was deleted" in err and "factory-deploy-s1.created" in err


@needs_service
def test_a_fatal_session_stops_the_daemon_with_exit_code_2(tmp_path, fake, monkeypatch, capsys):
    """会话进了致命状态:被套住的派发器跑完一条 pending 指令之后查到它,抛 FactoryConfigError,命令行退出码 2。
    要有一个动作:没有动作时派发器与处理器都不被调,无从查起。"""
    import factory.starling_session as ss
    repo = closed_loop_repo(str(tmp_path / "cp-run"))                     # 带一条 pending 指令
    monkeypatch.setattr(ss.ServiceSession, "fatal", property(lambda self: "commit cff319d != 0000000"))
    assert main(["reconcile", str(repo.repo_root), "--once", "--enable", "agent", "--allow-mock-evidence"],
                env=_service_env(tmp_path, fake)) == 2
    assert "memory service: commit cff319d != 0000000; see service.log" in capsys.readouterr().err


@needs_service
def test_closing_stops_the_spawned_service_last_and_releases_the_ownership_lock(store, tmp_path, fake):
    asm = assemble(store, FactoryOptions(switches=("agent",)), env=_service_env(tmp_path, fake))
    pid = json.loads((tmp_path / "mem" / "service.pid").read_text())["pid"]
    order = [type(getattr(c, "__self__", None)).__name__ for c in asm.closers]
    assert order[:2] == ["ServiceSession", "SpawnedService"]           # 最先登记,逆序执行时最后跑
    asm.close()
    assert not (tmp_path / "mem" / "service.pid").exists()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    with hold_file_lock(tmp_path / "mem" / "service.owner.lock"):
        pass


@needs_service
def test_a_failure_after_spawning_stops_the_service(store, tmp_path, fake, monkeypatch):
    import factory.memory_runtime as mr
    monkeypatch.setattr(mr, "StarlingTrainingWorlds", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        memory_wiring(store.repo_root, _service_env(tmp_path, fake), deployment=True)
    assert not (tmp_path / "mem" / "service.pid").exists()
    with hold_file_lock(tmp_path / "mem" / "service.owner.lock"):
        pass


def test_the_stand_in_round_handler_keeps_the_defaults(store, monkeypatch):
    import factory.handlers as fh
    seen = {}
    monkeypatch.setattr(fh, "make_start_round_handler", lambda **kw: seen.setdefault("round", kw) and None or (lambda s, a: None))
    asm = assemble(store, FactoryOptions(switches=("agent",)), env={})
    try:
        assert "stream_deadline_s" not in seen["round"]
        assert seen["round"]["client_factory"].__self__._timeout == 5.0
        assert asm.on_locked is None
    finally:
        asm.close()


def test_a_context_without_a_shared_runtime_gets_the_starling_budget_too(store, monkeypatch):
    """手工拼的 BuildContext(runtime 为 None)时 _agent 自己造运行时,时限与共用运行时同一个判法:拉起模式缺省配置
    推出的部署等提交 96 秒、后端 146、收尾 join 156,整条流 max_turns × 146。"""
    from factory.memory_budget import memory_time_budget
    import factory.handlers as fh
    from simple_actio.memory import FileTupleStore
    from factory.wire import BuildContext
    from mindagent.memory_layout import MemoryLayout
    from factory.memory_runtime import DeploymentMemory

    seen = {}
    monkeypatch.setattr(fh, "make_start_round_handler", lambda **kw: seen.setdefault("round", kw) and None or (lambda s, a: None))
    root = store.repo_root.parent / "mem-ctx"
    spawn = {"timeout_ms": 4000, "max_retries": 0}
    memory = DeploymentMemory(FileTupleStore(root / "deploy", held_root=root / "held", replay_root=root / "replay"),
                              MemoryLayout(root), {"backend": "starling"}, memory_time_budget(spawn, spawn))
    ctx = BuildContext(store=store, accept_mock_evidence=False, confirm_directives=None, planner_backend="heuristic",
                       env={}, log_dir=None, deployment_memory=memory)
    fh._agent(ctx)
    try:
        r = seen["round"]
        runtime = r["client_factory"].__self__
        assert r["stream_deadline_s"] == r["config"].max_turns * 146.0
        assert (runtime._timeout, runtime._backend_timeout, runtime._join_timeout) == (96.0, 146.0, 156.0)
    finally:
        wire._close_all(ctx.closers)


def test_run_passes_on_locked_to_the_daemon_and_the_stand_in_passes_none(store, monkeypatch):
    seen = []
    marker = lambda: None
    with monkeypatch.context() as m:
        m.setattr(wire, "run_daemon", lambda store_, interval_s, **kw: seen.append(kw.get("on_locked")))
        m.setattr(wire, "memory_wiring", lambda repo_root, env, *, deployment: _fake_wiring(repo_root, marker))
        wire.run(str(store.repo_root), FactoryOptions(switches=("agent",)), max_ticks=1, env={})
    with monkeypatch.context() as m:
        m.setattr(wire, "run_daemon", lambda store_, interval_s, **kw: seen.append(kw.get("on_locked")))
        wire.run(str(store.repo_root), FactoryOptions(switches=("agent",)), max_ticks=1, env={})
    assert seen == [marker, None]


def test_on_locked_is_not_run_by_assemble(store, monkeypatch):
    """核对要等持仓库锁:assemble 只交出函数,自己不调。"""
    called = []
    monkeypatch.setattr(wire, "memory_wiring",
                        lambda repo_root, env, *, deployment: _fake_wiring(repo_root, lambda: called.append(1)))
    asm = assemble(store, FactoryOptions(switches=("agent",)), env={})
    try:
        assert called == [] and callable(asm.on_locked)
    finally:
        asm.close()


def test_reconcile_runs_on_locked_after_taking_the_repo_lock_and_before_the_tick(store, monkeypatch):
    order = []
    real_lock = wire.acquire_repo_lock

    def lock(root):
        order.append("lock")
        return real_lock(root)

    monkeypatch.setattr(wire, "acquire_repo_lock", lock)
    monkeypatch.setattr(wire, "memory_wiring",
                        lambda repo_root, env, *, deployment: _fake_wiring(repo_root, lambda: order.append("check")))
    monkeypatch.setattr(wire, "reconcile_once", lambda *a, **kw: order.append("tick") or [])
    wire.reconcile_one_tick(str(store.repo_root), FactoryOptions(switches=("agent",)), env={})
    assert order == ["lock", "check", "tick"]


def test_a_failing_on_locked_closes_the_assembly_and_skips_the_tick(store, monkeypatch):
    closed, ticks = [], []

    def boom():
        raise FactoryConfigError("memory service: world rise.deploy.s1: world_settings_mismatch")

    monkeypatch.setattr(wire, "memory_wiring", lambda repo_root, env, *, deployment: _fake_wiring(repo_root, boom))
    monkeypatch.setattr(wire, "reconcile_once", lambda *a, **kw: ticks.append(1) or [])
    real_close = wire.Assembly.close
    monkeypatch.setattr(wire.Assembly, "close", lambda self: (closed.append(1), real_close(self)))
    with pytest.raises(FactoryConfigError, match="world_settings_mismatch"):
        wire.reconcile_one_tick(str(store.repo_root), FactoryOptions(switches=("agent",)), env={})
    assert ticks == [] and closed == [1]


def test_the_fatal_guard_turns_a_fatal_session_into_a_config_error_after_the_call(store, monkeypatch):
    """致命停机的组合根那一半:处理器返回或抛出之后查致命状态;没进致命状态原样返回或上抛。"""
    class Session:
        fatal = None

    session = Session()
    ok = wire._guard(lambda: "done", session)
    assert ok() == "done"
    failing = wire._guard(lambda: (_ for _ in ()).throw(ValueError("x")), session)
    with pytest.raises(ValueError):
        failing()
    session.fatal = "reconnect refused: unauthorized"
    with pytest.raises(FactoryConfigError, match="memory service: reconnect refused: unauthorized; see service.log"):
        ok()
    with pytest.raises(FactoryConfigError):
        failing()


def test_the_variable_is_not_read_when_no_memory_is_needed(store, tmp_path, monkeypatch):
    """不带 --enable 与 --interact 的路径(与 mindtrainer.wire.run 逐字节相同)不读这个变量,坏配置也不影响它。"""
    calls = []
    monkeypatch.setattr(wire, "memory_wiring", lambda *a, **kw: calls.append(1))
    env = {MEMORY_CONFIG_ENV: str(tmp_path / "missing.yaml")}
    assemble(store, FactoryOptions(), env=env).close()
    assemble(store, FactoryOptions(switches=("track", "plan")), env=env, builders=_no_builders()).close()
    assert calls == []


def _no_builders():
    return {"record_track": lambda ctx: (lambda s, a: None), "plan_next_round": lambda ctx: (lambda s, a: None)}


@pytest.mark.parametrize("options", [
    FactoryOptions(interact=True),                                   # --interact 单独用:wire.py 里 `if not keys:` 的独立分支
    FactoryOptions(switches=("admit",)),                             # 只靠 needs_deployment 集合里的 admit_proposal 触发
    FactoryOptions(switches=("agent",)),
    FactoryOptions(interact=True, switches=("agent",)),
], ids=["interact", "admit", "agent", "interact+agent"])
def test_a_bad_memory_config_is_refused_at_every_entry_that_needs_memory(store, tmp_path, options):
    """第 10 节:配置有问题不退回替身。四个需要记忆的入口各钉一次;memory_wiring 排在 _shared_runtime 与
    _interact_wiring 之前,所以不必准备端点。断言文字里有 missing.yaml:确认抛的是记忆配置那条,不是别的装配错误。"""
    env = {MEMORY_CONFIG_ENV: str(tmp_path / "missing.yaml")}
    with pytest.raises(FactoryConfigError) as caught:
        assemble(store, options, env=env)
    assert "missing.yaml" in str(caught.value)


def test_a_bad_memory_config_is_exit_code_2_even_with_the_stand_in_files_absent(store, tmp_path, capsys):
    """配置有任何问题都在装配期拒绝,不退回替身:这里配置文件不存在。"""
    env = {MEMORY_CONFIG_ENV: str(tmp_path / "missing.yaml")}
    assert main(["reconcile", str(store.repo_root), "--once", "--enable", "agent"], env=env) == 2
    err = capsys.readouterr().err
    assert err.startswith("factory: error: ") and "missing.yaml" in err


def test_a_memory_call_budget_over_four_seconds_is_exit_code_2(store, tmp_path, capsys):
    """I-3:一次调用 timeout_ms × (max_retries + 1) 加重试之间的退避超过 4000 毫秒,装配期按配置错误退出,外层时限不再被内层顶穿。"""
    env = _memory_env(tmp_path)
    path = tmp_path / "memory.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data.update(timeout_ms=2001, max_retries=1)                   # 2 × 2001 + 退避 1000 = 5002
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert main(["reconcile", str(store.repo_root), "--once", "--enable", "agent"], env=env) == 2
    err = capsys.readouterr().err
    assert err.startswith("factory: error: ") and "timeout_ms" in err and "max_retries" in err
    assert "2001" not in err and "127.0.0.1" not in err
    assert not (tmp_path / "mem").exists()                           # 拒绝先于认领记忆根目录


# ---------------- 旧布局拒绝启动(设计三 4.8 节):记忆根是按人格建库的旧样子,退出码 2 ----------------

def _starling_env(tmp_path, monkeypatch):
    """装没装客户端都走到旧布局检查:把安装检查打桩成通过(旧布局检查排在导入客户端与拉起服务之前)。"""
    _pass_install_checks(monkeypatch)
    return _memory_env(tmp_path)


def _touch(root, *rels):
    for rel in rels:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")


def _stand_in_root(store):
    from controlplane.lock import git_dir
    return git_dir(store.repo_root) / "rise-memory"


# 只按文件名判,分不出"已从 agent.yaml 移除的情境的世界"和"旧的按人格库":文字指向移回或移走,不指向清空
_MOVE_HINT = ("if any of these are worlds of scenarios removed from agent.yaml, add the scenarios back to agent.yaml "
              "or move the files out of the memory root; delete them only once they are confirmed to be old "
              "per-persona libraries")


def test_an_old_per_persona_memory_root_exits_2_listing_the_suspect_entries(tmp_path, store, monkeypatch):
    env = _starling_env(tmp_path, monkeypatch)
    root = tmp_path / "mem"
    _touch(root, "deploy/pa.db")
    with pytest.raises(FactoryConfigError, match=r"old per-persona layout \(deploy/pa.db") as caught:
        memory_wiring(store.repo_root, env, deployment=True)
    assert str(caught.value) == (
        f"memory root has the old per-persona layout (deploy/pa.db); {_MOVE_HINT}")
    assert "clear deploy/" not in str(caught.value)
    assert str(root) not in str(caught.value)
    assert (root / "deploy" / "pa.db").exists() and not (root / "runs").exists()      # 只读检查:什么都没动


def test_the_old_layout_refusal_is_exit_code_2_at_every_entry_that_needs_memory(tmp_path, store, monkeypatch, capsys):
    env = _starling_env(tmp_path, monkeypatch)
    _touch(tmp_path / "mem", "deploy/pa.db")
    for args in (["--enable", "agent"], ["--enable", "admit"], ["--interact"]):
        assert main(["reconcile", str(store.repo_root), "--once", *args], env=env) == 2
        err = capsys.readouterr().err
        assert err.startswith("factory: error: memory root has the old per-persona layout (deploy/pa.db")
        assert str(tmp_path / "mem") not in err


def test_the_old_layout_message_lists_up_to_five_entries_in_sorted_order(tmp_path, store, monkeypatch):
    env = _starling_env(tmp_path, monkeypatch)
    _touch(tmp_path / "mem", "deploy/pb.db", "deploy/pa.db", "snapshots/r1/deploy-m-s1-1-pa.db")
    with pytest.raises(FactoryConfigError) as caught:
        memory_wiring(store.repo_root, env, deployment=True)
    text = str(caught.value)
    assert f"(deploy/pa.db, deploy/pb.db, snapshots/r1/deploy-m-s1-1-pa.db); {_MOVE_HINT}" in text
    assert "more" not in text and "agent.yaml" in text and "clear deploy/" not in text


def test_the_old_layout_message_counts_the_entries_past_the_first_five(tmp_path, store, monkeypatch):
    env = _starling_env(tmp_path, monkeypatch)
    _touch(tmp_path / "mem", *(f"deploy/p{i}.db" for i in range(6)))
    with pytest.raises(FactoryConfigError) as caught:
        memory_wiring(store.repo_root, env, deployment=True)
    text = str(caught.value)
    assert "(deploy/p0.db, deploy/p1.db, deploy/p2.db, deploy/p3.db, deploy/p4.db and 1 more); " in text
    assert "deploy/p5.db" not in text and str(tmp_path) not in text


def _record(root, rel, world="factory-deploy-s1"):
    """写一份合法的在途记录(两个后端同一格式)。"""
    from mindagent.memory_layout import MemoryLayout, SnapshotRecord
    MemoryLayout(root).write_record(root / rel, SnapshotRecord(world, "snap-0", "d" * 64))


@needs_service
def test_records_markers_and_runtime_files_are_the_final_starling_layout(tmp_path, store, fake):
    """第 4 步起 Starling 后端的最终规则:`deploy/` 下带数据后缀的文件只许 `<已知情境>.created`;`.lock`、`-wal`、`.tmp`
    不查;在途记录与 `verify/` 合法。"""
    root = tmp_path / "mem"
    _touch(root, "deploy/factory-deploy-s1.created", "deploy/factory-deploy-s1.lock", "deploy/factory-deploy-s1.db-wal",
           "deploy/.factory-deploy-s1.created.123.tmp")
    _record(root, "snapshots/r1/deploy-deploy-m-s1-1.rec.json")
    _record(root, "snapshots/r1/verify-factory-deploy-s1.rec.json")
    w = memory_wiring(store.repo_root, _service_env(tmp_path, fake), deployment=True)
    try:
        assert w.backend == "starling"
    finally:
        for close in w.closers:
            close()


@pytest.mark.parametrize("rel", ["deploy/factory-deploy-s1.db", "seeds/k/s1.db", "runs/run-1/e.db",
                                 "snapshots/r1/deploy-m-1.world", "held/s1/snap-0.db",
                                 "replay/rise.verify.r1.s1.00000000.db"])
def test_a_starling_root_left_from_the_in_process_era_exits_2_listing_it(store, tmp_path, monkeypatch, capsys, rel):
    env = _starling_env(tmp_path, monkeypatch)
    _touch(tmp_path / "mem", rel)
    assert main(["reconcile", str(store.repo_root), "--once", "--enable", "agent"], env=env) == 2
    err = capsys.readouterr().err
    assert rel.split("/")[0] in err and str(tmp_path) not in err
    assert not (tmp_path / "mem" / "service.pid").exists()               # 旧布局在拉起服务之前就拒绝


def test_half_written_and_runtime_files_never_block_a_stand_in_start(store):
    root = _stand_in_root(store)
    for rel in ("deploy/s1.json.tmp", "deploy/s1.db-wal", "deploy/s1.db-shm", "held/s1/snap-0.json",
                "replay/rise.verify.r1.s1.00000000.json"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True); (root / rel).write_text("{}")
    memory_wiring(store.repo_root, {}, deployment=True)                  # 替身在造部署记忆时查,不抛


@pytest.mark.parametrize("rel", ["snapshots/r1/deploy-deploy-m-s1-1.db", "snapshots/r1/deploy-deploy-m-s1-1.world",
                                 "snapshots/r1/deploy-deploy-m-s1-1.json", "deploy/factory-deploy-s1.json",
                                 "deploy/factory-deploy-s1.rec.json", "deploy/factory-deploy-s9.created"])
def test_snapshot_files_world_tags_and_the_other_backends_worlds_are_the_old_layout(tmp_path, store, monkeypatch, rel):
    env = _starling_env(tmp_path, monkeypatch)
    _touch(tmp_path / "mem", rel)
    with pytest.raises(FactoryConfigError) as caught:
        memory_wiring(store.repo_root, env, deployment=True)
    assert str(caught.value) == f"memory root has the old per-persona layout ({rel}); {_MOVE_HINT}"


def test_without_an_agent_yaml_every_marker_under_deploy_is_the_old_layout(tmp_path, monkeypatch):
    """repo 里没有 config/agent.yaml:已知情境是空集,deploy/ 下任何数据文件都算旧布局(包括看起来像情境名的)。"""
    env = _starling_env(tmp_path, monkeypatch)
    _touch(tmp_path / "mem", "deploy/factory-deploy-s1.created")
    with pytest.raises(FactoryConfigError, match=r"old per-persona layout \(deploy/factory-deploy-s1.created\)"):
        memory_wiring(tmp_path / "repo", env, deployment=True)


def test_a_marker_outside_the_configured_scenarios_is_the_old_layout(tmp_path, store, monkeypatch):
    env = _starling_env(tmp_path, monkeypatch)
    _touch(tmp_path / "mem", "deploy/factory-deploy-s1.created", "deploy/factory-deploy-s9.created")
    with pytest.raises(FactoryConfigError, match=r"old per-persona layout \(deploy/factory-deploy-s9.created\)"):
        memory_wiring(store.repo_root, env, deployment=True)


def test_a_broken_agent_yaml_propagates_as_its_own_config_error(tmp_path, store, monkeypatch):
    from factory.agent import AGENT_CONFIG
    env = _starling_env(tmp_path, monkeypatch)
    (store.repo_root / AGENT_CONFIG).write_text("not: [an envelope", encoding="utf-8")
    with pytest.raises(FactoryConfigError, match="agent.yaml cannot be read") as caught:
        memory_wiring(store.repo_root, env, deployment=True)
    assert "old per-persona layout" not in str(caught.value)


def test_the_old_layout_check_runs_after_the_owner_claim_and_before_the_client_import(tmp_path, store, monkeypatch):
    env = _starling_env(tmp_path, monkeypatch)
    _touch(tmp_path / "mem", "deploy/pa.db")
    monkeypatch.setitem(sys.modules, "simple_actio.memory_starling", None)          # 导入会抛 ImportError:要是先导入就是安装提示
    with pytest.raises(FactoryConfigError, match="old per-persona layout"):
        memory_wiring(store.repo_root, env, deployment=True)
    assert (tmp_path / "mem" / ".owner").exists()                              # 认领在前


def test_an_unreadable_in_flight_record_is_exit_code_2_at_assembly_naming_it_relative_to_the_root(
        tmp_path, store, monkeypatch, capsys):
    env = _starling_env(tmp_path, monkeypatch)
    root = tmp_path / "mem"
    rec = root / "snapshots" / "r1" / "deploy-deploy-m-s1-1.rec.json"
    rec.parent.mkdir(parents=True); rec.write_text('{"world_id": "../escape", "snapshot_id": "x", "digest": "d"}')
    assert main(["reconcile", str(store.repo_root), "--once", "--enable", "agent"], env=env) == 2
    err = capsys.readouterr().err
    assert err.startswith("factory: error: ") and "snapshots/r1/deploy-deploy-m-s1-1.rec.json" in err
    assert str(root) not in err and "old per-persona layout" not in err and "../escape" not in err


def test_an_unreadable_in_flight_record_exits_2_naming_it_relative_to_the_root(tmp_path, store, monkeypatch, capsys):
    root = _stand_in_root(store)
    rec = root / "snapshots" / "r1" / "deploy-deploy-m-s1-1.rec.json"
    rec.parent.mkdir(parents=True); rec.write_text("not json")
    assert main(["reconcile", str(store.repo_root), "--once", "--enable", "agent"], env={}) == 2
    err = capsys.readouterr().err
    assert "snapshots/r1/deploy-deploy-m-s1-1.rec.json" in err and str(root) not in err


def test_the_stand_in_root_is_judged_by_the_stand_in_rules(store):
    from factory.memory_runtime import stand_in_deployment_memory
    root = _stand_in_root(store)
    _touch(root, "deploy/factory-deploy-s1.json", "deploy/factory-deploy-s1.json.tmp", "deploy/factory-deploy-s1.lock",
           "held/factory-deploy-s1/snap-0.json", "replay/rise.verify.r1.factory-deploy-s1.00000000.json")
    _record(root, "snapshots/r1/deploy-deploy-m-s1-1.rec.json")
    stand_in_deployment_memory(store.repo_root)                       # 替身的世界、快照与重放世界都合法
    _touch(root, "deploy/factory-deploy-s1.db")                        # 进程内 Starling 的世界在替身根下是旧布局
    with pytest.raises(FactoryConfigError, match=r"old per-persona layout \(deploy/factory-deploy-s1.db\)"):
        stand_in_deployment_memory(store.repo_root)


def test_an_old_per_persona_stand_in_root_is_refused_with_the_json_suffix(store, capsys):
    root = _stand_in_root(store)
    _touch(root, "deploy/pa.json", "deploy/factory-deploy-s1.json", "deploy/pa.lock")
    with pytest.raises(FactoryConfigError, match=r"old per-persona layout \(deploy/pa.json\); if any of these"):
        memory_wiring(store.repo_root, {}, deployment=True)
    assert main(["reconcile", str(store.repo_root), "--once", "--enable", "agent"], env={}) == 2
    assert "old per-persona layout (deploy/pa.json)" in capsys.readouterr().err
    assert (root / "deploy" / "pa.json").exists()


def test_a_stand_in_root_with_only_configured_scenario_worlds_and_records_is_fine(store):
    from factory.memory_runtime import stand_in_deployment_memory
    root = _stand_in_root(store)
    _touch(root, "deploy/factory-deploy-s1.json", "deploy/factory-deploy-s1.lock")
    _record(root, "snapshots/r1/deploy-deploy-m-s1-1.rec.json")
    _record(root, "snapshots/r1/verify-factory-deploy-s1.rec.json")
    mem = stand_in_deployment_memory(store.repo_root)
    assert mem.layout.root == root
    assert (mem.store._held_root, mem.store._replay_root) == (root / "held", root / "replay")


def test_the_stand_in_without_deployment_memory_checks_nothing(store):
    """--interact 单独用时替身不造部署记忆、不碰磁盘,记忆根里有旧文件也不拦。"""
    _touch(_stand_in_root(store), "deploy/pa.json")
    assert memory_wiring(store.repo_root, {}, deployment=False).deployment is None


# ---------------- 外部模式:端点跟 Starling 默认世界走(设计四补丁) ----------------

@pytest.fixture
def provider_service(tmp_path_factory, fake):
    """tmp 下的 starling-serve,`starling.json` 里有一个 provider 绑到抽取与嵌入,地址指向假端点,密钥是占位值。
    模拟用户的看板服务:rise 外部模式连它,不给任何端点键。"""
    pytest.importorskip("starling_client"); pytest.importorskip("starling.service")
    from starling_service import provider_starling_json, start_service
    proc = start_service(tmp_path_factory.mktemp("ext"), starling_json=provider_starling_json(fake))
    yield proc
    proc.kill()


def _external_env(tmp_path, proc, monkeypatch):
    """外部模式的配置:只有 root、url、token_file。安装来源打桩成服务报的提交;密钥变量不给。"""
    import starling_client
    _pass_install_checks(monkeypatch, commit=starling_client.COMMIT)
    path = tmp_path / "memory.yaml"
    path.write_text(yaml.safe_dump({"root": str(tmp_path / "mem"), "url": proc.url,
                                    "token_file": str(proc.token_path)}), encoding="utf-8")
    os.chmod(path, 0o600)
    return {MEMORY_CONFIG_ENV: str(path), "PATH": os.environ["PATH"]}


@needs_service
def test_external_mode_takes_the_default_worlds_endpoints_and_a_changed_model_exits_2(
        store, tmp_path, provider_service, monkeypatch, capsys):
    from starling_service import dashboard
    env = _external_env(tmp_path, provider_service, monkeypatch)
    asm = assemble(store, FactoryOptions(switches=("agent",)), env=env)
    try:
        mem = _round_store(asm)
        settings = mem._settings
        assert settings["extraction"]["provider_name"] == "p" and settings["embedding"]["provider_name"] == "e"
        assert "base_url" not in settings["extraction"] and settings["embedding"]["dim"] == 256
        runtime = _ROUND_RUNTIME[-1]
        assert runtime["extraction"]["model"] == "fake-extract" and "base_url" not in runtime["extraction"]
        mem.remember_event("factory-deploy-s1", "pa", "k#0@pa", "pb：你好", interlocutor="pb")   # 建世界、写一条
        asm.on_locked()                                         # 带 rise 设置开只读句柄比较:一致
    finally:
        asm.close()
    with dashboard(provider_service) as http:                  # 人在看板里换了抽取模型
        http.post("/api/config", json={"providers": {"p": {"model": "fake-extract-2"}}}).raise_for_status()
    assert main(["reconcile", str(store.repo_root), "--once", "--enable", "agent"], env=env) == 2
    err = capsys.readouterr().err
    assert "world_settings_mismatch" in err and "rise.deploy.factory-deploy-s1" in err
    assert provider_service.token not in err and "127.0.0.1" not in err


@needs_service
def test_external_mode_refuses_a_default_world_whose_embedding_role_is_unbound(
        store, tmp_path, provider_service, monkeypatch):
    from starling_service import dashboard
    env = _external_env(tmp_path, provider_service, monkeypatch)
    with dashboard(provider_service) as http:
        http.post("/api/config", json={"roles": {"embedding": ""}}).raise_for_status()
    try:
        with pytest.raises(FactoryConfigError, match="default world's embedding role has no provider bound"):
            memory_wiring(store.repo_root, env, deployment=True)
    finally:
        with dashboard(provider_service) as http:
            http.post("/api/config", json={"roles": {"embedding": "e"}}).raise_for_status()


# ---------------- 外部模式的时间预算(FC-51):各层按服务端点推出,经组合根传到每一层 ----------------

def _derived_wiring(budget):
    from simple_actio.memory import FileTupleStore
    from factory.memory_runtime import DeploymentMemory
    from mindagent.memory_layout import MemoryLayout

    def make(repo_root, env, *, deployment):
        root = repo_root.parent / "mem-derived"
        mem = DeploymentMemory(FileTupleStore(root / "deploy", held_root=root / "held", replay_root=root / "replay"),
                               MemoryLayout(root), {"backend": "starling"}, budget) if deployment else None
        return MemoryWiring(backend="starling", deployment=mem, memory_resolver=stand_in_memory_resolver,
                            training_worlds=StandInTrainingWorlds(), budget=budget)
    return make


def test_external_mode_passes_every_derived_layer_to_the_runtime_and_the_round_handler(store, monkeypatch):
    from factory.memory_budget import memory_time_budget
    import factory.handlers as fh
    import factory.memory_runtime as mr
    budget = memory_time_budget({"timeout_ms": 120000}, {"timeout_ms": 0})
    monkeypatch.setattr(mr, "memory_wiring", _derived_wiring(budget))
    seen = {}
    monkeypatch.setattr(fh, "make_start_round_handler", lambda **kw: seen.setdefault("round", kw) and None or (lambda s, a: None))
    asm = assemble(store, FactoryOptions(switches=("agent",)), env={})
    try:
        r = seen["round"]
        runtime = r["client_factory"].__self__
        assert (runtime._timeout, runtime._backend_timeout, runtime._join_timeout) == (8073.0, 8123.0, 8133.0)
        assert runtime._message_timeout is None                          # 只开部署路时不设相邻消息上限
        assert r["stream_deadline_s"] == r["config"].max_turns * 8123.0
        assert (r["close_wait_s"], r["admission_wait_s"]) == (8143.0, 8138.0)
    finally:
        asm.close()


def test_the_stand_in_passes_no_derived_layer(store, monkeypatch):
    import factory.handlers as fh
    seen = {}
    monkeypatch.setattr(fh, "make_start_round_handler", lambda **kw: seen.setdefault("round", kw) and None or (lambda s, a: None))
    asm = assemble(store, FactoryOptions(switches=("agent",)), env={})
    try:
        r = seen["round"]
        runtime = r["client_factory"].__self__
        assert (runtime._backend_timeout, runtime._join_timeout) == (None, None)
        assert "close_wait_s" not in r and "admission_wait_s" not in r
    finally:
        asm.close()
