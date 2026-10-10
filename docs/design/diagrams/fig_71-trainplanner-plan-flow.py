"""plan() 的重采样流程:一次渲染,多次采样,解析失败与校验违规共用一个预算。"""
from svgkit import Canvas

RED = "#dc2626"


def build() -> Canvas:
    c = Canvas(
        1080, 600, title="plan() 的重采样流程",
        desc="模板只渲染一次。每次尝试把此前的被拒原因拼进提示交给后端,严格解析,再交校验器。"
             "解析失败与校验违规都记一条原因并消耗同一个预算,预算用完抛 PlannerFailedError。"
             "渲染错误与后端异常不进重试,原样上抛。")

    s1 = c.box(40, 70, 220, "渲染提示", "render_prompt\n只做一次", kind="control", h=72)
    s2 = c.box(300, 70, 220, "拼入被拒原因", "with_feedback\n第 n 次带前 n−1 条", kind="control", h=72)
    s3 = c.box(560, 70, 220, "后端给出文本", "backend.decide\ntask_id 每次不同", kind="control", h=72)
    s4 = c.box(820, 70, 220, "严格解析", "parse_decision", kind="control", h=72)
    s5 = c.box(820, 230, 220, "领域校验", "validator(decision)\nNone 即通过", kind="control", h=72)
    s6 = c.box(820, 390, 220, "返回 PlannerResult", "决策 · 理由 · 模板版本\n尝试次数 · 后端名", kind="control", h=72)
    rec = c.box(430, 230, 260, "记一条被拒原因", "parse error 或 invalid decision", kind="danger", h=60)
    gate = c.gate(430, 360, 260, "预算还有剩?", "总次数 = 1 + max_parse_retries", h=72, kind="neutral")
    fail = c.box(430, 500, 260, "PlannerFailedError", "reason · attempts · reasons", kind="danger", h=60)
    up = c.box(40, 230, 260, "原样上抛,不计预算", "PromptRenderError\nBridgeError 与其他后端异常", kind="danger", h=72)

    c.edge(s1, s2)
    c.edge(s2, s3)
    c.edge(s3, s4)
    c.edge(s4, s5, "bottom", "top", label="合格")
    c.edge(s5, s6, "bottom", "top", label="通过")
    c.edge(s4, rec, "bottom", "right", a_f=0.2, b_f=0.3, via=[(864, 190), (720, 190), (720, 248)], label="不合格", color=RED)
    c.edge(s5, rec, "left", "right", a_f=0.65, b_f=0.75, label="违规", color=RED)
    c.edge(rec, gate, "bottom", "top")
    c.edge(gate, s2, "left", "bottom", a_f=0.5, b_f=0.5, via=[(330, 396), (330, 170), (410, 170)], label="有,再采样")
    c.edge(gate, fail, "bottom", "top", label="无")
    c.edge(s1, up, "bottom", "top", a_f=0.5, b_f=0.4231, label="模板缺陷", color=RED)
    c.edge(s3, up, "top", "left", a_f=0.2, b_f=0.5, via=[(604, 48), (22, 48), (22, 266)], label="后端异常", color=RED)

    for n, node in enumerate((s1, s2, s3, s4, s5, s6), 1):
        c.badge(node.x, node.y, n, kind="control")
    c.legend(40, 580, [("control", "正常步骤", False), ("danger", "失败与上抛", False)])
    return c
