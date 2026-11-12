"""SSH 隧道与健康探针:能在本机真跑的部分真跑(真 socket、真 HTTP 服务器),ssh 本身用替身。"""
from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

from compute.errors import ComputeError
from compute.tunnel import SshTunnel, free_local_port, http_probe, tunnel_factory_for


@pytest.fixture
def serve():
    """起测试 HTTP 服务器,测试结束时一定停循环并关监听 socket(只 shutdown 不 server_close 会泄漏 fd,-W error 下是失败)。"""
    started = []

    def make(status=200, require_key=None):
        seen = []

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, self.headers.get("Authorization")))
                ok = require_key is None or self.headers.get("Authorization") == f"Bearer {require_key}"
                self.send_response(status if ok else 401)
                self.end_headers()

            def log_message(self, *a):
                pass

        server = HTTPServer(("127.0.0.1", 0), H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        started.append((server, thread))
        return server, seen

    yield make
    for server, thread in started:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_the_probe_is_true_on_200_and_sends_the_key_as_a_bearer_token(serve):
    server, seen = serve(require_key="k1")
    assert http_probe(f"http://127.0.0.1:{server.server_port}/health", "k1") is True
    assert seen[0] == ("/health", "Bearer k1")


def test_the_probe_is_false_on_a_non_200(serve):
    """非 200 确实会抛 HTTPError,而实现要把它当 False、不当错;测试要验证非 200 这个定义域的边界。"""
    server, _ = serve(status=503)
    assert http_probe(f"http://127.0.0.1:{server.server_port}/health", "k") is False


def test_the_probe_is_false_when_the_wrong_key_is_rejected(serve):
    server, _ = serve(require_key="right")
    assert http_probe(f"http://127.0.0.1:{server.server_port}/health", "wrong") is False


def test_the_probe_is_false_when_nothing_listens_instead_of_raising():
    """服务还没起来时端口是关的:这是「还没好」,不是错误,轮询要继续。"""
    port = free_local_port()
    assert http_probe(f"http://127.0.0.1:{port}/health", "k") is False


def test_a_free_local_port_can_actually_be_bound():
    port = free_local_port()
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))


class FakeProc:
    def __init__(self, *, dies_at_once=False):
        self.terminated = self.killed = False
        self._rc = 255 if dies_at_once else None
        self.stderr = None

    def poll(self):
        return self._rc

    def terminate(self):
        self.terminated = True
        self._rc = -15

    def kill(self):
        self.killed = True
        self._rc = -9

    def wait(self, timeout=None):
        return self._rc


def _tunnel(spawn, **kw):
    return SshTunnel("guoruijie@172.16.120.211", remote_port=18081, spawn=spawn, port_ready=kw.pop("port_ready", lambda p: True), **kw)


def test_the_ssh_command_forwards_a_loopback_port_and_runs_nothing_remotely():
    captured = {}

    def spawn(argv):
        captured["argv"] = argv
        return FakeProc()

    with _tunnel(spawn) as t:
        argv = captured["argv"]
        assert argv[0] == "ssh" and "-N" in argv                         # 不执行远端命令
        assert "BatchMode=yes" in argv and "ExitOnForwardFailure=yes" in argv
        forward = argv[argv.index("-L") + 1]
        assert forward == f"127.0.0.1:{t.local_port}:127.0.0.1:18081"    # 两端都只绑回环
        assert "--" in argv and argv[argv.index("--") + 1] == "guoruijie@172.16.120.211"


def test_closing_terminates_the_ssh_process():
    proc = FakeProc()
    with _tunnel(lambda argv: proc):
        pass
    assert proc.terminated


def test_a_process_that_will_not_terminate_is_killed():
    class Stubborn(FakeProc):
        def terminate(self):
            self.terminated = True      # 不理会

        def wait(self, timeout=None):
            if not self.killed:
                raise TimeoutError("still running")
            return -9

    proc = Stubborn()
    with _tunnel(lambda argv: proc, close_grace_s=0.01):
        pass
    assert proc.killed


def test_an_ssh_that_dies_at_once_is_an_error_not_a_silent_tunnel_to_nowhere():
    with pytest.raises(OSError, match="exited"):
        with _tunnel(lambda argv: FakeProc(dies_at_once=True)):
            pass


