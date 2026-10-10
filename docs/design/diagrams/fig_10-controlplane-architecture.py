"""controlplane 包的模块与导入方向。"""
from svgkit import Canvas

RED = "#dc2626"


def build() -> Canvas:
    c = Canvas(
        1080, 560, title="controlplane 的模块",
        desc="三层模块。驱动层是纯函数 reconcile 与守护进程 daemon;"
             "工具层是只读解析、观测、配置函数,以及记账与证据等级;"
             "存储层是 GitStore、读取链、信封、路径与状态机。箭头是导入方向。")
    c.lane(14, 44, 1052, 124, "驱动层", kind="control", dashed=False)
    c.lane(14, 186, 1052, 124, "工具层(被其他包直接调用)", kind="control")
    c.lane(14, 328, 1052, 180, "存储与契约层", kind="store", dashed=False)

    rec = c.box(40, 84, 260, "reconcile.py", "纯函数 reconcile\nTreeState · Action", kind="control", h=66)
    dmn = c.box(420, 84, 240, "daemon.py", "run_daemon · reconcile_once\napply_action · 派发缝", kind="control", h=66)
    stub = c.box(800, 84, 240, "simulator.stubs", "骨架桩,包级回指", kind="danger", h=66)

    mod = c.box(40, 226, 180, "models.py", "latest_promoted\n_checkpoint", kind="control", h=66)
    obs = c.box(236, 226, 170, "observe.py", "trace · cost\nsum_costs", kind="control", h=66)
    cfg = c.box(680, 226, 170, "config.py", "凭证变量名\n脱敏 redact", kind="control", h=66)
    led = c.box(866, 226, 180, "ledger · evidence", "记账 · 证据等级", kind="neutral", h=66)

    pth = c.box(40, 380, 200, "paths.py", "目录常量与路径函数", kind="store", h=60)
    sta = c.box(40, 452, 200, "states.py", "两张迁移表", kind="store", h=48)
    git = c.box(420, 380, 240, "gitstore.py", "GitStore · git 子进程\n逐字节相同不提交", kind="store", h=66)
    mig = c.box(690, 380, 170, "读取链", "read_product 入口\n迁移 → Schema 校验", kind="store", h=66)
    env = c.box(886, 380, 160, "envelope.py", "Envelope · BigRef\nOwnerRef", kind="store", h=66)

    c.edge(dmn, rec, "left", "right", label="调用")
    c.edge(dmn, stub, label="导入", color=RED)
    c.edge(dmn, git, "bottom", "top", label="读写产物")
    c.edge(mod, git, "bottom", "top", b_f=0.12, via=[(130, 320), (449, 320)])
    c.edge(obs, git, "bottom", "top", b_f=0.25, via=[(321, 340), (480, 340)])
    c.edge(led, git, "bottom", "top", a_f=0.5, b_f=0.88, via=[(956, 320), (631, 320)])
    c.edge(git, pth, "left", "right", a_f=30 / 66)
    c.edge(git, mig)
    c.edge(mig, env)

    c.note(256, 454, 400, "daemon 另导入 states、paths、envelope\n命令行在 TypeScript 里各写一份迁移表与路径", kind="neutral")
    c.legend(40, 538, [("control", "驱动与工具", False), ("store", "存储与契约", False),
                       ("danger", "反向依赖", False)])
    return c
