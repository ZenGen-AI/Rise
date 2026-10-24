# MindAgent:部署态智能体的技能与记忆自进化

| 项 | 内容 |
|---|---|
| 一句话 | 新模型晋级后,部署 Actio 实例采集经验,分析行为缺口,生成并验证技能提案,经审核门②收录进正式库 |
| 状态 | 部分落地:配置、库读取、失败守卫、部署、分析、提案、验证、打包、轮次与收录两个处理器、调和循环触发与组合根装配 [已落地],已由 `factory run --enable agent,admit` 经 `reconcile_once` 端到端跑通;闭环里的模型后端是罐头实现(`factory.agent.CannedGuidedBackend`,不加载 checkpoint),轮次产物不记后端来源 |
| 所在层 | 计算层 |
| 代码位置 | `src/mindagent` [已落地] |
| 基线提交 | `b826440`(2026-10-21) |
| 依赖 | ControlPlane(`controlplane`)、Actio(`actio`)、外部推理桥接(`inference_bridge`)、SocialSimulator(`simulator` 的数据模型、产物读写、实时环境服务客户端) |
| 被依赖 | factory 组合根(注册两个处理器);调和循环经注入调用处理器;命令行读提案载荷 |
| 对应实施计划 | 工作包 7(任务 80 至 89)与工作包 8、9 的触发与装配 [已落地] |

## 0. 系统速览

<!-- shared:begin context-block -->
**系统是什么。** RismForge 是一套"AI 驱动的社会心智后训练工厂":输入通用开源基础模型,输出社会心智能力显著增强的模型,代号 **ZenGen**。工厂自己决定每一轮练什么、怎么练。训练决策可以交给外部更强的闭源模型(经命令行调用),关键决策点设人工审核门,其余环节自动。

**七个子系统。** 六个能力子系统加一个协调层 ControlPlane。

| 子系统 | 职责 | 所在层 | 现状 |
|---|---|---|---|
| ControlPlane | 协调层:git 版本化产物树、状态机、调和循环、三个审核门 | 控制层 | [已落地] |
| SocialSimulator | 情境与人格生成、多智能体仿真、实时环境服务、诊断评测 | 计算层 | [已落地];诊断默认用 mock 后端,`--evaluator someval` 装配 vLLM 加 SoMEval 的真实运行器 [已落地],真机只验证到预测阶段 |
| MindTrainer | 权重级训练:BatchTrain(SFT 后接 GRPO 加 OPD)与 InteractTrain | 计算层 | [已落地],默认入口的训练执行器是 mock;ROLL LoRA SFT 真实执行器 [已落地],由 `--executor roll` 装配,已在真实主机上端到端跑通一次 |
| MindAgent | 部署态智能体的技能与记忆自进化,不改权重 | 计算层 | [已落地],闭环里经 `--enable agent` 装配;部署驱动用的仍是罐头后端,真实模型后端未接 |
| RecipeStore | 验证有效的训练配方的版本化仓库 | 控制层 | [已落地] |
| TrainScheduler | 决策执行器:装配上下文、校验决策、构造训练指令、提名配方 | 控制层 | [已落地],含策略经验(默认关) |
| TrainPlanner | 决策学习者:经可插拔后端给出下一轮训练决策 | 控制层 | [已落地],默认后端经桥接调用命令行模型 |

共享基础设施有五个。**Actio** 是带技能库与显式记忆的智能体框架,每次响应附带激活轨迹。**外部推理桥接**用命令行调用外部强模型。**远端计算层** `compute` 经 SSH 在远端 GPU 主机上跑作业 [已落地]。**ROLL** 是分布式训练引擎,**vLLM** 是推理引擎。V1 经远端计算层接入 ROLL 的 LoRA SFT [已落地](由 `--executor roll` 装配,已在真实主机上从指令跑到候选一次)与 vLLM 推理服务 [已落地](命令拼装、SSH 隧道、服务生命周期;真机端到端待补)。已落地部分仍由确定性 mock 顶替。真实 GRPO 加 OPD 训练与真实 judge [推迟]。

**状态标记。** **[已落地]** 表示代码在主干上且有测试。**[计划中]** 表示设计与实施计划已定,代码还没写。**[推迟]** 表示明确不在 V1 范围内。图只画目标系统,不标进展,进展只看正文的状态标记。

**六条贯穿全系统的机制。**

| 机制 | 规则 |
|---|---|
| 产物树 | 所有协作数据是 git 树里的 YAML 产物,统一信封:`id`、`type`、`version`、`created_at`、`created_by`(含 `owner` 与 `visibility`)、`status`、`schema_version`、`links`、`payload`。checkpoint 等大体量内容只存引用 `{uri, sha256, size_bytes}`;V1 的数据集逐条 episode 进 git,只适合小规模 mock。git 树是唯一真相,读取时先经迁移器链再消费。 |
| 状态机 | 需审核类:`draft → pending_review → approved → effective`,之后可到 `deprecated` 或 `superseded`,`pending_review` 也可到 `rejected`。非审核类:`pending → executed → consumed`,`pending` 也可到 `failed`。被拒绝与失败的产物保留;被拒绝的候选进入下一轮规划的输入,失败只留痕、等人处理。 |
| 调和循环 | 纯函数 `reconcile(树视图) → 动作列表`,守护进程按间隔轮询并执行动作。循环无状态、动作幂等,进程崩溃后从 git 树续接。子系统之间不直接调用,只读写产物。 |
| 审核门 | 三道人工门:① checkpoint 晋级,② 技能与记忆增量纳入,③ 配方入库。证据不在场就不能批准:门①要诊断报告,门②要验证结果,门③要可复现性校验报告。门②与门③ [已落地]。 |
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
| Evidence grade(证据等级) | 诊断报告的可信等级 `stub`、`mock`、`real`,决定它能否驱动规划与提名;缺标记一律判 `stub`,失败关闭 |
<!-- shared:end context-block -->

## 1. 定位

MindAgent 是 AgentRSI 路径的执行者:不改权重,只让技能库与记忆库变好。

它回答三个问题:部署态智能体哪里做得不对,怎么改,改了是否生效。答案以增量提案交人审,批准后才进正式库。它站在计算层。上游是调和循环,下游是正式技能库与记忆库。它经受限直连接口②向 SocialSimulator 的实时环境服务采集部署经验,经 Actio 建出被部署的实例。

<!-- figure:begin 50-mindagent-context -->
![MindAgent 上下文](diagrams/50-mindagent-context.svg)

**图 1　MindAgent 上下文**　调和循环产生 start_agent_round 与 admit_proposal 两个动作,交给组合根注册的 MindAgent 处理器。MindAgent 读模型与正式库、写轮次与提案,经受限直连②向实时环境服务采集部署经验,用 Actio 建实例,可选经外部推理桥接生成技能策略。门②由人工批准提案。

