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

PLANNER_BACKENDS = ("bridge", "heuristic")
EXECUTORS = ("mock", "roll")


@dataclass(frozen=True)
class FactoryOptions:
    switches: tuple[str, ...] = ()
    allow_mock_evidence: bool = False
    confirm_directives: bool | None = None  # None:用 scheduler.yaml 的 confirm_directives
    planner_backend: str = "bridge"
    repo_commit: str | None = None  # None:取代码仓库的 git rev-parse HEAD(取不到仍是 None)
    executor: str = "mock"  # roll:真实 ROLL LoRA SFT 执行器(设计 FC-15),默认 mock
    endpoint: str | None = None  # 只在 executor == "roll" 时有意义:点名 compute/endpoints.yaml 里的端点


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
    env: Mapping[str, str]
    log_dir: Path | None
    repo_commit: str | None = None
    closers: list = field(default_factory=list)

    def bridge(self, role: str) -> CliBridge:
        return build_bridge(self.store, role, env=self.env, log_dir=self.log_dir)

    def register_closer(self, close: Callable[[], None]) -> None:
        """构造器起了要收尾的资源(如 loopback 实时服务端)时登记;run 结束时逆序调用。"""
        self.closers.append(close)


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
        for close in reversed(self.closers):
            close()

    @property
    def closed_loop(self) -> bool:
        return bool(self.handlers)


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

    environ = os.environ if env is None else env
    endpoint = select_endpoint(read_endpoints_config(store), options.endpoint)
    client = build_compute_client(endpoint, read_host_map(env=environ))

    def executor_factory(run_id: str) -> RollExecutor:
        return RollExecutor(client=client, endpoint=endpoint, run_id=run_id)

    converter = RollLoraConverter(client=client, endpoint=endpoint)
    return Assembly(
        make_mindtrainer_dispatcher(executor_factory=executor_factory, converter=converter),
        {}, None, False,
        executor_factory=executor_factory, converter=converter,
    )


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
    if options.endpoint is not None and options.executor != "roll":
        raise FactoryConfigError("--endpoint only makes sense with --executor roll")
    if options.executor == "roll":
        if keys:
            # 设计保证 7:真训练同步占住调和循环数小时(CP-15),闭环处理器的排队语义是按 mock
            # 训练写的;先让一条链路跑通真机,再决定叠加。以后放开比以后收紧容易。
            raise FactoryConfigError(
                "--executor roll cannot be combined with --enable yet: a real training run "
                "holds the reconcile loop for hours"
            )
        return _assemble_roll(store, options, env=env)
    if not keys:
        # 与 mindtrainer.wire.run 逐字节相同的路径:不读配置、不注入别的
        return Assembly(make_mindtrainer_dispatcher(), {}, None, False)
    table = default_builders() if builders is None else builders
    missing = sorted(keys - set(table))
    if missing:
        raise FactoryConfigError(f"no handler builder for {missing}")
    evaluator = build_mock_evaluator(store)
    ctx = BuildContext(
        store=store,
        accept_mock_evidence=options.allow_mock_evidence,
        confirm_directives=options.confirm_directives,
        planner_backend=options.planner_backend,
        env=dict(os.environ if env is None else env),
        log_dir=None if log_dir is None else Path(log_dir),
        repo_commit=resolve_repo_commit(options.repo_commit),
    )
    try:
        handlers = {key: table[key](ctx) for key in sorted(keys)}
    except BaseException:
        for close in reversed(ctx.closers):
            close()
        raise
    return Assembly(
        dispatcher=make_mindtrainer_dispatcher(data_producer=TemplateDataProducer()),
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
    if not a.closed_loop:
        run_daemon(store, interval_s, max_ticks=max_ticks, dispatcher=a.dispatcher)
        return
    try:
        run_daemon(
            store, interval_s, max_ticks=max_ticks, dispatcher=a.dispatcher,
            handlers=a.handlers, evaluator=a.evaluator, accept_mock_evidence=a.accept_mock_evidence,
        )
    finally:
        a.close()


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
        if not a.closed_loop:
            return reconcile_once(store, dispatcher=a.dispatcher)
        try:
            return reconcile_once(
                store, dispatcher=a.dispatcher, handlers=a.handlers, evaluator=a.evaluator,
                accept_mock_evidence=a.accept_mock_evidence,
            )
        finally:
            a.close()
