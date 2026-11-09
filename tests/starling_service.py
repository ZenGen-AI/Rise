"""在子进程里起真的 starling-serve,设计四第 3 步起各用例共用(照 Starling 的 tests/python/fakes/service_proc.py)。

- 数据目录是 `tmp_dir/data`,HOME 是 `tmp_dir/home`,token 由 `secrets.token_urlsafe(32)` 生成、写进 `tmp_dir/token`(0600)。
  不读也不写用户的 `~/.starling`,不连 8787 上的看板服务。
- 子进程环境去掉 `OPENAI_*` 与 `STARLING_*`,`OPENAI_API_KEY` 只放占位值 `offline-test-key`:读不到本机的真配置与密钥。
  抽取与嵌入要用时走 `tests/starling_fakes.py` 的假端点(由用例写进世界设置)。
- stdout 由一条读线程逐行放进队列(从 listening 行读回端口、ready 行判就绪);stderr 追加写文件(访问日志多,管道会写满)。
  每个等待都有上限。

用它的测试文件各自定义模块级夹具:

    @pytest.fixture(scope="module")
    def service(tmp_path_factory):
        pytest.importorskip("starling_client"); pytest.importorskip("starling.service")
        proc = start_service(tmp_path_factory.mktemp("svc"))
        yield proc
        proc.kill()
"""
from __future__ import annotations

import json
import os
import queue
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

LISTENING = re.compile(r"starling-serve listening on http://127\.0\.0\.1:(\d+)")
READY = "starling-serve ready"
START_TIMEOUT_S = 60.0
OFFLINE_API_KEY = "offline-test-key"


def clean_env(home: Path) -> dict:
    """去掉 OPENAI_* 与 STARLING_*;HOME 指到 home;OPENAI_API_KEY 只放占位值。"""
    env = {key: value for key, value in os.environ.items() if not key.startswith(("OPENAI_", "STARLING_"))}
    env["HOME"] = str(home)
    env["OPENAI_API_KEY"] = OFFLINE_API_KEY
    return env


class ServiceProc:
    """一个 starling-serve 子进程。`restart()` 在同一端口、同一 token、同一数据目录上重起(外部服务重启的用例用)。"""

    def __init__(self, tmp_dir: Path, *, port: int = 0) -> None:
        self.tmp_dir = tmp_dir
        self.data_dir = tmp_dir / "data"
        self.home = tmp_dir / "home"
        self.token_path = tmp_dir / "token"
        self.stderr_path = tmp_dir / "stderr.log"
        self.token = secrets.token_urlsafe(32)
        self.port = port
        self.proc: subprocess.Popen | None = None
        self.stdout: list[str] = []
        self._stderr = None
        self._reader: threading.Thread | None = None
        self._lines: queue.Queue = queue.Queue()

    def __repr__(self) -> str:                    # 不带 token
        return f"ServiceProc(port={self.port}, data_dir={self.data_dir})"

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _argv(self) -> list[str]:
        return [sys.executable, "-m", "starling.service", "--data-dir", str(self.data_dir),
                "--host", "127.0.0.1", "--port", str(self.port), "--token-file", str(self.token_path)]

    def start(self) -> "ServiceProc":
        """起子进程,等 listening 与 ready 两行(各至多 60 秒),返回自己。"""
        self._stderr = open(self.stderr_path, "ab")
        self._lines = queue.Queue()
        self.proc = subprocess.Popen(self._argv(), env=clean_env(self.home), cwd=str(self.tmp_dir),
                                     stdout=subprocess.PIPE, stderr=self._stderr, text=True)
        self._reader = threading.Thread(target=self._read, args=(self.proc, self._lines), daemon=True)
        self._reader.start()
        try:
            line = self._wait_line(lambda text: LISTENING.search(text) is not None)
            self.port = int(LISTENING.search(line).group(1))
            self._wait_line(lambda text: READY in text)
        except BaseException:
            self.kill()
            raise
        return self

    def _read(self, proc: subprocess.Popen, lines: queue.Queue) -> None:
        for line in proc.stdout:
            self.stdout.append(line)
            lines.put(line)
        lines.put(None)

    def _wait_line(self, predicate, timeout_s: float = START_TIMEOUT_S) -> str:
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"no matching stdout line; stdout={self.stdout!r}; "
                                     f"stderr={self.stderr()[-2000:]!r}")
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if line is None:
                raise AssertionError(f"service exited ({self.proc.wait()}) before the line; "
                                     f"stdout={self.stdout!r}; stderr={self.stderr()[-2000:]!r}")
            if predicate(line):
                return line

    def stderr(self) -> str:
        if self._stderr is not None and not self._stderr.closed:
            self._stderr.flush()
        return self.stderr_path.read_text("utf-8", "replace") if self.stderr_path.exists() else ""

    def terminate(self, timeout_s: float = 40.0) -> int:
        """SIGTERM,等它自己关完世界、放锁退出;返回退出码。"""
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
        code = self.proc.wait(timeout_s)
        self._close_pipes()
        return code

    def kill(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(10)
        self._close_pipes()

    def _close_pipes(self) -> None:
        if self._reader is not None:
            self._reader.join(10)
        if self.proc is not None and self.proc.stdout is not None:
            self.proc.stdout.close()
        if self._stderr is not None:
            self._stderr.close()

    def restart(self) -> None:
        """停掉(还活着就 SIGTERM),再在同一端口、同一 token、同一数据目录上起来。"""
        if self.proc is not None and self.proc.poll() is None:
            self.terminate()
        else:
            self._close_pipes()
        self.start()


def start_service(tmp_dir: Path, *, starling_json: dict | None = None, port: int = 0) -> ServiceProc:
    """在 tmp_dir 下起一个 starling-serve。`starling_json` 给了就先写成 `data/starling.json`(叶子继承的用例用)。"""
    tmp_dir.mkdir(parents=True, exist_ok=True)
    proc = ServiceProc(tmp_dir, port=port)
    proc.data_dir.mkdir()
    proc.home.mkdir()
    fd = os.open(proc.token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(proc.token + "\n")
    if starling_json is not None:
        (proc.data_dir / "starling.json").write_text(json.dumps(starling_json), encoding="utf-8")
    return proc.start()
