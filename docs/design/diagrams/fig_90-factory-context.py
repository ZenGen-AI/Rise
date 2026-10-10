"""factory 与命令行的上下文图:人从三个入口进来,组合根装配子系统并启动调和循环,一切经产物树。"""
from svgkit import Canvas


def build() -> Canvas:
    c = Canvas(
        1080, 600, title="factory 与命令行的上下文",
        desc="人经两种语言的命令行进入系统。TypeScript 命令行直接改产物树并提交;"
             "Python 入口调用组合根 factory,由它装配各子系统并启动调和循环。"
             "原守护进程入口保留,只注入 MindTrainer 派发器。")
    c.lane(210, 44, 320, 470, "人机入口", kind="human", dashed=False)

    human = c.box(24, 238, 160, "人工", "审核人 · 开发者", kind="human", h=64)

    ts = c.box(236, 80, 270, "TypeScript 命令行", "rismforge review · approve\nreject · status · confirm",
               kind="human", h=80)
    py = c.box(236, 230, 270, "Python 入口", "run_factory.py · run_factory_dev.py\nrun · reconcile · 只读子命令",
               kind="control", h=80)
    old = c.box(236, 390, 270, "守护进程入口", "run_controlplane.py\n→ mindtrainer.wire", kind="control",
                h=80)

    fac = c.box(580, 230, 220, "factory 组合根", "唯一知道所有包的地方\n任何包都不导入它", kind="control", h=80)
    subs = c.box(580, 390, 220, "被装配的子系统", "派发器 · 诊断评估器\n六个处理器 · 外部推理桥接",
                 kind="compute", h=80)

    tree = c.store(850, 70, 200, "git 产物树", "control-plane/\nYAML · 每次变更提交", h=100)
    cp = c.box(850, 230, 200, "ControlPlane", "调和循环 run_daemon\nreconcile_once", kind="control", h=80)

    c.edge(human, ts, "right", "left", a_f=0.2, label="审核 · 确认")
    c.edge(human, py, "right", "left", a_f=0.5)
    c.edge(human, old, "right", "left", a_f=0.8)
    c.edge(ts, tree, "right", "left", b_f=0.4, label="门控写 · 提交")
    c.edge(py, fac, label="调用")
    c.edge(fac, subs, "bottom", "top", label="构造")
    c.edge(fac, cp, label="注入并启动")
    c.edge(cp, tree, "top", "bottom", tail=True, label="读写")
    c.arrow([old.bottom(), (old.cx, 540), (950, 540), cp.bottom()], label="只注入 MindTrainer 派发器", label_at=0.5)
    c.edges.append((old.name, cp.name, "只注入 MindTrainer 派发器", False, False))

    c.legend(30, 584, [("human", "人工与门控写入口", False), ("control", "控制与协调", False),
                       ("compute", "计算层组件", False), ("store", "存储", False)])
    return c
