"""Python 入口(闭环集成设计 §10.2、R16):调和与只读可观测。门控写命令在 TypeScript 命令行。

    factory run <repo> [--enable ...] [--allow-mock-evidence] [--confirm-directives | --no-confirm-directives]
                       [--planner-backend bridge|heuristic] [--repo-commit SHA]
                       [--interval-s S] [--max-ticks N]
    factory reconcile <repo> --once [同上的装配开关]
    factory install-defaults <repo>
    factory trace <repo> <run>
    factory cost <repo> <run>
    factory logs <repo> <run> [--task <id>]

退出码:0 成功;1 只读子命令发现缺失(run 不存在、日志不可达);2 用法错误、装配错误或仓库被另一个进程锁住(只读子命令不取锁)。
"""
from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping

from controlplane.gitstore import GitStore
from controlplane.lock import RepoLockedError
from controlplane.paths import run_dir
from factory import readonly
from factory.errors import FactoryConfigError
from factory.switches import SWITCHES, UnknownSwitchError, parse_enable
from factory.wire import PLANNER_BACKENDS, FactoryOptions, HandlerBuilder, reconcile_one_tick, run


def _assembly_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("repo_root", help="ControlPlane 所在的 git 仓库根目录")
    p.add_argument("--enable", default=None,
                   help=f"逗号分隔的闭环开关,取自 {','.join(SWITCHES)};不传则与 run_controlplane.py 相同")
    p.add_argument("--allow-mock-evidence", action="store_true",
                   help="接受 backends: mock 的诊断报告驱动规划、提名与战绩(默认不接受,R17)")
    p.add_argument("--confirm-directives", dest="confirm_directives",
                   action=argparse.BooleanOptionalAction, default=None,
                   help="调度器写的指令是否先停在 awaiting_confirmation;不传则用 scheduler.yaml")
    p.add_argument("--planner-backend", choices=PLANNER_BACKENDS, default="bridge",
                   help="规划后端:bridge 走 backends.yaml 的 planner 桥接,heuristic 是确定性后端")
    p.add_argument("--repo-commit", default=None,
                   help="写进配方候选 code_version.repo_commit 的提交;不传则取代码仓库的 git rev-parse HEAD")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="factory", description="RismForge 组合根:调和循环与只读可观测")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="跑调和循环")
    _assembly_flags(p_run)
    p_run.add_argument("--interval-s", type=float, default=2.0, help="两次扫描之间的间隔秒数")
    p_run.add_argument("--max-ticks", type=int, default=None, help="跑几轮后退出;不填则一直跑")

    p_rec = sub.add_parser("reconcile", help="只跑一轮调和并打印动作")
    _assembly_flags(p_rec)
    p_rec.add_argument("--once", action="store_true", required=True, help="只跑一轮(必填)")

    p_def = sub.add_parser("install-defaults", help="写入闭环模式的默认诊断资产、评估器配置与提示模板")
    p_def.add_argument("repo_root")

    for name, text in (("trace", "打印一轮的全链路产物"), ("cost", "打印一轮的成本明细与汇总")):
        p = sub.add_parser(name, help=text)
        p.add_argument("repo_root")
        p.add_argument("run_id")

    p_logs = sub.add_parser("logs", help="按 run 目录里的 log_refs 打印日志")
    p_logs.add_argument("repo_root")
    p_logs.add_argument("run_id")
    p_logs.add_argument("--task", default=None, help="只看该任务的日志")
    return parser


def _options(args: argparse.Namespace) -> FactoryOptions:
    return FactoryOptions(
        switches=parse_enable(args.enable),
        allow_mock_evidence=args.allow_mock_evidence,
        confirm_directives=args.confirm_directives,
        planner_backend=args.planner_backend,
        repo_commit=args.repo_commit,
    )


def main(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    builders: Mapping[str, HandlerBuilder] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            run(args.repo_root, _options(args), interval_s=args.interval_s, max_ticks=args.max_ticks,
                env=env, builders=builders)
            return 0
        if args.command == "reconcile":
            actions = reconcile_one_tick(args.repo_root, _options(args), env=env, builders=builders)
            for a in actions:
                print(f"{a.kind} {a.target_id}")
            if not actions:
                print("no actions")
            return 0
        store = GitStore(args.repo_root)
        if args.command == "install-defaults":
            from factory.defaults import install_defaults  # 惰性导入:它拖进 grpc 与计划 4、5、7 的包(Ruling 16)
            for rel in install_defaults(store):
                print(rel)
            return 0
        if args.command == "trace":
            print(readonly.trace(store, args.run_id))
            return 0 if store.list_products(run_dir(args.run_id)) else 1
        if args.command == "cost":
            print(readonly.cost(store, args.run_id))
            return 0
        text, code = readonly.render_logs(store, args.run_id, task_id=args.task)
        print(text)
        return code
    except RepoLockedError as exc:
        # 设计 CP-12:另一个写者(守护进程或另一次 reconcile)正持锁;树里不写任何东西
        print(f"factory: error: repository is locked by another process: {exc}", file=sys.stderr)
        return 2
    except (FactoryConfigError, UnknownSwitchError) as exc:
        print(f"factory: error: {exc}", file=sys.stderr)
        return 2


def dev_main(argv: list[str] | None = None, **kw) -> int:
    """开发入口:run 与 reconcile 一律加上 --allow-mock-evidence(R17 的显式开关)。"""
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("run", "reconcile") and "--allow-mock-evidence" not in args:
        args.insert(1, "--allow-mock-evidence")
    return main(args, **kw)


if __name__ == "__main__":
    sys.exit(main())
