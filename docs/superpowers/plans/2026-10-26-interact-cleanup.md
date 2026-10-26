# 场景二清理:对手非 200 分类与清理清单 施工计划

**目标:** 第一部分把对手 API 的非 200 分成确定性和瞬态两类:瞬态的按条重跑,确定性的立即判失败,不再一律拖着整条训练延后。第二部分处理清理清单里能在本机完成的事项:行为小缺口、文档措辞、代码卫生。

**不在本计划:**
- 单次角色后端调用降到 15 秒。要等真机测出延迟。
- `policy_context` 的显存预检。要从宿主机读权重索引,只有真机上才验证得了。
- 情境 id 只由 `seed + i` 决定的覆盖问题。改 id 方案会改变已经写进树里的 id,要单独设计。
- `GitStore.write_product` 提交失败时留下孤儿文件。这是控制面的老问题,牵涉崩溃恢复语义,要单独设计。
- ruff:仓库没有配置,也没装。

**权威:** `docs/design/20-socialsimulator.md`(终止原因表、故障表)、`docs/design/30-mindtrainer.md`(§5、失败分类表)、`docs/design/50-mindagent.md`(部署的失败分类)、`docs/design/00-overview.md`(终止原因共九种那一段)。

每个任务一笔提交,按顺序做。

---

### Task 1: 新终止原因 `role_backend_rejected`(4xx 判 T2)

**分类口径:**

| 对手回复 | 异常 | 终止原因 | 处理 |
|---|---|---|---|
| 超时 | `OpponentTimeoutError`(已有) | `role_backend_timeout` | T1,按条重跑 |
| 429、5xx、连不上、对端中途断开 | `OpponentApiError`(不变) | `role_backend_failure` | 见 Task 2 |
| 400、401、403、404 等其余 4xx | 新增 `OpponentRejectedError(OpponentApiError, RoleBackendRejectedError)` | 新增 `role_backend_rejected` | T2,不重跑,训练记录 `failed`,指令 `failed` |
| 回复不是约定的形状(非 JSON、缺字段、超长) | `OpponentApiError`(不变) | `role_backend_failure` | 见 Task 2 |

**Files:**
- `src/simulator/realtime/backend_timeout.py`:在 `RoleBackendTimeoutError` 旁边新增 `RoleBackendRejectedError(Exception)`。文档字符串写明:对端明确拒绝了请求,重试无益(T2)。
- `src/simulator/realtime/driver.py`:在通用 `except Exception` 之前接住它,报 `role_backend_rejected`。
- `src/simulator/realtime/proto/realtime_env.proto`:只改 `EndSession.reason` 的注释,把新原因加进词表。字段不变,不用重新生成 pb2。
- `src/factory/opponents.py`:新增 `OpponentRejectedError`。`post_chat` 在 `HTTPError` 与 `status != 200` 两处,按状态码决定抛哪一种:4xx 且不是 429 抛 `OpponentRejectedError`,其余照旧。信息与抹密钥规则不变。
- `src/mindtrainer/trajectory_source.py`:新原因不进 `_TRANSIENT_REASONS`(本来就不会重跑),文档字符串补上它。
- `src/mindtrainer/orchestrator.py`:`except RolloutFailedError` 里,`invalid_session` 与 `role_backend_rejected` 都写 `failed`。抽一个模块级常量 `_DETERMINISTIC_ROLLOUT_REASONS = frozenset({"invalid_session", "role_backend_rejected"})`。
- `src/factory/interact_policy.py`:策略上下文放行的判断从 `== "invalid_session"` 改为属于这个集合。为此把常量从 `mindtrainer.orchestrator` 导出,factory 去导入它(factory 是叶子,可以导入 mindtrainer)。训练位策略自己抛的 `OpponentRejectedError` 仍是 `OpponentApiError`,会被译成延后。这一点要在注释里写明理由:训练位是本 run 自己起的 vLLM,4xx 意味着服务配置坏了,但这一点和起服务的瞬态失败区分不开,先按现状处理,并把它记进 Concerns。
- `src/mindagent/deployment.py`:新原因不进 `TRANSIENT_REASONS`,所以部署同样不重跑,行为与 `invalid_session` 一致。补一条测试钉住。

