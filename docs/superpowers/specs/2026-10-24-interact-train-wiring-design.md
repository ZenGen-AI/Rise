# 场景二接通设计:实时交互式训练在真实入口跑通

状态:草案第三版(按拆分重写),待审阅。日期:2026-10-25。
前置设计:`2026-10-03-mindtrainer-design.md`(InteractTrain 轨迹源)、`2026-10-09-closed-loop-integration-design.md`(闭环接线)、`2026-10-15-real-backends-design.md`(远端计算层、vLLM 服务)。
配套设计:`2026-10-25-actio-starling-memory-design.md`(智能体协议与记忆选型)。本稿不依赖它,两者可以各自独立落地。
权威文档集是 `docs/design/`。本稿审定后先并入其中的总览、SocialSimulator、MindTrainer、TrainScheduler、Factory 五篇,再写施工计划。

## 0. 这份文档做什么

场景二在测试里跑通,真实入口跑不通。`factory reconcile --once` 派发一条 interact 指令,立即判 `failed`(T2),原因是 `interact mode needs a client_factory and a policy`。本稿让场景二从真实入口走完:调度器写出 interact 指令,SocialSimulator 备好情境与人格、提供实时环境,训练位由加载了本轮基座的真实模型说话,MindTrainer 收集 rollout、算奖励、更新,产出候选。

三件已经定下的事:

1. 情境与人格两路来源:人工在配置里点名树里已有的,或由模拟器按种子生成。两路不能混用。
2. 训练位这一轮就接真实推理:经远端计算层在 vLLM 上服务本轮基座。
3. 对手位三种驱动:固定对话、站内 vLLM 的对手模型、外部 OpenAI 兼容 API。默认固定对话。

第 2 条与现有约束冲突:真实执行器只支持 SFT,开不了 interact 的实时会话。所以本轮是「真推理 + mock 训练」,证明的是真模型能进会话、发言能落盘、奖励管线对真实发言成立,不是模型因此变好。真实 GRPO 落地后再把执行器换掉,接入工作可原样复用。

不做:真实 GRPO/OPD 训练、真实 judge 归因、并发 rollout、对手 `model_ref` 的哈希锚定、异步派发。

范围之外,留给配套设计:抽公共数据模型、把智能体换成可扩展协议、把记忆换成 Starling 并让记忆指纹进 `config_hash`。本稿的角色位一律用模拟器现有的后端协议(`ModelBackend`:话语、内心独白),不引入智能体实例。

## 1. 事实依据

逐条读过代码,附位置。

1. 实时环境服务已落地。`factory.agent.AgentRuntime` 在进程内起 loopback 服务端,配 `scenario_resolver` 从树里读情境,不配 `on_session_closed`;它的说明已写明两个驱动方是 MindAgent 部署与 MindTrainer 轨迹源(`src/factory/agent.py:100-160`)。`EnvironmentServicer` 把每个角色后端包进 `TimeoutBackend`,单次调用超时归 `role_backend_timeout`(`src/simulator/realtime/servicer.py:287`)。
2. 训练侧接口已在。`make_mindtrainer_dispatcher(client_factory=..., policy=...)` 两样都注入时,`grpo_opd` 走 `InteractTrajectorySource`(`src/mindtrainer/dispatch.py:153-219`)。轨迹源不写 episode,依赖服务端 `on_session_closed` 落盘,只在 metadata 里记 id(`src/mindtrainer/trajectory_source.py:129-131`)。`policy` 是 `simulator.backends.ModelBackend`,一次派发只构造一个,与 run 无关。
3. 调度侧写不出 interact 指令:`validate_decision` 对 `new` 加 `interact` 一律违规(`src/trainscheduler/resolve.py:86-95`),`resolve_action` 对 `new` 非 batch 抛错(`:138-142`);默认训练配置没有 `scenario_ids` 与 `bindings`(`src/trainscheduler/config.py:185-222`);`allow_interact` 默认关(`:63`)。
4. 真实执行器只支持 SFT:`RollExecutor.supported_phases = {"sft"}`(`src/mindtrainer/roll_executor.py:87`),编排器在训练前检查,配了 `grpo_steps > 0` 就写 `failed`(`src/mindtrainer/orchestrator.py:187-197`)。`factory` 里 `--executor roll` 不能与 `--enable` 同用(`src/factory/wire.py:234-241`)。
5. vLLM 服务经 `compute.service.serve` 起停:取整机 GPU 租约(`gpu.lease`,一个持有者)、起作业、开 SSH 隧道、等就绪,退出时停服务放租约(`src/compute/service.py:60-111`)。租约被占抛 `ComputeBusyError`,是 `DeferredError`(`src/compute/errors.py:20`)。服务命令目前只能挂**一个** LoRA(`--max-loras 1 --lora-modules <name>=<path>`,`src/compute/vllm_service.py:65-81`)。
6. `RollExecutor.load` 只接受指向 `base_model_path` 的起点基座或训练存点,不接受适配器导出(`src/mindtrainer/roll_executor.py:104-126`)。vLLM 能挂适配器,训练侧不能从适配器接着训,这也是本轮只做单轮真推理的原因之一。
7. 实时服务端的时间上限:客户端每轮 30 秒内提交,创建会话 10 秒内发,后端单次调用 60 秒(`src/simulator/realtime/server.py:17-23`)。`AgentRuntime` 给整条 gRPC 流 15 秒 deadline、轮次超时 5 秒(`src/factory/agent.py:87,108`),这两个值是按罐头后端定的。
8. `run_episode` 每轮按 `binding.slot_id` 取后端、抹掉别人角色位的内心独白与轨迹,后端返回二元组或三元组都能解包(`src/simulator/runner.py:60-110`)。
9. `TrainingRun.from_dict` 用 `d.get(...)` 读可选字段,新增字段对旧产物兼容(`src/mindtrainer/datamodel.py:119-134`)。
10. `Scenario.from_dict` 用 `d["..."]` 读必填、`d.get("source", "auto")` 读可选(`src/simulator/datamodel.py:74-86`),新增可选字段不会让旧产物读不出。
11. `TrainingConfig.from_dict` 对未知键宽容(只读它认识的键),所以给 `training_config` 加字段不必迁移旧指令(`src/mindtrainer/training_config.py:134-160`)。

