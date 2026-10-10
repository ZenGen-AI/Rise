"""规划处理器的步骤、确定性失败出口与三个崩溃点的重入路径。"""
from svgkit import Canvas

RED = "#dc2626"


def build() -> Canvas:
    c = Canvas(
        1080, 790, title="规划处理器的步骤与重入",
        desc="处理器先看决策产物的状态:已终态就空操作,pending 就按记下的 run id 续写,不在才完整规划。"
             "run id 先落进 pending 决策,再写指令,最后把决策置 executed。任何一步确定性失败都把决策置 failed 并写 T3 失败记录。")

    g = c.gate(300, 60, 320, "决策产物的状态", "planner_decision.yaml", h=70)
    noop = c.box(40, 67, 200, "空操作", "零提交", h=56, kind="neutral")

    kw = dict(kind="control", h=56)
    x, w = 330, 260
    s2 = c.box(x, 180, w, "装配上下文", "assemble_context", **kw)
    s3 = c.box(x, 260, w, "规划", "plan() 带 validator", **kw)
    s4 = c.box(x, 340, w, "分配 run id", "allocate_run_id", **kw)
    s5 = c.box(x, 420, w, "写决策 pending", "载荷带 next_run_id", **kw)
    s6 = c.box(x, 500, w, "解析动作", "resolve_action", **kw)
    s7 = c.box(x, 580, w, "写指令", "construct_directive", **kw)
    s8 = c.box(x, 660, w, "决策置 executed", "本轮规划完成", **kw)

    fail = c.box(700, 180, 340, "决策置 failed", "failure = {T3, reason, attempts}\nrecord_failure 写失败记录\n正常返回,不上抛",
                 h=456, kind="danger")

    c.edge(g, noop, "left", "right", label="终态")
    c.edge(g, s2, "bottom", "top", label="不在")
    c.edge(g, s6, "bottom", "left", a_f=(334 - 300) / 320, via=[(334, 160), (270, 160), (270, 528)], label="(a)",
           label_at=0.45)
    c.edge(g, s8, "bottom", "left", a_f=(318 - 300) / 320, via=[(318, 150), (230, 150), (230, 688)], label="(b)",
           label_at=0.6)
    for a, b in ((s2, s3), (s3, s4), (s4, s5), (s5, s6), (s6, s7), (s7, s8)):
        c.edge(a, b, "bottom", "top")

    for node, lab in ((s2, "装配失败"), (s3, "耗尽或桥接失败"), (s4, "编号冲突"), (s6, "解析失败"), (s7, "构造失败")):
        c.edge(node, fail, "right", "left", b_f=(node.cy - fail.y) / fail.h, label=lab)

    for y, s in ((330, "崩溃点 A"), (490, "崩溃点 B"), (650, "崩溃点 C")):
        c.text(470, y, s, size=11, color=RED, bold=True)
        c.badge(x - 12, y - 4, s[-1], kind="danger")

    c.note(20, 300, 200, "A:树里什么都没写\n下次触发从头规划\nB:决策 pending、指令不在\n走 (a) 从解析续写\n"
                         "不调后端、不重新分配\nC:指令已在\n走 (b) 只置 executed", kind="danger")
    c.note(700, 660, 340, "非桥接异常(OSError 等)、配置错、模板缺失\n原样上抛,决策保持原状", kind="neutral")
    c.legend(40, 770, [("control", "处理器步骤", False), ("danger", "确定性失败与崩溃点", False),
                       ("neutral", "空操作与上抛", False)])
    return c
