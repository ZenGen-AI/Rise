"""延后通道的异常基类(真实后端设计 §7.3)。

没有任何依赖,所以 compute、mindtrainer、diagnostic 都能从这里导入而不互相牵连。
守护进程接住它之后不写树、状态不动、按退避重试;连续延后达到上限就带原因退出(见 daemon.DeferredExhaustedError)。
"""
from __future__ import annotations


class DeferredError(Exception):
    def __init__(self, reason: str, retry_after_seconds: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds
