"""一轮迭代与晋级之后并行的三条支线。步骤编号与总览正文的步骤表一一对应。"""
from svgkit import Canvas


def build() -> Canvas:
    c = Canvas(
        1080, 700, title="一轮迭代的闭环",
        desc="主链路从写入训练指令开始,经训练、诊断评测、人工审核门①晋级为新基座。"
             "晋级之后三条支线并行:规划下一轮并回到起点,提名配方经门③入库,MindAgent 自进化经门②入库。")
    c.lane(14, 44, 1052, 140, "主链路:每一轮都走", kind="control", dashed=False)
    c.lane(14, 330, 1052, 310, "晋级之后并行的三条支线", kind="neutral")

    xs = [24 + 214 * i for i in range(5)]
    a = c.box(xs[0], 86, 176, "写入训练指令", "人工或调度器写入\ndirective", kind="human", h=84)
    b = c.box(xs[1], 86, 176, "派发并训练", "缺清单时先合成数据\nSFT → GRPO + OPD", kind="compute", h=84)
    cc = c.box(xs[2], 86, 176, "候选 checkpoint", "pending_review\n等待诊断与审核", kind="compute", h=84)
    d = c.box(xs[3], 86, 176, "诊断评测", "DiagnosticReport\n编排器 run_diagnostic", kind="compute", h=84)
    e = c.gate(xs[4], 86, 176, "门①人工审核", "approve / reject\n需要诊断报告在场", h=84)

    f = c.box(415, 232, 250, "晋级 zengen-vN", "候选 → effective\n新基座 = 最新 effective 版本", kind="control", h=70)

    g = c.box(40, 384, 250, "规划下一轮", "装配上下文 · 决策\n写入下一条 directive", kind="control", h=76)
    h = c.box(415, 384, 250, "提名配方", "增益达标且无退化标记\n写入 recipe_candidate", kind="control", h=76)
    i = c.box(790, 384, 250, "MindAgent 轮次", "部署模拟 · 经验分析\n技能提案 · 离线验证", kind="compute", h=76)
    g3 = c.gate(415, 486, 250, "门③ 配方入库", "需要可复现性校验报告", h=60)
    rs = c.box(415, 572, 250, "RecipeStore 收录", "recipe · lineage · track_record", kind="control", h=60)
    g2 = c.gate(790, 486, 250, "门② 技能记忆纳入", "需要验证结果在场", h=60)
    lib = c.box(790, 572, 250, "正式技能库与记忆库", "library_state 版本递增", kind="compute", h=60)

    for s, t in ((a, b), (b, cc), (cc, d), (d, e)):
        c.edge(s, t)
    c.edge(e, f, "bottom", "top")
    c.edge(f, g, "left", "top", b_f=0.7)
    c.edge(f, h, "bottom", "top")
    c.edge(f, i, "right", "top")
    c.edge(g, a, "top", "bottom", a_f=0.1, b_f=(40 + 0.1 * 250 - 24) / 176, label="下一轮")
    c.edge(h, g3, "bottom", "top")
    c.edge(g3, rs, "bottom", "top")
    c.edge(i, g2, "bottom", "top")
    c.edge(g2, lib, "bottom", "top")

    for n, node in enumerate((a, b, cc, d, e), 1):
        c.badge(node.x + (16 if node is e else 0), node.y, n, kind="control")
    for n, node in zip((6, 7, 8, 9), (f, g, h, i)):
        c.badge(node.x, node.y, n, kind="control")

    c.note(40, 500, 250, "晋级之后三条支线并行\n规划不等待 MindAgent\n两者各自由产物状态触发", kind="neutral")
    c.legend(40, 672, [("human", "人工动作与审核门", False), ("compute", "计算层动作", False),
                       ("control", "控制层动作", False)])
    return c
