"""六个处理器键的真实构造器表:{处理器键: (BuildContext) -> Handler}。

- admit_recipe / record_track:计划 3 的处理器本身(recipestore.admit / recipestore.record)。
- admit_proposal:计划 7 的处理器工厂,记忆增量写树外的部署库。
- plan_next_round:计划 5 的 make_plan_handler;后端按 --planner-backend 选;--confirm-directives
  覆盖 scheduler.yaml 的 confirm_directives(只在内存里,不写回配置)。
- nominate_recipe:计划 5 的 make_nominate_handler;accept_mock_evidence 与传给 reconcile_once 的是
  同一个值(--allow-mock-evidence);repo_commit 来自 BuildContext。
- start_agent_round:计划 7 的 make_start_round_handler;AgentConfig 与对手台词来自 agent.yaml,
  实时服务端是进程内 loopback(factory.agent.AgentRuntime),模型后端是罐头后端。
  它与 admit_proposal 共用 BuildContext.deployment_memory(assemble 造的那一份),运行时开对手位端口也用其中的 store;
  部署驱动器经运行时的部署登记表把轮次 id 交给服务端。
"""
from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping

from controlplane.daemon import Handler
from controlplane.gitstore import GitStore
from controlplane.paths import backends_config_file
from factory.agent import (
    AGENT_CONFIG,
    AgentRuntime,
    canned_backend_factory,
    personas_from_tree,
    read_agent_settings,
)
from factory.configio import preflight, read_tree_config
from factory.errors import FactoryConfigError
from mindagent.admit import make_admit_proposal_handler
from mindagent.errors import MindAgentDataError
from mindagent.library import check_memory_library_empty
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


def _checked_memory_library(ctx) -> None:
    """装配期预检控制面 memory_library/ 为空:非空转成 FactoryConfigError,命令行退出码 2,树里什么都不写。"""
    preflight("memory_library/", lambda: check_memory_library_empty(ctx.store))


def _agent(ctx) -> Handler:
    _checked_memory_library(ctx)
    config, lines = read_agent_settings(ctx.store)
    mem = ctx.deployment_memory
    starling = mem.runtime["backend"] == "starling"
    budget = mem.budget
    runtime = ctx.runtime
    if runtime is None:
        # assemble 总会造共用运行时;走到这里的是手工拼的 BuildContext。时限与 wire._runtime_timeouts 在 interact=False
        # 时同一个判法(这里不导入 factory.wire:wire 在函数内导入本模块,反过来就成环)
        timeouts: dict = {"turn_timeout_s": budget.deploy_turn_s} if starling else {}
        if budget.derived:
            timeouts.update(backend_timeout_s=budget.backend_s, resource_join_timeout_s=budget.join_s)
        runtime = AgentRuntime(ctx.store, lines, deployment_store=mem.store, **timeouts)
        ctx.register_closer(runtime.close)
    # ctx.runtime 非空时是 assemble 造的共用运行时(--interact 与 agent 开关同开),它的收尾已登记过,
    # 部署管理面也已是这里的 mem.store。轮次处理器、门② 与运行时共用这一个管理面:端口数是它的实例内状态
    bridge = ctx.bridge("agent") if _has_bridge_role(ctx.store, "agent") else None
    # Starling:整条流的 deadline 按 max_turns × 每轮折算(拉起与外部模式都按服务端点推出,FC-31、FC-51),
    # 训练路的流 deadline 由轨迹源自己传;替身不传,保持 AgentRuntime 的缺省。推出的预算另传等端口全关与准入退避的上限,
    # 两者随服务端收尾的 join 一起放长
    extra: dict = {"stream_deadline_s": config.max_turns * budget.deploy_per_turn_s} if starling else {}
    if budget.derived:
        extra.update(close_wait_s=budget.close_wait_s, admission_wait_s=budget.admission_wait_s)
    return make_start_round_handler(
        config=config,
        client_factory=runtime.client_factory,
        backend_factory=canned_backend_factory,
        personas_loader=personas_from_tree,
        bridge=bridge,
        memory_store=mem.store,
        memory_layout=mem.layout,
        memory_runtime=mem.runtime,
        register_episode=runtime.register_deployment_episode,
        unregister_episode=runtime.unregister_deployment_episode,
        **extra,
    )


def _admit_proposal(ctx) -> Handler:
    # 门② 的记忆提案改走部署库,留在树里的旧条目会被误当成已收录,所以收录处理器装配时也查
    _checked_memory_library(ctx)
    mem = ctx.deployment_memory
    store = ctx.store

    def bound_pairs() -> frozenset[tuple[str, str]]:
        """部署配置里绑定的 (情境, 人格) 对:agent.yaml 的 scenario_ids × bindings。每次调用现读(人改了配置
        下一次就生效)。没有 agent.yaml(没开 agent 开关)就没有任何绑定对;读不了转成 MindAgentDataError,
        门② 按 T2 处理(`mindagent` 不导入 `factory`,转换在这里做)。"""
        if not store.exists(AGENT_CONFIG):
            return frozenset()
        try:
            config, _ = read_agent_settings(store)
        except FactoryConfigError as exc:
            raise MindAgentDataError(f"cannot read the deployment bindings: {exc}") from exc
        return frozenset((sid, b.persona_id) for sid in config.scenario_ids for b in config.bindings)

    return make_admit_proposal_handler(memory_store=mem.store, memory_layout=mem.layout, bound_pairs=bound_pairs)


DEFAULT_BUILDERS: Mapping[str, Callable] = {
    "admit_recipe": lambda ctx: handle_admit_recipe,
    "admit_proposal": _admit_proposal,
    "record_track": lambda ctx: handle_record_track,
    "nominate_recipe": _nominate,
    "plan_next_round": _plan,
    "start_agent_round": _agent,
}
