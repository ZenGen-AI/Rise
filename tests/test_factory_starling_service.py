"""拉起模式的服务进程(设计四 Task 11;90 篇拉起模式两张表、FC-41):归属锁、pid 文件、残留服务六项核对、停止与重启退避。

只对本文件自己起的进程发信号:真拉起的 starling-serve 子进程,和持锁、充当残留的 python 或 sleep 子进程。
pid 复用与命令行不符的情形一律用自己起的进程加注入的读取函数造,`signal` 换成 `pytest.fail`,不碰任何别的 pid。
"""
import json, os, signal, subprocess, sys, time
from dataclasses import replace

import pytest

from controlplane.lock import hold_file_lock
from factory.errors import FactoryConfigError
from factory.starling_service import (KILL_WAIT_S, READY_TIMEOUT_S, RESTART_BACKOFF_S, SYSTEM_PROBE, TERM_WAIT_S,
                                      ServiceRestartsExhausted, spawn_service)


def _env(tmp_path):
    return {"PATH": os.environ["PATH"], "OPENAI_API_KEY": "offline-test-key", "STARLING_CONFIG": "/nope"}


def test_the_constants_match_the_design():
    assert (READY_TIMEOUT_S, TERM_WAIT_S, KILL_WAIT_S, RESTART_BACKOFF_S) == (60.0, 40.0, 5.0, (1.0, 4.0, 16.0))


def test_a_spawned_service_is_ready_answers_and_stops_cleanly(tmp_path):
    pytest.importorskip("starling.service"); client = pytest.importorskip("starling_client")
    svc = spawn_service(tmp_path, _env(tmp_path))
    try:
        c = client.connect(svc.url, token=svc.token)
        assert c.info()["protocol"] == client.PROTOCOL
        c.close()
        assert not (tmp_path / "service.token").exists()                  # ready 之后就删
        assert json.loads((tmp_path / "service.pid").read_text())["pid"] == svc.pid
        assert oct((tmp_path / "service.pid").stat().st_mode & 0o777) == "0o600"
        assert svc.token not in repr(svc) and svc.url.startswith("http://127.0.0.1:")
        assert SYSTEM_PROBE.alive(svc.pid) and SYSTEM_PROBE.uid_of(svc.pid) == os.getuid()
    finally:
        svc.close()
    assert not (tmp_path / "service.pid").exists() and not svc.alive()
    with hold_file_lock(tmp_path / "service.owner.lock"):               # 归属锁已释放
        pass
    assert svc.token not in (tmp_path / "service.log").read_text("utf-8", "replace")
    svc.close()                                                          # 幂等


def test_the_child_environment_drops_starling_vars_and_points_home_into_the_data_dir(tmp_path):
    dump = tmp_path / "env.json"
    fake = [sys.executable, "-c", (
        "import json, os, sys, time; json.dump(dict(os.environ), open(sys.argv[1], 'w')); "
        "print('starling-serve listening on http://127.0.0.1:1', flush=True); print('starling-serve ready', flush=True); "
        "time.sleep(60)"), str(dump)]
    svc = spawn_service(tmp_path, _env(tmp_path), command=fake)
    try:
        seen = json.loads(dump.read_text())
    finally:
        svc.close()
    assert "STARLING_CONFIG" not in seen and seen["OPENAI_API_KEY"] == "offline-test-key"
    assert seen["HOME"] == str(tmp_path / "service" / "home")


def test_a_held_ownership_lock_refuses_before_anything_is_spawned(tmp_path):
    other = subprocess.Popen([sys.executable, "-c", (
        "import fcntl, sys, time; f = open(sys.argv[1], 'a+b'); fcntl.flock(f, fcntl.LOCK_EX); "
        "print('held', flush=True); time.sleep(60)"), str(tmp_path / "service.owner.lock")], stdout=subprocess.PIPE, text=True)
    try:
        assert other.stdout.readline().strip() == "held"
        with pytest.raises(FactoryConfigError, match="another rise process"):
            spawn_service(tmp_path, _env(tmp_path))
        assert not (tmp_path / "service.pid").exists() and not (tmp_path / "service").exists()
    finally:
        other.kill(); other.wait()


