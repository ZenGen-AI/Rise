"""TrainScheduler 的错误类型。

SchedulerError 及其子类都是确定性失败:同样的树、同样的配置重来还会失败。规划处理器把它们
记成 planner_decision 的 failed 与一份 T3 失败记录。SchedulerConfigError 故意不继承 SchedulerError:
配置写错是人改一下就好的事,不该把某个 run 的规划永久判死(R5 没有自动恢复),所以它原样上抛。
"""
from __future__ import annotations


class SchedulerError(Exception):
    """确定性失败的基类。reason 是给人看的一句话。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ContextAssemblyError(SchedulerError):
    """源 run 的候选、诊断或指令缺失或形状不对,装配不出规划上下文。"""


class ResolveError(SchedulerError):
    """规划决策解析不成一份训练配置(配方不在、扰动套不上、没有可用的默认配置)。"""


class DirectiveConstructionError(SchedulerError):
    """构造出的指令过不了 validate_training_config 或 data_spec 的交叉校验,或目标位置已有指令。"""


class RunIdOrderError(SchedulerError):
    """防御性断言:树里已有 run-<数字> 目录,其编号不小于新分配的编号(例如人手建了 run-050,算出的新编号也是 050)。
    正常路径走不到(next_run_id 取最大编号加一);走到说明树被人手工改过,判确定性失败留痕,不覆盖那个目录。"""


class SchedulerConfigError(ValueError):
    """scheduler.yaml 或 training_defaults.yaml 的类型错、未知键、缺必填键。信息里点名字段。"""
