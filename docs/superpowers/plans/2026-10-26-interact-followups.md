# 场景二补齐三项 施工计划

**目标:** 把设计里标 `[计划中]`、本轮能做的三项落地:收尾时服务停不下来判 T2;没开 `--interact` 时失败原因点名这个开关;外部 API 对手超时归 `role_backend_timeout`,非 200 的处理用测试钉住。

**不在本计划:** 单次角色后端调用从 60 秒降到 15 秒。设计把"真模型单轮延迟未知"列为未核实项(`docs/design/00-overview.md` 第 21 项),没有真机数据就降,真模型可能一轮都赶不上。等真机测出延迟再定。

**权威:** `docs/design/30-mindtrainer.md` §5.3(收尾时服务仍在运行)与 §5.1(都没注入时的原因);`docs/design/90-factory-and-cli.md`(不打开 `--interact` 时、没传 `--interact` 却有 interact 指令);`docs/design/20-socialsimulator.md`(外部 API 对手单次调用超时、返回非 200)。

三个任务互不依赖,按顺序做,每个任务一笔提交。

---

### Task 1: 收尾时服务仍在运行,判 T2

**Files:**
- Modify: `src/compute/errors.py`、`src/compute/service.py`、`src/factory/interact_policy.py`
- Test: `tests/test_compute_service.py`、`tests/test_factory_interact_wire.py`
- Docs: `docs/design/30-mindtrainer.md`(§5.3 表格那一行、紧接的说明段、未核实/缺口表里的同一条)

**行为:**
- `compute/errors.py` 新增 `ServeStopError(ServeStartupError)`:停服务后作业仍在运行,租约保留。它继承 `ServeStartupError`,所以 `SomevalRunner` 等其他调用方的行为不变,仍按延后类处理。
- `compute/service.py` 里"停服务后仍在运行"那一处改抛 `ServeStopError`,信息不变。
- `factory/interact_policy.py` 的策略上下文:在整个服务上下文(`with ExitStack()`)外面接住 `ServeStopError`,译成 `DirectiveFailedError`(T2),原因点名作业 id,并写明要人工停掉占着显存的作业。不论它在进入、训练中还是退出时冒出来,都这样处理:作业停不下来,重试只会撞上同一个租约。起服务阶段的其他 `ServeStartupError` 照旧原样上抛,走延后通道。
- 登记注销、deadline 还原、对手开关解绑仍在各自的 `finally` 里,`ServeStopError` 不能跳过它们。

**测试:**
- compute 层:停不下来时抛的是 `ServeStopError`,它同时是 `ServeStartupError` 与 `DeferredError`。
- 装配层:`FakeServe` 增加"退出时抛"的能力。训练成功后退出抛 `ServeStopError`:指令 `failed`、类别 T2、原因含作业 id;`training_run` 为 `executed`,候选在树里。
- 装配层:训练体抛错之后退出又抛 `ServeStopError`:指令 `failed`;登记已注销、deadline 已还原、开关已解绑。
- 起服务阶段抛普通 `ServeStartupError` 的现有测试保持 `pending`,不改。

**文档:** 30 篇 §5.3 那一行与紧接的说明段、缺口表里的同一条转 `[已落地]`,写清现在的行为与代价:要人工停一次作业,换来不在已完成的训练上反复起服务。

---

### Task 2: 没开 `--interact` 时,失败原因点名开关

**Files:**
- Modify: `src/mindtrainer/dispatch.py`
- Test: `tests/test_mt_dispatch.py`、`tests/test_factory_interact_wire.py`(或 `tests/test_factory_wire.py`)
- Docs: `docs/design/30-mindtrainer.md` §5.1、`docs/design/90-factory-and-cli.md` 两处

**行为:**
- 派发器"都没注入"时的原因保留原文,后面追加 `; start factory run or factory reconcile with --interact to run interact directives`。
- 冻结入口(`mindtrainer.wire`)与 factory 的无开关路径用的是同一个无参派发器,两边写出的文字相同,G1(两个入口逐字节相同)不受影响。

**测试:**
- `tests/test_mt_dispatch.py` 现有那条加断言:原因含 `--interact`。
- factory 入口不带 `--interact`、树里有一条 interact 指令:指令 `failed`、T2,原因含 `--interact`。

**文档:** 30 篇 §5.1 "都没注入"一行、90 篇"不打开时"一行与错误处理表"没传 `--interact` 却有 interact 指令"一行转 `[已落地]`,写出新的原因文字。

---

### Task 3: 外部 API 对手超时与非 200

**Files:**
- Modify: `src/factory/opponents.py`
- Test: `tests/test_factory_opponents.py`、`tests/test_realtime_servicer_limits.py`(或新文件)、`tests/test_factory_interact_wire.py`
- Docs: `docs/design/20-socialsimulator.md`(故障表两行、本节结论段)

**行为:**
- `opponents.py` 新增 `OpponentTimeoutError(OpponentApiError, RoleBackendTimeoutError)`,`RoleBackendTimeoutError` 取自 `simulator.realtime.backend_timeout`。`post_chat` 的两处超时分支改抛它,信息与抹密钥规则不变。
- 服务端驱动在通用异常之前先接 `RoleBackendTimeoutError`,所以对手位超时会归 `role_backend_timeout`。这是 T1,轨迹源按 `max_retries_per_episode` 整条重跑。vLLM 对手与外部 API 共用 `post_chat`,也一并归到这里。
- 它仍是 `OpponentApiError`,所以训练位策略超时时,策略上下文照旧把它译成延后。
- 非 200 不改代码。现状:抛 `OpponentApiError` → 驱动归 `role_backend_failure` → 轨迹源不按条重跑(分不出 T1、T2) → `RolloutFailedError` → 策略上下文译成 `RolloutDeferredError` → 指令保持 `pending` 退避;续训次数到 `max_resume_attempts` 后升为 T3。用测试把这条链钉住。

**测试:**
- 单元:慢桩触发 `OpponentTimeoutError`;它对两个父类的 `isinstance` 都成立;信息里没有密钥。
- 真实服务端:外部 API 后端对着慢桩 → 会话以 `role_backend_timeout` 结束,不是 `role_backend_failure`。
- 装配层:外部对手第一次超时、第二次正常 → 指令 `executed`,episode 数对得上。
- 装配层:外部对手回 500 → 指令 `pending`,延后原因含 `role_backend_failure`。

**文档:** 20 篇故障表"外部 API 对手单次调用超时""返回非 200"两行与本节结论段转 `[已落地]`,按上面的实际行为写。"外部 API 对手的速率上限与成本"仍是 `[计划中]`。

---

### 收尾(控制者做)

- `docs/superpowers/specs/2026-10-24-interact-train-wiring-design.md` 的状态头,从"草案第三版,待审阅"改为已施工、已合并,并指向本计划。
- 验证:每个任务跑聚焦测试;最后跑一次 `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider -m "not slow"`;`docs/design` 的两个检查器都要 0 错误 0 告警。
