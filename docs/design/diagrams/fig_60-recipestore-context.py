"""RecipeStore 的上下文:谁调用它,它读写产物树的哪些目录。"""
from svgkit import Canvas

ORANGE = "#d97706"


def build() -> Canvas:
    c = Canvas(
        1080, 720, title="RecipeStore 的上下文",
        desc="TrainScheduler 写配方候选并调用 RecipeStore 的检索、增益与校验函数;"
             "调和循环经两个处理器驱动收录与战绩回写;RecipeStore 只读写产物树里的四类目录。")

    ts = c.box(30, 250, 200, "TrainScheduler", "提名器 · 规划处理器", kind="control", h=80)
    cp = c.box(350, 60, 420, "ControlPlane 调和循环", "产生 admit_recipe / record_track 动作", kind="control", h=80)
    fc = c.box(820, 60, 220, "factory 组合根", "注册两个处理器", kind="neutral", h=80)
    rs = c.box(360, 250, 400, "RecipeStore", "校验 · 检索 · 增益 · 战绩 · 生命周期\n收录处理器 · 战绩处理器",
               kind="control", h=100)

    rc = c.store(40, 480, 210, "recipe_candidates/", "配方候选 · 审核类")
    rp = c.store(300, 480, 210, "recipes/", "配方 · 谱系 · 战绩")
    rn = c.store(560, 480, 210, "runs/<run>/", "指令 · 候选 · 诊断报告")
    md = c.store(820, 480, 210, "models/", "zengen-vN · 只读")
    g3 = c.gate(40, 620, 210, "门③ 配方入库", "repro 在场才可批准", h=60)

    c.edge(ts, rs, label="检索 · 增益 · 校验")
    c.edge(ts, rc, "bottom", "top", b_f=(130 - 40) / 210, label="提名:写候选", label_at=0.25)
    c.edge(fc, cp, "left", "right", label="注册")
    c.edge(cp, rs, "bottom", "top", label="调用处理器")
    c.edge(rs, rc, "bottom", "top", a_f=0.06, b_f=0.7, label="读候选 · 置 effective", label_at=0.75)
    c.edge(rs, rp, "bottom", "top", a_f=(405 - 360) / 400, label="收录 · 战绩 · 废弃", label_at=0.35)
    c.edge(rs, rn, "bottom", "top", a_f=(665 - 360) / 400, label="读报告 · 写战绩标记", label_at=0.35)
    c.edge(rs, md, "right", "top", a_f=0.8, label="读基线 links")
    c.edge(g3, rc, "top", "bottom", label="approve / reject", color=ORANGE)

    c.legend(300, 650, [("control", "控制层", False), ("store", "产物树目录", False),
                        ("human", "人工审核门", False)])
    return c
