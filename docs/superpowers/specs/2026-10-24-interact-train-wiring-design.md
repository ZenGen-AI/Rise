# 场景二接通设计:实时交互式训练在真实入口跑通

状态:草案,待审阅。日期:2026-10-24。
前置设计:`2026-10-03-mindtrainer-design.md`(InteractTrain 轨迹源)、`2026-10-09-closed-loop-integration-design.md`(闭环接线)、`2026-10-15-real-backends-design.md`(远端计算层、vLLM 服务)。
权威文档集是 `docs/design/`。本稿审定后,先并入其中的总览、SocialSimulator、MindTrainer、TrainScheduler、Factory 五篇,再写施工计划。

## 0. 这份文档做什么

场景二在测试里跑通,在真实入口跑不通。`factory reconcile --once` 派发一条 interact 指令,指令立即判 `failed`(T2),原因是 `interact mode needs a client_factory and a policy`。本稿要让场景二从真实入口走完:调度器写出 interact 指令,SocialSimulator 准备情境与人格并提供实时环境,训练位由加载了本轮基座的真实模型说话,MindTrainer 收集 rollout、算奖励、更新,产出候选。

已经定下的两条取舍:

1. 情境与人格由 SocialSimulator 在派发时现生成,写进产物树。与 batch 的「缺清单就合成」走同一条路,调度器不维护固定情境。
2. 训练位这一轮就接真实推理:经远端计算层在 vLLM 上服务本轮基座,轨迹源每轮向它要一句话。

第 2 条与现有约束有冲突,见第 1 节第 4 条与第 2 节 D1。冲突不解决,第 2 条在本轮实现不了,所以本稿把它拆成两步(第 9 节)。

不做:真实 GRPO 与 OPD 训练、真实 judge 归因、对手位接真实模型、并发 rollout、把对手 `model_ref` 锚定到带哈希的引用、异步派发。

## 1. 事实依据

以下逐条读过代码,附位置。

1. 实时环境服务已落地。`factory.agent.AgentRuntime` 在进程内起 loopback 服务端,对手位用 `ScriptedBackend` 念台词,情境经 `scenario_resolver` 从树里读;它的说明已写明两个驱动方是 MindAgent 部署与 MindTrainer 轨迹源。它不配 `on_session_closed`,episode 由 MindAgent 驱动器自己落盘(`src/factory/agent.py:100-160`)。
2. 训练侧的接口已在。`make_mindtrainer_dispatcher(client_factory=..., policy=...)` 两样都注入时,`grpo_opd` 阶段走 `InteractTrajectorySource`(`src/mindtrainer/dispatch.py:153-219`)。轨迹源不写 episode,依赖服务端的 `on_session_closed` 落盘,只在 metadata 里记 id(`src/mindtrainer/trajectory_source.py:129-131`)。`policy` 是 `simulator.backends.ModelBackend`:`respond(persona, scenario, history, speaker_slot) -> (utterance, inner_thought)`。它一次派发只构造一个,不随 run 变。
3. 调度侧写不出 interact 指令。`validate_decision` 对 `new` 加 `interact` 一律返回违规(`src/trainscheduler/resolve.py:86-95`);`resolve_action` 对 `new` 非 batch 抛 `ResolveError`(`:138-142`)。默认训练配置只有 batch 的形状,没有 `scenario_ids` 与 `bindings`(`src/trainscheduler/config.py:185-222`)。`allow_interact` 默认关(`:63`)。
4. 真实推理与训练执行器的约束:
   - `RollExecutor.supported_phases = {"sft"}`,GRPO 逐步方法抛错(`src/mindtrainer/roll_executor.py:87,182`);编排器训练前检查,配置带 `grpo_steps > 0` 时写 `failed`(`src/mindtrainer/orchestrator.py:187-197`)。interact 的实时会话只发生在 `grpo_opd` 阶段,所以**真实执行器下 interact 根本开不了会话**。
   - `factory` 里 `--executor roll` 不能与 `--enable` 同用(`src/factory/wire.py:234-241`)。
   - vLLM 服务经 `compute.service.serve` 起停:先取 GPU 租约,再起作业、开 SSH 隧道、等就绪,退出时停服务、放租约(`src/compute/service.py:60-111`)。租约是独占的,另一作业持有时抛 `ComputeBusyError`,走延后通道(`src/compute/errors.py:20`)。一次起服务的超时是 900 秒(`src/factory/sombench_runner.py:44`)。
   - `RollExecutor.load` 只接受指向 `base_model_path` 的起点基座或训练存点,不接受适配器导出(`roll_executor.py:104-126`)。vLLM 服务的命令行只能挂基座加一个 LoRA 适配器(`src/compute/vllm_service.py:65-81`)。
   - 默认配置的初始基座是 `file:///compute/checkpoints/initial-base.safetensors`、哈希全零的占位(`src/trainscheduler/config.py:189-192`),不是真实权重。
