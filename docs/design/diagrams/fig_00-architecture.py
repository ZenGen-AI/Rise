"""总体架构图:控制层、协调层、计算层、共享基础设施,以及两条受限直连接口。"""
from svgkit import Canvas

ORANGE = "#d97706"


def build() -> Canvas:
    c = Canvas(
        1080, 730, title="RismForge 系统架构",
        desc="四层结构。控制层放决策与配方相关的四个组件。协调层 ControlPlane 配三道人工审核门。"
             "计算层放三个子系统。共享基础设施放 Actio、Starling 记忆库、远端计算层、训练与推理引擎。"
             "所有子系统经 ControlPlane 读写产物,只有两条受限直连接口绕过它。")
    c.lane(14, 44, 1052, 146, "控制层(本地开发机)", kind="control")
    c.lane(14, 214, 1052, 128, "协调层", kind="control", dashed=False)
    c.lane(14, 366, 1052, 150, "计算层(远程 GPU 服务器)", kind="compute", label_align="end")
    c.lane(14, 546, 1052, 130, "共享基础设施", kind="infra")

    rs = c.box(60, 90, 210, "RecipeStore", "配方仓库 · 战绩 · 生命周期", kind="control", h=80)
    ts = c.box(330, 90, 210, "TrainScheduler", "装配上下文 · 校验决策\n构造指令 · 提名配方", kind="control", h=80)
    tp = c.box(600, 90, 210, "TrainPlanner", "决策学习者\n可插拔决策后端", kind="control", h=80)
    br = c.box(870, 90, 180, "外部推理桥接", "命令行调用外部强模型\n凭证只留在控制层", kind="infra", h=80)

    cp = c.box(60, 255, 790, "ControlPlane", "git 版本化产物树 · 状态机 · 调和循环 · 指令确认", kind="control", h=64)
    gate = c.gate(880, 238, 170, "三道人工审核门", "①checkpoint 晋级\n②技能记忆纳入\n③配方入库", h=98)

    mt = c.box(60, 412, 210, "MindTrainer", "权重级训练\nSFT → GRPO + OPD", kind="compute", h=80)
    ss = c.box(330, 412, 210, "SocialSimulator", "情境仿真 · 实时环境服务\n诊断评测", kind="compute", h=80)
    ma = c.box(600, 412, 210, "MindAgent", "部署态智能体\n技能与记忆自进化", kind="compute", h=80)

    cm = c.box(40, 590, 215, "远端计算层 compute", "经 SSH 在远端 GPU 主机上跑作业\nGPU 租约 · 预检 · 作业控制", kind="infra", h=80)
    rv = c.box(300, 590, 160, "ROLL · vLLM", "分布式训练 · 推理引擎", kind="infra", h=80)
    ac = c.box(500, 590, 240, "Actio", "带技能库与显式记忆的智能体框架\n每次响应附带激活轨迹\nStarling 记忆", kind="infra", h=80)
    st = c.box(800, 590, 250, "Starling", "个体记忆 · 语句级抽取 · 语义召回\nSQLite 内嵌,一个人格一个库文件", kind="infra", h=80)

    def fx(node, target):
        return (node.cx - target.x) / target.w

    for n in (rs, ts, tp):
        c.edge(n, cp, "bottom", "top", b_f=fx(n, cp), tail=True)
    for n in (mt, ss, ma):
        c.edge(cp, n, "bottom", "top", a_f=fx(n, cp), tail=True)
    c.edge(cp, gate, "right", "left", tail=True)
    c.edge(rs, ts, label="查配方", tail=True)
    c.edge(ts, tp, label="规划", tail=True)
    c.edge(tp, br, label="推理")
    c.edge(mt, ss, label="①", tail=True, color=ORANGE, width=2.4)
    c.edge(ss, ma, label="②", tail=True, color=ORANGE, width=2.4)
    c.edge(mt, cm, "bottom", "top")
    c.edge(cm, rv, "right", "left", label="驱动")
    # SocialSimulator 正下方是 ROLL · vLLM,出口靠右、在两层之间的空隙里横移,再落到 Actio 顶边
    c.edge(ss, ac, "bottom", "top", a_f=0.8, b_f=0.17)
    c.edge(ma, ac, "bottom", "top", a_f=0.2, b_f=(ma.x + ma.w * 0.2 - ac.x) / ac.w)
    c.edge(ac, st, "right", "left", label="读写", tail=True)

    c.note(860, 412, 190, "①② 是受限直连接口\n只在计算层内部发生\n不经过 ControlPlane")
    c.legend(40, 706, [
        ("control", "控制层", False), ("compute", "计算层", False), ("infra", "共享基础设施", False),
        ("human", "人工审核门 / 受限直连", False)])
    return c
