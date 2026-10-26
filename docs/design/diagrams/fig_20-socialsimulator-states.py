"""实时会话状态机。与 src/simulator/realtime/session.py 的迁移表和 driver.py 的收尾分支一致。"""
from svgkit import Canvas


def build() -> Canvas:
    c = Canvas(
        1080, 592, title="实时会话状态机",
        desc="创建请求校验通过才建驱动器,随即进入 ACTIVE;校验、准入或依赖失败时不建会话,直接以 EndSession 拒绝。"
             "运行器返回后进入 COMPLETING 再到 CLOSED 并落盘;超时、后端失败、记忆失败、断连转入 FAILED,不落盘。"
             "CLOSED 与 FAILED 之后,服务端按 quiesce、join、close 的顺序收尾会话资源。")
    kw = dict(h=46, title_size=14)
    st = c.box(16, 74, 150, "流已连接", "等待 CreateSession", kind="neutral", h=58)
    cr = c.box(236, 80, 140, "CREATED", kind="neutral", **kw)
    ac = c.box(446, 80, 140, "ACTIVE", kind="compute", **kw)
    co = c.box(656, 80, 150, "COMPLETING", kind="compute", **kw)
    cl = c.box(916, 80, 140, "CLOSED", kind="store", **kw)
    rj = c.box(16, 270, 230, "创建被拒(无会话)",
               "invalid_session\nrole_backend_unavailable\nrole_backend_failure", kind="danger", h=82)
    fa = c.box(430, 280, 200, "FAILED", kind="danger", **kw)
    sh = c.box(866, 372, 190, "终态收尾", "quiesce → join → close", kind="neutral", h=56)

    c.edge(st, cr, label="校验通过")
    c.edge(cr, ac, label="start()")
    c.edge(ac, co, label="运行器返回")
    c.edge(co, cl, label="推 EndSession")
    c.edge(st, rj, "bottom", "top", a_f=0.9, b_f=(151 - 16) / 230, label="校验 · 准入 · 依赖失败 · 10 秒未创建")
    c.edge(cr, fa, "bottom", "left", label="迁移表允许")
    c.edge(ac, fa, "bottom", "top", b_f=(516 - 430) / 200, label="超时 · 后端失败 · 记忆失败 · 断连")
    c.edge(co, fa, "bottom", "right", label="收尾时已有中止原因", label_at=0.4)
    c.edge(cl, sh, "bottom", "top", b_f=(986 - 866) / 190, label="进入终态")
    c.edge(fa, sh, "bottom", "left", label="进入终态", label_at=0.75)

    c.note(16, 450, 330, "CLOSED 的终止原因\nmax_turns · client_terminated\ngoal_reached 只出现在批量路径\n收尾时调 on_session_closed 落盘", kind="neutral")
    c.note(372, 450, 420, "FAILED 的终止原因\ntimeout · client_disconnected\nrole_backend_timeout · role_backend_unavailable\nrole_backend_rejected · role_backend_failure\nrole_memory_unavailable · role_memory_failure\n不调落盘钩子,episode_id 为空", kind="neutral")
    c.legend(40, 570, [("compute", "进行中", False), ("store", "正常终态", False),
                       ("danger", "失败与拒绝", False)])
    return c
