import hashlib
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import pytest

import inference_bridge
from controlplane.config import DEFAULT_ENV_ALLOWLIST, MissingCredentialError, load_backend_config
from controlplane.gitstore import GitStore
from controlplane.observe import cost
from controlplane.paths import run_trace_file
from inference_bridge import DEFAULT_COMMANDS, BridgeError, CliBridge, InferenceBridge, RunOutcome

SECRET = "sk-test-SECRET-0123456789"


def _cfg(**over):
    raw = {
        "backend": "fake_cli", "invocation_form": "cli", "model_name": "fake-model",
        "prompt_template_version": "v1", "token_env": "FAKE_TOKEN",
        "retry_policy": {"max_attempts": 3, "backoff_base_s": 0.5},
        "command": ["fake-tool", "-p", "--model", "{model}"], "timeout_s": 5,
    }
    raw.update(over)
    return load_backend_config(raw)


class FakeRunner:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.cwd_seen = []  # 每次调用当时工作目录的状态:(是否存在, 目录里的文件名)

    def __call__(self, argv, stdin, timeout_s, env, cwd):
        self.calls.append((argv, stdin, timeout_s, dict(env), cwd))
        path = Path(cwd)
        self.cwd_seen.append((path.is_dir(), [p.name for p in path.iterdir()] if path.is_dir() else []))
        out = self.outcomes.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out


def _ok(text="hello"):
    return RunOutcome(returncode=0, stdout=text, stderr="")


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path / "repo"))
    s.init()
    return s


def _bridge(runner, *, store=None, log_dir=None, sleeps=None, env=None, **over):
    return CliBridge(
        _cfg(**over), store=store, log_dir=log_dir,
        env={"FAKE_TOKEN": SECRET, "PATH": "/bin"} if env is None else env,
        runner=runner, sleep=(sleeps.append if sleeps is not None else (lambda s: None)),
    )


def _log_path(ref):
    return Path(urlparse(ref.uri).path)


def test_prompt_goes_through_stdin_and_the_template_is_filled():
    runner = FakeRunner([_ok("  the answer \n")])
    b = _bridge(runner)
    assert isinstance(b, InferenceBridge)
    r = b.complete("plan the next round", run_id="run-1", task_id="plan-1")
    argv, stdin, timeout_s, env, cwd = runner.calls[0]
    assert argv == ["fake-tool", "-p", "--model", "fake-model"]
    assert stdin == "plan the next round"
    assert timeout_s == 5.0
    assert env["FAKE_TOKEN"] == SECRET  # 凭证只经环境变量给子进程
    assert SECRET not in " ".join(argv)
    assert (r.text, r.model, r.input_tokens, r.output_tokens, r.log_ref) == (
        "the answer", "fake-model", None, None, None,
    )


@pytest.mark.parametrize(
    "bad",
    [
        RunOutcome(returncode=None, stdout="", stderr="", timed_out=True),
        RunOutcome(returncode=2, stdout="partial", stderr="boom"),
        RunOutcome(returncode=0, stdout="   \n", stderr=""),
    ],
    ids=["timeout", "nonzero", "empty"],
)
def test_transient_failures_are_retried_with_exponential_backoff(bad):
    sleeps = []
    runner = FakeRunner([bad, bad, _ok("finally")])
    r = _bridge(runner, sleeps=sleeps).complete("p", run_id="run-1", task_id="t")
    assert r.text == "finally"
    assert len(runner.calls) == 3
    assert sleeps == [0.5, 1.0]


def test_exhausted_retries_raise_t1_with_the_attempt_count():
    sleeps = []
    bad = RunOutcome(returncode=1, stdout="", stderr="rate limited")
    runner = FakeRunner([bad, bad, bad])
    with pytest.raises(BridgeError) as exc:
        _bridge(runner, sleeps=sleeps).complete("p", run_id="run-1", task_id="t")
    assert exc.value.category == "T1"
    assert exc.value.attempts == 3
    assert "exit code 1" in exc.value.reason and "rate limited" in exc.value.reason
    assert sleeps == [0.5, 1.0]  # 最后一次失败之后不再睡


def test_a_single_attempt_policy_does_not_sleep():
    sleeps = []
    runner = FakeRunner([RunOutcome(returncode=1, stdout="", stderr="")])
    with pytest.raises(BridgeError):
        _bridge(runner, sleeps=sleeps, retry_policy={"max_attempts": 1, "backoff_base_s": 9}).complete(
            "p", run_id="r", task_id="t"
        )
    assert sleeps == []


