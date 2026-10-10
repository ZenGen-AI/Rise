"""退化判定:detect_regressions 的判定链,以及两个并列的检测函数。"""
from svgkit import Canvas


def build() -> Canvas:
    c = Canvas(
        1080, 680, title="退化判定",
        desc="每个候选维度分按维度、层级、轨道三元组找基线。找到、候选更低且两个置信区间不相交,"
             "才打一条社会能力退化标记。两份基准版本都给出且不可比时直接报错。"
             "G 组探针与 reward hacking 的检测函数与退化判定并列。")
    c.lane(14, 44, 1052, 410, "detect_regressions:编排器每次都调用", kind="compute", dashed=False)
    c.lane(14, 478, 1052, 160, "并列的两个检测", kind="neutral")

    inp = c.box(30, 104, 170, "维度分", "候选与基线\nDimensionScore", kind="compute", h=78)
    g1 = c.gate(220, 100, 196, "版本可比?", "两份 BenchmarkVersion\n编排器从不传", h=84, kind="neutral")
    g2 = c.gate(440, 100, 180, "基线有同键?", "维度 · 层级 · 轨道\n三元组完全相同", h=84, kind="neutral")
    g3 = c.gate(650, 100, 180, "候选更低?", "候选分 < 基线分", h=84, kind="neutral")
    g4 = c.gate(860, 100, 190, "区间不相交?", "候选上界 < 基线下界", h=84, kind="neutral")

    xv = c.box(218, 300, 200, "CrossVersionError", "不可比,整次判定失败", kind="danger", h=64)
    no = c.box(470, 300, 300, "不打标记", "没有基线 · 提升或持平 · 区间重叠", kind="neutral", h=64)
    fl = c.box(860, 360, 190, "social_regression", "drop · level · track\nbaseline_version", kind="danger", h=70)

    c.edge(inp, g1)
    c.edge(g1, g2, label="可比")
    c.edge(g2, g3, label="有")
    c.edge(g3, g4, label="是")
    c.edge(g1, xv, "bottom", "top", label="不可比")
    c.edge(g2, no, "bottom", "top", b_f=0.1, label="无")
    c.edge(g3, no, "bottom", "top", b_f=0.75, label="否")
    c.edge(g4, no, "bottom", "right", a_f=0.2, label="重叠")
    c.edge(g4, fl, "bottom", "top", a_f=0.65, b_f=0.65, label="不相交", label_at=0.7)

    c.box(40, 530, 470, "detect_g_probe_failure", "G 组探针分数 < 0.7 → g_probe_failure\n输入是 G 组探针题的分数",
          kind="neutral", h=74)
    c.box(560, 530, 480, "detect_reward_hacking", "奖励涨幅 − 诊断涨幅 > 0.15 → reward_hacking_suspected\n输入是训练奖励与诊断分的涨幅",
          kind="neutral", h=74)

    c.legend(40, 662, [("compute", "输入", False), ("neutral", "判断点与无标记", False),
                       ("danger", "退化标记与报错", False)])
    return c
