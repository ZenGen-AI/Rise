"""MindAgent 的模块组件图:两个处理器、五段流水线与三个支撑模块。"""
from svgkit import Canvas


def build() -> Canvas:
    c = Canvas(
        1080, 600, title="MindAgent 模块组成",
        desc="轮次处理器按编号顺序调用五段流水线,读配置与正式库,失败经统一守卫留痕。"
             "收录处理器独立运行,只用正式库与失败守卫。全部模块都在 src/mindagent 下。")
    c.lane(14, 30, 1052, 520, "src/mindagent", kind="compute")

    kw = dict(kind="compute", h=64)
    cfg = c.box(40, 80, 220, "AgentConfig", "config · 冻结数据类", **kw)
    lib = c.box(300, 80, 220, "正式库读取", "library · load_library", **kw)
    fl = c.box(560, 80, 220, "失败守卫", "failures · fail_once", **kw)
    ad = c.box(820, 80, 220, "收录处理器", "admit\nmake_admit_proposal_handler", kind="compute", h=64)

    rd = c.box(40, 240, 1000, "轮次处理器", "rounds · make_start_round_handler 返回 (store, action) 处理器",
               kind="compute", h=64)

    w, gap = 184, 20
    xs = [40 + (w + gap) * i for i in range(5)]
    specs = [("部署驱动器", "deployment\n跑部署 episode"), ("经验分析器", "analysis\n跨 episode 统计"),
             ("提案生成器", "proposals\n规则模板或桥接"), ("提案验证器", "verify\n离线重放度量行为"),
             ("提案打包器", "packaging\n筛选并写提案")]
    pipe = [c.box(x, 400, w, t, s, kind="compute", h=70) for x, (t, s) in zip(xs, specs)]

    for n in (cfg, lib, fl):
        c.edge(rd, n, "top", "bottom", a_f=(n.cx - rd.x) / rd.w)
    c.edge(ad, fl, "left", "right", a_f=0.5, b_f=0.5)
    c.edge(ad, lib, "top", "top", via=[(ad.cx, 50), (lib.cx, 50)])
    for i, p in enumerate(pipe, 1):
        c.edge(rd, p, "bottom", "top", a_f=(p.cx - rd.x) / rd.w)
        c.badge(p.x, p.y, i, kind="compute")

    c.note(40, 486, 700, "可以导入:controlplane、simple_actio、inference_bridge、simulator(数据模型、产物读写、实时客户端)\n"
                         "不得导入:mindtrainer、diagnostic、factory;controlplane 也不导入 mindagent", kind="neutral")
    c.legend(40, 580, [("compute", "计算层模块", False)])
    return c
