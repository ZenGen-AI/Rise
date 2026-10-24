"""一次调和 tick 的流程。步骤编号与正文步骤表一一对应。"""
from svgkit import Canvas

RED = "#dc2626"


def build() -> Canvas:
    c = Canvas(
        1080, 600, title="一次调和 tick",
        desc="读树投影成视图,纯函数算出动作列表,逐个执行。"
             "下发指令走派发器,诊断走评估器,晋级做三次写入,等待审核不做事。"
             "资源暂时不可用时延后重试;读不出的产物记为 unreadable 并暂停闭环;"
             "其余未捕获的异常让守护进程退出;正常结束后休眠再来。")

    rd = c.box(40, 60, 300, "读树 read_tree_state", "扫 runs/* 的指令与候选\n取 models/ 的最大版本号\n读不出的记为 unreadable", kind="control", h=88)
    rc = c.box(410, 60, 290, "决策 reconcile", "纯函数,无 IO\n输出有序动作列表", kind="control", h=88)
    sl = c.box(790, 60, 250, "休眠 interval_s", "max_ticks 到达则退出", kind="neutral", h=88)

    dp = c.box(40, 230, 230, "dispatch_directive", "调派发器\n返回 → executed\nDirectiveFailedError → failed", kind="compute", h=92)
    dg = c.box(300, 230, 230, "request_diagnostic", "评估器写 diagnostic_report\n缺省时由桩占位", kind="compute", h=92)
    pr = c.box(560, 230, 230, "promote_candidate", "写 zengen-vN\n候选 → effective\n写 outcome", kind="control", h=92)
    aw = c.gate(820, 230, 220, "await_review", "无自动动作\n等人工审核门①", h=92)

    ex = c.box(40, 420, 230, "未捕获异常上抛", "派发器其他异常\ngit 失败 · OSError\n守护进程退出\n指令仍 pending 等重启", kind="danger", h=110)
    df = c.box(300, 420, 230, "DeferredError 延后", "不写树 · 状态不动\n退避重试,超上限才退出", kind="neutral", h=110)
    tr = c.store(560, 430, 480, "git 产物树", "每次写入一个提交 · 内容相同不提交", h=86)

    c.edge(rd, rc, label="TreeState")
    c.edge(rc, sl, label="动作执行完")
    c.edge(sl, rd, "top", "top", via=[(915, 30), (190, 30)], label="下一次 tick")
    for node, f in ((dp, 0.1), (dg, 0.35), (pr, 0.6), (aw, 0.9)):
        c.edge(rc, node, "bottom", "top", a_f=f)
    c.edge(dp, ex, "bottom", "top", color=RED, label="其他异常")
    c.edge(dp, df, "bottom", "top", a_f=0.9, b_f=0.1, label="资源暂不可用")
    c.edge(dg, tr, "bottom", "top", b_f=0.05)
    c.edge(pr, tr, "bottom", "top", a_f=0.5, b_f=(675 - 560) / 480, label="三次写入")

    for n, node in enumerate((rd, rc, dp, dg, pr, aw), 1):
        c.badge(node.x + (16 if node is aw else 0), node.y, n, kind="control")
    c.badge(sl.x, sl.y, 7, kind="control")
    c.legend(40, 580, [("control", "ControlPlane 自身", False), ("compute", "经注入函数完成", False),
                       ("human", "等待人工", False), ("danger", "失败路径", False), ("store", "存储", False)])
    return c
