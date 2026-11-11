"""导入边界:有些模块不该为了被导入而拖进 grpc。

不开 `--interact` 的入口、对手 API 客户端与装配期检查都不需要实时服务端;grpc 的载入有代价,而且一旦被
顺手拖进来,这条边界就没人再记得。每个用例在子进程里只导入一个模块,看 `grpc` 在不在 `sys.modules` 里,
不受本进程里别的测试已经导入了什么的影响。

`factory.cli` 与 `factory.wire` 也在此列:`mindtrainer.dispatch` 经 `mindtrainer.orchestrator ->
mindtrainer.trajectory_source` 载入 grpc 与 `simulator.realtime`,所以 wire 只在真正造派发器时才导入它
(模块层留一个同名的转发函数,测试仍能在 `factory.wire` 上替换)。
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


def _run_probe(body: str) -> subprocess.CompletedProcess:
    """在干净的子进程里跑 body(已把 SRC 放进 sys.path),返回完整结果,由调用方断言。"""
    code = f"import sys\nsys.path.insert(0, {str(SRC)!r})\n" + body
    return subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=False, timeout=120)


def test_the_probe_can_tell_a_module_that_loads_grpc():
    """对照:factory.agent 直接 import grpc。探测若认不出它,下面两条的"没载入"就是空话。"""
    assert _loads_grpc("factory.agent") is True


@pytest.mark.parametrize("module", ["factory.opponents", "factory.interact_checks"])
def test_importing_it_does_not_load_grpc(module):
    assert _loads_grpc(module) is False


# ---------------- factory.cli:不拖进 grpc 与实时服务端,而分派照旧 ----------------

_HEAVY = ("grpc", "simulator.realtime", "mindtrainer.dispatch", "mindtrainer.orchestrator", "mindtrainer.trajectory_source")


@pytest.mark.parametrize("module", ["factory.cli", "factory.wire"])
def test_the_factory_entry_does_not_load_grpc_or_the_realtime_stack(module):
    body = (
        f"import {module}\n"
        f"heavy = {_HEAVY!r}\n"
        "print(sorted(k for k in sys.modules if any(k == h or k.startswith(h + '.') for h in heavy)))\n"
    )
    out = _run_probe(body)
    assert out.returncode == 0, f"importing {module} failed: {out.stderr[-500:]}"
    assert out.stdout.strip() == "[]", f"{module} pulled in {out.stdout.strip()}"


def test_the_cli_help_entry_still_works_without_the_heavy_stack():
    """`python -m factory.cli --help` 的等价调用:argparse 以 0 退出并打印子命令,期间 grpc 没进来。"""
    body = (
        "import runpy\n"
        "sys.argv = ['factory.cli', '--help']\n"
        "try:\n"
        "    runpy.run_module('factory.cli', run_name='__main__')\n"
        "except SystemExit as exc:\n"
        "    print('exit', exc.code)\n"
        "print('grpc', 'grpc' in sys.modules, 'realtime', any(k.startswith('simulator.realtime') for k in sys.modules))\n"
    )
    out = _run_probe(body)
    assert out.returncode == 0, out.stderr[-500:]
    assert "reconcile" in out.stdout and "install-defaults" in out.stdout
    assert out.stdout.splitlines()[-2:] == ["exit 0", "grpc False realtime False"]


def test_a_reconcile_through_main_still_builds_the_dispatcher_lazily(tmp_path):
    """惰性导入没有切断分派:reconcile --once 走 main -> reconcile_one_tick -> assemble,
    此时才载入 mindtrainer.dispatch(及 grpc),并正常退出 0。"""
    root = str(tmp_path / "cp")
    body = (
        "from controlplane.gitstore import GitStore\n"
        "from factory.cli import main\n"
        f"GitStore({root!r}).init()\n"
        "assert 'mindtrainer.dispatch' not in sys.modules\n"
        f"rc = main(['reconcile', {root!r}, '--once'], env={{}})\n"
        "print('rc', rc, 'dispatch', 'mindtrainer.dispatch' in sys.modules)\n"
    )
    out = _run_probe(body)
    assert out.returncode == 0, out.stderr[-500:]
    assert out.stdout.splitlines()[-2:] == ["no actions", "rc 0 dispatch True"]
