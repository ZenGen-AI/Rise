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
