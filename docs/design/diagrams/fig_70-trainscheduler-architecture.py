"""TrainScheduler 的模块组成与包外依赖。"""
from svgkit import Canvas


def build() -> Canvas:
    c = Canvas(
        1080, 690, title="TrainScheduler 的模块",
        desc="两个处理器是对外入口。规划处理器串起 run id 分配、上下文装配、动作解析与指令构造;"
             "提名处理器只经 recipes_port 用 RecipeStore。对 RecipeStore 的导入全部集中在 recipes_port,"
             "对 MindTrainer 只用 training_config 与 data_spec 两个模块。")
    c.lane(20, 44, 780, 470, "src/trainscheduler", kind="control")

    kw = dict(kind="control")
    nom = c.box(40, 96, 250, "nominate", "提名判定 · 配方候选\nmake_nominate_handler", h=70, **kw)
    ph = c.box(380, 96, 360, "plan_handler", "make_plan_handler\n写入顺序与三条重入规则", h=70, **kw)
    tp = c.box(820, 96, 220, "trainplanner", "plan · PlanningContext\nPlannerDecision", h=70,
               kind="neutral")

    rid = c.box(200, 250, 130, "run_ids", "分配 run-NNN", h=60, **kw)
    asm = c.box(350, 250, 130, "assemble", "装配上下文", h=60, **kw)
    res = c.box(500, 250, 130, "resolve", "校验与解析", h=60, **kw)
    drv = c.box(650, 250, 130, "directive", "构造指令", h=60, **kw)
    mt = c.box(840, 250, 200, "mindtrainer", "training_config\ndata_spec", h=64, kind="compute")

    port = c.box(40, 410, 220, "recipes_port", "对 RecipeStore 的全部导入", h=60, **kw)
    cfg = c.box(300, 410, 180, "config", "两份配置的严格解析", h=60, **kw)
    err = c.box(520, 410, 180, "errors", "SchedulerError 家族", h=60, **kw)
    rs = c.box(40, 560, 220, "recipestore", "gain · ids · library · validate", h=60,
               kind="neutral")

    c.edge(ph, tp, label="调用")
    c.edge(ph, rid, "bottom", "top", a_f=(390 - 380) / 360, b_f=0.5, via=[(390, 200), (265, 200)])
    c.edge(ph, asm, "bottom", "top", a_f=(415 - 380) / 360, b_f=0.5)
    c.edge(ph, res, "bottom", "top", a_f=(565 - 380) / 360, b_f=0.5)
    c.edge(ph, drv, "bottom", "top", a_f=(715 - 380) / 360, b_f=0.5)
    c.edge(drv, mt, label="校验")
    c.edge(asm, port, "bottom", "top", b_f=(200 - 40) / 220, via=[(415, 360), (200, 360)])
    c.edge(res, port, "bottom", "top", b_f=(230 - 40) / 220, via=[(565, 380), (230, 380)])
    c.edge(nom, port, "bottom", "top", a_f=(70 - 40) / 250, b_f=(70 - 40) / 220)
    c.edge(port, rs, "bottom", "top", label="唯一导入口")

    c.note(300, 548, 470, "每个模块都可导入 controlplane、config 与 errors,连线从略\n"
                          "assemble、resolve、directive、config 都用 mindtrainer 的两个模块", kind="neutral")
    c.legend(40, 670, [("control", "本包模块", False), ("neutral", "控制层邻居包", False),
                       ("compute", "计算层邻居包", False)])
    return c