5. 实时服务端的时间上限:客户端每轮 30 秒内要提交,创建会话 10 秒内要发,后端单次调用 60 秒(`src/simulator/realtime/server.py:17-23`)。`AgentRuntime` 的流 deadline 是 15 秒,轮次超时 5 秒(`src/factory/agent.py:87,108`);它为罐头后端设定,真实推理要放宽。
6. 落盘钩子失败时,服务端报 `persistence_failed`,训练与部署都按瞬态整条重跑(`bad6df6`)。
7. 数据合成只写 episode 与清单,不写情境与人格文件(`src/mindtrainer/data_producer.py:111-147`);情境 id 形如 `scen-auto-<seed>`,人格 id 形如 `pers-auto-<seed>`(`src/simulator/backends.py:78-92`)。轨迹源每个 GRPO step 都从树里读情境与人格(`trajectory_source.py:178-185`)。

## 2. 裁决总表

| 编号 | 裁决 | 备选 | 理由 | 判错的代价 |
|---|---|---|---|---|
| D1 | 分两步落地:第一步训练位接一个**可替换的策略工厂**,默认是确定性替身,真实入口跑通场景二(mock 执行器);第二步把工厂换成 vLLM 真实推理,同时为它解开第 1 节第 4 条的执行器约束 | 一步到位接真实推理 | 真实执行器不支持 GRPO,真实推理在本轮找不到能开会话的执行器;硬接只能是「mock 训练配真推理」,训练结果不受真推理影响,而 GPU 租约要被占着 | 第一步交付的场景二仍不代表模型能力;第二步的工作量与真实 GRPO 绑在一起 |
| D2 | 情境与人格由 SocialSimulator 在派发时生成并写进树,与 SFT 冷启动数据同批合成 | 默认配置里放固定情境 | 与 batch 的数据合成同一路径;情境随种子变,能覆盖不同冲突 | 合成多写一批文件;重跑必须逐字节相同,否则提交噪声 |
| D3 | 新增 `interact` 段的默认配置:角色位布局与对手 `model_ref`,不含情境 id;情境 id 由合成器按 `data_spec` 算出并写进指令的 `training_config.scenario_ids` | 默认配置直接写情境 id | 情境 id 依赖种子,写死在默认配置里就和 D2 冲突 | 指令在派发前后 `scenario_ids` 是否在场不同,校验要分两态 |
| D4 | 训练用的实时服务端复用 `AgentRuntime`,加一个可选的落盘钩子:配了就由服务端落盘 | 训练另起一个服务端 | 一个进程一个 loopback 服务端;两个驱动方的落盘方式不同,用参数区分 | 共享服务端时两路并发会话;服务端本就按会话隔离 |
| D5 | `allow_interact` 仍默认关;`--enable` 之外加一个装配开关 `--interact`,只有它打开时装配才接实时服务端与策略工厂 | 打开 `allow_interact` 就接 | 实时服务端常驻进程、占端口;规划许可与运行时装配是两件事 | 两个开关要同时开,漏开一个时要报清楚是哪个 |
| D6 | 策略工厂签名 `(run_id, base_checkpoint_ref) -> ModelBackend`,派发每条指令时现构造;指令载荷里没有基座的模型 id,只有 `base_checkpoint_ref` | 一次派发构造一个固定策略 | 训练位必须是本轮基座;现有接口一次派发只有一个 `policy`,与 run 无关 | 派发器接口要改,`policy=` 参数保留做兼容 |

## 3. 指令从哪里来

### 3.1 默认配置的 interact 段(D3)

`training_defaults.yaml` 增加可选的 `interact` 键,形状如下;缺它时 `new` 加 `interact` 仍判违规,与今天一致。

