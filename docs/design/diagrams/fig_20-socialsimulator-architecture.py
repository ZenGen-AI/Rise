"""SocialSimulator 仿真部分的组件:批量模块与实时模块共享同一个运行器核心。"""
from svgkit import Canvas

ORANGE = "#d97706"


def build() -> Canvas:
    c = Canvas(
        1080, 770, title="SocialSimulator 仿真部分的组件",
        desc="三层组件。共享核心是数据模型、后端协议与纯函数运行器。"
             "批量模块按需运行,生成情境与人格、产出数据集与清单并落盘;"
             "实时模块是由调用方启动的 gRPC 服务端,驱动器在后台线程调用同一个运行器。客户端运行在调用方进程。")
    c.lane(14, 44, 1052, 150, "批量模块:按需调用,跑完即止", kind="compute")
    c.lane(14, 216, 1052, 150, "共享核心:无 IO 的纯函数与协议", kind="compute", dashed=False)
    c.lane(14, 388, 1052, 330, "实时模块:gRPC 服务端,由调用方启动(客户端除外)", kind="compute")

    kw = dict(kind="compute", h=78)
    gen = c.box(40, 92, 280, "情境与人格生成", "generators.py\ngenerate_* · load_*", **kw)
    ds = c.box(400, 92, 280, "数据集与清单", "dataset.py\nproduce_grouped_dataset", **kw)
    sa = c.box(760, 92, 280, "存储适配", "store_adapter.py\nwrite_* · read_*", **kw)

    be = c.box(40, 264, 280, "后端协议", "backends.py\nModelBackend · GenerationBackend", **kw)
    rn = c.box(400, 264, 280, "仿真运行器", "runner.py\nrun_episode", **kw)
    dm = c.box(760, 264, 280, "数据模型", "datamodel.py\nPersona · Scenario · Episode", **kw)

    rb = c.box(40, 436, 280, "实时后端", "ClientDrivenBackend\nTimeoutBackend", **kw)
    dr = c.box(400, 436, 280, "会话驱动器", "driver.py · SessionDriver\n后台线程 · outbox 队列", **kw)
    se = c.box(760, 436, 280, "会话状态机", "session.py\nassert_transition", **kw)

    cl = c.box(40, 604, 200, "客户端", "client.py\nEnvironmentServiceClient", kind="neutral", h=78)
    sv = c.box(400, 604, 280, "服务端", "servicer.py · server.py\nEnvironmentServicer", **kw)
    vc = c.box(760, 604, 280, "校验与编解码", "validation.py · codec.py\nrealtime_env.proto", **kw)

    c.edge(gen, be, "bottom", "top", label="GenerationBackend")
    c.edge(ds, rn, "bottom", "top", label="每条 rollout 调一次")
    c.edge(ds, sa, label="落盘与读回")
    c.edge(rn, be, "left", "right", label="respond")
    c.edge(dr, rn, "top", "bottom", label="同一个 run_episode")
    c.edge(dr, rb, "left", "right", label="客户端位 · 对手位")
    c.edge(dr, se, label="状态迁移")
    c.edge(rb, be, "top", "bottom", label="实现 ModelBackend")
    c.edge(sv, dr, "top", "bottom", label="启动 · 转发提交")
    c.edge(sv, vc, label="先校验再启动")
    c.edge(cl, sv, label="gRPC 双向流(接口①②)", tail=True, color=ORANGE, width=2.2)

    c.note(760, 224, 280, "数据模型被三层共同引用,连线从略", kind="neutral")
    c.legend(40, 748, [("compute", "SocialSimulator 组件", False), ("neutral", "运行在调用方进程", False)])
    return c
