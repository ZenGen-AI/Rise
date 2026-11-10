"""组合根(闭环集成设计 §10.2)。

两条路径:
- 不传 --enable(switches 为空):与 mindtrainer.wire.run 完全相同,只注入
  make_mindtrainer_dispatcher(),不读任何配置,不注入评估器与数据生产器。
- 传了 --enable:启动时一次装配完(缺配置、缺资产、缺凭证立刻 FactoryConfigError),
  派发器带默认数据生产器,request_diagnostic 换成 mock 后端的真实评估器(报告标 mock),
  按开关映射出的处理器键逐个调用处理器构造器。
"""
from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from controlplane.daemon import Dispatcher, Evaluator, Handler, reconcile_once, run_daemon
from controlplane.gitstore import GitStore
from controlplane.lock import acquire_repo_lock
from controlplane.reconcile import Action
from factory.bridges import build_bridge
from factory.errors import FactoryConfigError
from factory.evaluation import build_mock_evaluator
from factory.switches import handler_keys
from inference_bridge import CliBridge
from mindtrainer.data_producer import TemplateDataProducer
from mindtrainer.dispatch import make_mindtrainer_dispatcher

if TYPE_CHECKING:  # factory.agent 与 factory.memory_runtime 拖进 grpc 与 mindagent,只在类型检查时导入
    from simple_actio.memory_port import DeploymentMemoryStore
    from factory.agent import AgentRuntime
    from factory.interact_policy import OpponentSwitch
    from factory.memory_runtime import DeploymentMemory, MemoryWiring, TrainingWorlds

_log = logging.getLogger("factory.wire")

PLANNER_BACKENDS = ("bridge", "heuristic")
EXECUTORS = ("mock", "roll")
EVALUATORS = ("mock", "someval")  # 设计 FC-16


@dataclass(frozen=True)
class FactoryOptions:
    switches: tuple[str, ...] = ()
    allow_mock_evidence: bool = False
    confirm_directives: bool | None = None  # None:用 scheduler.yaml 的 confirm_directives
    planner_backend: str = "bridge"
    repo_commit: str | None = None  # None:取代码仓库的 git rev-parse HEAD(取不到仍是 None)
    executor: str = "mock"  # roll:真实 ROLL LoRA SFT 执行器(设计 FC-15),默认 mock
    endpoint: str | None = None  # 只在 executor == "roll" 或 interact 时有意义:点名 compute/endpoints.yaml 里的端点
    evaluator: str = "mock"  # someval:真实 SoMEval 评测(设计 FC-16);必须与 --executor roll 同用
    interact: bool = False  # 给派发器注入实时客户端与策略工厂,让 interact 指令跑完;与 --executor roll 互斥


CODE_ROOT = Path(__file__).resolve().parents[2]  # src/factory/wire.py -> 代码仓库根


def resolve_repo_commit(override: str | None, *, code_root: Path = CODE_ROOT) -> str | None:
    """--repo-commit 优先;否则 git -C <代码仓库根> rev-parse HEAD;不是 git 仓库或没有 git 时返回 None。

    实现放在 controlplane.code_version:mindtrainer 侧的分层规则不许它导入 factory,而训练记录也要
    写同一个代码版本。这里保留旧名字,装配与测试仍从这里取。"""
    from controlplane.code_version import resolve_code_commit

    return resolve_code_commit(override, code_root=code_root)


@dataclass(frozen=True)
class BuildContext:
    """交给处理器构造器的装配上下文。bridge(role) 每次调用都新建一个 CliBridge。"""

    store: GitStore
    accept_mock_evidence: bool
    confirm_directives: bool | None
    planner_backend: str
    # 不进 repr:整个 env 映射里有凭证(repr(ctx) 会把它们原样带出来)。
    env: Mapping[str, str] = field(repr=False)
    log_dir: Path | None
    repo_commit: str | None = None
    closers: list = field(default_factory=list)
    # --interact 或 --enable agent 时由 assemble 造的那一个运行时;其余情形是 None。
    # 它的收尾函数由 assemble 登记一次,构造器只取用,不再登记。
    runtime: AgentRuntime | None = None
    # 启用 start_agent_round 或 admit_proposal 时由 assemble 造的那一份部署记忆,两个构造器共用;其余是 None。
    deployment_memory: DeploymentMemory | None = None

    def bridge(self, role: str) -> CliBridge:
        return build_bridge(self.store, role, env=self.env, log_dir=self.log_dir)

    def register_closer(self, close: Callable[[], None]) -> None:
        """构造器起了要收尾的资源(如 loopback 实时服务端)时登记;run 结束时逆序调用。"""
        self.closers.append(close)


