"""注入的四个步骤:从条目里选最多三条,渲染进规划上下文的第七段。"""
from svgkit import Canvas


def build() -> Canvas:
    c = Canvas(
        1080, 500, title="条目如何进入规划上下文",
        desc="装配上下文时按目标维度与当前基准版本筛出可用条目,按支持数与平均增益排序,"
             "截断到最多三条并受字符预算约束,再渲染成规划上下文的第七段。"
             "没有可用条目时该段渲染为空列表,提示逐字节稳定。")
    c.lane(14, 44, 1052, 330, "assemble_context 的第七段", kind="control")

    b1 = c.box(40, 96, 250, "7 取目标维度", "诊断里一级分最低的维度\n同分按维度名升序", kind="control", h=84)
    b2 = c.box(340, 96, 250, "8 筛可用条目", "effective 且基准版本相同\n反例过半的不注入", kind="control", h=84)
    b3 = c.box(640, 96, 250, "9 排序并截断", "支持数降序 · 平均增益降序\n最多三条", kind="control", h=84)
    b4 = c.box(640, 250, 250, "10 渲染第七段", "每条一行\n字符预算 900", kind="control", h=84)
    b5 = c.box(340, 250, 250, "11 交给规划器", "PlanningContext 第七段\n模板 PLANNER_V2", kind="control", h=84)
    c.note(40, 250, 250, "没有可用条目时\n该段为空列表\n渲染结果稳定", kind="neutral")

    for n, node in enumerate((b1, b2, b3, b4, b5), 7):
        c.badge(node.x, node.y, n, kind="control")
    c.edge(b1, b2)
    c.edge(b2, b3)
    c.edge(b3, b4, "bottom", "top")
    c.edge(b4, b5, "left", "right")

    c.legend(40, 460, [("control", "控制层步骤", False)])
    return c
