# MindTrainer:把一条训练指令跑成一个待审的候选 checkpoint

| 项 | 内容 |
|---|---|
| 一句话 | 权重级训练子系统:读训练指令与数据集清单,跑 SFT 与 GRPO 加 OPD,产出候选 checkpoint,崩溃后从存点续训 |
| 状态 | 部分落地:配置、两种轨迹源、奖励管线、分组、mock 执行器、续训、编排、派发器 [已落地];ROLL LoRA SFT 真实执行器与适配器导出转换器 [已落地],但还没有入口装配、也没在真实主机上跑过;数据合成接线 [计划中];真实 GRPO 加 OPD 训练 [推迟] |
| 所在层 | 计算层 |
| 代码位置 | `src/mindtrainer`;`src/mindtrainer/data_spec.py`、`src/mindtrainer/data_producer.py` [计划中] |
| 基线提交 | `8dd295b`(2026-10-10) |
| 依赖 | ControlPlane(`controlplane` 的信封、GitStore、路径、状态机、派发缝);SocialSimulator(`simulator` 的数据模型、数据集清单、实时环境服务客户端);真实执行器另依赖远端计算层(`compute`) |
| 被依赖 | ControlPlane 调和循环经注入的派发器调用它;`scripts/run_controlplane.py`;factory 与 TrainScheduler [计划中] |
| 对应实施计划 | 工作包 2(任务 30 至 34)[计划中] |

## 0. 系统速览

<!-- shared:begin context-block -->
**系统是什么。** RismForge 是一套"AI 驱动的社会心智后训练工厂":输入通用开源基础模型,输出社会心智能力显著增强的模型,代号 **ZenGen**。工厂自己决定每一轮练什么、怎么练。训练决策可以交给外部更强的闭源模型(经命令行调用),关键决策点设人工审核门,其余环节自动。

**七个子系统。** 六个能力子系统加一个协调层 ControlPlane。

| 子系统 | 职责 | 所在层 | 现状 |
|---|---|---|---|
| ControlPlane | 协调层:git 版本化产物树、状态机、调和循环、三个审核门 | 控制层 | 核心 [已落地],闭环扩展 [计划中] |
| SocialSimulator | 情境与人格生成、多智能体仿真、实时环境服务、诊断评测 | 计算层 | [已落地],诊断用 mock 后端;vLLM 加 SoMEval 的真实运行器 [计划中] |
| MindTrainer | 权重级训练:BatchTrain(SFT 后接 GRPO 加 OPD)与 InteractTrain | 计算层 | [已落地],入口装配的训练执行器仍是 mock;ROLL LoRA SFT 真实执行器 [已落地],未装配、未在真实主机上验证 |
| MindAgent | 部署态智能体的技能与记忆自进化,不改权重 | 计算层 | [计划中] |
| RecipeStore | 验证有效的训练配方的版本化仓库 | 控制层 | [计划中] |
| TrainScheduler | 决策执行器:装配上下文、校验决策、构造训练指令、提名配方 | 控制层 | [计划中] |
| TrainPlanner | 决策学习者:经可插拔后端给出下一轮训练决策 | 控制层 | [计划中] |

共享基础设施有五个。**Actio** 是带技能库与显式记忆的智能体框架,每次响应附带激活轨迹。**外部推理桥接**用命令行调用外部强模型。**远端计算层** `compute` 经 SSH 在远端 GPU 主机上跑作业 [已落地]。**ROLL** 是分布式训练引擎,**vLLM** 是推理引擎。V1 经远端计算层接入 ROLL 的 LoRA SFT [已落地](尚未装配进入口,也未在真实主机上验证)与 vLLM 推理服务 [计划中]。已落地部分仍由确定性 mock 顶替。真实 GRPO 加 OPD 训练与真实 judge [推迟]。

**状态标记。** **[已落地]** 表示代码在主干上且有测试。**[计划中]** 表示设计与实施计划已定,代码还没写。**[推迟]** 表示明确不在 V1 范围内。图只画目标系统,不标进展,进展只看正文的状态标记。

**六条贯穿全系统的机制。**

| 机制 | 规则 |
|---|---|
| 产物树 | 所有协作数据是 git 树里的 YAML 产物,统一信封:`id`、`type`、`version`、`created_at`、`created_by`(含 `owner` 与 `visibility`)、`status`、`schema_version`、`links`、`payload`。checkpoint 等大体量内容只存引用 `{uri, sha256, size_bytes}`;V1 的数据集逐条 episode 进 git,只适合小规模 mock。git 树是唯一真相,读取时先经迁移器链再消费。 |
| 状态机 | 需审核类:`draft → pending_review → approved → effective`,之后可到 `deprecated` 或 `superseded`,`pending_review` 也可到 `rejected`。非审核类:`pending → executed → consumed`,`pending` 也可到 `failed`。被拒绝与失败的产物保留;被拒绝的候选进入下一轮规划的输入,失败只留痕、等人处理。 |
| 调和循环 | 纯函数 `reconcile(树视图) → 动作列表`,守护进程按间隔轮询并执行动作。循环无状态、动作幂等,进程崩溃后从 git 树续接。子系统之间不直接调用,只读写产物。 |
| 审核门 | 三道人工门:① checkpoint 晋级,② 技能与记忆增量纳入,③ 配方入库。证据不在场就不能批准:门①要诊断报告,门②要验证结果,门③要可复现性校验报告。门②与门③ [计划中]。 |
| 受限直连 | 两处高频实时调用绕过 ControlPlane:① MindTrainer 与 SocialSimulator 之间的 InteractTrain rollout,② MindAgent 与 SocialSimulator 之间的部署经验采集。两处都走 gRPC 双向流,发生在计算层内部。是否开会话仍由 ControlPlane 决策。 |
| 失败分类 | T1 瞬态:有限次指数退避重试,耗尽升为 T3。T2 确定性:不重放,按所属子系统的校验链处理,LLM 的随机输出可有限次重采样。T3 需人工:写失败记录,不自动重跑。失败是数据,不是异常。 |

**核心术语。**

| 术语 | 含义 |
|---|---|
| Run | 一轮迭代,对应产物树里的 `runs/run-<编号>/` 目录,该轮所有产物都挂在其下 |
| Directive(训练指令) | TrainScheduler 或人写入的 `directive.yaml`,调和循环据此下发训练 |
| Candidate(候选 checkpoint) | MindTrainer 产出的 `candidate_checkpoint.yaml`,经诊断评测与门①才能晋级 |
| ZenGen 版本 | 晋级后写入 `models/zengen-v<N>.yaml`;下一轮训练的基座取最新的 effective 版本 |
| Episode | 一次多轮交互的完整记录:情境、角色分配、逐轮话语、终止原因 |
| Scenario、Persona、RoleAssignment | 社会情境、人格模板,以及把角色位绑定到具体人格与模型后端的分配 |
| Recipe(配方) | 验证有效的完整训练方案,分 batch、interact、agent_rsi 三类 |
| DiagnosticReport | 诊断评测产出的报告:维度、分数、置信区间、错误类型、退化标记 |
| Activation trace(激活轨迹) | Actio 每次响应附带的记录:命中的技能候选、选中的技能、命中的记忆 |
| Evidence grade(证据等级) | 诊断报告的可信等级 `stub`、`mock`、`real`,决定它能否驱动规划与提名 [计划中] |
<!-- shared:end context-block -->

## 1. 定位

MindTrainer 把一条 `pending` 的训练指令变成一个 `pending_review` 的候选 checkpoint,并留下可续训的训练记录。它解决两件事:训练配置与数据被哈希锚定,训练可复现;训练中途崩溃不丢进度,也不无限重跑。

它不决定练什么,也不评价练得好不好。练什么由指令作者决定(人工,或 TrainScheduler [计划中]),好不好由诊断评测与审核门①判断。

<!-- figure:begin 30-mindtrainer-context -->
![MindTrainer 的上下文](diagrams/30-mindtrainer-context.svg)

