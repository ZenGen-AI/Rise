"""策略条目的生命周期。条目不是产物状态机的成员,状态写在载荷里。"""
from svgkit import Canvas

ORANGE = "#d97706"


def build() -> Canvas:
    c = Canvas(
        1080, 430, title="策略条目的生命周期",
        desc="条目首次写出时证据不足,支持数到二且平均增益为正才转为可用;"
             "反例过半即废弃,基准版本变更则整体失效。状态写在条目载荷里,由重算决定;"
             "人直接改写过的条目不再被机器改写。")
    c.lane(14, 44, 1052, 300, "experience/exp-<维度>-<哈希>.yaml", kind="control")

    kw = dict(h=60, title_size=14)
    ins = c.box(60, 110, 200, "insufficient", "支持数不足二", kind="human", name="条目 insufficient", **kw)
    eff = c.box(340, 110, 200, "effective", "可注入", kind="compute", name="条目 effective", **kw)
    dep = c.box(340, 260, 200, "deprecated", "反例过半", kind="danger", name="条目 deprecated", **kw)
    stl = c.box(680, 110, 200, "stale", "基准版本已换", kind="neutral", name="条目 stale", **kw)
    hum = c.box(680, 260, 200, "人改的状态", "机器不再改写", kind="neutral", name="人工保留", **kw)

    c.edge(ins, eff, label="支持数 ≥ 2", color=ORANGE)
    c.edge(ins, dep, "bottom", "left", label="反例过半", color=ORANGE)
    c.edge(eff, dep, "bottom", "top", label="反例过半", color=ORANGE)
    c.edge(eff, stl, label="换基准版本")
    c.edge(ins, stl, "top", "top", via=[(160, 78), (780, 78)], label="换基准版本")
    c.edge(eff, hum, "right", "left", a_f=0.8, b_f=0.5, label="人直接改载荷", dashed=True)
    c.note(60, 300, 200, "状态写在载荷里\n不走产物状态机", kind="neutral")

    c.legend(40, 404, [("human", "证据不足", False), ("compute", "可注入", False),
                       ("danger", "废弃", False), ("neutral", "人工直接改文件", True)])
    return c
