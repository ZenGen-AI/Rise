"""Actio 的上下文图:谁实例化它、它调谁、它只依赖什么。"""
from svgkit import Canvas

ORANGE = "#d97706"


def build() -> Canvas:
    c = Canvas(
        1080, 610, title="Actio 的上下文",
        desc="SocialSimulator 的运行器与 MindAgent 的客户端循环都实例化 Actio 并调用 respond。"
             "Actio 把技能、经验与记忆渲染成指导语交给模型后端,包外只导入 simulator.datamodel。"
             "技能库与记忆库由 MindAgent 读入后传给 Actio,Actio 自己不读产物树。")
    c.lane(14, 40, 1052, 150, "计算层", kind="compute")
    c.lane(14, 214, 1052, 170, "共享基础设施", kind="infra")
    c.lane(14, 408, 1052, 160, "Actio 之外的依赖与数据", kind="neutral")

    ss = c.box(60, 86, 320, "SocialSimulator", "仿真运行器 run_episode\n实时环境服务 · episode 落盘",
               h=84, kind="compute")
    ma = c.box(700, 86, 320, "MindAgent", "部署驱动器的客户端循环\n分析 · 提案 · 验证",
               h=84, kind="compute")
    ac = c.box(380, 262, 320, "Actio", "instantiate 组装 ActioInstance\nrespond 返回三元组与激活轨迹",
               h=90, kind="infra")
    dm = c.box(60, 458, 280, "simulator.datamodel", "Persona · Scenario · TurnRecord",
               h=70, kind="compute")
    mb = c.box(400, 458, 280, "模型后端", "respond 或 respond_guided\n测试用罐头后端", h=70, kind="neutral")
    lib = c.store(740, 448, 280, "技能库与记忆库", "skill_library/ · memory_library/\n由 MindAgent 读写",
                  h=90)

    c.edge(ss, ac, "bottom", "top", a_f=0.75, b_f=0.15, label="runner 把实例当后端")
    c.edge(ma, ac, "bottom", "top", a_f=0.25, b_f=0.85, label="instantiate · respond")
    c.edge(ma, ss, "left", "right", label="② submit_turn 带轨迹", tail=True, color=ORANGE, width=2.4)
    c.edge(ac, mb, "bottom", "top", label="指导语 + history 副本")
    c.edge(ac, dm, "left", "top", label="唯一的包外导入")
    c.edge(lib, ma, "top", "bottom", b_f=0.75, label="读入条目与库版本")

    c.legend(40, 592, [("compute", "计算层", False), ("infra", "共享基础设施", False),
                       ("store", "产物", False), ("human", "受限直连②", False)])
    return c
