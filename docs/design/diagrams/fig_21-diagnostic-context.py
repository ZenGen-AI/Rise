"""诊断评测的上下文图:谁请求诊断,它读谁、调谁,报告被谁读。"""
from svgkit import Canvas


def build() -> Canvas:
    c = Canvas(
        1080, 720, title="诊断评测的上下文",
        desc="ControlPlane 在候选待审时请求诊断,由注入的评估器调用诊断评测并写报告。"
             "诊断评测调 SoMEval 运行器与 judge 后端,动态轨复用 SocialSimulator 的运行器。"
             "报告写进产物树,由审核门①、规划与配方增益读取。")
    c.lane(14, 44, 1052, 128, "控制层", kind="control")
    c.lane(14, 196, 1052, 124, "协调层", kind="control", dashed=False)
    c.lane(14, 344, 1052, 160, "计算层", kind="compute", label_align="end")
    c.lane(14, 528, 1052, 140, "外部评测后端(经协议注入)", kind="infra")

    ts = c.box(60, 90, 230, "TrainScheduler", "装配规划上下文", kind="control", h=60)
    rs = c.box(330, 90, 230, "RecipeStore", "增益与战绩", kind="control", h=60)
    gate = c.gate(820, 84, 200, "审核门①", "review 打印报告\napprove 要求报告在场", h=74)

    cp = c.box(60, 236, 330, "ControlPlane", "调和循环 · request_diagnostic", kind="control", h=64)
    tree = c.store(560, 226, 300, "产物树", "runs/<run>/diagnostic_report.yaml", h=80)

    ss = c.box(40, 400, 230, "SocialSimulator 运行器", "run_episode · 多轮驱动", kind="compute", h=70)
    dx = c.box(390, 390, 300, "诊断评测", "src/diagnostic\n双轨评测 · 聚合 · 退化判定", kind="compute", h=90)
    gen = c.box(810, 400, 230, "情境生成器", "训练情境批量生成", kind="compute", h=70)

    sm = c.box(300, 584, 220, "SoMEval 运行器", "SoMEvalRunner 协议", kind="infra", h=60)
    jd = c.box(600, 584, 220, "judge 后端", "JudgeBackend 协议", kind="infra", h=60)

    c.edge(tree, ts, "top", "bottom", a_f=0.1, via=[(590, 206), (175, 206)], label="读分数与退化")
    c.edge(tree, rs, "top", "bottom", a_f=0.3, via=[(650, 180), (445, 180)], label="读 L1 分")
    c.edge(tree, gate, "right", "bottom", label="读报告")
    c.edge(cp, dx, "bottom", "top", a_f=0.85, b_f=0.1, label="request_diagnostic")
    c.edge(dx, tree, "top", "bottom", a_f=0.75, b_f=0.4, label="写报告")
    c.edge(dx, ss, "left", "right", label="动态轨")
    c.edge(gen, dx, "left", "right", label="隔离校验")
    c.edge(dx, sm, "bottom", "top", a_f=0.25, label="静态轨")
    c.edge(dx, jd, "bottom", "top", a_f=0.75, label="judge 打分")

    c.note(40, 572, 230, "评估器由 factory 注入守护进程\n替换默认的占位诊断\n报告带 backends 标记")
    c.legend(40, 698, [("control", "控制层与协调层", False), ("compute", "计算层", False),
                       ("infra", "外部后端", False), ("store", "产物树", False)])
    return c
