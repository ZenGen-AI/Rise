"""落账的六个步骤。编号与正文步骤表一一对应。"""
from svgkit import Canvas


def build() -> Canvas:
    c = Canvas(
        1080, 540, title="一次落账的六个步骤",
        desc="规划处理器在装配上下文之前先跑落账:读终态 run 的决策与结果,算策略签名,判定归因,"
             "写一条 run 级经验记录;再由全部记录重算出长期条目,并判定失效与废弃。"
             "每一步都可重入,记录与条目内容不变时不产生提交。")
    c.lane(14, 44, 1052, 360, "plan_next_round 处理器内的落账", kind="control")

    b1 = c.box(40, 96, 280, "1 读终态 run", "候选 effective 或 rejected\n决策 · 报告 · 账本", kind="control", h=80)
    b2 = c.box(380, 96, 280, "2 算策略签名", "维度 · 动作 · 模式\n扰动键排序", kind="control", h=80)
    b3 = c.box(720, 96, 280, "3 判归因", "new 记 low\nclone 无扰动归配方", kind="control", h=80)
    b4 = c.box(720, 250, 280, "4 写 run 记录", "strategy_experience.yaml\n幂等键", kind="control", h=80)
    b5 = c.box(380, 250, 280, "5 重算条目", "由全部记录纯函数重算\n不是增量累加", kind="control", h=80)
    b6 = c.box(40, 250, 280, "6 失效与废弃", "反例过半 · 基准换版\n人改过的条目不动", kind="control", h=80)

    for n, node in enumerate((b1, b2, b3, b4, b5, b6), 1):
        c.badge(node.x, node.y, n, kind="control")
    c.edge(b1, b2)
    c.edge(b2, b3)
    c.edge(b3, b4, "bottom", "top")
    c.edge(b4, b5, "left", "right")
    c.edge(b5, b6, "left", "right")

    c.note(40, 420, 620, "落账只读产物树,不调外部模型\n"
                         "重入时按同一批记录重算,结果逐字节相同\n"
                         "内容不变时 GitStore 不提交", kind="neutral")
    c.legend(700, 440, [("control", "控制层步骤", False)])
    return c
