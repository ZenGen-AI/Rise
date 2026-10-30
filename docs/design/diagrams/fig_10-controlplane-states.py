"""两类状态机,标出每条迁移由谁驱动。与 src/controlplane/states.py 的迁移表一致。"""
from svgkit import Canvas

ORANGE = "#d97706"


def build() -> Canvas:
    c = Canvas(
        1080, 560, title="两类状态机与驱动者",
        desc="需审核类:候选由派发器直接写成 pending_review,命令行批准或拒绝,守护进程晋级为 effective。"
             "非审核类:指令由守护进程置 executed 或 failed;awaiting_confirmation 经命令行 confirm 转 pending。"
             "迁移表还允许 draft 提交审核、effective 废弃或被取代、executed 被下游消费。")
    c.lane(14, 44, 520, 460, "需审核类:候选 checkpoint · 配方候选 · 技能记忆提案", kind="control", dashed=False)
    c.lane(546, 44, 520, 460, "非审核类:directive(其余产物直接写成终态)", kind="compute", dashed=False)

    kw = dict(h=40, title_size=14)
    draft = c.box(50, 96, 170, "draft", kind="neutral", **kw)
    pend = c.box(50, 196, 170, "pending_review", kind="human", **kw)
    appr = c.box(50, 296, 170, "approved", kind="control", **kw)
    eff = c.box(50, 396, 170, "effective", kind="compute", **kw)
    rej = c.box(330, 196, 160, "rejected", kind="danger", **kw)
    dep = c.box(330, 370, 160, "deprecated", kind="neutral", **kw)
    sup = c.box(330, 440, 160, "superseded", kind="neutral", **kw)
    c.edge(draft, pend, "bottom", "top", label="提交审核")
    c.edge(pend, appr, "bottom", "top", label="CLI approve", color=ORANGE)
    c.edge(pend, rej, label="CLI reject", color=ORANGE)
    c.edge(appr, eff, "bottom", "top", label="promote_candidate")
    c.edge(appr, rej, "right", "bottom", label="仅收录处理器", label_at=0.35, dashed=True)
    c.edge(eff, dep, "right", "left", a_f=0.3, label="战绩退化")
    c.edge(eff, sup, "right", "left", a_f=0.8, label="被取代")
    c.note(270, 96, 240, "派发器写候选时直接写\npending_review,不经 draft", kind="neutral")

    aw = c.box(590, 96, 230, "awaiting_confirmation", kind="human", **kw)
    pe = c.box(590, 196, 230, "pending", kind="control", **kw)
    ex = c.box(590, 296, 230, "executed", kind="compute", **kw)
    co = c.box(590, 396, 230, "consumed", kind="neutral", **kw)
    fa = c.box(890, 296, 150, "failed", kind="danger", **kw)
    c.edge(aw, pe, "bottom", "top", label="CLI confirm", color=ORANGE)
    c.edge(pe, ex, "bottom", "top", label="派发器正常返回")
    c.edge(ex, co, "bottom", "top", label="被下游读取")
    c.edge(pe, fa, "right", "top", b_f=0.3, label="DirectiveFailedError", label_at=0.75)
    c.edge(aw, fa, "right", "top", b_f=0.8, dashed=True, label="人工改文件", label_at=0.35)
    c.note(860, 396, 190, "failed 是终态\n原因写进 payload.failure", kind="neutral")

    c.legend(40, 534, [("human", "等待人工", False), ("compute", "生效 / 完成", False),
                       ("danger", "失败 / 拒绝", False), ("neutral", "虚线 = 人工直接改文件 / approved → rejected 仅收录处理器走", True)])
    return c
