"""一次训练的编排:从派发器拿到指令,到候选注册、记录置 executed。步骤号与正文步骤表一一对应。"""
from svgkit import Canvas


def build() -> Canvas:
    c = Canvas(
        1080, 560, title="一次训练的编排",
        desc="派发器校验指令、算好起点记忆摘要并备好轨迹源。编排器读训练记录判定起点,落盘后加载权重。"
             "batch 先对整份 GRPO 数据集预检,再逐步训练并周期存点;最后一步之后存最终点、转换格式、注册候选、记录置 executed。"
             "确定性失败把记录写成 failed,其余异常让记录保持 pending。")
    W, H = 230, 80
    s1 = c.box(30, 70, W, "派发器准备", "读并校验 training_config\ninteract 算起点记忆摘要\n构造归因后端与轨迹源", kind="compute", h=H)
    s2 = c.box(290, 70, W, "判定起点", "decide_resume\nnoop 返回 · escalate 抛出", kind="compute", h=H)
    s3 = c.box(550, 70, W, "起点落盘", "写 pending 记录\n再 executor.load", kind="compute", h=H)
    s4 = c.box(810, 70, W, "batch 预检", "整份 GRPO 数据算奖励\n校验组大小与组数", kind="compute", h=H)
    s5 = c.box(550, 240, W, "逐步训练", "phase_at · batch_for_step\nsft_step / grpo_opd_step", kind="compute", h=H)
    s6 = c.box(810, 240, W, "周期存点", "每 checkpoint_every_steps 步\n回写引用与曲线摘要", kind="compute", h=H)
    s7 = c.box(550, 410, W, "最终存点与转换", "先回写 pending\n再 converter.convert", kind="compute", h=H)
    s8 = c.box(810, 410, W, "注册候选", "candidate pending_review\n记录置 executed", kind="compute", h=H)
    det = c.box(30, 240, W, "确定性失败", "记录写 failed · T2\n异常原样重抛", kind="danger", h=H)
    tra = c.box(30, 410, W, "其余异常", "记录保持 pending\n下次派发时续训", kind="neutral", h=H)

    c.edge(s1, s2)
    c.edge(s2, s3)
    c.edge(s3, s4)
    c.edge(s4, s5, "bottom", "top")
    c.edge(s5, s6, a_f=0.3, b_f=0.3)
    c.edge(s6, s5, "left", "right", a_f=0.7, b_f=0.7)
    c.edge(s5, s7, "bottom", "top", label="最后一步之后")
    c.edge(s7, s8)
    c.edge(s5, det, "left", "right", a_f=0.3, b_f=0.3, label="数据或配置坏(含步骤 4)")
    c.edge(s5, tra, "left", "right", a_f=0.7, b_f=0.5, label="崩溃 · 瞬态")

    for n, node in enumerate((s1, s2, s3, s4, s5, s6, s7, s8), 1):
        c.badge(node.x, node.y, n, kind="compute")
    c.legend(40, 540, [("compute", "编排步骤", False), ("danger", "确定性失败", False), ("neutral", "交给续训", False)])
    return c