def test_a_missing_executable_is_t2_and_not_retried():
    runner = FakeRunner([FileNotFoundError(2, "No such file or directory")])
    with pytest.raises(BridgeError) as exc:
        _bridge(runner).complete("p", run_id="r", task_id="t")
    assert (exc.value.category, exc.value.attempts) == ("T2", 1)
    assert len(runner.calls) == 1


def test_success_records_one_external_inference_cost_with_tokens_omitted(store):
    bad = RunOutcome(returncode=1, stdout="", stderr="")
    _bridge(FakeRunner([bad, _ok()]), store=store).complete("p", run_id="run-1", task_id="plan-1")
    [rec] = store.read_product(run_trace_file("run-1")).payload["cost_records"]
    assert (rec["task_id"], rec["cost_type"], rec["calls"]) == ("plan-1", "external_inference", 2)
    assert "tokens" not in rec
    assert cost(store, "run-1").endswith("total: gpu_hours=0.0 tokens=0")


def test_failure_records_no_cost(store):
    bad = RunOutcome(returncode=1, stdout="", stderr="")
    with pytest.raises(BridgeError):
        _bridge(FakeRunner([bad] * 3), store=store).complete("p", run_id="run-1", task_id="t")
    assert not store.exists(run_trace_file("run-1"))


def test_without_store_or_log_dir_nothing_is_written(tmp_path):
    r = _bridge(FakeRunner([_ok()])).complete("p", run_id="r", task_id="t")
    assert r.log_ref is None


def test_the_log_is_redacted_hashed_and_referenced(tmp_path):
    leaky = RunOutcome(returncode=0, stdout=f"echo {SECRET} back", stderr=f"debug token={SECRET}")
    r = _bridge(FakeRunner([leaky]), log_dir=tmp_path / "logs").complete(
        f"prompt that quotes {SECRET}", run_id="run-1", task_id="plan-1",
    )
    path = _log_path(r.log_ref)
    assert path == (tmp_path / "logs" / "run-1" / "plan-1-1.log").resolve()
    data = path.read_bytes()
    assert r.log_ref.sha256 == hashlib.sha256(data).hexdigest()
    assert r.log_ref.size_bytes == len(data)
    text = data.decode()
    assert SECRET not in text
    assert "***REDACTED***" in text and "prompt that quotes" in text and "debug token=" in text
    assert SECRET not in r.text


def test_repeated_calls_for_the_same_task_get_separate_logs(tmp_path):
    b = _bridge(FakeRunner([_ok("a"), _ok("b")]), log_dir=tmp_path / "logs")
    r1 = b.complete("p", run_id="run-1", task_id="t")
    r2 = b.complete("p", run_id="run-1", task_id="t")
    assert _log_path(r1.log_ref).name == "t-1.log" and _log_path(r2.log_ref).name == "t-2.log"


def test_the_failure_log_holds_every_attempt_and_the_error_never_carries_the_secret(tmp_path):
    bad = RunOutcome(returncode=3, stdout=f"oops {SECRET}", stderr=f"auth failed for {SECRET}")
    with pytest.raises(BridgeError) as exc:
        _bridge(FakeRunner([bad] * 3), log_dir=tmp_path / "logs").complete("p", run_id="run-1", task_id="t")
    assert SECRET not in str(exc.value) and SECRET not in exc.value.reason
    assert SECRET not in repr(exc.value.args)
    text = _log_path(exc.value.log_ref).read_text()
    assert SECRET not in text
    assert [f"attempt {i}: exit code 3" in text for i in (1, 2, 3)] == [True, True, True]


def test_a_not_runnable_error_message_is_redacted(tmp_path):
    runner = FakeRunner([PermissionError(13, f"denied while holding {SECRET}")])
    with pytest.raises(BridgeError) as exc:
        _bridge(runner, log_dir=tmp_path / "logs").complete("p", run_id="r", task_id="t")
    assert SECRET not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__context__ is None
    assert SECRET not in _log_path(exc.value.log_ref).read_text()


@pytest.mark.parametrize(
    "err",
    [
        NotADirectoryError(20, "Not a directory"),
        OSError(8, "Exec format error"),
        OSError(7, "Argument list too long"),
    ],
    ids=["ENOTDIR", "ENOEXEC", "E2BIG"],
)
def test_any_os_error_at_start_is_t2_and_not_retried(err):
    runner = FakeRunner([err, _ok()])
    with pytest.raises(BridgeError) as exc:
        _bridge(runner).complete("p", run_id="r", task_id="t")
    assert (exc.value.category, exc.value.attempts) == ("T2", 1)
    assert len(runner.calls) == 1
    assert exc.value.__context__ is None


