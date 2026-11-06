"""触发条件的小型谓词语言(闭环集成设计 §8.1)。

    {"signal": name}          已注册信号为真
    {"all": [t, ...]}         全部为真(非空列表)
    {"any": [t, ...]}         任一为真(非空列表)
    {"not": t}                取反
    {"turn_at_least": n}      len(history) >= n,n 是非负整数(不接受 bool 与浮点数)

每个节点是恰好一个键的字典;嵌套不超过 MAX_TRIGGER_DEPTH 层(自引用结构也因此被拒,
抛 InvalidTriggerError 而不是 RecursionError)。校验与求值都是纯函数。
"""
from __future__ import annotations

from simple_actio.errors import SimpleActioDataError
from simple_actio.signals import SignalRegistry
from simulator.datamodel import TurnRecord

_OPS = ("signal", "all", "any", "not", "turn_at_least")
MAX_TRIGGER_DEPTH = 32


class InvalidTriggerError(SimpleActioDataError):
    """触发条件结构不合法,或引用了注册表里没有的信号。"""


def validate_trigger(trigger: object, registry: SignalRegistry | None = None) -> None:
    """结构不合法抛 InvalidTriggerError。给了 registry 时还检查信号名都已注册。"""
    _validate(trigger, registry, "trigger", 1)


def _validate(node: object, registry: SignalRegistry | None, where: str, depth: int) -> None:
    if depth > MAX_TRIGGER_DEPTH:
        raise InvalidTriggerError(f"{where}: nested deeper than {MAX_TRIGGER_DEPTH} levels")
    if not isinstance(node, dict) or len(node) != 1:
        raise InvalidTriggerError(f"{where}: expected a dict with exactly one key, got {node!r}")
    (op, arg), = node.items()
    if op not in _OPS:
        raise InvalidTriggerError(f"{where}: unknown operator {op!r}; expected one of {list(_OPS)}")
    if op == "signal":
        if not isinstance(arg, str) or not arg:
            raise InvalidTriggerError(f"{where}.signal: expected a non-empty string, got {arg!r}")
        if registry is not None and arg not in registry:
            raise InvalidTriggerError(
                f"{where}.signal: unknown signal {arg!r}; registered: {registry.names()}"
            )
    elif op in ("all", "any"):
        if not isinstance(arg, list) or not arg:
            raise InvalidTriggerError(f"{where}.{op}: expected a non-empty list, got {arg!r}")
        for i, child in enumerate(arg):
            _validate(child, registry, f"{where}.{op}[{i}]", depth + 1)
    elif op == "not":
        _validate(arg, registry, f"{where}.not", depth + 1)
    else:  # turn_at_least
        if isinstance(arg, bool) or not isinstance(arg, int) or arg < 0:
            raise InvalidTriggerError(
                f"{where}.turn_at_least: expected a non-negative int, got {arg!r}"
            )


def evaluate_trigger(
    trigger: dict, history: list[TurnRecord], speaker_slot: str, registry: SignalRegistry,
) -> bool:
    """对已校验过的触发条件求值。未注册的信号抛 KeyError(调用方应先校验)。"""
    (op, arg), = trigger.items()
    if op == "signal":
        return bool(registry.get(arg)(history, speaker_slot))
    if op == "all":
        return all(evaluate_trigger(c, history, speaker_slot, registry) for c in arg)
    if op == "any":
        return any(evaluate_trigger(c, history, speaker_slot, registry) for c in arg)
    if op == "not":
        return not evaluate_trigger(arg, history, speaker_slot, registry)
    if op == "turn_at_least":
        return len(history) >= arg
    raise InvalidTriggerError(f"unknown operator {op!r}")


def referenced_signals(trigger: dict) -> frozenset[str]:
    """触发条件"针对"的信号:出现在偶数层 not 之下的 {"signal": name}。
    MindAgent 的经验分析器据此判断"被选中的技能是否针对某个信号"。"""
    return frozenset(_refs(trigger, negated=False))


def _refs(node: dict, *, negated: bool) -> set[str]:
    (op, arg), = node.items()
    if op == "signal":
        return set() if negated else {arg}
    if op in ("all", "any"):
        out: set[str] = set()
        for c in arg:
            out |= _refs(c, negated=negated)
        return out
    if op == "not":
        return _refs(arg, negated=not negated)
    return set()
