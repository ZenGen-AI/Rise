"""命令地图:两个语言、三个入口各管哪些命令,读写什么。"""
from svgkit import Canvas


def build() -> Canvas:
    c = Canvas(
        1080, 560, title="命令地图",
        desc="TypeScript 命令行管门控写命令,直接改产物状态并提交。Python 一侧管调和与只读可观测,"
             "只读子命令不提交。两侧只经产物树通信。")
    c.lane(14, 44, 500, 450, "TypeScript 命令行 rise(门控写)", kind="human", dashed=False)
    c.lane(566, 44, 500, 450, "Python 入口 factory(调和与只读)", kind="control", dashed=False)

    rv = c.box(40, 90, 210, "review <id>", "按类型打印证据\n只读", kind="human", h=64)
    st = c.box(280, 90, 210, "status", "列出待审与待确认\n只读", kind="human", h=64)
    ap = c.box(40, 200, 210, "approve <id>", "证据不在场就拒绝\npending_review → approved", kind="human", h=64)
    rj = c.box(280, 200, 210, "reject <id> [原因]", "写 reject_reason\npending_review → rejected", kind="human",
               h=64)
    cf = c.box(40, 310, 450, "confirm <directive-id>", "awaiting_confirmation → pending", kind="human", h=56)

    run = c.box(592, 90, 210, "run", "一直跑调和循环\n退出码 0 或 2", kind="control", h=64)
    rec = c.box(832, 90, 210, "reconcile --once", "跑一轮并打印动作\n退出码 0 或 2", kind="control", h=64)
    ins = c.box(592, 200, 450, "install-defaults", "显式写默认资产与配置 · run 与 reconcile 从不自动写",
                kind="control", h=56)
    ro = c.box(592, 300, 450, "trace · cost · logs <run>", "只读 · 不提交 · 缺失时退出码 1(cost 除外)",
               kind="control", h=56)

    tree = c.store(380, 410, 320, "git 产物树", "control-plane/ · 两侧唯一的通信面", h=70)

    c.edge(ap, tree, "left", "left", b_f=0.5, via=[(26, 232), (26, 445)], label="写状态并提交")
    c.edge(cf, tree, "bottom", "top", a_f=0.85, b_f=0.15)
    c.edge(ro, tree, "bottom", "top", a_f=0.15, b_f=0.85, label="只读")
    c.legend(40, 540, [("human", "门控写命令", False), ("control", "调和与只读", False), ("store", "存储", False)])
    return c