## 2. 裁决总表

| 编号 | 裁决 | 备选 | 理由 | 判错的代价 |
|---|---|---|---|---|
| D1 | 本轮「真推理 + mock 训练」;执行器换成真实 GRPO 单独做 | 只做替身;只做推理验证入口 | 你要的是这一轮就接真推理;真实执行器开不了 interact 会话,只能配 mock | 本轮证明不了学习效果;真 GRPO 落地时要改装配开关 |
| D2 | 情境与人格两路:配置点名或按种子生成,**不能混用** | 允许点名一部分、补齐其余 | 混用会产出无法追溯的混合语料 | 使用者要多写几行配置 |
| D3 | 点名的情境 `purpose` 必须是 `training` | 不限 | `install-defaults` 写入的三个是 `deployment_simulation`,拿它们训练等于污染 | 部署素材不能顺手拿来训练 |
| D4 | 对手位三驱动:固定对话(默认)、站内 vLLM、外部 OpenAI 兼容 API | 只做一种 | 你要求三种都支持 | 三种失败分类与超时口径都要各自成立 |
| D5 | 固定对话优先用情境自带剧本,情境没带时用全局台词 | 只有全局台词 | 你要固定对话,且要能按情境区分 | `Scenario` 加一个可选字段 |
| D6 | 训练位与站内对手**共用一个** vLLM 进程,最多两个 LoRA | 两个进程 | GPU 租约是整机的,一个持有者 | 服务命令与 `VllmServeConfig` 要从单适配器改成列表 |
| D7 | vLLM 按指令起停,覆盖整个 `grpo_opd` 阶段 | 常驻 | 租约长期被占会让真实评测与训练作业都起不来 | 每轮多付一次启动开销(上限 900 秒) |
| D8 | 默认替身策略,`--interact` 才接真推理 | 默认真推理 | 默认跑测试不碰远端、不占卡 | 打开时要在装配期确认配置齐全 |
| D9 | 加 `--interact` 作为装配开关,与 `--executor roll` 互斥 | 并进 `--enable` | `--enable` 管调和动作,不管派发器装配 | 漏开时报错要指明是哪个开关 |
| D10 | 训练记录新增 `policy_backend` 块,记录两位实际用的是什么 | 只靠日志 | 「谁在说话」决定这一轮的数字怎么解读 | 记录多一个块;旧记录读回时缺省为空 |
| D11 | 派发器新增 `policy_factory`,按 run 现构造策略 | 沿用一次派发一个 `policy` | 训练位必须用本轮基座,固定策略做不到 | 派发器接口多一个参数,旧参数保留兼容 |

## 3. 情境与人格(D2、D3)

`training_defaults.yaml` 增加可选 `interact` 段:

```yaml
interact:
  trained_slot: a
  slots:
    - {slot_id: a, model_backend: self_play}
    - {slot_id: b, model_backend: frozen_reference, model_ref: zengen-v1}
  max_turns_per_episode: 4
  # 两选一,不能同时写
  scenario_ids: [scen-auto-0, scen-auto-1]      # 人工点名
  persona_ids: {a: pers-auto-0, b: pers-auto-1}  # 与上面必须成对出现
  data_spec: {scenario_count: 2, seed: 0, purpose: training, stages: {sft: {group_size: 1}}}
```

- 点了 `scenario_ids` 就必须点 `persona_ids`,反之亦然;只写一半是配置错误。
- 都不写时按 `data_spec` 的 `seed` 生成,`scenario_count` 个情境、每个角色位一个人格,写进树。
- 点名的情境 `purpose` 必须是 `training`(D3);人格不做此限制。
- 缺整个 `interact` 段时,`new` 加 `interact` 仍判违规,与今天一致。

派发时合成器把最终的情境 id 与 `bindings`(含人格 id)回写指令的 `training_config`,只在 `scenario_ids` 为空时写一次。此后 `config_hash` 覆盖它们,续训与复现都按这一份。合成器今天已经会为带 `data_spec` 的指令补清单,回写只是多写一个字段。

## 4. 对手位三种驱动(D4、D5)

| `model_backend` | 驱动 | 来源 |
|---|---|---|
| `frozen_reference` | 站内 vLLM 上挂对手模型的 LoRA | `model_ref` 经树里的模型产物解析成 `checkpoint_ref` |
| `external_api` | OpenAI 兼容 HTTP | 新增 `opponent` 配置:端点、模型名、凭证的环境变量名 |
| 固定对话 | `ScriptedBackend` | 情境自带剧本优先,没带时用全局台词 |

- **情境自带剧本**:`Scenario` 增加可选字段 `opponent_script`,形如 `{b: ["台词一", "台词二"]}`,按角色位给一串台词,像 `ScriptedBackend` 一样按序消耗;台词用完仍在说话时循环。`to_dict` 只在非空时写这个键,`from_dict` 用 `d.get` 读,旧产物与既有哈希不变。默认配置不写它,所以默认仍用全局台词。
- **`model_ref` 解析**:全仓库现在没有一处把 `model_ref` 解析成权重,本设计新增。解析不到、或指到的不是适配器导出,派发时确定性失败,不静默退回脚本台词。
- **外部 API**:仓库现在没有 OpenAI 兼容客户端,新增一个极小的(`urllib` 即可,与 `src/compute/tunnel.py:35-48` 的 HTTP 探测同一手法)。凭证只从环境变量读,异常信息只报变量名(与 `factory.bridges` 同一口径)。超时由服务端的 `TimeoutBackend` 兜住,单次调用超时归 `role_backend_timeout`,按瞬态整条重跑。

三类驱动都在 `AgentRuntime._backends` 里按角色位选,实现在组合根;模拟器本身仍只认后端协议。

## 5. 训练位与 vLLM(D1、D6、D7、D11)

### 5.1 策略工厂(D11)

`make_mindtrainer_dispatcher` 增加 `policy_factory: Callable[[str, BigRef], ModelBackend] | None`。派发 interact 指令时用 run id 与指令的 `base_checkpoint_ref` 构造策略。`resolve_action` 算出的 `base_model_id` 不进指令载荷,所以工厂拿不到它;需要时由引用反查模型产物。只给了旧的 `policy=` 时行为不变。

### 5.2 一个服务、最多两个 LoRA(D6)

训练位与站内对手共用一个 vLLM 进程:基座来自端点,`--max-loras 2`,`--lora-modules` 按实际挂的适配器生成。`VllmServeConfig` 从单适配器改成适配器列表;适配器列表为空时不加 `--enable-lora`。

- 训练位:`base_checkpoint_ref` 是 `roll_ssh` 适配器导出就挂它,否则用基座。
- 站内对手:`model_ref` 解析出的引用,同样要求是适配器导出。
- 闭环里的候选是 `mock://` 引用,所以闭环里训练位永远退回基座。这个事实写进记录(D10)。

### 5.3 生命周期(D7)

一条 interact 指令派发时起服务,整轮训练在服务句柄的上下文里跑,退出即停、放租约。租约被占抛 `ComputeBusyError`,走延后通道;起服务失败抛 `ServeStartupError`,同样是 `DeferredError`。

### 5.4 超时

- 单次角色后端调用:默认 15 秒(现为 60 秒,按罐头后端定得偏松)。
- 客户端提交一轮的上限:必须大于"最慢一方一次调用 + 余量",默认保持 30 秒。
- gRPC 流 deadline:按 `max_turns × 单轮上限` 算,不再是固定 15 秒。
- 真机延迟未知,30 秒够不够要真机测,本稿标未核实。