def _close_all(closers) -> None:
    """逆序调用每个收尾函数,把异常攒起来。

    裸循环会在第一个抛异常时跳过剩下的收尾函数,还会把装配中途的原始错误盖掉——
    那正是最需要看到的信息。全部跑完之后,若有失败,抛一个带明细的异常;调用方在
    异常路径上会忽略它(原异常已经在传播),正常退出路径上它会被报出来。
    """
    failures = []
    for close in reversed(list(closers)):
        try:
            close()
        except Exception as exc:          # 收尾失败不该让后续收尾也不跑
            failures.append(f"{type(exc).__name__}: {exc}")
    if failures:
        raise RuntimeError("closer(s) failed: " + "; ".join(failures))


def _guard(fn: Callable, session) -> Callable:
    """致命停机的组合根那一半(90 篇"停守护进程分两半"、FC-47):调用返回或抛出之后查服务会话的致命状态,查到就抛
    `FactoryConfigError`。调和循环不接它,命令行译成退出码 2。会话没进致命状态时原样返回或原样上抛。"""
    def guarded(*args, **kwargs):
        try:
            out = fn(*args, **kwargs)
        except BaseException:
            _raise_if_fatal(session)
            raise
        _raise_if_fatal(session)
        return out

    return guarded


def _raise_if_fatal(session) -> None:
    reason = session.fatal
    if reason is not None:
        tail = "" if "service.log" in reason else "; see service.log"
        raise FactoryConfigError(f"memory service: {reason}{tail}") from None


HandlerBuilder = Callable[[BuildContext], Handler]


@dataclass(frozen=True)
class Assembly:
    dispatcher: Dispatcher
    handlers: dict[str, Handler]
    evaluator: Evaluator | None
    accept_mock_evidence: bool
    closers: tuple = ()
    # --executor roll 时才有(设计 FC-15);mock 路径下两者都是 None,Assembly 与改动前等价
    executor_factory: Callable[[str], object] | None = None
    converter: object | None = None
    # Starling 且启用部署记忆时,持仓库锁之后才跑的部署世界核对(90 篇第 23 条);替身与不需要的路径是 None。
    # assemble 只交出它、不调它:第二个进程撞锁之前不能开过任何句柄,由 run(经 run_daemon 的 on_locked)与
    # reconcile_one_tick 在锁内调用。
    on_locked: Callable[[], None] | None = None

    def close(self) -> None:
        """逆序收尾。一个收尾函数抛异常不能跳过其余几个,也不能盖住装配中途的原异常。"""
        _close_all(self.closers)

    @property
    def closed_loop(self) -> bool:
        return bool(self.handlers)


# --interact 的时间预算(设计源 6.4 节),外层必须高于内层的最坏耗时,内层才先超时、外层看到分类过的失败。
# 真机(eager,两适配器)每轮 20 到 65 秒,2026-11-02 从 60/25/60/90/90 整体放宽到下面这组。
INTERACT_TURN_TIMEOUT_S = 150.0     # 服务端等一次提交:训练位策略 HTTP 120 秒加一轮记忆最坏 20 秒;默认的 5 秒是给罐头后端定的
INTERACT_BACKEND_TIMEOUT_S = 90.0   # 服务端单次后端调用:对手 HTTP 60 秒加一轮记忆最坏 20 秒
INTERACT_MESSAGE_TIMEOUT_S = 120.0  # 客户端相邻两条服务端消息:间隔以一个对手轮为界,高于后端调用上限


def memory_wiring(repo_root: Path, env: Mapping[str, str], *, deployment: bool) -> MemoryWiring:
    """按 RISE_MEMORY_CONFIG 选记忆后端,真实现在 `factory.memory_runtime.memory_wiring`。

    这里只是薄的转发,保证 `factory.wire` 上有这个名字(装配与测试都从这里取)而不在导入期拖进 mindagent:
    `factory.cli` 顶层导入本模块,而不带 --enable 的入口不许载入 mindagent(Ruling 16)。"""
    from factory.memory_runtime import memory_wiring as _impl   # 惰性:拖进 mindagent(Ruling 16)

    return _impl(repo_root, env, deployment=deployment)


