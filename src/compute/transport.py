"""SSH 传输(真实后端设计 §3.1):BatchMode、连接超时、命令用 shlex.join 拼成一个字符串,数据走 stdin。"""
from __future__ import annotations

import re
import shlex
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from compute.errors import ComputeError, TransportError

# 不能以 '-' 开头(会被 ssh 当成选项),不含空白与引号。再加 `--` 双保险。
_ALIAS = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._@:-]*$")


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class Runner(Protocol):
    """把一条完整的 ssh 命令行跑完。测试里换成本机替身。"""

    def __call__(self, argv: Sequence[str], *, input: bytes, timeout: float) -> RunResult: ...


class Transport(Protocol):
    def run(self, remote_argv: Sequence[str], *, stdin: bytes = b"", timeout: float = 60.0) -> RunResult: ...


def subprocess_runner(argv: Sequence[str], *, input: bytes, timeout: float) -> RunResult:
    try:
        out = subprocess.run(list(argv), input=input, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise TransportError(f"ssh did not finish within {timeout:g}s") from None
    except OSError as exc:
        raise TransportError(f"cannot run ssh ({type(exc).__name__})") from None
    return RunResult(out.returncode, out.stdout, out.stderr)


class SshTransport:
    def __init__(self, alias: str, *, connect_timeout_s: float = 10.0, runner: Runner = subprocess_runner) -> None:
        if not isinstance(alias, str) or not _ALIAS.match(alias):
            raise ComputeError("the ssh alias has an invalid form")
        self.alias = alias
        self._alias = alias
        self._connect_timeout = max(1, int(connect_timeout_s))
        self._runner = runner

    def ssh_argv(self, remote_argv: Sequence[str]) -> list[str]:
        return ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={self._connect_timeout}",
                "--", self._alias, shlex.join(remote_argv)]

    def run(self, remote_argv: Sequence[str], *, stdin: bytes = b"", timeout: float = 60.0) -> RunResult:
        return self._runner(self.ssh_argv(remote_argv), input=stdin, timeout=timeout)
