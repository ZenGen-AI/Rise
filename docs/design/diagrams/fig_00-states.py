"""两套产物状态机:需审核类与非审核类。与 src/controlplane/states.py 的迁移表一致,
approved → rejected 是 Python 侧独有的一条边,只许收录处理器走,用虚线画。"""
from svgkit import Canvas

ORANGE = "#d97706"


def build() -> Canvas:
    c = Canvas(
        1080, 520, title="产物状态机",
        desc="需审核类产物经人工批准才能生效,可被拒绝,生效后可废弃或被取代。"
             "已批准的提案在收录失败时,只许收录处理器把它改为被拒绝。"
             "非审核类产物从待执行走到已执行再到已消费,失败是保留记录的终态。")
    c.lane(14, 44, 500, 440, "需审核类产物:checkpoint 候选 · 配方候选 · 技能记忆提案", kind="control", dashed=False)
    c.lane(566, 44, 500, 440, "非审核类产物:指令 · 诊断报告 · 轮次记录", kind="compute", dashed=False)

    kw = dict(h=40, title_size=14)
    draft = c.box(60, 96, 160, "draft", kind="neutral", **kw)
    pend = c.box(60, 176, 160, "pending_review", kind="human", **kw)
    appr = c.box(60, 256, 160, "approved", kind="control", **kw)
    eff = c.box(60, 336, 160, "effective", kind="compute", **kw)
    rej = c.box(300, 176, 140, "rejected", kind="danger", **kw)
    dep = c.box(300, 336, 140, "deprecated", kind="neutral", **kw)
    sup = c.box(300, 396, 140, "superseded", kind="neutral", **kw)
    c.edge(draft, pend, "bottom", "top", label="提交审核")
    c.edge(pend, appr, "bottom", "top", label="approve", color=ORANGE)
    c.edge(pend, rej, label="reject", color=ORANGE)
    c.edge(appr, eff, "bottom", "top", label="收录 / 晋级")
    c.edge(appr, rej, "right", "bottom", label="仅收录处理器", label_at=0.35, dashed=True)
    c.edge(eff, dep, label="战绩退化")
    c.edge(eff, sup, "bottom", "left", label="被取代")
    c.note(300, 96, 190, "拒绝与废弃都保留记录\n被拒绝的候选进入下一轮规划", kind="neutral")

    aw = c.box(610, 96, 220, "awaiting_confirmation", kind="human", **kw)
    pe = c.box(610, 196, 220, "pending", kind="control", **kw)
    ex = c.box(610, 296, 220, "executed", kind="compute", **kw)
    co = c.box(610, 396, 220, "consumed", kind="neutral", **kw)
    fa = c.box(880, 296, 140, "failed", kind="danger", **kw)
    c.edge(aw, pe, "bottom", "top", label="confirm 命令", color=ORANGE)
    c.edge(pe, ex, "bottom", "top", label="派发成功")
    c.edge(ex, co, "bottom", "top", label="被下游读取")
    c.edge(pe, fa, "right", "top", b_f=0.3, label="确定性失败", label_at=0.25)
    c.edge(aw, fa, "right", "top", b_f=0.8, dashed=True, label="人工改文件", label_at=0.3)
    c.note(860, 380, 190, "failed 是终态,记录保留\n原因记入载荷", kind="neutral")
    c.legend(40, 506, [("human", "等待人工", False), ("compute", "生效 / 完成", False),
                       ("danger", "失败 / 拒绝", False), ("neutral", "虚线 = 人工直接改文件 / approved → rejected 仅收录处理器走", True)])
    return c