<details>
<summary>文字版:图中的节点与连线</summary>

**节点**

- factory 组合根:注册两个处理器 / 注入配置与工厂
- ControlPlane:调和循环 · 产物树 / 动作 start_agent_round · admit_proposal
- 门② 人工审核:review · approve · reject / 验证结果在场才可批准
- MindAgent:轮次处理器 · 收录处理器 / 部署 · 分析 · 提案 · 验证 · 打包
- 实时环境服务:SocialSimulator / gRPC 双向流 · 对手人格
- Actio:技能库 · 显式记忆 / 每次响应附激活轨迹
- 外部推理桥接:可选:生成技能策略

**连线**

- factory 组合根 → ControlPlane:注册处理器
- 门② 人工审核 → ControlPlane:改提案状态
- factory 组合根 → MindAgent:注入依赖
- ControlPlane → MindAgent:调用处理器
- MindAgent → ControlPlane:读写产物
- MindAgent ↔ 实时环境服务:② 部署会话
- MindAgent → Actio:instantiate
- MindAgent → 外部推理桥接:可选

</details>
<!-- figure:end 50-mindagent-context -->

调和循环产生 `start_agent_round` 与 `admit_proposal` 两个动作,交给 MindAgent 的两个处理器。factory 组合根在启动时注册处理器,并注入配置与各类工厂。MindAgent 读模型产物与正式库,写轮次产物与提案。部署会话经接口②走 gRPC 双向流,对手由实时环境服务扮演。外部推理桥接是可选的,V1 只用于生成技能策略;验证函数支持可选的 judge 附注,V1 的轮次处理器不装配它。

## 2. 目标、非目标与约束

结论:V1 只做"技能提案 → 行为验证 → 人工审核 → 收录"这一条环路,其余能力推迟。

**目标**

| 编号 | 目标 | 验收方式 |
|---|---|---|
| MA-G1 | 每个新晋级的 effective 模型自动开一轮 agent 轮次 | 轮次产物 `agent-round-<model>` 走到 `executed` 或 `failed` |
| MA-G2 | 从部署 episode 中统计出"选中的技能不针对当前信号"的缺口 | 分析器在构造数据上给出确定的发现,同一批数据重跑同一 id |
| MA-G3 | 每条发现产出一份技能增量草案 | 草案能被 Actio 的技能解析读回 |
| MA-G4 | 用离线重放证明候选技能按预期改变行为(R13) | 提案载荷带 `fired_on_motivating`、`fired_elsewhere` |
| MA-G5 | 批准的提案收录进正式库,库版本加一 | `library_state` 版本递增,`admitted` 记下来源提案 |
| MA-G6 | 两个处理器在任一崩溃点重入都收敛 | 故障注入测试:不重复部署、不重复写提案、不重复递增 |

**非目标与推迟项**

| 项 | 状态 | V1 的处理 |
|---|---|---|
| 记忆增量生成器 | [推迟] | 生成器只产出 `kind: skill`;记忆增量的数据模型与收录路径可用,靠人手写提案 |
| 接入真实生产日志 | [推迟] | 经验只来自模拟部署 |
| 本包自己读树内配置 | [推迟] | 配置由组合根从 `config/agent.yaml` 读出,构造冻结的 `AgentConfig` 后注入;实际取值写进轮次产物 |
| 对话质量评审 | [推迟] | 只度量行为;验证函数支持可选 judge 附注,V1 不装配,也不参与筛选 |
| 留出集验证 | [推迟] | 验证用触发提案的同一批 episode,过拟合说明写进 `notes` |
| AgentRSI 配方自动提名 | [推迟] | 轮次产物记下锚定字段,人工写配方候选 |
| 运行中轮次被新模型打断 | [推迟] | 轮次同步执行,下次触发时才置 superseded |

**质量属性与硬约束**

| 约束 | 内容 | 影响 |
|---|---|---|
| 依赖方向 | 只导入 `controlplane`、`actio`、`inference_bridge`、`simulator`、`grpc`、`yaml` 与标准库 | 失败分类与信号视图规则要在本包另写一份 |
| 反向隔离 | `controlplane` 不导入 `mindagent` | 处理器类型写成字面 `Callable`,不引用调和循环的别名 |
| 产物读写 | 一律经 `GitStore` 与信封 | 写入内容不变时不提交,重入可用 HEAD 不变来判定空操作 |
| 不碰凭证 | 桥接只经注入对象调用 | 测试一律用脚本化桥接 |
| 脱离真实环境可测 | 无 GPU、无外网、无真实模型 | gRPC 用 loopback 服务端,模型后端用罐头实现 |

## 3. 架构

结论:两个处理器,一个驱动五段流水线,一个独立负责收录;三个支撑模块被二者共用。

<!-- figure:begin 50-mindagent-architecture -->
![MindAgent 模块组成](diagrams/50-mindagent-architecture.svg)

**图 2　MindAgent 模块组成**　轮次处理器按编号顺序调用五段流水线,读配置与正式库,失败经统一守卫留痕。收录处理器独立运行,只用正式库与失败守卫。全部模块都在 src/mindagent 下。

<details>
<summary>文字版:图中的节点与连线</summary>

**节点**

- AgentConfig:config · 冻结数据类
- 正式库读取:library · load_library
- 失败守卫:failures · fail_once
- 收录处理器:admit / handle_admit_proposal
- 轮次处理器:rounds · make_start_round_handler 返回 (store, action) 处理器
- 部署驱动器:deployment / 跑部署 episode
- 经验分析器:analysis / 跨 episode 统计
- 提案生成器:proposals / 规则模板或桥接
- 提案验证器:verify / 离线重放度量行为
- 提案打包器:packaging / 筛选并写提案

**连线**

- 轮次处理器 → AgentConfig
- 轮次处理器 → 正式库读取
- 轮次处理器 → 失败守卫
- 收录处理器 → 失败守卫
- 收录处理器 → 正式库读取
- 轮次处理器 → 部署驱动器
- 轮次处理器 → 经验分析器
- 轮次处理器 → 提案生成器
- 轮次处理器 → 提案验证器
- 轮次处理器 → 提案打包器

</details>
<!-- figure:end 50-mindagent-architecture -->

轮次处理器按编号 1 到 5 依次调用部署、分析、提案、验证、打包。它读 `AgentConfig` 与正式库,失败一律经失败守卫留痕。收录处理器不经过流水线,只用正式库读取与失败守卫。全部模块都在 `src/mindagent` 下 [已落地]。

**模块表**

