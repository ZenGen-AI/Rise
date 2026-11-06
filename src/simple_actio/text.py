"""词面切分:触发信号与记忆检索共用的唯一一处分词规则。"""
from __future__ import annotations

import re

_ASCII_WORD = re.compile(r"[A-Za-z0-9_]+")
_CJK_RUN = re.compile(r"[㐀-䶿一-鿿豈-﫿]+")


def tokens(text: str) -> frozenset[str]:
    """把文本切成词面集合:ASCII 词按小写整词;连续汉字按相邻二字切分,
    只有一个字的汉字串保留单字。标点与空白丢弃。纯函数,同输入同输出。"""
    out: set[str] = {w.lower() for w in _ASCII_WORD.findall(text)}
    for run in _CJK_RUN.findall(text):
        if len(run) == 1:
            out.add(run)
        else:
            out.update(run[i:i + 2] for i in range(len(run) - 1))
    return frozenset(out)
