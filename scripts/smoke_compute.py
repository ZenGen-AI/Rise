#!/usr/bin/env python3
"""远端计算层的冒烟:上真实主机之前,一步步确认链路是通的。

默认**只读、不动 GPU**。每一步只做一件事,打印一行 OK / FAIL,**第一个 FAIL 就停**:
后面的步骤依赖前面的,继续跑只会把一个根因变成一串噪声。

    python3 scripts/smoke_compute.py <repo_root> [--endpoint gpu-1]

走的是真正入口会走的那条配置路径(factory.compute_config),所以这里暴露的配置错误,
`factory run --executor roll` 也会在同一处暴露。需要:
  - <repo_root>/control-plane/compute/endpoints.yaml        (人工写,见设计 90 号文档)
  - 环境变量 RISMFORGE_COMPUTE_HOSTS 指向 {endpoint_id: ssh 别名} 的 YAML 文件
  - 本机 `ssh <别名> true` 免密可通

步骤(按顺序,前一步不过不会跑后一步):
  1 配置     读端点与主机映射,选定端点
  2 连通     ssh 过去执行 `true`
  3 助手     把 job_tool.py 传到 work_root/bin/ 并核对哈希           [远端写 work_root/bin/]
  4 预检     租约空闲、主机内存与磁盘够、GPU 上没有别的计算进程       [只读]
  5 空作业   起一个 `echo`,轮询到退出,取日志尾巴                     [远端写 work_root/jobs/<id>/]
  6 基座     hash-tree(sampled)base_model_path                      [只读,目录大时要几十秒]
  7 路径     roll_root 下训练脚本与导出脚本是否存在                   [只读]

不做:不取 GPU 租约、不跑训练、不导出、不删任何东西。第 5 步会留下 work_root/jobs/smoke-*/,
需要时自己删(这个脚本不删,与「绝不清理非本任务创建的东西」同一个原则)。

--local 把 ssh 换成本机 /bin/sh,用来在上机前自测脚本本身;此时主机映射里写什么别名都行。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from compute.client import ComputeClient, JobSpec  # noqa: E402
from compute.errors import ComputeError, HelperError  # noqa: E402
from compute.transport import RunResult, SshTransport  # noqa: E402
from controlplane.deferred import DeferredError  # noqa: E402
from controlplane.gitstore import GitStore  # noqa: E402
from factory.compute_config import (  # noqa: E402
    read_endpoints_config, read_host_map, select_endpoint,
)
from factory.errors import FactoryConfigError  # noqa: E402

from mindtrainer.roll_executor import TRAIN_SCRIPT  # noqa: E402  训练入口,与执行器用同一个常量,不会各写各的
EXPORT_SCRIPT = "mcore_adapter/src/mcore_adapter/models/converter/post_converter.py"  # 导出是 LoRAHFConverter 的 Python 调用,没有命令行脚本


class Stop(Exception):
    """一步失败。带一句人话,不带取值,也不带可能含内容的原始输出。"""


def _local_runner(argv, *, input: bytes, timeout: float) -> RunResult:
    """--local:把 `ssh <别名> -- <cmd>` 换成本机 /bin/sh -c '<cmd>'(与测试里同一个替身)。"""
    sep = argv.index("--")
    proc = subprocess.run(["/bin/sh", "-c", argv[sep + 2]], input=input, capture_output=True,
                          timeout=timeout, check=False)
    return RunResult(proc.returncode, proc.stdout, proc.stderr)


def _say(n: int, label: str, ok: bool, detail: str = "", took: float | None = None) -> None:
    tag = "OK  " if ok else "FAIL"
    suffix = f"  ({took:.1f}s)" if took is not None else ""
    print(f"[{n}] {tag} {label}{(': ' + detail) if detail else ''}{suffix}", flush=True)


def _step(n: int, label: str, fn):
    started = time.monotonic()
    try:
        detail = fn() or ""
    except Stop as exc:
        _say(n, label, False, str(exc), time.monotonic() - started)
        raise
    except DeferredError as exc:
        # 忙、传输故障、作业失败:可重试类。冒烟里它就是"这一步没过",原因照实写。
        _say(n, label, False, f"{type(exc).__name__}: {exc.reason}", time.monotonic() - started)
        raise Stop(str(exc.reason)) from None
    except (ComputeError, HelperError, FactoryConfigError) as exc:
        _say(n, label, False, f"{type(exc).__name__}: {exc}", time.monotonic() - started)
        raise Stop(str(exc)) from None
    _say(n, label, True, detail, time.monotonic() - started)


def run(repo_root: str, endpoint_name: str | None, *, local: bool) -> int:
    ctx: dict = {}

    def configure():
        store = GitStore(repo_root)
        endpoints = read_endpoints_config(store)
        ep = select_endpoint(endpoints, endpoint_name)
        hosts = read_host_map(env=os.environ)
        alias = hosts.get(ep.endpoint_id)
        if alias is None:
            raise Stop(f"RISMFORGE_COMPUTE_HOSTS has no entry for endpoint {ep.endpoint_id!r}")
        ctx["ep"] = ep
        # 不用 build_compute_client:--local 要换 runner,而它没有这个口子。形态校验是同一条(SshTransport)。
        runner = _local_runner if local else None
        transport = SshTransport(alias) if runner is None else SshTransport(alias, runner=runner)
        ctx["client"] = ComputeClient(ep, transport)
        ctx["alias"] = alias
        return f"endpoint {ep.endpoint_id!r}, work_root {ep.work_root}" + (" [--local]" if local else "")

    def reach():
        result = ctx["client"]._transport.run(["true"], timeout=30.0)
        if result.returncode != 0:
            tail = result.stderr.decode("utf-8", "replace").strip()[-160:]
            raise Stop(f"`ssh {ctx['alias']} true` exited {result.returncode}: {tail}")
        return f"ssh {ctx['alias']} true"

    def helper():
        ctx["client"].ensure_helper()
        return "助手已就位且哈希核对过"

    def preflight():
        if local and not Path("/proc/meminfo").exists():
            # 助手的 probe 读 /proc/meminfo,macOS 没有;真主机是 Linux,不会走到这里。
            # 自测的目的是验证脚本的其余步骤,所以明说跳过,不静默吞掉,也不改生产代码迁就。
            return "跳过:--local 且本机没有 /proc/meminfo(助手的内存探测只支持 Linux)"
        ctx["client"].preflight()
        ep = ctx["ep"]
        return (f"租约空闲;内存 >= {ep.min_free_host_mem_gib:g} GiB,磁盘 >= {ep.min_free_disk_gib:g} GiB;"
                "GPU 上没有别的计算进程")

    def echo_job():
        job_id = "smoke-" + uuid.uuid4().hex[:8]
        spec = JobSpec(argv=("echo", "smoke-ok"))
        # lease=False:冒烟不碰 GPU 租约,否则一个空作业就会挡住别人
        result = ctx["client"].run_job(job_id, spec, tail_lines=5, lease=False)
        if "smoke-ok" not in result.log_tail:
            raise Stop(f"job {job_id} exited {result.exit_code} but its log has no marker")
        ctx["job_id"] = job_id
        return f"job {job_id} 退出码 {result.exit_code},日志里有标记"

    def base_model():
        ep = ctx["ep"]
        try:
            digest = ctx["client"].hash_tree(ep.base_model_path, "sampled")
        except HelperError as exc:
            if exc.code == "not_found":
                raise Stop(f"base_model_path {ep.base_model_path} 不是远端的一个目录") from None
            raise
        return f"{ep.base_model_path} sampled 摘要 {digest[:12]}…"

    def scripts():
        ep = ctx["ep"]
        roll = ep.roll_root.rstrip("/")
        missing = []
        for rel, what in ((TRAIN_SCRIPT, "训练入口"), (EXPORT_SCRIPT, "导出转换器模块")):
            path = f"{roll}/{rel}"
            # 用 get 探测存在性会把整个脚本读回来;hash-tree 又只认目录。用 `test -f` 走一个最小作业最省。
            r = ctx["client"]._transport.run(["test", "-f", path], timeout=30.0)
            if r.returncode != 0:
                missing.append(f"{what} {path}")
        for p in (ep.sft_template, ep.heldout_jsonl):
            r = ctx["client"]._transport.run(["test", "-f", p], timeout=30.0)
            if r.returncode != 0:
                missing.append(p)
        if missing:
            raise Stop("远端找不到:" + "; ".join(missing))
        return "训练入口、导出脚本、SFT 模板、留出集都在"

    steps = [
        ("配置", configure), ("连通", reach), ("助手", helper), ("预检", preflight),
        ("空作业", echo_job), ("基座", base_model), ("路径", scripts),
    ]
    print(f"远端计算层冒烟  repo={repo_root}" + ("  [--local:ssh 换成本机 sh]" if local else ""), flush=True)
    try:
        for n, (label, fn) in enumerate(steps, 1):
            _step(n, label, fn)
    except Stop:
        print(f"\n在第 {n} 步({label})停下;后面的步骤没跑。", file=sys.stderr)
        return 1
    print("\n全部通过。远端上留有 work_root/bin/ 与 work_root/jobs/" + ctx.get("job_id", "smoke-*")
          + "/,需要时自己清。")
    print("这只说明链路通;RollExecutor 里标 V__ 的占位(训练脚本真实参数、--max-steps 能否截到段尾、"
          "--resume 路径层级、导出脚本入口)还没核实,要在真训练的第一次输出上看。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="远端计算层冒烟(默认只读、不动 GPU)")
    parser.add_argument("repo_root", help="ControlPlane 所在的 git 仓库根目录(读 compute/endpoints.yaml)")
    parser.add_argument("--endpoint", default=None, help="端点 id;只有一个端点时可省")
    parser.add_argument("--local", action="store_true",
                        help="把 ssh 换成本机 /bin/sh,用来在上机前自测脚本本身")
    args = parser.parse_args(argv)
    return run(args.repo_root, args.endpoint, local=args.local)


if __name__ == "__main__":
    sys.exit(main())
