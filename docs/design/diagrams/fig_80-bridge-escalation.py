"""失败分类与升级:桥接内部分 T1、T2,调用方按 R18 一律升级为 T3;非桥接瞬态异常与缺凭证各走各的出口。"""
from svgkit import Canvas


def build() -> Canvas:
    c = Canvas(
        1080, 600, title="桥接失败的分类与升级",
        desc="子进程超时、非零退出、空输出是 T1,退避重试耗尽后抛 BridgeError T1;启动阶段的 OSError 是 T2,立即抛。"
             "调用方处理器见到任何 BridgeError 都升级为 T3:产物置 failed、写失败记录、等人处理。"
             "调用方自身的非桥接瞬态异常原样上抛,产物保持 pending;缺凭证在装配时失败。")
    c.lane(14, 44, 520, 266, "桥接内部", kind="infra")
    c.lane(14, 322, 520, 242, "桥接之外", kind="neutral")
    c.lane(546, 44, 520, 520, "调用方与组合根", kind="control")

    t1 = c.box(30, 90, 220, "子进程失败", "超时 · 非零退出 · 空输出", kind="neutral", h=70)
    t2 = c.box(30, 220, 220, "子进程起不来", "启动阶段的任何 OSError", kind="neutral", h=70)
    e1 = c.box(296, 90, 220, "BridgeError T1", "退避重试已耗尽", kind="danger", h=70)
    e2 = c.box(296, 220, 220, "BridgeError T2", "确定性,不重试", kind="danger", h=70)
    own = c.box(30, 350, 220, "调用方自身的瞬态异常", "OSError 等,非桥接", kind="neutral", h=70)
    cred = c.box(30, 470, 220, "缺凭证", "token_env 指向的变量为空", kind="neutral", h=70)

    g = c.gate(580, 130, 220, "规划或轮次处理器", "按 R18 收口", h=120, kind="control")
    t3 = c.box(840, 150, 210, "T3", "产物置 failed\nrecord_failure 留痕\n等人处理", kind="danger", h=80)
    pend = c.box(830, 350, 230, "保持 pending", "产物不变\n下一轮 tick 重来", kind="neutral", h=70)
    asm = c.box(830, 462, 230, "装配失败", "MissingCredentialError\nfactory 抛 FactoryConfigError\n此时才有退出码 2,不写树", kind="danger", h=86)

    c.edge(t1, e1, label="重试耗尽")
    c.edge(t2, e2, label="立即")
    c.edge(e1, g, "right", "left", b_f=0.3)
    c.edge(e2, g, "right", "left", b_f=0.7)
    c.edge(g, t3, label="任何 BridgeError")
    c.edge(own, pend, label="原样上抛")
    c.edge(cred, asm, label="构造时")

    c.legend(40, 585, [("danger", "失败产出", False), ("neutral", "失败来源 / 中性结果", False),
                       ("control", "调用方", False)])
    return c
