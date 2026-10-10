"""可观测的三层数据:谁写、存在哪、哪个只读命令读它。"""
from svgkit import Canvas


def build() -> Canvas:
    c = Canvas(
        1080, 560, title="可观测的三层数据",
        desc="L1 在 git 树里:记账模块写 run_trace 的成本与失败记录。L3 本地日志由命令行桥接写,树里只留引用。"
             "L2 指标与计算层 L3 日志留在计算层,只读命令读不到。cost 读 run_trace,trace 列产物与引用可达性,logs 顺引用读日志。")
    c.lane(14, 44, 780, 200, "L1 结构化摘要:git 树", kind="store", dashed=False)
    c.lane(14, 258, 780, 120, "L3 桥接日志:控制面仓库工作区,git 不可见", kind="infra")
    c.lane(14, 392, 780, 120, "L2 指标流与计算层 L3 日志", kind="compute")
    c.lane(806, 44, 260, 468, "只读命令", kind="neutral")

    led = c.box(30, 110, 230, "controlplane.ledger", "record_cost\nrecord_failure", kind="control", h=80)
    rt = c.box(330, 76, 220, "run_trace.yaml", "cost_records", kind="store", h=64)
    fr = c.box(330, 164, 220, "failure-<task>.yaml", "category · log_refs", kind="store", h=64)
    cb = c.box(30, 290, 230, "CliBridge", "每次调用一份日志", kind="infra", h=70)
    lf = c.store(330, 284, 220, "本地日志文件", ".rismforge/logs/<run>/")
    ct = c.box(30, 424, 230, "计算层任务", "训练 · 评测进程", kind="compute", h=70)
    cf = c.store(330, 418, 220, "计算层文件", "指标流 · 进程日志")

    cost = c.box(830, 76, 220, "cost", "明细与汇总", kind="neutral", h=64)
    tr = c.box(830, 210, 220, "trace", "产物状态 · links\n引用可达性", kind="neutral", h=80)
    lo = c.box(830, 380, 220, "logs", "顺 log_refs 读内容", kind="neutral", h=80)

    c.edge(led, rt, a_f=0.3, b_f=0.6)
    c.edge(led, fr, a_f=0.7, b_f=0.4)
    c.edge(cb, led, "top", "bottom", label="成功后记账")
    c.edge(cb, lf, label="脱敏写入")
    c.edge(fr, lf, "bottom", "top", label="log_refs")
    c.edge(ct, cf, label="写入")
    c.edge(rt, cost, label="读")
    c.edge(fr, tr, b_f=0.4, label="读")
    c.edge(lf, lo, b_f=0.3, label="file:// 读")
    c.edge(cf, lo, b_f=0.75, label="不可达", dashed=True)

    c.legend(40, 540, [("store", "git 树里的产物", False), ("infra", "桥接", False),
                       ("compute", "计算层", False), ("neutral", "虚线 = 命令读不到", True)])
    return c