| 模块 | 职责 | 依赖 | 状态 |
|---|---|---|---|
| `config` | 冻结配置 `AgentConfig`,找出 Actio 占的角色位 | `simulator.datamodel` | [已落地] |
| `library` | 读正式库与库版本,推出收录后的条目 id | `actio`、`controlplane.paths` | [已落地] |
| `failures` | 判"读不出"、校验 run id、只记一次失败记录 | `controlplane.ledger` | [已落地] |
| `deployment` | 流水线 1:经接口②跑部署 episode 并落盘 | 实时环境服务客户端、`store_adapter` | [已落地] |
| `analysis` | 流水线 2:跨 episode 统计行为缺口 | `actio.signals`、`actio.triggers` | [已落地] |
| `proposals` | 流水线 3:规则模板或桥接生成技能草案 | `actio.skills`、`inference_bridge` | [已落地] |
| `verify` | 流水线 4:离线重放度量行为改变 | `analysis`、Actio 实例 | [已落地] |
| `packaging` | 流水线 5:按阈值筛选,写提案产物 | `controlplane.paths` | [已落地] |
| `rounds` | 轮次处理器工厂,取代旧轮次 | 以上全部 | [已落地] |
| `admit` | 收录处理器 | `library`、`failures` | [已落地] |
| `errors` | 本包异常:数据错误、解析耗尽、轮次失败 | 无 | [已落地] |

**依赖方向规则**

| 规则 | 内容 |
|---|---|
| 可以导入 | `controlplane`、`actio`、`inference_bridge`、`simulator.datamodel`、`simulator.store_adapter`、`simulator.realtime.client` |
| 不得导入 | `mindtrainer`、`diagnostic`、`recipestore`、`trainplanner`、`trainscheduler`、`factory` |
| 反向 | `controlplane` 不出现 `mindagent`;静态测试强制 [已落地] |

## 4. 数据模型与状态机

结论:MindAgent 写五类产物;轮次走非审核类状态机,提案走审核类状态机。

**实体表**

| 实体 | 字段与含义 | 约束 |
|---|---|---|
| `AgentConfig` [已落地] | 情境清单、角色绑定、每情境 episode 数、重跑上限、分析阈值、验证阈值、解析重试上限 | 冻结;情境非空不重复;绑定恰好一个 `self_play`;比例在 0 到 1 |
| `LibraryState` [已落地] | `skills`、`memories`、`version{skill, memory}` | 只收 `effective` 条目,按 id 排序;版本为十进制数字串 |
| `BehaviorFinding` [已落地] | `id`、`signal`、`pattern`、`support`、`ratio`、`episode_refs` | `id` 由信号与排序后的 episode id 求 sha256 前 12 位 |
| `ProposalDraft` [已落地] | `finding_id`、`increments`、`generator` | V1 只有一个 `kind: skill` 增量 |
| `Verification` [已落地] | `method`、`episodes`、`fired_on_motivating`、`fired_elsewhere`、`notes`、可选 `quality_notes` | `method` 固定为 `replay_behavior` |

| `AgentConfig` 字段 [已落地] | `max_turns` | `episodes_per_scenario` | `max_retries_per_episode` | `min_support` / `min_ratio` | 验证阈值 | `max_parse_retries` |
|---|---|---|---|---|---|---|
| 默认值 | 6 | 1 | 1 | 3 / 0.5 | 0.8 / 0.2 | 2 |

**产物与路径表**(产物树,即 git 仓库里的 `control-plane/` 目录)

| 产物类型 | 路径 | 状态机种类 | 写方 | 状态 |
|---|---|---|---|---|
| `agent_round` | `control-plane/agent_rounds/agent-round-<model>.yaml` | 非审核类 | 轮次处理器 | [已落地] |
| 部署 episode | `control-plane/runs/<run>/episodes/deploy-<model>-<scenario>-<k>.yaml` | 无 | 部署驱动器 | [已落地] |
| `skill_memory_proposal` | `control-plane/skill_memory_proposals/proposal-<round>-<n>/proposal.yaml` | 审核类 | 打包器;人改状态 | [已落地] |
| `skill` / `memory` | `control-plane/skill_library/<id>.yaml`、`control-plane/memory_library/<id>.yaml` | 直接 `effective`;`deprecated` 在 V1 没有自动写方,人工改成它后 `load_library` 不再读到 | 收录处理器 | [已落地] |
| `library_state` | `control-plane/library_state.yaml` | 恒为 `executed` | 收录处理器 | [已落地] |
| 失败记录 | `control-plane/runs/<run>/failure-<task>.yaml` | 无 | 失败守卫 | [已落地] |

**轮次状态**:`pending → executed | failed`。终态再触发是空操作。`failed` 用 `payload.failure.category` 区分三种:`T3` 是真失败,`superseded` 是被新模型取代。

**提案状态**:`pending_review → approved | rejected`,`approved → effective`。审核类状态机里 `approved` 没有通往失败的出口,所以收录失败时提案原样留在 `approved`。

正式库状态产物 `library_state` [已落地]:id 为 `library-state`,类型 `library_state`,状态恒为 `executed`。载荷是 `skill_version`、`memory_version`、`admitted` 三个键,两个版本都是十进制数字串,`admitted` 是收录记录的列表,每条含 `proposal_id`、`skill_version`、`memory_version` 与 `entries`(该次收录写入的条目 id 列表)。信封 `version` 每次收录加一,`links` 是最近一次收录的提案 id。文件不存在时等价于两个版本都是 `"0"`。版本不是十进制数字串(如 `lib-3`、空串、`3.0`、布尔、负数)时读取方抛 `MindAgentDataError`:收录处理器当作确定性失败,轮次处理器判 T3。空库不需要这份产物,预置初始技能时由人手写技能产物与这份产物。

候选技能与桥接输出的形状 [已落地]:规则模板生成的候选技能 id 为 `skill-candidate-<信号名>`,版本 1,层级 `micro`,优先级 100,触发条件是单个信号,指令取模板文本。桥接路径下外部模型只写 `instructions` 与 `description`,必须恰好输出这两个键,多一个键也不合格;不合格带着原因重问,总次数 `1 + max_parse_retries`,耗尽抛 `ProposalParseError`。传了桥接而 `run_id` 为空时,调用桥接之前就抛 `MindAgentDataError`。规则生成器标识 `rule_template.v1`,桥接生成器标识 `bridge.v1`,提示模板版本 `mindagent-skill-v1`。每次尝试的 task id 带尝试序号,所以每次尝试各记一条成本。

`dropped` 有两种条目 [已落地]:被阈值筛掉的草案,含 `finding_id`、原因、两个验证比例;重入后留下的过期提案,含 `proposal_id` 与原因,原因写明它是更早一次重入留下的。

轮次产物载荷 [已落地]:

