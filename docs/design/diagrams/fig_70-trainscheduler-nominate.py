"""提名处理器的判定流程:幂等键、已有候选的保护、还没到与终局两类不提名。"""
from svgkit import Canvas


def build() -> Canvas:
    c = Canvas(
        1080, 740, title="提名处理器的判定",
        desc="提名标记 nomination.yaml 是幂等键。候选已在时只补标记、不改候选。"
             "nomination_check 按固定顺序判定:还没到的情形不写标记,终局不提名写 skipped,通过才构造配方候选并写 nominated。")

    g1 = c.gate(360, 50, 320, "提名标记在吗", "nomination.yaml", h=66)
    noop = c.box(40, 55, 230, "空操作", "标记就是幂等键", h=56, kind="neutral")
    g2 = c.gate(360, 160, 320, "配方候选已在吗", "recipe_candidates/recipe-<run>", h=66)
    patch = c.box(40, 165, 230, "只补 nominated 标记", "候选一个字节都不改", h=56, kind="control")
    chk = c.box(360, 280, 320, "nomination_check", "按固定顺序判定,第一条命中即停", h=60,
                kind="control")
    wait = c.box(40, 282, 230, "不写标记,返回", "还没到,下次触发再判", h=56, kind="neutral")
    skip = c.box(40, 400, 230, "写 skipped 标记", "终局,原因写进 reason", h=56, kind="danger")
    build_ = c.box(360, 400, 320, "build_recipe_candidate", "按配方载荷拼候选并附 repro", h=60,
                   kind="control")
    cand = c.box(360, 510, 320, "写配方候选", "状态 pending_review,等审核门③", h=60, kind="control")
    note = c.box(360, 620, 320, "写 nominated 标记", "result · reason · gain · 基线 run", h=60,
                 kind="control")

    c.edge(g1, noop, "left", "right", label="在")
    c.edge(g1, g2, "bottom", "top", label="不在")
    c.edge(g2, patch, "left", "right", label="在")
    c.edge(g2, chk, "bottom", "top", label="不在")
    c.edge(chk, wait, "left", "right", a_f=0.35, b_f=(0.35 * 60 + 280 - 282) / 56, label="还没到")
    c.edge(chk, skip, "left", "right", a_f=0.8, via=[(315, 328), (315, 428)], label="终局不提名", label_at=0.7)
    c.edge(chk, build_, "bottom", "top", label="提名")
    c.edge(build_, cand, "bottom", "top")
    c.edge(cand, note, "bottom", "top")

    c.note(720, 280, 340,
           "判定顺序\n"
           "1 无候选 checkpoint:还没到\n"
           "2 候选 rejected:终局\n"
           "3 候选 pending_review 或 approved:还没到\n"
           "4 无指令:还没到\n"
           "5 指令 mode 不是 batch 或 interact:终局\n"
           "6 指令无 target_dimension:终局\n"
           "7 无诊断报告:还没到\n"
           "8 证据等级不可接受:终局\n"
           "9 compute_gain 为 None:终局\n"
           "10 增益低于 min_gain:终局\n"
           "11 报告带退化标记:终局", kind="neutral")
    c.legend(40, 722, [("control", "处理器动作", False), ("danger", "终局不提名", False),
                       ("neutral", "空操作与等待", False)])
    return c
