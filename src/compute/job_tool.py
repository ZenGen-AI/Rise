"""远端助手(真实后端设计 §3.2、D10):在共享 GPU 主机上安全地跑一个作业。

只依赖标准库,语法写到 Python 3.8;sha256 写死在 compute/helper.py,改这个文件必须同步改那里。

调用:python3 job_tool.py <命令>。命令行上只有命令名;请求是 stdin 里的一个 JSON 对象,应答是 stdout
里的一行 JSON:{"ok": true, ...} 或 {"ok": false, "error": <码>, "detail": <说明>}。错误说明只写字段名,
不回显请求里的取值(密钥会经过这里)。

远端只写 work_root。作业目录 <work_root>/jobs/<job_id>/ 里:
  spec.json   启动规格(不含密钥)         secret      密钥,0600,包装进程读入后立即删除
  proc.json   包装进程的 pid 与启动标记     child.json  被包装进程的 pid 与启动标记
  stdout.log  合并后的输出                exit_code   被包装进程的退出码(被信号杀死时为负数)
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid

# ===== core =====

JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_PUT_BYTES = 16 * 1024 * 1024
SAMPLE_THRESHOLD = 64 * 1024 * 1024
SAMPLE_CHUNK = 1024 * 1024
START_WAIT_S = 5.0
STARTING_GRACE_S = 15.0
DEFAULT_LEASE_GRACE_S = 120.0


COMMANDS = {}


def command(name):
    def register(fn):
        COMMANDS[name] = fn
        return fn
    return register


class Failure(Exception):
    def __init__(self, code, detail=""):
        Exception.__init__(self, code)
        self.code = code
        self.detail = detail


# ---------- 请求字段 ----------

def _str(req, key):
    value = req.get(key)
    if not isinstance(value, str) or not value:
        raise Failure("bad_request", "%s must be a non-empty string" % key)
    return value


def _number(req, key, default, low, high):
    value = req.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not (low <= value <= high):
        raise Failure("bad_request", "%s must be a number in [%s, %s]" % (key, low, high))
    return value


def _work_root(req):
    root = _str(req, "work_root")
    if not os.path.isabs(root) or ".." in root.split("/") or os.path.normpath(root) == "/":
        raise Failure("bad_request", "work_root must be an absolute path below /")
    return os.path.normpath(root)


def _job_id(req, key="job_id"):
    value = _str(req, key)
    if not JOB_ID_RE.match(value):
        raise Failure("bad_request", "%s has an invalid form" % key)
    return value


def _job_dir(root, job_id):
    return os.path.join(root, "jobs", job_id)


def _abs_path_inside(root, req, key="path"):
    path = _str(req, key)
    if not os.path.isabs(path) or ".." in path.split("/"):
        raise Failure("bad_path", "%s must be an absolute path without '..'" % key)
    real_root = os.path.realpath(root)
    real = os.path.realpath(path)
    if real != real_root and not real.startswith(real_root + os.sep):
        raise Failure("bad_path", "%s is outside work_root" % key)
    return path


# ---------- 文件 ----------

def _atomic_write(path, data, mode=0o644, sync=False):
    if isinstance(data, str):
        data = data.encode("utf-8")
    tmp = "%s.tmp-%d-%s" % (path, os.getpid(), uuid.uuid4().hex[:8])
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            if sync:
                os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        _unlink(tmp)
        raise


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _read_text(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except (OSError, ValueError):
        return None


def _read_json(path):
    text = _read_text(path)
    if text is None:
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _age(path):
    try:
        return time.time() - os.stat(path).st_mtime
    except OSError:
        return 0.0


def _wait_for(predicate, timeout, step=0.05):
    deadline = time.time() + timeout
    while True:
        value = predicate()
        if value or time.time() >= deadline:
            return value
        time.sleep(step)


# ---------- 进程身份(防 pid 复用) ----------

def _parse_proc_stat(text):
    """/proc/<pid>/stat → (状态, 启动时刻)。comm 字段可含空格与括号,所以从最后一个 ')' 之后切。"""
    rest = text[text.rindex(")") + 1:].split()
    return rest[0], rest[19]


def _proc_marker(pid):
    """进程的启动标记;进程不存在或已是僵尸时返回 None。Linux 读 /proc,其余平台(开发机)用 ps。"""
    if os.path.exists("/proc/self/stat"):
        try:
            with open("/proc/%d/stat" % pid) as handle:
                state, start = _parse_proc_stat(handle.read())
        except (OSError, ValueError, IndexError):
            return None
        return None if state in ("Z", "X") else "p" + start
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    try:
        out = subprocess.run(["ps", "-o", "stat=,lstart=", "-p", str(pid)], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, env=env, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    parts = out.stdout.decode("ascii", "replace").split(None, 1)
    if out.returncode != 0 or len(parts) < 2 or parts[0].startswith("Z"):
        return None
    return "s" + " ".join(parts[1].split())


def _record_alive(record):
    if not record or not isinstance(record.get("pid"), int) or not record.get("start"):
        return False
    return _proc_marker(record["pid"]) == record["start"]


def _exit_code(jobdir):
    text = _read_text(os.path.join(jobdir, "exit_code"))
    try:
        return int(text.strip())
    except (AttributeError, ValueError):
        return None


def _job_state(jobdir):
    """→ (state, exit_code);state 是 missing、running、exited、lost 之一(Ruling 3)。"""
    if not os.path.isdir(jobdir):
        return "missing", None
    code = _exit_code(jobdir)
    if code is not None:
        return "exited", code
    wrapper = _read_json(os.path.join(jobdir, "proc.json"))
    child = _read_json(os.path.join(jobdir, "child.json"))
    if _record_alive(wrapper) or _record_alive(child):
        return "running", None
    code = _exit_code(jobdir)  # 两个进程刚好在两次检查之间收尾
    if code is not None:
        return "exited", code
    if wrapper is None and _age(jobdir) < STARTING_GRACE_S:
        return "running", None
    return "lost", None


def _spec_digest(spec):
    blob = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _status_result(jobdir):
    state, code = _job_state(jobdir)
    spec = _read_json(os.path.join(jobdir, "spec.json"))
    return {"state": state, "exit_code": code, "spec_sha256": _spec_digest(spec) if spec else None}

# ===== jobs =====

SELF = os.path.abspath(__file__)
WRAPPER_ENV = "RISMFORGE_JOBDIR"


def _ignore_signal(signum, frame):
    # 不用 SIG_IGN:被忽略的信号会被 exec 出的子进程继承,子进程就杀不死了。函数处理器在 exec 时复位为默认。
    pass


def _parse_spec(spec):
    if not isinstance(spec, dict):
        raise Failure("bad_request", "spec must be an object")
    extra = sorted(set(spec) - {"argv", "cwd", "env", "secret_env"})
    if extra:
        raise Failure("bad_request", "spec has unknown keys: " + ",".join(str(k) for k in extra))
    argv = spec.get("argv")
    if (not isinstance(argv, list) or not argv or not all(isinstance(a, str) and "\0" not in a for a in argv)
            or not argv[0]):
        raise Failure("bad_request", "spec.argv must be a non-empty list of strings")
    cwd = spec.get("cwd")
    if cwd is not None and (not isinstance(cwd, str) or not os.path.isabs(cwd) or "\0" in cwd):
        raise Failure("bad_request", "spec.cwd must be an absolute path")
    env = spec.get("env", {})
    if not isinstance(env, dict) or not all(
            isinstance(k, str) and ENV_NAME_RE.match(k) and isinstance(v, str) and "\0" not in v
            for k, v in env.items()):
        raise Failure("bad_request", "spec.env must map variable names to strings")
    secret_env = spec.get("secret_env")
    if secret_env is not None and (not isinstance(secret_env, str) or not ENV_NAME_RE.match(secret_env)
                                   or secret_env in env):
        raise Failure("bad_request", "spec.secret_env must be a variable name not present in spec.env")
    return {"argv": argv, "cwd": cwd, "env": env, "secret_env": secret_env}


def _write_exit(jobdir, code):
    _atomic_write(os.path.join(jobdir, "exit_code"), str(code), sync=True)


def _note(jobdir, text):
    with open(os.path.join(jobdir, "stdout.log"), "ab") as log:
        log.write((text + "\n").encode("utf-8"))


def _run_wrapped(jobdir):
    spec = _read_json(os.path.join(jobdir, "spec.json"))
    me = os.getpid()
    _atomic_write(os.path.join(jobdir, "proc.json"), json.dumps({"pid": me, "start": _proc_marker(me)}))
    stop_flag = os.path.join(jobdir, "stop_requested")
    secret_path = os.path.join(jobdir, "secret")
    if spec is None:
        _note(jobdir, "spec.json is unreadable")
        return _write_exit(jobdir, 126)
    if os.path.exists(stop_flag):
        _unlink(secret_path)
        return _write_exit(jobdir, -signal.SIGTERM)
    env = dict(os.environ)
    env.update(spec["env"])
    if spec["secret_env"]:
        secret = _read_text(secret_path)
        if secret is None:
            _note(jobdir, "the secret file is missing")
            return _write_exit(jobdir, 125)
        env[spec["secret_env"]] = secret
    with open(os.path.join(jobdir, "stdout.log"), "ab") as log:
        try:
            child = subprocess.Popen(spec["argv"], cwd=spec["cwd"], env=env, stdin=subprocess.DEVNULL,
                                     stdout=log, stderr=subprocess.STDOUT, close_fds=True)
        except OSError as exc:
            log.write(("cannot launch the job: %s\n" % type(exc).__name__).encode("utf-8"))
            _unlink(secret_path)
            return _write_exit(jobdir, 127)
    _unlink(secret_path)  # 密钥只活到子进程拉起(Ruling 4)
    marker = _proc_marker(child.pid)
    if marker:
        _atomic_write(os.path.join(jobdir, "child.json"), json.dumps({"pid": child.pid, "start": marker}))
    if os.path.exists(stop_flag):
        child.terminate()
    _write_exit(jobdir, child.wait())


def _wrapper_main():
    signal.signal(signal.SIGTERM, _ignore_signal)
    signal.signal(signal.SIGHUP, _ignore_signal)
    jobdir = os.environ.pop(WRAPPER_ENV, "")
    if not jobdir:
        return 2
    try:
        _run_wrapped(jobdir)
    except BaseException as exc:  # 包装进程自己出错:留一行类名,写一个退出码,别让作业永远显示 running
        try:
            _note(jobdir, "wrapper error: %s" % type(exc).__name__)
            if _exit_code(jobdir) is None:
                _write_exit(jobdir, 126)
        except OSError:
            pass
    return 0


@command("start")
def cmd_start(req):
    root = _work_root(req)
    job_id = _job_id(req)
    spec = _parse_spec(req.get("spec"))
    secret = req.get("secret")
    if secret is not None and (not isinstance(secret, str) or not secret or "\0" in secret):
        raise Failure("bad_request", "secret must be a non-empty string")
    if secret is not None and not spec["secret_env"]:
        raise Failure("bad_request", "secret requires spec.secret_env")
    jobdir = _job_dir(root, job_id)
    digest = _spec_digest(spec)
    os.makedirs(os.path.join(root, "jobs"), exist_ok=True)
    for _ in range(2):
        try:
            os.mkdir(jobdir, 0o700)
        except FileExistsError:
            existing = _wait_for(lambda: _read_json(os.path.join(jobdir, "spec.json")), START_WAIT_S)
            if existing is not None:
                if _spec_digest(existing) != digest:
                    raise Failure("spec_conflict", "the job exists with a different spec")
                return _status_result(jobdir)  # 幂等:同一规格的重复 start 不再拉起第二份
            if _age(jobdir) < STARTING_GRACE_S:
                raise Failure("job_starting", "another start of this job is still in progress")
            shutil.rmtree(jobdir, ignore_errors=True)  # 上一次 start 在写 spec 之前崩了
            continue
        break
    else:
        raise Failure("job_starting", "another start of this job is still in progress")
    if spec["secret_env"] and secret is None:
        shutil.rmtree(jobdir, ignore_errors=True)
        raise Failure("bad_request", "spec.secret_env is set but no secret was given")
    _atomic_write(os.path.join(jobdir, "spec.json"), json.dumps(spec, sort_keys=True))
    if secret is not None:
        _atomic_write(os.path.join(jobdir, "secret"), secret, mode=0o600)
    env = dict(os.environ)
    env[WRAPPER_ENV] = jobdir
    try:
        subprocess.Popen([sys.executable, SELF, "_wrap"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, env=env, close_fds=True, start_new_session=True)
    except OSError as exc:
        _unlink(os.path.join(jobdir, "secret"))
        raise Failure("launch_failed", type(exc).__name__)
    _wait_for(lambda: os.path.exists(os.path.join(jobdir, "child.json"))
              or os.path.exists(os.path.join(jobdir, "exit_code")), START_WAIT_S)
    return _status_result(jobdir)


@command("status")
def cmd_status(req):
    return _status_result(_job_dir(_work_root(req), _job_id(req)))


@command("tail")
def cmd_tail(req):
    jobdir = _job_dir(_work_root(req), _job_id(req))
    if not os.path.isdir(jobdir):
        raise Failure("not_found", "no such job")
    lines = int(_number(req, "lines", 50, 1, 5000))
    try:
        with open(os.path.join(jobdir, "stdout.log"), "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 1024 * 1024))
            data = handle.read()
    except OSError:
        data = b""
    return {"text": "\n".join(data.decode("utf-8", "replace").splitlines()[-lines:])}


def _signal_job(wrapper, child, sig):
    """只向启动标记仍匹配的进程发信号(防 pid 复用);包装进程活着就发整个进程组。"""
    try:
        if _record_alive(wrapper):
            os.killpg(wrapper["pid"], sig)
            return True
        if _record_alive(child):
            os.kill(child["pid"], sig)
            return True
    except OSError:
        pass
    return False


@command("stop")
def cmd_stop(req):
    root = _work_root(req)
    jobdir = _job_dir(root, _job_id(req))
    grace = _number(req, "grace_s", 10, 0, 600)
    if not os.path.isdir(jobdir):
        return {"state": "missing", "exit_code": None, "spec_sha256": None, "signaled": False}
    signaled = False
    if _job_state(jobdir)[0] == "running":
        _atomic_write(os.path.join(jobdir, "stop_requested"), "1")  # 包装进程还没拉起子进程时,靠它拦住
        wrapper = _read_json(os.path.join(jobdir, "proc.json"))
        child = _read_json(os.path.join(jobdir, "child.json"))
        signaled = _signal_job(wrapper, child, signal.SIGTERM)
        if not _wait_for(lambda: _job_state(jobdir)[0] != "running", grace):
            if _record_alive(child):
                _signal_job(None, child, signal.SIGKILL)
            if not _wait_for(lambda: _job_state(jobdir)[0] != "running", 3.0) and _record_alive(wrapper):
                _signal_job(wrapper, None, signal.SIGKILL)
                _wait_for(lambda: _job_state(jobdir)[0] != "running", 3.0)
    _unlink(os.path.join(jobdir, "secret"))
    result = _status_result(jobdir)
    result["signaled"] = signaled
    return result

# ===== files =====

@command("put")
def cmd_put(req):
    root = _work_root(req)
    path = _abs_path_inside(root, req)
    expected = _str(req, "sha256")
    try:
        data = base64.b64decode(_str(req, "content_b64").encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError):
        raise Failure("bad_request", "content_b64 is not valid base64")
    if len(data) > MAX_PUT_BYTES:
        raise Failure("too_large", "content exceeds %d bytes" % MAX_PUT_BYTES)
    digest = hashlib.sha256(data).hexdigest()
    if digest != expected:
        raise Failure("hash_mismatch", "the content does not match the given sha256")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _atomic_write(path, data, mode=0o755 if req.get("executable") is True else 0o644, sync=True)
    return {"size": len(data), "sha256": digest}


MAX_GET_BYTES = 1 << 20  # 完成标记、tracker 这类小文件;大文件不走这条路


@command("get")
def cmd_get(req):
    root = _work_root(req)
    path = _abs_path_inside(root, req)
    if not os.path.isfile(path):
        raise Failure("not_found", "no such file")
    size = os.lstat(path).st_size
    if size > MAX_GET_BYTES:
        raise Failure("too_large", "file exceeds %d bytes" % MAX_GET_BYTES)
    with open(path, "rb") as handle:
        data = handle.read()
    return {"content_b64": base64.b64encode(data).decode("ascii"),
            "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


@command("rename")
def cmd_rename(req):
    """os.replace:同一文件系统内原子改名。目标已存在就拒绝,不覆盖别人的产物。"""
    root = _work_root(req)
    src = _abs_path_inside(root, req, key="src")
    dst = _abs_path_inside(root, req, key="dst")
    if not os.path.exists(src):
        raise Failure("not_found", "no such source path")
    if os.path.exists(dst):
        raise Failure("exists", "the destination already exists")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    os.replace(src, dst)
    return {"renamed": True}


@command("rm-tmp")
def cmd_rm_tmp(req):
    """只删形如 <...>.tmp-<十六进制> 的目录:调用方自己造的未完成产物。

    删除是不可逆的,所以不接受任意路径:名字不合这个形状一律 bad_path。
    """
    root = _work_root(req)
    path = _abs_path_inside(root, req)
    name = os.path.basename(path.rstrip("/"))
    if ".tmp-" not in name or not name.rsplit(".tmp-", 1)[1].isalnum():
        raise Failure("bad_path", "rm-tmp only removes directories named '<name>.tmp-<token>'")
    if not os.path.isdir(path):
        return {"removed": False}
    shutil.rmtree(path)
    return {"removed": True}


def _digest_file(path, size, mode):
    """full:整个文件;sampled:小于 SAMPLE_THRESHOLD 的整体哈希,更大的取头、中、尾各 SAMPLE_CHUNK(Ruling 6)。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        if mode == "full" or size < SAMPLE_THRESHOLD:
            for block in iter(lambda: handle.read(SAMPLE_CHUNK), b""):
                digest.update(block)
        else:
            for offset in (0, (size - SAMPLE_CHUNK) // 2, size - SAMPLE_CHUNK):
                handle.seek(offset)
                digest.update(handle.read(SAMPLE_CHUNK))
    return digest.hexdigest()


@command("hash-tree")
def cmd_hash_tree(req):
    top = _str(req, "path")
    if not os.path.isabs(top) or ".." in top.split("/"):
        raise Failure("bad_path", "path must be an absolute path without '..'")
    mode = req.get("mode")
    if mode not in ("sampled", "full"):
        raise Failure("bad_request", "mode must be 'sampled' or 'full'")
    exclude = req.get("exclude", [])
    if not isinstance(exclude, list) or not all(isinstance(name, str) and name for name in exclude):
        raise Failure("bad_request", "exclude must be a list of relative file names")
    if not os.path.isdir(top):
        raise Failure("not_found", "no such directory")
    entries = []
    for base, dirs, names in os.walk(top, followlinks=False):
        dirs.sort()
        for name in sorted(names):
            full = os.path.join(base, name)
            rel = os.path.relpath(full, top).replace(os.sep, "/")
            if rel in exclude:
                continue
            info = os.lstat(full)
            if os.path.islink(full):
                entries.append((rel, "link", hashlib.sha256(os.readlink(full).encode("utf-8", "replace")).hexdigest()))
            elif os.path.isfile(full):
                entries.append((rel, str(info.st_size), _digest_file(full, info.st_size, mode)))
    entries.sort()
    manifest = hashlib.sha256()
    for rel, size, digest in entries:
        manifest.update(("%s\0%s\0%s\n" % (rel, size, digest)).encode("utf-8"))
    total = sum(int(size) for _, size, _ in entries if size != "link")
    return {"sha256": manifest.hexdigest(), "files": len(entries), "bytes": total}

# ===== lease =====

LEASE_NAME = "gpu.lease"


def _lease_path(root):
    return os.path.join(root, LEASE_NAME)


def _holder_stale(root, holder, lease_age, grace):
    """Ruling 13:持有者的作业已 exited 或 lost 即陈旧;作业目录不存在或租约内容为空时,只在超过宽限期后才算陈旧。"""
    if not holder or not JOB_ID_RE.match(holder):
        return lease_age > grace
    jobdir = _job_dir(root, holder)
    if not os.path.isdir(jobdir):
        return lease_age > grace
    return _job_state(jobdir)[0] in ("exited", "lost")


def _take_lease(root):
    """把租约文件 rename 到私有名字再读:别的申请者同时回收时只有一个 rename 成功。返回 (私有路径, 内容) 或 None。"""
    taken = os.path.join(root, "%s.stale-%s" % (LEASE_NAME, uuid.uuid4().hex[:12]))
    try:
        os.rename(_lease_path(root), taken)
    except OSError:
        return None
    return taken, (_read_text(taken) or "").strip()


def _put_back(taken, root):
    """原样放回被误取的租约。目标已被别人重新创建时(极窄的竞态)不覆盖,私有文件留作证据,调用方按忙处理。"""
    try:
        os.link(taken, _lease_path(root))
    except OSError:
        return False
    _unlink(taken)
    return True


def _create_lease(root, holder):
    try:
        fd = os.open(_lease_path(root), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as handle:
        handle.write(holder)
    return True


@command("lease-acquire")
def cmd_lease_acquire(req):
    root = _work_root(req)
    holder = _job_id(req, "holder")
    grace = _number(req, "grace_s", DEFAULT_LEASE_GRACE_S, 0, 86400)
    os.makedirs(root, exist_ok=True)
    if _create_lease(root, holder):
        return {"acquired": True, "holder": holder, "reclaimed": False}
    current = (_read_text(_lease_path(root)) or "").strip()
    if current == holder:
        return {"acquired": True, "holder": holder, "reclaimed": False}
    if not _holder_stale(root, current, _age(_lease_path(root)), grace):
        return {"acquired": False, "holder": current or None, "reclaimed": False}
    taken = _take_lease(root)
    if taken is None:  # 别人先一步回收了
        return {"acquired": False, "holder": None, "reclaimed": False}
    path, seen = taken
    if seen != current:  # 取到的不是判定陈旧的那份:放回,按忙处理
        _put_back(path, root)
        return {"acquired": False, "holder": seen or None, "reclaimed": False}
    _unlink(path)
    if _create_lease(root, holder):
        return {"acquired": True, "holder": holder, "reclaimed": True}
    return {"acquired": False, "holder": None, "reclaimed": True}


@command("lease-release")
def cmd_lease_release(req):
    root = _work_root(req)
    holder = _job_id(req, "holder")
    taken = _take_lease(root)
    if taken is None:
        return {"released": False, "holder": None}
    path, seen = taken
    if seen == holder:
        _unlink(path)
        return {"released": True, "holder": holder}
    _put_back(path, root)
    return {"released": False, "holder": seen or None}


@command("lease-peek")
def cmd_lease_peek(req):
    root = _work_root(req)
    grace = _number(req, "grace_s", DEFAULT_LEASE_GRACE_S, 0, 86400)
    path = _lease_path(root)
    text = _read_text(path)
    if text is None:
        return {"holder": None, "stale": False}
    holder = text.strip()
    return {"holder": holder or None, "stale": _holder_stale(root, holder, _age(path), grace)}


# ===== probe =====

@command("probe")
def cmd_probe(req):
    root = _work_root(req)
    mem = None
    try:
        with open("/proc/meminfo") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    mem = round(int(line.split()[1]) / (1024.0 * 1024.0), 3)  # kB → GiB
    except (OSError, ValueError, IndexError):
        mem = None
    probe_path = root
    while not os.path.exists(probe_path) and probe_path != "/":
        probe_path = os.path.dirname(probe_path)
    return {"mem_available_gib": mem,
            "disk_free_gib": round(shutil.disk_usage(probe_path).free / float(1 << 30), 3)}


# ===== entry =====

MAX_REQUEST_BYTES = 32 * 1024 * 1024


def _emit(payload):
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def main(argv):
    if len(argv) == 2 and argv[1] == "_wrap":
        return _wrapper_main()
    if len(argv) != 2 or argv[1] not in COMMANDS:
        _emit({"ok": False, "error": "unknown_command", "detail": "expected one of: " + ", ".join(sorted(COMMANDS))})
        return 0
    try:
        req = json.loads(sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1).decode("utf-8") or "null")
        if not isinstance(req, dict):
            raise Failure("bad_request", "the request must be a JSON object")
        _emit(dict(COMMANDS[argv[1]](req), ok=True))
    except Failure as failure:
        _emit({"ok": False, "error": failure.code, "detail": failure.detail})
    except ValueError:
        _emit({"ok": False, "error": "bad_request", "detail": "the request is not valid JSON"})
    except Exception as exc:  # 请求里可能有密钥:异常信息不回显,只给类名
        _emit({"ok": False, "error": "internal", "detail": type(exc).__name__})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
