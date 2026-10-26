"""导入边界:有些模块不该为了被导入而拖进 grpc。

不开 `--interact` 的入口、对手 API 客户端与装配期检查都不需要实时服务端;grpc 的载入有代价,而且一旦被
顺手拖进来,这条边界就没人再记得。每个用例在子进程里只导入一个模块,看 `grpc` 在不在 `sys.modules` 里,
不受本进程里别的测试已经导入了什么的影响。

不断言 `factory.wire`:它经 `mindtrainer.dispatch -> mindtrainer.orchestrator -> mindtrainer.trajectory_source`
本来就会载入 grpc,改动之前就是这样。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"


def _loads_grpc(module: str) -> bool:
    """在干净的子进程里导入 module,返回 grpc 是否因此进了 sys.modules。导入本身失败则让测试失败。"""
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(SRC)!r})\n"
        f"import {module}\n"
        "print('grpc' in sys.modules)\n"
    )
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=False, timeout=60)
    assert out.returncode == 0, f"importing {module} failed: {out.stderr[-500:]}"
    return out.stdout.strip() == "True"


def test_the_probe_can_tell_a_module_that_loads_grpc():
    """对照:factory.agent 直接 import grpc。探测若认不出它,下面两条的"没载入"就是空话。"""
    assert _loads_grpc("factory.agent") is True


@pytest.mark.parametrize("module", ["factory.opponents", "factory.interact_checks"])
def test_importing_it_does_not_load_grpc(module):
    assert _loads_grpc(module) is False
