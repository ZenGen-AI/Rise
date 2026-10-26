"""Actio 包的模块组件图:十一个包内模块里的主要模块、记忆端口的两个实现,以及两处包外依赖。箭头是主要导入方向。"""
from svgkit import Canvas


def fx(node, x):
    """节点边上某个绝对横坐标对应的位置比例。"""
    return (x - node.x) / node.w


def build() -> Canvas:
    c = Canvas(
        1080, 620, title="Actio 的模块组成",
        desc="instance 在顶层编排技能路由、推理经验、记忆端口与轨迹组装。"
             "技能与经验共用触发谓词语言,谓词语言从信号注册表取信号,信号与替身记忆共用一处词面切分。"
             "记忆端口 memory_port 只定义协议与数据类型;memory 里的 TupleMemory 与 memory_starling 里的 StarlingMemory 是它的两个实现,"
             "starling 包只被 memory_starling 导入。箭头指向被导入的模块。errors 与 _check 被各模块导入,不画线。")
    inst = c.box(150, 40, 780, "instance.py", "instantiate · ActioInstance.respond / decide\n"
                 "GuidedBackend · IgnoreGuidance · render_guidance", h=84, kind="infra")

    sk = c.box(25, 190, 170, "skills.py", "Skill · route\ncheck_library", h=76, kind="infra")
    ex = c.box(215, 190, 180, "experience.py", "ReasoningExperience\nactivate · select_tier", h=76, kind="infra")
    me = c.box(415, 190, 190, "memory.py", "MemoryEntry · snapshot\nTupleMemory 替身", h=76, kind="infra")
    mp = c.box(655, 190, 210, "memory_port.py", "MemoryBackend 协议\nMemoryHit · MemoryEvent", h=76,
               kind="infra")
    tr = c.box(885, 190, 170, "trace.py", "build_trace\nvalidate_trace", h=76, kind="infra")

    tg = c.box(65, 330, 200, "triggers.py", "五个算子 · 校验 · 求值\nreferenced_signals", h=76, kind="infra")
    sg = c.box(295, 330, 230, "signals.py", "SignalRegistry\ndefault_registry · 三个内置信号",
               h=76, kind="infra")
    ms = c.box(655, 330, 210, "memory_starling.py", "StarlingMemory 适配器\n唯一导入 starling", h=76,
               kind="infra")

    dm = c.box(40, 470, 250, "simulator.datamodel", "TurnRecord · Persona\nScenario · 包外的项目模块", h=76,
               kind="compute")
    tx = c.box(440, 470, 190, "text.py", "tokens:ASCII 整词\n汉字相邻二字切分", h=76, kind="infra")
    st = c.box(655, 470, 210, "starling", "原生 SQLite 记忆库的 Python 包\n可选 extra memory", h=76,
               kind="neutral")

    # instance 向下:五条线各占一段水平折线,互不重叠
    c.edge(inst, sk, "bottom", "top", a_f=fx(inst, 180), b_f=fx(sk, 110))
    c.edge(inst, ex, "bottom", "top", a_f=fx(inst, 305), b_f=fx(ex, 305))
    c.edge(inst, me, "bottom", "top", a_f=fx(inst, 510), b_f=fx(me, 510))
    c.edge(inst, mp, "bottom", "top", a_f=fx(inst, 760), b_f=fx(mp, 760))
    c.edge(inst, tr, "bottom", "top", a_f=fx(inst, 900), b_f=fx(tr, 970))

    c.edge(sk, tg, "bottom", "top", a_f=fx(sk, 95), b_f=fx(tg, 125))
    c.edge(ex, tg, "bottom", "top", a_f=fx(ex, 255), b_f=fx(tg, 225))
    c.edge(tg, sg, "right", "left")
    c.edge(sg, dm, "bottom", "top", a_f=fx(sg, 330), b_f=fx(dm, 270), label="TurnRecord")
    c.edge(sg, tx, "bottom", "top", a_f=fx(sg, 505), b_f=fx(tx, 505))
    c.edge(me, tx, "bottom", "top", a_f=fx(me, 575), b_f=fx(tx, 575))
    c.edge(me, mp, "right", "left")
    c.edge(ms, mp, "top", "bottom")
    c.edge(ms, st, "bottom", "top", label="唯一的第三方导入")

    c.legend(40, 602, [("infra", "actio 包内模块", False), ("compute", "项目内的包外模块", False),
                       ("neutral", "第三方包", False)])
    return c
