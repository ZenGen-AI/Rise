"""SocialSimulator 仿真部分的上下文:谁调用它,它写什么。"""
from svgkit import Canvas

ORANGE = "#d97706"


def build() -> Canvas:
    c = Canvas(
        1080, 560, title="SocialSimulator 仿真部分的上下文",
        desc="仿真部分居中。MindTrainer 经清单读批量数据集,经接口①跑实时会话;"
             "诊断评测与 ControlPlane 骨架桩以库调用复用它;它把情境、人格、Episode 与清单写进产物树。"
             "MindAgent 经接口②连入会话,Actio 的激活轨迹随每轮回传。")
    c.lane(14, 44, 300, 470, "调用方(计算层)", kind="compute")
    c.lane(766, 44, 300, 470, "存储与共享基础设施", kind="infra", label_align="end")

    mt = c.box(40, 96, 250, "MindTrainer", "batch:读清单并校验\ninteract:实时会话客户端", kind="compute", h=80)
    ma = c.box(40, 236, 250, "MindAgent", "部署经验采集客户端", kind="compute", h=64)
    dx = c.box(40, 372, 250, "诊断评测", "动态轨直接调用 run_episode", kind="compute", h=64)

    ss = c.box(390, 200, 300, "SocialSimulator 仿真部分", "情境与人格 · 仿真运行器\n批量数据集 · 实时环境服务", kind="compute", h=96)
    cp = c.box(415, 72, 250, "ControlPlane 骨架桩", "simulator.stubs 调情境生成器", kind="control", h=64)

    st = c.store(800, 186, 240, "产物树", "scenarios/ · personas/\nruns/<run>/episodes/\ndataset_manifest_<阶段>", h=124)
    ac = c.box(800, 392, 240, "Actio", "每轮附带激活轨迹", kind="infra", h=64)

    c.edge(mt, ss, "right", "left", a_f=0.65, b_f=0.3, label="① gRPC 会话", tail=True, color=ORANGE, width=2.2)
    c.edge(ma, ss, "right", "left", b_f=0.6, label="② gRPC 会话", tail=True, color=ORANGE, width=2.2)
    c.edge(dx, ss, "right", "left", b_f=0.88, label="库调用")
    c.edge(cp, ss, "bottom", "top", label="库调用")
    c.edge(ss, st, "right", "left", label="写入并读回", tail=True)
    c.edge(ac, ss, "left", "bottom", b_f=0.75, label="activation_trace")

    c.legend(40, 540, [("compute", "计算层", False), ("control", "控制层", False), ("store", "存储", False),
                       ("infra", "共享基础设施", False)])
    return c