def test_logs_default_under_the_store_and_stay_untracked(store):
    r = _bridge(FakeRunner([_ok()]), store=store).complete("p", run_id="run-1", task_id="plan-1")
    path = _log_path(r.log_ref)
    assert path.is_relative_to(store.repo_root.resolve() / ".rismforge" / "logs")
    assert (store.repo_root / ".rismforge" / "logs" / ".gitignore").read_text() == "*\n"
    status = subprocess.run(
        ["git", "-C", str(store.repo_root), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert ".rismforge" not in status  # 日志目录对 git 不可见
    tracked = subprocess.run(
        ["git", "-C", str(store.repo_root), "ls-files"], capture_output=True, text=True, check=True,
    ).stdout
    assert ".rismforge" not in tracked and "run_trace.yaml" in tracked


def test_the_credential_is_never_written_into_the_git_tree(store):
    leaky = RunOutcome(returncode=0, stdout=f"x {SECRET}", stderr=SECRET)
    _bridge(FakeRunner([leaky]), store=store).complete(f"p {SECRET}", run_id="run-1", task_id="t")
    tracked = subprocess.run(
        ["git", "-C", str(store.repo_root), "ls-files"], capture_output=True, text=True, check=True,
    ).stdout.split()
    for rel in tracked:
        assert SECRET not in (store.repo_root / rel).read_text()


def test_a_missing_credential_fails_at_construction_naming_only_the_variable():
    with pytest.raises(MissingCredentialError, match="FAKE_TOKEN"):
        CliBridge(_cfg(), env={}, runner=FakeRunner([]))


def test_a_tool_with_its_own_login_needs_no_credential():
    runner = FakeRunner([_ok()])
    b = CliBridge(_cfg(token_env=None), env={"PATH": "/bin"}, runner=runner, sleep=lambda s: None)
    assert b.complete("p", run_id="r", task_id="t").text == "hello"


@pytest.mark.parametrize(
    ("over", "match"),
    [
        ({"invocation_form": "api"}, "invocation_form"),
        ({"command": None}, "command"),
        ({"retry_policy": {"max_attempts": 0}}, "max_attempts"),
        ({"retry_policy": {"backoff_base_s": -1}}, "backoff_base_s"),
    ],
)
def test_bad_configs_are_rejected_at_construction(over, match):
    with pytest.raises(ValueError, match=match):
        CliBridge(_cfg(**over), env={"FAKE_TOKEN": SECRET}, runner=FakeRunner([]))


@pytest.mark.parametrize(("run_id", "task_id"), [("", "t"), ("r", "a/b"), ("..", "t")])
def test_ids_must_be_safe_path_segments(run_id, task_id):
    with pytest.raises(ValueError):
        _bridge(FakeRunner([_ok()])).complete("p", run_id=run_id, task_id=task_id)


_FAKE_TOOL = (
    "import os, sys\n"
    "data = sys.stdin.read()\n"
    "if sys.argv[1] == 'fail':\n"
    "    sys.stderr.write('nope ' + os.environ.get('FAKE_TOKEN', ''))\n"
    "    sys.exit(4)\n"
    "if sys.argv[1] == 'sleep':\n"
    "    import time; time.sleep(5)\n"
    "print('echo:' + data.upper() + ' model=' + sys.argv[2])\n"
)


def _real(mode, store=None, **over):
    return CliBridge(
        _cfg(command=[sys.executable, "-c", _FAKE_TOOL, mode, "{model}"], **over),
        store=store, env={"FAKE_TOKEN": SECRET}, sleep=lambda s: None,
    )


def test_a_real_subprocess_round_trip(store):
    r = _real("ok", store=store).complete("hello bridge", run_id="run-1", task_id="plan-1")
    assert r.text == "echo:HELLO BRIDGE model=fake-model"
    assert _log_path(r.log_ref).is_file()
    assert store.exists(run_trace_file("run-1"))


def test_a_real_subprocess_failure_is_t1_and_redacted(store):
    with pytest.raises(BridgeError) as exc:
        _real("fail", store=store, retry_policy={"max_attempts": 2, "backoff_base_s": 0}).complete(
            "p", run_id="run-1", task_id="t"
        )
    assert exc.value.category == "T1" and exc.value.attempts == 2
    assert "exit code 4" in exc.value.reason and "nope ***REDACTED***" in exc.value.reason
    assert SECRET not in _log_path(exc.value.log_ref).read_text()


def test_a_real_subprocess_timeout_is_t1():
    with pytest.raises(BridgeError) as exc:
        _real("sleep", timeout_s=0.5, retry_policy={"max_attempts": 1}).complete("p", run_id="r", task_id="t")
    assert exc.value.category == "T1" and "timed out" in exc.value.reason


def test_a_real_missing_executable_is_t2():
    b = CliBridge(
        _cfg(command=["/nonexistent/rismforge-no-such-tool"]), env={"FAKE_TOKEN": SECRET}, sleep=lambda s: None,
    )
    with pytest.raises(BridgeError) as exc:
        b.complete("p", run_id="r", task_id="t")
    assert exc.value.category == "T2"


def test_a_real_script_without_a_shebang_is_t2(tmp_path):
    tool = tmp_path / "no-shebang-tool"
    tool.write_text("echo hello\n", encoding="utf-8")
    tool.chmod(0o755)
    b = CliBridge(_cfg(command=[str(tool)]), env={"FAKE_TOKEN": SECRET}, sleep=lambda s: None)
    with pytest.raises(BridgeError) as exc:
        b.complete("p", run_id="r", task_id="t")
    assert exc.value.category == "T2" and exc.value.attempts == 1
    assert exc.value.__context__ is None


def test_non_utf8_output_is_decoded_with_replacement():
    script = "import sys; sys.stdout.buffer.write(b'ok \\xff\\xfe end'); sys.stdout.flush()"
    b = CliBridge(
        _cfg(command=[sys.executable, "-c", script]), env={"FAKE_TOKEN": SECRET}, sleep=lambda s: None,
    )
    r = b.complete("p", run_id="r", task_id="t")
    assert r.text == "ok �� end"


def _child_env_of(runner):
    return runner.calls[0][3]


def test_the_child_env_is_the_default_allowlist_plus_token_env_and_nothing_else():
    env = {
        "FAKE_TOKEN": SECRET, "PATH": "/bin", "HOME": "/home/u", "LANG": "C",
        "AWS_SECRET_ACCESS_KEY": "other-secret", "GITHUB_TOKEN": "other-token",
    }
    runner = FakeRunner([_ok()])
    _bridge(runner, env=env).complete("p", run_id="r", task_id="t")
    assert _child_env_of(runner) == {"FAKE_TOKEN": SECRET, "PATH": "/bin", "HOME": "/home/u", "LANG": "C"}
    assert set(_child_env_of(runner)) - {"FAKE_TOKEN"} <= set(DEFAULT_ENV_ALLOWLIST)


def test_an_explicit_allowlist_replaces_the_default_and_token_env_is_added_automatically():
    env = {"FAKE_TOKEN": SECRET, "PATH": "/bin", "LANG": "C", "EXTRA": "x"}
    runner = FakeRunner([_ok()])
    _bridge(runner, env=env, env_allowlist=["LANG", "EXTRA", "ABSENT"]).complete("p", run_id="r", task_id="t")
    # PATH 不在名单里,不传;FAKE_TOKEN 不在名单里也自动加入;名单里有、环境里没有的 ABSENT 直接略过
    assert _child_env_of(runner) == {"FAKE_TOKEN": SECRET, "LANG": "C", "EXTRA": "x"}


def test_without_a_token_env_no_credential_variable_is_passed():
    env = {"FAKE_TOKEN": SECRET, "PATH": "/bin"}
    runner = FakeRunner([_ok()])
    _bridge(runner, env=env, token_env=None).complete("p", run_id="r", task_id="t")
    assert _child_env_of(runner) == {"PATH": "/bin"}


def test_an_empty_allowlist_passes_only_the_token_env():
    env = {"FAKE_TOKEN": SECRET, "PATH": "/bin", "HOME": "/home/u"}
    runner = FakeRunner([_ok()])
    _bridge(runner, env=env, env_allowlist=[]).complete("p", run_id="r", task_id="t")
    assert _child_env_of(runner) == {"FAKE_TOKEN": SECRET}


def test_every_call_runs_in_its_own_empty_directory_that_is_removed_afterwards():
    runner = FakeRunner([_ok(), _ok()])
    b = _bridge(runner)
    b.complete("p", run_id="r", task_id="t1")
    b.complete("p", run_id="r", task_id="t2")
    (_, _, _, _, cwd1), (_, _, _, _, cwd2) = runner.calls
    assert cwd1 != cwd2
    assert runner.cwd_seen == [(True, []), (True, [])]  # 调用当时:目录存在,而且是空的
    assert not Path(cwd1).exists() and not Path(cwd2).exists()  # 调用之后:已删除


def test_the_attempts_of_one_call_share_one_directory_which_is_removed_after_success():
    bad = RunOutcome(returncode=1, stdout="", stderr="")
    runner = FakeRunner([bad, _ok()])
    _bridge(runner).complete("p", run_id="r", task_id="t")
    cwds = {call[4] for call in runner.calls}
    assert len(runner.calls) == 2 and len(cwds) == 1  # 目录是每次调用一个,不是每次尝试一个
    assert runner.cwd_seen == [(True, []), (True, [])]
    assert not Path(next(iter(cwds))).exists()


def test_the_directory_is_removed_when_the_retries_are_exhausted():
    bad = RunOutcome(returncode=1, stdout="", stderr="")
    runner = FakeRunner([bad, bad, bad])
    with pytest.raises(BridgeError) as exc:
        _bridge(runner).complete("p", run_id="r", task_id="t")
    assert exc.value.category == "T1"
    cwds = {call[4] for call in runner.calls}
    assert len(runner.calls) == 3 and len(cwds) == 1
    assert not Path(next(iter(cwds))).exists()


def test_the_directory_is_removed_when_the_tool_cannot_start():
    runner = FakeRunner([FileNotFoundError(2, "No such file or directory")])
    with pytest.raises(BridgeError) as exc:
        _bridge(runner).complete("p", run_id="r", task_id="t")
    assert exc.value.category == "T2" and exc.value.__context__ is None
    assert runner.cwd_seen == [(True, [])]
    assert not Path(runner.calls[0][4]).exists()


_ENV_TOOL = (
    "import json, os\n"
    "print(json.dumps({'env': sorted(os.environ), 'cwd': os.getcwd(), 'ls': os.listdir('.')}))\n"
)


def test_a_real_subprocess_sees_only_allowlisted_variables_and_an_empty_working_directory(monkeypatch):
    monkeypatch.setenv("FAKE_TOKEN", SECRET)
    monkeypatch.setenv("RISMFORGE_UNLISTED", "must-not-leak")
    b = CliBridge(_cfg(command=[sys.executable, "-c", _ENV_TOOL]), sleep=lambda s: None)  # env 缺省取 os.environ
    seen = json.loads(b.complete("p", run_id="r", task_id="t").text)
    assert "FAKE_TOKEN" in seen["env"]
    assert "RISMFORGE_UNLISTED" not in seen["env"]
    assert seen["ls"] == []
    assert Path(seen["cwd"]).resolve() != Path.cwd().resolve()
    assert not Path(seen["cwd"]).exists()  # 调用结束后目录已删除


def test_default_commands_turn_tools_off_and_keep_sessions_out_of_the_way():
    assert DEFAULT_COMMANDS == {
        "claude": ["claude", "-p", "--model", "{model}", "--tools", "", "--no-session-persistence"],
        "codex": [
            "codex", "exec", "-m", "{model}", "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral",
        ],
    }
    claude, codex = DEFAULT_COMMANDS["claude"], DEFAULT_COMMANDS["codex"]
    assert claude[claude.index("--tools") + 1] == ""  # 关闭全部工具
    assert codex[codex.index("--sandbox") + 1] == "read-only"  # 只读沙箱


def test_default_commands_are_exported_from_the_package():
    assert "DEFAULT_COMMANDS" in inference_bridge.__all__


@pytest.mark.parametrize("tool", ["claude", "codex"])
def test_a_default_command_template_is_filled_and_passed_as_argv(tool):
    cfg = _cfg(command=DEFAULT_COMMANDS[tool])  # 经 load_backend_config:默认模板必须能被配置加载器接受
    runner = FakeRunner([_ok()])
    CliBridge(cfg, env={"FAKE_TOKEN": SECRET}, runner=runner, sleep=lambda s: None).complete(
        "p", run_id="r", task_id="t"
    )
    argv = runner.calls[0][0]
    assert argv[0] == tool and "fake-model" in argv and "{model}" not in argv
    assert len(argv) == len(DEFAULT_COMMANDS[tool])