def deploy_turn_timeout_s(*, interact: bool, starling: bool) -> float | None:
    """共用运行时里「服务端等一次提交」的上限(设计二 6.4 节、FC-31);None 即保持 AgentRuntime 的默认 5 秒。

    `--interact` 开着取 90 秒(训练位策略 HTTP 加记忆最坏的一轮),与 `--enable agent` 同开也是它:两条路共用一个运行时,
    取较大值。只有 `--enable agent` 且是 Starling 时取 40 秒(`DEPLOY_MEMORY_TURN_TIMEOUT_S`,单一来源在
    `factory.memory_runtime`)。替身没有网络调用,保持现值。"""
    if interact:
        return INTERACT_TURN_TIMEOUT_S
    if starling:
        from factory.memory_runtime import DEPLOY_MEMORY_TURN_TIMEOUT_S   # 惰性:拖进 mindagent(Ruling 16)

        return DEPLOY_MEMORY_TURN_TIMEOUT_S
    return None


def _shared_runtime(
    store: GitStore, options: FactoryOptions, keys: frozenset[str],
    *, turn_timeout_s: float | None, training_worlds: TrainingWorlds | None,
    deployment_store: DeploymentMemoryStore | None = None,
) -> tuple[AgentRuntime | None, OpponentSwitch | None]:
    """`--interact` 或 `--enable agent` 时造那唯一的 AgentRuntime(进程内 loopback 实时服务端),否则 (None, None)。

    两者同开就共用这一个,不起第二个服务端。对手罐头台词取自 agent.yaml;文件不在时给空元组,
    由 `agent` 开关的构造器照旧报"缺 agent.yaml"(--interact 单独用时,台词只是没有剧本的剧本驱动位的兜底)。
    文件在但写坏了就在这里报 FactoryConfigError,而不是留到派发期。

    --interact 时运行时拿到的对手后端工厂是一个可切换的 OpponentSwitch:vLLM 对手要用会话里才有的 ServiceHandle,
    所以运行时先带着空开关造好,策略上下文进入时装上真工厂、退出时卸下(见 factory.interact_policy)。
    卸下的开关给的是运行时原来的罐头后端,所以共用运行时上的部署会话行为不变。

    turn_timeout_s 为 None 时不传,保持 AgentRuntime 的默认值(见 `deploy_turn_timeout_s`)。training_worlds 只在
    --interact 时传,且必须是交给策略工厂的同一个对象(客户端在它里面建 rollout 世界,服务端按 episode id 加入):
    部署会话没登记给训练 run,用不到它。deployment_store 只在启用了 start_agent_round 时传
    (部署记忆的那一个管理面,与轮次处理器、门② 同一个对象):运行时据此在部署世界里开对手位的端口。"""
    if not options.interact and "start_agent_round" not in keys:
        return None, None
    from factory.agent import AGENT_CONFIG, AgentRuntime, read_agent_settings   # 惰性:拖进 grpc 与 mindagent

    lines = read_agent_settings(store)[1] if store.exists(AGENT_CONFIG) else ()
    kwargs: dict = {} if turn_timeout_s is None else {"turn_timeout_s": turn_timeout_s}
    if deployment_store is not None:
        kwargs["deployment_store"] = deployment_store
    if options.interact:
        from factory.interact_policy import OpponentSwitch

        switch = OpponentSwitch(lines)
        return AgentRuntime(store, lines, message_timeout_s=INTERACT_MESSAGE_TIMEOUT_S,
                            backend_timeout_s=INTERACT_BACKEND_TIMEOUT_S,
                            opponent_backend_factory=switch, training_worlds=training_worlds, **kwargs), switch
    return AgentRuntime(store, lines, **kwargs), None