**图 1　MindTrainer 的上下文**　调和循环把 pending 的训练指令交给 MindTrainer 派发器。MindTrainer 从产物树读数据集清单,InteractTrain 经受限直连①向实时环境服务要 rollout,训练由执行器协议完成,结果以训练记录与候选 checkpoint 写回产物树。

<details>
<summary>文字版:图中的节点与连线</summary>

**节点**

- 指令作者:人工写入 directive / 或由 TrainScheduler 写入
- 产物树 control-plane/:directive · dataset_manifest / training_run · candidate_checkpoint
- ControlPlane 调和循环:dispatch_directive 动作 / 可注入的派发器 Dispatcher
- 批量数据集:SocialSimulator 生产 / 清单带 sha256
- MindTrainer:派发器 · 编排器 / 轨迹源 · 奖励 · 执行器
- 实时环境服务:SocialSimulator · gRPC 双向流 / 一条流一次会话
- ROLL · vLLM:训练执行器的后端 / 分布式训练 · 推理引擎
- DataProducer:缺清单时先合成数据 / 由组合根注入

**连线**

- 指令作者 → 产物树 control-plane/:写 directive
- ControlPlane 调和循环 ↔ 产物树 control-plane/:扫描 / 回写状态
- ControlPlane 调和循环 → MindTrainer:派发 pending 指令
- MindTrainer ↔ 产物树 control-plane/:读清单 · 写记录与候选
- 批量数据集 → 产物树 control-plane/:写清单与 episode
- MindTrainer ↔ 实时环境服务:① rollout 会话
- MindTrainer → ROLL · vLLM:TrainingExecutor 协议
- MindTrainer → DataProducer:produce

</details>
<!-- figure:end 30-mindtrainer-context -->

调和循环发现 `pending` 指令,调用注入的 MindTrainer 派发器。MindTrainer 从产物树读数据集清单与 episode,InteractTrain 经受限直连接口①向实时环境服务要 rollout。训练由 `TrainingExecutor` 协议完成,今天是 mock;ROLL 的 LoRA SFT 真实执行器经远端计算层在远端 GPU 主机上跑 [计划中]。结果以训练记录与候选 checkpoint 写回产物树,之后的审核与晋级不归 MindTrainer。

## 2. 目标、非目标与约束

目标是"编排正确、失败可分类、续训可证明",不是"训练效果好"。

| 编号 | 目标 | 验收方式 |
|---|---|---|
| G1 | batch 与 interact 两种模式共用同一条下游流水线 | 两种轨迹源都产出 `TrajectoryBatch`,奖励、分组、执行器、checkpoint 代码无模式分支 |
| G2 | 配置与数据可锚定 | 记录与候选都带 `config_hash`;数据集清单逐条校验 sha256 |
| G3 | 崩溃后从存点续训,batch 下与一次跑完逐字节等价 | `test_mt_orchestrator.py` 在 6 个 step 各注入一次崩溃,终态哈希相同 |
| G4 | 失败分类明确,确定性失败不重放 | 每类失败点有测试钉住 `failed` 或 `pending` |
| G5 | 续训有上限,不会无限循环 | `resume_attempts` 触顶后升级为 T3。资源暂时不可用走延后通道,预检在 `run_training` 之前,`ComputeBusyError` 不耗 `resume_attempts`;通道自己有连续延后上限(CP-14),G5 对真正的失败仍成立 |
| G6 | 候选能走完审核门①并成为下一轮的基座 | `test_mt_integration.py` 两个用例。只对 mock 成立:真实后端 [计划中] 的候选是适配器导出,不是训练起点,第二轮由真实执行器拒绝(见已知局限) |
| G7 | 带 `data_spec` 的指令缺清单时能自补数据并训练成候选 [计划中] | 派发器合成分支的测试:合成后训练、缺生产器判 `failed`、合成中途崩溃重入收敛 |

| 非目标或推迟项 | 状态 | V1 的处理 |
|---|---|---|
| ROLL LoRA SFT 真实执行器与 vLLM LoRA 适配器导出 | [已落地] | 代码与测试在主干上,但入口仍装配 `MockExecutor` 与 `MockConverter`;真实执行器只声明 `supported_phases = {"sft"}` |
| 真实 GRPO 加 OPD 训练、把适配器合并回完整 HF 权重 | [推迟] | `grpo_steps > 0` 与真实执行器混用在配置校验期失败 |
| 异步派发 | [推迟] | 派发器同步返回;接真实 LoRA SFT 的这一轮作为有限定的例外接受同步阻塞(CP-15),异步派发留给下一轮 |
| 缺清单时先合成数据 | [计划中] | 缺清单的指令判 `failed`,原因写明缺哪份清单 |
| judge 归因 | 代码已有,入口不提供 judge | judge 归因的指令在派发器里判 `failed` |
| interact 的实时服务端 | 入口不启动 | `scripts/run_controlplane.py` 下 interact 指令判 `failed`;注入 `client_factory` 与 `policy` 的调用方可以跑 |
| 成本记账、完整失败记录产物 | [计划中] | 由工作包 1 的统一写入函数负责,MindTrainer 现在不写 |
| interact 的并行 rollout、真实策略模型 | [推迟] | 顺序 rollout,策略由调用方注入 |
| 读诊断结果调整训练 | 不做 | MindTrainer 只记录 OPD 系数与损失 |

| 约束 | 内容 | 影响 |
|---|---|---|
| 分层 | `mindtrainer` 可导入 `controlplane` 与 `simulator`,不导入 `diagnostic`;`controlplane` 不导入 `mindtrainer` | 派发器住在 `mindtrainer` 侧,调和循环只见到一个 Callable |
| 产物读写 | 一律经 `GitStore` 与信封;大体量内容只存 `BigRef` | checkpoint 不进 git,只进引用 |
| 时间戳 | 记录与候选的 `created_at` 固定为 `2026-10-03T00:00:00Z` | 产物是输入的纯函数,重放写入是空操作 |
| 可测性 | 不依赖 GPU 与外部服务 | 执行器、转换器、judge 都有确定性 mock |
| 凭证 | 配置里不含凭证字段 | judge 只存模型标识与 rubric 版本 |

## 3. 架构

MindTrainer 分两层:入口与编排一层决定"从哪跑、跑不跑",模式无关的流水线一层负责"每一步怎么算"。两种模式只在轨迹源处分叉。

<!-- figure:begin 30-mindtrainer-architecture -->
![MindTrainer 的组件](diagrams/30-mindtrainer-architecture.svg)

**图 2　MindTrainer 的组件**　上层是入口与编排:派发器读配置并构造轨迹源工厂,编排器读训练记录交给续训判定,再逐步驱动训练。下层是模式无关的流水线:两种轨迹源产出同一种轨迹批,之后的奖励、分组、执行器与 checkpoint 管理器完全共享。真实执行器与转换器经远端计算层在远端主机上跑 ROLL 作业。

<details>
<summary>文字版:图中的节点与连线</summary>

**节点**

- 训练配置:training_config.py / 校验 · 哈希 · phase_at
- 派发器:dispatch.py · wire.py / 指令 → 训练
- 编排器:orchestrator.py / resume_or_run · run_training
- 续训判定:resume.py / decide_resume 纯函数
- Interact 轨迹源:InteractTrajectorySource / 实时会话 · 受限直连①
- Batch 轨迹源:BatchTrajectorySource / 读清单 · 校验哈希
- 奖励管线:reward_pipeline.py / 归因 · 校验 · 组合
- 分组与优势:grouping.py / 组内均值基线
- 训练执行器:executor.py / MockExecutor
- checkpoint 管理器:checkpoint.py / 转换 · 注册候选
- 产物树:dataset_manifest · episodes · training_run · candidate_checkpoint
- 远端计算层 compute:ComputeClient · 助手脚本 · GPU 租约与预检 / 真实执行器与转换器经它在远端主机上跑 ROLL 作业

**连线**

