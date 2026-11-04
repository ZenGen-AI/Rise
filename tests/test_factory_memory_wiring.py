"""组合根的记忆后端选择(设计二 5.1、6.2、6.4 节,第 10 节拒绝项)。"""
from __future__ import annotations

import logging
import os
import sys

import pytest
import yaml

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


@pytest.fixture
def store(tmp_path):
    return closed_loop_repo(str(tmp_path / "cp"), run_id=None)


def _memory_env(tmp_path, root=None, **extra):
    path = tmp_path / "memory.yaml"
    path.write_text(yaml.safe_dump({
        "root": str(root or tmp_path / "mem"), "extraction_base_url": "http://127.0.0.1:9/v1",
        "extraction_model": "m-x", "embedding_base_url": "http://127.0.0.1:9/v1",
        "embedding_model": "m-e", "embedding_dim": 256,
    }), encoding="utf-8")
    os.chmod(path, 0o600)
    return {MEMORY_CONFIG_ENV: str(path), "OPENAI_API_KEY": "offline-test-key", **extra}


def _fake_wiring(repo_root, sweep):
    return MemoryWiring(backend="stand-in", deployment=stand_in_deployment_memory(repo_root),
                        memory_resolver=stand_in_memory_resolver, training_worlds=StandInTrainingWorlds(), sweep=sweep)


def test_turn_timeouts_follow_section_6_4():
    assert deploy_turn_timeout_s(interact=False, starling=False) is None     # 替身:保持 5 秒
    assert deploy_turn_timeout_s(interact=False, starling=True) == 40.0
    assert deploy_turn_timeout_s(interact=True, starling=True) == 150.0      # 同开取较大的 150
    assert deploy_turn_timeout_s(interact=True, starling=False) == 150.0


def test_without_the_variable_the_wiring_is_the_stand_in_and_logs_it_once(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="factory.memory_runtime"):
        w = memory_wiring(tmp_path / "repo", {}, deployment=True)
    assert w.backend == "stand-in" and isinstance(w.training_worlds, StandInTrainingWorlds) and w.sweep is None
    assert w.deployment is not None and w.memory_resolver is stand_in_memory_resolver
    assert caplog.text.count("memory backend: stand-in") == 1


def test_the_stand_in_builds_no_deployment_memory_unless_asked(tmp_path):
    assert memory_wiring(tmp_path / "repo", {}, deployment=False).deployment is None


def test_a_set_config_without_starling_installed_is_exit_code_2(tmp_path, monkeypatch, store, capsys):
    """Review Focus:Starling 未安装但设了记忆配置。"""
    import importlib.metadata as md
    monkeypatch.setattr(md, "distribution", lambda name: (_ for _ in ()).throw(md.PackageNotFoundError(name)))
    code = main(["reconcile", str(store.repo_root), "--once", "--enable", "agent"], env=_memory_env(tmp_path))
    assert code == 2
    err = capsys.readouterr().err
    assert "pip install '.[memory]'" in err and "offline-test-key" not in err
    assert str(tmp_path / "mem") not in err and "127.0.0.1" not in err
    assert not (tmp_path / "mem" / ".owner").exists()        # 拒绝在碰根目录之前


def test_an_empty_api_key_is_refused_by_name(tmp_path, monkeypatch, store):
    """装没装 Starling 都要走到密钥这一条:把安装检查打桩成通过。密钥检查排在认领根目录之前。"""
    import factory.memory_runtime as mr
    monkeypatch.setattr(mr, "starling_install_source", lambda: "git:abc")
    with pytest.raises(FactoryConfigError, match="^OPENAI_API_KEY "):
        memory_wiring(store.repo_root, _memory_env(tmp_path, OPENAI_API_KEY=""), deployment=True)
    assert not (tmp_path / "mem").exists()


