"""Actio 的上下文图:谁实例化它、它调谁、它只依赖什么,以及技能库与记忆库各自从哪来。"""
from svgkit import Canvas

ORANGE = "#d97706"


def build() -> Canvas:
    c = Canvas(
        1080, 610, title="Actio 的上下文",
        desc="SocialSimulator 的运行器把实例当后端调用,MindAgent 的客户端循环实例化 Actio 并调用 respond,"
             "factory 组合根构造训练路的实例。Actio 把技能、经验与记忆渲染成指导语交给模型后端,包外只导入 simulator.datamodel。"
             "技能库由 MindAgent 读入后传给 Actio,Actio 自己不读产物树。"
             "记忆经记忆端口读写 Starling 的树外 SQLite 库,Starling 是可选依赖。")
    c.lane(14, 40, 1052, 150, "计算层", kind="compute")
    c.lane(14, 214, 1052, 170, "共享基础设施", kind="infra")
    c.lane(14, 408, 1052, 160, "Actio 之外的依赖与数据", kind="neutral")

    ss = c.box(60, 86, 320, "SocialSimulator", "仿真运行器 run_episode\n实时环境服务 · episode 落盘",
               h=84, kind="compute")
    ma = c.box(700, 86, 320, "MindAgent", "部署驱动器的客户端循环\n分析 · 提案 · 验证",
               h=84, kind="compute")
    ac = c.box(380, 262, 320, "Actio", "instantiate 组装 ActioInstance\nrespond 返回三元组与激活轨迹",
               h=90, kind="infra")
    dm = c.box(28, 458, 244, "simulator.datamodel", "Persona · Scenario · TurnRecord",
               h=70, kind="compute")
    mb = c.box(288, 458, 244, "模型后端", "respond 或 respond_guided\n测试用罐头后端", h=70, kind="neutral")
    mem = c.store(548, 448, 244, "Starling 记忆库", "一个世界一个 SQLite 库(树外)\n可选依赖 extra memory",
                  h=90)
    lib = c.store(808, 448, 244, "技能库", "skill_library/\n由 MindAgent 读写", h=90)
    fac = c.box(730, 262, 200, "factory 组合根", "构造训练路的全部实例\n训练位 · 对手位", h=90, kind="neutral")

    c.edge(ss, ac, "bottom", "top", a_f=0.75, b_f=0.15, label="运行器把实例当后端调用")
    c.edge(ma, ac, "bottom", "top", a_f=0.25, b_f=0.85, label="instantiate · respond")
    c.edge(ma, ss, "left", "right", label="② submit_turn 带轨迹", tail=True, color=ORANGE, width=2.4)
    # 下行连线竖直对齐:Actio 底边 x = 380 + 320 * a_f,目标顶边 x = 左缘 + 244 * b_f
    c.edge(ac, mb, "bottom", "top", a_f=(470 - 380) / 320, b_f=(470 - 288) / 244, label="指导语 + history 副本")
    c.edge(ac, mem, "bottom", "top", a_f=(640 - 380) / 320, b_f=(640 - 548) / 244,
           label="recall · remember", tail=True)
    c.edge(ac, dm, "left", "top", b_f=(215 - 28) / 244, label="唯一的包外导入")
    c.edge(fac, ac, "left", "right")
    c.edge(lib, ma, "top", "bottom", a_f=(1000 - 808) / 244, b_f=(1000 - 700) / 320, label="读入条目与库版本")

    c.legend(40, 592, [("compute", "计算层", False), ("infra", "共享基础设施", False),
                       ("store", "产物与记忆库", False), ("neutral", "组合根与外部", False),
                       ("human", "受限直连②", False)])
    return c