```yaml
interact:
  trained_slot: a
  slots:
    - {slot_id: a, model_backend: self_play}
    - {slot_id: b, model_backend: frozen_reference, model_ref: zengen-v1}
  max_turns_per_episode: 4
  data_spec: {scenario_count: 2, seed: 0, purpose: training, stages: {sft: {group_size: 1}}}
```

`resolve_action` 对 `new` 加 `interact`:取 batch 的训练配置为底,覆盖 `mode`、`trained_slot`、`max_turns_per_episode`,`bindings` 留空,`scenario_ids` 留空,`data_spec` 取 interact 段的。人格 id 与情境 id 都由合成器填(3.2)。`allow_interact` 关时照旧拒绝。

### 3.2 合成器补情境与人格(D2)

`TemplateDataProducer.produce` 对 `mode == "interact"`:

1. 按 `data_spec` 生成 `scenario_count` 个情境与每个角色位一个人格,写进树(`write_scenario`、`write_persona`)。
2. 合成 SFT 冷启动的分组数据集与清单,与今天相同。
3. 把情境 id 与 `bindings`(含人格 id)写回指令的 `training_config`,指令仍是 `pending`。

第 3 步改了指令,是新行为:今天派发器不改指令载荷。备选是让合成器只写情境,派发器在内存里拼配置、不回写;那样指令与实际训练的配置不一致,`config_hash` 也对不上。本稿取回写,回写只发生在 `scenario_ids` 为空时,一次写成,之后不变。

全部写入都是确定性的:同一 `(run, data_spec)` 写出逐字节相同的文件,崩溃后重跑由 GitStore 判为空操作。

### 3.3 规划

`HeuristicBackend` 不变,仍只 clone batch 配方(TP-14)。要规划出 interact,用桥接后端,或由人写指令。interact 配方的 clone 路径已经存在,不改。

## 4. 实时环境与落盘(D4)

`AgentRuntime` 增加构造参数 `on_session_closed`。训练装配时传入一个落盘函数:`write_episode(store, run_id, episode)`。实时会话的 episode id 是服务端起的 `ep-sess-<随机>`(`src/simulator/realtime/servicer.py:164,294`),从它取不到 run,所以由轨迹源在开会话前登记「这个会话属于哪个 run」,服务端按会话查表落盘。备选是让轨迹源自己落盘(它在会话结束时拿得到策略视角的 episode),但那一份缺对手的内心独白,不是权威版本(`trajectory_source.py:144-148`)。

落盘失败时服务端报 `persistence_failed`,轨迹源整条重跑(第 1 节第 6 条)。

## 5. 训练位

### 5.1 策略工厂(D6)

`make_mindtrainer_dispatcher` 增加 `policy_factory: Callable[[str, BigRef], ModelBackend] | None`。派发 interact 指令时,用 run id 与指令的 `base_checkpoint_ref` 构造一次策略。`resolve_action` 算出的 `base_model_id` 不进指令载荷,所以工厂拿不到它;需要时由引用反查模型产物。只给了旧的 `policy=` 时行为不变。

### 5.2 第一步的默认策略

确定性替身:训练位按 rollout 序号交替说出奖励规则的命中词与中性话,与数据合成器的 `keyword_alternating_rollouts` 同一手法,保证每组奖励一高一低,不退化。它不看基座,只证明编排、落盘、奖励与可追溯。候选与训练记录照常写,诊断报告仍是 `mock`。

### 5.3 第二步:真实推理

策略工厂换成 vLLM 客户端:

1. 派发 interact 指令时,经 `compute.service.serve` 起服务,挂本轮基座;服务覆盖整个 `grpo_opd` 阶段,阶段结束即停,放租约。
2. 每轮把人格、情境与对话历史拼成提示,调 `/v1/chat/completions`,取回一句话。
3. 单次调用超时要小于实时服务端的 30 秒提交上限;超时按瞬态处理,整条 rollout 重跑。

第二步有三个前置条件,缺任一项就做不成:

- 执行器能跑 GRPO。今天只有 mock 能跑,而 mock 训练配真推理时,推理结果只进奖励,不改权重;这一组合本稿不支持,装配期就拒绝。
- 本轮基座能被 vLLM 加载。今天第二轮起的基座是适配器导出,`RollExecutor` 不接受它作训练起点(第 1 节第 4 条);vLLM 能挂它,但训练侧不能从它接着训。
- GPU 租约。服务占着租约时训练作业起不来;GRPO 与 rollout 要么同一作业里交替,要么同机分卡。这是第二步的核心设计问题,本稿只列出,不裁。

