"""配方候选与配方的生命周期。迁移表即 controlplane.states.REVIEW_TRANSITIONS。"""
from svgkit import Canvas

ORANGE = "#d97706"


def build() -> Canvas:
    c = Canvas(
        1080, 470, title="配方的生命周期",
        desc="配方候选由提名器写成 pending_review,经门③批准后由收录处理器写出 effective 的配方,"
             "候选随之置 effective;配方在战绩退化时被战绩处理器废弃,被取代时置 superseded。")
    c.lane(14, 44, 520, 380, "配方候选 recipe_candidates/recipe-<run>.yaml", kind="control")
    c.lane(560, 44, 506, 380, "配方 recipes/recipe-<run>/recipe.yaml", kind="control")

    kw = dict(h=44, title_size=14)
    pr = c.box(60, 100, 190, "pending_review", "提名器写入", kind="human", name="候选 pending_review", **dict(kw, h=56))
    ap = c.box(60, 210, 190, "approved", "等待收录", kind="control", name="候选 approved", **dict(kw, h=56))
    ce = c.box(60, 320, 190, "effective", "已收录", kind="compute", name="候选 effective", **dict(kw, h=56))
    rj = c.box(320, 100, 170, "rejected", "保留记录", kind="danger", name="候选 rejected", **dict(kw, h=56))

    re = c.box(590, 210, 180, "effective", "收录时直接写成", kind="compute", name="配方 effective", **dict(kw, h=56))
    dp = c.box(860, 210, 170, "deprecated", "最近一条战绩退化", kind="neutral", name="配方 deprecated", **dict(kw, h=56))
    sp = c.box(860, 330, 170, "superseded", "被新配方取代", kind="neutral", name="配方 superseded", **dict(kw, h=56))

    c.edge(pr, ap, "bottom", "top", label="门③ approve", color=ORANGE)
    c.edge(pr, rj, label="门③ reject", color=ORANGE)
    c.edge(ap, re, "right", "left", label="收录处理器写配方")
    c.edge(ap, ce, "bottom", "top", label="收录最后一步")
    c.edge(re, dp, label="战绩处理器")
    c.edge(re, sp, "bottom", "left", label="被取代")
    c.note(610, 100, 260, "配方新建即 effective,不经迁移\n战绩退化时自动 deprecated", kind="neutral")

    c.legend(40, 452, [("human", "等待人工", False), ("compute", "生效", False),
                       ("danger", "拒绝", False)])
    return c
