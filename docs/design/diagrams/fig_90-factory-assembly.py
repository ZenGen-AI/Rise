"""组合根的装配流程:解析开关,分两条路径;闭环路径在第一个 tick 之前装配完毕,出错退出码 2 不写树。"""
from svgkit import Canvas


def build() -> Canvas:
    c = Canvas(
        1080, 620, title="组合根的装配流程",
        desc="不传 --enable 时只构造 MindTrainer 派发器,与现有入口逐字节相同。"
             "传了 --enable 时读配置、校验资产与凭证、构造评估器与处理器,全部在第一个 tick 之前完成;"
             "任一步出错打印一行错误、退出码 2,树里不写任何东西。")

    p = c.box(40, 60, 230, "解析开关", "--enable → ENABLE_MAP\n未知开关与空项拒绝", kind="control", h=72)
    g = c.gate(330, 60, 200, "有处理器键吗", "handler_keys 非空?", kind="control", h=72)

    d0 = c.box(590, 60, 230, "默认路径", "只构造\nmake_mindtrainer_dispatcher()", kind="control", h=72)
    r0 = c.box(870, 60, 180, "run_daemon", "只传 dispatcher\n不读任何配置", kind="control", h=72)

    s1 = c.box(40, 230, 230, "读评估器配置", "config/evaluator.yaml\n恰好五个键", kind="control", h=72)
    s2 = c.box(330, 230, 220, "校验诊断资产", "分类表与题库在树里\n评估后端注入", kind="compute", h=72)
    s3 = c.box(610, 230, 220, "构造处理器", "DEFAULT_BUILDERS\n桥接 · 凭证 · agent.yaml", kind="control", h=72)
    s4 = c.box(860, 230, 190, "注入并启动", "dispatcher + 数据生产器\nhandlers · evaluator", kind="control", h=72)

    err = c.box(330, 420, 400, "FactoryConfigError", "标准错误打印 factory: error: ...\n退出码 2 · 树里不写任何东西",
                kind="danger", h=72)

    c.edge(p, g)
    c.edge(g, d0, label="否")
    c.edge(d0, r0)
    c.edge(g, s1, "bottom", "top", b_f=0.75, label="是")
    c.edge(s1, s2)
    c.edge(s2, s3)
    c.edge(s3, s4)
    c.edge(s1, err, "bottom", "left", label="缺文件或键")
    c.edge(s2, err, "bottom", "top", a_f=0.5, b_f=0.275, label="缺资产")
    c.edge(s3, err, "bottom", "top", a_f=0.5, b_f=0.875, label="缺角色或凭证")

    for n, node in enumerate((p, g, s1, s2, s3, s4), 1):
        c.badge(node.x, node.y, n, kind="control")

    c.note(760, 420, 290, "装配只做一次:运行中改 evaluator.yaml、\nbackends.yaml、agent.yaml 要重启才生效",
           kind="neutral")
    c.note(40, 520, 290, "run 与 reconcile 在第 1 步之前先取\n仓库排他文件锁,被占用退出码 2", kind="neutral")
    c.legend(40, 600, [("control", "装配步骤", False), ("compute", "诊断资产", False),
                       ("danger", "装配失败", False)])
    return c
