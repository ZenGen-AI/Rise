"""计算层异常。确定性错误继承 ComputeError;可重试的继承 DeferredError(Ruling 9)。"""
from __future__ import annotations

from controlplane.deferred import DeferredError


class ComputeError(Exception):
    """确定性错误:请求不合法、规格冲突、助手哈希不符。重试没有用,不走延后通道。"""


class HelperError(ComputeError):
    """远端助手返回了 ok=false 的确定性错误。code 是助手的错误码,调用方按它分支。"""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class ComputeBusyError(DeferredError):
    """GPU 被别人占着,或租约申请失败。不是训练失败,不耗续训预算。"""


class TransportError(DeferredError):
    """SSH 或远端助手出错。reason 只含退出码与 stderr 尾巴,不含请求内容。"""


class RemoteJobFailedError(DeferredError):
    """作业非零退出或丢失。日志尾巴放在 log_tail,不进 reason(Ruling 10)。"""

    def __init__(self, reason: str, *, exit_code: int | None = None, log_tail: str = "",
                 retry_after_seconds: int | None = None) -> None:
        super().__init__(reason, retry_after_seconds=retry_after_seconds)
        self.exit_code = exit_code
        self.log_tail = log_tail


class RemoteJobTimeoutError(DeferredError):
    """作业超过 job_timeout_seconds,已先 stop。"""
