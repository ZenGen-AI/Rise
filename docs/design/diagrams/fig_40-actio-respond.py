"""一次 respond 的内部流程。步骤编号与正文第 6 章的步骤表一一对应。"""
from svgkit import Canvas


def build() -> Canvas:
    c = Canvas(
        1080, 580, title="一次 respond 的内部流程",
        desc="前三步是 decide:求信号并路由技能、激活推理经验、从记忆端口 recall。"
             "第四步按固定顺序渲染指导语,第五步把 history 副本与指导语交给后端,"
             "第六步把对方新发言逐条写进记忆再只做嵌入,第七步组装并校验激活轨迹,第八步返回三元组。"
             "后端返回值不合规时抛 TypeError,记忆调用失败时抛 MemoryFailureError。")
    c.lane(14, 40, 1052, 170, "decide:不调用模型后端,不写记忆", kind="infra")
    c.lane(14, 240, 1052, 300, "调用后端、写记忆并组装激活轨迹", kind="infra", dashed=False)

    xs = [30, 290, 550, 810]
    inp = c.box(xs[0], 96, 220, "调用方", "respond(persona, scenario,\nhistory, speaker_slot)", h=84, kind="neutral")
    rt = c.box(xs[1], 96, 220, "信号与技能路由", "registry.fired 求信号\nroute 排序后选第一个", h=84,
               kind="infra")
    ex = c.box(xs[2], 96, 220, "推理经验", "activate:applies_when\n主 → 次 → 保底", h=84, kind="infra")
    me = c.box(xs[3], 96, 220, "记忆检索", "查询词 = 对方最后一句\nrecall 取前 memory_limit 条", h=84,
               kind="infra")

    rg = c.box(xs[3], 290, 220, "渲染指导语", "技能 → 经验 → 记忆\n每行带来源前缀", h=84, kind="infra")
    bk = c.box(xs[2], 290, 220, "后端 respond_guided", "传 history 深拷贝\n必须返回二元组", h=84, kind="neutral")
    mw = c.box(xs[1], 290, 220, "写记忆", "逐条 remember 对方新发言\n再 embed_pending 一次\n只读重放时跳过", h=84,
               kind="infra")
    tr = c.box(xs[0], 290, 220, "组装激活轨迹", "build_trace 组装\n随即 validate_trace", h=84, kind="infra")
    out = c.box(xs[0], 440, 220, "返回三元组", "(utterance,\ninner_thought, trace)", h=64, kind="neutral")
    err = c.box(xs[2], 440, 220, "TypeError", "后端返回值不是二元组", h=64, kind="danger")
    merr = c.box(xs[1], 440, 220, "MemoryFailureError", "记忆调用抛异常\n带 transient 标记上抛", h=64,
                 kind="danger")

    c.edge(inp, rt)
    c.edge(rt, ex)
    c.edge(ex, me)
    c.edge(me, rg, "bottom", "top")
    c.edge(rg, bk, "left", "right")
    c.edge(bk, mw, "left", "right")
    c.edge(mw, tr, "left", "right")
    c.edge(tr, out, "bottom", "top")
    c.edge(bk, err, "bottom", "top", label="不合规", color="#dc2626")
    c.edge(mw, merr, "bottom", "top", label="失败", color="#dc2626")

    for n, node in enumerate((rt, ex, me, rg, bk, mw, tr, out), 1):
        c.badge(node.x, node.y, n, kind="infra")

    c.legend(40, 562, [("infra", "Actio 内部步骤", False), ("neutral", "调用方与后端", False),
                       ("danger", "失败", False)])
    return c
