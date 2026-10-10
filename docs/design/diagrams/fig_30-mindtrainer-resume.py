"""续训判定 decide_resume:六条规则按顺序判,先命中者生效。"""
from svgkit import Canvas


def build() -> Canvas:
    c = Canvas(
        1080, 610, title="续训判定",
        desc="decide_resume 对已有的训练记录按六条规则依次判定,先命中者生效;未知状态直接抛 ValueError。"
             "结论只有四种:noop 原样返回,resume 从中间存点续,restart 从基座重来,escalate 交给人。")
    GW, GH = 330, 52
    xs = 40
    g1 = c.gate(xs, 56, GW, "已 executed 或 consumed?", h=GH, kind="control")
    g2 = c.gate(xs, 140, GW, "状态是 failed?", h=GH, kind="control")
    g3 = c.gate(xs, 224, GW, "resume_attempts ≥ 上限?", h=GH, kind="control")
    g4 = c.gate(xs, 308, GW, "allow_resume 为假?", h=GH, kind="control")
    g5 = c.gate(xs, 392, GW, "有 last_checkpoint_ref?", h=GH, kind="control")
    g6 = c.gate(xs, 476, GW, "checkpoint_intact 为 True?", h=GH, kind="control")

    noop = c.box(720, 56, 300, "noop", "原样返回已有记录", kind="neutral", h=GH)
    esc = c.box(720, 140, 300, "escalate", "抛 NeedsHumanError\n派发器记 T3,指令置 failed\n记录本身不改", kind="human", h=220)
    rst = c.box(720, 392, 300, "restart", "从 base_checkpoint_ref 的 step 0 重来", kind="compute", h=GH)
    rsm = c.box(720, 476, 300, "resume", "从 checkpoint_step 续训", kind="compute", h=GH)

    for a, b in ((g1, g2), (g2, g3), (g3, g4), (g4, g5)):
        c.edge(a, b, "bottom", "top", label="否")
    c.edge(g5, g6, "bottom", "top", label="是")
    c.edge(g1, noop, label="是")
    c.edge(g2, esc, b_f=(166 - 140) / 220, label="是 · 规则 2")
    c.edge(g3, esc, b_f=(250 - 140) / 220, label="是 · 规则 3")
    c.edge(g4, esc, b_f=(334 - 140) / 220, label="是 · 规则 4")
    c.edge(g5, rst, label="否 · 从未存点")
    c.edge(g6, rsm, label="是")
    c.edge(g6, esc, "bottom", "right", b_f=0.85,
           via=[(xs + GW / 2, 556), (1048, 556), (1048, 140 + 220 * 0.85)], label="否 · 损坏或无法确认", label_at=0.3)
    c.legend(40, 594, [("control", "判断点", False), ("human", "需人工 T3", False),
                       ("compute", "自动续训", False), ("neutral", "无动作", False)])
    return c
