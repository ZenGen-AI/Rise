"""ControlPlane 的上下文:谁驱动它,它调用谁,它读写什么。"""
from svgkit import Canvas


def build() -> Canvas:
    c = Canvas(
        1080, 560, title="ControlPlane 的上下文",
        desc="审核人经 TypeScript 命令行改审核状态,守护进程由入口启动并注入派发器。"
             "守护进程扫描 git 产物树,把指令交给派发器,缺省时调用骨架桩。"
             "诊断评估器与闭环处理器经注入缝接入,由 factory 组合根装配。")
    c.lane(250, 150, 560, 150, "ControlPlane(协调层,本地开发机)", kind="control", dashed=False)

    rev = c.box(30, 196, 190, "审核人", "人工审核门①\n确认指令", kind="human", h=76)
    cli = c.box(270, 196, 250, "人工命令行 rise", "TypeScript · 门控写命令\nreview approve reject status", kind="control", h=76)
    dmn = c.box(550, 196, 240, "调和守护进程", "Python · run_daemon\n纯函数 reconcile + 执行壳", kind="control", h=76)

    ent = c.box(550, 44, 240, "入口 run_controlplane", "mindtrainer.wire.main\n注入 MindTrainer 派发器", kind="compute", h=70)
    fac = c.box(830, 44, 220, "factory 组合根", "组装处理器与评估器", kind="neutral", h=70)

    mtd = c.box(850, 170, 200, "MindTrainer 派发器", "训练出候选 checkpoint", kind="compute", h=60)
    dia = c.box(850, 250, 200, "诊断评估器", "替换 request_diagnostic", kind="compute", h=60)
    hdl = c.box(850, 330, 200, "闭环处理器", "规划 · 提名 · 战绩\n智能体轮次 · 收录", kind="control", h=70)

    tree = c.store(270, 420, 450, "git 产物树 control-plane/", "YAML 信封 · 每次变更一次提交 · 唯一真相", h=76)
    stub = c.box(850, 440, 200, "simulator.stubs", "骨架桩:默认派发器\n与默认诊断器", kind="neutral", h=70)

    c.edge(rev, cli, label="命令")
    c.edge(cli, tree, "bottom", "top", b_f=(395 - 270) / 450, label="改状态并提交")
    c.edge(dmn, tree, "bottom", "top", a_f=0.3, b_f=(622 - 270) / 450, tail=True, label="扫描 · 写回")
    c.edge(ent, dmn, "bottom", "top", label="启动并注入")
    c.edge(fac, dmn, "bottom", "top", a_f=0.3, b_f=0.65, via=[(896, 140), (706, 140)])
    c.edge(dmn, mtd, a_f=0.2, b_f=0.5, label="派发指令")
    c.edge(dmn, dia, a_f=0.5, via=[(810, 233), (810, 280)])
    c.edge(dmn, hdl, a_f=0.8, b_f=0.3, via=[(830, 257), (830, 351)])
    c.edge(dmn, stub, "bottom", "left", a_f=0.85, b_f=0.5, via=[(754, 475)], label="缺省时调用", label_at=0.45, color="#dc2626")

    c.note(30, 330, 200, "派发器、评估器与处理器\n自己经 GitStore 写产物\n守护进程只管状态迁移", kind="neutral")
    c.legend(40, 540, [("control", "协调层 / 控制层", False), ("compute", "计算层与入口", False),
                       ("human", "人工", False), ("store", "存储", False)])
    return c