- 派发器 → 训练配置:读取并校验
- 派发器 → 编排器:调用
- 编排器 → 续训判定:判定
- 派发器 → Interact 轨迹源:make_sources 构造轨迹源
- 派发器 → Batch 轨迹源
- 编排器 → 训练执行器:逐步驱动
- Interact 轨迹源 → 奖励管线:轨迹批
- Batch 轨迹源 → 奖励管线:轨迹批
- 奖励管线 → 分组与优势
- 分组与优势 → 训练执行器
- 训练执行器 → checkpoint 管理器
- Batch 轨迹源 → 产物树:读清单与 episode
- checkpoint 管理器 → 产物树:注册候选
- 训练执行器 → 远端计算层 compute

</details>
<!-- figure:end 30-mindtrainer-architecture -->

| 模块 | 职责 | 依赖 | 状态 |
|---|---|---|---|
| `src/mindtrainer/training_config.py` | `TrainingConfig` 与 `RewardConfig`、校验、`config_hash`、`phase_at` | `controlplane.envelope`、`simulator.datamodel` | [已落地] |
| `src/mindtrainer/datamodel.py` | `TrajectoryBatch`、`RewardedTrajectory`、`StepResult`、`TrainingRun` | `simulator.datamodel` | [已落地] |
| `src/mindtrainer/trajectory_source.py` | `BatchTrajectorySource`、`InteractTrajectorySource`、`batch_for_step` | `simulator.dataset`、实时客户端 | [已落地] |
| `src/mindtrainer/reward_pipeline.py` | 归因、校验、组合,`run_reward_pipeline` | `simulator.dataset` | [已落地] |
| `src/mindtrainer/grouping.py` | 按 `group_id` 分组、组内均值基线、退化组判定 | 无 | [已落地] |
| `src/mindtrainer/executor.py` | `TrainingExecutor` 协议与 `MockExecutor` | `grouping`、`simulator.dataset` | [已落地] |
| `src/mindtrainer/checkpoint.py` | `CheckpointConverter` 协议、`MockConverter`、`register_candidate` | `controlplane` | [已落地] |
| `src/mindtrainer/resume.py` | `decide_resume` 纯函数 | `datamodel`、`training_config` | [已落地] |
| `src/mindtrainer/store_adapter.py` | 训练记录的读写与状态迁移检查 | `controlplane` | [已落地] |
| `src/mindtrainer/orchestrator.py` | `resume_or_run`、`run_training` | 上述全部 | [已落地] |
| `src/mindtrainer/dispatch.py` | `make_mindtrainer_dispatcher`:指令到训练,异常到失败类别 | `controlplane.daemon`、编排器 | [已落地] |
| `src/mindtrainer/wire.py` | 把调和循环接到派发器的入口 | `controlplane.daemon` | [已落地] |
| `src/mindtrainer/data_spec.py` | `data_spec` 严格解析、要合成哪些阶段、`DataProducer` 协议 | `training_config` | [计划中] |
| `src/mindtrainer/data_producer.py` | 默认数据生产器 `TemplateDataProducer` | `simulator` | [计划中] |
| `src/mindtrainer/roll_executor.py` | `RollExecutor`:把 ROLL 的整段 LoRA SFT 作业映射到执行器协议,远端数据渲染、作业控制与指标回收 | `compute`、`executor`、`datamodel` | [已落地] |
| `src/mindtrainer/roll_converter.py` | `RollLoraConverter`:调用 ROLL 的 LoRA 导出器产出 vLLM 适配器,幂等 | `compute`、`checkpoint` | [已落地] |

依赖方向规则:

- `mindtrainer` 可以导入 `controlplane` 与 `simulator`。复用 `Episode`、`RoleBinding` 等数据结构是允许的,受限的是子系统间的运行时调用。真实执行器与转换器另可导入 `compute`;`compute` 反过来不认识训练,只导入标准库与 `controlplane.deferred`。
- `mindtrainer` 不导入 `diagnostic`。系统没有 MindTrainer 与诊断评测之间的直连;OPD 是否失效由诊断侧判定。
- 编排器只调下层纯函数和协议,不含判定逻辑。续训判定在 `resume.py`,失败到类别的映射在 `dispatch.py`。
- TrainScheduler 只可导入 `mindtrainer.data_spec` 与 `mindtrainer.training_config` 两个模块 [计划中]。factory 可以导入 `mindtrainer.dispatch` 与 `mindtrainer.data_producer` [计划中]。

## 4. 数据模型与状态机

训练配置内联在指令里且不可变;运行期状态只有训练记录一份,候选在收尾时注册。

| 实体 | 字段与含义 | 约束 |
|---|---|---|
| `TrainingConfig` | `mode`、`trained_slot`、`base_checkpoint_ref`、`teacher_model_ref`、`reward`、`sft_steps`、`grpo_steps`、`sft_batch_size`、`groups_per_step`、`group_size`、`grpo_config`、`opd_coefficient`、`checkpoint_every_steps`、`allow_resume` | 冻结;`validate_training_config` 返回第一条违规原因 |
| `TrainingConfig` 可选字段 | `stage_manifest_refs`、`scenario_ids`、`bindings`、`max_turns_per_episode`、`max_retries_per_episode`、`min_group_size`、`max_resume_attempts` | 后三项默认 2、2、3;interact 专用字段在 interact 下必填 |
| `RewardConfig` | `components`、`attribution`、`combination`、`outcome_rules`、`gamma`、`judge_model_id`、`rubric_version` | `attribution` 取 `outcome_split` 或 `judge`;`combination` 取 `sum` 或 `product` |
| `TrajectoryBatch` | `episodes`、`metadata` | 两种轨迹源的统一输出 |
| `RewardedTrajectory` | `episode`、`group_id`、`per_turn_rewards`、`combined_reward` | 逐轮奖励与 turns 等长;非训练轮为 None |
| `StepResult` | `step`、`phase`、`metrics` | 指标名是契约,见第 5 章 |
| `TrainingRun` | `run_id`、`config_hash`、`checkpoint_step`、`last_checkpoint_ref`、`resume_attempts`、`curve_summary`、`metric_stream_refs`、`status`、`failure` | `failed` 必须带 `failure["category"]` |

| 配置校验要点 | 规则 |
|---|---|
| 步数 | `sft_steps`、`grpo_steps` 不小于 0,合计不小于 1 |
| 分组 | `min_group_size` 不小于 2,`group_size` 不小于 `min_group_size` |
| 清单 | 配了 `sft_steps` 必须点 `sft` 清单;batch 配了 `grpo_steps` 必须点 `grpo_opd` 清单 |
| 引用 | 两个 `BigRef` 的 sha256 必须是 64 位小写十六进制 |
| interact 角色位 | 训练位必须是 `self_play` 且不带 `model_ref`;非训练位不能是 `self_play`,必须带 `model_ref` |
| OPD | `opd_coefficient` 是有限数且不小于 0 |

| 产物类型 | 路径 | 状态机种类 | 写方 | 状态 |
|---|---|---|---|---|
| 训练指令 | `runs/<run>/directive.yaml` | 非审核类 | 人工;TrainScheduler [计划中] | [已落地] |
| 数据集清单 | `runs/<run>/dataset_manifest_<阶段>.yaml` | 非审核类,写 `executed` | SocialSimulator;数据生产器 [计划中] | [已落地] |
| 训练记录 | `runs/<run>/training_run.yaml` | 非审核类 | MindTrainer | [已落地] |
| 候选 checkpoint | `runs/<run>/candidate_checkpoint.yaml` | 需审核类 | MindTrainer 只写 `pending_review` | [已落地] |
| checkpoint 本体 | `BigRef` 指向的外部存储 | 无 | 训练执行器 | mock 下是哈希链,无文件 |

<!-- figure:begin 30-mindtrainer-states -->
![训练记录与候选的状态](diagrams/30-mindtrainer-states.svg)

**图 3　训练记录与候选的状态**　训练记录走非审核类状态机:pending 期间可反复回写存点进度,确定性失败到 failed,收尾到 executed。候选由 MindTrainer 以 pending_review 注册,之后的审核与晋级归审核门①与调和循环。

<details>
<summary>文字版:图中的节点与连线</summary>

**节点**

- pending:运行中 · 可续训 / 回写 checkpoint 进度
- executed:候选已注册
- failed:确定性失败 · T2 / 终态,保留
- consumed:不由 MindTrainer 写
- pending_review:register_candidate 写入
- approved:审核门① 人工批准
- rejected:审核门① 人工拒绝
- effective:调和循环晋级为 zengen

