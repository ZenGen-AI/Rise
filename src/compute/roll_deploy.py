"""把钉住的 ROLL 与运行环境放到远端主机上,并起好训练容器(真实后端的 ROLL 集成)。

全部经宿主机传输(`ComputeClient.host_run`),不经 launch_prefix:容器是这一步自己建的,
建之前没有容器可进;work_root 在宿主机与容器里同路径挂载,宿主机写进去的文件容器里看得到。

顺序与失败分类:
1. 镜像在不在、依赖副本的指纹对不对:不对就是 ComputeError(确定性,要人处理)。
2. ROLL 部署目录 `<work_root>/deps/roll-<commit12>`:`.deployed` 记着提交、目录指纹(去掉标记)等于归档的指纹
   就跳过;否则解进 `<目录>.tmp-<hex8>`,写标记,原子改名。改名撞上已存在的目录:那份是对的(别的进程先到)
   就删掉自己的 tmp 复用它,不对就抛错要人清。装完再核对一次指纹。
3. 容器 `forge-roll-<规格摘要>`:规格摘要由 docker run 的完整参数算出,参数(镜像、挂载、环境变量)任何一项变了
   就是另一个容器。不存在就建;存在没在跑就 start;标签不符就抛错。只碰这个名字,从不停、不删任何容器。
4. 导入自检:容器里 `import roll, mcore_adapter, megatron.core`,三者必须来自部署目录与依赖挂载点。
SSH 断开(TransportError)原样上抛,是延后类。

每个进 argv 的路径都过 is_clean_abs_path;ROLL 目录还要在 work_root 之下(这条路径不经助手的围栏)。
"""
from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import tarfile
import uuid
from dataclasses import dataclass
from typing import Any

from compute.endpoint import ComputeEndpoint, is_clean_abs_path
from compute.errors import ComputeError
from compute.roll_env import CONTAINER_PREFIX, SFT_TEMPLATE, RollEnv

DEPLOY_MARKER = ".deployed"
SPEC_LABEL = "rismforge.roll-spec"

# 原子改名:目标已存在且非空时 os.rename 失败(ENOTEMPTY/EEXIST),这就是"先到先得"。
# 用宿主机的 python3 而不是 `mv -T`:后者只有 GNU coreutils 有。
RENAME = "import os, sys\nos.rename(sys.argv[1], sys.argv[2])\n"

# 自检程序:固定文本,不拼任何外来值;期望的前缀经 argv 传进去。
SELF_CHECK = (
    "import sys, roll, mcore_adapter, megatron.core\n"
    "roll_dir, megatron_mount = sys.argv[1:3]\n"
    "got = {'roll': roll.__file__, 'mcore_adapter': mcore_adapter.__file__, 'megatron': megatron.core.__file__}\n"
    "want = {'roll': roll_dir + '/', 'mcore_adapter': roll_dir + '/', 'megatron': megatron_mount + '/'}\n"
    "bad = [k for k in got if not got[k].startswith(want[k])]\n"
    "print('ok' if not bad else 'wrong:' + ','.join(bad))\n"
)


@dataclass(frozen=True)
class RollRuntime:
    """部署好之后执行器与转换器用的东西。endpoint 是派生端点:roll_root、sft_template、launch_prefix 换成这次的。"""
    endpoint: ComputeEndpoint
    env: RollEnv
    container: str


class RollRuntimeCache:
    """一个进程里执行器与转换器共用的那份运行时,第一次要用时才部署(部署碰远端、耗时,不放在装配期)。

    build 由组合根给,返回它要缓存的任何东西(factory.wire 返回运行时与绑在派生端点上的客户端)。
    部署失败不缓存:下一次调用再试(延后类的 TransportError 本来就该重试;确定性错误再试也只会再报同一句)。"""

    def __init__(self, build) -> None:
        self._build = build
        self._value = None

    def get(self):
        if self._value is None:
            self._value = self._build()
        return self._value


def tree_digest(archive: bytes) -> str:
    """归档解开之后的 hash-tree full + skip_pycache,与助手 `cmd_hash_tree` 逐字节同一口径(测试钉住)。

    在内存里从 tar 成员算,不落盘:成员名就是相对路径,符号链接哈希链接目标,普通文件哈希内容。"""
    entries = []
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar.getmembers():
            rel = member.name.rstrip("/")
            if "__pycache__" in rel.split("/")[:-1] or rel.endswith((".pyc", ".pyo")):
                continue
            if member.issym():
                entries.append((rel, "link", hashlib.sha256(member.linkname.encode("utf-8", "replace")).hexdigest()))
            elif member.isfile():
                data = tar.extractfile(member).read()
                entries.append((rel, str(len(data)), hashlib.sha256(data).hexdigest()))
    entries.sort()
    manifest = hashlib.sha256()
    for rel, size, digest in entries:
        manifest.update(("%s\0%s\0%s\n" % (rel, size, digest)).encode("utf-8"))
    return manifest.hexdigest()


