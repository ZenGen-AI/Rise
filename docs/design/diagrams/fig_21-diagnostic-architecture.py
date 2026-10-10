"""诊断评测的组件图:入口、八步流水线模块、横切模块。"""
from svgkit import Canvas


def build() -> Canvas:
    c = Canvas(
        1080, 680, title="诊断评测的组件",
        desc="评估器从产物树取资产与基线后调用编排器。编排器是薄协调层,依次调用流水线模块,"
             "模块之间不互相调用,箭头是数据流向。统计模块被多处调用,隔离校验器独立暴露,供情境生成路径调用。")
    c.lane(14, 44, 1052, 110, "入口", kind="control")
    c.lane(14, 168, 1052, 330, "流水线模块:编排器依次调用,箭头为数据流向", kind="compute", dashed=False)
    c.lane(14, 512, 1052, 128, "横切模块", kind="neutral")

    ev = c.box(40, 80, 300, "evaluator · assets", "make_diagnostic_evaluator\n资产读写 · 基线解析 · backends", kind="control", h=66)
    orch = c.box(520, 80, 400, "orchestrator", "run_diagnostic · 只编排,不含统计与决策", kind="compute", h=66)

    w = 200
    tl = c.box(40, 236, w, "taxonomy_loader", "按档选题 · 变体族", kind="compute", h=64)
    sa = c.box(310, 200, w, "someval_adapter", "静态轨执行 · 原始输出", kind="compute", h=64)
    dt = c.box(310, 296, w, "dynamic_track", "动态轨 episode\n题目来自调用方 spec", kind="compute", h=70)
    sc = c.box(580, 236, w, "scorer", "规则 · judge · 偏差守卫", kind="compute", h=64)
    ec = c.box(850, 236, w, "error_classifier", "变体族通过模式\n动态一致性", kind="compute", h=64)
    rp = c.box(40, 400, w, "store_adapter", "report 组装 · 写产物树", kind="compute", h=64)
    rg = c.box(310, 400, w, "regression", "退化判定 · 版本可比", kind="compute", h=64)
    ag = c.box(580, 400, w, "aggregator", "L3 → L2 → L1 · 融合", kind="compute", h=64)

    st = c.box(310, 556, w, "stats", "Wilson 区间 · 通过率\n方差 · 区间相交", kind="neutral", h=64)
    iso = c.box(850, 556, w, "isolation", "污染过滤 · 独立暴露\n供情境生成路径调用", kind="neutral", h=64)

    c.edge(ev, orch, label="配置 · 基线 · backends")
    c.edge(orch, tl, "bottom", "top", a_f=0.05, via=[(540, 162), (140, 162)], label="按序调用")
    c.edge(tl, sa, label="静态题", b_f=0.6)
    c.edge(sa, sc, b_f=0.3)
    c.edge(dt, sc, b_f=0.75)
    c.edge(sc, ec, label="ItemResult")
    c.edge(sc, ag, "bottom", "top", label="ItemResult")
    c.edge(ag, rg, "left", "right", label="分数")
    c.edge(rg, rp, "left", "right", label="标记")
    c.edge(ec, rp, "bottom", "bottom", via=[(950, 484), (140, 484)], label="ErrorProfile", label_at=0.3)

    c.note(580, 556, 230, "stats 被 scorer、someval_adapter\naggregator、regression 调用", kind="neutral")
    c.legend(40, 662, [("control", "入口", False), ("compute", "流水线", False), ("neutral", "横切", False)])
    return c