所以第二步在本稿里只定接口(5.1)与超时规则,不定实现。它与真实 GRPO 一起设计。

## 6. 装配(D5)

`factory run|reconcile` 增加 `--interact`。打开时:

- 读 `training_defaults.yaml` 的 interact 段与 `scheduler.yaml` 的 `allow_interact`,两者都要在,否则退出码 2,指出缺哪一项。
- 构造 `AgentRuntime`(带训练落盘钩子)与策略工厂,注入派发器;`AgentRuntime` 若已因 `--enable agent` 构造,复用同一个。
- 不允许与 `--executor roll` 同用(真实执行器不支持 GRPO,第 1 节第 4 条)。

不打开时,interact 指令照旧判 `failed`,原因改成点名 `--interact`。

## 7. 失败与复现

| 失败点 | 类 | 处理 |
|---|---|---|
| `--interact` 打开但缺 interact 段或 `allow_interact` | 配置错误 | 装配期退出码 2 |
| 合成情境或人格失败 | T2 | 指令 `failed`,不留半套情境 |
| 回写 `training_config` 失败 | T1 | 上抛,指令仍 `pending`,重跑时内容相同不提交 |
| 落盘钩子失败 | T1 | `persistence_failed`,整条重跑 |
| 实时会话 `timeout`、`role_backend_timeout`、传输不可用 | T1 | 整条重跑,有上限(已落地) |
| 第二步:起服务失败、租约被占 | 延后 | `ServeStartupError`、`ComputeBusyError` 走延后通道(已落地) |

复现性与今天的 interact 相同:`config_hash` 覆盖情境、角色位与对手 `model_ref`;GRPO 阶段没有清单,不声称续训等价。D2 让情境与人格进了树,复现时至少知道当时用的是哪些情境。

## 8. 测试策略

| 层次 | 对象 | 替身 |
|---|---|---|
| 单元 | `resolve_action` 对 `new` 加 `interact`、默认配置 interact 段的严格解析 | 无 |
| 单元 | 合成器写情境与人格、回写指令、重跑逐字节相同 | 临时仓库 |
| 集成 | `AgentRuntime` 带训练落盘钩子,会话结束 episode 进 run 目录;钩子失败时整条重跑 | loopback 服务端 |
| 端到端 | `factory reconcile --once --interact`:人写或规划出的 interact 指令走到候选 `pending_review`,episode 都在树里 | loopback 服务端、mock 执行器、确定性策略 |
| 装配 | `--interact` 缺配置、与 `--executor roll` 同用时退出码 2 | 无 |

不依赖 GPU、网络与真实模型。第二步的测试另行设计。

## 9. 实施顺序

第一步(本稿可施工):

1. 默认配置 interact 段与 `resolve_action`(3.1)。
2. 合成器补情境、人格并回写指令(3.2)。
3. `AgentRuntime` 的训练落盘钩子与 run 登记(4)。
4. 策略工厂接口与确定性替身(5.1、5.2)。
5. `--interact` 装配与端到端(6、8)。
6. 并入 `docs/design/` 五篇,场景二的「今天走得到哪」改写。

第二步(待与真实 GRPO 一起设计):vLLM 策略工厂、租约与训练作业的共存、适配器作训练起点。

## 10. 待你确认

1. **D1 的拆分。** 你选了「这一轮就接真推理」,但第 1 节第 4 条说明真实执行器开不了 interact 会话,mock 执行器配真推理又换不来学习。本稿据此把真推理推到第二步,与真实 GRPO 绑在一起。如果你的本意是「哪怕训练是 mock,也要先看到真模型在实时会话里说话」,那可以在第一步之后单独加一个只做推理、不训练的验证入口,工作量小于第二步。请选一种。
2. **3.2 回写指令。** 合成器回写 `training_config.scenario_ids` 与 `bindings` 是新行为,派发器今天不改指令载荷。可接受吗?
3. **D5 的 `--interact` 开关。** 也可以并进 `--enable`(比如 `--enable interact`),但 `--enable` 现在只管调和动作,不管派发器装配。倾向单独开关。
