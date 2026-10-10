"""TrainScheduler 写出的产物各自的状态走向。"""
from svgkit import Canvas

ORANGE = "#d97706"


def build() -> Canvas:
    c = Canvas(
        1080, 470, title="调度器产物的状态",
        desc="规划决策从 pending 走到 executed 或 failed,规划前就失败时直接以 failed 写出。"
             "调度器写的指令按 confirm_directives 取初始状态。配方候选以 pending_review 写出,之后由人与 RecipeStore 推进。")
    c.lane(14, 44, 400, 380, "planner_decision", kind="control", dashed=False)
    c.lane(434, 44, 300, 380, "调度器写的 directive", kind="control", dashed=False)
    c.lane(754, 44, 312, 380, "recipe_candidate", kind="control", dashed=False)

    kw = dict(h=40, title_size=14)
    none = c.box(40, 96, 160, "尚无决策", kind="neutral", dashed=True, **kw)
    pend = c.box(40, 206, 160, "pending", kind="control", name="决策 pending", **kw)
    exe = c.box(40, 316, 160, "executed", kind="compute", **kw)
    fail = c.box(240, 316, 150, "failed", kind="danger", **kw)
    c.edge(none, pend, "bottom", "top", label="写下 next_run_id")
    c.edge(pend, exe, "bottom", "top", label="指令已写")
    c.edge(pend, fail, "right", "top", label="确定性失败", label_at=0.35)
    c.edge(none, fail, "right", "top", b_f=0.75, label="规划前失败", label_at=0.3)

    aw = c.box(460, 96, 250, "awaiting_confirmation", kind="human", **kw)
    dp = c.box(460, 236, 250, "pending", kind="control", name="指令 pending", **kw)
    c.edge(aw, dp, "bottom", "top", label="人工 confirm", color=ORANGE)
    c.note(460, 316, 250, "confirm_directives 为真:\n写成 awaiting_confirmation\n为假:直接写成 pending", kind="neutral")

    pr = c.box(780, 96, 200, "pending_review", kind="human", **kw)
    ap = c.box(780, 206, 200, "approved", kind="control", **kw)
    ef = c.box(780, 316, 200, "effective", kind="compute", **kw)
    c.edge(pr, ap, "bottom", "top", label="门③ approve", color=ORANGE)
    c.edge(ap, ef, "bottom", "top", label="RecipeStore 收录")

    c.legend(40, 450, [("human", "等待人工", False), ("control", "进行中", False), ("compute", "完成", False),
                       ("danger", "失败", False), ("neutral", "虚线框 = 产物尚不存在", True)])
    return c
