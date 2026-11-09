#!/usr/bin/env python3
"""设计四第 0 步:核实风险 32 与 37(两行 Starling 依赖可锁、可装,提交可比;`Transport` 签名不变)。升级 Starling 时重跑。

在 --workdir 里放 pyproject.toml 与 uv.lock 的副本,把 extra `memory` 换成钉在 --commit 的两行(starling-client 带子目录、
starling-memory[dashboard]),跑 `uv lock` 与 `uv sync --frozen --extra memory --no-install-project`,再用 <workdir>/venv 的
解释器跑一段探针:读两份 direct_url.json 与 starling_client.COMMIT,起一次 `python -m starling.service`(数据目录与 HOME 都在
<workdir> 下,OPENAI_API_KEY 只放占位值),调 info(),再用 Transport(url, token, 300.0) 发一次不带会话的 list_worlds。
仓库的 pyproject.toml、uv.lock 与 .venv 都不动。逐项打印 OK / FAIL,有 FAIL 时退出 1。
token 由本脚本生成,写进 0600 的文件,探针结束即删,不打印。只用标准库。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

REPO = Path(__file__).resolve().parent.parent
STARLING_GIT = "git+https://github.com/ruijieguo/starling"

CHECKS = (
    "uv lock resolves starling-client (subdirectory=clients/python) and starling-memory[dashboard] at one commit",
    "uv sync installs httpx2 from PyPI",
    "both dist-infos record vcs_info.commit_id == --commit",
    "starling_client.COMMIT == --commit",
    "service info()['commit'] == --commit and protocol == starling_client.PROTOCOL",
    "starling_client.Transport(url, token, timeout_s) and .post(path, json, *, timeout_s) keep their signatures",
    "a session-less admin POST through Transport succeeds",
)
# Transport 的签名按 inspect.signature 比字符串(docs/design 90 篇第 5 章管理传输一段依赖它):
EXPECTED_INIT = "(self, url: 'str', token: 'str', timeout_s: 'float' = 30.0, *, _httpx_transport=None)"
EXPECTED_POST = "(self, path: 'str', json: 'dict | None' = None, *, timeout_s: 'float | None' = None) -> 'dict'"
ADMIN_TIMEOUT_S = 300.0

LOCK_TIMEOUT_S = 600
SYNC_TIMEOUT_S = 1800
PROBE_TIMEOUT_S = 300

# 探针在 <workdir>/venv 的解释器里跑(python -I)。argv: 数据目录、HOME、token 文件、stderr 日志。只收集事实,判定在外面做;
# 最后一行 stdout 是一个 JSON。错误只记异常类名与客户端异常的 code,不记 url 与 token。
PROBE = r'''
import importlib.metadata as md, inspect, json, os, queue, re, signal, subprocess, sys, threading, time
data_dir, home, token_file, stderr_path = sys.argv[1:5]
facts = {"python": sys.version.split()[0]}

def err(exc):
    code = getattr(exc, "code", None)
    return type(exc).__name__ + (f" code={code}" if code else "")

for name in ("starling-client", "starling-memory", "httpx2"):
    try:
        dist = md.distribution(name)
        raw = dist.read_text("direct_url.json")
        facts[name] = {"version": dist.version, "direct_url": json.loads(raw) if raw else None}
    except md.PackageNotFoundError:
        facts[name] = None

import starling_client
from starling_client import _transport
facts["client_commit"] = starling_client.COMMIT
facts["client_protocol"] = starling_client.PROTOCOL
facts["transport_exported"] = "Transport" in starling_client.__all__ and starling_client.Transport is _transport.Transport
facts["sig_init"] = str(inspect.signature(_transport.Transport.__init__))
facts["sig_post"] = str(inspect.signature(_transport.Transport.post))

token = open(token_file, encoding="utf-8").read().strip()
env = {k: v for k, v in os.environ.items() if not k.startswith(("OPENAI_", "STARLING_"))}
env.update(HOME=home, OPENAI_API_KEY="offline-test-key")
argv = [sys.executable, "-I", "-m", "starling.service", "--data-dir", data_dir, "--host", "127.0.0.1",
        "--port", "0", "--token-file", token_file]
lines = queue.Queue()
with open(stderr_path, "wb") as stderr:
    started = time.monotonic()
    proc = subprocess.Popen(argv, env=env, cwd=os.path.dirname(data_dir), stdout=subprocess.PIPE,
                            stderr=stderr, text=True)
    def read():
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)
    threading.Thread(target=read, daemon=True).start()
    try:
        port = None
        deadline = started + 60.0
        while True:
            line = lines.get(timeout=max(deadline - time.monotonic(), 0.01))
            if line is None:
                raise RuntimeError(f"service exited with {proc.wait()} before ready")
            m = re.search(r"starling-serve listening on http://127\.0\.0\.1:(\d+)", line)
            if m:
                port = int(m.group(1))
            if "starling-serve ready" in line and port is not None:
                break
        facts["ready_s"] = round(time.monotonic() - started, 2)
        url = f"http://127.0.0.1:{port}"
        try:
            client = starling_client.connect(url, token=token)
            try:
                info = client.info()
            finally:
                client.close()
            facts["info"] = {"commit": info.get("commit"), "version": info.get("version"),
                             "protocol": info.get("protocol"),
                             "default_settings_keys": sorted((info.get("default_settings") or {}).keys())}
        except Exception as exc:
            facts["info_error"] = err(exc)
        try:
            admin = starling_client.Transport(url, token, ''' + repr(ADMIN_TIMEOUT_S) + r''')
            try:
                out = admin.post("/api/v1/client/admin/list_worlds",
                                 {"labels": None, "page": 1, "page_size": 50, "with_digest": False})
            finally:
                admin.close()
            facts["admin"] = {"total": out.get("total"), "worlds": len(out.get("worlds") or [])}
        except Exception as exc:
            facts["admin_error"] = err(exc)
        proc.send_signal(signal.SIGTERM)
        facts["sigterm_exit"] = proc.wait(40)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)
facts["home_files"] = sorted(os.path.relpath(os.path.join(d, f), home)
                             for d, _, fs in os.walk(home) for f in fs)
print(json.dumps(facts))
'''


def memory_extra(commit: str) -> str:
    return ("memory = [\n"
            f'    "starling-client @ {STARLING_GIT}@{commit}#subdirectory=clients/python",\n'
            f'    "starling-memory[dashboard] @ {STARLING_GIT}@{commit}",\n'
            "]\n")


def replace_memory_extra(text: str, commit: str) -> str:
    """把 [project.optional-dependencies] 里 `memory = [...]`(一行或多行)整段换成两行;按方括号配平找结尾。"""
    out: list[str] = []
    lines = text.splitlines(keepends=True)
    i = 0
    found = False
    while i < len(lines):
        if not found and lines[i].startswith("memory = ["):
            depth = 0
            while True:
                depth += lines[i].count("[") - lines[i].count("]")
                i += 1
                if depth <= 0 or i >= len(lines):
                    break
            out.append(memory_extra(commit))
            found = True
            continue
        out.append(lines[i])
        i += 1
    if not found:
        raise SystemExit("pyproject.toml 里没有 `memory = [` 一行")
    return "".join(out)


def uv_env(workdir: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV" and not k.startswith(("OPENAI_", "STARLING_"))}
    env["UV_PROJECT_ENVIRONMENT"] = str(workdir / "venv")
    return env


def run(cmd: list[str], workdir: Path, timeout_s: int) -> tuple[subprocess.CompletedProcess | None, float, str]:
    started = time.monotonic()
    try:
        proc = subprocess.run(cmd, cwd=workdir, env=uv_env(workdir), capture_output=True, text=True,
                              timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return None, time.monotonic() - started, f"timed out after {timeout_s} s"
    elapsed = time.monotonic() - started
    if proc.returncode != 0:
        return proc, elapsed, f"exit {proc.returncode}: {proc.stderr.strip()[-1500:]}"
    return proc, elapsed, ""


def lock_packages(path: Path) -> dict[str, dict]:
    return {pkg["name"]: pkg for pkg in tomllib.loads(path.read_text("utf-8")).get("package", [])}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--commit", required=True, help="Starling 的 40 位提交")
    ap.add_argument("--workdir", required=True, help="空的临时目录(不存在则建)")
    args = ap.parse_args()
    commit = args.commit
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise SystemExit("--commit 要 40 位小写十六进制")
    workdir = Path(args.workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    if any(workdir.iterdir()):
        raise SystemExit(f"--workdir 不是空目录: {workdir}")

    results: dict[int, str] = {}  # 检查项下标 -> "" 表示 OK,否则是 FAIL 的原因

    def info(text: str) -> None:
        print(f"INFO {text}", flush=True)

    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("找不到 uv")
    info(subprocess.run([uv, "--version"], capture_output=True, text=True).stdout.strip())
    info(f"commit {commit}")
    shutil.copy2(REPO / "uv.lock", workdir / "uv.lock")
    (workdir / "pyproject.toml").write_text(
        replace_memory_extra((REPO / "pyproject.toml").read_text("utf-8"), commit), "utf-8")
    before = set(lock_packages(workdir / "uv.lock"))

    # 1 锁:两项都从同一仓库、同一提交来,客户端带子目录
    _, elapsed, problem = run([uv, "lock"], workdir, LOCK_TIMEOUT_S)
    info(f"uv lock {elapsed:.1f} s")
    if not problem:
        pkgs = lock_packages(workdir / "uv.lock")
        added = sorted(set(pkgs) - before)
        info(f"uv lock added {len(added)} packages: {', '.join(added)}")
        for name, sub in (("starling-client", True), ("starling-memory", False)):
            source = (pkgs.get(name) or {}).get("source", {}).get("git", "")
            info(f"lock {name} {(pkgs.get(name) or {}).get('version')} {source}")
            # uv 把子目录写成查询参数并转义斜杠(subdirectory=clients%2Fpython),按查询串解析再比
            query = parse_qs(urlsplit(source).query)
            if not source.endswith(f"#{commit}") or query.get("rev") != [commit]:
                problem = f"{name} not locked at {commit} (source={source!r})"
            elif query.get("subdirectory") != (["clients/python"] if sub else None):
                problem = f"{name}: subdirectory={query.get('subdirectory')!r} (source={source!r})"
        memory = (lock_packages(workdir / "uv.lock").get("rise-controlplane") or {}).get(
            "optional-dependencies", {}).get("memory", [])
        if not problem and not any(d.get("name") == "starling-memory" and "dashboard" in (d.get("extra") or [])
                                   for d in memory):
            problem = "extra memory does not pull starling-memory[dashboard]"
    results[0] = problem

    # 2 装:uv sync 成功,httpx2 在锁里来自 PyPI(探针再确认它装上了)
    probe: dict | None = None
    if not results[0]:
        _, elapsed, problem = run([uv, "sync", "--frozen", "--extra", "memory", "--no-install-project"],
                                  workdir, SYNC_TIMEOUT_S)
        info(f"uv sync --frozen --extra memory --no-install-project {elapsed:.1f} s")
        if not problem:
            src = (lock_packages(workdir / "uv.lock").get("httpx2") or {}).get("source", {})
            if src.get("registry") != "https://pypi.org/simple":
                problem = f"httpx2 not locked from PyPI (source={src!r})"
        results[1] = problem

        if not problem:
            data_dir, home = workdir / "data", workdir / "home"
            data_dir.mkdir()
            home.mkdir()
            token_file = workdir / "token"
            fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(secrets.token_urlsafe(32) + "\n")
            try:
                proc, elapsed, problem = run(
                    [uv, "run", "--project", str(workdir), "--no-sync", "python", "-I", "-c", PROBE,
                     str(data_dir), str(home), str(token_file), str(workdir / "service-stderr.log")],
                    workdir, PROBE_TIMEOUT_S)
            finally:
                token_file.unlink(missing_ok=True)
            info(f"probe {elapsed:.1f} s")
            if problem:
                results[1] = f"probe failed: {problem}"
            else:
                probe = json.loads(proc.stdout.strip().splitlines()[-1])

    if probe is not None:
        info(f"probe python {probe['python']}")
        httpx2 = probe.get("httpx2")
        if httpx2 is None:
            results[1] = "httpx2 not installed"
        elif httpx2["direct_url"] is not None:
            results[1] = f"httpx2 installed from a direct URL: {httpx2['direct_url']!r}"
        else:
            info(f"httpx2=={httpx2['version']}")

        # 3 两份 direct_url.json
        problem = ""
        for name, sub in (("starling-client", "clients/python"), ("starling-memory", None)):
            dist = probe.get(name)
            if dist is None or dist["direct_url"] is None:
                problem = f"{name}: no direct_url.json"
                break
            du = dist["direct_url"]
            info(f"{name} {dist['version']} direct_url {json.dumps(du, sort_keys=True)}")
            if (du.get("vcs_info") or {}).get("commit_id") != commit:
                problem = f"{name}: vcs_info.commit_id={(du.get('vcs_info') or {}).get('commit_id')!r}"
            elif du.get("subdirectory") != sub:
                problem = f"{name}: subdirectory={du.get('subdirectory')!r}"
        results[2] = problem

        # 4 客户端的提交
        results[3] = "" if probe["client_commit"] == commit else f"COMMIT={probe['client_commit']!r}"

        # 5 服务报的提交与协议
        if "info" in probe:
            got = probe["info"]
            info(f"service info commit={got['commit']} version={got['version']} protocol={got['protocol']} "
                 f"default_settings keys={','.join(got['default_settings_keys'])}")
            if got["commit"] != commit:
                results[4] = f"info()['commit']={got['commit']!r}"
            elif got["protocol"] != probe["client_protocol"]:
                results[4] = f"protocol {got['protocol']!r} != client PROTOCOL {probe['client_protocol']!r}"
            else:
                results[4] = ""
        else:
            results[4] = f"info() failed: {probe.get('info_error', 'service not ready')}"

        # 6 Transport 签名
        info(f"Transport.__init__{probe['sig_init']}")
        info(f"Transport.post{probe['sig_post']}")
        if probe["sig_init"] != EXPECTED_INIT:
            results[5] = f"__init__ signature changed: {probe['sig_init']}"
        elif probe["sig_post"] != EXPECTED_POST:
            results[5] = f"post signature changed: {probe['sig_post']}"
        elif not probe["transport_exported"]:
            results[5] = "Transport not exported as starling_client.Transport"
        else:
            results[5] = ""

        # 7 不带会话的管理调用
        if "admin" in probe:
            info(f"admin list_worlds total={probe['admin']['total']}; ready {probe.get('ready_s')} s; "
                 f"SIGTERM exit {probe.get('sigterm_exit')}; files under temp HOME: {len(probe['home_files'])}")
            results[6] = ""
        else:
            results[6] = f"list_worlds failed: {probe.get('admin_error', 'service not ready')}"

    failed = False
    for index, name in enumerate(CHECKS):
        problem = results.get(index, "not run: an earlier check failed")
        if problem:
            failed = True
            print(f"FAIL {name}: {problem}")
        else:
            print(f"OK {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
