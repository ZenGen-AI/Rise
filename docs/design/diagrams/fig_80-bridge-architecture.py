"""桥接与横切记账的组件:inference_bridge 三个模块,controlplane 里的配置、记账、观测。"""
from svgkit import Canvas


def build() -> Canvas:
    c = Canvas(
        1080, 600, title="桥接组件与横切记账",
        desc="inference_bridge 由公共类型、命令行桥接、测试替身三个模块组成,只向 controlplane 一个方向依赖。"
             "controlplane 里的配置模块管凭证与脱敏,记账模块写成本与失败,观测模块只读,三者都经 GitStore 读写信封。")
    c.lane(14, 44, 850, 160, "inference_bridge 包", kind="infra")
    c.lane(14, 222, 850, 340, "controlplane 包", kind="control", dashed=False)
    c.lane(876, 44, 190, 518, "包外", kind="neutral")

    sc = c.box(30, 92, 230, "scripted.py", "ScriptedBridge\nScriptExhaustedError", kind="infra", h=80)
    base = c.box(300, 92, 250, "base.py", "BridgeResult · BridgeError\nInferenceBridge · 记账函数", kind="infra", h=80)
    cli = c.box(590, 92, 260, "cli.py", "CliBridge · RunOutcome\nRunner · subprocess_runner", kind="infra", h=80)

    ext = c.box(890, 92, 162, "外部命令行", "子进程\n提示词走标准输入", kind="neutral", h=80)
    logf = c.store(890, 250, 162, "日志文件", "<run>/<task>-<k>.log")

    obs = c.box(30, 280, 230, "observe.py", "trace · cost\n只读", kind="control", h=80)
    led = c.box(300, 280, 250, "ledger.py", "record_cost\nrecord_failure", kind="control", h=80)
    cfg = c.box(590, 280, 260, "config.py", "BackendConfig · 凭证解析 · 脱敏\n配置信封读写", kind="control", h=80)
    gs = c.box(30, 440, 820, "envelope.py · gitstore.py", "Envelope · BigRef · GitStore:读写信封并提交", kind="store", h=70)

    c.edge(sc, base, label="共用")
    c.edge(cli, base, "left", "right", label="共用")
    c.edge(base, led, "bottom", "top", b_f=0.5, a_f=(led.cx - base.x) / base.w, label="record_cost")
    c.edge(cli, cfg, "bottom", "top", label="解析凭证 · 脱敏")
    c.edge(cli, ext, a_f=0.5, label="argv")
    c.edge(cli, logf, "right", "left", a_f=0.85, b_f=0.5, via=[(870, cli.y + 0.85 * cli.h), (870, logf.cy)], label="写")
    c.edge(obs, gs, "bottom", "top", b_f=(obs.cx - gs.x) / gs.w, label="只读")
    c.edge(led, gs, "bottom", "top", b_f=(led.cx - gs.x) / gs.w, label="写信封")
    c.edge(cfg, gs, "bottom", "top", b_f=(cfg.cx - gs.x) / gs.w, label="读写配置")

    c.legend(40, 585, [("infra", "inference_bridge", False), ("control", "controlplane", False),
                       ("store", "存储层", False)])
    return c
