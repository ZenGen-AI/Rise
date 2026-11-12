"""本机到远端回环端口的 SSH 隧道,和服务健康探针(设计 DX-13 到 DX-15)。

SshTransport 是一问一答的:每次调用起一个 ssh、拿回输出就结束。隧道是一个要一直活着的 ssh 进程,
性质不同,所以单独成类,不塞进 Transport 协议。别名的形态校验复用传输层的那一条,不另抄一份。
"""
from __future__ import annotations

import logging
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from compute.errors import ComputeError
from compute.transport import _ALIAS

if TYPE_CHECKING:
    from compute.client import ComputeClient

_log = logging.getLogger(__name__)


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

    进入之后有一个看守线程:ssh 进程退出(主机拒绝 SSH、网络断了一阵)就在**同一个本地端口**上重新拉起,
    失败按 1、2、4…至多 `restart_backoff_max_s` 秒退避重试,直到退出上下文。端口不变,所以调用方手里的
    base_url 一直有效,不用重建会话。断开期间的请求照旧连不上、按各自的失败分类处理(真机 2026-11-12:161 拒绝
    SSH 约 80 秒,隧道没再起来,之后 25 分钟的对手请求全部连不上,整条 rollout 判延后)。
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
        watch_s: float = 2.0,
        restart_backoff_max_s: float = 30.0,
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
        self._watch = watch_s
        self._backoff_max = restart_backoff_max_s
        self._spawn = spawn
        self._port_ready = port_ready
        self._proc: Any = None
        self._lock = threading.Lock()       # 护 _proc:看守线程换进程、退出时取进程,两边不能交错
        self._stopping = threading.Event()
        self._watcher: threading.Thread | None = None
        self.restarts = 0                   # 看守线程成功重启的次数;只给日志与测试看
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
        try:
            self._proc = self._open()
        except BaseException:
            self._terminate()
            raise
        self._watcher = threading.Thread(target=self._watch_loop, name="ssh-tunnel-watch", daemon=True)
        self._watcher.start()
        return self

    def __exit__(self, *exc: object) -> bool:
        self._terminate()
        return False

    def _open(self) -> Any:
        """起一个 ssh,等本地端口能连上后返回进程;起不来就终止它并抛 OSError。进入与看守线程重启共用。"""
        proc = self._spawn(self.argv())
        deadline = time.monotonic() + self._open_timeout
        try:
            while True:
                if proc.poll() is not None:
                    raise OSError(f"the ssh tunnel process exited with code {proc.poll()}")
                if self._port_ready(self.local_port):
                    return proc
                if time.monotonic() >= deadline:
                    raise OSError(f"the tunnel's local port did not accept connections within {self._open_timeout:g}s")
                if self._stopping.wait(self._poll):
                    raise OSError("the tunnel is closing")
        except BaseException:
            self._stop_proc(proc)
            raise

    def _watch_loop(self) -> None:
        """ssh 退出就在同一个本地端口上重启,失败按指数退避一直试,直到退出上下文。

        日志只写远端端口与退出码或异常类名,不写别名:别名里有用户名与主机地址。"""
        failures = 0
        while not self._stopping.wait(self._watch):
            with self._lock:
                proc = self._proc
            if proc is None or proc.poll() is None:
                continue
            _log.warning("ssh tunnel to remote port %d exited (code %s); restarting on local port %d",
                         self._remote_port, proc.poll(), self.local_port)
            while not self._stopping.is_set():
                try:
                    fresh = self._open()
                except OSError as exc:
                    failures += 1
                    delay = min(2.0 ** (failures - 1), self._backoff_max)
                    _log.warning("ssh tunnel restart failed (%s); retrying in %gs", type(exc).__name__, delay)
                    if self._stopping.wait(delay):
                        return
                    continue
                with self._lock:
                    if self._stopping.is_set():      # 正在退出:新起的这个不交出去,自己关掉
                        self._stop_proc(fresh)
                        return
                    self._proc = fresh
                failures = 0
                self.restarts += 1
                _log.warning("ssh tunnel to remote port %d restored", self._remote_port)
                break

    def _terminate(self) -> None:
        self._stopping.set()
        watcher, self._watcher = self._watcher, None
        if watcher is not None:
            watcher.join(timeout=self._open_timeout + self._close_grace)
        with self._lock:
            proc, self._proc = self._proc, None
        self._stop_proc(proc)

    def _stop_proc(self, proc: Any) -> None:
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=self._close_grace)
        except (subprocess.TimeoutExpired, TimeoutError):
            proc.kill()
            proc.wait(timeout=self._close_grace)


def tunnel_factory_for(client: ComputeClient, remote_port: int) -> Callable[[], SshTunnel]:
    """从计算客户端取 ssh 别名,造"每次调用开一条到 remote_port 的隧道"的工厂。

    推理服务的隧道用的是客户端传输层的同一个别名(ComputeClient.alias);别名只在传输层与本机映射里,不进 git 树。
    客户端不带别名(没有这个属性、为 None 或为空)时立刻抛 ComputeError:此时还没占租约、没起任何东西。
    起服务(vllm_policy.policy_context)与评测(sombench_runner)共用这一处。"""
    alias = getattr(client, "alias", None)
    if not alias:
        raise ComputeError("the compute client does not expose its ssh alias for the tunnel")
    return lambda: SshTunnel(alias, remote_port=remote_port)
