"""拉起模式的记忆服务进程(设计四;90 篇拉起模式两张表、FC-41)。

`url` 与 `token_file` 都不给时,rise 用自己的解释器拉起 `python -m starling.service`(数据目录 `D = <root>/service`),
只给测试与没有看板的机器用。外部模式下 rise 绝不碰服务进程。

- 归属锁:拉起之前对 `<root>/service.owner.lock` 取独占非阻塞锁,持到 `close()`;被占就是另一个 rise 进程在用拉起的服务。
- token:每次启动新生成,写临时文件(0600)再改名成 `<root>/service.token`,看到 ready 后删;不进命令行、日志、异常与 repr。
- pid 文件:`<root>/service.pid`(JSON:`pid` 与完整 `argv`),看到 listening 行才写:服务先取 `D/.lock` 再绑端口
  (`S:python/starling/service/serve.py:103-108`),看到 listening 说明新进程已持锁;更早写会把残留服务的 pid 文件盖掉。
- 残留服务:子进程以退出码 3(`data_dir_in_use`)退出时按序六项核对,任一项不过就拒绝启动、不发任何信号;
  全部通过才 SIGTERM,等 `D/.lock` 释放,再重试启动一次。第 6 项只证明有人持锁(macOS 没有按进程查 flock 的接口),
  第 5 项证明命令行就是 rise 为这个 D 拉起的那条,两项合起来防 pid 复用时误杀。

本模块不导入 `starling` 与 `starling_client`:服务是子进程,就绪靠读 stdout。失败文字只写相对名 `service.log`,
不写 token 与绝对路径。
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import re
import secrets
import signal as _signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from controlplane.lock import FileLockBusyError, hold_file_lock
from factory.errors import FactoryConfigError

_log = logging.getLogger(__name__)

READY_TIMEOUT_S = 60.0
# 服务排空 30 秒(S:python/starling/service/serve.py:17-18),加 10 秒余量
TERM_WAIT_S = 40.0
KILL_WAIT_S = 5.0
RESTART_BACKOFF_S = (1.0, 4.0, 16.0)
_LOCK_POLL_S = 0.2
_PROBE_TIMEOUT_S = 5
EXIT_DATA_DIR_IN_USE = 3                 # S:python/starling/service/serve.py:115-120

OWNER_LOCK = "service.owner.lock"
DATA_DIR = "service"
TOKEN_FILE = "service.token"
PID_FILE = "service.pid"
LOG_FILE = "service.log"
_LISTENING = re.compile(r"starling-serve listening on http://127\.0\.0\.1:(\d+)")
_READY = "starling-serve ready"


# ---------------- 读别的进程:属主与命令行 ----------------

def _run(argv: list[str]) -> str | None:
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:              # 进程在,只是不归我们
        return True
    return True


def _uid_of(pid: int) -> int | None:
    if sys.platform.startswith("linux"):
        try:
            return os.stat(f"/proc/{pid}").st_uid
        except OSError:
            return None
    text = _run(["ps", "-o", "uid=", "-p", str(pid)])
    try:
        return int(text.strip()) if text and text.strip() else None
    except ValueError:
        return None


def _command_of(pid: int) -> str | list[str] | None:
    if sys.platform.startswith("linux"):
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return None
        parts = raw.split(b"\0")
        if parts and parts[-1] == b"":
            parts = parts[:-1]
        return [p.decode("utf-8", "surrogateescape") for p in parts] or None
    # 不加 -ww 时 ps 按终端宽度截断
    text = _run(["ps", "-ww", "-o", "command=", "-p", str(pid)])
    text = text.rstrip("\n") if text else ""
    return text or None


@dataclass(frozen=True)
class ProcessProbe:
    """读别的进程的属主与命令行;测试注入假实现。"""

    alive: Callable[[int], bool]                          # os.kill(pid, 0),ProcessLookupError 为假
    uid_of: Callable[[int], int | None]                   # macOS: ps -o uid= -p;Linux: os.stat(f"/proc/{pid}").st_uid
    command_of: Callable[[int], str | list[str] | None]   # macOS: ps -ww -o command= -p(字符串);Linux: cmdline 按 NUL 拆
    signal: Callable[[int, int], None]                    # os.kill


SYSTEM_PROBE = ProcessProbe(alive=_alive, uid_of=_uid_of, command_of=_command_of, signal=os.kill)


class ServiceRestartsExhausted(Exception):
    """拉起的服务在进程生命周期内已重启 3 次,第 4 次不再拉起。"""


class _Residual(Exception):
    """残留核对的一项没过;`item` 是那一项的名字,`pid` 读得出时给出。"""

    def __init__(self, item: str, pid: int | None) -> None:
        super().__init__(item)
        self.item, self.pid = item, pid


def _python_name(first: str) -> bool:
    # 不比解释器路径:macOS 上 venv 的 python 会换成框架里的 Python.app/Contents/MacOS/Python
    return Path(first).name.lower().startswith("python")


def _contains(argv: Sequence[str], pair: tuple[str, str]) -> bool:
    return any(tuple(argv[i:i + 2]) == pair for i in range(len(argv) - 1))


def _command_matches(seen: str | list[str] | None, argv: list[str], data_dir: str) -> bool:
    """第 5 项:命令行除第一项外等于记下的 `argv[1:]`,第一项的文件名以 python 开头;记下的 argv 是本 D 的服务命令。"""
    if not (_contains(argv, ("-m", "starling.service")) and _contains(argv, ("--data-dir", data_dir))):
        return False
    if isinstance(seen, list):
        return len(seen) >= 1 and seen[1:] == argv[1:] and _python_name(seen[0])
    if isinstance(seen, str):
        tail = " " + " ".join(argv[1:])
        if not seen.endswith(tail):
            return False
        head = seen[:-len(tail)]
        return bool(head) and _python_name(head)
    return False


def _read_pid_file(path: Path) -> tuple[int, list[str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise _Residual("pid file", None) from None
    pid = data.get("pid") if isinstance(data, dict) else None
    argv = data.get("argv") if isinstance(data, dict) else None
    if (isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 or not isinstance(argv, list) or not argv
            or not all(isinstance(a, str) for a in argv)):
        raise _Residual("pid file", pid if isinstance(pid, int) and not isinstance(pid, bool) else None)
    return pid, argv


def _write_private(path: Path, text: str) -> None:
    """临时文件(0600)加改名:读者要么看不到,要么看到完整内容。"""
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _unlink(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


def _lock_is_free(path: Path) -> bool:
    try:
        with hold_file_lock(path):
            return True
    except FileLockBusyError:
        return False


class SpawnedService:
    """rise 拉起的一个服务进程。`url`、`token`、`pid` 随重启而变;token 只在内存里,不进 repr、日志与异常。"""

    url: str
    token: str

    def __init__(self, root: Path, env: Mapping[str, str], *, python: str, probe: ProcessProbe,
                 ready_timeout_s: float, sleep: Callable[[float], None], command: Sequence[str] | None) -> None:
        self._root = root
        self._data_dir = root / DATA_DIR
        self._python, self._probe, self._ready_timeout_s, self._sleep = python, probe, ready_timeout_s, sleep
        self._command = list(command) if command is not None else None
        self._env = {k: v for k, v in env.items() if not k.startswith("STARLING_")}
        self._env["HOME"] = str(self._data_dir / "home")
        self._owner = contextlib.ExitStack()
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._restarts = 0
        self._closed = False
        self.url, self.token = "", ""

    def __repr__(self) -> str:
        return f"SpawnedService(url={self.url!r}, pid={self.pid})"

    @property
    def pid(self) -> int:
        return self._proc.pid if self._proc is not None else 0

    # ---- 拉起 ----

    def _argv(self) -> list[str]:
        if self._command is not None:
            return list(self._command)
        return [self._python, "-m", "starling.service", "--data-dir", str(self._data_dir),
                "--host", "127.0.0.1", "--port", "0", "--token-file", str(self._root / TOKEN_FILE)]

    def _acquire(self) -> None:
        try:
            self._owner.enter_context(hold_file_lock(self._root / OWNER_LOCK))
        except FileLockBusyError:
            raise FactoryConfigError("another rise process is using the spawned memory service") from None
        if " " in str(self._data_dir):
            raise FactoryConfigError("memory root path contains a space; the spawned memory service cannot use it")

    def _start(self) -> None:
        """拉起一次;退出码 3 时走残留核对,至多重试一次。"""
        for attempt in range(2):
            code = self._launch()
            if code is None:
                return
            if code != EXIT_DATA_DIR_IN_USE:
                raise FactoryConfigError(f"memory service exited with code {code} before it was ready; see {LOG_FILE}")
            if attempt == 1:
                raise FactoryConfigError(
                    f"memory service data dir is still in use after the residual service left; see {LOG_FILE}")
            self._clear_residual()
        raise AssertionError("unreachable")

    def _launch(self) -> int | None:
        """起一个子进程,等 listening 与 ready。就绪返回 None;就绪前退出返回退出码;超时杀掉子进程后抛。"""
        token_path, pid_path = self._root / TOKEN_FILE, self._root / PID_FILE
        _unlink(token_path)
        self.token = secrets.token_urlsafe(32)
        _write_private(token_path, self.token + "\n")
        argv = self._argv()
        lines: queue.Queue = queue.Queue()
        pid_written = False
        try:
            with open(self._root / LOG_FILE, "ab") as log:
                proc = subprocess.Popen(argv, env=self._env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=log, close_fds=True, text=True, cwd=str(self._root))
            self._proc = proc
            self._reader = threading.Thread(target=_drain, args=(proc, lines), name="starling-service-stdout",
                                            daemon=True)
            self._reader.start()
            deadline = time.monotonic() + self._ready_timeout_s
            port = None
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._kill_child()
                    raise FactoryConfigError(
                        f"memory service not ready within {self._ready_timeout_s:g} s; see {LOG_FILE}")
                try:
                    line = lines.get(timeout=remaining)
                except queue.Empty:
                    continue
                if line is None:                                   # stdout 关了:子进程退出
                    return proc.wait()
                if port is None:
                    match = _LISTENING.search(line)
                    if match:
                        port = int(match.group(1))
                        _write_private(pid_path, json.dumps({"pid": proc.pid, "argv": argv}))
                        pid_written = True
                elif _READY in line:
                    self.url = f"http://127.0.0.1:{port}"
                    pid_written = False                            # 就绪:pid 文件留着,close() 删
                    return None
        finally:
            _unlink(token_path)                                    # 服务只在启动时读一次
            if pid_written:
                _unlink(pid_path)

    def _kill_child(self) -> None:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(KILL_WAIT_S)
        self._join_reader()

    def _join_reader(self) -> None:
        if self._reader is not None:
            self._reader.join(KILL_WAIT_S)
            self._reader = None
        if self._proc is not None and self._proc.stdout is not None:
            with contextlib.suppress(OSError):
                self._proc.stdout.close()

    # ---- 残留服务 ----

    def _clear_residual(self) -> None:
        """退出码 3 之后的六项核对。不过就抛 FactoryConfigError、不发任何信号;D/.lock 已没人持就直接返回(重试启动);
        全部通过才 SIGTERM,等锁释放,不放就 SIGKILL,删 service.pid。"""
        self._join_reader()
        try:
            pid = self._check_residual()
        except _Residual as exc:
            shown = f" (pid {exc.pid})" if exc.pid is not None else ""
            _log.warning("memory service residual%s: %s check failed; not signalled", shown, exc.item)
            raise FactoryConfigError(
                f"memory service data dir is held by another process{shown}: {exc.item} check failed; "
                f"not stopped; see {LOG_FILE}") from None
        if pid is None:
            return
        lock = self._data_dir / ".lock"
        _log.warning("memory service residual pid %d: every check passed; sending SIGTERM", pid)
        self._probe.signal(pid, _signal.SIGTERM)
        if not self._wait_free(lock, TERM_WAIT_S):
            _log.warning("memory service residual pid %d: data dir still locked after %g s; sending SIGKILL",
                         pid, TERM_WAIT_S)
            self._probe.signal(pid, _signal.SIGKILL)
            self._wait_free(lock, KILL_WAIT_S)
        _unlink(self._root / PID_FILE)
        _log.warning("memory service residual pid %d: stopped; retrying the start once", pid)

    def _check_residual(self) -> int | None:
        # ① 归属锁已在手:_acquire 先取了
        pid, argv = _read_pid_file(self._root / PID_FILE)            # ②
        if not self._probe.alive(pid):                               # ③
            raise _Residual("process alive", pid)
        if self._probe.uid_of(pid) != os.getuid():                   # ④
            raise _Residual("process owner", pid)
        if not _command_matches(self._probe.command_of(pid), argv, str(self._data_dir)):   # ⑤
            raise _Residual("command line", pid)
        if _lock_is_free(self._data_dir / ".lock"):                  # ⑥ 拿得到:残留已退出
            _log.warning("memory service residual pid %d: data dir lock is free; retrying without a signal", pid)
            return None
        return pid

    @staticmethod
    def _wait_free(lock: Path, limit_s: float) -> bool:
        deadline = time.monotonic() + limit_s
        while True:
            if _lock_is_free(lock):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(_LOCK_POLL_S)

    # ---- 运行期 ----

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def restart(self) -> None:
        """进程还活着就什么都不做;否则按 1、4、16 秒退避后重新拉起(新端口、新 token,归属锁不放)。
        进程生命周期内第 4 次需要重拉时抛 ServiceRestartsExhausted。"""
        if self.alive():
            return
        if self._restarts >= len(RESTART_BACKOFF_S):
            raise ServiceRestartsExhausted(f"memory service restarted {len(RESTART_BACKOFF_S)} times; see {LOG_FILE}")
        delay = RESTART_BACKOFF_S[self._restarts]
        self._restarts += 1
        _log.warning("memory service exited; restart %d of %d in %g s", self._restarts, len(RESTART_BACKOFF_S), delay)
        if self._proc is not None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                self._proc.wait(KILL_WAIT_S)
        self._join_reader()
        self._sleep(delay)
        self._start()

    def close(self) -> None:
        """SIGTERM,至多 40 秒,再 SIGKILL 至多 5 秒;删 service.pid;最后释放归属锁。幂等。"""
        if self._closed:
            return
        self._closed = True
        try:
            proc = self._proc
            if proc is not None and proc.poll() is None:
                proc.send_signal(_signal.SIGTERM)
                try:
                    proc.wait(TERM_WAIT_S)
                except subprocess.TimeoutExpired:
                    _log.warning("memory service pid %d did not stop within %g s; sending SIGKILL", proc.pid, TERM_WAIT_S)
                    proc.kill()
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        proc.wait(KILL_WAIT_S)
            self._join_reader()
            if proc is not None:
                _unlink(self._root / PID_FILE)
            _unlink(self._root / TOKEN_FILE)
        finally:
            self._owner.close()


def _drain(proc: subprocess.Popen, lines: queue.Queue) -> None:
    """读完子进程的 stdout(逐行放进队列),EOF 时放 None。"""
    try:
        for line in proc.stdout:
            lines.put(line)
    except (OSError, ValueError):
        pass
    lines.put(None)


def spawn_service(root: Path, env: Mapping[str, str], *, python: str = sys.executable,
                  probe: ProcessProbe = SYSTEM_PROBE, ready_timeout_s: float = READY_TIMEOUT_S,
                  sleep: Callable[[float], None] = time.sleep, command: Sequence[str] | None = None) -> SpawnedService:
    """取归属锁,拉起服务,等到 ready。`env` 是父进程环境(测试传显式字典):子进程去掉 `STARLING_*`、`HOME` 指到
    `D/home`、`OPENAI_API_KEY` 照传。`command` 只给测试换掉子进程的命令行。任何失败都先放掉归属锁再抛。"""
    svc = SpawnedService(Path(root), env, python=python, probe=probe, ready_timeout_s=ready_timeout_s,
                         sleep=sleep, command=command)
    try:
        svc._acquire()
        svc._start()
    except BaseException:
        try:
            svc._kill_child()
        finally:
            svc._owner.close()
        raise
    return svc
