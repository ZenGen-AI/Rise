"""一次 CliBridge 调用:构造、校验、起子进程、判定、退避重试、写日志、记账、返回或抛错。"""
from svgkit import Canvas


def build() -> Canvas:
    c = Canvas(
        1080, 640, title="一次 CliBridge 调用",
        desc="构造时解析凭证,缺凭证直接失败。每次调用先校验路径段,再起子进程并判定结果:"
             "T1 且未耗尽则退避后重试,其余情形都先写一份脱敏日志;成功的调用记一条成本后返回结果,失败的抛 BridgeError。")
    c.lane(14, 44, 1052, 160, "构造与一次尝试", kind="infra")
    c.lane(14, 222, 1052, 380, "收尾", kind="neutral")

    a = c.box(30, 96, 210, "构造 CliBridge", "校验 retry_policy\n解析凭证", kind="infra", h=80)
    b = c.box(280, 96, 200, "complete()", "校验 run_id · task_id", kind="infra", h=80)
    r = c.box(520, 96, 230, "起子进程", "白名单环境 · 空临时目录\n标准输入 · 超时 timeout_s", kind="infra", h=80)
    g = c.gate(800, 86, 250, "判定本次尝试", "OSError → T2\n超时 · 非零 · 空输出 → T1\n其余 → 成功", h=100, kind="neutral")

    bad = c.box(30, 270, 210, "构造失败", "MissingCredentialError\n不是 BridgeError", kind="danger", h=70)
    bk = c.box(520, 260, 230, "退避", "base × 2^(n-1) 秒\n最后一次失败后不睡", kind="infra", h=80)
    lg = c.box(800, 380, 250, "写日志", "每次 complete 一份 · 含各次尝试\n整体过 redact · 返回 BigRef", kind="infra", h=80)
    rc = c.box(520, 380, 230, "记账", "external_inference\ncalls = 本次尝试数", kind="infra", h=80)
    ok = c.box(280, 380, 200, "返回 BridgeResult", "text 去首尾空白\ntoken 恒为 None", kind="compute", h=80)
    er = c.box(800, 510, 250, "抛 BridgeError", "T1:attempts = max_attempts\nT2:立即,不重试", kind="danger", h=70)

    c.edge(a, b)
    c.edge(b, r)
    c.edge(r, g)
    c.edge(a, bad, "bottom", "top", label="缺凭证")
    c.edge(g, bk, "bottom", "right", a_f=0.2, label="T1 未耗尽")
    c.edge(bk, r, "top", "bottom", label="重试")
    c.edge(g, lg, "bottom", "top", a_f=0.75, b_f=0.75, label="成功 / T1 耗尽 / T2", label_at=0.45)
    c.edge(lg, rc, "left", "right", label="成功")
    c.edge(rc, ok, "left", "right")
    c.edge(lg, er, "bottom", "top", label="失败")

    for n, node in enumerate((a, b, r), 1):
        c.badge(node.x, node.y, n, kind="infra")
    c.badge(g.x + 16, g.y, 4, kind="infra")
    for n, node in zip((5, 6, 7, 8, 9), (bk, lg, rc, ok, er)):
        c.badge(node.x, node.y, n, kind="infra")

    c.legend(40, 625, [("infra", "桥接内的步骤", False), ("compute", "正常返回", False),
                       ("danger", "失败出口", False)])
    return c