def _interact_wiring(
    store: GitStore, options: FactoryOptions, runtime: AgentRuntime, switch: OpponentSwitch,
    environ: Mapping[str, str], memory: MemoryWiring,
) -> dict:
    """`--interact` 要交给派发器的东西:策略工厂、它所用端点的名字,以及解起点记忆的解析器(来自 `memory`:
    替身或 Starling)。rollout 世界经 `memory.training_worlds` 交给策略工厂,与运行时手里的是同一个;启动日志在
    `memory_wiring` 里记过一次,这里不再记。

    端点只选一次:点名的或唯一的那个。同一个端点既给 policy_context 起服务,也作为 policy_endpoint
    写进 training_run.policy_backend,所以记录里的端点就是服务实际起在的端点。配置错一律 FactoryConfigError。"""
    # 惰性导入:不走 --interact 的入口不该为此多载入 compute、grpc 与调度器配置
    from factory.compute_config import build_compute_client, read_endpoints_config, read_host_map, select_endpoint
    from factory.interact_checks import check_interact_config
    from factory.interact_policy import make_policy_factory

    check_interact_config(store, environ)          # 设计 90 拒绝项 2 到 9,先于端点与主机映射。只作启动门槛:
    # opponent 段不在这里取,策略上下文每次进入时自己从树里重读(装配之后改了也生效)
    endpoint = select_endpoint(read_endpoints_config(store), options.endpoint)
    client = build_compute_client(endpoint, read_host_map(env=environ))
    policy_factory = make_policy_factory(
        store=store, runtime=runtime, switch=switch, client=client, endpoint=endpoint, env=environ,
        training_worlds=memory.training_worlds,
    )
    return {
        "policy_factory": policy_factory,
        "policy_endpoint": endpoint.endpoint_id,
        "memory_resolver": memory.memory_resolver,
    }


def _dispatcher(options: FactoryOptions, runtime: AgentRuntime | None, **kwargs):
    """--interact 时给派发器注入实时客户端(并一律带数据生产器,「按种子生成」一路要靠它合成)。
    策略工厂与端点名由 _interact_wiring 经 kwargs 带进来;repo_commit 由装配方给(派发器不得导入 factory
    自己查),写进训练记录的 code_versions。"""
    if options.interact:
        assert runtime is not None
        kwargs["client_factory"] = runtime.client_factory
        kwargs.setdefault("data_producer", TemplateDataProducer())
    kwargs.setdefault("repo_commit", resolve_repo_commit(options.repo_commit))
    return make_mindtrainer_dispatcher(**kwargs)


def default_builders() -> Mapping[str, HandlerBuilder]:
    """六个处理器键的真实构造器,在 factory.handlers 里(依赖计划 3、5、7)。"""
    from factory.handlers import DEFAULT_BUILDERS

    return DEFAULT_BUILDERS


def _assemble_roll(store: GitStore, options: FactoryOptions, *, env: Mapping[str, str] | None) -> Assembly:
    """`--executor roll`:读端点与主机映射,造客户端,再造执行器工厂与转换器(设计 FC-15)。

    配置错误一律 FactoryConfigError,全在第一个 tick 之前。工厂接收 run_id,因为真实执行器把它
    编进作业身份与远端输出目录,而装配期不知道它——它是每条指令来了才有的。"""
    # 惰性导入:不走 roll 的入口不该为此多载入 compute 与两个真实后端模块
    from factory.compute_config import build_compute_client, read_endpoints_config, read_host_map, select_endpoint
    from mindtrainer.roll_converter import RollLoraConverter
    from mindtrainer.roll_executor import RollExecutor

    from diagnostic.contamination import load_index_file, make_train_gate
    from factory.eval_config import read_eval_config

    environ = os.environ if env is None else env
    endpoint = select_endpoint(read_endpoints_config(store), options.endpoint)
    if endpoint.host_deps_root is None:
        raise FactoryConfigError(
            f"endpoint {endpoint.endpoint_id!r} has no host_deps_root; --executor roll mounts the Megatron-LM "
            "and fla sources from there into the training container"
        )
    hosts = read_host_map(env=environ)
    client = build_compute_client(endpoint, hosts)
    eval_config = read_eval_config(env=environ, repo_root=store.repo_root)
    dataset_sha = _sombench_sha(eval_config)
    row_gate = make_train_gate(load_index_file(eval_config.index_path, dataset_sha))
    roll_runtime = _roll_runtime_cache(endpoint, hosts, client)
    converter = RollLoraConverter(client=client, endpoint=endpoint)

    def bind(target) -> None:
        # 第一次派发时才部署 ROLL、建容器(碰远端、耗时),不在装配期;执行器与转换器共用同一份
        runtime, bound_client = roll_runtime.get()
        target.bind_runtime(bound_client, runtime.endpoint, runtime.env.record())

    def executor_factory(run_id: str) -> RollExecutor:
        return RollExecutor(client=client, endpoint=endpoint, run_id=run_id, row_gate=row_gate,
                            on_first_preflight=lambda target: (bind(target), bind(converter)))
    evaluator = None
    if options.evaluator == "someval":
        evaluator = _build_someval_evaluator(store, client, endpoint, eval_config, dataset_sha)
    return Assembly(
        make_mindtrainer_dispatcher(
            executor_factory=executor_factory, converter=converter,
            repo_commit=resolve_repo_commit(options.repo_commit),
        ),
        {}, evaluator, False,
        executor_factory=executor_factory, converter=converter,
    )


