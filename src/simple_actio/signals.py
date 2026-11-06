"""对话信号:注册的纯函数,触发条件谓词语言里 {"signal": name} 的取值来源。

信号的签名是 (history, speaker_slot) -> bool:history 是"轮到 speaker_slot
说话时它能看到的历史"(runner 传给后端的那份),speaker_slot 是即将说话的角色位。
V1 内置三个信号,词典是闭环集成设计 §8.1 拟的默认值,没有数据支持,可替换。
"""
from __future__ import annotations

import re
from typing import Callable

from simple_actio.text import tokens
from simulator.datamodel import TurnRecord

Signal = Callable[[list[TurnRecord], str], bool]

_NAME = re.compile(r"^[a-z][a-z0-9_]*$")

DEFAULT_EMOTION_LEXICON: tuple[str, ...] = (
    "生气", "愤怒", "气死", "受够", "太过分", "崩溃", "伤心", "难过", "失望", "害怕", "焦虑",
    "angry", "furious", "upset", "hate", "scared",
)


def _opponent_turns(history: list[TurnRecord], speaker_slot: str) -> list[TurnRecord]:
    return [t for t in history if t.speaker_slot != speaker_slot]


def make_emotion_high(lexicon: tuple[str, ...] | list[str] = DEFAULT_EMOTION_LEXICON) -> Signal:
    """对方最后一句含词典里任一词(不区分大小写的子串匹配),或含两个及以上感叹号。"""
    words = tuple(w.lower() for w in lexicon)
    if not words or any(not w for w in words):
        raise ValueError("emotion lexicon must be a non-empty list of non-empty strings")

    def emotion_high(history: list[TurnRecord], speaker_slot: str) -> bool:
        opp = _opponent_turns(history, speaker_slot)
        if not opp:
            return False
        text = opp[-1].utterance.lower()
        if text.count("!") + text.count("！") >= 2:
            return True
        return any(w in text for w in words)

    return emotion_high


def no_new_information(history: list[TurnRecord], speaker_slot: str) -> bool:
    """对方至少说过两句,且最后一句的词面全都在它此前说过的话里出现过(空话也算)。"""
    opp = _opponent_turns(history, speaker_slot)
    if len(opp) < 2:
        return False
    last = tokens(opp[-1].utterance)
    earlier: set[str] = set()
    for t in opp[:-1]:
        earlier |= tokens(t.utterance)
    return last <= earlier


def first_turn(history: list[TurnRecord], speaker_slot: str) -> bool:
    """speaker_slot 此前一句都没说过(不论对方是否已开场)。"""
    return all(t.speaker_slot != speaker_slot for t in history)


class SignalRegistry:
    """信号名 → 纯函数。每个实例独立,注册不影响其他实例(没有全局可变状态)。"""

    def __init__(self) -> None:
        self._signals: dict[str, Signal] = {}

    def register(self, name: str, fn: Signal) -> None:
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError(f"signal name must match {_NAME.pattern}, got {name!r}")
        if name in self._signals:
            raise ValueError(f"signal {name!r} is already registered")
        if not callable(fn):
            raise TypeError(f"signal {name!r} must be callable")
        self._signals[name] = fn

    def __contains__(self, name: object) -> bool:
        return name in self._signals

    def get(self, name: str) -> Signal:
        try:
            return self._signals[name]
        except KeyError:
            raise KeyError(f"unknown signal {name!r}; registered: {self.names()}") from None

    def names(self) -> list[str]:
        return sorted(self._signals)

    def fired(self, history: list[TurnRecord], speaker_slot: str) -> list[str]:
        """全部已注册信号里此刻为真的那些,按名字排序。"""
        return [n for n in self.names() if self._signals[n](history, speaker_slot)]


def default_registry(
    *, emotion_lexicon: tuple[str, ...] | list[str] = DEFAULT_EMOTION_LEXICON,
) -> SignalRegistry:
    """新建一个带三个内置信号的注册表;词典可替换。"""
    reg = SignalRegistry()
    reg.register("emotion_high", make_emotion_high(emotion_lexicon))
    reg.register("first_turn", first_turn)
    reg.register("no_new_information", no_new_information)
    return reg