```yaml
id: agent-round-zengen-v2
type: agent_round
links: [zengen-v2, run-002]
payload:
  model_id: zengen-v2
  model_version: 2
  run_id: run-002
  library_version: {skill: "1", memory: "0"}
  scenario_ids: [s1, s2, s3]
  analyzer_config: {min_support: 3, min_ratio: 0.5}
  verifier_config: {method: replay_behavior, min_fired_on_motivating: 0.8, max_fired_elsewhere: 0.2}
  generator_version: rule_template.v1
  uses_bridge: false
  episode_ids: [deploy-zengen-v2-s1-1, deploy-zengen-v2-s2-1, deploy-zengen-v2-s3-1]
  findings: [...]; proposals: [proposal-agent-round-zengen-v2-1]; dropped: []; failure: null
```

载荷还含 `persona_ids`、`bindings`、`checkpoint_ref`(模型有时)、`prompt_template_version`(仅用桥接时)。重入时整份按当前绑定重写,保留首次的 `created_at`。

**不变量**

| 编号 | 不变量 |
|---|---|
| MA-I1 | 每个模型至多一个轮次产物,id 为 `agent-round-<model>` |
| MA-I2 | 部署 episode id 为 `deploy-<model>-<scenario>-<k>`,`k` 从 1 起,是幂等键 |
| MA-I3 | 提案 id 为 `proposal-<round>-<n>`,`n` 按通过顺序连续编号,被筛掉的不占号 |
| MA-I4 | 收录后的技能 id 为 `skill-<提案 id 去掉 proposal->-<k>`,记忆为 `mem-` 前缀,`k` 是增量位置 |
| MA-I5 | 同一提案在 `admitted` 里至多出现一次,库版本因它至多加一 |
| MA-I6 | 已不是 `pending_review` 的提案不被机器改写 |
| MA-I7 | 同一 `(run, task)` 的失败记录只写一次 |

## 5. 接口契约

结论:对外只有两个处理器入口,形状都是 `Callable[[GitStore, Action], None]`;以下签名 [已落地]。

**入口**

```python
# mindagent.rounds —— 组合根先调工厂,再把返回值注册为 start_agent_round 的处理器
def make_start_round_handler(*, config: AgentConfig, client_factory, backend_factory,
                             personas_loader, bridge=None) -> Callable[[GitStore, Action], None]: ...
# backend_factory(model_id, checkpoint_ref) -> 新的模型后端
# personas_loader(store, persona_ids) -> {persona_id: Persona}

# mindagent.admit —— 直接注册为 admit_proposal 的处理器
def handle_admit_proposal(store: GitStore, action: Action) -> None: ...
```

轮次处理器的 `action.target_id` 是模型 id `zengen-vN`,收录处理器的是提案 id;两者都不读 `detail` [已落地]。

三个注入的工厂 [已落地]:`client_factory()` 零参数,每条会话新建一个客户端;`backend_factory` 取 `model_id` 与 `checkpoint_ref`,每次新建模型后端,部署一份、验证时基线与候选各一份;`base_instance_factory` 取 `extra_skills`,每次返回新的 Actio 实例,传空元组得到基线。部署实例的 `profile` 是 `{model_id, round_id}`。

**流水线函数**

```python
def run_deployment_episodes(store, instance, client_factory, *, run_id, scenario_ids, personas,
                            bindings, model_id, max_turns, episodes_per_scenario=1,
                            max_retries_per_episode=1) -> list[Episode]: ...
def analyze(episodes, *, min_support, min_ratio, skills=(), registry=None,
            signals=None) -> list[BehaviorFinding]: ...
def propose(findings, *, bridge=None, run_id="", task_id="mindagent-propose",
            max_parse_retries=2) -> list[ProposalDraft]: ...
def verify(draft, findings, episodes, *, base_instance_factory, personas, scenarios,
           judge=None, run_id="", task_id="mindagent-judge") -> Verification: ...
def package(store, round_id, findings, drafts, verifications, *, thresholds, run_id,
            bound_model_id) -> list[Envelope]: ...
def load_library(store) -> LibraryState: ...
```

与闭环集成设计签名的偏差:部署驱动器多 `model_id` 等四个参数;分析器多 `skills`、`registry`、`signals`;提案生成器与验证器多记账用的 `run_id`、`task_id`;轮次处理器改成闭包工厂。理由都是统一签名只有 `(store, action)`,其余依赖只能从外面注入。

**触发条件**由调和循环判定 [已落地]:`start_agent_round` 在最新 effective 模型没有轮次产物、轮次 `pending` 或读不出时产生;`admit_proposal` 在存在 `approved` 提案时产生。

**提案载荷:命令行读取的键**

提案载荷是命令行 `review` 与 `approve` 的契约,打包器是唯一写方,键名与层次不得改动。

| 键 | 类型 | 用途 |
|---|---|---|
| `payload.round_id` | str | `review` 首行 |
| `payload.finding.{id, pattern, support, ratio, episode_refs}` | str、str、int、float、list | `review` |
| `payload.increments[].{kind, definition}` | `skill` 或 `memory`、dict | `review` 逐条打印 |
| `payload.verification` | 非空 dict | `approve` 缺它就拒绝 |
| `payload.verification.{method, fired_on_motivating, fired_elsewhere, episodes, notes}` | str、float、float、list、str | `review` |

额外写、命令行不读的键:`run_id`、`bound_model_id`、`generator`、`verification.quality_notes`;`links = [round_id, finding_id]`。

**对邻居的假设**

| 邻居 | 本文依赖它什么 | 它依赖本文什么 |
|---|---|---|
| ControlPlane | 路径函数、`ledger.record_failure`、状态迁移检查、`GitStore` 写入内容不变不提交 [已落地] | 两个处理器的名字、位置与形状;终态由处理器自己写回 |
| Actio | `instantiate`、`respond` 返回三元组、`library_version`、`validate_trace`、`referenced_signals`、内置信号、技能与记忆的 id 前缀 `skill-`、`mem-` [已落地] | 正式库条目的格式;库版本为字符串 |
| SocialSimulator | 实时环境服务客户端的"客户端后说"、`submit_turn` 带激活轨迹、回显自己轮次时保留轨迹、`write_episode` [已落地] | 部署会话的 `purpose` 固定为 `deployment_simulation`;Actio 占 `self_play` 位 |
| 外部推理桥接 | `complete(prompt, *, run_id, task_id)`、`BridgeError` 带 `category` 与 `attempts`、每次成功调用记一条成本 [已落地] | 传入非空 `run_id`;每次尝试用不同 `task_id` |
| factory 组合根 | 注入配置、客户端工厂、模型后端工厂、人格加载器、可选桥接 [已落地] | 按名字 `mindagent.rounds.make_start_round_handler` 取工厂 [已落地] |
| 命令行 | 无 | 上表的提案载荷键 |

