"""本机到远端回环端口的 SSH 隧道,和服务健康探针(设计 DX-13 到 DX-15)。

SshTransport 是一问一答的:每次调用起一个 ssh、拿回输出就结束。隧道是一个要一直活着的 ssh 进程,
性质不同,所以单独成类,不塞进 Transport 协议。别名的形态校验复用传输层的那一条,不另抄一份。
"""
from __future__ import annotations

import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from typing import Any

from compute.errors import ComputeError
from compute.transport import _ALIAS


def free_local_port() -> int:
    """挑一个当前空闲的本地端口。关掉再用之间有极小的竞争窗口,ssh 绑定失败时会以非零退出(见 ExitOnForwardFailure)。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def port_accepts(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


def http_probe(url: str, api_key: str) -> bool:
    """健康检查。服务没起来时端口是关的,那是「还没好」不是错误,所以任何连接层的失败都返回 False。

    非 200 时 urllib 抛 HTTPError,而它自己持有一个打开的响应,必须关掉:服务起来之前守护进程会
    反复轮询,每轮漏一个就是一个泄漏的 socket。"""
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(request, timeout=5.0) as response:
            return response.status == 200
    except urllib.error.HTTPError as error:
        error.close()
        return False
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _spawn(argv: Sequence[str]) -> subprocess.Popen:
    return subprocess.Popen(list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)


class SshTunnel:
    """上下文管理器。进入时起 `ssh -N -L`,等本地端口真能连上;退出时终止 ssh,不听话就强杀。

    只转发回环到回环:本地端只绑 127.0.0.1(不对局域网开放),远端端只连远端的 127.0.0.1。
    """

    def __init__(
        self,
        alias: str,
        *,
        remote_port: int,
        connect_timeout_s: float = 10.0,
        open_timeout_s: float = 30.0,
        close_grace_s: float = 5.0,
        poll_s: float = 0.25,
        spawn: Callable[[Sequence[str]], Any] = _spawn,
        port_ready: Callable[[int], bool] = port_accepts,
    ) -> None:
        if not isinstance(alias, str) or not _ALIAS.match(alias):
            raise ComputeError("the ssh alias has an invalid form")
        self._alias = alias
        self._remote_port = remote_port
        self._connect_timeout = max(1, int(connect_timeout_s))
        self._open_timeout = open_timeout_s
        self._close_grace = close_grace_s
        self._poll = poll_s
        self._spawn = spawn
        self._port_ready = port_ready
        self._proc: Any = None
        self.local_port = 0

    def argv(self) -> list[str]:
        return [
            "ssh", "-N", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={self._connect_timeout}",
            "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=15",
            "-L", f"127.0.0.1:{self.local_port}:127.0.0.1:{self._remote_port}",
            "--", self._alias,
        ]

    def __enter__(self) -> "SshTunnel":
        self.local_port = free_local_port()
        self._proc = self._spawn(self.argv())
        deadline = time.monotonic() + self._open_timeout
        try:
            while True:
                if self._proc.poll() is not None:
                    raise OSError(f"the ssh tunnel process exited with code {self._proc.poll()}")
                if self._port_ready(self.local_port):
                    return self
                if time.monotonic() >= deadline:
                    raise OSError(f"the tunnel's local port did not accept connections within {self._open_timeout:g}s")
                time.sleep(self._poll)
        except BaseException:
            self._terminate()
            raise

    def __exit__(self, *exc: object) -> bool:
        self._terminate()
        return False

    def _terminate(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=self._close_grace)
        except (subprocess.TimeoutExpired, TimeoutError):
            proc.kill()
            proc.wait(timeout=self._close_grace)
