"""一次 InteractTrain 会话的消息流程:客户端占 a 位先说,对手 b 位由服务端后端驱动,max_turns=4。"""
from svgkit import Canvas

ORANGE = "#d97706"


def build() -> Canvas:
    c = Canvas(
        1080, 760, title="一次 InteractTrain 会话的消息流程",
        desc="MindTrainer 的客户端建会话,服务端校验后启动驱动器。每一轮客户端提交话语,驱动器经运行器记一轮并立即推送,"
             "对手轮由服务端后端产生且抹掉内心推理。跑满 4 轮后服务端落盘 Episode 并推 EndSession,客户端在本地重建同一条 Episode。")
    c.lane(14, 44, 242, 680, "调用方进程:MindTrainer", kind="compute", dashed=False)
    c.lane(300, 44, 766, 680, "实时环境服务进程", kind="compute")
    c.text(430, 84, "服务端主循环", size=12, anchor="middle")
    c.text(670, 84, "驱动器与运行器", size=12, anchor="middle")
    c.text(940, 84, "对手后端", size=12, anchor="middle")

    kw = dict(kind="compute", h=76)
    s1 = c.box(30, 100, 210, "建会话", "create_session\n客户端位 a · max_turns 4", kind="neutral", h=76)
    s2 = c.box(330, 100, 200, "校验并启动", "validate_create_session\n建后端 · start()", **kw)
    s3 = c.box(550, 210, 240, "第 0 轮等客户端", "ClientDrivenBackend\n阻塞,最多 30 秒", **kw)
    s4 = c.box(30, 210, 210, "策略出第 0 句", "policy.respond(turns)\nsubmit_turn", kind="neutral", h=76)
    s5 = c.box(550, 330, 240, "第 0 轮入记录", "on_turn 入 outbox\n客户端轮原样回显", **kw)
    s6 = c.box(830, 330, 220, "对手第 1 轮", "TimeoutBackend 60 秒\n看不到 a 的内心推理", **kw)
    s7 = c.box(330, 450, 200, "推送 TurnUpdate", "回显 + 对手轮\n对手轮抹掉推理", **kw)
    s8 = c.box(30, 450, 210, "收回显与对手轮", "吞掉回显\n返回对手轮列表", kind="neutral", h=76)
    s9 = c.box(550, 560, 240, "第 3 轮后返回", "terminated_reason max_turns\non_session_closed 落盘", **kw)
    s10 = c.box(330, 630, 200, "推 EndSession", "max_turns\nepisode_id ep-sess-…", **kw)
    s11 = c.box(30, 624, 210, "本地重建 Episode", "drain_until_end()\n只收 max_turns\n或 goal_reached", kind="neutral", h=90)

    c.edge(s1, s2, label="CreateSession", color=ORANGE)
    c.edge(s2, s3, "bottom", "top", b_f=0.2, label="后台线程")
    c.edge(s2, s4, "bottom", "top", a_f=0.2, b_f=0.75, label="SessionReady", color=ORANGE)
    c.edge(s4, s3, a_f=0.5, b_f=0.5, label="SubmitTurn", color=ORANGE)
    c.edge(s3, s5, "bottom", "top", label="收到话语")
    c.edge(s5, s6, label="第 1 轮")
    c.edge(s6, s7, "bottom", "right", label="对手话语入 outbox")
    c.edge(s7, s8, label="TurnUpdate ×2", color=ORANGE)
    c.edge(s8, s9, "bottom", "left", a_f=0.5, b_f=0.3, label="第 2、3 轮重复 4 至 8")
    c.edge(s9, s10, "bottom", "right", a_f=0.3, label="closed 事件")
    c.edge(s10, s11, label="EndSession", color=ORANGE)

    for n, node in enumerate((s1, s2, s3, s4, s5, s6, s7, s8, s9, s10, s11), 1):
        c.badge(node.x, node.y, n, kind="compute")
    c.legend(40, 744, [("neutral", "调用方动作", False), ("compute", "服务端动作", False)])
    return c