def _wait_until(cond, timeout=5.0):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_an_ssh_that_dies_mid_session_is_restarted_on_the_same_local_port():
    """真机 2026-11-12:161 拒绝 SSH 约 80 秒,隧道进程退出后没再起来,之后对手请求全部连不上。
    看守线程要在同一个本地端口上重新拉起 ssh,调用方手里的 base_url 不用变。"""
    spawned = []

    def spawn(argv):
        spawned.append(list(argv))
        return FakeProc()

    with _tunnel(spawn, watch_s=0.01) as t:
        port = t.local_port
        spawned_first = spawned[0]
        t._proc._rc = 255                                         # ssh 中途退出
        assert _wait_until(lambda: t.restarts == 1)
        assert len(spawned) == 2 and spawned[1] == spawned_first  # 同一条命令,同一个本地端口
        assert t.local_port == port and t._proc.poll() is None


def test_a_restart_that_keeps_failing_backs_off_and_succeeds_once_ssh_is_back():
    """主机拒绝 SSH 时重启一直失败:按退避重试,不放弃;主机恢复后接上。"""
    attempts = []
    host_up = {"v": True}

    def spawn(argv):
        attempts.append(1)
        return FakeProc(dies_at_once=not host_up["v"])

    with _tunnel(spawn, watch_s=0.01, restart_backoff_max_s=0.02) as t:
        host_up["v"] = False
        t._proc._rc = 255
        assert _wait_until(lambda: len(attempts) >= 4)            # 起了几次都立刻退出,还在试
        assert t.restarts == 0
        host_up["v"] = True
        assert _wait_until(lambda: t.restarts == 1)
        assert t._proc.poll() is None


def test_closing_stops_the_watcher_and_the_restarted_ssh():
    procs = []

    def spawn(argv):
        procs.append(FakeProc())
        return procs[-1]

    with _tunnel(spawn, watch_s=0.01) as t:
        t._proc._rc = 255
        assert _wait_until(lambda: t.restarts == 1)
        watcher = t._watcher
    assert not watcher.is_alive()
    assert procs[-1].terminated                                   # 关掉的是重启后的那一个
    count = len(procs)
    import time
    time.sleep(0.05)
    assert len(procs) == count                                    # 退出之后不再重启


def test_a_tunnel_whose_port_never_opens_is_an_error_and_is_cleaned_up():
    proc = FakeProc()
    with pytest.raises(OSError, match="not accept"):
        with _tunnel(lambda argv: proc, port_ready=lambda p: False, open_timeout_s=0.05, poll_s=0.01):
            pass
    assert proc.terminated


@pytest.mark.parametrize("alias", ["-oProxyCommand=touch /tmp/x", "a b", "has'quote", ""])
def test_a_malformed_alias_is_refused_before_anything_is_spawned(alias):
    """别名会进 ssh 的 argv;以 `-` 开头的会被 ssh 当选项读,ProxyCommand 能执行任意命令。"""
    spawned = []
    with pytest.raises(ComputeError):
        SshTunnel(alias, remote_port=18081, spawn=lambda argv: spawned.append(argv))
    assert spawned == []


# ---------------- tunnel_factory_for:从客户端取别名造隧道 ----------------

def test_tunnel_factory_for_builds_an_ssh_tunnel_from_the_clients_alias_and_the_given_port():
    factory = tunnel_factory_for(SimpleNamespace(alias="gpu-181"), 18081)
    tunnel = factory()
    assert isinstance(tunnel, SshTunnel) and factory() is not tunnel          # 每次调用一个新隧道
    argv = tunnel.argv()
    assert argv[-1] == "gpu-181" and "127.0.0.1:0:127.0.0.1:18081" in argv


@pytest.mark.parametrize("client", [SimpleNamespace(), SimpleNamespace(alias=None), SimpleNamespace(alias="")])
def test_tunnel_factory_for_refuses_a_client_that_hides_its_alias_before_anything_is_opened(client):
    with pytest.raises(ComputeError, match="alias"):
        tunnel_factory_for(client, 18081)