def test_a_starling_that_cannot_be_loaded_is_the_same_install_hint(tmp_path, monkeypatch, store):
    """装了包但 C++ 扩展载不进来:导入 actio.memory_starling 抛 ImportError,译成同一条安装提示,不带根目录与端点。"""
    import factory.memory_runtime as mr
    monkeypatch.setattr(mr, "starling_install_source", lambda: "git:abc")
    monkeypatch.setitem(sys.modules, "actio.memory_starling", None)          # `from ... import` 抛 ImportError
    with pytest.raises(FactoryConfigError) as caught:
        memory_wiring(store.repo_root, _memory_env(tmp_path), deployment=True)
    text = str(caught.value)
    assert "pip install '.[memory]'" in text and "cannot be loaded" in text
    assert str(tmp_path / "mem") not in text and "127.0.0.1" not in text and "offline-test-key" not in text


def test_a_root_owned_by_another_repo_is_exit_code_2(tmp_path, store, capsys):
    """Review Focus:记忆根目录被另一个仓库占用(两个子进程并发的用例在 Task 16)。"""
    pytest.importorskip("starling")
    root = tmp_path / "mem"
    root.mkdir()
    (root / ".owner").write_text("/somewhere/else/repo\n", encoding="utf-8")
    assert main(["reconcile", str(store.repo_root), "--once", "--enable", "agent"], env=_memory_env(tmp_path)) == 2
    err = capsys.readouterr().err
    assert "/somewhere/else/repo" in err and str(store.repo_root.resolve()) in err


def test_the_starling_wiring_logs_only_its_backend_name(tmp_path, store, caplog):
    pytest.importorskip("starling")
    with caplog.at_level(logging.INFO, logger="factory.memory_runtime"):
        w = memory_wiring(store.repo_root, _memory_env(tmp_path), deployment=True)
    assert w.backend == "starling" and w.deployment is not None and callable(w.sweep)
    assert type(w.training_worlds).__name__ == "StarlingTrainingWorlds" and w.sweep == w.training_worlds.sweep
    assert caplog.text.count("memory backend: starling") == 1 and "stand-in" not in caplog.text
    assert str(tmp_path / "mem") not in caplog.text and "127.0.0.1" not in caplog.text
    assert w.deployment.runtime["backend"] == "starling"


def test_with_starling_the_round_handler_gets_starling_memory_and_a_40s_budget(tmp_path, store, monkeypatch):
    pytest.importorskip("starling")
    from actio.memory_starling import StarlingDeploymentStore
    import factory.handlers as fh
    seen = {}
    monkeypatch.setattr(fh, "make_start_round_handler", lambda **kw: seen.setdefault("round", kw) and None or (lambda s, a: None))
    asm = assemble(store, FactoryOptions(switches=("agent", "admit")), env=_memory_env(tmp_path))
    try:
        r = seen["round"]
        assert isinstance(r["memory_store"], StarlingDeploymentStore)
        assert r["memory_layout"].suffix == ".db" and r["memory_layout"].root == tmp_path / "mem"
        assert r["memory_runtime"]["backend"] == "starling"
        assert r["stream_deadline_s"] == r["config"].max_turns * 40.0
        assert r["client_factory"].__self__._timeout == 40.0           # AgentRuntime 的服务端等提交
        assert (tmp_path / "mem" / ".owner").read_text(encoding="utf-8").strip() == str(store.repo_root.resolve())
        assert callable(asm.sweep)
    finally:
        asm.close()


def test_the_stand_in_round_handler_keeps_the_defaults(store, monkeypatch):
    import factory.handlers as fh
    seen = {}
    monkeypatch.setattr(fh, "make_start_round_handler", lambda **kw: seen.setdefault("round", kw) and None or (lambda s, a: None))
    asm = assemble(store, FactoryOptions(switches=("agent",)), env={})
    try:
        assert "stream_deadline_s" not in seen["round"]
        assert seen["round"]["client_factory"].__self__._timeout == 5.0
        assert asm.sweep is None
    finally:
        asm.close()