## 6. 关键流程

结论:一轮 agent 轮次分 8 步,写 `pending` 产物在部署之前,所以任一步之后崩溃都能从同一处续跑。

<!-- figure:begin 50-mindagent-round-flow -->
![一轮 agent_round 的处理](diagrams/50-mindagent-round-flow.svg)

**图 3　一轮 agent_round 的处理**　调和循环为最新 effective 模型产生 start_agent_round。处理器先判终态,再取代旧轮次,写 pending 轮次产物作幂等键,然后部署、分析、提案、验证、打包,最后置 executed。确定性错误与任何桥接失败置 failed 并记 T3;非桥接瞬态上抛,轮次保持 pending,守护进程退出,重启后的 tick 重入。

<details>
<summary>文字版:图中的节点与连线</summary>

**节点**

- 产生动作:最新 effective 模型 / 轮次缺失或 pending
- 读轮次产物:终态:直接返回 / 读不出:写 failed
- 取代旧轮次:旧模型 pending 轮次 / 过期动作自身同样
- 绑定并写 pending:读模型与正式库 / 轮次产物 = 幂等键
- 部署 episode:已落盘且库版本一致 / 则复用不重跑
- 分析 · 提案 · 验证:纯函数为主 / 桥接仅在提案生成
- 筛选并打包:提案 pending_review / id 由轮次与序号确定
- 轮次 executed:写 findings · proposals / dropped
- 空操作:不写 · 不提交
- 旧轮次 failed:category superseded
- 上抛 · 保持 pending:非桥接瞬态 / 如会话 T1 重跑用尽
- 轮次 failed:T3 失败记录 / failure-agent-<model>

**连线**

- 产生动作 → 读轮次产物
- 读轮次产物 → 取代旧轮次
- 取代旧轮次 → 绑定并写 pending
- 绑定并写 pending → 部署 episode
- 部署 episode → 分析 · 提案 · 验证
- 分析 · 提案 · 验证 → 筛选并打包
- 筛选并打包 → 轮次 executed
- 读轮次产物 → 空操作:已终态
- 取代旧轮次 → 旧轮次 failed:被取代
- 部署 episode → 上抛 · 保持 pending:瞬态
- 分析 · 提案 · 验证 → 轮次 failed:确定性 / 桥接
- 部署 episode → 轮次 failed:不可重跑
- 上抛 · 保持 pending → 产生动作:下一 tick 重入(R19)(虚线)

</details>
<!-- figure:end 50-mindagent-round-flow -->

调和循环为最新 effective 模型产生动作,处理器先看轮次是否已在终态。随后取代旧模型的 `pending` 轮次,绑定模型与正式库并写 `pending`。部署、分析、提案、验证、打包依次执行,最后置 `executed`。红色是两条失败出口,虚线是 R19 的重入。

**轮次处理步骤表**(与图中编号一一对应)

| 步 | 名称 | 做什么 | 出口 |
|---|---|---|---|
| 1 | 产生动作 | 调和循环发现最新 effective 模型缺轮次或轮次 `pending`,产生 `start_agent_round` | 进入 2 |
| 2 | 读轮次产物 | 终态直接返回;文件在但读不出,整份重写为 `failed`(T3) | 已终态:空操作 |
| 3 | 取代旧轮次 | 版本低于最新 effective 的 `pending` 轮次置 `failed`,类别 `superseded`;过期动作本身同样处理 | 被取代:不记失败记录 |
| 4 | 绑定并写 pending | 读模型产物(须 `effective`、`links` 至少两项)与正式库,写 `pending` 轮次产物 | 模型不合法:`failed`,无失败记录 |
| 5 | 部署 episode | 已落盘且每个 agent 轮次轨迹库版本一致的 episode 直接复用,其余新建会话 | 瞬态:上抛;不可重跑:`failed` |
| 6 | 分析 · 提案 · 验证 | 求发现,出草案,离线重放;桥接只出现在提案生成 | 确定性错误或任何桥接失败:`failed` |
| 7 | 筛选并打包 | 按阈值筛选,通过的写 `pending_review` 提案 | 被筛掉的记进 `dropped` |
| 8 | 轮次 executed | 写 `episode_ids`、`findings`、`proposals`、`dropped`,置 `executed` | 终态 |

**部署会话的失败分类**(第 5 步,[已落地])

| 终止原因或状态 | 处理 |
|---|---|
| `max_turns`、`goal_reached` | 接受,落盘 |
| `timeout`、`role_backend_timeout`、`persistence_failed`、传输层 `UNAVAILABLE` | T1:整条重跑,至多 `max_retries_per_episode` 次;用尽后轮次保持 `pending` 并上抛 |
| `invalid_session`、`role_backend_failure`、`client_disconnected`、其他状态码、协议错乱 | 不重跑,轮次 `failed`(T3) |
| Actio 实例自身抛异常 | 先 `abort()` 会话,原异常原样上抛,不重跑、不包装 |

**分析器的判定**(第 6 步,[已落地])

| 项 | 规则 |
|---|---|
| 信号求值 | 按 agent 每次发言前能看到的历史重新求值,不读轨迹里记下的信号;默认只看 `emotion_high`、`no_new_information` |
| 受影响 | 某 agent 轮次信号为真、`selected` 非空、选中技能的触发条件不引用该信号;奇数层 `not` 包住的不算引用 |
| 出发现 | `ratio = 受影响数 / 有 agent 轮次的 episode 数`;`support ≥ min_support` 且 `ratio ≥ min_ratio` |

**验证器的口径**(第 6 步,[已落地])

| 量 | 定义 |
|---|---|
| 重放对象 | 发现引用的动机 episode;对手原话照抄,agent 轮次由实例重新生成 |
| 基线实例 | 当前库 |
| 候选实例 | 当前库加草案技能(优先级 100) |
| 动机轮次 | 重放中该信号在 agent 发言前为真的 agent 轮次 |
| `fired_on_motivating` | 候选在动机轮次上选中草案技能的比例,分母为 0 记 0.0 |
| `fired_elsewhere` | 候选在动机 episode 其余 agent 轮次上选中草案技能的比例 |

**提案到收录**

<!-- figure:begin 50-mindagent-admission -->
![提案到入库的审核链](diagrams/50-mindagent-admission.svg)

**图 4　提案到入库的审核链**　验证结果先按阈值筛选,未过的写进轮次的 dropped。通过的打包成 pending_review 提案,人工在门②批准或拒绝。批准后收录处理器先校验全部增量,再写库条目、递增 library_state,最后把提案置 effective。增量不合法时记一次 T2 失败记录,提案留在 approved。

<details>
<summary>文字版:图中的节点与连线</summary>

**节点**

