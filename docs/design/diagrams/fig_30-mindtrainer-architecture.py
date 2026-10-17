"""MindTrainer 的组件:入口与编排一层,模式无关的训练流水线一层。两种轨迹源只在流水线入口分叉。"""
from svgkit import Canvas


def build() -> Canvas:
    c = Canvas(
        1080, 730, title="MindTrainer 的组件",
        desc="上层是入口与编排:派发器读配置并构造轨迹源工厂,编排器读训练记录交给续训判定,再逐步驱动训练。"
             "下层是模式无关的流水线:两种轨迹源产出同一种轨迹批,之后的奖励、分组、执行器与 checkpoint 管理器完全共享。"
             "真实执行器与转换器经远端计算层在远端主机上跑 ROLL 作业。")
    c.lane(14, 44, 1052, 152, "入口与编排", kind="compute", dashed=False)
    c.lane(14, 222, 1052, 344, "模式无关的训练流水线", kind="compute")
    c.lane(14, 590, 1052, 116, "远端计算(本地开发机经 SSH 驱动)", kind="infra")

    cfg = c.box(40, 92, 210, "训练配置", "training_config.py\n校验 · 哈希 · phase_at", kind="compute", h=78)
    disp = c.box(300, 92, 210, "派发器", "dispatch.py · wire.py\n指令 → 训练", kind="compute", h=78)
    orch = c.box(560, 92, 220, "编排器", "orchestrator.py\nresume_or_run · run_training", kind="compute", h=78)
    res = c.box(830, 92, 210, "续训判定", "resume.py\ndecide_resume 纯函数", kind="compute", h=78)

    inter = c.box(40, 276, 210, "Interact 轨迹源", "InteractTrajectorySource\n实时会话 · 受限直连①", kind="compute", h=78)
    batch = c.box(40, 380, 210, "Batch 轨迹源", "BatchTrajectorySource\n读清单 · 校验哈希", kind="compute", h=78)
    rew = c.box(290, 328, 170, "奖励管线", "reward_pipeline.py\n归因 · 校验 · 组合", kind="compute", h=78)
    grp = c.box(490, 328, 150, "分组与优势", "grouping.py\n组内均值基线", kind="compute", h=78)
    ex = c.box(670, 328, 160, "训练执行器", "executor.py\nMockExecutor", kind="compute", h=78)
    ck = c.box(860, 328, 180, "checkpoint 管理器", "checkpoint.py\n转换 · 注册候选", kind="compute", h=78)
    tree = c.store(290, 478, 750, "产物树", "dataset_manifest · episodes · training_run · candidate_checkpoint")
    rc = c.box(560, 626, 440, "远端计算层 compute", "ComputeClient · 助手脚本 · GPU 租约与预检\n真实执行器与转换器经它在远端主机上跑 ROLL 作业", kind="infra", h=64)

    c.edge(disp, cfg, "left", "right", label="读取并校验")
    c.edge(disp, orch, label="调用")
    c.edge(orch, res, label="判定")
    c.edge(disp, inter, "bottom", "top", a_f=0.2, b_f=0.5, via=[(342, 258), (145, 258)],
           label="make_sources 构造轨迹源")
    c.edge(disp, batch, "bottom", "left", a_f=0.2, via=[(342, 258), (26, 258), (26, 419)])
    c.edge(orch, ex, "bottom", "top", a_f=0.5, b_f=0.5, label="逐步驱动")
    c.edge(inter, rew, "right", "left", b_f=0.3, label="轨迹批")
    c.edge(batch, rew, "right", "left", b_f=0.7, label="轨迹批")
    c.edge(rew, grp)
    c.edge(grp, ex)
    c.edge(ex, ck)
    c.edge(batch, tree, "bottom", "left", label="读清单与 episode")
    c.edge(ck, tree, "bottom", "top", b_f=(950 - 290) / 750, label="注册候选")
    c.edge(ex, rc, "bottom", "right", a_f=0.5, via=[(750, 466), (1053, 466), (1053, 658)])
    return c