def test_a_context_without_a_shared_runtime_gets_the_starling_budget_too(store, monkeypatch):
    """手工拼的 BuildContext(runtime 为 None)时 _agent 自己造运行时:Starling 一样等 40 秒、整条流 max_turns × 40。"""
    import factory.handlers as fh
    from actio.memory import FileTupleStore
    from factory.wire import BuildContext
    from mindagent.memory_layout import MemoryLayout
    from factory.memory_runtime import DeploymentMemory

    seen = {}
    monkeypatch.setattr(fh, "make_start_round_handler", lambda **kw: seen.setdefault("round", kw) and None or (lambda s, a: None))
    root = store.repo_root.parent / "mem-ctx"
    memory = DeploymentMemory(FileTupleStore(root / "deploy"), MemoryLayout(root), {"backend": "starling"}, FileTupleStore)
    ctx = BuildContext(store=store, accept_mock_evidence=False, confirm_directives=None, planner_backend="heuristic",
                       env={}, log_dir=None, deployment_memory=memory)
    fh._agent(ctx)
    try:
        r = seen["round"]
        assert r["stream_deadline_s"] == r["config"].max_turns * 40.0
        assert r["client_factory"].__self__._timeout == 40.0
    finally:
        wire._close_all(ctx.closers)


def test_run_passes_the_sweep_as_on_locked_and_the_stand_in_passes_none(store, monkeypatch):
    seen = []
    marker = lambda: 0
    with monkeypatch.context() as m:
        m.setattr(wire, "run_daemon", lambda store_, interval_s, **kw: seen.append(kw.get("on_locked")))
        m.setattr(wire, "memory_wiring", lambda repo_root, env, *, deployment: _fake_wiring(repo_root, marker))
        wire.run(str(store.repo_root), FactoryOptions(switches=("agent",)), max_ticks=1, env={})
    with monkeypatch.context() as m:
        m.setattr(wire, "run_daemon", lambda store_, interval_s, **kw: seen.append(kw.get("on_locked")))
        wire.run(str(store.repo_root), FactoryOptions(switches=("agent",)), max_ticks=1, env={})
    assert seen == [marker, None]


def test_the_sweep_is_not_run_by_assemble(store, monkeypatch):
    """清扫要等持仓库锁:assemble 只交出函数,自己不调。"""
    called = []
    monkeypatch.setattr(wire, "memory_wiring",
                        lambda repo_root, env, *, deployment: _fake_wiring(repo_root, lambda: called.append(1) or 0))
    asm = assemble(store, FactoryOptions(switches=("agent",)), env={})
    try:
        assert called == [] and callable(asm.sweep)
    finally:
        asm.close()


def test_reconcile_sweeps_after_taking_the_repo_lock_and_before_the_tick(store, monkeypatch):
    order = []
    real_lock = wire.acquire_repo_lock

    def lock(root):
        order.append("lock")
        return real_lock(root)

    monkeypatch.setattr(wire, "acquire_repo_lock", lock)
    monkeypatch.setattr(wire, "memory_wiring",
                        lambda repo_root, env, *, deployment: _fake_wiring(repo_root, lambda: order.append("sweep") or 0))
    monkeypatch.setattr(wire, "reconcile_once", lambda *a, **kw: order.append("tick") or [])
    wire.reconcile_one_tick(str(store.repo_root), FactoryOptions(switches=("agent",)), env={})
    assert order == ["lock", "sweep", "tick"]


def test_a_failing_sweep_closes_the_assembly_and_skips_the_tick(store, monkeypatch):
    closed, ticks = [], []

    def boom():
        raise RuntimeError("sweep exploded")

    monkeypatch.setattr(wire, "memory_wiring", lambda repo_root, env, *, deployment: _fake_wiring(repo_root, boom))
    monkeypatch.setattr(wire, "reconcile_once", lambda *a, **kw: ticks.append(1) or [])
    real_close = wire.Assembly.close
    monkeypatch.setattr(wire.Assembly, "close", lambda self: (closed.append(1), real_close(self)))
    with pytest.raises(RuntimeError, match="sweep exploded"):
        wire.reconcile_one_tick(str(store.repo_root), FactoryOptions(switches=("agent",)), env={})
    assert ticks == [] and closed == [1]


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


