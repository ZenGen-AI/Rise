"""接线入口:把 ControlPlane 的调和循环接到 MindTrainer 派发器上。

分层方向:入口住在 mindtrainer 侧(mindtrainer 依赖 controlplane,反之不成立),
所以 controlplane.daemon 只见到一个 Callable,不认识 MindTrainer。

训练用 mock 执行器与 mock 转换器(设计 §11:真实 ROLL 后置)。调和循环本身是真的:
它扫 git 树、决定动作、写产物、把指令置 executed。

batch 指令可跑;interact 指令还需要 client_factory 与 policy(实时服务端与策略模型),
本入口不提供,所以 interact 指令会被判 failed 并写明原因,而不是静默跑错。

request_diagnostic 仍是骨架里的桩,所以诊断报告带着 stub 标记,不是真实评估结果。
循环遇到第一个没被接住的异常就整段退出,需要外部的监督者(supervisor)把它重启,
本入口自己不重试。failed 是终态,不会被自动重跑;一条指令要能跑,它点名的数据集清单
必须先在 git 树里,而不是指令本身先就位、清单随后再补。
"""
from __future__ import annotations

import argparse
import sys

from controlplane.daemon import run_daemon
from controlplane.gitstore import GitStore
from mindtrainer.dispatch import make_mindtrainer_dispatcher


def run(repo_root: str, *, interval_s: float = 2.0, max_ticks: int | None = None) -> None:
    """在 repo_root 上跑调和循环,下发指令时用 MindTrainer 派发器。"""
    store = GitStore(repo_root)
    run_daemon(
        store, interval_s, max_ticks=max_ticks,
        dispatcher=make_mindtrainer_dispatcher(),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="调和循环 + MindTrainer 派发器")
    parser.add_argument("repo_root", help="ControlPlane 所在的 git 仓库根目录")
    parser.add_argument("--interval-s", type=float, default=2.0, help="两次扫描之间的间隔秒数")
    parser.add_argument("--max-ticks", type=int, default=None, help="跑几轮后退出;不填则一直跑")
    args = parser.parse_args(argv)
    run(args.repo_root, interval_s=args.interval_s, max_ticks=args.max_ticks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