## 6. 装配(D8、D9)

`factory run|reconcile` 增加 `--interact`:

- 打开时读 `training_defaults.yaml` 的 `interact` 段与 `scheduler.yaml` 的 `allow_interact`,缺任一项退出码 2 并指出缺哪个;
- 构造带落盘钩子的 `AgentRuntime` 与策略工厂,注入派发器;若已因 `--enable agent` 构造了运行时,复用同一个;
- 与 `--executor roll` 同用直接拒绝(真实执行器开不了 interact 会话)。

不打开时 interact 指令照旧判 `failed`,原因改成点名 `--interact`。

## 7. 如实记录(D10)

训练记录新增 `policy_backend` 块:

```yaml
policy_backend:
  trained: {ref: "file:///compute/checkpoints/initial-base.safetensors", kind: "base", endpoint: local-h800}
  opponents:
    b: {driver: "scripted", source: "scenario"}
  real_inference_with_mock_training: true
```

`kind` 取 `base` 或 `adapter`;`driver` 取 `scripted`、`vllm`、`external_api`。读记录的人据此判断这一轮的数字该怎么解读。

## 8. 失败与复现

| 失败点 | 类 | 处理 |
|---|---|---|
| `--interact` 打开但缺 `interact` 段或缺 `allow_interact` | 配置错误 | 装配期退出码 2 |
| 点名的情境 `purpose` 不是 `training` | 配置错误 | 装配期退出码 2 |
| 点名的情境与人格只写了一半 | 配置错误 | 装配期退出码 2 |
| 点名的情境或人格不在树里 | 配置错误 | 装配期退出码 2 |
| 对手 `model_ref` 解析不到,或不是适配器导出 | T2 | 指令 `failed`,原因点名引用 |
| 合成情境或人格失败 | T2 | 指令 `failed`,不留半套 |
| 回写 `training_config` 失败 | T1 | 上抛,指令仍 `pending`,重跑内容相同不提交 |
| 起服务失败、租约被占 | 延后 | `DeferredError`,走退避通道 |
| 单次角色后端调用超时 | T1 | `role_backend_timeout`,整条重跑,有上限 |
| 落盘钩子失败 | T1 | `persistence_failed`,整条重跑 |

复现性:interact 的 `grpo_opd` 没有清单与数据哈希,不声称续训等价;`config_hash` 覆盖情境、人格、角色位与 `model_ref`,所以配置层面可复现。

## 9. 测试策略

| 层次 | 对象 | 替身 |
|---|---|---|
| 单元 | `interact` 段与 `opponent` 段的严格解析;两路来源互斥;`purpose` 校验 | 无 |
| 单元 | 合成器生成情境与人格、回写指令、重跑逐字节相同 | 临时仓库 |
| 单元 | `Scenario.opponent_script` 可选字段的读写兼容 | 无 |
| 单元 | `model_ref` 解析:成品、不是适配器、不存在 | 临时仓库 |
| 集成 | `AgentRuntime` 落盘钩子;三类对手驱动各一条 | loopback 服务端 |
| 集成 | 外部 API 对手的 HTTP 客户端:成功、超时、非 200、缺凭证只报变量名 | 本地 HTTP 替身 |
| 端到端 | `factory reconcile --once --interact` 走到候选 `pending_review`,episode 都在树里 | 假 vLLM(本地 HTTP 回固定文本)、mock 执行器 |
| 装配 | `--interact` 的六条拒绝项各一条 | 无 |

不依赖 GPU、真实网络与真实模型。真机验证单列,标未核实。

## 10. 实施顺序

1. 数据模型:`Scenario.opponent_script`。
2. 配置与校验:`interact` 段、`opponent` 段、两路互斥、`purpose` 校验。
3. 指令链:`resolve_action` 支持 `new interact`、合成器生成与回写。
4. 对手三驱动与 `model_ref` 解析。
5. vLLM:多适配器、策略工厂、按指令起停。
6. 超时与 `policy_backend` 记录。
7. `--interact` 装配与端到端。
8. 并入 `docs/design/` 五篇,改场景二的「今天走得到哪」。

## 11. 未核实

- 真模型单轮推理延迟,决定 30 秒提交上限够不够。要真机测。
- 一个 vLLM 进程挂两个 LoRA 时,切换的开销与显存余量。按 `--max-loras 2` 设计,真机确认。
- `VllmServeConfig` 从单适配器改成列表后,现有 `SomevalRunner` 的调用点能否原样工作。
- 外部 API 对手的速率上限与成本,按实际供应商测。
