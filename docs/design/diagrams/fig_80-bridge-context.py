"""外部推理桥接的上下文:谁调用它、谁装配它、它调用谁、它往哪里写。"""
from svgkit import Canvas


def build() -> Canvas:
    c = Canvas(
        1080, 560, title="外部推理桥接的上下文",
        desc="TrainPlanner 与 MindAgent 经桥接调用外部强模型,factory 读配置装配桥接。"
             "桥接按变量名从环境取凭证,经子进程调外部命令行工具,成功调用记成本,日志脱敏后写进 git 不可见的目录。"
             "TrainScheduler 把任何桥接失败升级为 T3 写失败记录。")
    c.lane(14, 44, 1052, 140, "调用方与装配", kind="control")
    c.lane(14, 370, 720, 140, "落地位置", kind="store")

    ts = c.box(30, 90, 230, "TrainScheduler", "规划处理器\n桥接失败升级为 T3", kind="control", h=70)
    tp = c.box(290, 90, 230, "TrainPlanner", "BridgeDecisionBackend", kind="control", h=70)
    ma = c.box(550, 90, 240, "MindAgent", "技能提案生成器\n可选 judge 附注", kind="compute", h=70)
    fc = c.box(820, 90, 230, "factory 组合根", "读 backends.yaml\n按角色构造 CliBridge", kind="control", h=70)

    br = c.box(380, 230, 320, "外部推理桥接", "InferenceBridge 协议\nCliBridge · ScriptedBridge", kind="infra", h=90)
    ext = c.box(820, 230, 230, "外部强模型命令行", "claude -p · codex exec\n本机登录态", kind="neutral", h=80)

    tree = c.store(30, 410, 300, "ControlPlane 产物树", "run_trace · failure_record\nconfig/backends.yaml")
    logs = c.store(400, 410, 300, "桥接日志目录", ".rismforge/logs\n对 git 不可见")
    env = c.box(800, 410, 250, "环境变量", "凭证值只在这里\n不进 git 树", kind="human", h=80)

    c.edge(ts, tp, label="plan()")
    c.edge(tp, br, "bottom", "top", b_f=(tp.cx - br.x) / br.w, label="complete()")
    c.edge(ma, br, "bottom", "top", a_f=50 / 240, b_f=220 / 320, label="complete()")
    c.edge(fc, br, "bottom", "right", a_f=15 / 230, b_f=15 / 90, via=[(835, 200), (735, 200), (735, 245)], label="构造", label_at=0.35)
    c.edge(br, ext, a_f=40 / 90, label="子进程 · 标准输入")
    c.edge(env, br, "left", "right", b_f=70 / 90, via=[(765, 450), (765, 300)], label="按变量名解析")
    c.edge(br, tree, "left", "top", a_f=63 / 90, b_f=0.9, label="成本记录", label_at=0.3)
    c.edge(br, logs, "bottom", "top", a_f=0.6, b_f=(br.x + 0.6 * br.w - logs.x) / logs.w, label="脱敏日志")
    c.edge(ts, tree, "bottom", "top", a_f=0.5, b_f=(ts.cx - tree.x) / tree.w, label="T3 失败记录")

    c.legend(40, 540, [("control", "控制层", False), ("compute", "计算层", False), ("infra", "共享基础设施", False),
                       ("store", "存储", False), ("human", "凭证(人工提供)", False)])
    return c