**连线**

- pending → executed:收尾完成
- pending → failed:数据或配置坏
- executed → consumed:下游消费(虚线)
- pending_review → approved:approve
- pending_review → rejected:reject
- approved → effective:promote

</details>
<!-- figure:end 30-mindtrainer-states -->

训练记录的信封 id 是 `trainrun-<run>`,候选的 id 是 `cand-<run>`,`links` 是 run id 加各阶段清单 id。一份崩溃后待续训的记录:

```yaml
id: trainrun-run-1
type: training_run
status: pending
payload:
  run_id: run-1
  config_hash: <64 位十六进制>
  checkpoint_step: 2
  last_checkpoint_ref: {uri: "mock://ckpt/<state>@2", sha256: <state>, size_bytes: 0}
  resume_attempts: 0
  curve_summary: {sft_steps_done: 2, final_sft_loss: 0.5, failed_rollout_attempts: 0}
  metric_stream_refs: []
  failure: null
```

不变量:

1. 训练阶段不另存,由 `phase_at(step, cfg)` 推出:`step < sft_steps` 是 `sft`,其余是 `grpo_opd`。
2. 每个 step 用的 batch 数据是 step 的纯函数(`batch_for_step` 按固定顺序轮转)。
3. `curve_summary` 只在存点时落盘,所以续训后的摘要与存点时一致,不重复计入崩溃前多跑的步。
4. `executed` 与 `failed` 的记录只接受内容相同的重放,内容不同抛 `ValueError`。
5. 每次 resume 或 restart 先把 `resume_attempts` 加一并落盘,再调用 `executor.load`。
6. 候选的 `checkpoint_ref` 是转换后的引用,调和循环晋级时原样带进 zengen 版本。该键名与形状是契约。

## 5. 接口契约

对外只有一个入口:派发器。编排器是库级入口,测试与组合根直接调用。

```python
def make_mindtrainer_dispatcher(
    *, executor_factory: Callable[[str], TrainingExecutor] = lambda run_id: MockExecutor(),
    converter: CheckpointConverter | None = None,
    client_factory: Callable[[], object] | None = None,
    policy: ModelBackend | None = None,
) -> Dispatcher: ...

def resume_or_run(
    cfg: TrainingConfig, *, store: GitStore, run_id: str,
    make_sources: Callable[[int], dict[str, TrajectorySource]],
    attribution: AttributionBackend, executor: TrainingExecutor,
    converter: CheckpointConverter,
    metric_stream_refs: list[BigRef] | None = None,
    on_step: Callable[[StepResult], None] | None = None,
) -> TrainingRun: ...
```

`Dispatcher` 的类型是 `Callable[[GitStore, Envelope], None]`。正常返回即派发成功;抛 `DirectiveFailedError` 表示确定性失败;抛 `DeferredError` 表示资源暂时不可用,由守护进程的延后通道接住;其余异常表示瞬态。

```python
class TrainingExecutor(Protocol):
    def load(self, checkpoint_ref: BigRef) -> None: ...
    def sft_step(self, episodes: list[Episode], *, trained_slot: str) -> StepResult: ...
    def grpo_opd_step(self, rewarded: list[RewardedTrajectory], *,
                      opd_coefficient: float, teacher_model_ref: BigRef) -> StepResult: ...
    def save_checkpoint(self, tag: str) -> BigRef: ...
    def checkpoint_intact(self, checkpoint_ref: BigRef) -> bool: ...

class TrajectorySource(Protocol):
    def collect(self) -> TrajectoryBatch: ...
```

真实执行器 [已落地] 在上面五个必选方法之外,可以实现下面这些可选成员。编排器用 `getattr` 探测,没有它们的执行器(`MockExecutor`)走原来的逐步路径,行为与今天字节级相同:

```python
class StagedExecutor(TrainingExecutor, Protocol):
    supported_phases: frozenset[str]          # 真实执行器只有 {"sft"}
    checkpoint_type: str                      # 存点 fetch_hint.type,真实执行器为 "roll_ssh"
    def run_sft_stage(self, batches: list[list[Episode]], *, first_step: int,
                      trained_slot: str, prior_batches: list[list[Episode]]) -> list[StepResult]: ...
    def stage_end(self, batches: list[list[Episode]], *, first_step: int, limit: int) -> int: ...
    def preflight(self) -> None: ...          # 资源不可用抛 ComputeBusyError(DeferredError 子类)
```

`run_sft_stage` 一次跑一段连续的 SFT 步:`batches[i]` 是全局第 `first_step + i` 步的数据,与逐步路径里 `batch_for_step` 取到的相同,返回的 `StepResult` 按步排列、`step` 连续且从 `first_step` 起,编排器仍按全局计数逐个对账。`prior_batches` 是 `[0, first_step)` 各步的数据:执行器不在这些步上训练,但必须把它们写进训练数据的前缀,因为 ROLL 续训按位置跳过已消费的批次,前缀缺失会让它从错误的数据位置接着跑而且不报错。`stage_end` 可选,让执行器把段切得更细,返回值是开区间终点且必须满足 `first_step < 返回值 <= limit`,否则编排器抛 `RuntimeError`。`preflight` 在 `resume_or_run` 里 `decide_resume` 判定之后、`run_training` 之前调用,`noop` 与 `escalate` 不触发。`supported_phases` 不覆盖配置要求的阶段时,在配置校验期判 T2。

行数与截断预检(执行器的可选方法 `validate_sft_data`,数据有问题时在动执行器之前确定性失败)属于真实后端设计,实施计划不含它,待主机验证之后再加 [计划中]。
GRPO 与 OPD 只有一个方法,因为两者在同一个训练 step 里联合更新。`StepResult.step` 是执行器自己的全局计数,编排器发现它与自己的 step 不一致时抛 `RuntimeError`。

| 指标 | 阶段 | 含义 |
|---|---|---|
| `sft_loss` | sft | mock 下为 `1/(1+step)`,不是学习证据 |
| `grpo_loss`、`opd_loss`、`total_loss` | grpo_opd | mock 下是 step 的合成函数;`total_loss = grpo_loss + opd_coefficient × opd_loss` |
| `opd_weight` | grpo_opd | 实际使用的 OPD 系数,系数为 0 时 OPD 未生效 |
| `mean_reward` | grpo_opd | 本步轨迹的 `combined_reward` 均值,来自输入,是真值 |
| `n_groups`、`n_degenerate_groups` | grpo_opd | 本步组数、组内奖励全相同的组数 |

| 产物读写 | 约定 |
|---|---|
| 读指令 | 取 `payload["training_config"]`,缺失或不是 dict 判 `failed` |
| 读清单 | 清单 id 形如 `dsm-<数据所在 run>-<阶段>`,数据所在 run 从 id 解出,允许复用前一轮的数据 |
| 写训练记录 | 开跑前、每次存点、最终存点后、置 `executed` 时各写一次;同内容重写是空操作 |
| 写候选 | `status=pending_review`,payload 含 `checkpoint_ref`、`train_curve_summary`、`training_run_ref`、`base_checkpoint_ref`、`config_hash` |
| 不写 | 指令的状态(归调和循环);episode(batch 由 SocialSimulator 写,interact 由服务端钩子写) |

触发条件:调和循环对每条 `pending` 指令产出 `dispatch_directive` 动作。指令保持 `pending` 时,下一轮 tick 会再次派发,续训靠这一点发生。

