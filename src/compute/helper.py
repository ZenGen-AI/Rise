"""远端助手的字节、写死的 sha256、以及把它送上远端的引导脚本。

改 job_tool.py 必须同步改 HELPER_SHA256(测试会拦住),等于每次改动都过一遍评审(设计 §3.2)。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from compute.errors import ComputeError

HELPER_SHA256 = "19954982d86ef3605d0de481533e71b54bcb95981f329d63aa2ec3f2f224b83d"
_HELPER_FILE = Path(__file__).with_name("job_tool.py")

# 引导脚本只做一件事:把 stdin 里的助手原子地写到指定路径,哈希不符就不写。已有同哈希文件则什么都不写。
# 路径与哈希走位置参数,脚本里没有任何可被 shell 解释的数据。
BOOTSTRAP = (
    "import hashlib, os, sys, uuid\n"
    "d, p, h = sys.argv[1:4]\n"
    "b = sys.stdin.buffer.read()\n"
    "if hashlib.sha256(b).hexdigest() != h: sys.exit(3)\n"
    "if os.path.exists(p) and hashlib.sha256(open(p, 'rb').read()).hexdigest() == h: sys.exit(0)\n"
    "os.makedirs(d, exist_ok=True)\n"
    "t = p + '.tmp-' + uuid.uuid4().hex[:8]\n"
    "f = open(t, 'wb'); f.write(b); f.flush(); os.fsync(f.fileno()); f.close()\n"
    "os.chmod(t, 0o755); os.replace(t, p)\n"
)


def helper_bytes() -> bytes:
    data = _HELPER_FILE.read_bytes()
    if hashlib.sha256(data).hexdigest() != HELPER_SHA256:
        raise ComputeError("job_tool.py does not match the pinned sha256 in compute/helper.py")
    return data


def remote_helper_path(work_root: str) -> str:
    return f"{work_root.rstrip('/')}/bin/job_tool-{HELPER_SHA256[:8]}.py"
