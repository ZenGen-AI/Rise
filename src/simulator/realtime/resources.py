from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

from simulator.backends import ModelBackend
from simulator.datamodel import Persona, RoleAssignment, Scenario

logger = logging.getLogger(__name__)


class SessionClosingError(Exception):
    """会话资源已进入收尾（quiesce 之后），不再接受新的后端调用。
    由 SessionResources.enter_call 抛出，TimeoutBackend.respond 在调用方线程里原样透出，
    不起后端线程。"""


@dataclass(frozen=True)
class SessionInfo:
    """后端工厂造一条会话的资源所需的全部输入。session_id 是服务端的会话标识，
    episode_id 是这条会话对应的情境运行标识；两者都由 servicer 在调工厂之前定好。"""

    scenario: Scenario
    assignment: RoleAssignment
    personas: dict[str, Persona]
    client_slot: str
    session_id: str
    episode_id: str


class SessionResources:
    """服务端一条会话持有的资源：各角色位的后端，加上收尾时要跑的关闭动作。

    收尾分四步，由 servicer 在流结束的 finally 里按序调用：
    quiesce（拒绝新调用）→ join（等在途线程退出）→ close（跑 closers）。
    TimeoutBackend 在调用方线程里先 enter_call、在后端线程退出时 exit_call，
    所以超时只是调用方不再等，线程仍然算在途，直到它自然结束。

    backends 是未套期限的后端；关智能体与删副本目录都由工厂放进 closers，
    close() 直接调它们，不经 TimeoutBackend 之类的包装层转发，也不碰 backends。

    一把 Condition 守护 _quiescing、_in_flight、_closed 三样状态。join 用 wait_for 等待，
    等待期间锁是放开的，别的线程照常 exit_call。"""

    def __init__(
        self,
        backends: dict[str, ModelBackend],
        closers: tuple[Callable[[], None], ...] = (),
    ) -> None:
        self.backends = backends
        self._closers = closers
        self._cond = threading.Condition()
        self._quiescing = False
        self._in_flight = 0
        self._closed = False

    def enter_call(self) -> None:
        """登记一次在途后端调用。已 quiesce 则抛 SessionClosingError，不计数。"""
        with self._cond:
            if self._quiescing:
                raise SessionClosingError("session is closing")
            self._in_flight += 1

    def exit_call(self) -> None:
        """注销一次在途后端调用；归零时唤醒 join 的等待者。"""
        with self._cond:
            self._in_flight -= 1
            if self._in_flight == 0:
                self._cond.notify_all()

    def quiesce(self) -> None:
        """拒绝之后的 enter_call。已登记的调用不受影响。可重复调用。"""
        with self._cond:
            self._quiescing = True

    def join(self, timeout_s: float) -> bool:
        """等所有在途调用退出。在 timeout_s 内归零返回 True，否则返回 False。"""
        with self._cond:
            return self._cond.wait_for(lambda: self._in_flight == 0, timeout_s)

    def close(self) -> None:
        """第一次调用按顺序跑 closers，之后的调用是空操作。每个 closer 单独 try：
        一个失败只记日志（只记异常类名）、继续跑后面的，副本目录删不掉由清扫兜底。
        不调 quiesce：调用方（servicer）负责先 quiesce、再 join。"""
        with self._cond:
            if self._closed:
                return
            self._closed = True
        for closer in self._closers:
            try:
                closer()
            except Exception as exc:
                logger.warning("session closer failed: %s", type(exc).__name__)