| 邻居 | 本文依赖它什么 | 它依赖本文什么 |
|---|---|---|
| ControlPlane | 派发缝、`DirectiveFailedError` 的 `category`、非审核类状态机、`GitStore.write_product` 对相同内容空操作 | 派发器签名;候选的 id、状态与 `checkpoint_ref` 形状 |
| SocialSimulator 数据集 | `verify_manifest` 逐条校验;`episode_group_id` 是分组键 | 无 |
| SocialSimulator 实时服务 | 一条流一次会话;终止原因;`on_session_closed` 落盘 episode | 客户端放弃会话时调用 `abort()`,不发 EndSession |
| 审核门①与晋级 | 候选进入 `pending_review` 后由人审核 | `latest_promoted_checkpoint` 从晋级版本解析下一轮的 `base_checkpoint_ref` |
| TrainScheduler [计划中] | 按 `dsm-<run>-<阶段>` 写 `stage_manifest_refs` 与 `data_spec` | `validate_training_config`、`load_config_from_directive` 与 `data_spec` 解析 |
| factory [计划中] | 无 | 用 `make_mindtrainer_dispatcher` 组装,并注入数据生产器 |

数据合成接线 [计划中]:`make_mindtrainer_dispatcher` 末尾加 keyword-only 的 `data_producer=None` [计划中]。签名来自工作包 2,施工时可能微调。

```python
class DataProducer(Protocol):
    def produce(self, store: GitStore, run_id: str,
                cfg: TrainingConfig, data_spec: DataSpec) -> None: ...
```

派发器在读清单前检查三件事:清单文件缺失、指令带 `data_spec`、数据所在 run 就是本 run。三者都成立才调用生产器,其余情况与现在完全相同 [计划中]。`data_spec` 恰好四个键:`scenario_count`、`seed`、`purpose`、`stages`,缺键或多键都拒绝 [计划中]。

`data_spec` 的字段规则 [计划中]:

| 键 | 类型与范围 | 说明 |
|---|---|---|
| `scenario_count` | 整数,1 至 64 | 要合成的情境数;上限防止一条写错的指令在一个 tick 里写出成千上万个文件,每个文件都是一次 git 提交 |
| `seed` | 整数,0 至 2 的 31 次方减 1 | 第 i 个情境用 `seed + i` |
| `purpose` | `training` 或 `deployment_simulation` | 取自情境的用途字段 |
| `stages` | 阶段名到 `{group_size}` 的映射,阶段名只能是 `sft`、`grpo_opd` | `group_size` 整数,1 至 16;`grpo_opd` 的 `group_size` 不小于 2,否则组内奖励必然零方差 |

类型先于范围:整数字段排除布尔与浮点(含 `4.0`、`nan`)。任何不合规都只抛 `InvalidDataSpecError` [计划中],不让别的异常类型漏出守护循环。错误信息里的展示值经截断函数,超过 4300 位的整数也不会让信息拼接抛 `ValueError`。

交叉校验:要合成的阶段在 `data_spec.stages` 里都有条目;合成 `grpo_opd` 时 `group_size` 不小于 `min_group_size`,`scenario_count` 不小于 `groups_per_step` [计划中]。

`mindtrainer.data_spec` 的对外名字 [计划中]:`DataSpec`、`StageSpec`、`parse_data_spec`、`data_spec_from_directive`(没有 `data_spec` 键或值为空时返回 `None`)、`InvalidDataSpecError`、`DataSynthesisError`、`required_stages`、`manifest_data_run`、`stages_to_synthesize`、`check_data_spec_against_config`。

生产器的错误契约 [计划中]:确定性做不成必须抛 `DataSynthesisError`,派发器把它判为 T2;其余异常原样上抛,指令保持 `pending`。替代生产器必须自己把确定性错误包成 `DataSynthesisError`,否则守护进程会每轮重派同一条指令并退出。

默认数据生产器 [计划中]:`TemplateDataProducer`,由组合根注入。
- 构造参数含 `rollout_factory`、生成后端、`max_turns`(默认 4,取值 1 至 200,与实时服务端的上限一致)与对手的 `model_ref`(默认 `template-opponent-v1`)。`max_turns` 不放进 `data_spec`,想改轮数只能在组合根构造时传参。
- 默认的 `keyword_alternating_rollouts`:训练位在偶数序号的 rollout 里每句都说各分量的第一个关键词,奇数序号只说不含任何关键词的中性话,对手位恒说中性话。因此每个组的奖励是高低混合,不会退化。
- 只支持 `outcome_split` 归因,其他归因抛 `DataSynthesisError` [计划中]。第 i 个情境用种子 `seed + i`。
- **V1 训练数据里的奖励差异是人造的,只证明流程跑通,不代表任何社会能力。**
- 情境的角色位按训练配置改写:带 `bindings`(interact)就照它,训练位排第一;不带(batch)就配一个 `frozen_reference` 的对手位 `opponent`。
- 生产器只写 episode 与清单,不写 `scenarios/` 与 `personas/`。

## 6. 关键流程

一次训练分八步。前三步决定起点并落盘,第 4 到 6 步训练,最后两步收尾。

<!-- figure:begin 30-mindtrainer-flow -->
![一次训练的编排](diagrams/30-mindtrainer-flow.svg)

**图 4　一次训练的编排**　派发器校验指令并备好轨迹源,编排器读训练记录判定起点,落盘后加载权重。batch 先对整份 GRPO 数据集预检,再逐步训练并周期存点;最后一步之后存最终点、转换格式、注册候选、记录置 executed。确定性失败把记录写成 failed,其余异常让记录保持 pending。

<details>
<summary>文字版:图中的节点与连线</summary>

**节点**

- 派发器准备:读并校验 training_config / 构造归因后端与轨迹源
- 判定起点:decide_resume / noop 返回 · escalate 抛出
- 起点落盘:写 pending 记录 / 再 executor.load
- batch 预检:整份 GRPO 数据算奖励 / 校验组大小与组数
- 逐步训练:phase_at · batch_for_step / sft_step / grpo_opd_step
- 周期存点:每 checkpoint_every_steps 步 / 回写引用与曲线摘要
- 最终存点与转换:先回写 pending / 再 converter.convert
- 注册候选:candidate pending_review / 记录置 executed
- 确定性失败:记录写 failed · T2 / 异常原样重抛
- 其余异常:记录保持 pending / 下次派发时续训

**连线**

- 派发器准备 → 判定起点
- 判定起点 → 起点落盘
- 起点落盘 → batch 预检
- batch 预检 → 逐步训练
- 逐步训练 → 周期存点
- 周期存点 → 逐步训练
- 逐步训练 → 最终存点与转换:最后一步之后
- 最终存点与转换 → 注册候选
- 逐步训练 → 确定性失败:数据或配置坏(含步骤 4)
- 逐步训练 → 其余异常:崩溃 · 瞬态

</details>
<!-- figure:end 30-mindtrainer-flow -->

| 步 | 谁 | 做什么 | 写什么 |
|---|---|---|---|
| 1 | 派发器 | 读并校验配置,构造归因后端;interact 检查 `client_factory` 与 `policy`;batch 检查清单在场且 id 对得上 | 失败时不写记录,指令判 `failed` |
| 1a | 派发器 | 缺清单且指令带 `data_spec` 时先合成,插在客户端检查之后、读清单之前:先严格解析 `data_spec`(带了就解析,即便清单都在场),再检查不合成的其余阶段的清单在场,再交叉校验,最后调生产器。interact 指令只合成 `sft`。已在场的清单不重做 [计划中] | 失败时不写记录,指令判 `failed` |
| 2 | 编排器 | 读训练记录,交给 `decide_resume`;`noop` 直接返回,`escalate` 抛 `NeedsHumanError` | 无 |
| 3 | 编排器 | 新运行从 step 0 起;resume 从 `checkpoint_step` 起;restart 从 step 0 起。之后先写 `pending` 记录,再 `executor.load` | 训练记录 |
| 4 | 编排器 | batch 且有 GRPO 步:整份 GRPO 数据集一次算完奖励,校验组大小与组数 | 失败写 `failed` |
| 5 | 编排器 | 逐步训练:SFT 取 `sft_batch_size` 条 episode;GRPO 取 `groups_per_step` 个完整组 | 无 |
| 6 | 编排器 | 每 `checkpoint_every_steps` 步存点(最后一步除外),回写引用、step 与曲线摘要 | 训练记录 |
| 7 | 编排器 | 存最终 checkpoint,先回写 `pending` 记录,再转换格式 | 训练记录 |
| 8 | 编排器 | 注册候选,记录置 `executed`;派发器返回,调和循环把指令置 `executed` | 候选、训练记录 |

