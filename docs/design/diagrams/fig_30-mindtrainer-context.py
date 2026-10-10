"""MindTrainer 的上下文:谁给它指令与数据,它把记录与候选写到哪,它依赖哪些计算层服务。"""
from svgkit import Canvas


def build() -> Canvas:
    c = Canvas(
        1080, 640, title="MindTrainer 的上下文",
        desc="调和循环把 pending 的训练指令交给 MindTrainer 派发器。MindTrainer 从产物树读数据集清单,"
             "InteractTrain 经受限直连①向实时环境服务要 rollout,训练由执行器协议完成,"
             "结果以训练记录与候选 checkpoint 写回产物树。")
    c.lane(14, 44, 1052, 156, "控制层与协调层(本地开发机)", kind="control")
    c.lane(14, 226, 1052, 372, "计算层", kind="compute", label_align="end")

    author = c.box(40, 92, 220, "指令作者", "人工写入 directive\n或由 TrainScheduler 写入", kind="human", h=80)
    tree = c.store(330, 82, 340, "产物树 control-plane/", "directive · dataset_manifest\ntraining_run · candidate_checkpoint")
    cp = c.box(750, 92, 290, "ControlPlane 调和循环", "dispatch_directive 动作\n可注入的派发器 Dispatcher", kind="control", h=80)

    batch = c.box(40, 300, 230, "批量数据集", "SocialSimulator 生产\n清单带 sha256", kind="compute", h=80)
    mt = c.box(400, 300, 200, "MindTrainer", "派发器 · 编排器\n轨迹源 · 奖励 · 执行器", kind="compute", h=84)

    rt = c.box(300, 500, 240, "实时环境服务", "SocialSimulator · gRPC 双向流\n一条流一次会话", kind="compute", h=76)
    roll = c.box(600, 500, 200, "ROLL · vLLM", "训练执行器的后端\n分布式训练 · 推理引擎", kind="infra", h=76)
    dp = c.box(840, 500, 200, "DataProducer", "缺清单时先合成数据\n由组合根注入", kind="compute", h=76)

    c.edge(author, tree, label="写 directive")
    c.edge(cp, tree, "left", "right", label="扫描 / 回写状态", tail=True)
    c.edge(cp, mt, "bottom", "right", a_f=0.5, b_f=0.4, label="派发 pending 指令")
    c.edge(mt, tree, "top", "bottom", label="读清单 · 写记录与候选", tail=True)
    c.edge(batch, tree, "right", "bottom", a_f=0.3, b_f=0.1, label="写清单与 episode")
    c.edge(mt, rt, "bottom", "top", a_f=0.25, b_f=0.5, label="① rollout 会话", tail=True, color="#d97706", width=2.2)
    c.edge(mt, roll, "bottom", "top", a_f=0.75, b_f=0.5, label="TrainingExecutor 协议")
    c.edge(mt, dp, "right", "top", a_f=0.85, b_f=0.5, label="produce")

    c.legend(40, 622, [("control", "协调层", False), ("compute", "计算层", False), ("human", "人工动作 / 受限直连", False),
                       ("infra", "共享基础设施", False)])
    return c