def _roll_runtime_cache(endpoint, hosts, client):
    """`--executor roll` 的运行时:钉住的 ROLL(rise HEAD 里的 submodule gitlink)部署到端点上、起好容器。

    返回 RollRuntimeCache,第一次 get() 才做远端的事。读 submodule、打包在本机,失败(没初始化、工作树脏、
    HEAD 不是钉住的提交)和远端的确定性失败一样是 ComputeError,由 _BoundOnPreflight 译成 T2。
    返回 (RollRuntime, 绑在派生端点上的客户端):派生端点的 launch_prefix 进的是系统建的容器,
    所以要另造一个客户端;它与原客户端同一个 ssh 别名。"""
    from compute.roll_deploy import RollRuntimeCache, ensure_roll_env, tree_digest
    from compute.roll_env import RollEnv
    from compute.roll_source import pinned_roll_commit, roll_archive
    from factory.compute_config import build_compute_client

    def build():
        commit = pinned_roll_commit(CODE_ROOT)
        archive = roll_archive(CODE_ROOT, commit)
        roll_env = RollEnv(roll_commit=commit, roll_tree=tree_digest(archive))
        runtime = ensure_roll_env(client, endpoint, roll_env, archive)
        return runtime, build_compute_client(runtime.endpoint, hosts)

    return RollRuntimeCache(build)



def _sombench_sha(eval_config) -> str:
    """现算数据集哈希。索引与资产版本都拿它比对:数据集换了,旧索引与旧题库都不许再用。

    版本比对在两处:索引由 `load_index_file` 校验头部,资产由 `check_sombench_version` 校验。"""
    from factory.errors import FactoryConfigError
    from factory.sombench import file_sha256, sombench_parquet

    parquet = sombench_parquet(eval_config.someval_root)
    if not parquet.is_file():
        raise FactoryConfigError(f"the SoMBench parquet is not at {parquet}")
    return file_sha256(parquet)


def _build_someval_evaluator(store, client, endpoint, eval_config, dataset_sha: str):
    """真实评估器:资产与版本在装配期校验一次,运行期每次仍从树里读。"""
    from diagnostic.evaluator import make_diagnostic_evaluator
    from diagnostic.scorer import MockJudgeBackend
    from factory.evaluation import _someval_evaluator
    from factory.sombench import check_sombench_version
    from factory.sombench_runner import SomevalRunner

    cfg = read_evaluator_config_dict(store)
    check_sombench_version(cfg["taxonomy_version"], dataset_sha)
    check_sombench_version(cfg["item_pool_version"], dataset_sha)
    if cfg["tier"] != "full":
        raise FactoryConfigError(
            "--evaluator someval evaluates the whole benchmark; evaluator.yaml must say tier: full"
        )
    if cfg["sampling_k"] != 1:
        raise FactoryConfigError(
            "--evaluator someval samples each item once; evaluator.yaml must say sampling_k: 1"
        )
    runner = SomevalRunner(store=store, client=client, endpoint=endpoint, cfg=eval_config)
    return _someval_evaluator(store=store, runner=runner, cfg=cfg)


def read_evaluator_config_dict(store) -> dict:
    from factory.evaluation import read_evaluator_config

    return read_evaluator_config(store)


