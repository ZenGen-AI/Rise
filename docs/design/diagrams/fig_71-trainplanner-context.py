"""TrainPlanner 的上下文图:谁给它输入,它把输出交给谁,它经谁调用外部强模型。"""
from svgkit import Canvas


def build() -> Canvas:
    c = Canvas(
        1080, 560, title="TrainPlanner 的上下文",
        desc="TrainScheduler 装配规划上下文并带着领域校验器调用 TrainPlanner,拿回决策或明确的失败。"
             "TrainPlanner 只经外部推理桥接调用外部强模型;产物树里它只碰提示模板。"
             "组合根负责选后端和安装默认模板。")
    c.lane(14, 44, 1052, 300, "控制层(本地开发机)", kind="control")

    ts = c.box(44, 96, 250, "TrainScheduler", "装配 PlanningContext\n领域校验 · 写 planner_decision",
               kind="control", h=84)
    tp = c.box(415, 96, 250, "TrainPlanner", "打包 · 决策后端 · 严格解析\n无状态",
               kind="control", h=84)
    br = c.box(786, 96, 250, "外部推理桥接", "complete(prompt) → 文本\n退避重试 · 成本记账",
               kind="infra", h=84)
    fc = c.box(415, 246, 250, "factory 组合根", "选决策后端\n安装默认模板 PLANNER_V1",
               kind="neutral", h=72)
    ext = c.box(786, 246, 250, "外部强模型", "命令行进程\n只返回文本", kind="neutral", h=72)

    cp = c.store(44, 400, 300, "ControlPlane 产物树", "config/prompts/planner-<版本>.yaml\nruns/<run>/planner_decision.yaml",
                 h=96)
    c.note(415, 404, 621, "TrainPlanner 不读树里的 run、配方、诊断\n"
                              "也不写 planner_decision;它只产出结构化决策,不改任何代码", kind="neutral")

    c.edge(ts, tp, a_f=0.35, b_f=0.35, label="上下文 + 校验器")
    c.edge(tp, ts, "left", "right", a_f=0.72, b_f=0.72, label="决策或失败")
    c.edge(tp, br, label="提示文本")
    c.edge(br, ext, "bottom", "top", label="子进程")
    c.edge(fc, tp, "top", "bottom", label="注入后端")
    c.edge(ts, cp, "bottom", "top", a_f=0.3, b_f=0.2167, label="读上下文 · 写决策")
    c.edge(fc, cp, "left", "top", a_f=0.5, b_f=0.75, label="安装模板")
    c.legend(40, 535, [("control", "控制层", False), ("infra", "共享基础设施", False),
                       ("store", "存储", False)])
    return c
