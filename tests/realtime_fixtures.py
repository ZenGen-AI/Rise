"""实时服务端测试共用的适配器。不是测试文件(不以 test_ 开头),pytest 不收集它;
测试文件靠 pytest 默认的 prepend 导入模式把 tests/ 放进 sys.path 后直接导入(与 rs_fixtures.py 同一用法)。

后端工厂与落盘钩子的签名变过一次:工厂收 SessionInfo 返回 SessionResources,钩子收 (session_id, episode)。
大量既有测试只关心旧形状(四个位置参数的工厂、单参的钩子),用这里的两个适配器包一层即可。"""
from __future__ import annotations

from collections.abc import Callable

from simulator.backends import ModelBackend
from simulator.datamodel import Episode, Persona, RoleAssignment, Scenario
from simulator.realtime.resources import SessionResources
from simulator.realtime.servicer import BackendsFactory, SessionClosedHook


def plain_factory(
    fn: Callable[[Scenario, RoleAssignment, dict[str, Persona], str], dict[str, ModelBackend]],
) -> BackendsFactory:
    """把旧形状的工厂(scenario, assignment, personas, client_slot) -> backends 包成新签名。"""

    def factory(info) -> SessionResources:
        return SessionResources(backends=fn(info.scenario, info.assignment, info.personas, info.client_slot))

    return factory


def plain_hook(fn: Callable[[Episode], object]) -> SessionClosedHook:
    """把单参的钩子(episode) 包成新签名:丢掉 session_id,把 episode 交给 fn。"""

    def hook(session_id: str, episode: Episode) -> None:
        fn(episode)

    return hook