**测试:**
- 单元:`post_chat` 对 400、401、403、404 抛 `OpponentRejectedError`;对 429、500、502、503 抛普通 `OpponentApiError`(不是 `OpponentRejectedError`);信息里没有密钥。
- 真实服务端:对手后端抛 `RoleBackendRejectedError` 时,会话以 `role_backend_rejected` 结束。
- 轨迹源:`role_backend_rejected` 不重跑,只尝试一次就抛 `RolloutFailedError`。
- 编排器:`role_backend_rejected` 时训练记录写 `failed`。
- 装配层:外部对手回 404,指令 `failed`、T2,原因含 `role_backend_rejected`。现有"回 500 时保持 pending"的测试,按 Task 2 的新行为改写。
- 部署:`role_backend_rejected` 不重跑。

---

### Task 2: 429、5xx 与连不上,按条重跑(新终止原因 `role_backend_unavailable`)

`role_backend_failure` 在服务端同时覆盖"后端确定性异常"和"工厂/解析器故障",轨迹源因此分不出 T1 和 T2,一律不重跑。本任务把能确定是瞬态的那部分单独拿出来。

**Files:**
- `src/simulator/realtime/backend_timeout.py`:新增 `RoleBackendUnavailableError(Exception)`,表示对端暂时不可用(429、5xx、连不上),是 T1。
- `src/simulator/realtime/driver.py`:接住它,报 `role_backend_unavailable`。
- proto 注释与 `src/simulator/realtime/client.py` 的文档字符串:补上新原因。
- `src/factory/opponents.py`:新增 `OpponentUnavailableError(OpponentApiError, RoleBackendUnavailableError)`。`post_chat` 遇到 429、5xx、`URLError`(非超时)、`OSError`、`http.client.HTTPException` 时抛它。回复形状不对的情况仍抛普通 `OpponentApiError`,归 `role_backend_failure`。
- `src/mindtrainer/trajectory_source.py` 与 `src/mindagent/deployment.py`:`role_backend_unavailable` 加进各自的瞬态集合。
- 两处瞬态集合是复制关系(mindagent 不导入 mindtrainer)。在 `tests/test_interact_parity.py` 里加一条,钉住两边相等。

**测试:**
- 单元:429、503 与连接被拒都抛 `OpponentUnavailableError`;回复形状不对抛普通 `OpponentApiError`。
- 真实服务端:会话以 `role_backend_unavailable` 结束。
- 装配层:外部对手第一次 503、之后正常,指令 `executed`。外部对手一直 503,重跑用尽,走 `RolloutDeferredError`,指令 `pending`(与旧的 500 测试同形)。
- parity:两份瞬态集合相等。

**文档(Task 1、2 一起):**
- 20 篇:终止原因表加两行;故障表"返回非 200"一行拆成 4xx、429/5xx/连不上、形状不对三行;"速率上限与成本"一行更新,仍标 `[计划中]`。
- 00 篇:终止原因"共九种"改为"共十一种",列出新的两种。
- 30 篇:失败分类表与 §5 里关于非 200 的说法。
- 50 篇:部署的失败分类表。
- 状态图源 `docs/design/diagrams/fig_20-socialsimulator-states.py` 第 29 行的 FAILED 注记补上两个新原因,然后跑 `python3 docs/design/diagrams/build_all.py 20-socialsimulator-states` 重新生成 svg 和 meta,再跑 `python3 docs/design/tools/sync_docs.py 20-socialsimulator.md` 把图写回文档。生成区只能用这两个脚本改,不能手改。

---

### Task 3: 行为小缺口