- 验证结果:fired_on_motivating / fired_elsewhere
- 阈值筛选:≥ 0.8 且 ≤ 0.2 才通过
- 提案 pending_review:skill_memory_proposal / 带 verification
- 门② 人工审核:review 看证据 / approve / reject
- dropped 记录:写进轮次产物
- 提案 approved:调和循环产生 / admit_proposal
- 校验全部增量:Skill / MemoryEntry / 不合法则一条不写
- 写库条目:skill_library/ / memory_library/
- library_state:对应版本 +1 / admitted 追加一条
- 提案 effective:收录完成
- T2 失败记录:提案留在 approved / failure-admit-<提案>
- 提案 rejected:保留记录

**连线**

- 验证结果 → 阈值筛选
- 阈值筛选 → 提案 pending_review:通过
- 阈值筛选 → dropped 记录:未通过
- 提案 pending_review → 门② 人工审核
- 门② 人工审核 → 提案 approved:approve
- 门② 人工审核 → 提案 rejected:reject
- 提案 approved → 校验全部增量
- 校验全部增量 → 写库条目
- 写库条目 → library_state
- library_state → 提案 effective
- 校验全部增量 → T2 失败记录:不合法

</details>
<!-- figure:end 50-mindagent-admission -->

验证结果先过阈值,通过的成为 `pending_review` 提案,人在审核门②看证据后批准或拒绝。批准后收录处理器按编号 1 到 4 执行。每一步都可重入:条目已在就跳过,`admitted` 已记就不再递增。

| 步 | 名称 | 做什么 | 重入时 |
|---|---|---|---|
| 1 | 校验全部增量 | 每个增量按技能或记忆格式解析;任一不合法则一条不写 | 再校验一次 |
| 2 | 写库条目 | 按增量顺序写 `skill_library/`、`memory_library/`,状态 `effective`,`links = [提案 id]` | 已存在的条目跳过 |
| 3 | 更新 `library_state` | 有技能增量时技能版本加一,有记忆增量时记忆版本加一,追加一条 `admitted` | `admitted` 已有该提案则跳过 |
| 4 | 提案 effective | `approved → effective` | 已 `effective`:空操作 |

**失败与恢复路径**

| 情形 | 结果 | 恢复 |
|---|---|---|
| 部署会话 T1 重跑用尽 | 上抛,轮次 `pending`,守护进程退出 | 外部监督者重启后的 tick 重入,复用已落盘 episode,只补跑缺的会话 |
| 打包后、置 `executed` 前崩溃 | 轮次 `pending`,提案已写 | 重入得到同一提案 id,载荷相同不提交 |
| 重入时通过的草案变少 | 编号超出本次数目的旧提案仍为 `pending_review` | 写进 `dropped`,标为 stale,审核前核对 |
| 收录在第 2、3、4 步之间崩溃 | 提案仍 `approved` | 重入收敛:版本只加一,条目只一份 |
| 增量不合法或库版本非数字 | T2 失败记录 `failure-admit-<提案>`,提案留在 `approved` | 人改提案;此后每次触发只读不写 |

**走查例子**

数值取自工作包 7 的端到端测试,与 `tests/test_ma_e2e.py` 一致。配置:情境 `s1`、`s2`、`s3`,`max_turns=4`,每情境 1 个 episode,对手依次说"我真的很生气!!""好吧,你说说看""那就这样吧"。

| 轮 | 前置 | 结果 |
|---|---|---|
| 一 | `zengen-v1` 晋级,库为空 | 3 个 episode、6 个 agent 轮次,`selected` 全为 `None`;零发现、零提案;轮次 `executed` |
| — | 人放基线技能 `skill-goal`(不处理情绪),库版本 `"1"` | — |
| 二 | `zengen-v2` 晋级 | 一条 `emotion_high` 发现,`support=3`、`ratio=1.0`;提案 `proposal-agent-round-zengen-v2-1`,验证 1.0 / 0.0 |
| 门② | 人把提案置 `approved` | 收录 `skill-agent-round-zengen-v2-1-1`,库版本变 `{"skill": "2", "memory": "0"}` |
| 三 | `zengen-v3` 晋级 | 对方怒语后的轮次选中新技能,`candidates` 为新技能与 `skill-goal`,`conflict` 非空;其余轮次仍选 `skill-goal`;零新发现 |

第二轮 3 个 episode 都受影响,`ratio = 3 / 3 = 1.0`。第一轮库为空,没有 `selected`,不受影响。

## 7. 关键决策

结论:决策的共同取向是"宁可停在 `failed` 等人,也不让守护循环空转";幂等键全部由确定性 id 承担。

