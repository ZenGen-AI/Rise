"""部署拓扑:控制层在本地开发机,计算层在远程 GPU 服务器,。"""
from svgkit import Canvas

ORANGE = "#d97706"


def build() -> Canvas:
    c = Canvas(
        1080, 640, title="部署拓扑",
        desc="控制层跑在本地开发机,含命令行、调和守护进程、git 产物树和决策组件;"
             "计算层跑在远程 GPU 服务器,含批量任务、训练进程、实时环境服务和大体量产物。"
             "两层之间经 SSH 下发任务,大体量产物在 git 树里只存引用。")
    c.lane(14, 44, 460, 430, "控制层 · 本地开发机", kind="control")
    c.lane(606, 44, 460, 430, "计算层 · 远程 GPU 服务器", kind="compute")
    c.lane(14, 500, 460, 100, "外部服务", kind="neutral")

    n1 = c.box(34, 92, 200, "人工命令行", "TypeScript\n审核 · 批准 · 驳回 · 状态", kind="human", h=70)
    n2 = c.box(250, 92, 200, "调和守护进程", "Python · 轮询产物树\n纯函数决定动作", kind="control", h=70)
    n3 = c.store(134, 210, 200, "git 产物树", "control-plane/\nYAML · 每次变更提交", h=80)
    n4 = c.box(34, 330, 190, "外部推理桥接", "命令行调用外部模型\n凭证只在本机", kind="infra", h=84)
    n5 = c.box(250, 330, 200, "决策与配方组件", "TrainScheduler\nTrainPlanner\nRecipeStore", kind="control", h=84)
    ext = c.box(34, 538, 420, "外部强模型", "Claude Code · Codex · DeepSeek", kind="neutral", h=56)

    m2 = c.box(626, 92, 200, "批量任务", "数据合成 · 诊断评测\n按需启动,跑完退出", kind="compute", h=70)
    m3 = c.box(842, 92, 200, "训练进程", "MindTrainer + ROLL\n训练执行器", kind="compute", h=70)
    m4 = c.box(626, 210, 200, "推理服务 vLLM", "rollout 与评测的模型调用", kind="infra", h=70)
    m1 = c.box(842, 210, 200, "实时环境服务", "gRPC 双向流 · 常驻进程\n每会话状态隔离", kind="compute", h=70)
    m5 = c.store(626, 330, 416, "大体量产物", "checkpoint · 数据集 · 日志\n训练与批量任务写入,git 树里只留引用", h=80)

    c.edge(n1, n3, "bottom", "top", b_f=0.25, label="写状态")
    c.edge(n2, n3, "bottom", "top", b_f=0.75, tail=True)
    c.edge(n5, n3, "top", "bottom", b_f=0.9, tail=True)
    c.edge(n5, n4, "left", "right")
    c.edge(n4, ext, "bottom", "top", b_f=(129 - 34) / 420, label="命令行进程")
    c.edge(n2, m2, "right", "left", tail=True, label="SSH 下发")
    c.edge(m3, m1, "bottom", "top", tail=True, color=ORANGE, width=2.4, label="①")
    c.edge(n3, m5, "right", "left", dashed=True, label="引用 + sha256")

    c.note(606, 500, 460, "MindAgent 进程同样驻留计算层,经受限接口②连接实时环境服务\n①② 只发生在计算层内部,不经过 ControlPlane", kind="human")
    c.legend(40, 622, [("control", "控制层", False), ("compute", "计算层", False), ("infra", "共享基础设施", False),
                       ("neutral", "虚线 = 只存引用,不存本体", True)])
    return c