1. **scripted 对手没有剧本、也没有全局台词。** 只开 `--interact`、没有 `agent.yaml`,而某个对手位是 scripted 时,对手会一直说"(无更多发言)",产出的是垃圾 rollout。修法:在策略上下文进入时检查本 run 的 scripted 对手位,如果某个情境的 `opponent_script` 里没有该位的台词,运行时的全局台词也是空的,就判 `DirectiveFailedError`(T2),原因点名情境 id、角色位,并说明要在 `agent.yaml` 里给台词或者给情境写剧本。生成路情境的剧本来自生成后端,检查放在派发时做,点名路和生成路都覆盖。
2. **`opponent` 段在装配期冻结。** 现在改 `training_defaults.yaml` 的 `opponent` 段要重启守护进程才生效。修法:策略上下文每次进入时重读 `training_defaults.yaml` 的 `opponent` 段。读不到或写坏了,判 `DirectiveFailedError`,原因只报段名和异常类型,不带地址。装配期检查保留不变,继续作为启动门槛。`make_policy_factory` 的 `opponent` 参数删掉,改成在 scope 里读。

**测试:** 每项一条装配层测试;第 2 项要证明装配之后改了 `opponent` 段,下一条指令会用新值。

---

### Task 4: 代码卫生

1. `AgentRuntime.interact_deadline_s` 的 getter 去掉锁。读 float 是原子的,而锁不可重入,留着就是自死锁的陷阱。setter 保留锁。文档字符串写明理由。
2. `AgentRuntime.episode_write_failures` 改成有上限的容器(`collections.deque(maxlen=100)`),对外类型说明随之更新。测试里的 `== []` 断言改成 `list(...) == []`。
3. `opponents.py` 解析 JSON 时,把 `RecursionError` 与 `ValueError` 一起接住,归 `OpponentApiError`。加一条测试:发一个嵌套很深的 JSON。
4. `vllm_policy.policy_context` 和 `sombench_runner` 里重复的"取 alias 造 `SshTunnel`"代码,抽到 `compute/tunnel.py`,写成 `tunnel_factory_for(client, remote_port)`,两处都改用它。`policy_context` 的 `client`、`endpoint` 参数按实际类型标注(`ComputeClient` 一类;实际类型名去 `compute/` 里查)。
5. `servicer.py` 中落盘钩子失败时的 `failure_detail` 改为只放异常类名,不带异常文字(文字可能带远端地址)。`tests/test_realtime_servicer_e2e.py:295` 断言的是文字,改成断言类名,同时断言文字不在里面。
6. 导入边界测试:新增 `tests/test_import_boundaries.py`,在子进程里导入 `factory.opponents`、`factory.interact_checks`,断言 `grpc` 不在 `sys.modules` 里。不要断言 `factory.wire`:它经 `mindtrainer.dispatch → orchestrator → trajectory_source` 本来就会载入 grpc,改动前就是这样。
7. `tests/test_interact_parity.py` 里写死的 `model_backend` 名单,改成从 `mindtrainer.training_config` 里非训练位允许的取值推出来。没有常量就抽一个模块级常量 `OPPONENT_MODEL_BACKENDS`,`validate_training_config` 改用它。
8. `test_an_external_opponent_that_times_out_once...` 不再 monkeypatch 模块常量,改为通过 `make_policy_factory` 已有的参数注入超时。如果那条测试走的是装配入口、拿不到这个参数,就把模块常量改成测试夹具显式设置并在注释里说明,二选一,在报告里写清选了哪个。

---

### Task 5: 文档措辞

1. `30-mindtrainer.md` G8 的"奖励管线在这些发言上算得"改成"跑通"。MT-16(约 698 行)与 `00-overview.md` SYS-25(约 750 行)的理由栏把"奖励管线对真实发言成立"改为设计目标的写法,与旁边的 `[已落地]` 不矛盾:写明当前只用替身验证过。
2. "一直失败最终是 T3,不会无限退避"(30、90 两篇)补上前提:`max_resume_attempts`(默认 3)要小于 `DeferralBook.max_consecutive`(20);超过时,守护进程会因 `DeferredExhaustedError` 退出。
3. `90-factory-and-cli.md` 约 664 行 `test_factory_handlers.py` 的用例数改成实际收集数。最后统一核对一遍各测试表里的数字,本计划新增测试的文件要更新。

---

### 收尾(控制者做)

- 每个任务跑聚焦测试;最后跑一次 `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider -m "not slow"`;`docs/design` 的两个检查器都要 0 错误 0 告警。
- 从记忆 `interact-cleanup-list` 里划掉已做的项。
