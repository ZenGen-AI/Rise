"""激活轨迹经实时协议落盘的数据流。步骤编号与正文第 6 章的轨迹路径表一一对应。"""
from svgkit import Canvas


def build() -> Canvas:
    c = Canvas(
        1080, 600, title="激活轨迹如何经协议落盘",
        desc="客户端把 respond 产出的轨迹放进 SubmitTurn 的 Struct 字段。服务端解出字典,"
             "经驱动器与客户端驱动后端交给 runner,runner 写进 TurnRecord,会话结束时整份 episode 落盘。"
             "逐轮回显时只抹掉对手轮次的轨迹与内心推理,客户端自己的轮次原样回显。")
    c.lane(14, 40, 1052, 150, "客户端进程(MindAgent 部署驱动器)", kind="compute")
    c.lane(14, 222, 1052, 340, "实时环境服务进程(SocialSimulator)", kind="compute", dashed=False)

    xs = [30, 290, 550, 810]
    rs = c.box(xs[0], 86, 240, "ActioInstance.respond", "返回三元组\n轨迹叶子没有数字", h=80,
               kind="infra")
    sb = c.box(xs[1], 86, 240, "client.submit_turn", "activation_trace 关键字参数\nSetInParent 后 update", h=80,
               kind="compute")
    rc = c.box(xs[3], 86, 240, "客户端本地历史", "自己的轮次带轨迹\n下一轮作 history", h=80, kind="compute")

    sv = c.box(xs[0], 300, 240, "EnvironmentServicer", "HasField 才 MessageToDict\n得 dict,否则传 None", h=80,
               kind="compute")
    dr = c.box(xs[1], 300, 240, "SessionDriver", "submit_turn 原样转交轨迹", h=80,
               kind="compute")
    cb = c.box(xs[2], 300, 240, "ClientDrivenBackend", "带轨迹时返回三元组\n不带时仍是二元组", h=80,
               kind="compute")
    rn = c.box(xs[2], 450, 240, "run_episode", "_unpack_reply 收二元组或三元组\n深拷贝写进 TurnRecord", h=80,
               kind="compute")
    st = c.store(xs[1], 440, 240, "episode 落盘", "会话结束时写入完整记录\n对手轮次的轨迹也在", h=96)
    dn = c.box(xs[3], 450, 240, "_drain_ready_events", "逐轮事件:对手轮次抹掉两项\n自己的轮次原样回显", h=80,
               kind="compute")

    c.edge(rs, sb)
    c.edge(sb, sv, "bottom", "top", via=[(sb.cx, 206), (sv.cx, 206)], label="SubmitTurn.activation_trace")
    c.edge(sv, dr)
    c.edge(dr, cb)
    c.edge(cb, rn, "bottom", "top", label="三元组")
    c.edge(rn, st, "left", "right")
    c.edge(rn, dn, "right", "left")
    c.edge(dn, rc, "top", "bottom", label="TurnUpdate")

    for n, node in enumerate((rs, sb, sv, dr, cb, rn, st, dn, rc), 1):
        c.badge(node.x, node.y, n, kind="compute")

    c.legend(40, 586, [("compute", "SocialSimulator 与客户端代码", False), ("infra", "Actio", False),
                       ("store", "产物", False)])
    return c
