"""端到端测试走的闭环:手工登记基线,一条人工指令转两轮,并行验证 MindAgent 链。"""
from svgkit import Canvas


def build() -> Canvas:
    c = Canvas(
        1080, 640, title="端到端测试走的闭环",
        desc="测试先手工登记全错的基线 run-000 与 zengen-v1,再从人工写的 run-001 出发,"
             "每一步都经 factory reconcile --once,人工动作用写状态模拟。"
             "主链转到第二轮训练与第三条指令;MindAgent 链在同一套装配下并行验证。")
    c.lane(14, 44, 1052, 330, "主链:test_the_whole_loop_turns_twice", kind="control", dashed=False)
    c.lane(14, 400, 1052, 180, "MindAgent 链:test_the_mindagent_line_runs_inside_the_reconcile_loop", kind="compute", dashed=False)

    xs = [36 + 208 * i for i in range(5)]
    b0 = c.box(xs[0], 90, 180, "登记基线", "run-000 全错报告\nzengen-v1", kind="human", h=70)
    t1 = c.box(xs[1], 90, 180, "run-001 训练", "派发 · mock 诊断", kind="compute", h=70)
    g1 = c.gate(xs[2], 90, 180, "门①批准", "写状态模拟 approve", h=70)
    p1 = c.box(xs[3], 90, 180, "晋级 zengen-v2", "promote_candidate", kind="control", h=70)
    n1 = c.box(xs[4], 90, 180, "提名配方", "增益 > min_gain", kind="control", h=70)

    g3 = c.gate(xs[4], 240, 180, "门③批准并收录", "recipe-run-001", h=70)
    pl = c.box(xs[3], 240, 180, "规划 run-002", "HeuristicBackend clone", kind="control", h=70)
    cf = c.gate(xs[2], 240, 180, "确认指令", "awaiting → pending", h=70)
    t2 = c.box(xs[1], 240, 180, "第二轮训练", "晋级 zengen-v3", kind="compute", h=70)
    e2 = c.box(xs[0], 240, 180, "战绩 · 再规划", "run-003 待确认", kind="control", h=70)

    a1 = c.box(36, 470, 520, "两次轮次 start_agent_round", "zengen-v2 空库零提案 · 放基线技能\nzengen-v3 产出 pending_review 提案",
               kind="compute", h=70)
    a3 = c.gate(624, 470, 420, "门②批准并收录", "admit_proposal\n库版本是字符串", h=70)

    for s, t in ((b0, t1), (t1, g1), (g1, p1), (p1, n1)):
        c.edge(s, t)
    c.edge(n1, g3, "bottom", "top")
    for s, t in ((g3, pl), (pl, cf), (cf, t2), (t2, e2)):
        c.edge(s, t, "left", "right")
    c.edge(a1, a3)

    steps = (b0, t1, g1, p1, n1, g3, pl, cf, t2, e2)
    for n, node in enumerate(steps, 1):
        c.badge(node.x + (16 if node in (g1, g3, cf) else 0), node.y, n, kind="control")
    for n, node in zip((11, 12), (a1, a3)):
        c.badge(node.x + (16 if node is a3 else 0), node.y, n, kind="compute")

    c.legend(40, 616, [("human", "人工动作与审核门", False), ("compute", "计算层动作", False),
                       ("control", "控制层动作", False)])
    return c
