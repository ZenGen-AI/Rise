"""六个处理器键的真实构造器表:{处理器键: (BuildContext) -> Handler}。

- admit_recipe / record_track:计划 3 的处理器本身(recipestore.admit / recipestore.record)。
- admit_proposal:计划 7 的处理器本身(mindagent.admit)。
- plan_next_round:计划 5 的 make_plan_handler;后端按 --planner-backend 选;--confirm-directives
  覆盖 scheduler.yaml 的 confirm_directives(只在内存里,不写回配置)。
- nominate_recipe:计划 5 的 make_nominate_handler;accept_mock_evidence 与传给 reconcile_once 的是
  同一个值(--allow-mock-evidence);repo_commit 来自 BuildContext。
- start_agent_round:计划 7 的 make_start_round_handler;AgentConfig 与对手台词来自 agent.yaml,
  实时服务端是进程内 loopback(factory.agent.AgentRuntime),模型后端是罐头后端。
"""
from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping

from controlplane.daemon import Handler
from controlplane.gitstore import GitStore
from controlplane.paths import backends_config_file
from factory.agent import (
    AgentRuntime,
    canned_backend_factory,
    personas_from_tree,
    read_agent_settings,
)
from factory.configio import preflight, read_tree_config
from mindagent.admit import handle_admit_proposal
from mindagent.rounds import make_start_round_handler
from recipestore.admit import handle_admit_recipe
from recipestore.record import handle_record_track
from trainplanner.backends import BridgeDecisionBackend, HeuristicBackend
from trainplanner.templates import load_template
from trainscheduler.config import SchedulerConfig, load_scheduler_config, load_training_defaults
from trainscheduler.nominate import make_nominate_handler
from trainscheduler.plan_handler import make_plan_handler


def scheduler_config_loader(confirm_directives: bool | None) -> Callable[[GitStore], SchedulerConfig]:
    """读 scheduler.yaml;confirm_directives 不为 None 时只在返回值里覆盖同名键。"""

    def load(store: GitStore) -> SchedulerConfig:
        config = load_scheduler_config(store)
        if confirm_directives is None:
            return config
        return dataclasses.replace(config, confirm_directives=confirm_directives)

    return load


def _checked_scheduler(ctx) -> SchedulerConfig:
    """装配期预检 scheduler.yaml:读一次、转成 FactoryConfigError,但不缓存。处理器仍在每次调用时自己重读(计划 5,Ruling 21),
    所以人改了配置下一次动作就生效;预检只保证"起跑时这份配置是好的",不是"一直是好的"。"""
    return preflight("scheduler.yaml", lambda: scheduler_config_loader(ctx.confirm_directives)(ctx.store))


def _plan(ctx) -> Handler:
    config = _checked_scheduler(ctx)
    preflight("training_defaults.yaml", lambda: load_training_defaults(ctx.store))
    preflight("planner prompt template", lambda: load_template(ctx.store, config.template_version))
    if ctx.planner_backend == "heuristic":
        backend = HeuristicBackend()
    else:
        backend = BridgeDecisionBackend(ctx.bridge("planner"), name="bridge")
    return make_plan_handler(backend=backend, config_loader=scheduler_config_loader(ctx.confirm_directives))


def _nominate(ctx) -> Handler:
    _checked_scheduler(ctx)
    return make_nominate_handler(
        config_loader=scheduler_config_loader(ctx.confirm_directives),
        accept_mock_evidence=ctx.accept_mock_evidence,
        repo_commit=ctx.repo_commit,
    )


def _has_bridge_role(store: GitStore, role: str) -> bool:
    if not store.exists(backends_config_file()):
        return False
    return role in read_tree_config(store, backends_config_file())


def _agent(ctx) -> Handler:
    config, lines = read_agent_settings(ctx.store)
    runtime = AgentRuntime(ctx.store, lines)
    ctx.register_closer(runtime.close)
    bridge = ctx.bridge("agent") if _has_bridge_role(ctx.store, "agent") else None
    return make_start_round_handler(
        config=config,
        client_factory=runtime.client_factory,
        backend_factory=canned_backend_factory,
        personas_loader=personas_from_tree,
        bridge=bridge,
    )


DEFAULT_BUILDERS: Mapping[str, Callable] = {
    "admit_recipe": lambda ctx: handle_admit_recipe,
    "admit_proposal": lambda ctx: handle_admit_proposal,
    "record_track": lambda ctx: handle_record_track,
    "nominate_recipe": _nominate,
    "plan_next_round": _plan,
    "start_agent_round": _agent,
}
