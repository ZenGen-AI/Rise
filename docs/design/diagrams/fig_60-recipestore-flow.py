"""提名、入库、复用与战绩回写的一条完整路径。步骤号与正文步骤表一一对应。"""
from svgkit import Canvas

ORANGE = "#d97706"


def build() -> Canvas:
    c = Canvas(
        1080, 600, title="提名到入库到战绩回写",
        desc="来源 run 晋级后提名器写配方候选,门③批准后收录处理器写入 recipes/;"
             "之后某个 run 复用该配方,走完门①后战绩处理器追加战绩条目,最近一条退化就废弃配方。")
    c.lane(14, 44, 1052, 230, "入库:来源 run", kind="control")
    c.lane(14, 296, 1052, 230, "复用:后续 run", kind="control")

    h = 84
    s1 = c.box(40, 110, 220, "提名", "TrainScheduler 提名器\ncompute_gain · 校验\n写候选与 repro", kind="control", h=h)
    s2 = c.gate(300, 110, 200, "门③ 人工审核", "review 看 repro\napprove / reject", h=h)
    s3 = c.box(540, 110, 240, "收录处理器", "写 recipe · lineage\n写空 track_record\n候选置 effective", kind="control", h=h)
    st = c.store(820, 110, 220, "recipes/recipe-<run>/", "配方 effective", h=h)

    s4 = c.box(40, 362, 220, "规划选中配方", "find_recipes 排序\n指令带 recipe_ref", kind="control", h=h)
    s5 = c.box(300, 362, 200, "训练 · 诊断 · 门①", "候选到 effective\n或 rejected", kind="compute", h=h)
    s6 = c.box(540, 362, 240, "战绩处理器", "compute_gain · 退化标记\n追加条目 · verdict", kind="control", h=h)
    s7 = c.box(820, 362, 220, "写战绩标记", "runs/<run>/recipe_track.yaml\n必要时 deprecate", kind="control", h=h)

    c.edge(s1, s2)
    c.edge(s2, s3, label="approve", color=ORANGE)
    c.edge(s3, st)
    c.edge(st, s4, "bottom", "top", a_f=0.5, b_f=0.5, via=[(930, 300), (150, 300)], label="可被检索", dashed=True, label_at=0.5)
    c.edge(s4, s5)
    c.edge(s5, s6)
    c.edge(s6, s7)
    c.edge(s7, st, "top", "bottom", a_f=0.85, b_f=0.85, label="条目 · 废弃", label_at=0.35)

    for n, node in enumerate((s1, s2, s3), 1):
        c.badge(node.x + (16 if node is s2 else 0), node.y, n, kind="control")
    for n, node in zip((4, 5, 6, 7), (s4, s5, s6, s7)):
        c.badge(node.x, node.y, n, kind="control")

    c.legend(40, 560, [("control", "控制层动作", False), ("compute", "计算层与门①", False),
                       ("human", "门③", False), ("store", "产物", False), ("neutral", "虚线 = 只可被检索,不主动触发", True)])
    return c
