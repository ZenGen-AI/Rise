"""run id 的分配(设计 §4.1):格式 run-NNN,至少三位补零,取现有最大序号加一。

只数形如 run-<数字> 的 run 目录;人工起的别的名字(exp-a、run-1b、zz)不参与。
计划 8 的 plan_next_round 与这里同一套数值语义(设计 §3.2):只有 run-<数字> 参与比较,按编号取最大;
补零只为可读,超过 999 轮(run-1000)仍按数值排在 run-999 之后。
"""
from __future__ import annotations

import re
from collections.abc import Iterable

from controlplane.gitstore import GitStore
from controlplane.paths import CONTROL_PLANE_ROOT
from trainscheduler.errors import RunIdOrderError

_RUN_ID = re.compile(r"^run-([0-9]+)\Z")


def run_number(name: str) -> int | None:
    """run-007 -> 7;不是 run-<数字> 的形状返回 None。"""
    match = _RUN_ID.match(name) if isinstance(name, str) else None
    return int(match.group(1)) if match else None


def format_run_id(n: int) -> str:
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise ValueError(f"run number must be a positive int, got {n!r}")
    return f"run-{n:03d}"


def next_run_id(existing: Iterable[str]) -> str:
    """纯函数:已有名字里最大的 run 序号加一;一个都没有就是 run-001。"""
    numbers = [n for n in (run_number(name) for name in existing) if n is not None]
    return format_run_id(max(numbers, default=0) + 1)


def list_run_dirs(store: GitStore) -> list[str]:
    root = store.repo_root / CONTROL_PLANE_ROOT / "runs"
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def blocking_run_dirs(existing: Iterable[str], new_id: str) -> list[str]:
    """纯函数:编号不小于 new_id 编号的 run-<数字> 目录(按名字排序)。其他名字不参与,永不挡路。
    new_id 必须是 run-<数字>,否则 ValueError。"""
    new_n = run_number(new_id)
    if new_n is None:
        raise ValueError(f"new_id must be run-<digits>, got {new_id!r}")
    return sorted(name for name in existing if (n := run_number(name)) is not None and n >= new_n)


def allocate_run_id(store: GitStore) -> str:
    """扫描 runs/ 下的目录,返回下一个 run id。不创建目录:目录随指令一起出现。

    防御性检查:若已有 run-<数字> 目录的编号不小于新编号,抛 RunIdOrderError(确定性失败),信息里列出
    这些目录。next_run_id 取最大编号加一,所以正常路径走不到;走到说明树被人手工改过
    (例如有人绕过分配器手建了同号目录),宁可失败留痕也不覆盖。"""
    existing = list_run_dirs(store)
    new_id = next_run_id(existing)
    blocking = blocking_run_dirs(existing, new_id)
    if blocking:
        raise RunIdOrderError(
            f"next run id {new_id} is not numerically greater than existing run dir(s) {blocking}; "
            "the tree was edited by hand, refusing to allocate"
        )
    return new_id


def run_order_key(name: str) -> tuple[int, int, str]:
    """窗口取"最近 K 轮"时的排序键:run-<数字> 按编号数值排在后面;别的名字排在前面(彼此按名字排)。"""
    n = run_number(name)
    return (1, n, name) if n is not None else (0, 0, name)
