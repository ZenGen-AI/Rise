"""依赖方向与分层守卫:factory 在最右端,守卫测试钉住的边。"""
from svgkit import Canvas

RED = "#dc2626"


def build() -> Canvas:
    c = Canvas(
        1080, 560, title="依赖方向与分层守卫",
        desc="factory 在依赖图最右端,可以导入任何包,任何包都不导入它。"
             "红色虚线是守卫测试禁止的导入边。")

    fac = c.box(840, 220, 210, "factory", "组合根 · 叶子", kind="control", h=64)

    cp = c.box(40, 220, 200, "controlplane", "协调层核心", kind="control", h=64)
    sim = c.box(320, 80, 200, "simulator", "仿真 · 实时服务", kind="compute", h=64)
    mt = c.box(320, 220, 200, "mindtrainer", "训练 · 派发器", kind="compute", h=64)
    dg = c.box(320, 360, 200, "diagnostic", "诊断评测", kind="compute", h=64)
    new = c.box(580, 440, 220, "闭环新包", "recipestore · trainplanner\ntrainscheduler · actio\nmindagent · inference_bridge",
                kind="neutral", h=92)

    c.edge(sim, cp, "left", "top", b_f=0.5, label="导入")
    c.edge(mt, cp, label="导入")
    c.edge(dg, cp, "left", "bottom", b_f=0.5, label="导入")
    c.edge(mt, sim, "top", "bottom", label="导入")
    c.edge(fac, mt, "left", "right", label="导入任何包")
    c.edge(fac, sim, "top", "right", a_f=0.3)
    c.edge(fac, dg, "bottom", "right", a_f=0.2)
    c.edge(fac, new, "bottom", "right", a_f=0.6, b_f=0.4)

    c.edge(mt, dg, "bottom", "top", a_f=0.3, b_f=0.3, dashed=True, color=RED, tail=True, label="互不导入")
    c.edge(cp, mt, "bottom", "bottom", a_f=0.75, b_f=0.12, via=[(190, 320), (344, 320)], dashed=True, color=RED,
           label="禁止")
    c.edge(cp, new, "bottom", "left", a_f=0.3, via=[(100, 486)], dashed=True, color=RED, label="禁止")

    c.note(580, 136, 230, "守卫测试\ntest_layer_rules_hold\n只看绝对导入的顶层包名", kind="danger")
    c.legend(40, 540, [("control", "控制与协调", False), ("compute", "计算层", False),
                       ("danger", "红色虚线 = 禁止的导入", True)])
    return c