def assemble(
    store: GitStore,
    options: FactoryOptions,
    *,
    env: Mapping[str, str] | None = None,
    log_dir: str | Path | None = None,
    builders: Mapping[str, HandlerBuilder] | None = None,
) -> Assembly:
    if options.planner_backend not in PLANNER_BACKENDS:
        raise FactoryConfigError(
            f"planner backend must be one of {list(PLANNER_BACKENDS)}, got {options.planner_backend!r}"
        )
    keys = handler_keys(options.switches)
    if options.executor not in EXECUTORS:
        raise FactoryConfigError(f"executor must be one of {list(EXECUTORS)}, got {options.executor!r}")
    if options.interact and options.executor == "roll":
        # 设计 90 拒绝项 1:真实执行器只支持 SFT(supported_phases 是 {"sft"}),开不了 interact 的实时会话
        raise FactoryConfigError(
            "--interact cannot be combined with --executor roll: the real executor only supports SFT, "
            "so it cannot run a live interact session"
        )
    if options.endpoint is not None and options.executor != "roll" and not options.interact:
        raise FactoryConfigError("--endpoint only makes sense with --executor roll or --interact")
    if options.evaluator not in EVALUATORS:
        raise FactoryConfigError(f"evaluator must be one of {list(EVALUATORS)}, got {options.evaluator!r}")
    if options.evaluator == "someval" and options.executor != "roll":
        raise FactoryConfigError("--evaluator someval needs --executor roll: it evaluates a real checkpoint")
    if options.evaluator == "someval" and keys:
        raise FactoryConfigError(
            "--evaluator someval cannot be combined with --enable yet: a real evaluation holds "
            "the reconcile loop and the GPU lease for hours"
        )
    if options.executor == "roll":
        if keys:
            # 设计保证 7:真训练同步占住调和循环数小时(CP-15),闭环处理器的排队语义是按 mock
            # 训练写的;先让一条链路跑通真机,再决定叠加。以后放开比以后收紧容易。
            raise FactoryConfigError(
                "--executor roll cannot be combined with --enable yet: a real training run "
                "holds the reconcile loop for hours"
            )
        return _assemble_roll(store, options, env=env)
    if "plan_next_round" in keys and not options.interact:
        from factory.interact_checks import check_plan_needs_interact

        check_plan_needs_interact(store)          # 设计 90 拒绝项 11
    if not keys and not options.interact:
        # 与 mindtrainer.wire.run 逐字节相同的路径:不读配置、不注入别的;代码版本两边写的是同一个
        return Assembly(
            make_mindtrainer_dispatcher(repo_commit=resolve_repo_commit(options.repo_commit)),
            {}, None, False,
        )
    environ = os.environ if env is None else env
    if not keys:
        # --interact 单独用:没有处理器和评估器,但有运行时要收尾
        memory = memory_wiring(store.repo_root, environ, deployment=False)        # 配置有问题在这里退出码 2,先于造运行时
        closers = list(memory.closers)              # 服务的收尾最先登记、最后执行
        try:
            runtime, switch = _shared_runtime(
                store, options, keys,
                turn_timeout_s=deploy_turn_timeout_s(interact=True, starling=memory.backend == "starling"),
                training_worlds=memory.training_worlds,
            )
            closers.append(runtime.close)
            wiring = _interact_wiring(store, options, runtime, switch, environ, memory)
        except BaseException:
            try:
                _close_all(closers)          # 还没起过服务端,关它只是把状态收干净
            except Exception:
                pass
            raise
        dispatcher = _dispatcher(options, runtime, **wiring)
        if memory.session is not None:
            dispatcher = _guard(dispatcher, memory.session)
        return Assembly(dispatcher, {}, None, False, closers=tuple(closers), on_locked=memory.on_locked)
    table = default_builders() if builders is None else builders
    missing = sorted(keys - set(table))
    if missing:
        raise FactoryConfigError(f"no handler builder for {missing}")
    evaluator = build_mock_evaluator(store)
    # 记忆后端只在需要时选(--interact,或启用了轮次与收录处理器之一);没设 RISE_MEMORY_CONFIG 走替身。
    # 其余入口不读这个变量。早于 _shared_runtime:运行时的等提交上限随后端取值。
    needs_deployment = bool(keys & {"start_agent_round", "admit_proposal"})
    memory = None
    if options.interact or needs_deployment:
        memory = memory_wiring(store.repo_root, environ, deployment=needs_deployment)
    # 服务的收尾(关会话、停拉起的服务)最先登记:逆序执行,所以最后跑,先关运行时与端口
    early = [] if memory is None else list(memory.closers)
    # 部署对手位进部署世界:只有轮次处理器登记部署 episode,只开 admit 时不给。与轮次处理器、门② 同一个管理面
    deployment_store = None
    if "start_agent_round" in keys and memory is not None and memory.deployment is not None:
        deployment_store = memory.deployment.store
    try:
        runtime, switch = _shared_runtime(
            store, options, keys,
            turn_timeout_s=deploy_turn_timeout_s(
                interact=options.interact, starling=memory is not None and memory.backend == "starling"),
            training_worlds=None if memory is None else memory.training_worlds,
            deployment_store=deployment_store,
        )
    except BaseException:
        try:
            _close_all(early)
        except Exception:
            pass
        raise
    ctx = BuildContext(
        store=store,
        accept_mock_evidence=options.allow_mock_evidence,
        confirm_directives=options.confirm_directives,
        planner_backend=options.planner_backend,
        env=dict(environ),
        log_dir=None if log_dir is None else Path(log_dir),
        repo_commit=resolve_repo_commit(options.repo_commit),
        runtime=runtime,
        deployment_memory=None if memory is None else memory.deployment,
        closers=early,
    )
    if runtime is not None:
        ctx.register_closer(runtime.close)         # 登记一次;构造器只取用
    try:
        handlers = {key: table[key](ctx) for key in sorted(keys)}
        wiring = _interact_wiring(store, options, runtime, switch, environ, memory) if options.interact else {}
    except BaseException:
        try:
            _close_all(ctx.closers)
        except Exception:
            pass          # 正在传播的才是根因:收尾失败不能把它盖掉
        raise
    dispatcher = _dispatcher(options, runtime, data_producer=TemplateDataProducer(), **wiring)
    session = None if memory is None else memory.session
    if session is not None:
        dispatcher = _guard(dispatcher, session)
        handlers = {key: _guard(handler, session) for key, handler in handlers.items()}
    return Assembly(
        dispatcher=dispatcher,
        handlers=handlers,
        evaluator=evaluator,
        accept_mock_evidence=options.allow_mock_evidence,
        closers=tuple(ctx.closers),
        on_locked=None if memory is None else memory.on_locked,
    )


