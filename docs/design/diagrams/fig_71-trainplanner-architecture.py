"""TrainPlanner 的组件图:三段分离(上下文打包、决策后端、输出解析)由 plan() 串起。"""
from svgkit import Canvas


def build() -> Canvas:
    c = Canvas(
        1080, 640, title="TrainPlanner 的三段分离",
        desc="planner.plan 串起三段:上下文打包把 PlanningContext 套进版本化模板,"
             "决策后端把打包结果变成文本,输出解析把文本严格变成 PlannerDecision。"
             "三种后端都返回文本,走同一条解析路径。包外只依赖 controlplane 与 inference_bridge。")

    pl = c.box(390, 56, 300, "planner.plan", "一次渲染 · 多次采样\n共用重试预算 · 耗尽即失败",
               kind="control", h=76)

    c.lane(14, 170, 344, 330, "① 上下文打包", kind="control")
    c.lane(368, 170, 344, 330, "② 决策后端", kind="control")
    c.lane(722, 170, 344, 330, "③ 输出解析", kind="control")

    ctx = c.box(40, 214, 292, "context", "PlanningContext 与五种子结构\n构造即校验 · 严格互逆", kind="control", h=70)
    tpl = c.box(40, 314, 292, "templates", "PLANNER_V1 · install_template\nload_template", kind="control", h=70)
    pr = c.box(40, 414, 292, "prompt", "render_prompt → PackedContext\n占位符白名单", kind="control", h=70)

    bb = c.box(394, 214, 292, "BridgeDecisionBackend", "packed.prompt 交给桥接", kind="control", h=60)
    hb = c.box(394, 314, 292, "HeuristicBackend", "只读 packed.context\n输出 JSON 文本", kind="control", h=70)
    sb = c.box(394, 414, 292, "ScriptedDecisionBackend", "测试用罐头回答", kind="control", h=60)

    dc = c.box(748, 214, 292, "decision", "parse_decision\nDecisionParseError", kind="control", h=70)
    pd = c.box(748, 314, 292, "PlannerDecision", "七个字段 · 冻结", kind="control", h=60)

    cpk = c.box(40, 548, 292, "controlplane", "Envelope · GitStore · 路径函数", kind="control", h=60)
    ib = c.box(394, 548, 292, "inference_bridge", "InferenceBridge · BridgeError", kind="infra", h=60)

    c.edge(pl, pr, "left", "right", a_f=0.5, b_f=0.8, via=[(363, 94), (363, 470)], label="渲染", label_at=0.08)
    c.edge(pl, bb, "bottom", "top", label="decide", label_at=0.35)
    c.edge(pl, dc, "right", "top", a_f=0.5, b_f=0.5, via=[(894, 94)], label="解析")
    c.edge(ctx, pr, "right", "right", via=[(346, 249), (346, 449)], label="上下文")
    c.edge(tpl, pr, "bottom", "top", label="模板文本")
    c.edge(dc, pd, "bottom", "top", label="合格才构造")
    c.edge(bb, ib, "left", "top", a_f=0.5, b_f=0.12, via=[(380, 244), (380, 530), (429, 530)])
    c.edge(tpl, cpk, "left", "left", a_f=0.5, b_f=0.5, via=[(26, 349), (26, 578)])
    c.legend(720, 580, [("control", "控制层包", False), ("infra", "共享基础设施", False)])
    return c
