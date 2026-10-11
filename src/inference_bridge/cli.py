"""命令行形态的桥接(设计 §5.1、R15):子进程调外部工具,提示词走标准输入。

- 参数模板来自 BackendConfig.command(argv 列表,不经 shell);模板里的 "{model}" 换成 model_name,
  其余文本原样传。凭证只经环境变量传给子进程,绝不出现在 argv 里。
- 子进程默认隔离(设计 BR-8):环境变量只有「白名单 ∪ {token_env}」与传入环境的交集,
  工作目录是每次 complete 调用新建、结束即删的空临时目录。DEFAULT_COMMANDS 是两个工具的默认参数模板。
- 超时、非零退出、空输出算 T1,按 retry_policy 指数退避重试;耗尽抛 BridgeError("T1")。
  启动阶段的任何 OSError(不存在、无权限、不是目录、ENOEXEC、E2BIG……)都是确定性的,
  立即抛 BridgeError("T2"),不重试。
- 子进程输出按 UTF-8 解码,非法字节替换成 U+FFFD,不会因外部工具的编码抛 UnicodeDecodeError。
- 请求与响应过 controlplane.config.redact 之后写日志文件;git 树里只留 BigRef 形式的 log_ref。
  日志根目录里放一个内容为 "*" 的 .gitignore,日志不会被任何 git 仓库追踪。
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from controlplane.config import DEFAULT_ENV_ALLOWLIST, BackendConfig, redact, resolve_credential
from controlplane.envelope import BigRef
from controlplane.gitstore import GitStore
from inference_bridge.base import BridgeError, BridgeResult, _check_segment, record_bridge_cost

LOG_SUBDIR = ".rismforge/logs"  # 相对 store.repo_root 的默认日志根目录
_TAIL = 300  # 异常信息里最多带多少字符的 stderr 尾巴(已脱敏)

# 两个工具的默认参数模板(设计 §5.1):claude 关闭全部工具、不落会话文件;codex 只读沙箱、
# 不要求工作目录是 git 仓库、不落会话文件。只是常量:不自动写进任何配置,组合根按需取用。
DEFAULT_COMMANDS: dict[str, list[str]] = {
    "claude": ["claude", "-p", "--model", "{model}", "--tools", "", "--no-session-persistence"],
    "codex": ["codex", "exec", "-m", "{model}", "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral"],
}


@dataclass(frozen=True)
class RunOutcome:
    returncode: int | None  # 超时为 None
    stdout: str
    stderr: str
    timed_out: bool = False


# (argv, 标准输入文本, 超时秒数, 子进程环境, 工作目录) -> RunOutcome。
# 子进程起不来时抛 OSError(FileNotFoundError、PermissionError、ENOEXEC 等)。
Runner = Callable[[list[str], str, float, Mapping[str, str], str], RunOutcome]


def _text(data: str | bytes | None) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data


def subprocess_runner(
    argv: list[str], stdin: str, timeout_s: float, env: Mapping[str, str], cwd: str
) -> RunOutcome:
    """默认 runner:真起子进程,工作目录是 cwd。超时由 subprocess.run 杀掉子进程。"""
    try:
        proc = subprocess.run(
            argv, input=stdin, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout_s,
            env=dict(env), cwd=cwd, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return RunOutcome(returncode=None, stdout=_text(exc.stdout), stderr=_text(exc.stderr), timed_out=True)
    return RunOutcome(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


def _retry_policy(policy: dict) -> tuple[int, float]:
    max_attempts = policy.get("max_attempts", 3)
    backoff = policy.get("backoff_base_s", 1.0)
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
        raise ValueError(f"retry_policy.max_attempts must be an int >= 1, got {max_attempts!r}")
    if isinstance(backoff, bool) or not isinstance(backoff, (int, float)) or backoff < 0:
        raise ValueError(f"retry_policy.backoff_base_s must be a number >= 0, got {backoff!r}")
    return max_attempts, float(backoff)


class CliBridge:
    def __init__(
        self,
        config: BackendConfig,
        *,
        store: GitStore | None = None,
        log_dir: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        runner: Runner = subprocess_runner,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if config.invocation_form != "cli":
            raise ValueError(f"CliBridge needs invocation_form 'cli', got {config.invocation_form!r}")
        if not config.command:
            raise ValueError(f"backend {config.backend!r} has no 'command' template")
        self._config = config
        self._max_attempts, self._backoff = _retry_policy(config.retry_policy)
        self._env = dict(os.environ if env is None else env)
        # 缺凭证在构造时就明确失败(MissingCredentialError,只报变量名);token_env 为 None 时 secret 为 None
        self._secret = resolve_credential(config, self._env)
        self._argv = [a.replace("{model}", config.model_name) for a in config.command]
        # 子进程只拿「白名单 ∪ {token_env}」与完整环境的交集。self._env 仍是完整环境(解析凭证、脱敏要用);
        # env_allowlist 为 None 才用默认白名单,给了空 list 就是"只传 token_env"。
        allowed = set(DEFAULT_ENV_ALLOWLIST if config.env_allowlist is None else config.env_allowlist)
        if config.token_env:
            allowed.add(config.token_env)
        self._child_env = {k: v for k, v in self._env.items() if k in allowed}
        self._store = store
        if log_dir is not None:
            self._log_root: Path | None = Path(log_dir)
        elif store is not None:
            self._log_root = store.repo_root / LOG_SUBDIR
        else:
            self._log_root = None
        self._runner = runner
        self._sleep = sleep

    def _redact(self, text: str) -> str:
        return redact(text, [self._secret])

    def _write_log(self, run_id: str, task_id: str, prompt: str, attempts: list[str]) -> BigRef | None:
        if self._log_root is None:
            return None
        self._log_root.mkdir(parents=True, exist_ok=True)
        ignore = self._log_root / ".gitignore"
        if not ignore.exists():
            ignore.write_text("*\n", encoding="utf-8")
        run_logs = self._log_root / run_id
        run_logs.mkdir(parents=True, exist_ok=True)
        k = 1
        while (run_logs / f"{task_id}-{k}.log").exists():
            k += 1
        path = (run_logs / f"{task_id}-{k}.log").resolve()
        body = "\n".join(
            [
                f"backend: {self._config.backend}",
                f"model: {self._config.model_name}",
                f"argv: {self._redact(repr(self._argv))}",
                f"run_id: {run_id}",
                f"task_id: {task_id}",
                "--- prompt ---",
                self._redact(prompt),
                *attempts,
                "",
            ]
        )
        data = body.encode("utf-8")
        path.write_bytes(data)
        return BigRef(uri=f"file://{path}", sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))

    def complete(self, prompt: str, *, run_id: str, task_id: str) -> BridgeResult:
        _check_segment("run_id", run_id)
        _check_segment("task_id", task_id)
        # 每次调用(不是每次尝试)一个空临时目录:成功、T1 耗尽、T2 都由 with 删掉,一次调用的所有尝试共用它。
        with tempfile.TemporaryDirectory(prefix="rismforge-bridge-") as cwd:
            return self._complete_in(cwd, prompt, run_id, task_id)

    def _complete_in(self, cwd: str, prompt: str, run_id: str, task_id: str) -> BridgeResult:
        attempts_log: list[str] = []
        last = ""
        for attempt in range(1, self._max_attempts + 1):
            not_runnable: str | None = None
            try:
                out = self._runner(list(self._argv), prompt, self._config.timeout_s, self._child_env, cwd)
            except OSError as exc:
                # 只在块内算出脱敏后的原因;出块后再抛,BridgeError 的 __context__ 不会挂着带凭证的原异常
                not_runnable = self._redact(f"cannot run {self._argv[0]!r}: {type(exc).__name__}: {exc}")
            if not_runnable is not None:
                attempts_log.append(f"--- attempt {attempt}: not runnable ---\n{not_runnable}")
                ref = self._write_log(run_id, task_id, prompt, attempts_log)
                raise BridgeError("T2", not_runnable, attempts=attempt, log_ref=ref)
            stdout, stderr = self._redact(out.stdout), self._redact(out.stderr)
            if out.timed_out:
                last = f"timed out after {self._config.timeout_s}s"
            elif out.returncode != 0:
                last = f"exit code {out.returncode}"
            elif not stdout.strip():
                last = "empty output"
            else:
                attempts_log.append(f"--- attempt {attempt}: ok ---\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}")
                ref = self._write_log(run_id, task_id, prompt, attempts_log)
                result = BridgeResult(
                    text=stdout.strip(), input_tokens=None, output_tokens=None,
                    model=self._config.model_name, log_ref=ref,
                )
                record_bridge_cost(self._store, result, run_id=run_id, task_id=task_id, calls=attempt)
                return result
            tail = stderr.strip()[-_TAIL:]
            if tail:
                last = f"{last}; stderr tail: {tail}"
            attempts_log.append(f"--- attempt {attempt}: {last} ---\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}")
            if attempt < self._max_attempts:
                self._sleep(self._backoff * (2 ** (attempt - 1)))
        ref = self._write_log(run_id, task_id, prompt, attempts_log)
        raise BridgeError(
            "T1", f"{self._config.backend}: {self._max_attempts} attempt(s) failed; last: {last}",
            attempts=self._max_attempts, log_ref=ref,
        )