| 编号 | 决策 | 备选 | 理由 | 判错的代价 |
|---|---|---|---|---|
| R11 | 正式技能库放 `skill_library/`,记忆库放 `memory_library/`,版本记在 `library_state.yaml` | 技能与记忆混放在提案目录 | 库是版本化产物,要经审核门进入产物树 | 无 |
| R13 | 验证只度量"行为是否按预期改变",质量评审可选接 judge | 用对话质量打分筛选 | 质量评分没有可靠来源;行为改变可确定地重放 | 审核人看到的是行为证据,不是质量证据 |
| R18 | 任何 `BridgeError` [已落地] 都让轮次 `failed` 并记 T3;非桥接瞬态原样上抛 | 桥接 T1 也留 `pending` | 桥接自己已退避重试,留 `pending` 会让守护循环无限空转外部推理 | 外部工具短暂不可用也让这一轮 `failed`,要人删轮次产物重开 |
| R19 | `start_agent_round` 在"无轮次或轮次 `pending`"时都触发,处理器负责写终态,重入必须幂等 | 只在无轮次时触发 | 否则瞬态失败或进程被杀留下的 `pending` 永不再派发 | 处理器会被连续唤醒,全靠幂等键挡重复长任务 |
| MA-1 | 部署 episode 由驱动器用客户端重建的 Episode 改 id 落盘 | 服务端关闭钩子落盘 | 钩子拿不到模型 id、情境内序号与 run;确定性 id 就是幂等键 | 落盘 episode 没有对手内心独白;要它得改成服务端按会话 id 关联 |
| MA-2 | 已落盘 episode 只在每个 agent 轮次轨迹的库版本与当前实例一致时复用 | 有就复用 | 两次重入之间若有收录,旧 episode 会把已修好的缺口再报一遍 | 库在轮次中途变化时多跑一遍部署 |
| MA-3 | 配置是注入的冻结数据类,实际取值写进轮次产物 | 本包自己读树内配置 | 本包不读树,`config/agent.yaml` 由组合根读出并注入;轮次产物里的取值足够复现 | 改阈值要重启守护进程 |
| MA-4 | 缺口定义为"选中的技能不针对该信号";`selected` 为空不算 | 没有选中也算缺口 | 没有路由谈不上路由错 | 空库永远长不出第一条技能,要人放 |
| MA-5 | 候选技能触发条件、层级、优先级由发现决定,桥接只写指导语与描述 | 外部模型写完整技能 | 否则无法保证候选针对这个信号 | 库里有优先级高于 100 的同类技能时,候选会被筛掉 |
| MA-6 | 桥接输出严格解析,失败把原因拼进下一次提示,总次数 `1 + max_parse_retries` | 宽松解析 | 严格解析才能保证外部模型只改指导语 | 外部模型输出不稳时整轮 `failed` |
| MA-7 | 每次桥接尝试用 `<task_id>-<i>-attempt<n>` | 复用同一 task_id | 账本对同一 `(task_id, cost_type)` 只记一次,复用会丢成本 | 重入时编号从 1 重来,重跑成本被去重 |
| MA-8 | 打包按阈值 0.8 / 0.2 筛选,两端含等号,审过的提案不被改写 | 全部提案交给人 | 减少人审负担;人的决定不能被机器覆盖 | 阈值没有数据支持,误筛时有效提案进不了审核 |
| MA-9 | 收录先校验全部增量,再写条目、递增版本、改状态,`admitted` 作幂等键 | 边校验边写 | 半写入的库无法恢复;崩溃后重跑收敛 | 一条坏提案让 `admit_proposal` 每个 tick 空转 |
| MA-10 | 收录的确定性错误记 T2,轮次的记 T3 | 统一记 T3 | T2 表示处理器空操作只记一次;T3 表示闭环停在 `failed` 等人 | 只影响留痕分类,不改行为 |
| MA-11 | 库版本必须是十进制数字串 | 允许任意字符串标识 | 收录要按整数加一;不在入口挡住会在 `int()` 处崩守护进程 | 想用非数字版本标识要改校验与递增规则 |
| MA-12 | 轮次产物读不出时处理器自己重写为 `failed`,不抛 | 抛出由人处理 | 抛出会让守护进程退出、重启再触发,形成崩溃循环 | 人手改坏的轮次产物被整份覆盖,原文只在 git 历史里 |
| MA-13 | 写失败记录前校验 run id 为单个路径段且 run 目录存在 | 直接写 | run id 来自树内数据;含斜杠会抛错,不存在会凭空建目录 | 这类失败只在产物上可见,观测命令看不到 |

## 8. 失败处理与可观测

结论:轮次的确定性失败都是 T3;收录的确定性失败是 T2;只有非桥接的瞬态是 T1,靠上抛加 R19 重入恢复。

| 失败点 | 类 | 处理 | 留痕产物 |
|---|---|---|---|
| 部署会话超时、后端超时、传输不可用 | T1 | 整条重跑;用尽后上抛,轮次 `pending`,守护进程退出 | 无;重启后的 tick 重入 |
| 人格加载器等非桥接异常 | T1 | 原样上抛,轮次 `pending` | 无 |
| 不可重跑的会话失败、会话被拒 | T3 | 轮次 `failed` | `failure-agent-<model>.yaml` |
| 情境或人格不在树里、库条目不合法、轨迹引用库外技能 | T3 | 轮次 `failed`,不建会话或中止 | `failure-agent-<model>.yaml` |
| `BridgeError` [已落地](T1 已用尽或 T2) | T3 | 轮次 `failed`,`retries = attempts - 1` | `failure-agent-<model>.yaml` |
| 桥接输出解析耗尽 | T3 | 轮次 `failed`,`retries` 为已重试次数 | `failure-agent-<model>.yaml` |
| 轮次产物读不出 | T3 | 整份重写为 `failed` | `failure-agent-<model>.yaml`(run 可得时) |
| 模型产物缺失、非 `effective`、`links` 不足 | T3 | 轮次 `failed` | 只在轮次产物 `payload.failure` 上 |
| 旧模型的 `pending` 轮次 | superseded | 置 `failed` | 只在轮次产物上 |
| 提案增量不合法、库版本非数字 | T2 | 提案留在 `approved`,库不变 | `failure-admit-<提案>.yaml` |
| run id 坏掉或 run 目录不存在 | — | 不写失败记录 | 只在轮次产物或提案上 |

轮次的失败记录挂在绑定模型所在 run 下,task id 为 `agent-<model>`。收录的失败记录挂在提案载荷里的 `run_id` 下,task id 为 `admit-<提案 id>` [已落地]。

本包的异常 [已落地]:`errors` 模块里有 `MindAgentDataError`(数据不合法的基类,库版本不是十进制数字串也抛它)、`ProposalParseError`、`AgentRoundFailedError`;`deployment` 模块里有 `DeploymentFailedError`。

**只读观测手段**

| 手段 | 看什么 |
|---|---|
| 轮次产物 | 绑定的模型与库版本、用过的配置、`findings`、`proposals`、`dropped`、`failure` |
| 命令行 `review` | 提案的发现、增量、验证比例与 `notes` |
| 部署 episode | 每个 agent 轮次的激活轨迹:信号、候选、选中技能、冲突、库版本 |
| 失败记录 | 失败点、类别、重试次数、原因 |
| 成本台账 | 每次桥接尝试一条 `external_inference` |

## 9. 测试策略

结论:全部测试脱离真实环境可跑,gRPC 用真实 loopback 服务端,桥接用脚本化替身;共 152 个用例。

| 层次 | 对象 | 替身 |
|---|---|---|
| 单元 | 配置校验、库读取、分析、提案、验证、打包 | 罐头模型后端、构造的 Episode |
| 组件 | 部署驱动器与两个处理器 | 真实 loopback 服务端、替身客户端、`ScriptedBridge` [已落地]、故障注入 |
| 端到端 | 三轮 AgentRSI 环路加一次收录 | 真实 loopback 服务端,对手为脚本化后端 |
| 静态 | 依赖方向、`__future__` 首行 | 无 |

**用例数**

| 测试文件 | 用例数 | 覆盖 |
|---|---|---|
| `tests/test_ma_config_library.py` [已落地] | 27 | 配置校验、库版本 8 个反例、静态检查 |
| `tests/test_ma_deployment.py` [已落地] | 19 | 落盘 id、轨迹校验、复用、T1 重跑、实例异常 |
| `tests/test_ma_analysis.py` [已落地] | 19 | 判定、阈值等号、`not` 包裹、库外技能 |
| `tests/test_ma_proposals.py` [已落地] | 20 | 规则模板、严格解析、重试、成本条数 |
| `tests/test_ma_verify.py` [已落地] | 8 | 精准命中、触发过宽、优先级压不过 |
| `tests/test_ma_packaging.py` [已落地] | 8 | 筛选、编号、重入不改写 |
| `tests/test_ma_admit.py` [已落地] | 22 | 三个崩溃点、T2、5 种坏 run id、记忆增量 |
| `tests/test_ma_rounds.py` [已落地] | 28 | 空库沉默、R18、R19 重入、取代、读不出、过期提案 |
| `tests/test_ma_e2e.py` [已落地] | 1 | 第 6 章走查例子 |

