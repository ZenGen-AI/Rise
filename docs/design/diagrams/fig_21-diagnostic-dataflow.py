"""一次诊断的双轨数据流。步骤编号与正文第 6 章的步骤表一一对应。"""
from svgkit import Canvas


def build() -> Canvas:
    c = Canvas(
        1080, 640, title="一次诊断的双轨数据流",
        desc="静态轨从题库按档选题,经 SoMEval 运行器取 k 个回答,规则式题目重打分;"
             "someval 模式的题由运行器直接判分,不经重打分。动态轨由调用方给 spec,"
             "跑多轮 episode 后由 judge 给候选话语打分。两轨的 ItemResult 分开存放,"
             "分别送入错误归类与逐轨聚合,L2 融合后聚到 L1,再与基线比较,最后写报告。")
    c.lane(14, 56, 726, 160, "静态轨", kind="compute", dashed=False)
    c.lane(14, 286, 726, 150, "动态轨", kind="compute", dashed=False)

    sel = c.box(30, 100, 200, "选题", "select_items\nfull 全部 · quick 每 L3 一题", kind="compute", h=74)
    spec = c.box(30, 330, 200, "动态 spec", "调用方提供\n题目 · 情境 · 绑定 · 后端", kind="neutral", h=74)
    sm = c.box(270, 100, 200, "SoMEval 执行", "run_static_items\n每题 k 个回答\nsomeval 题:运行器判分", kind="compute", h=88)
    ep = c.box(270, 330, 200, "多轮 episode", "run_dynamic_item\n取候选角色位的话语", kind="compute", h=74)
    rule = c.box(510, 100, 200, "规则重打分", "RuleScorer\n只对 rule 模式题", kind="compute", h=74)
    jd = c.box(510, 330, 200, "judge 打分", "LLMJudgeScorer\n先过自评偏差守卫", kind="compute", h=74)

    ir = c.store(780, 170, 200, "ItemResult", "按轨分开存放", h=110)
    ec = c.box(780, 330, 200, "错误归类", "静态:变体族\n动态:一致性", kind="compute", h=74)

    ag = c.box(780, 520, 200, "逐轨聚合", "L3 · L2 各轨分开", kind="compute", h=64)
    fu = c.box(530, 520, 200, "融合到 L1", "两轨都有的 L2 融合", kind="compute", h=64)
    rg = c.box(280, 520, 200, "退化判定", "对照基线 · 区间不相交", kind="compute", h=64)
    wr = c.box(30, 520, 200, "组装并写入", "报告 · 错误画像\nwrite_report", kind="compute", h=64)

    c.edge(sel, sm, label="静态题")
    c.edge(sm, rule, label="原始输出")
    c.edge(spec, ep)
    c.edge(ep, jd, label="话语")
    c.edge(rule, ir, b_f=0.3)
    c.edge(sm, ir, "top", "top", via=[(370, 78), (880, 78)], label="someval 题:已判分", label_at=0.5)
    c.edge(jd, ir, "right", "left", b_f=0.75, via=[(745, 367), (745, 252)])
    c.edge(ir, ec, "bottom", "top")
    c.edge(ir, ag, "right", "right", via=[(1030, 225), (1030, 552)], label="分数")
    c.edge(ag, fu, "left", "right")
    c.edge(fu, rg, "left", "right")
    c.edge(rg, wr, "left", "right", label="标记")
    c.edge(ec, wr, "left", "top", via=[(755, 367), (755, 490), (130, 490)], label="ErrorProfile", label_at=0.55)

    for n, node in ((1, sel), (2, sm), (3, ep), (4, ec), (5, ag), (6, fu), (7, rg), (8, wr)):
        c.badge(node.x, node.y, n, kind="compute")
    c.legend(40, 620, [("compute", "计算步骤", False), ("store", "中间结果", False),
                       ("neutral", "调用方输入", False)])
    return c
