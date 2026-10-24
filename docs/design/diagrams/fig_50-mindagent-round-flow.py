"""一次 start_agent_round 的处理流程:终态空操作、取代旧轮次、幂等写 pending、失败分流与 R19 重入。"""
from svgkit import Canvas

RED = "#dc2626"


def build() -> Canvas:
    c = Canvas(
        1080, 680, title="一轮 agent_round 的处理",
        desc="调和循环为最新 effective 模型产生 start_agent_round。处理器先判终态,再取代旧轮次,"
             "写 pending 轮次产物作幂等键,然后部署、分析、提案、验证、打包,最后置 executed。"
             "确定性错误与任何桥接失败置 failed 并记 T3;非桥接瞬态上抛,轮次保持 pending,守护进程退出,重启后的 tick 重入。")
    w, h = 200, 84
    xs = [40, 310, 580, 850]
    s1 = c.box(xs[0], 160, w, "产生动作", "最新 effective 模型\n轮次缺失或 pending", kind="control", h=h)
    s2 = c.box(xs[1], 160, w, "读轮次产物", "终态:直接返回\n读不出:写 failed", kind="compute", h=h)
    s3 = c.box(xs[2], 160, w, "取代旧轮次", "旧模型 pending 轮次\n过期动作自身同样", kind="compute", h=h)
    s4 = c.box(xs[3], 160, w, "绑定并写 pending", "读模型与正式库\n轮次产物 = 幂等键", kind="compute", h=h)
    s5 = c.box(xs[3], 340, w, "部署 episode", "已落盘且库版本一致\n则复用不重跑", kind="compute", h=h)
    s6 = c.box(xs[2], 340, w, "分析 · 提案 · 验证", "纯函数为主\n桥接仅在提案生成", kind="compute", h=h)
    s7 = c.box(xs[1], 340, w, "筛选并打包", "提案 pending_review\nid 由轮次与序号确定", kind="compute", h=h)
    s8 = c.box(xs[0], 340, w, "轮次 executed", "写 findings · proposals\ndropped", kind="compute", h=h)

    noop = c.box(xs[1], 40, w, "空操作", "不写 · 不提交", kind="neutral", h=56)
    sup = c.box(xs[2], 40, w, "旧轮次 failed", "category superseded", kind="neutral", h=56)
    pend = c.box(xs[3], 520, w, "上抛 · 保持 pending", "非桥接瞬态\n如会话 T1 重跑用尽", kind="danger", h=h)
    fail = c.box(xs[2], 520, w, "轮次 failed", "T3 失败记录\nfailure-agent-<model>", kind="danger", h=h)

    c.edge(s1, s2)
    c.edge(s2, s3)
    c.edge(s3, s4)
    c.edge(s4, s5, "bottom", "top")
    c.edge(s5, s6, "left", "right")
    c.edge(s6, s7, "left", "right")
    c.edge(s7, s8, "left", "right")
    c.edge(s2, noop, "top", "bottom", label="已终态")
    c.edge(s3, sup, "top", "bottom", label="被取代")
    c.edge(s5, pend, "bottom", "top", label="瞬态", color=RED)
    c.edge(s6, fail, "bottom", "top", label="确定性 / 桥接", color=RED)
    c.edge(s5, fail, "bottom", "top", a_f=0.2, b_f=0.85, via=[(890, 476), (750, 476)], label="不可重跑",
           label_at=0.45, color=RED)
    c.edge(pend, s1, "bottom", "left", via=[(pend.cx, 640), (20, 640), (20, s1.cy)],
           label="下一 tick 重入(R19)", label_at=0.35, dashed=True)

    for n, node in enumerate((s1, s2, s3, s4, s5, s6, s7, s8), 1):
        c.badge(node.x, node.y, n, kind="compute")
    c.note(40, 454, 470, "R18:任何桥接失败都升级为 T3,不留 pending\n避免守护循环无限空转外部推理", kind="neutral")
    c.legend(40, 664, [("control", "调和循环", False), ("compute", "MindAgent 步骤", False),
                       ("danger", "失败路径", False), ("neutral", "虚线 = 下一 tick 重入", True)])
    return c