def run(
    repo_root: str,
    options: FactoryOptions = FactoryOptions(),
    *,
    interval_s: float = 2.0,
    max_ticks: int | None = None,
    env: Mapping[str, str] | None = None,
    log_dir: str | Path | None = None,
    builders: Mapping[str, HandlerBuilder] | None = None,
) -> None:
    store = GitStore(repo_root)
    a = assemble(store, options, env=env, log_dir=log_dir, builders=builders)
    try:
        if not a.closed_loop:
            run_daemon(store, interval_s, max_ticks=max_ticks, dispatcher=a.dispatcher, evaluator=a.evaluator,
                       on_locked=a.on_locked)
            return
        run_daemon(
            store, interval_s, max_ticks=max_ticks, dispatcher=a.dispatcher,
            handlers=a.handlers, evaluator=a.evaluator, accept_mock_evidence=a.accept_mock_evidence,
            on_locked=a.on_locked,      # 部署世界核对要等持锁(另一个守护进程可能正在用);run_daemon 在第一个 tick 前调
        )
    finally:
        a.close()          # --interact 单独用时没有处理器,但运行时仍要收尾


def reconcile_one_tick(
    repo_root: str,
    options: FactoryOptions = FactoryOptions(),
    *,
    env: Mapping[str, str] | None = None,
    log_dir: str | Path | None = None,
    builders: Mapping[str, HandlerBuilder] | None = None,
) -> list[Action]:
    store = GitStore(repo_root)
    # 设计 CP-12:一次性的 reconcile 也是一次写入,与 run_daemon 争同一把仓库排他锁(计划 8 任务 98)。
    # 锁在装配之前取:装配失败(退出码 2)时树里同样不写任何东西,with 会释放锁。
    with acquire_repo_lock(repo_root):
        a = assemble(store, options, env=env, log_dir=log_dir, builders=builders)
        try:
            if a.on_locked is not None:
                a.on_locked()      # 持锁之后、第一个动作之前:核对已有部署世界的设置与创建标记
            if not a.closed_loop:
                # --evaluator someval 不开闭环,但评估器必须交给调和循环,否则 request_diagnostic 走占位桩
                return reconcile_once(store, dispatcher=a.dispatcher, evaluator=a.evaluator)
            return reconcile_once(
                store, dispatcher=a.dispatcher, handlers=a.handlers, evaluator=a.evaluator,
                accept_mock_evidence=a.accept_mock_evidence,
            )
        finally:
            a.close()          # --interact 单独用时没有处理器,但运行时仍要收尾
