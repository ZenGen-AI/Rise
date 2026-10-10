"""TrainScheduler 的上下文:谁触发它,它调谁,它读写树里的什么。"""
from svgkit import Canvas


def build() -> Canvas:
    c = Canvas(
        1080, 600, title="TrainScheduler 的上下文",
        desc="调和循环把 plan_next_round 与 nominate_recipe 两种动作交给 TrainScheduler 的处理器。"
             "处理器调 TrainPlanner 拿决策,查 RecipeStore 的配方与增益,借 MindTrainer 的两个数据模块校验指令,"
             "结果全部写回产物树。人工经命令行确认指令、审核配方候选,也可以直接写指令。")
    mt = c.box(40, 60, 240, "MindTrainer 数据模块", "training_config · data_spec\n只导入这两个模块",
               kind="compute", h=76)
    cp = c.box(400, 60, 280, "ControlPlane 调和循环", "读树 · 纯函数算动作\n注入的处理器执行动作",
               kind="control", h=76)
    hu = c.box(800, 60, 240, "人工命令行", "confirm 指令 · 门③ 审核\n直接手写指令",
               kind="human", h=76)

    tp = c.box(40, 250, 240, "TrainPlanner", "plan(ctx, validator)\n返回结构化决策",
               kind="control", h=76)
    ts = c.box(400, 250, 280, "TrainScheduler", "装配上下文 · 领域校验\n构造指令 · 提名配方",
               kind="control", h=76)
    rs = c.box(800, 250, 240, "RecipeStore", "find_recipes · get_recipe\ncompute_gain · 复现校验",
               kind="control", h=76)

    tree = c.store(400, 430, 280, "产物树", "读:候选 · 诊断 · 模型 · 成本 · 配置\n写:决策 · 指令 · 提名标记 · 配方候选",
                   h=96)

    c.edge(cp, ts, "bottom", "top", label="Action(run id)")
    c.edge(ts, tp, tail=True, label="规划")
    c.edge(ts, rs, tail=True, label="查配方与增益")
    c.edge(ts, mt, "left", "right", a_f=0.2, label="校验配置")
    c.edge(ts, tree, "bottom", "top", tail=True, label="读证据 · 写产物")
    c.edge(hu, tree, "right", "right", b_f=0.75, via=[(1060, 98), (1060, 502)], label="写状态", label_at=0.8)

    c.legend(40, 580, [("control", "控制层", False), ("compute", "计算层", False), ("human", "人工", False),
                       ("store", "产物树", False)])
    return c