第 2 步的判定规则如下,先命中者生效。

<!-- figure:begin 30-mindtrainer-resume -->
![续训判定](diagrams/30-mindtrainer-resume.svg)

**图 5　续训判定**　decide_resume 对已有的训练记录按六条规则依次判定,先命中者生效;未知状态直接抛 ValueError。结论只有四种:noop 原样返回,resume 从中间存点续,restart 从基座重来,escalate 交给人。

<details>
<summary>文字版:图中的节点与连线</summary>

**节点**

- 已 executed 或 consumed?
- 状态是 failed?
- resume_attempts ≥ 上限?
- allow_resume 为假?
- 有 last_checkpoint_ref?
- checkpoint_intact 为 True?
- noop:原样返回已有记录
- escalate:抛 NeedsHumanError / 派发器记 T3,指令置 failed / 记录本身不改
- restart:从 base_checkpoint_ref 的 step 0 重来
- resume:从 checkpoint_step 续训

**连线**

- 已 executed 或 consumed? → 状态是 failed?:否
- 状态是 failed? → resume_attempts ≥ 上限?:否
- resume_attempts ≥ 上限? → allow_resume 为假?:否
- allow_resume 为假? → 有 last_checkpoint_ref?:否
- 有 last_checkpoint_ref? → checkpoint_intact 为 True?:是
- 已 executed 或 consumed? → noop:是
- 状态是 failed? → escalate:是 · 规则 2
- resume_attempts ≥ 上限? → escalate:是 · 规则 3
- allow_resume 为假? → escalate:是 · 规则 4
- 有 last_checkpoint_ref? → restart:否 · 从未存点
- checkpoint_intact 为 True? → resume:是
- checkpoint_intact 为 True? → escalate:否 · 损坏或无法确认

</details>
<!-- figure:end 30-mindtrainer-resume -->

失败与恢复路径:

| 场景 | 路径 |
|---|---|
| 崩溃在第一次存点之前 | 记录 `pending` 且无引用,下一次派发 restart,从基座 step 0 重来 |
| 崩溃在存点之后 | 下一次派发 resume,从 `checkpoint_step` 续,`make_sources` 收到 `max(0, start - sft_steps)` |
| 崩溃在最终存点之后、注册之前 | 下一次派发 resume 到 `checkpoint_step = total`,不重训任何一步,直接转换并注册 |
| 注册之后、置 `executed` 之前被人批准 | 候选确属本 run、本配置、同一份权重,视为注册已完成,照常置 `executed`;否则交给人 |
| 每次续训都在同一步崩溃 | `resume_attempts` 到 `max_resume_attempts` 后升级为 T3 |
| 续训时配置变了 | `config_hash` 不同,抛 `NeedsHumanError`,记录不改 |
| interact 的 rollout | 失败的会话整条重跑,不续接;续训时 `InteractTrajectorySource` 的 `start_step` 让情境轮转接着走 |
| 合成中途崩溃 | 已在树里的清单不再合成;episode 写完而清单未写的阶段整段重跑覆盖,逐字节相同的文件不产生新提交 [计划中] |

走查例子:batch 指令在第 3 步崩溃,从第 2 步的存点续训,第 6 步完成。数值来自 `tests/test_mt_orchestrator.py` 的同一套夹具,经 `uv run python` 实跑。

| 配置项 | 值 |
|---|---|
| `sft_steps`、`grpo_steps` | 2、4,共 6 步 |
| `sft_batch_size`、`groups_per_step`、`group_size` | 1、1、2 |
| `checkpoint_every_steps` | 2 |
| `opd_coefficient` | 0.1 |
| 奖励 | `outcome_split`,分量 `goal`,关键词 `agreed`,`sum` 组合 |
| 数据 | 两个情境;sft 清单每组 1 条,grpo_opd 清单每组 2 条,一条含 `agreed`(奖励 1.0),一条不含(0.0) |

| 时刻 | 事件 | 训练记录 |
|---|---|---|
| 第一次派发 | 跑完 step 0、1,第 2 步之后存点 `@2`;跑完 step 2;进入 step 3 时执行器崩溃 | `pending`,`checkpoint_step=2`,`resume_attempts=0`,摘要只含 2 个 SFT 步 |
| 第二次派发 | `decide_resume` 判 resume;`resume_attempts` 加到 1 并落盘;新执行器 load `@2`,重跑 step 2,再跑 step 3 到 5 | 第 4 步之后存点 `@4` |
| 收尾 | 最终存点 `@6`,转换为 `@6.hf`,注册候选 `cand-run-1` | `executed`,`checkpoint_step=6`,`resume_attempts=1` |

| step | 阶段 | 主要指标 |
|---|---|---|
| 0 | sft | `sft_loss=1.0` |
| 1 | sft | `sft_loss=0.5` |
| 2 | grpo_opd | `grpo_loss=0.3333`,`total_loss=0.35`,`mean_reward=0.5` |
| 3 | grpo_opd | `grpo_loss=0.25`,`total_loss=0.2625`,`mean_reward=0.5` |
| 4 | grpo_opd | `grpo_loss=0.2`,`total_loss=0.21`,`mean_reward=0.5` |
| 5 | grpo_opd | `grpo_loss=0.1667`,`total_loss=0.175`,`mean_reward=0.5` |

与一次跑完的对照:执行器终态哈希相同(前缀 `26c1ec45fa6f`),候选的 `checkpoint_ref` 相同,`curve_summary` 相同。唯一的差别是 `resume_attempts`,一次跑完为 0,续训为 1。step 2 在崩溃前后各跑一次,因为它在 `@2` 存点之后、崩溃之前。

## 7. 关键决策

| 编号 | 决策 | 备选 | 理由 | 判错的代价 |
|---|---|---|---|---|
| MT-1 | 派发器住在 `mindtrainer` 侧,经 ControlPlane 的派发缝注入 | 在 `controlplane` 里直接调用训练 | 依赖方向单向,调和循环不认识 MindTrainer | 每个入口都要自己组装派发器 |
| MT-2 | 训练配置内联在指令里,用规范化 JSON 的 sha256 锚定 | 配置单独成产物;引用配方 | RecipeStore 尚未落地,不提前设计它的一部分 | 配方落地后要改成引用,配置结构不变 |
| MT-3 | batch 模式用离线分组 GRPO:数据集同一上下文至少 2 条,按 `group_id` 算优势 | 训练时在数据集上下文重采样;退化为奖励加权 SFT | 重采样会把 interact 搬进 batch;奖励加权 SFT 不是 GRPO,`grpo_config` 失去意义 | 不是 on-policy;ROLL 对离线分组数据的语义未核实 |
| MT-4 | 组内优势取均值基线,不除标准差 | 标准化优势 | 是否标准化交给真实执行器经 `grpo_config` 决定 | 换执行器时要确认它自己做了标准化 |
| MT-5 | `combined_reward` 取训练轮逐轮奖励的均值 | 取总和 | 总和随对话变长而变大,会奖励拖长对话 | `gamma<1` 时较长的 episode 均值偏低 |
| MT-6 | 成员不足 `min_group_size` 的组是错误;成员够但奖励全同只计数 | 两者都告警;两者都报错 | 一组 1 条时优势恒为 0,训练静默白跑;奖励恰好相同是数据的正常情形 | 奖励全同的组仍白跑,只能看 `degenerate_groups_total` |
| MT-7 | batch 的 GRPO 数据在训练开始前整份预检 | 训练到 GRPO 阶段才检查 | 数据有问题时一步都不浪费 | 预检多读一遍数据集 |
| MT-8 | 从未存点的 pending 记录判 restart,不判 T3 | 一律 T3 | 什么都没丢,从头跑等价于新运行 | 一条在第一次存点前反复崩溃的指令,靠 `resume_attempts` 上限挡住 |
| MT-9 | `resume_attempts` 先加一并落盘,再加载权重 | 训练成功后再计数 | load 本身反复失败也会触顶升级 | 无 |
| MT-10 | 曲线摘要只在存点时快照 | 每步写入 | 续训后摘要与一次跑完相同,不重复计入 | 崩溃前多跑的步在摘要里看不到 |
| MT-11 | `role_backend_failure` 不重试 | 按 T1 重试 | 服务端用它同时表示后端确定性异常和工厂故障,分不出 T1 与 T2 | 真正的瞬态后端故障也会让本次 collect 失败 |
| MT-12 | judge 与被训模型相同或互为前缀时构造即拒绝 | 只在诊断侧防自评 | 训练会被直接优化去讨好自己的 judge | 前缀匹配不是真正的血缘判断,漏判与误判都有 |
| MT-13 | `allow_resume` 与 `max_resume_attempts` 计入 `config_hash` | 只哈希影响训练产物的字段 | 原设计未记录理由,代码表现为整份配置一起哈希 | 两项一旦改动,同一个 run 判"配置变了",无法续训,只能重跑;要支持放宽后续训,须把它们移出 `config_hash` |
| MT-14 | SFT 只以训练位的 `utterance` 为目标,不训练 `inner_thought` | 同时训练内心推理 | 原设计未记录理由,是实现者的选择 | 被训模型学不到内心推理 |
| R14 | 指令携带 `data_spec`,派发器缺清单时调用注入的数据生产器 [计划中] | 由调度器先单独合成 | 合成与训练同一条指令,不多一种动作 | V1 模板生成器与目标维度无关,闭环只是流程跑通 |

