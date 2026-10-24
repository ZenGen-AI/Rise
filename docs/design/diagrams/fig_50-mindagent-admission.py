"""从验证结果到正式库:筛选、打包、门②、收录处理器的三步写入与失败出口。"""
from svgkit import Canvas

ORANGE = "#d97706"
RED = "#dc2626"


def build() -> Canvas:
    c = Canvas(
        1080, 600, title="提案到入库的审核链",
        desc="验证结果先按阈值筛选,未过的写进轮次的 dropped。通过的打包成 pending_review 提案,"
             "人工在门②批准或拒绝。批准后收录处理器先校验全部增量,再写库条目、递增 library_state,"
             "最后把提案置 effective。增量不合法时记一次 T2 失败记录(提案缺 run_id、run 目录不在或已记过时不写),提案留在 approved。")
    w, h = 200, 76
    xs = [30, 290, 550, 810]
    ver = c.box(xs[0], 150, w, "验证结果", "fired_on_motivating\nfired_elsewhere", kind="compute", h=h)
    scr = c.box(xs[1], 150, w, "阈值筛选", "≥ 0.8 且 ≤ 0.2 才通过", kind="compute", h=h)
    pkg = c.box(xs[2], 150, w, "提案 pending_review", "skill_memory_proposal\n带 verification", kind="compute", h=h)
    g2 = c.gate(xs[3], 150, 190, "门② 人工审核", "review 看证据\napprove / reject", h=h)
    drop = c.box(xs[1], 40, w, "dropped 记录", "写进轮次产物", kind="neutral", h=56)

    appr = c.box(xs[3], 320, 190, "提案 approved", "调和循环产生\nadmit_proposal", kind="control", h=h)
    chk = c.box(xs[2], 320, w, "校验全部增量", "Skill / MemoryEntry\n不合法则一条不写", kind="compute", h=h)
    wr = c.box(xs[1], 320, w, "写库条目", "skill_library/\nmemory_library/", kind="store", h=h)
    st = c.box(xs[0], 320, w, "library_state", "对应版本 +1\nadmitted 追加一条", kind="store", h=h)

    eff = c.box(xs[0], 480, w, "提案 effective", "收录完成", kind="compute", h=64)
    t2 = c.box(xs[2], 480, w, "T2 失败记录", "提案留在 approved\nfailure-admit-<提案>", kind="danger", h=64)
    rej = c.box(xs[3], 480, 190, "提案 rejected", "保留记录", kind="danger", h=64)

    c.edge(ver, scr)
    c.edge(scr, pkg, label="通过")
    c.edge(scr, drop, "top", "bottom", label="未通过")
    c.edge(pkg, g2)
    c.edge(g2, appr, "bottom", "top", a_f=0.35, b_f=0.35, label="approve", color=ORANGE)
    c.edge(g2, rej, "right", "right", via=[(1035, g2.cy), (1035, rej.cy)], label="reject", label_at=0.5,
           color=ORANGE)
    c.edge(appr, chk, "left", "right")
    c.edge(chk, wr, "left", "right")
    c.edge(wr, st, "left", "right")
    c.edge(st, eff, "bottom", "top")
    c.edge(chk, t2, "bottom", "top", label="不合法", color=RED)

    for n, node in enumerate((chk, wr, st, eff), 1):
        c.badge(node.x, node.y, n, kind="compute")
    c.note(xs[1], 470, 230, "每步可重入:条目已在就跳过\nadmitted 已记就不再递增", kind="neutral")
    c.legend(40, 584, [("compute", "MindAgent 步骤", False), ("store", "正式库", False),
                       ("human", "人工门", False), ("danger", "失败 / 拒绝", False)])
    return c
