"""起服务之前的显存核算(纯函数,不连远端)。

这次在真机上踩到的坑:权重 335 GiB 分到 8 张 79 GiB 的卡上,每卡约 42 GiB,看起来还有余量;
但实际起服务时,激活、专家并行的通信缓冲、vLLM 的图捕获与 inductor 编译还要额外占一大块,
结果要么启动阶段 OOM,要么报「没有可用的缓存块」。这些都要等到加载几分钟之后才暴露。

这里把它提前成一次算术。模型是启发式的:

    每卡需求 = 权重/卡数 + 非权重开销 (+ 开图优化时的编译峰值)
    每卡可用 = 单卡容量 × gpu_memory_utilization
    需求超过可用就确定性失败,不占租约、不起作业

非权重开销的默认值按真机实测标定(见 DEFAULT_OVERHEAD_GIB 的注释),它随模型、上下文长度
与并行方式变化,所以是配置项而不是常量。这个预检是「早点报错」,不是「保证起得来」;
最终裁决仍是 vLLM 自己的启动错误,那边的日志尾巴会随 ServeStartupError 一起抛出来。
"""
from __future__ import annotations

from dataclasses import dataclass

from compute.errors import ComputeError

# vLLM 在图捕获与 inductor 编译阶段的额外峰值。真机实测一次编译要 11.92 GiB,向上取整。
COMPILE_HEADROOM_GIB = 12.0
# 非权重开销:激活、注意力工作区、专家并行的通信缓冲、运行时杂项。
# 标定依据:335 GiB 权重 / 8 卡 = 41.88 GiB/卡,0.85 的可用量是 67.29 GiB/卡,
# 实测此时连最小的 KV 缓存块都分不出来,所以权重之外至少还要 26 GiB 左右。
DEFAULT_OVERHEAD_GIB = 26.0


def weights_gib_from_index(config: dict) -> float | None:
    """从权重索引文件(形如 model.safetensors.index.json)里读总字节数。

    vLLM 加载的就是这些分片,所以它就是权重的实际体积。读不出返回 None:调用方据此跳过预检,
    而不是拿一个猜出来的数去拦正常启动。"""
    total = config.get("metadata", {}).get("total_size")
    if isinstance(total, bool) or not isinstance(total, (int, float)) or total <= 0:
        return None
    return float(total) / float(1 << 30)


@dataclass(frozen=True)
class MemorySizing:
    weights_gib: float
    cards: int
    card_gib: float
    utilization: float
    enforce_eager: bool
    overhead_gib: float = DEFAULT_OVERHEAD_GIB

    def report(self) -> dict:
        usable = self.card_gib * self.utilization
        per_card = self.weights_gib / self.cards
        compile_peak = 0.0 if self.enforce_eager else COMPILE_HEADROOM_GIB
        required = per_card + self.overhead_gib + compile_peak
        return {
            "weights_gib": round(self.weights_gib, 2),
            "per_card_weights_gib": round(per_card, 2),
            "overhead_gib": self.overhead_gib,
            "compile_peak_gib": compile_peak,
            "required_per_card_gib": round(required, 2),
            "usable_per_card_gib": round(usable, 2),
            "slack_gib": round(usable - required, 2),
            "fits": usable >= required,
        }


def check_memory_fits(sizing: MemorySizing) -> dict:
    """放不下就抛 ComputeError(确定性:同一组参数重试不会变好),信息里带算式,不带路径与凭证。"""
    if sizing.cards < 1:
        raise ComputeError("tensor_parallel_size must be at least 1")
    if sizing.card_gib <= 0:
        raise ComputeError("the host did not report usable GPU memory")
    if sizing.overhead_gib < 0:
        raise ComputeError("the non-weight memory overhead must not be negative")
    report = sizing.report()
    if not report["fits"]:
        raise ComputeError(
            "the model does not fit with this configuration: "
            f"{report['weights_gib']:g} GiB of weights over {sizing.cards} card(s) is "
            f"{report['per_card_weights_gib']:g} GiB per card, plus {report['overhead_gib']:g} GiB of "
            f"activations and buffers and {report['compile_peak_gib']:g} GiB of compilation peak, "
            f"so {report['required_per_card_gib']:g} GiB is needed per card but only "
            f"{report['usable_per_card_gib']:g} GiB is usable at gpu_memory_utilization="
            f"{sizing.utilization:g}; lower the model size, raise the utilization, use more cards, "
            f"or turn on enforce_eager"
        )
    return report
