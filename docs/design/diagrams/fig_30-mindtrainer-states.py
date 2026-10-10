"""训练记录 training_run 与候选 checkpoint 的状态:MindTrainer 写哪几步,哪几步归别人。"""
from svgkit import Canvas


def build() -> Canvas:
    c = Canvas(
        1080, 440, title="训练记录与候选的状态",
        desc="训练记录走非审核类状态机:pending 期间可反复回写存点进度,确定性失败到 failed,收尾到 executed。"
             "候选由 MindTrainer 以 pending_review 注册,之后的审核与晋级归审核门①与调和循环。")
    c.lane(14, 44, 1052, 170, "training_run(MindTrainer 写)", kind="compute", dashed=False)
    c.lane(14, 236, 1052, 160, "candidate_checkpoint(MindTrainer 只写第一步)", kind="compute", dashed=False)

    pend = c.box(60, 100, 220, "pending", "运行中 · 可续训\n回写 checkpoint 进度", kind="compute", h=80)
    exe = c.box(440, 70, 200, "executed", "候选已注册", kind="compute", h=56)
    fail = c.box(440, 146, 200, "failed", "确定性失败 · T2\n终态,保留", kind="danger", h=64)
    cons = c.box(800, 70, 220, "consumed", "不由 MindTrainer 写", kind="neutral", h=56, dashed=True)

    pr = c.box(60, 296, 220, "pending_review", "register_candidate 写入", kind="compute", h=60)
    ap = c.box(440, 270, 200, "approved", "审核门① 人工批准", kind="human", h=52)
    rj = c.box(440, 334, 200, "rejected", "审核门① 人工拒绝", kind="human", h=52)
    ef = c.box(800, 270, 220, "effective", "调和循环晋级为 zengen", kind="control", h=52)

    c.edge(pend, exe, b_f=0.5, a_f=0.3, label="收尾完成")
    c.edge(pend, fail, b_f=0.5, a_f=0.75, label="数据或配置坏")
    c.edge(exe, cons, label="下游消费", dashed=True)
    c.arrow([(130, 100), (130, 82), (210, 82), (210, 100)], label="存点回写")
    c.edge(pr, ap, a_f=0.3, b_f=0.5, label="approve")
    c.edge(pr, rj, a_f=0.75, b_f=0.5, label="reject")
    c.edge(ap, ef, label="promote")

    c.legend(40, 420, [("compute", "MindTrainer 写", False), ("danger", "失败终态", False),
                       ("human", "人工动作", False), ("control", "调和循环", False),
                       ("neutral", "虚线 = 别的写方", True)])
    return c
