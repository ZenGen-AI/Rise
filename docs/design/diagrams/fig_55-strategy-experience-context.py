"""策略经验的上下文图:它挂在 TrainScheduler 上,写读都发生在规划处理器的同一次调用里。"""
from svgkit import Canvas


def build() -> Canvas:
    c = Canvas(
        1080, 560, title="策略经验的上下文",
        desc="TrainScheduler 的规划处理器在装配上下文之前先落账,把一轮的决策与结果压成一条经验记录,"
             "再由全部记录重算出长期条目;随后装配的规划上下文带上选中的条目,交给 TrainPlanner 渲染。"
             "策略经验只读写产物树,不调用外部模型,也不碰外部推理桥接。")
    c.lane(14, 44, 1052, 300, "控制层(本地开发机)", kind="control")

    ts = c.box(44, 116, 250, "TrainScheduler", "plan_handler · assemble_context\n落账在装配之前",
               kind="control", h=84)
    se = c.box(415, 116, 250, "strategy_experience", "落账 · 聚合 · 选取\n纯函数 + 一次幂等写",
               kind="control", h=84)
    tp = c.box(786, 116, 250, "TrainPlanner", "把条目渲染进提示\n模板 PLANNER_V2",
               kind="control", h=84)

    cp = c.store(44, 404, 300, "ControlPlane 产物树", "runs/<run>/strategy_experience.yaml\nexperience/exp-<维度>-<哈希>.yaml",
                 h=96)
    diag = c.store(415, 404, 300, "诊断与账本", "diagnostic_report.yaml\nrun_trace.yaml",
                   h=96)
    c.note(786, 404, 250, "策略经验不调用外部模型\n条目的正负只由诊断报告决定\n外部 agent 不参与写入", kind="neutral")

    c.edge(ts, se, a_f=0.32, b_f=0.32, label="终态 run")
    c.edge(se, cp, "bottom", "top", a_f=0.28, b_f=0.25, label="写记录与条目")
    c.edge(cp, se, "top", "bottom", a_f=0.62, b_f=0.72, label="读全部记录")
    c.edge(diag, se, "top", "bottom", a_f=0.3, b_f=0.62, label="分数 · 成本 · 退化")
    c.edge(ts, tp, "top", "top", via=[(169, 78), (911, 78)], label="含第七段的上下文")

    c.legend(40, 532, [("control", "控制层", False), ("store", "存储", False)])
    return c
