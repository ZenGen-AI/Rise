"""Actio 包的模块组件图:八个源模块与唯一的包外依赖,箭头是主要导入方向。"""
from svgkit import Canvas


def build() -> Canvas:
    c = Canvas(
        1080, 630, title="Actio 的模块组成",
        desc="instance 在顶层编排技能路由、推理经验、记忆检索与轨迹组装。"
             "技能与经验共用触发谓词语言,谓词语言从信号注册表取信号,信号与记忆共用一处词面切分。"
             "箭头指向被导入的模块。errors 与 _check 被各模块导入,不画线。")
    inst = c.box(330, 50, 420, "instance.py", "instantiate · ActioInstance.respond / decide\n"
                 "GuidedBackend · IgnoreGuidance · render_guidance", h=84, kind="infra")

    sk = c.box(40, 210, 230, "skills.py", "Skill · route · check_library\n排序键 (-priority, -version, id)",
               h=76, kind="infra")
    ex = c.box(290, 210, 230, "experience.py", "ReasoningExperience\nactivate · select_tier 三级回退",
               h=76, kind="infra")
    me = c.box(560, 210, 230, "memory.py", "MemoryEntry · snapshot\nretrieve 词面重叠检索",
               h=76, kind="infra")
    tr = c.box(810, 210, 230, "trace.py", "build_trace · validate_trace\nnormalize_library_version",
               h=76, kind="infra")

    tg = c.box(165, 360, 230, "triggers.py", "五个算子 · 校验 · 求值\nreferenced_signals",
               h=76, kind="infra")
    sg = c.box(435, 360, 230, "signals.py", "SignalRegistry\ndefault_registry · 三个内置信号",
               h=76, kind="infra")

    er = c.box(40, 500, 300, "errors.py · _check.py", "ActioDataError · 字段校验\n被各模块导入",
               h=76, kind="infra")
    tx = c.box(560, 500, 230, "text.py", "tokens:ASCII 整词\n汉字相邻二字切分", h=76, kind="infra")
    dm = c.box(810, 500, 230, "simulator.datamodel", "TurnRecord · Persona\nScenario · 唯一的包外导入",
               h=76, kind="compute")

    for node, af in ((sk, 0.1), (ex, 0.37), (me, 0.63), (tr, 0.9)):
        c.edge(inst, node, "bottom", "top", a_f=af)
    c.edge(sk, tg, "bottom", "top", a_f=(200 - 40) / 230, b_f=(200 - 165) / 230)
    c.edge(ex, tg, "bottom", "top", a_f=(360 - 290) / 230, b_f=(360 - 165) / 230)
    c.edge(tg, sg, "right", "left")
    c.edge(sg, tx, "bottom", "top", a_f=(610 - 435) / 230, b_f=(610 - 560) / 230)
    c.edge(me, tx, "bottom", "top", a_f=(730 - 560) / 230, b_f=(730 - 560) / 230)
    c.edge(sg, dm, "right", "top", b_f=0.5, label="TurnRecord")

    c.legend(40, 612, [("infra", "actio 包内模块", False), ("compute", "包外模块", False)])
    return c
