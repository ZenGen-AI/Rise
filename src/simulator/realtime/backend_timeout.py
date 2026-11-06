from __future__ import annotations

import threading

from simulator.backends import ModelBackend
from simulator.datamodel import Persona, Scenario, TurnRecord
from simulator.realtime.resources import SessionResources


class RoleBackendTimeoutError(Exception):
    """服务端角色后端（对手模型/外部 API）在期限内未返回（§2.6.5 明确列举的
    "角色绑定的外部API模型调用超时"）。这是 T1 类瞬态失败，与后端抛出的
    确定性异常（T2，映射为 role_backend_failure）必须区分开，故单独成类。"""


class RoleBackendRejectedError(Exception):
    """服务端角色后端（外部 API）的对端明确拒绝了这次请求（4xx，408、425、429 除外：密钥不对、模型名不存在、请求体不合规）。
    同样的请求再发还是同样的回答，重试无益，是 T2。驱动把它归 role_backend_rejected，与"后端抛了说不清的
    异常"的 role_backend_failure 分开：后者在服务端同时覆盖工厂/解析器故障，轨迹源分不出 T1 与 T2，
    所以保守地不重跑；这一个则能确定，轨迹源不重跑，编排器把训练记录写成 failed。"""


class RoleBackendUnavailableError(Exception):
    """服务端角色后端（外部 API）的对端暂时不可用：请求超时（408）、来得太早（425）、限流（429）、对端自己的毛病（5xx）、连不上、中途断开。
    过一会儿可能就好，是 T1。驱动把它归 role_backend_unavailable，轨迹源据此按条重跑。
    与 role_backend_failure 分开：后者在服务端同时覆盖"后端抛了说不清的异常"和"工厂/解析器故障"，
    轨迹源分不出 T1 与 T2，所以保守地不重跑；这一个则能确定是瞬态。"""


class RoleMemoryUnavailableError(Exception):
    """角色位的记忆暂时不可用：对应 Starling 的 failure_category 为 timeout 或 transport_failure
    （抽取/嵌入端点超时、连不上）。过一会儿可能就好，是 T1。驱动器把它归终止原因
    role_memory_unavailable，轨迹源与部署按条重跑。

    simulator 不认识 actio.MemoryFailureError（依赖方向不允许导入 actio）。组合根给每个 Actio 实例
    外面套一层转换后端，接住 MemoryFailureError，按 transient 改抛本类的子类；simulator 只认这个基类。
    与三个 RoleBackend*Error 互不继承，与 RoleMemoryFailureError 也互不继承。"""


class RoleMemoryFailureError(Exception):
    """角色位的记忆确定性失败：Starling 的 failure_category 为 timeout 与 transport_failure 之外的
    其余类别。同样的输入再来一遍还是同样的结果，重试无益，是 T2。驱动器把它归终止原因
    role_memory_failure，轨迹源不重跑，编排器把训练记录写成 failed。

    不并入 role_backend_failure：后者在服务端同时覆盖工厂/解析器故障，会被译成延后反复重试。
    与 RoleMemoryUnavailableError 的分工、转换后端的位置同上；本类同样由组合根的转换后端改抛子类。"""


class TimeoutBackend:
    """给任意 ModelBackend 套一层调用期限：每次 respond() 在一个 daemon 线程里
    执行被包装的后端，超过 timeout_s 未返回即抛 RoleBackendTimeoutError。

    这是 §2.6.5 "角色后端调用超时" 的落地——一个挂死的对手模型不应把整个会话
    拖成永久挂起。

    每次调用起独立的 daemon 线程（而不是 ThreadPoolExecutor）：executor 的
    工作线程不是 daemon，挂死的后端调用会在解释器退出时被 join，从而拖住
    进程退出。超时后底层线程无法被安全强杀，会跑到自然结束、结果被丢弃；
    因为是 daemon，它不会阻止进程退出。一次调用一个线程的开销相对于模型
    推理延迟可以忽略。

    给了 resources 时，每次调用在途期间登记在 SessionResources 里：在调用方线程里先
    enter_call（quiesce 之后在这里抛 SessionClosingError，不起线程），后端线程在 finally
    里 exit_call。登记在起线程之前，所以没有"线程已起、尚未登记、被 quiesce 之后的 join 漏掉"
    的窗口。超时只是调用方不再等，线程仍在计数里，直到它自然结束。resources 为 None 时
    不登记，行为不变。"""

    def __init__(
        self,
        inner: ModelBackend,
        timeout_s: float,
        resources: SessionResources | None = None,
    ) -> None:
        self._inner = inner
        self._timeout_s = timeout_s
        self._resources = resources

    def respond(
        self,
        persona: Persona,
        scenario: Scenario,
        history: list[TurnRecord],
        speaker_slot: str,
    ) -> tuple[str, str | None]:
        resources = self._resources
        if resources is not None:
            resources.enter_call()
        outcome: dict = {}

        def _call() -> None:
            try:
                outcome["value"] = self._inner.respond(
                    persona, scenario, history, speaker_slot,
                )
            except BaseException as exc:  # 跨线程搬运：原样在调用方线程重抛
                outcome["error"] = exc
            finally:
                if resources is not None:
                    resources.exit_call()

        worker = threading.Thread(target=_call, daemon=True, name="timeout-backend-call")
        try:
            worker.start()
        except BaseException:
            # 线程没起来，_call 不会跑，登记要在这里撤掉，否则 join 永远等不到归零。
            if resources is not None:
                resources.exit_call()
            raise
        worker.join(self._timeout_s)
        if worker.is_alive():
            raise RoleBackendTimeoutError(
                f"role backend for slot {speaker_slot!r} did not respond within {self._timeout_s}s"
            )
        if "error" in outcome:
            raise outcome["error"]
        return outcome["value"]
