"""组合根(闭环集成设计 §10.2)。

两条路径:
- 不传 --enable(switches 为空):与 mindtrainer.wire.run 完全相同,只注入
  make_mindtrainer_dispatcher(),不读任何配置,不注入评估器与数据生产器。
- 传了 --enable:启动时一次装配完(缺配置、缺资产、缺凭证立刻 FactoryConfigError),
  派发器带默认数据生产器,request_diagnostic 换成 mock 后端的真实评估器(报告标 mock),
  按开关映射出的处理器键逐个调用处理器构造器。
"""
from __future__ import annotations

import os
import subprocess
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

if TYPE_CHECKING:  # factory.agent 拖进 grpc 与 mindagent,只在类型检查时导入
    from factory.agent import AgentRuntime

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
    """--repo-commit 优先;否则 git -C <代码仓库根> rev-parse HEAD;不是 git 仓库或没有 git 时返回 None。"""
    if override is not None:
        return override
    try:
        out = subprocess.run(["git", "-C", str(code_root), "rev-parse", "HEAD"],
                             capture_output=True, text=True, check=False)
    except OSError:
        return None
    commit = out.stdout.strip()
    return commit if out.returncode == 0 and commit else None


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

    def close(self) -> None:
        """逆序收尾。一个收尾函数抛异常不能跳过其余几个,也不能盖住装配中途的原异常。"""
        _close_all(self.closers)

    @property
    def closed_loop(self) -> bool:
        return bool(self.handlers)


INTERACT_TURN_TIMEOUT_S = 30.0  # 真模型说一句比罐头慢得多;默认的 5 秒是给罐头后端定的


def _shared_runtime(store: GitStore, options: FactoryOptions, keys: frozenset[str]) -> AgentRuntime | None:
    """`--interact` 或 `--enable agent` 时造那唯一的 AgentRuntime(进程内 loopback 实时服务端),否则返回 None。

    两者同开就共用这一个,不起第二个服务端。对手罐头台词取自 agent.yaml;文件不在时给空元组,
    由 `agent` 开关的构造器照旧报"缺 agent.yaml"(--interact 单独用时,台词只是没有剧本的剧本驱动位的兜底)。
    文件在但写坏了就在这里报 FactoryConfigError,而不是留到派发期。"""
    if not options.interact and "start_agent_round" not in keys:
        return None
    from factory.agent import AGENT_CONFIG, AgentRuntime, read_agent_settings   # 惰性:拖进 grpc 与 mindagent

    lines = read_agent_settings(store)[1] if store.exists(AGENT_CONFIG) else ()
    if options.interact:
        return AgentRuntime(store, lines, turn_timeout_s=INTERACT_TURN_TIMEOUT_S)
    return AgentRuntime(store, lines)


def _dispatcher(options: FactoryOptions, runtime: AgentRuntime | None, **kwargs):
    """--interact 时给派发器注入实时客户端(并一律带数据生产器,「按种子生成」一路要靠它合成)。"""
    if options.interact:
        assert runtime is not None
        kwargs["client_factory"] = runtime.client_factory
        kwargs.setdefault("data_producer", TemplateDataProducer())
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
    client = build_compute_client(endpoint, read_host_map(env=environ))
    eval_config = read_eval_config(env=environ, repo_root=store.repo_root)
    dataset_sha = _sombench_sha(eval_config)
    row_gate = make_train_gate(load_index_file(eval_config.index_path, dataset_sha))

    def executor_factory(run_id: str) -> RollExecutor:
        return RollExecutor(client=client, endpoint=endpoint, run_id=run_id, row_gate=row_gate)

    converter = RollLoraConverter(client=client, endpoint=endpoint)
    evaluator = None
    if options.evaluator == "someval":
        evaluator = _build_someval_evaluator(store, client, endpoint, eval_config, dataset_sha)
    return Assembly(
        make_mindtrainer_dispatcher(executor_factory=executor_factory, converter=converter),
        {}, evaluator, False,
        executor_factory=executor_factory, converter=converter,
    )


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
    if not keys and not options.interact:
        # 与 mindtrainer.wire.run 逐字节相同的路径:不读配置、不注入别的
        return Assembly(make_mindtrainer_dispatcher(), {}, None, False)
    if not keys:
        # --interact 单独用:没有处理器和评估器,但有运行时要收尾
        runtime = _shared_runtime(store, options, keys)
        return Assembly(_dispatcher(options, runtime), {}, None, False, closers=(runtime.close,))
    table = default_builders() if builders is None else builders
    missing = sorted(keys - set(table))
    if missing:
        raise FactoryConfigError(f"no handler builder for {missing}")
    evaluator = build_mock_evaluator(store)
    runtime = _shared_runtime(store, options, keys)
    ctx = BuildContext(
        store=store,
        accept_mock_evidence=options.allow_mock_evidence,
        confirm_directives=options.confirm_directives,
        planner_backend=options.planner_backend,
        env=dict(os.environ if env is None else env),
        log_dir=None if log_dir is None else Path(log_dir),
        repo_commit=resolve_repo_commit(options.repo_commit),
        runtime=runtime,
    )
    if runtime is not None:
        ctx.register_closer(runtime.close)         # 登记一次;构造器只取用
    try:
        handlers = {key: table[key](ctx) for key in sorted(keys)}
    except BaseException:
        try:
            _close_all(ctx.closers)
        except Exception:
            pass          # 正在传播的才是根因:收尾失败不能把它盖掉
        raise
    return Assembly(
        dispatcher=_dispatcher(options, runtime, data_producer=TemplateDataProducer()),
        handlers=handlers,
        evaluator=evaluator,
        accept_mock_evidence=options.allow_mock_evidence,
        closers=tuple(ctx.closers),
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
            run_daemon(store, interval_s, max_ticks=max_ticks, dispatcher=a.dispatcher, evaluator=a.evaluator)
            return
        run_daemon(
            store, interval_s, max_ticks=max_ticks, dispatcher=a.dispatcher,
            handlers=a.handlers, evaluator=a.evaluator, accept_mock_evidence=a.accept_mock_evidence,
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
            if not a.closed_loop:
                # --evaluator someval 不开闭环,但评估器必须交给调和循环,否则 request_diagnostic 走占位桩
                return reconcile_once(store, dispatcher=a.dispatcher, evaluator=a.evaluator)
            return reconcile_once(
                store, dispatcher=a.dispatcher, handlers=a.handlers, evaluator=a.evaluator,
                accept_mock_evidence=a.accept_mock_evidence,
            )
        finally:
            a.close()          # --interact 单独用时没有处理器,但运行时仍要收尾