def ensure_roll_env(client: Any, endpoint: ComputeEndpoint, env: RollEnv, archive: bytes) -> RollRuntime:
    """见模块说明。archive 是 `roll_source.roll_archive` 的结果,env.roll_tree 必须等于它的 tree_digest。"""
    if endpoint.host_deps_root is None:
        raise ComputeError(f"endpoint {endpoint.endpoint_id!r} has no host_deps_root; --executor roll needs it")
    work_root = _clean(endpoint.work_root, "work_root")
    deps_root = _clean(endpoint.host_deps_root, "host_deps_root")
    base = _clean(endpoint.base_model_path, "base_model_path")
    roll_dir = _clean(env.roll_dir(work_root), "the ROLL directory")
    if not roll_dir.startswith(work_root + "/"):
        raise ComputeError("the ROLL directory must be inside work_root")
    if tree_digest(archive) != env.roll_tree:
        raise ComputeError("the ROLL archive does not match the environment it was built for")

    # 1. 镜像与依赖。镜像不在时 docker image inspect 非零退出:与"在但 id 不符"同一句报错
    image = client.host_run(["docker", "image", "inspect", "--format", "{{.Id}}", env.image_id], timeout=120.0)
    if image.returncode != 0 or image.stdout.decode("utf-8", "replace").strip() != env.image_id:
        raise ComputeError(f"image {env.image_id[:19]} is not on the host; load it before training")
    for dep in env.dependencies:
        path = _clean(f"{deps_root}/{dep.name}", f"dependency {dep.name}")
        actual = _host_tree(client, path)
        if actual != dep.fingerprint:
            raise ComputeError(f"{path} does not match the pinned {dep.name} source "
                               f"(want {dep.fingerprint[:12]}, got {actual[:12]})")

    # 2. ROLL 代码
    if not _deployed(client, roll_dir, env):
        _install(client, roll_dir, env, archive)

    # 3. 容器
    container = _ensure_container(client, env, work_root=work_root, base=base, deps_root=deps_root, roll_dir=roll_dir)

    # 4. 导入自检
    megatron = _mount(env, "Megatron-LM")
    verdict = _run(client, ["docker", "exec", container, "python3", "-W", "ignore", "-c", SELF_CHECK,
                            roll_dir, megatron], "the import self-check", timeout=300.0)
    last = verdict.splitlines()[-1] if verdict else ""
    if last != "ok":
        raise ComputeError(f"the ROLL container {container} imports the wrong sources ({last[:120]})")

    derived = dataclasses.replace(
        endpoint, roll_root=roll_dir, sft_template=f"{roll_dir}/{SFT_TEMPLATE}",
        launch_prefix=("docker", "exec", "-i", container))
    return RollRuntime(derived, env, container)


# ---------- 内部 ----------

def _clean(path: str, what: str) -> str:
    if not is_clean_abs_path(path):
        raise ComputeError(f"{what} must be an absolute path without '..'")
    return path.rstrip("/")


def _mount(env: RollEnv, name: str) -> str:
    return next(d.mount for d in env.dependencies if d.name == name)


def _tail(result) -> str:
    lines = result.stderr.decode("utf-8", "replace").strip().splitlines()
    return lines[-1][:200] if lines else ""


def _run(client: Any, argv: list[str], what: str, *, stdin: bytes = b"", timeout: float = 120.0) -> str:
    result = client.host_run(argv, stdin=stdin, timeout=timeout)
    if result.returncode != 0:
        raise ComputeError(f"{what} failed on the host (exit {result.returncode}): {_tail(result)}")
    return result.stdout.decode("utf-8", "replace").strip()


def _host_tree(client: Any, path: str, *, exclude: tuple[str, ...] = ()) -> str:
    """宿主机上目录的 hash-tree full + skip_pycache。

    依赖副本不在 work_root 里,容器又可能还没建,所以在宿主机的 python3 上跑助手自己的那段代码:
    助手字节先过 HELPER_SHA256 校验(helper_bytes),以模块身份执行(它自己的 main 不跑),口径与容器里的
    hash-tree 逐字节相同。路径与 exclude 经 argv 传进去,程序文本是固定的。"""
    from compute.helper import helper_bytes

    loader = ("import json, sys, types\n"
              "m = types.ModuleType('job_tool')\n"
              "m.__file__ = 'job_tool.py'\n"   # 助手在导入期读 __file__(包装进程的自身路径),这里用不到
              "exec(compile(sys.stdin.buffer.read(), 'job_tool.py', 'exec'), m.__dict__)\n"
              "r = m.cmd_hash_tree({'path': sys.argv[1], 'mode': 'full', 'skip_pycache': True,"
              " 'exclude': sys.argv[2:]})\n"
              "sys.stdout.write(json.dumps(r) + '\\n')\n")
    out = _run(client, ["python3", "-c", loader, path, *exclude], f"hashing {path}",
               stdin=helper_bytes(), timeout=1800.0)
    try:
        return str(json.loads(out.splitlines()[-1])["sha256"])
    except (ValueError, KeyError, TypeError, IndexError):
        raise ComputeError(f"hashing {path} returned unparseable output") from None