服务端夹具沿用 `tests/test_mt_trajectory_interact.py` 的搭法。gRPC 测试给每条流加 15 秒 deadline,收尾等服务端停止,防止挂住套件。

**不依赖的外部服务**:GPU、外部网络、真实模型 checkpoint、真实外部推理工具、远程计算服务器。

## 10. 现状、局限与未核实项

结论:`src/mindagent` 十一个模块 [已落地],两个处理器由组合根按 `--enable agent,admit` 装配,并经 `reconcile_once` 端到端跑通;闭环里的模型后端是罐头实现,不加载 checkpoint,轮次产物也不记后端来源。

**能力现状**

| 能力 | 状态 | 说明 |
|---|---|---|
| 实时环境服务的 `deployment_simulation` 用途 | [已落地] | 数据模型与协议已有该取值 |
| 激活轨迹字段 | [已落地] | `TurnRecord.activation_trace` 已存在,运行器接受后端返回的三元组并写进该轮 |
| `submit_turn` 携带激活轨迹 | [已落地] | 工作包 6 |
| 产物路径函数与失败台账 | [已落地] | 工作包 1 |
| 部署、分析、提案、验证、打包 | [已落地] | 工作包 7 |
| 轮次处理器与收录处理器 | [已落地] | 工作包 7 |
| 调和循环触发与注入、组合根装配与端到端 | [已落地] | 工作包 8、工作包 9 |

**三条必须知道的设计取舍**

| 取舍 | 内容 |
|---|---|
| 版本绑定策略 | 每轮绑定"最新 effective 模型 + 当前正式库",绑定值写进轮次产物。旧模型的 `pending` 轮次被置 superseded。已落盘 episode 只在库版本一致时复用。正在跑的轮次不会被新模型中途打断 |
| 第一轮沉默是预期行为 | 正式库没有任何技能时,轨迹 `selected` 恒为 `None`,分析器不产出发现,轮次 `executed` 且零提案、无失败记录。第一条技能要人放 |
| AgentRSI 配方不自动提名 | 配方提名只由训练 run 的候选触发,轮次结果没有对应触发。`recipe_type: agent_rsi` 只在校验与检索里存在。需要时人工写配方候选,锚定字段从轮次产物取,须含 `uses_bridge` |

**已知局限**

| 局限 | 影响 | 缓解 |
|---|---|---|
| 轮次同步执行 | 一轮期间调和循环不转 | 部署 episode 数与轮数由配置控制 |
| 瞬态上抛让守护进程退出 | 外部服务一直不可用时每次启动在同处退出 | 外部进程管理;重启后按 R19 续跑 |
| 验证无留出集 | `fired_elsewhere` 低估误触发 | `notes` 写明过拟合说明 |
| 候选优先级固定 100 | 高优先级同类技能存在时候选被筛掉 | 记进 `dropped`,人调优先级 |
| 默认只分析两个信号 | 新注册信号不被分析 | 经 `signals` 参数显式传入 |
| 重入时外部推理成本被去重 | 成本被低估 | V1 接受;修法是 task id 加重入序号 |
| 库条目、模型、提案读不出时不兜底 | 守护进程退出 | 与现有读取层一致;只有轮次产物有兜底 |
| 过期提案仍为 `pending_review` | 审核人会误批 | 审核前核对 `dropped` 与 `round_id` |

**未核实项**

| 项 | 影响 | 核实办法 |
|---|---|---|
| 累计通过数 | 九个测试文件共 152 个用例,数目已按 `--collect-only` 实测;在主干上的全量通过数没有单独记录 | 在主干跑全量测试取基线,逐任务核对增量 |
| gRPC 用例稳定性 | 只在 macOS 上连跑三次 | 在 Linux 与 CI 上重复运行三个 gRPC 测试文件 |
| 外部模型能否稳定只输出一个 JSON 对象 | 解析耗尽频繁时轮次大量 `failed` | 接真实外部工具后统计解析失败率 |
| 真实模型后端按 `checkpoint_ref` 加载并支持带指导语响应 | 部署与验证无法接真实模型 | 计算层接入时用真实 checkpoint 跑一轮 |
| 提案生成走本地的外部推理桥接,而 MindAgent 属于计算层 | 整体放到计算层需要把外部命令行的登录态与凭证带过去,与「凭证只留在控制层」冲突 | 部署时定下提案生成留在控制层,或为跨层调用设计接口 |
| 信号词典与阈值 | 0.8 / 0.2、`min_support=3`、`min_ratio=0.5`、优先级 100 都是拟定值 | 积累部署 episode 后回看误报与漏报 |
| 处理器经调和循环注入 | 只在 mock 训练与罐头模型后端下跑通 | 工作包 9 的端到端经 `reconcile_once` 跑通;真实执行器与闭环处理器暂不能同时启用(`--executor roll` 与 `--enable` 互斥) |
| 生产客户端没有流 deadline | 服务端失联时轮次挂住 | 组合根用 `DeadlineStub` 给每条流加 15 秒 deadline,不改生产客户端 [已落地];失联场景未压测 |

**明确不做**:不改模型权重;不在运行中打断轮次;不从生产日志采集经验;不自动生成记忆增量;不自动提名 AgentRSI 配方;不让机器改写人审过的提案。

## 附 本文术语

| 术语 | 含义 |
|---|---|
| agent 轮次 | 绑定一个晋级模型的一次完整 MindAgent 运行,对应一份 `agent_round` 产物 |
| 部署 episode | 部署驱动器经接口②跑出、以 `deploy-` 开头落盘的会话记录 |
| 行为缺口 | 某信号为真时,选中的技能触发条件不引用该信号 |
| 发现 | 分析器对一个信号统计出的缺口,含支持数与占比 |
| 草案 | 一条发现对应的增量,尚未验证 |
| 增量 | 提案里的一条技能或记忆定义,`kind` 为 `skill` 或 `memory` |
| 动机 episode / 动机轮次 | 发现引用的 episode;其中信号在 agent 发言前为真的轮次 |
| 正式库 | `skill_library/` 与 `memory_library/` 下状态为 `effective` 的条目 |
| 库版本 | `library_state` 里技能库与记忆库的十进制数字串版本 |
| superseded | 轮次因更新的模型晋级而被取代,记在 `failure.category` |
| 过期提案 | 重入后编号超出本次通过数、仍为 `pending_review` 的旧提案 |