## 8. 失败处理与可观测

失败的类别在派发器里定:确定性失败变成带类别的 `DirectiveFailedError`,其余异常原样上抛。

| 失败点 | 类 | 处理 | 留痕产物 |
|---|---|---|---|
| 配置缺字段、类型错、校验不过 | T2 | 指令判 `failed`,不写训练记录 | 指令 `payload["failure"]` |
| 清单缺失、id 形状不对、读不出、id 不符 | T2 | 指令判 `failed` | 指令 `payload["failure"]` |
| `data_spec` 解析不过(`InvalidDataSpecError`) | T2 | 指令判 `failed`,不写训练记录 | 指令 `payload["failure"]` [计划中] |
| 需要合成清单却没注入生产器 | T2 | 同上 | 同上 [计划中] |
| `data_spec` 与训练配置交叉校验不过 | T2 | 同上 | 同上 [计划中] |
| 生产器抛 `DataSynthesisError` | T2 | 同上,原因前缀 `data synthesis failed` | 同上 [计划中] |
| 生产器的其余异常(`OSError` 等) | T1 | 原样上抛,指令保持 `pending`,下一轮重入时已在场的清单不再合成 | 无 [计划中] |
| 数据集哈希不符(`DatasetIntegrityError`) | T2 | 记录写 `failed`,指令判 `failed` | 训练记录 `failure`、指令 `failure` |
| 组不足(`DegenerateGroupError`)、组数不够(`NotEnoughGroupsError`) | T2 | 同上 | 同上 |
| 归因不合法(`AttributionError`)、judge 归因而入口无 judge | T2 | 同上;无 judge 时不写记录 | 同上 |
| interact 会话被拒(`invalid_session`) | T2 | 记录写 `failed`,当轮把指令判 `failed` | 同上 |
| 不可续:记录已 `failed`、续训次数耗尽、禁止续训、存点损坏或无法确认、配置变了 | T3 | 抛 `NeedsHumanError`,指令判 `failed`,记录不改 | 指令 `failure.category = T3` |
| 跨模式续训:mock 存点配真实执行器,或反过来 | T3 | 在 `checkpoint_intact` 之前比对存点的 `fetch_hint.type` 与执行器的 `checkpoint_type`,不一致抛 `NeedsHumanError`,理由写「后端模式不一致」,记录不改 | 指令 `failure.category = T3` |
| `supported_phases` 不覆盖配置要求的阶段 | T2 | 配置校验期记录写 `failed`,指令判 `failed`,一步都不动执行器 | 训练记录 `failure`、指令 `failure` |
| 资源暂时不可用:`ComputeBusyError` | 延后 | 预检在 `run_training` 之前,不写记录,不耗 `resume_attempts`;守护进程接住并按退避重试 | 守护进程结构化日志 |
| 传输层故障 `TransportError`、远端作业超时 `RemoteJobTimeoutError` | 延后 | 同上,经延后通道重试;进入 `run_training` 之后抛出的,`resume_attempts` 已加一,重试仍受 G5 的上限约束 | 同上 |
| 远端作业非零退出 `RemoteJobFailedError` | 延后 | 同上;它是真失败,计入 `resume_attempts`,耗尽后升级为 T3 | 训练记录 `resume_attempts` |
| 远端作业成功但指标解析不出 `MetricsMissingError` | T1 | 记录保持 `pending`,计入 `resume_attempts`;重试时作业命中已成功退出的那次尝试,只重新解析,不重训 | 训练记录 `resume_attempts` |
| 执行器崩溃、转换器故障、读清单时 `OSError` | T1 | 原样上抛,记录与指令保持 `pending`,下一次派发续训 | 训练记录的存点进度 |
| interact 的 `timeout`、`role_backend_timeout`、传输层 UNAVAILABLE | T1 | 原地重跑,至多 `1 + max_retries_per_episode` 次;耗尽后上抛,记录保持 `pending` | `curve_summary["failed_rollout_attempts"]` |
| interact 的 `role_backend_failure` 等其余原因 | T1 | 不重试,上抛,记录保持 `pending`,由续训上限兜底 | 同上 |

T1 的上抛异常会让 `run_daemon` 退出,需要外部监督者重启。重启后同一条指令再次派发,`resume_attempts` 上限保证它最终升级为 T3,不会无限空转。`DeferredError` 的子类不在此列:守护进程接住它们,进程不退出;同一动作连续延后超过上限才带原因退出(CP-14)。

只读观测手段:

| 手段 | 看什么 |
|---|---|
| 读 `runs/<run>/training_run.yaml` | 状态、存点进度、续训次数、曲线摘要、失败原因 |
| `controlplane.observe.trace` | run 目录下各产物的状态与 `links`,以及 `metric_stream_refs` 是否可达 |
| `on_step` 回调 | 逐步指标;逐步序列不进 git,只有调用方能看到 |
| git 历史 | 每次记录回写都是一次提交,提交信息带 `training_run: <run> [<状态>]` |

## 9. 测试策略

全部测试不依赖 GPU 与外部服务;interact 的测试在本进程里起真实的实时环境服务端。

| 层次 | 对象 | 替身 |
|---|---|---|
| 纯函数 | 配置校验与哈希、分组与优势、归因与组合、续训判定、`batch_for_step` | 无 |
| 组件 | 两种轨迹源、执行器、转换器与注册、记录读写 | `ScriptedBackend` 驱动的数据集;`MockTurnJudge` |
| 编排 | 一次跑完、6 处崩溃注入后续训、升级、各类确定性失败 | `MockExecutor(crash_at_step=…)`、可失败的转换器 |
| interact | rollout 轮转、续训接着轮转、各终止原因的分类 | 本进程的实时环境服务端 |
| 派发与接线 | 指令到候选、失败到类别、坏指令不挡后面的指令 | 临时 git 仓库 |
| 端到端 | 候选走完未修改的调和循环到晋级,再成为下一轮基座 | 占位诊断 |

默认测试套件不依赖的外部服务:ROLL、vLLM、真实 LLM judge、远程 GPU、SSH。真实后端的测试用假传输和假客户端,远端助手脚本在本机真跑 [已落地];真起子进程的那个文件标 `slow`,`-m 'not slow'` 可跳。连真实主机的冒烟用例标 `remote`、默认跳过 [计划中]:这个标记还没有注册,也还没有这样的用例。

