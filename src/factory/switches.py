"""--enable 开关到处理器键的映射(闭环集成设计 §10.2)。

开关是给人用的五个短名;处理器键是 controlplane.reconcile.CLOSED_LOOP_ACTIONS 里的六个动作名。
admit 一个开关同时启用两类收录(配方候选与技能提案),因为两者都是"门已批准 → 写进正式库"。
"""
from __future__ import annotations

ENABLE_MAP: dict[str, tuple[str, ...]] = {
    "plan": ("plan_next_round",),
    "nominate": ("nominate_recipe",),
    "track": ("record_track",),
    "agent": ("start_agent_round",),
    "admit": ("admit_recipe", "admit_proposal"),
}
SWITCHES: tuple[str, ...] = tuple(ENABLE_MAP)


class UnknownSwitchError(ValueError):
    """--enable 里出现了五个开关之外的名字,或有空项。"""


def parse_enable(text: str | None) -> tuple[str, ...]:
    """'plan,admit' -> ('plan', 'admit')。None 表示没传 --enable,返回空元组。
    结果按 SWITCHES 的固定顺序去重;未知名字与空项(如 'plan,,admit'、'')一律拒绝。"""
    if text is None:
        return ()
    parts = [p.strip() for p in text.split(",")]
    if any(not p for p in parts):
        raise UnknownSwitchError(f"--enable has an empty item: {text!r}")
    unknown = sorted(set(parts) - set(ENABLE_MAP))
    if unknown:
        raise UnknownSwitchError(f"--enable: unknown switch(es) {unknown}; choose from {list(SWITCHES)}")
    return tuple(s for s in SWITCHES if s in parts)


def handler_keys(switches: tuple[str, ...] | list[str]) -> frozenset[str]:
    """开关 -> 要注入的处理器键。未知开关抛 UnknownSwitchError。
    裸字符串抛 TypeError:它会被按字符拆开,报出一串单字母的"未知开关"。先用 parse_enable 解析。"""
    if isinstance(switches, str):
        raise TypeError("handler_keys takes a tuple or list of switch names, not a str; parse it with parse_enable first")
    unknown = sorted(set(switches) - set(ENABLE_MAP))
    if unknown:
        raise UnknownSwitchError(f"unknown switch(es) {unknown}")
    return frozenset(k for s in switches for k in ENABLE_MAP[s])
