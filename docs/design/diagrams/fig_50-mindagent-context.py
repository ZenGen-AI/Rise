"""MindAgent 的上下文图:谁触发它,它读写什么,经哪条受限直连采集经验。"""
from svgkit import Canvas

ORANGE = "#d97706"


def build() -> Canvas:
    c = Canvas(
        1080, 640, title="MindAgent 上下文",
        desc="调和循环产生 start_agent_round 与 admit_proposal 两个动作,交给组合根注册的 MindAgent 处理器。"
             "MindAgent 读模型与正式库、写轮次与提案,经受限直连②向实时环境服务采集部署经验,"
             "用 Actio 建实例,可选经外部推理桥接生成技能策略。门②由人工批准提案。")
    fac = c.box(24, 70, 180, "factory 组合根", "注册两个处理器\n注入配置与工厂", kind="neutral",
                h=80)
    cp = c.box(330, 70, 400, "ControlPlane", "调和循环 · 产物树\n动作 start_agent_round · admit_proposal", kind="control",
               h=80)
    g2 = c.gate(846, 70, 210, "门② 人工审核", "review · approve · reject\n验证结果在场才可批准", h=80)

    ma = c.box(330, 270, 400, "MindAgent", "轮次处理器 · 收录处理器\n部署 · 分析 · 提案 · 验证 · 打包", kind="compute", h=90)
    ss = c.box(856, 275, 200, "实时环境服务", "SocialSimulator\ngRPC 双向流 · 对手人格", kind="compute", h=80)

    ac = c.box(330, 470, 200, "Actio", "技能库 · 显式记忆\n每次响应附激活轨迹", kind="infra", h=80)
    br = c.box(550, 470, 200, "外部推理桥接", "可选:生成技能策略", kind="infra", h=80)

    c.edge(fac, cp, label="注册处理器")
    c.edge(g2, cp, "left", "right", label="改提案状态", color=ORANGE)
    c.edge(fac, ma, "bottom", "left", label="注入依赖")
    c.edge(cp, ma, "bottom", "top", a_f=0.3, b_f=0.3, label="调用处理器")
    c.edge(ma, cp, "top", "bottom", a_f=0.7, b_f=0.7, label="读写产物")
    c.edge(ma, ss, label="② 部署会话", tail=True, color=ORANGE, width=2.4)
    c.edge(ma, ac, "bottom", "top", a_f=(430 - 330) / 400, label="instantiate")
    c.edge(ma, br, "bottom", "top", a_f=(650 - 330) / 400, label="可选")

    c.note(800, 470, 256, "② 是受限直连,只在计算层内部\n开不开会话由调和循环决定\n部署 episode 写回产物树", kind="neutral")
    c.legend(40, 616, [("control", "协调层", False), ("compute", "计算层", False), ("infra", "共享基础设施", False),
                       ("human", "人工门 / 受限直连", False)])
    return c