def _runs_refusal_env(tmp_path, monkeypatch):
    """装没装 Starling 都走到 runs/ 这一条:把安装检查打桩成通过(它排在 runs/ 检查之前)。"""
    import factory.memory_runtime as mr
    monkeypatch.setattr(mr, "starling_install_source", lambda: "git:abc")
    (tmp_path / "mem").mkdir()
    return _memory_env(tmp_path)


def _assert_refused_before_claiming(code, capsys, tmp_path):
    assert code == 2
    err = capsys.readouterr().err
    assert err.startswith("factory: error: ") and "runs/" in err
    assert str(tmp_path / "mem") not in err and "127.0.0.1" not in err and "offline-test-key" not in err
    assert not (tmp_path / "mem" / ".owner").exists()                # 拒绝先于认领


def test_a_symlinked_runs_dir_is_exit_code_2_at_assembly_and_nothing_behind_it_is_touched(
        store, tmp_path, monkeypatch, capsys):
    """runs/ 是符号链接:启动清扫会跟进去删。装配期就拒绝,而不是等到持锁后的 sweep 抛原始异常(退出码 1、traceback)。"""
    env = _runs_refusal_env(tmp_path, monkeypatch)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep.txt").write_text("precious", encoding="utf-8")
    (tmp_path / "mem" / "runs").symlink_to(elsewhere, target_is_directory=True)
    code = main(["reconcile", str(store.repo_root), "--once", "--enable", "agent"], env=env)
    _assert_refused_before_claiming(code, capsys, tmp_path)
    assert (elsewhere / "keep.txt").read_text(encoding="utf-8") == "precious"


def test_a_runs_entry_that_is_a_plain_file_is_exit_code_2_at_assembly(store, tmp_path, monkeypatch, capsys):
    env = _runs_refusal_env(tmp_path, monkeypatch)
    (tmp_path / "mem" / "runs").write_text("not a directory", encoding="utf-8")
    code = main(["reconcile", str(store.repo_root), "--once", "--enable", "agent"], env=env)
    _assert_refused_before_claiming(code, capsys, tmp_path)
    assert (tmp_path / "mem" / "runs").read_text(encoding="utf-8") == "not a directory"


def test_a_bad_memory_config_is_exit_code_2_even_with_the_stand_in_files_absent(store, tmp_path, capsys):
    """配置有任何问题都在装配期拒绝,不退回替身:这里配置文件不存在。"""
    env = {MEMORY_CONFIG_ENV: str(tmp_path / "missing.yaml")}
    assert main(["reconcile", str(store.repo_root), "--once", "--enable", "agent"], env=env) == 2
    err = capsys.readouterr().err
    assert err.startswith("factory: error: ") and "missing.yaml" in err


def test_a_memory_call_budget_over_four_seconds_is_exit_code_2(store, tmp_path, capsys):
    """I-3:timeout_ms × (max_retries + 1) 超过 4000 毫秒,装配期按配置错误退出,外层时限不再被内层顶穿。"""
    env = _memory_env(tmp_path)
    path = tmp_path / "memory.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data.update(timeout_ms=2001, max_retries=1)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert main(["reconcile", str(store.repo_root), "--once", "--enable", "agent"], env=env) == 2
    err = capsys.readouterr().err
    assert err.startswith("factory: error: ") and "timeout_ms" in err and "max_retries" in err
    assert "2001" not in err and "127.0.0.1" not in err
    assert not (tmp_path / "mem").exists()                           # 拒绝先于认领记忆根目录