def test_a_root_with_a_space_is_refused_without_naming_it(tmp_path):
    root = tmp_path / "mem root"
    with pytest.raises(FactoryConfigError, match="space") as info:
        spawn_service(root, _env(tmp_path))
    assert str(root) not in str(info.value) and not (root / "service").exists()
    with hold_file_lock(root / "service.owner.lock"):
        pass


def _holder(path):
    """一个持着 `path` 上 flock 的子进程(本测试自己起的)。"""
    proc = subprocess.Popen([sys.executable, "-c", (
        "import fcntl, sys, time; f = open(sys.argv[1], 'a+b'); fcntl.flock(f, fcntl.LOCK_EX); "
        "print('held', flush=True); time.sleep(120)"), str(path)], stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "held"
    return proc


def _residual(tmp_path, argv_tail):
    """一个持着 D/.lock 的无关进程,service.pid 记着它;返回 (进程, 记下的 argv)。"""
    d = tmp_path / "service"; d.mkdir(parents=True, exist_ok=True)
    proc = _holder(d / ".lock")
    argv = [sys.executable, "-m", "starling.service", "--data-dir", str(d), *argv_tail]
    (tmp_path / "service.pid").write_text(json.dumps({"pid": proc.pid, "argv": argv}))
    return proc, argv


@pytest.mark.parametrize("seen, item", [
    (lambda argv: "/usr/bin/python3 -m starling.service --data-dir /elsewhere --port 0", "command line"),   # 别的 D
    (lambda argv: "/usr/bin/python3 -m starling.service --data-dir", "command line"),                     # 被截断
    (lambda argv: "/bin/sleep 120", "command line"),                                                       # pid 被复用
    (lambda argv: None, "command line"),                                                                   # 读不到
    (lambda argv: ["/bin/sleep", *argv[1:]], "command line"),                                              # 列表:解释器不是 python
    (lambda argv: [argv[0], *argv[1:-1]], "command line"),                                                 # 列表:少一项
])
def test_a_residual_that_does_not_match_is_refused_and_left_running(tmp_path, seen, item):
    pytest.importorskip("starling.service")
    proc, argv = _residual(tmp_path, ["--host", "127.0.0.1", "--port", "0", "--token-file", "t"])
    probe = replace(SYSTEM_PROBE, command_of=lambda pid: seen(argv),
                    signal=lambda pid, sig: pytest.fail("must not signal"))
    try:
        with pytest.raises(FactoryConfigError, match=item) as info:
            spawn_service(tmp_path, _env(tmp_path), probe=probe)
        assert str(proc.pid) in str(info.value) and "service.log" in str(info.value)
        assert str(tmp_path) not in str(info.value)
        assert proc.poll() is None                                       # 没被杀
        assert json.loads((tmp_path / "service.pid").read_text())["pid"] == proc.pid   # 残留的 pid 文件不动
    finally:
        proc.kill(); proc.wait()
    with hold_file_lock(tmp_path / "service.owner.lock"):               # 拒绝之后归属锁也放了
        pass


def test_a_recorded_argv_that_is_not_this_services_command_is_refused(tmp_path):
    pytest.importorskip("starling.service")
    d = tmp_path / "service"; d.mkdir()
    proc = _holder(d / ".lock")
    argv = [sys.executable, "-c", "pass", "--data-dir", str(d)]                       # 没有 -m starling.service
    (tmp_path / "service.pid").write_text(json.dumps({"pid": proc.pid, "argv": argv}))
    probe = replace(SYSTEM_PROBE, command_of=lambda pid: argv, signal=lambda pid, sig: pytest.fail("no"))
    try:
        with pytest.raises(FactoryConfigError, match="command line"):
            spawn_service(tmp_path, _env(tmp_path), probe=probe)
        assert proc.poll() is None
    finally:
        proc.kill(); proc.wait()


def test_a_real_pid_reuse_is_caught_by_the_real_command_line_reader(tmp_path):
    """pid 文件记着本测试起的一个 sleep(pid 被一个无关进程复用);真的 ps 或 /proc 读出它的命令行,对不上就拒绝。"""
    pytest.importorskip("starling.service")
    d = tmp_path / "service"; d.mkdir()
    holder = _holder(d / ".lock")
    sleeper = subprocess.Popen(["sleep", "120"])
    argv = [sys.executable, "-m", "starling.service", "--data-dir", str(d), "--host", "127.0.0.1", "--port", "0"]
    (tmp_path / "service.pid").write_text(json.dumps({"pid": sleeper.pid, "argv": argv}))
    probe = replace(SYSTEM_PROBE, signal=lambda pid, sig: pytest.fail("must not signal"))
    try:
        with pytest.raises(FactoryConfigError, match="command line") as info:
            spawn_service(tmp_path, _env(tmp_path), probe=probe)
        assert str(sleeper.pid) in str(info.value)
        assert sleeper.poll() is None and holder.poll() is None
    finally:
        for p in (sleeper, holder):
            p.kill(); p.wait()


@pytest.mark.parametrize("content", [None, "not json", json.dumps({"pid": "1", "argv": []}),
                                     json.dumps({"pid": 5, "argv": "python"}), json.dumps({"pid": True, "argv": ["x"]})])
def test_a_missing_or_malformed_pid_file_is_refused(tmp_path, content):
    pytest.importorskip("starling.service")
    d = tmp_path / "service"; d.mkdir()
    proc = _holder(d / ".lock")
    if content is not None:
        (tmp_path / "service.pid").write_text(content)
    probe = replace(SYSTEM_PROBE, alive=lambda pid: pytest.fail("no probe"), uid_of=lambda pid: pytest.fail("no probe"),
                    command_of=lambda pid: pytest.fail("no probe"), signal=lambda pid, sig: pytest.fail("no"))
    try:
        with pytest.raises(FactoryConfigError, match="pid file") as info:
            spawn_service(tmp_path, _env(tmp_path), probe=probe)
        assert "service.log" in str(info.value) and proc.poll() is None
    finally:
        proc.kill(); proc.wait()


def test_a_residual_pid_that_is_not_alive_is_refused(tmp_path):
    pytest.importorskip("starling.service")
    proc, argv = _residual(tmp_path, [])
    probe = replace(SYSTEM_PROBE, alive=lambda pid: False, signal=lambda pid, sig: pytest.fail("no"))
    try:
        with pytest.raises(FactoryConfigError, match="alive") as info:
            spawn_service(tmp_path, _env(tmp_path), probe=probe)
        assert str(proc.pid) in str(info.value) and proc.poll() is None
    finally:
        proc.kill(); proc.wait()


def test_a_residual_owned_by_another_user_is_refused(tmp_path):
    pytest.importorskip("starling.service")
    proc, argv = _residual(tmp_path, [])
    probe = replace(SYSTEM_PROBE, uid_of=lambda pid: os.getuid() + 1, signal=lambda pid, sig: pytest.fail("no"))
    try:
        with pytest.raises(FactoryConfigError, match="owner"):
            spawn_service(tmp_path, _env(tmp_path), probe=probe)
        assert proc.poll() is None
    finally:
        proc.kill(); proc.wait()


def test_a_residual_that_matches_every_check_is_stopped_and_the_start_is_retried(tmp_path):
    pytest.importorskip("starling.service")
    proc, argv = _residual(tmp_path, [])
    probe = replace(SYSTEM_PROBE, command_of=lambda pid: argv if pid == proc.pid else SYSTEM_PROBE.command_of(pid))
    try:
        svc = spawn_service(tmp_path, _env(tmp_path), probe=probe)
    finally:
        if proc.poll() is None:
            proc.kill()
    try:
        assert proc.wait(timeout=45) is not None and svc.alive()        # 残留被 SIGTERM 停掉,新服务起来
        assert json.loads((tmp_path / "service.pid").read_text())["pid"] == svc.pid
    finally:
        svc.close()


def test_a_real_residual_left_by_a_crashed_rise_is_stopped_with_the_real_probe(tmp_path):
    """上一个 rise 崩了:它拉起的服务还活着、service.pid 还在,归属锁随进程放了。真 ps 读出的命令行要对得上记下的 argv。"""
    pytest.importorskip("starling.service")
    old = spawn_service(tmp_path, _env(tmp_path))
    old_proc = old._proc
    old._owner.close()                                                   # 模拟崩溃:只放归属锁,服务与 pid 文件留着
    try:
        new = spawn_service(tmp_path, _env(tmp_path))
        try:
            assert old_proc.wait(timeout=45) == 0                       # SIGTERM 后服务自己排空退出
            assert new.alive() and new.pid != old_proc.pid
        finally:
            new.close()
    finally:
        if old_proc.poll() is None:
            old_proc.kill(); old_proc.wait()


def test_a_data_dir_lock_that_is_no_longer_held_means_the_residual_already_left(tmp_path):
    pytest.importorskip("starling.service")
    proc, argv = _residual(tmp_path, [])
    signals = []

    def uid_then_leave(pid):
        proc.kill(); proc.wait()                                         # 核对途中残留自己退了
        return os.getuid()

    probe = replace(SYSTEM_PROBE, command_of=lambda pid: argv, uid_of=uid_then_leave,
                    alive=lambda pid: True, signal=lambda pid, sig: signals.append(sig))
    svc = spawn_service(tmp_path, _env(tmp_path), probe=probe)
    svc.close()
    assert signals == []                                                 # 不发信号,直接重试启动


def test_a_child_that_fails_to_start_exits_2_naming_only_the_log(tmp_path):
    boom = [sys.executable, "-c", "import sys; sys.exit(1)"]
    with pytest.raises(FactoryConfigError, match="code 1") as info:
        spawn_service(tmp_path, _env(tmp_path), command=boom)
    assert "service.log" in str(info.value) and str(tmp_path) not in str(info.value)
    assert not (tmp_path / "service.token").exists() and not (tmp_path / "service.pid").exists()


def test_no_ready_line_within_the_limit_kills_the_child_and_exits_2(tmp_path):
    hang = [sys.executable, "-c", "print('starling-serve listening on http://127.0.0.1:1', flush=True); import time; time.sleep(60)"]
    with pytest.raises(FactoryConfigError, match="not ready") as info:
        spawn_service(tmp_path, _env(tmp_path), command=hang, ready_timeout_s=0.5)
    assert "service.log" in str(info.value) and str(tmp_path) not in str(info.value)
    assert not (tmp_path / "service.token").exists() and not (tmp_path / "service.pid").exists()
    with hold_file_lock(tmp_path / "service.owner.lock"):
        pass


def test_restarts_back_off_and_stop_after_three(tmp_path):
    pytest.importorskip("starling.service")
    slept = []
    svc = spawn_service(tmp_path, _env(tmp_path), sleep=slept.append)
    try:
        first = svc.pid
        svc.restart()
        assert slept == [] and svc.pid == first                         # 活着时什么都不做
        for _ in range(3):
            os.kill(svc.pid, signal.SIGKILL); time.sleep(0.2)
            svc.restart()
            assert svc.alive()
        os.kill(svc.pid, signal.SIGKILL); time.sleep(0.2)
        with pytest.raises(ServiceRestartsExhausted):
            svc.restart()
        assert slept == [1.0, 4.0, 16.0]
    finally:
        svc.close()
