"""mindagent 的确定性错误。轮次处理器把它们判成"轮次 failed + T3 失败记录",不重试。"""
from __future__ import annotations


class MindAgentDataError(ValueError):
    """输入数据确定性地不对(轨迹引用了库里没有的技能、提案增量不合法等),重跑无益。"""


class ProposalParseError(MindAgentDataError):
    """外部模型的输出解析失败,且重试次数已用尽(T2)。attempts 是总尝试次数。"""

    def __init__(self, reason: str, *, attempts: int) -> None:
        super().__init__(f"{reason} (after {attempts} attempt(s))")
        self.reason = reason
        self.attempts = attempts


class AgentRoundFailedError(Exception):
    """这一轮确定性地做不成(配置、树里的数据不对),重跑无益。point 是失败点(子系统.模块)。"""

    def __init__(self, reason: str, *, point: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.point = point