def _deployed(client: Any, roll_dir: str, env: RollEnv) -> bool:
    """标记写着钉住的提交,且目录(去掉标记本身)的指纹等于归档的指纹。

    目录不在、标记不对、内容被改过一律是 False;哈希程序本身跑不起来(宿主机没有 python3 之类)才抛。"""
    marker = client.host_run(["cat", f"{roll_dir}/{DEPLOY_MARKER}"], timeout=30.0)
    if marker.returncode != 0 or marker.stdout.decode("utf-8", "replace").strip() != env.roll_commit:
        return False
    return _host_tree(client, roll_dir, exclude=(DEPLOY_MARKER,)) == env.roll_tree


def _install(client: Any, roll_dir: str, env: RollEnv, archive: bytes) -> None:
    tmp = f"{roll_dir}.tmp-{uuid.uuid4().hex[:8]}"
    try:
        _run(client, ["mkdir", "-p", tmp], "mkdir")
        _run(client, ["tar", "-x", "-f", "-", "-C", tmp, "--no-same-owner", "--no-same-permissions"],
             "unpacking ROLL", stdin=archive, timeout=600.0)
        _run(client, ["sh", "-c", 'printf %s "$1" > "$2"', "sh", env.roll_commit, f"{tmp}/{DEPLOY_MARKER}"],
             "writing the deploy marker")
        moved = client.host_run(["python3", "-c", RENAME, tmp, roll_dir], timeout=60.0)
    finally:
        client.host_run(["rm", "-rf", "--", tmp], timeout=600.0)   # 改名成功后 tmp 已不在,是空操作
    if not _deployed(client, roll_dir, env):
        raise ComputeError(
            f"cannot install ROLL {env.roll_commit[:12]} at {roll_dir}"
            + (f" (rename exit {moved.returncode}: {_tail(moved)})" if moved.returncode != 0 else "")
            + "; if a directory is in the way, a human should remove it")


def _ensure_container(client: Any, env: RollEnv, *, work_root: str, base: str, deps_root: str,
                      roll_dir: str) -> str:
    # ROLL 目录在 work_root 里面,但再单独只读挂一次(嵌套挂载盖住读写的那层):训练若往 ROLL 树里写,
    # 直接报错,而不是悄悄改掉部署目录的指纹。作业的 cwd 是各自的作业目录(见 roll_executor)。
    pythonpath = ":".join([_mount(env, "fla"), f"{roll_dir}/mcore_adapter/src", roll_dir, _mount(env, "Megatron-LM")])
    body = ["--entrypoint", "sleep", "--gpus", "all", "--network", "host", "--shm-size", env.shm_size,
            "-v", f"{work_root}:{work_root}", "-v", f"{roll_dir}:{roll_dir}:ro", "-v", f"{base}:{base}:ro"]
    for dep in env.dependencies:
        body += ["-v", f"{deps_root}/{dep.name}:{dep.mount}:ro"]
    body += ["-e", f"PYTHONPATH={pythonpath}"]
    for key, value in env.env:
        body += ["-e", f"{key}={value}"]
    body += [env.image_id, "infinity"]
    spec = hashlib.sha256("\0".join(body).encode("utf-8")).hexdigest()[:12]
    name = f"{CONTAINER_PREFIX}{spec}"

    state = client.host_run(["docker", "inspect", "--format",
                             "{{.State.Running}} {{index .Config.Labels \"" + SPEC_LABEL + "\"}}", name], timeout=60.0)
    if state.returncode == 0:
        running, label = (state.stdout.decode("utf-8", "replace").split() + ["", ""])[:2]
        if label != spec:
            raise ComputeError(f"container {name} exists but was not built from this spec; a human should remove it")
        if running != "true":
            _run(client, ["docker", "start", name], "docker start")
        return name
    _run(client, ["docker", "run", "-d", "--name", name, "--label", f"{SPEC_LABEL}={spec}", *body],
         "docker run", timeout=300.0)
    return name