| 测试文件 | 用例数 | 状态 |
|---|---|---|
| `tests/test_mt_config.py` | 171 | [已落地] |
| `tests/test_mt_reward_pipeline.py` | 59 | [已落地] |
| `tests/test_mt_dispatch.py` | 39 | [已落地] |
| `tests/test_mt_orchestrator.py` | 36 | [已落地] |
| `tests/test_mt_executor.py`、`tests/test_mt_grouping.py`、`tests/test_mt_trajectory_interact.py` | 各 26 | [已落地] |
| `tests/test_mt_training_run.py` | 22 | [已落地] |
| `tests/test_mt_resume.py` | 19 | [已落地] |
| `tests/test_mt_trajectory_batch.py` | 17 | [已落地] |
| `tests/test_mt_checkpoint.py` | 14 | [已落地] |
| `tests/test_mt_datamodel.py` | 10 | [已落地] |
| `tests/test_mt_wire.py`、`tests/test_mt_paths.py`、`tests/test_mt_integration.py`、`tests/test_mt_packaging.py` | 4、3、2、2 | [已落地] |
| 合计 | 476 | [已落地] |
| `tests/test_daemon_dispatcher.py`(ControlPlane 侧的派发缝) | 14 | [已落地] |
| `tests/test_mt_data_spec.py`、`tests/test_mt_data_spec_stages.py` | 预计 70、17 | [计划中] |
| `tests/test_mt_data_producer.py`、`tests/test_mt_dispatch_synthesis.py` | 预计 25、33 | [计划中] |
| `tests/test_mt_roll_executor.py`、`tests/test_mt_roll_converter.py` | 21、17 | [已落地] |
| `tests/test_mt_orchestrator.py` 的阶段路径、真实执行器与转换器集成用例 | 新增 6、3、1 | [已落地] |
| `tests/test_daemon_deferred.py`(ControlPlane 侧的延后通道) | 10 | [已落地] |

用例数由 `uv run pytest --collect-only -q` 逐文件得出。

## 10. 现状、局限与未核实项

编排、续训与失败分类是真的;训练本身是 mock,损失值不是学习证据。

| 能力 | 状态 | 说明 |
|---|---|---|
| 配置校验与哈希锚定 | [已落地] | |
| batch 轨迹源与清单校验 | [已落地] | |
| interact 轨迹源 | [已落地] | 入口不提供实时服务端与策略 |
| 规则归因与 judge 归因 | [已落地] | 入口不提供 judge |
| 离线分组 GRPO 的分组契约 | [已落地] | 算法语义未在 ROLL 上核实 |
| 续训判定与崩溃续训 | [已落地] | 逐字节等价只在 mock 下有证据 |
| 调和循环接线 | [已落地] | `scripts/run_controlplane.py` 用 mock 执行器与转换器 |
| 数据合成接线 | [计划中] | 工作包 2 |
| ROLL LoRA SFT 真实执行器、适配器导出转换器、阶段协议 | [已落地] | 远端计算层 `compute` 是它们的前置,已先于执行器与转换器落地;入口装配与真实主机验证仍待做 |
| 真实 GRPO 加 OPD 训练、适配器合并回完整权重 | [推迟] | |

| 已知局限 | 影响 | 缓解 |
|---|---|---|
| 派发器不传 `metric_stream_refs` 与 `on_step` | 接线路径下没有逐步指标,只有存点时的摘要 | 组合根调用编排器时自行传入 |
| interact 的 T1 重试没有退避间隔 | 服务端持续不可用时,重试在瞬间耗尽 | 续训上限兜底;需要时在客户端工厂里加退避 |
| interact 失败的 collect 会在产物树留下不属于任何 batch 的 episode | 按情境扫描取数据会取错 | 下游只按 `metadata["episode_ids"]` 取数据 |
| 续训次数耗尽后无法通过放宽配置救回 | 人只能写一条新指令 | 改为只哈希影响训练产物的字段 |
| `gamma<1` 时较长的 episode 均值偏低 | batch 数据集长度不一时奖励有长度偏置 | 默认 `gamma=1.0`;interact 下 episode 都跑满 `max_turns`,不受影响 |
| 训练同步阻塞调和循环 | 训练期间审核与晋级动作排队,一轮可达数小时 | 异步派发:指令加 `submitted` 状态,派发器拆成提交与轮询,训练记录里的 `remote_job_ref` 作幂等键 [推迟];接真实 LoRA SFT 的这一轮作为有限定的例外接受阻塞(CP-15) |
| 真实后端只支持单轮 [计划中] | 真实转换器产出适配器导出,晋级把它原样复制进模型产物,下一轮基座解析得到它,真实执行器 `load` 拒绝它:第二轮必然确定性失败。G6 在真实后端下不成立 | 候选多带一个训练格式引用,晋级与基座解析随之改 [推迟];本轮由 `RollExecutor.load` 响亮失败,理由写明原因 |
| 服务端 `turn_timeout_s` 小于策略最坏延迟时,慢策略被当作 T1 重试 | 推理成本按重试次数放大 | 组合根配置时保证超时大于策略延迟 |
| 合成的情境与人格不落盘 | 想按 id 查生成的情境,只能看 episode 里的 `scenario_id` 与 `role_assignment` | 无 [计划中] |
| 默认生产器的奖励差异是人造的 | 合成数据只证明流程跑通 | 换桥接生成器 [计划中] |
| judge 归因用的 `trained_model_id` 没有锚定 | 它不是训练配置字段,也不进 `config_hash` | 接入 judge 时取晋级基座的 `model_id` |
| interact 的客户端工厂每次新建通道 | 并发会话多时连接数增长 | 工厂里复用同一个 gRPC 通道,每次只新建存根与客户端;已在真实服务端上验证过 4 个客户端共用 1 个通道,含一次中止之后再收集 |
| interact 的 sft 合成不用 `bindings` 点名的人格 | 合成 episode 的人格与实时会话里的不同 | 无 [计划中] |

| 未核实项 | 影响 | 核实办法 |
|---|---|---|
| ROLL 对离线分组 GRPO 数据的语义 | 算法正确性无保证 | 接入 ROLL 前对照其文档与源码 |
| 远端容器内外的工作目录是否同一路径、宿主机的 `nvidia-smi` 能否看到别的容器的计算进程 | 助手的路径假设与预检的 GPU 占用判定都依赖这两点 | 在验证主机上从容器内外各建一个文件、各跑一次 `nvidia-smi` |
| 远端 Python 版本 | 助手按 3.8 语法写,实际版本更低则无法运行 | 在验证主机上跑一次助手的 `probe` |
| 真实执行器下续训是否逐字节等价 | 取决于优化器与随机数状态是否存点、浮点归约顺序 | 真实执行器上做崩溃注入 |
| 长跑 rollout 的 keepalive | 死连接可能不被及时发现 | 构造断连场景压测 |
| 计划中四个测试文件的用例数取自施工计划,未实测 [计划中] | 第 9 章的预计数可能与落地后不同 | 工作包 2 落地后逐文件 `uv run pytest --collect-only -q` |
| 对手模型只以 `model_ref` 字符串记录 | 对手版本没有制品锚定 | 要锚定就得给 `model_ref` 加带 sha256 的引用形式;RecipeStore 的校验目前只要求它是具体 id,不校验哈希 [计划中] |
| interact 的 episode 不会以 `goal_reached` 结束 | 依赖目标达成的奖励规则恒为 0 | 实时服务支持早停时重新评估 |

明确不做:MindTrainer 不读诊断结果,不决定练什么,不改指令状态,不写成本记录,不并行 rollout。

## 附 本文术语

| 术语 | 含义 |
|---|---|
| 训练记录 | `training_run.yaml`,一个 run 的运行状态与断点 |
| 存点 | 训练中途保存的 checkpoint,引用写进训练记录 |
| 轨迹源 | 产出 `TrajectoryBatch` 的组件,batch 读清单,interact 跑实时会话 |
| 归因 | 把一条 episode 的奖励分到训练角色位的每一轮 |
| 离线分组 GRPO | 在静态数据集上按同一上下文分组算优势的 GRPO |
| 退化组 | 成员不足 `min_group_size` 的组(报错),或奖励全同的组(计数) |
| 续训 | 从存点(resume)或基座(restart)接着跑同一个 run |
| `group_id` | GRPO 分组键,由情境 id 与各角色位的人格、后端、`model_ref` 推出 |
| `data_spec` | 指令里描述要合成什么数据的载荷 [计划中] |
