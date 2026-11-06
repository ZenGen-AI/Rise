# 诊断评测:给候选 checkpoint 打带置信区间的分并标出退化

| 项 | 内容 |
|---|---|
| 一句话 | 对候选 checkpoint 跑静态轨与动态轨评测,逐层聚合成带置信区间的维度分,对照基线标出退化,写成诊断报告 |
| 状态 | [已落地]:评测流水线、编排器、评估器与资产读写、SoMBench 题库导入、真实 SoMEval 运行器、训练行污染准入检查。真机只验证到预测阶段,判分阶段与真实裁判未在真机上跑通,见「现状」。 |
| 所在层 | 计算层,属于 SocialSimulator 的评测部分 |
| 代码位置 | `src/diagnostic`,含评估器 `evaluator.py` 与资产 `assets.py`;污染索引与准入检查 `src/diagnostic/contamination.py`;真实运行器、导入命令与评测配置在 `src/factory`(`sombench.py`、`sombench_runner.py`、`eval_config.py`) |
| 基线提交 | `b826440`(2026-10-21) |
| 依赖 | ControlPlane 的产物树与信封(`controlplane.gitstore`、`controlplane.envelope`、`controlplane.paths`);SocialSimulator 的仿真运行器(`simulator.runner`) |
| 被依赖 | ControlPlane 审核门①(读报告);factory 组合根(装配评估器与真实运行器)[已落地];RecipeStore 增益与战绩、TrainScheduler 规划上下文 [已落地] |
| 对应实施计划 | 评估器与资产:工作包 8、9 [已落地];远端推理服务组件:计算层计划 [已落地];SoMBench 评测接入与污染准入检查:九任务计划 [已落地],其中第 9 项真机验证只完成了一部分 |

## 0. 系统速览

<!-- shared:begin context-block -->
**系统是什么。** Rise(Recursive Interactive Social mind Evolution)是一套"AI 驱动的社会心智后训练工厂":输入通用开源基础模型,输出社会心智能力显著增强的模型,代号 **ZenGen**。工厂自己决定每一轮练什么、怎么练。训练决策可以交给外部更强的闭源模型(经命令行调用),关键决策点设人工审核门,其余环节自动。

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

共享基础设施有五个。**Actio** 是带技能库与显式记忆的智能体框架,每次响应附带激活轨迹;记忆由内嵌的 **Starling** 提供,可选 extra `memory`,一个模拟世界一个树外 SQLite 库,人格是其中的主体(部署一个情境一个世界,训练一条 rollout 一个世界)[已落地]。**外部推理桥接**用命令行调用外部强模型。**远端计算层** `compute` 经 SSH 在远端 GPU 主机上跑作业 [已落地]。**ROLL** 是分布式训练引擎,**vLLM** 是推理引擎。V1 经远端计算层接入 ROLL 的 LoRA SFT [已落地](由 `--executor roll` 装配,已在真实主机上从指令跑到候选一次)与 vLLM 推理服务 [已落地](命令拼装、SSH 隧道、服务生命周期;真机端到端待补)。已落地部分仍由确定性 mock 顶替。真实 GRPO 加 OPD 训练与真实 judge [推迟]。

**状态标记。** **[已落地]** 表示代码在主干上且有测试。**[计划中]** 表示设计与实施计划已定,代码还没写。**[推迟]** 表示明确不在 V1 范围内。图只画目标系统,不标进展,进展只看正文的状态标记。

**六条贯穿全系统的机制。**

| 机制 | 规则 |
|---|---|
| 产物树 | 所有协作数据是 git 树里的 YAML 产物,统一信封:`id`、`type`、`version`、`created_at`、`created_by`(含 `owner` 与 `visibility`)、`status`、`schema_version`、`links`、`payload`。checkpoint 等大体量内容只存引用 `{uri, sha256, size_bytes}`;V1 的数据集逐条 episode 进 git,只适合小规模 mock。git 树是唯一真相,读取时先经迁移器链再消费。 |
| 状态机 | 需审核类:`draft → pending_review → approved → effective`,之后可到 `deprecated` 或 `superseded`,`pending_review` 也可到 `rejected`,`approved` 只在门② 记忆收录一条都没提交成时到 `rejected`。非审核类:`pending → executed → consumed`,`pending` 也可到 `failed`。被拒绝与失败的产物保留;被拒绝的候选进入下一轮规划的输入,失败只留痕、等人处理。 |
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

诊断评测是工厂的导航仪:它回答"这个候选 checkpoint 在每个能力维度上多强,比基座退步了没有"。审核门①靠它决定是否晋级。规划下一轮练什么、配方战绩好不好,也都读它的报告。它只产出分数、错误画像和退化标记,不做任何晋级或规划决定。

<!-- figure:begin 21-diagnostic-context -->
![诊断评测的上下文](diagrams/21-diagnostic-context.svg)

**图 1　诊断评测的上下文**　ControlPlane 在候选待审时请求诊断,由注入的评估器调用诊断评测并写报告。诊断评测调 SoMEval 运行器与 judge 后端,动态轨复用 SocialSimulator 的运行器。报告写进产物树,由审核门①、规划与配方增益读取。

<details>
<summary>文字版:图中的节点与连线</summary>

**节点**

- TrainScheduler:装配规划上下文
- RecipeStore:增益与战绩
- 审核门①:review 打印报告 / approve 要求报告在场
- ControlPlane:调和循环 · request_diagnostic
- 产物树:runs/<run>/diagnostic_report.yaml
- SocialSimulator 运行器:run_episode · 多轮驱动
- 诊断评测:src/diagnostic / 双轨评测 · 聚合 · 退化判定
- 情境生成器:训练情境批量生成
- SoMEval 运行器:SoMEvalRunner 协议
- judge 后端:JudgeBackend 协议

**连线**

- 产物树 → TrainScheduler:读分数与退化
- 产物树 → RecipeStore:读 L1 分
- 产物树 → 审核门①:读报告
- ControlPlane → 诊断评测:request_diagnostic
- 诊断评测 → 产物树:写报告
- 诊断评测 → SocialSimulator 运行器:动态轨
- 情境生成器 → 诊断评测:隔离校验
- 诊断评测 → SoMEval 运行器:静态轨
- 诊断评测 → judge 后端:judge 打分

</details>
<!-- figure:end 21-diagnostic-context -->

默认入口里守护进程遇到 `request_diagnostic` 时,写的是 `simulator.stubs` 的占位报告,不调用编排器;闭环模式下换成 mock 评估器,`--evaluator someval` 换成真实评估器。评测执行经两个协议注入:SoMEval 运行器负责静态轨,judge 后端负责动态轨打分,V1 都注入 mock。报告写进 `runs/<run>/diagnostic_report.yaml`,下游只读产物,不调用诊断代码。

## 2. 目标、非目标与约束

诊断评测保证分数带不确定性、两轨分数留痕、同版本才比较。静态轨经真实 SoMEval 评测 SoMBench 全部四种题型;动态轨与真实 judge 后端的接入不在 V1。SoMBench 的题目只用于评测诊断,不得以任何形式进入训练。

| 编号 | 目标 | 验收方式 | 状态 |
|---|---|---|---|
| G1 | 静态轨:按档选题,经 SoMEval 运行器取每题 k 个回答,规则式重打分 | `tests/test_diag_someval_adapter.py`、`tests/test_diag_orchestrator.py` | [已落地] |
| G2 | 动态轨:跑多轮 episode,judge 给候选话语打分 | `tests/test_diag_dynamic_track.py`、`tests/test_diag_orchestrator.py` | [已落地] |
| G3 | 按变体族通过模式归类错误类型 | `tests/test_diag_error_classifier.py` | [已落地] |
| G4 | L3 到 L2 到 L1 逐层聚合,每个分数带 Wilson 置信区间与轨道标注 | `tests/test_diag_aggregator.py`、`tests/test_diag_stats.py` | [已落地] |
| G5 | 对照基线,只在区间不相交时标退化 | `tests/test_diag_regression.py` | [已落地] |
| G6 | 评估器从产物树读资产与基线,报告带 `backends` 标记 | `tests/test_diag_evaluator.py`、`tests/test_diag_assets.py` | [已落地] |
| G7 | 静态轨以 SoMBench 为题库:3481 题、题型 Q1 到 Q4 全部评测,打分委托给 SoMEval,每题采样一次 | `tests/test_factory_sombench_import.py`、`tests/test_factory_sombench_runner.py`、`tests/test_diag_someval_scored.py`;真实主机上端到端一次 | [已落地],真机待补 |
| G8 | SoMBench 的题目不进训练:树内资产不含题面,SoMEval 的工作目录在树外,训练行上传前过污染准入检查,命中即整条指令失败 | `tests/test_diag_contamination.py`、`tests/test_mt_roll_gate.py`、`tests/test_factory_wire_someval.py` 与树扫描测试 | [已落地] |

| 非目标或推迟项 | 状态 | V1 的处理 |
|---|---|---|
| 真实 SoMEval 子进程,经远端 vLLM 服务推理 | [已落地],未在真机验证 | 由 `--evaluator someval` 装配(DX-22);默认装配仍注入 `MockSoMEvalRunner`,报告标 `backends: mock` |
| 动态轨的真实 judge 后端 | [推迟] | 注入 `MockJudgeBackend`。静态轨不调用它,所以真实 SoMEval 的报告等级是 `real`,见 DX-22;若有动态 spec 带 mock judge,报告等级仍是 `mock` |
| 评测基座模型本身 | [推迟] | 没有基座的诊断报告,首个候选没有基线,不产生退化标记 |
| 每题多次采样 | [推迟] | `sampling_k` 固定为 1(DX-17) |
| 按档选题 | [推迟] | 真实运行器只支持 `full`,`quick` 在装配期被拒(DX-16) |
| 评估器跑动态轨 | [推迟] | 评估器传空的动态 spec,只跑静态轨;没有情境与绑定的来源 |
| G 组探针与 reward hacking 检测进报告 | [推迟] | 检测函数已实现,编排器不调用,报告里只有社会能力退化 |
| 一致性信号 | [推迟] | 报告里恒为空映射与 0.0 |
| 隔离校验器接入情境生成 | [推迟] | 旧的词元相似度函数已实现并独立暴露,没有调用方。它按空白切词,对中文无效:一篇 664 字的 SoMBench 故事只算 1 个词元,删掉一句其余逐字相同的近似拷贝相似度是 0.0。SoMBench 的隔离不靠它,靠 G8 的哈希准入检查 |
| 晋级决定 | 非目标 | 只产标记与证据,决定归审核门①的人 |

| 约束 | 内容 | 影响 |
|---|---|---|
| 分数必带不确定性 | 每个维度分带 95% Wilson 区间与样本量 | 区间重叠的下降不算退化 |
| 两轨不可无痕合并 | 每个分数标 `static`、`dynamic` 或 `fused` | 退化比较按轨道配对 |
| 跨版本不可比 | 报告带 `benchmark_version`,基线版本不同就不比较 | 换版本后第一轮没有退化标记 |
| judge 不能是候选的近亲 | judge 与候选的模型 id 互为前缀即拒绝 | 构造 judge 打分器时抛 `SelfEvalBiasError` |
| 不需要 GPU 与外部服务 | 执行与打分全经协议注入 | 全部测试用固定输出跑 |
| 不导入 MindTrainer | `diagnostic` 不导入 `mindtrainer` | 训练奖励拿不到,reward hacking 检测无输入 |
| 评测题不进训练 | SoMBench 的题面、选项、答案、评分提示词在任何训练数据里都不得出现 | 准入检查在训练行上传前做哈希比对,命中则整条指令失败,不剔行(剔行会破坏训练步与行的对应) |
| 评测凭证不进树、不进命令行 | 评测端点地址与密钥只在树外的本机配置里,权限必须是 0600 | 配置文件缺失、权限过宽、放进仓库目录都在装配期被拒 |

## 3. 架构

诊断评测由一个薄编排器加一组纯函数模块组成。编排器只决定调用顺序,统计与判定逻辑都在各模块里。模块之间不互相调用,数据经编排器传递。

<!-- figure:begin 21-diagnostic-architecture -->
![诊断评测的组件](diagrams/21-diagnostic-architecture.svg)

**图 2　诊断评测的组件**　评估器从产物树取资产与基线后调用编排器。编排器是薄协调层,依次调用流水线模块,模块之间不互相调用,箭头是数据流向。统计模块被多处调用,隔离校验器独立暴露,供情境生成路径调用。

<details>
<summary>文字版:图中的节点与连线</summary>

**节点**

- evaluator · assets:make_diagnostic_evaluator / 资产读写 · 基线解析 · backends
- orchestrator:run_diagnostic · 只编排,不含统计与决策
- taxonomy_loader:按档选题 · 变体族
- someval_adapter:静态轨执行 · 原始输出
- dynamic_track:动态轨 episode / 题目来自调用方 spec
- scorer:规则 · judge · 偏差守卫
- error_classifier:变体族通过模式 / 动态一致性
- store_adapter:report 组装 · 写产物树
- regression:退化判定 · 版本可比
- aggregator:L3 → L2 → L1 · 融合
- stats:Wilson 区间 · 通过率 / 方差 · 区间相交
- isolation:污染过滤 · 独立暴露 / 供情境生成路径调用

**连线**

- evaluator · assets → orchestrator:配置 · 基线 · backends
- orchestrator → taxonomy_loader:按序调用
- taxonomy_loader → someval_adapter:静态题
- someval_adapter → scorer
- dynamic_track → scorer
- scorer → error_classifier:ItemResult
- scorer → aggregator:ItemResult
- aggregator → regression:分数
- regression → store_adapter:标记
- error_classifier → store_adapter:ErrorProfile

</details>
<!-- figure:end 21-diagnostic-architecture -->

| 模块 | 职责 | 依赖 | 行数 | 状态 |
|---|---|---|---|---|
| `orchestrator` | `run_diagnostic(...)`:八步流水线,写报告 | 下列全部流水线模块 | 240 | [已落地] |
| `taxonomy_loader` | `ItemPool`、按档选题、选出完整的变体族 | `datamodel` | 72 | [已落地] |
| `someval_adapter` | `SoMEvalRunner` 协议与 mock,原始输出转 `ItemResult` | `stats` | 103 | [已落地] |
| `dynamic_track` | 跑一道动态题的 episode,带关键词早停 | `simulator.runner` | 75 | [已落地] |
| `scorer` | 规则打分器、judge 协议与 mock、自评偏差守卫 | `stats` | 165 | [已落地] |
| `error_classifier` | 变体族错误归类、动态一致性、按 L2 汇总分布 | `datamodel` | 108 | [已落地] |
| `aggregator` | L3、L2、L1 聚合与两轨融合 | `stats` | 89 | [已落地] |
| `regression` | 退化判定;G 组探针与 reward hacking 检测 | `stats`、`benchmark_version` | 105 | [已落地] |
| `report`、`store_adapter` | 组装报告;信封读写 | `controlplane` | 38、30 | [已落地] |
| `stats` | Wilson 区间、通过率、总体方差、区间相交 | 标准库 `math` | 98 | [已落地] |
| `benchmark_version` | 五段式版本记录与可比判定 | 无 | 52 | [已落地] |
| `isolation` | 训练情境与题库的污染判定与过滤(词元相似度,对中文无效,无调用方) | 无 | 137 | [已落地] |
| `contamination` | 污染索引:SoMBench 文本的 20 字滑窗哈希集合的构建、读取、命中计数;训练数据的准入检查 | 标准库 | 120 | [已落地] |
| `evaluator`、`assets` | 接入调和循环;分类表与题库的信封读写 | 编排器、`controlplane.evidence` | 111、74 | [已落地] |
| 真实运行器与导入命令 | 起服务、跑 SoMEval 子进程、解析结果;把 SoMBench 导入成分类表、题库与污染索引 | `compute`、`diagnostic`,在 `factory` 里装配 | 393、209、135 | [已落地] |
| `compute.sizing` | 起服务前的显存核算(权重体积、非权重开销、编译峰值) | `compute.errors` | 88 | [已落地] |

依赖方向规则:

| 规则 | 现状 |
|---|---|
| `diagnostic` 可以导入 `controlplane` 与 `simulator` | 已满足 |
| `diagnostic` 不导入 `mindtrainer`、`trainscheduler`、`recipestore`、`factory`、`compute` | 已满足;一条语法树扫描测试强制不导入 `mindtrainer`,其余靠约定。真实运行器要用 `compute`,所以它住在 `factory`,只实现 `diagnostic` 定义的协议 |
| 下游只读报告产物,不导入 `diagnostic` | 已满足:RecipeStore 在 `src/recipestore/gain.py`、TrainScheduler 在 `src/trainscheduler/assemble.py` 按 `to_dict()` 的键名直接读 payload,两包都不导入 `diagnostic` |
| 评估器的返回类型写成可调用对象,不导入守护模块 | 已满足 |
| 污染准入检查由 `factory` 作为可调用对象注入训练执行器,`mindtrainer` 不导入 `diagnostic` | 准入检查是 `Callable[[bytes], tuple[int, int]]`:收整份上传字节,返回(有命中的行数, 命中窗口数) |

## 4. 数据模型与状态机

所有实体都是带 `to_dict`、`from_dict` 的数据类,定义在 `src/diagnostic/datamodel.py`。报告是非审核类产物,写入即 `executed`,没有自己的状态流转。

| 实体 | 字段与含义 | 约束 |
|---|---|---|
| `DiagnosticTaxonomy` | `version`;`l1_dimensions`;`l2_dimensions` 从 L1 到 L2 列表;`l3_tasks` 从 L2 到 L3 列表;`g_probes` | 维度枚举是配置,不写死。SoMBench 导入的分类表直接用样本里的编号做键(3 个 L1、17 个 L2、71 个 L3,形如 `1`、`1.1`、`1.1.1`),`g_probes` 为空 |
| `DiagnosticItem` | `id`、`l3_task`、`item_type`、`scoring_mode`(`rule`、`judge` 或 `someval`)、`track`、`reference_answer`、`rubric`、`family_id` | 动态题的 `rubric` 可带 `goal_keyword`。`someval` 模式的题只存 id、维度、题型和变体族,`reference_answer` 与 `rubric` 恒为空,题面、选项、答案一律不进树(DX-19);打分委托给 SoMEval,编排器不重打分 |
| `VariantFamily` | `family_id`、`base_item_id`、`variant_item_ids` | 诊断的最小单元。SoMBench 导入时,一道基题加上它的 `varA`、`varB` 变体成一族(529 族,其中 39 族有两个变体);`hardest` 不入族,见 DX-18 |
| `SampleOutcome` | `score`(0 或 1)、`extracted`(SoMEval 抽出的答案字母,开放题为空)、`error_reason`(SoMEval 的原因码,正确时为空) | 真实运行器按题返回它;`ItemResult.raw_outputs` 存 `extracted`,从不存模型的完整输出 |
| `ItemResult` | 每题 k 个原始输出、各次得分、通过率、总体方差、轨道 | 得分是 0 或 1,judge 分可为小数。k 为 1 时方差恒为 0,`unstable` 归类不会出现 |
| `DimensionScore` | `dimension`、`level`、`score`、`ci_low`、`ci_high`、`sample_size`、`track_provenance` | 每个分数都带区间与轨道 |
| `ErrorProfile` | `l2_dimension`、`error_type_distribution`、`track` | 分布和为 1;空标签 `""` 表示没有错误 |
| `RegressionFlags`、`ConsistencySignals` | 退化标记列表;规则与 judge 分歧、诊断与奖励分歧 | 一致性信号 V1 恒为空 |
| `DiagnosticReport` | 头部(`id`、`candidate_ref`、`baseline_ref`、`benchmark_version`、`run_tier`、`sampling_k`)加以上全部与 `links` | 载荷就是 `to_dict()` |

| 产物 | 路径 | 状态机 | 写方 | 状态 |
|---|---|---|---|---|
| 诊断报告 | `runs/<run>/diagnostic_report.yaml` | 非审核类,写入即 `executed` | 编排器或占位桩 | [已落地] |
| 分类表 | `diagnostics/taxonomy_<版本>.yaml` | 写入即 `effective`,不走审核门 | `factory install-defaults`;`factory import-sombench` | [已落地] |
| 题库 | `diagnostics/item_pool_<版本>.yaml` | 写入即 `effective`,不走审核门 | `factory install-defaults`;`factory import-sombench` | [已落地] |
| 污染索引 | 树外,路径在本机评测配置里 | 无;内容寻址,头部记数据集哈希 | `factory import-sombench` | [已落地] |

真实报告的载荷形状如下。`backends` 键由评估器写入。真实 SoMEval 评测另带一个只作记录、不参与判级的 `someval` 块。

```yaml
id: run-002              # 信封 id 与报告 id 都等于 run id
candidate_ref: cand-run-002
baseline_ref: zengen-v1  # 基线模型 id;无基线时为 none
benchmark_version: bench-v1
run_tier: full           # full 或 quick
sampling_k: 4
dimension_scores:        # L3、L2、L1 各一条起
  - {dimension: belief, level: l2, score: 0.25, ci_low: 0.071, ci_high: 0.591, sample_size: 8, track_provenance: static}
error_profiles: [{l2_dimension: belief, error_type_distribution: {unstable: 1.0}, track: static}]
regression_flags: {flags: [...]}
consistency_signals: {rule_vs_judge_divergence: {}, diagnostic_vs_reward_divergence: 0.0}
links: [run-002]
backends: mock           # mock 或 real,必写;缺失的报告一律判 stub
someval:                 # 仅真实 SoMEval 评测;只作记录,不参与判级,不含任何题面
  protocol: cot
  samples: 3481
  extraction_failed: 12
  unknown_error_reasons: {}     # 未识别的原因码及其计数
  dataset_sha256: 5ace4bd4b8726e5b…
  someval_commit: 6453a83+dirty  # SoMEval 检出的 HEAD;工作树有未提交改动时后缀 +dirty
```

证据等级由 `controlplane.evidence.diagnostic_grade` 判定,诊断评测自己不判等级。

| 载荷 | 证据等级 |
|---|---|
| `stub` 为真(占位桩写的报告) | `stub` |
| `backends` 等于 `mock` | `mock`,不看分数 |
| `dimension_scores` 非空,且 `backends` 等于 `real` | `real` |
| 其余,含没有 `backends`、`backends` 为未知值或没有分数 | `stub`,判不出来按最弱算 |

不变量:

1. 每个 `DimensionScore` 都有区间与轨道标注,聚合过程不丢轨道。
2. 同一个 L2 两轨都有分时,先各自出分,再出一条 `fused` 分,三条都进报告。
3. 退化标记只在同键(维度、层级、轨道)且区间不相交时产生。
4. 报告重写时内容逐字节相同就不产生新提交,信封时间戳是固定常量。
5. 编排器不读产物树,只写报告;资产与基线由调用方传入。

## 5. 接口契约

编排器是入口。调用方准备好分类表、题库、两个后端和基线分数,编排器跑完并写报告。

```python
@dataclass
class DiagnosticConfig:
    run_id: str; candidate_ref: str; candidate_model_id: str
    baseline_ref: str; tier: str; sampling_k: int; benchmark_version: str

def run_diagnostic(cfg, taxonomy, pool, someval_runner, judge_backend, store, *,
                   baseline_scores: list[DimensionScore],
                   dynamic_specs: list[dict], backends: str) -> DiagnosticReport: ...

class SoMEvalRunner(Protocol):
    def evaluate(self, items, candidate_ref: str, k: int) -> dict[str, list[str]]: ...
class ScoredSoMEvalRunner(SoMEvalRunner, Protocol):
    def evaluate_scored(self, items, candidate_ref: str, k: int) -> dict[str, list[SampleOutcome]]: ...
class JudgeBackend(Protocol):
    model_id: str
    def judge(self, item, response: str, sample_index: int) -> float: ...
```

`scoring_mode` 为 `rule` 的题走 `evaluate`,拿回原始文本后由规则打分器重打分;为 `someval` 的题走 `evaluate_scored`,拿回 SoMEval 已判好的 `SampleOutcome`,编排器不再打分。SoMBench 的选项由 SoMEval 按「数据集、样本 id、重复序号」确定性打乱,正确字母每次不同,所以「输出等于参考答案」的精确匹配对它不成立;开放题(Q4)的分数来自外部大模型按评分细则打的总分,过阈值才算对,这些只有 SoMEval 自己能判。mock 运行器没有 `evaluate_scored`,题库里有 `someval` 题而运行器不支持时,在跑任何一题之前抛确定性错误。

每条动态 spec 是一个字典,带 `item`、`scenario`、`assignment`、`personas`、`backends`,可选 `episode_id`。`assignment` 里必须恰有一个 `model_backend == "self_play"` 的绑定,它就是候选角色位。

接入调和循环的评估器:

```python
# diagnostic.evaluator
def make_diagnostic_evaluator(*, taxonomy_version, item_pool_version, someval_runner,
        judge_backend, benchmark_version, tier="full", sampling_k=4,
        backends="mock") -> Callable[[GitStore, str, str], None]: ...
def resolve_baseline(store, run_id, benchmark_version, *,
        backends: str) -> tuple[str, list[DimensionScore]]: ...
# diagnostic.store_adapter 与 orchestrator
def write_report(store, run_id, report, *, backends: str) -> None: ...
```

`backends` 是必填的关键字参数,载荷里一定有这个键;取值不在 `BACKENDS_VALUES` 里时写之前抛 `ValueError`。

评估器的行为:

| 项 | 规则 |
|---|---|
| 资产 | 每次调用时用 `read_taxonomy(...)` 与 `read_item_pool(...)` 从树里读,缺失或版本不符抛 `DiagnosticAssetError` |
| `backends` | 构造时校验,只接受 `BACKENDS_VALUES` 里的 `mock`、`real` |
| 配置 | `candidate_model_id` 取候选 id,动态 spec 为空 |
| 基线 | 版本号最大的 `effective` zengen 模型,取其 `links` 第二项那个 run 的报告 |
| 无基线 | 没有模型、`links` 不足两项、基线 run 就是本 run、没有报告、报告是 `stub`、没有分数、`benchmark_version` 不同、证据等级与本次 `backends` 不同;返回 `NO_BASELINE` 即 `none` 与空列表 |

诊断资产的读写:`write_taxonomy`、`write_item_pool` 与常量 `TAXONOMY_TYPE`、`ITEM_POOL_TYPE` 放在 `diagnostic.assets`。产物类型是 `diagnostic_taxonomy` 与 `diagnostic_item_pool`,状态 `effective`,登记即生效,题库变更本身不走审核门。信封 id 为 `taxonomy-<版本>` 与 `item-pool-<版本>`。读取时校验信封类型与载荷里的版本号,路径上放错东西会明确报错。组合根的 `install-defaults` 与 `import-sombench` 调这两个写函数。

`candidate_ref` 是候选 id,mock 运行器忽略它。真实 SoMEval 运行器的协议签名不变,仍收候选 id;但候选文件是按 run 存放的,文件名里不带候选 id,所以评估器在调用编排器之前先把 `run_id` 绑定给运行器(`bind`),运行器按 run 读 `candidate_checkpoint.yaml`,并核对信封的 id 就是传进来的候选 id,不符就按确定性失败处理。从里面取 `checkpoint_ref` 的 `BigRef`,再由 `fetch_hint` 的 `type` 与 `format` 判断存点是真实导出还是 mock。存点所在的端点必须就是装配时选定的那个端点,因为服务要在存点所在主机上起。取不到、解不出、是 mock 导出或端点对不上,抛确定性错误,走下面的失败落点。

触发条件:候选为 `pending_review` 或 `approved` 且没有诊断报告时,调和循环产生 `request_diagnostic`。默认入口里执行它的是占位桩;闭环模式下组合根换成 mock 评估器;`--evaluator someval` 换成真实评估器。

| 邻居 | 本文依赖它什么 | 它依赖本文什么 |
|---|---|---|
| ControlPlane | `GitStore` 的幂等写入;信封;路径函数;`request_diagnostic` 触发;延后通道 `DeferredError` | 报告文件存在即"有诊断";审核门①的 `review` 打印分数与退化标记 |
| SocialSimulator 运行器 | `run_episode` 的 `stop_predicate` 早停与 `terminated_reason` | 无 |
| 远端计算层 `compute` | 真实运行器经它起 vLLM 服务、开隧道、持有 GPU 租约(DX-13 到 DX-15) | 无;`compute` 不认识评测 |
| MindTrainer | 无 | 训练行上传前调用污染准入检查(DX-19);准入检查是 factory 注入的可调用对象,`mindtrainer` 不导入 `diagnostic` |
| SoMEval 检出目录 | 数据集 parquet、评测入口 `tasks/SoMBench/run.py`、结果文件格式;只读,不改它 | 无 |
| RecipeStore [已落地] | 无 | 读 L1 分算增益;读 `regression_flags.flags` 判战绩退化 |
| TrainScheduler [已落地] | 无 | 读 L1 分、退化标记、错误画像装配规划上下文;丢掉空标签 `""` |
| factory | 写入默认资产与 SoMBench 资产;装配评估器、真实运行器与污染准入检查 | 评估器构造函数 |

## 6. 关键流程

一次诊断是一条八步流水线,两轨在归类与聚合处汇合。步骤编号与图中徽标一一对应。

<!-- figure:begin 21-diagnostic-dataflow -->
![一次诊断的双轨数据流](diagrams/21-diagnostic-dataflow.svg)

**图 3　一次诊断的双轨数据流**　静态轨从题库按档选题,经 SoMEval 运行器取 k 个回答,规则式题目重打分;someval 模式的题由运行器直接判分,不经重打分。动态轨由调用方给 spec,跑多轮 episode 后由 judge 给候选话语打分。两轨的 ItemResult 分开存放,分别送入错误归类与逐轨聚合,L2 融合后聚到 L1,再与基线比较,最后写报告。

<details>
<summary>文字版:图中的节点与连线</summary>

**节点**

- 选题:select_items / full 全部 · quick 每 L3 一题
- 动态 spec:调用方提供 / 题目 · 情境 · 绑定 · 后端
- SoMEval 执行:run_static_items / 每题 k 个回答 / someval 题:运行器判分
- 多轮 episode:run_dynamic_item / 取候选角色位的话语
- 规则重打分:RuleScorer / 只对 rule 模式题
- judge 打分:LLMJudgeScorer / 先过自评偏差守卫
- ItemResult:按轨分开存放
- 错误归类:静态:变体族 / 动态:一致性
- 逐轨聚合:L3 · L2 各轨分开
- 融合到 L1:两轨都有的 L2 融合
- 退化判定:对照基线 · 区间不相交
- 组装并写入:报告 · 错误画像 / write_report

**连线**

- 选题 → SoMEval 执行:静态题
- SoMEval 执行 → 规则重打分:原始输出
- 动态 spec → 多轮 episode
- 多轮 episode → judge 打分:话语
- 规则重打分 → ItemResult
- SoMEval 执行 → ItemResult:someval 题:已判分
- judge 打分 → ItemResult
- ItemResult → 错误归类
- ItemResult → 逐轨聚合:分数
- 逐轨聚合 → 融合到 L1
- 融合到 L1 → 退化判定
- 退化判定 → 组装并写入:标记
- 错误归类 → 组装并写入:ErrorProfile

</details>
<!-- figure:end 21-diagnostic-dataflow -->

| 步 | 做什么 | 关键规则 |
|---|---|---|
| 1 | `select_items(...)` 按档选题,只留静态题 | `full` 取全部;`quick` 每个 L3 取第一道;其余档位抛 `ValueError` |
| 2 | 静态轨执行:规则式题用 `RuleScorer` 重打分;`someval` 模式的题由运行器的 `evaluate_scored` 直接判分,不经重打分 | 重打分前后去空白、忽略大小写;judge 模式的静态题保留精确匹配分;题库里有 `someval` 题而运行器没有 `evaluate_scored` 时,一道题都不跑就抛 `ValueError`;运行器的统计取自 `last_stats()`。真实报告的分数都走 `someval` 这条路 |
| 3 | 动态轨:每条 spec 跑一个 episode,judge 给候选话语打分 | 话语带 `goal_keyword` 时早停;先过自评偏差守卫 |
| 4 | 错误归类,按 L2 汇总分布 | 静态轨只用基题与全部变体都在静态入选集合里的变体族;动态轨按话语一致性归类 |
| 5 | 每轨各自做 L3、L2 聚合 | 按样本量合并通过次数,再算 Wilson 区间 |
| 6 | 两轨都有的 L2 融合为 `fused`,再聚到 L1 | L1 成员轨道不一时标 `fused` |
| 7 | 与基线分逐键比较,产出退化标记 | 无基线时不打标记 |
| 8 | 组装报告,一致性信号置空,写入产物树 | 写入幂等 |

变体族错误归类按固定优先级取第一个命中的标签。编排器不传格式校验器与拒答检测器,所以前两条在 V1 不会命中。动态轨的立场函数在 V1 恒返回真,所以动态轨永远归不出 `inconsistency`,动态错误画像恒为空。

| 优先级 | 标签 | 条件 |
|---|---|---|
| 1 | `refusal` | 拒答检测器对基题任一输出为真 |
| 2 | `format_fail` | 格式校验器对基题任一输出为真 |
| 3 | `unstable` | 基题方差大于 0.2 |
| 4 | `surface_match` | 基题通过率不低于 0.5,且有变体低于 0.5 |
| 5 | `reasoning_break` | 基题通过率低于 0.5 |
| 6 | `""` | 都不满足,即没有错误 |

退化判定只产生一种标记。另外两个检测函数已实现,编排器不调用。

<!-- figure:begin 21-diagnostic-regression -->
![退化判定](diagrams/21-diagnostic-regression.svg)

**图 4　退化判定**　每个候选维度分按维度、层级、轨道三元组找基线。找到、候选更低且两个置信区间不相交,才打一条社会能力退化标记。两份基准版本都给出且不可比时直接报错。G 组探针与 reward hacking 的检测函数与退化判定并列。

<details>
<summary>文字版:图中的节点与连线</summary>

**节点**

- 维度分:候选与基线 / DimensionScore
- 版本可比?:两份 BenchmarkVersion / 编排器从不传
- 基线有同键?:维度 · 层级 · 轨道 / 三元组完全相同
- 候选更低?:候选分 < 基线分
- 区间不相交?:候选上界 < 基线下界
- CrossVersionError:不可比,整次判定失败
- 不打标记:没有基线 · 提升或持平 · 区间重叠
- social_regression:drop · level · track / baseline_version
- detect_g_probe_failure:G 组探针分数 < 0.7 → g_probe_failure / 输入是 G 组探针题的分数
- detect_reward_hacking:奖励涨幅 − 诊断涨幅 > 0.15 → reward_hacking_suspected / 输入是训练奖励与诊断分的涨幅

**连线**

- 维度分 → 版本可比?
- 版本可比? → 基线有同键?:可比
- 基线有同键? → 候选更低?:有
- 候选更低? → 区间不相交?:是
- 版本可比? → CrossVersionError:不可比
- 基线有同键? → 不打标记:无
- 候选更低? → 不打标记:否
- 区间不相交? → 不打标记:重叠
- 区间不相交? → social_regression:不相交

</details>
<!-- figure:end 21-diagnostic-regression -->

| 检测 | 判据 | 标记 | 状态 |
|---|---|---|---|
| `detect_regressions(...)` | 同键、候选更低、区间不相交 | `social_regression`,带 `drop`、`level`、`track`、`baseline_version` | 每次调用 |
| `detect_g_probe_failure(...)` | 探针分数低于 0.7 | `g_probe_failure` | 未调用 |
| `detect_reward_hacking(...)` | 奖励涨幅减诊断涨幅大于 0.15 | `reward_hacking_suspected` | 未调用 |

标记随聚合层级向上传播,标记数不等于独立问题数。一次 L3 下降若让所属 L2、L1 的区间也不相交,会产出三条标记,它们是同一次退化在三个层级上的证据,不是三次退化。产出侧不去重;只想看某一层的消费方按 `level` 筛选。规划上下文的 `RegressionMark` 照抄 `level` [已落地]。

失败与恢复路径:

| 情形 | 结果 | 恢复 |
|---|---|---|
| 任一步抛异常 | 不写报告,异常上抛给调用方 | 修好原因后重跑;接入循环时守护进程退出,重启后报告仍缺,再请求一次(`DeferredError` 例外:同一进程退避重试) |
| 两份基准版本都给出且不可比 | `CrossVersionError` | 编排器不传版本记录,V1 不会发生 |
| 基线版本不同或等级不同 | 评估器按无基线处理 | 无需恢复;本轮没有退化标记 |
| 同一 run 重跑且结果相同 | 不产生新提交 | 无需恢复 |
| 真实评估器:候选不可用或评测输出不合规 | 评估器写 `stub: true` 报告并正常返回 | 该候选不再触发评测;门①看到桩报告,人决定拒绝或修好后另起一轮 [已落地] |
| 真实评估器:资源暂时不可用 | 抛 `DeferredError` | 守护进程退避重试,不退出 [已落地] |

走查例子。一个一级维度 `mental_representation`,一个二级 `belief`,一个三级 `false_belief_transfer`。一个变体族:基题 `i1` 答案 `B`,变体 `i1v` 答案 `C`。`k` 为 4,`full` 档,只跑静态轨。基线一列对应 `tests/test_diag_orchestrator.py` 的夹具;候选一列的回答没有测试夹具,数值由直接调用编排器 `run_diagnostic` 实跑得出。把 `run-1` 当基线有前提:真实流程里基线由评估器解析,`run-1` 的候选必须已晋级为最新的 effective 模型,`benchmark_version` 与证据等级都与本次相同;不满足时没有基线,`run-2` 一条标记也没有,分数不变。

| 量 | 基线 run(`run-1`) | 候选 run(`run-2`) |
|---|---|---|
| `i1` 的 4 个回答 | `B B B B` | `B B X X` |
| `i1v` 的 4 个回答 | `C C C C` | `X X X X` |
| L3、L2、L1 分(样本量 8) | 1.0 | 0.25 |
| 95% Wilson 区间 | 0.676 至 1.0 | 0.071 至 0.591 |
| 错误画像 | `{"": 1.0}`,没有错误 | `{"unstable": 1.0}`:基题通过率 0.5,方差 0.25 大于 0.2 |
| 退化标记 | 无基线,0 条 | 3 条 `social_regression`,L3、L2、L1 各一条,`drop` 0.75 |

候选区间上界 0.591 低于基线下界 0.676,所以下降算数。三条标记是同一次退化,不是三次。同一套题改用 `quick` 档时只选出基题,样本量降为 4,变体族不完整,错误画像为空。

## 7. 关键决策

| 编号 | 决策 | 备选 | 理由 | 判错的代价 |
|---|---|---|---|---|
| R17 | 诊断报告带 `backends` 标记,默认不接受 `mock` 证据驱动规划与提名 | 不标记,mock 分数当真 | mock 分数在默认配置下不会让闭环自己转起来 | 开发时要显式打开开关 |
| R21 | 错误画像里的空标签 `""` 表示没有错误,规划上下文装配前丢掉 | 诊断侧不写空标签 | 诊断侧原样计数,分布和为 1,信息不丢 | 丢掉后规划器看不到二级维度答对的比例 |
| DX-1 | 编排器是薄协调层,统计与判定都在纯函数模块里 | 一个大函数 | 每个模块能单独用合成数据测 | 编排器的调用顺序只有端到端测试覆盖 |
| DX-2 | 置信区间用标准库手写 Wilson 区间,z 为 1.96 | 正态近似;引入统计库 | 小样本与 0、1 边界下 Wilson 不越界,零新依赖 | 置信水平写死,改要动默认参数 |
| DX-3 | 上层聚合按样本量合并通过次数,不对下层分取平均 | 下层分等权平均 | 区间宽度如实反映总样本量 | 题多的 L3 主导 L2 分 |
| DX-4 | 退化要求区间不相交,不设固定下降阈值 | 下降超过固定值就标 | 区间内的波动不当作信号 | 样本小时区间宽,真实下降可能漏标 |
| DX-5 | 退化比较按维度、层级、轨道三元组配对 | 只按维度配对 | 两轨分数不能互相比较 | 基线只有静态分、候选有融合分时,该 L2 不比较 |
| DX-6 | judge 与候选的模型 id 互为前缀即拒绝 | 只拒绝相同 id | 微调版与其基座互评都有偏差 | 名字恰好共享前缀的无关模型也被拒绝 |
| DX-7 | 评估器缺资产时抛 `DiagnosticAssetError`,不写空报告 [已落地] | 写一份没有分数的报告 | 空报告会被判 `stub`,候选卡在门前且看不出原因 | 资产缺失时守护进程每次启动都在同一处退出 |
| DX-8 | 基线是最新 `effective` 模型所在 run 的报告;版本或等级不同就视为无基线 | 跨版本也比较 | 跨版本、跨等级的分数不可比 | 换版本或切到真实后端后的第一轮没有退化标记 |
| DX-9 | 评估器只跑静态轨,`candidate_model_id` 取候选 id [已落地] | 接入动态轨 | V1 没有动态轨的情境与绑定来源 | 接入动态轨时要换成真实模型 id,否则自评偏差守卫形同虚设 |
| DX-10 | `backends` 作为 `write_report` 的参数一次写入 | 写完后再改信封 | 两次提交之间会有一份无标记、被判 `real` 的 mock 报告 | `run_diagnostic` 多一个参数 |
| DX-11 | 退化标记在 L3、L2、L1 各层独立产出,产出侧不去重;只看某一层时在消费侧按 `level` 筛选 | 只在最低层标记;产出侧合并 | 标记是随聚合层级向上传播的证据,审核门①与 RecipeStore 战绩回看都消费它 | 消费方若数标记条数,会把一次退化读成三次 |
| DX-12 | 证据等级失败关闭:只有 `backends` 为 `real` 才判 `real`,缺失一律判 `stub`;`run_diagnostic`、`write_report`、`resolve_baseline` 的 `backends` 必填 | 失败开放:有分数就判 `real` | 直接调用编排器写出的 mock 报告不会被当作真证据,默认不接受 mock 的规则无法被绕过 | 七个既有调用点要补传参数;手写的无标记报告永远不可接受,要人补标记 |
| DX-13 | 推理服务当作一个不会退出的远端作业,由远端计算层的 `start`、`status`、`stop` 托管,不新增助手命令 [已落地] | 为服务加专用助手命令;评测改走 ROLL 的 worker | 作业控制本来就把启动、查询、停止分开,`stop` 对整个进程组先发终止信号再强杀,能收拾 vLLM 的多进程树;不动助手就不用重钉哈希,已部署的助手继续有效 | 服务的就绪判定要自己做(作业状态只说进程在不在,不说端口通不通) |
| DX-14 | 服务的接入密钥由本机现生成,只经作业的 `secret_env` 通道交给服务,不出现在命令行、日志、磁盘与报告里;每次启动换新的 [已落地] | 配置里存一个长期密钥;靠回环地址隔离、不设密钥 | vLLM 的 HTTP 接口没有别的访问控制,同机其他容器共用主机网络能连到回环端口;密钥每次换,泄露的窗口就是一次评测 | 因为密钥只在启动那一刻存在,服务一旦在守护进程崩溃后残留,接不回去,只能停掉重起,所以每次启动都用带尝试序号的新作业目录,绝不接管旧的 |
| DX-15 | 服务存活期间持有 GPU 租约,无论评测成败都按「关隧道、停服务、放租约」的顺序收尾 [已落地]。停服务撞上 SSH 失败时隔 5 秒、20 秒各重试一次(`STOP_RETRY_DELAYS_S`);都失败就照旧抛出、不放租约 [已落地] | 只在起服务时取租约 | 服务一直占着显存,训练作业此时不得起来;任何一步收尾失败都不能挡住后面几步。stop 是幂等的,重试安全;不重试的话,一次网络抖动就把服务留在主机上,下一轮起服务只会撞上租约、无限延后(真机撞过) | 收尾顺序里服务没停成功就放租约,会让训练撞上还没释放的显存,所以放租约前要核对作业确已不在运行。三次都连不上时服务仍会留下,要人停;重试只把收尾多拖至多 25 秒 |
| DX-16 | SoMBench 评测范围是 Q1 到 Q4 全部 3481 题,打分全部委托给 SoMEval 自己的判定(选择题抽 `\boxed{}` 字母比对,开放题由外部大模型按评分细则打总分、过阈值 7.0 才算对),不用本包的规则打分器重打分;真实运行器只支持 `full` 档 | 只评 Q1 到 Q3;自己重写判分;按题生成子集数据文件以支持 `quick` | 选项被确定性打乱、答案以文本存放,精确匹配对不上;SoMEval 不能按 id 选题,要评子集就得生成只含这些样本的数据文件,而全量一次的耗时只有一个粗略参照:SoMEval 的历史日志里,同类模型一次预测加判分加出图共 2 小时 06 分,服务配置与硬件和我们的不同,未在本系统验证。是否值得为 `quick` 档维护子集生成代码,等真机测出实际耗时再定 | 判分逻辑跟着 SoMEval 走:SoMEval 改了判分,同一个检出目录下的分数就不可比,所以版本里记 SoMEval 的提交,工作树不干净时在报告里标 `dirty` |
| DX-17 | 每题采样一次(`sampling_k` 为 1),协议用 `cot` | 每题多次;`del_tom` 八次投票 | 与 SoMEval 历史口径一致,成本最低;SoMEval 设了协议后重复次数由协议决定,`cot` 就是 1 | 方差恒为 0,`unstable` 归类不会出现,L3 只有 42 到 53 道题所以区间很宽,退化主要在 L1(700 到 1450 道)和部分 L2 上才检得出 |
| DX-18 | 变体族只由基题和它的 `varA`、`varB` 构成;`hardest` 题计入各维度分但不入族 | `hardest` 也入族 | `varA`、`varB` 换了故事、题干多数不变,是「同一个问题换个表面」,基题通过而变体失败才说得上表面匹配;`hardest` 故事不变但题干整个换了(1209 道里题干相同的是 0),是另一个更难的问题,把它的失败算成表面匹配会误导 | 一个基题的变体少于完整的 `hardest`,`surface_match` 只在 529 个有变体的基题上判得出 |
| DX-19 | 评测题不进训练,四道防线:① 树内的题库只存 id、维度、题型、变体族,不存题面、选项、答案;② SoMEval 的工作目录(含带完整题面的 `prediction.jsonl`)在树外且不在 SoMEval 检出目录内,装配时校验;③ 训练行上传前做 20 字滑窗哈希比对,命中即整条指令失败;④ 报告只带维度级聚合,不带题目 id 之外的任何题面 | 只靠词元相似度过滤;只靠约定 | 词元相似度对中文无效(按空白切词,整篇故事算一个词元);约定拦不住「把评测集当素材合成数据」这种最容易发生的事。准入检查放在上传前的最后一道,不论数据从哪条路径来都要经过 | 滑窗比对只抓逐字的重叠,改写与释义抓不到;基座模型预训练时见过 SoMBench 的情况我们管不了。命中的是整条指令而不是行,因为 ROLL 要求每步行数固定,剔行会让训练步与数据错位 |
| DX-20 | 评测所用的端点地址与密钥放在树外的本机配置文件,权限必须是 0600,路径由环境变量 `RISE_EVAL_CONFIG` 指定;只经子进程环境变量交给 SoMEval,不进命令行、日志、报告、树;每次运行后扫描工作目录,出现任一凭证字面值即删除该目录并让守护进程退出 | 写进树里的配置信封;写进 SoMEval 的配置文件 | 用户明确要求评测凭证可由配置文件提供;SoMEval 自己的 `config.json` 会脱敏,但日志与预测文件是否会带出凭证没有保证,所以不信它,自己扫 | 凭证放在 SoMEval 检出目录里会被他人的未提交改动一起带走,所以工作目录与配置都不放那里 |
| DX-21 | 为 SoMEval 起的服务把上下文上限提到 40960 [已落地] | 沿用默认的 4096 | 本地 SoMEval 检出的工作树里,`cot` 协议把单次生成上限固定为 32768(其提交 `6453a83` 里是 8192,改动未提交),再加最长约 1700 字符的提示;上限不够时 vLLM 会拒绝请求,SoMEval 的客户端把失败当作无答案,整批被判错 | 显存占用和吞吐随之变化,验证时 4096、关图捕获的数据不再适用,需要在真机上重测 |
| DX-22 | 真实评估器由 `--evaluator someval` 装配,必须同时给 `--executor roll`,暂不能与 `--enable` 同用 | 并入 `--enable`;让 `--executor roll` 自动带上 | 评估要真实存点,所以依赖真实执行器;两个开关各管一件事,装配期一次校验完;与闭环处理器叠加的排队语义尚未验证,先单链路跑通 | 想只换评估器、不要真实训练做不到;真实评估期间持有 GPU 租约,守护进程同步阻塞整个评测时长(估计为数小时,真机未测) |

## 8. 失败处理与可观测

诊断评测内部不重试,也不写失败记录。所有异常原样上抛,由调用方决定分类;报告缺失本身就是重新请求的条件。

| 失败点 | 类 | 处理 | 留痕 |
|---|---|---|---|
| SoMEval 运行器或 judge 后端瞬时出错 | T1 | 原样上抛;接入循环后守护进程退出,重启后重新请求 | 无报告 |
| 资产缺失、类型不对、版本不符 | T1 | 抛 `DiagnosticAssetError`,写明路径,补齐资产后重启 | 无报告 |
| judge 是候选近亲 | T2 | 构造打分器时抛 `SelfEvalBiasError` | 无报告 |
| 未知档位、mock 缺题目的固定输出、judge 缺固定分 | T2 | 抛 `ValueError` 或 `KeyError` | 无报告 |
| 基准版本不可比 | T2 | 抛 `CrossVersionError`;V1 不触发 | 无报告 |
| 报告分数可疑或画像异常 | T3 | 不自动处理,审核门①的人读报告判断 | 报告本身 |
| 真实 SoMEval 的确定性失败:题库条目在 SoMEval 的结果里找不到、数据集哈希与题库版本对不上、候选存点解不出或是 mock 导出、存点端点与装配的端点不一致 | T2 | 评估器内部捕获,写一份 `stub: true` 的报告,载荷带 `failure_reason` 与缺失清单(只有题目 id 的数量与前若干个,不含题面);`request_diagnostic` 动作正常结束,该候选此后不再触发评测 | 报告本身,等级为 `stub` |
| 真实 SoMEval 的瞬态失败:服务启动超时、隧道建立失败、GPU 被占用、SoMEval 子进程非零退出、裁判端点出错(`judge_error`)、没有答案的样本占比超过阈值 | 延后 | 抛 `DeferredError` 的子类,守护进程不写树、按退避重试;不写报告,租约在失败时释放。连续失败到上限升级为需要人看 | 守护进程结构化日志 |
| 评测凭证出现在 SoMEval 工作目录的任何文件里 | T3 | 删除该次运行的工作目录,抛不继承 `DeferredError` 的异常,守护进程带着原因退出,等人处理 | 守护进程退出原因,只写文件相对路径,不写密钥 |

写 `stub` 报告而不是让动作一直失败:报告是评测的结论载体,`diagnostic_grade` 对 `stub: true` 已经失败关闭,闭环里记录、提名、规划下一轮都要求证据可接受,所以不推进。晋级本身只看对应报告是否在场、不看等级,这条语义不变,拦它的是人工审核门:`review` 对 `stub` 报告标 `[STUB]` [已落地] 并打印 `failure_reason`,批准的人能区分「占位」和「评测没跑成」。判错的代价:把确定性错误当瞬态,守护进程会无限重试同一个必然失败的评测。

**哪些组件产生了分数,报告的等级就看哪些组件。** 真实 SoMEval 的静态轨不调用 `JudgeBackend`(动态 spec 为空),分数全部来自真实存点加真实 SoMEval,所以这类报告的 `backends` 写 `real`,即使装配里的 judge 后端仍是 mock。只要动态 spec 非空而 judge 是 mock,`backends` 仍写 `mock`,`diagnostic_grade` 与下游门控一行不改。报告载荷另加一个只作记录、不参与判级的字段 `component_backends`,形如 `{executor: real, converter: real, someval: real, judge: unused}`,让人看出每个组件是真实、mock 还是没被用到。`executor` 与 `converter` 两项由候选存点引用的 `fetch_hint.type` 推出(`roll_ssh` 即训练与转换都是真实的,`mock` 即都是 mock),`someval` 与 `judge` 两项由评估器装配时声明。该字段与 `backends` 在同一次 `write_report` 里落盘。

只读观测手段:

| 手段 | 看什么 |
|---|---|
| `review <候选 id>` 命令 | 报告头部、各级分数、退化标记;占位报告标 `[STUB]`;mock 报告标 `[MOCK BACKENDS]` |
| 读 `runs/<run>/diagnostic_report.yaml` | 完整载荷,含错误画像与区间 |
| `git log` 该文件 | 每次重写的提交;内容相同不提交 |

## 9. 测试策略

全部测试用固定题目和罐头输出,不需要 GPU、真实 SoMEval、真实 judge 或 gRPC。诊断包自己的测试有 19 个文件,共 150 个用例;本轮接入 SoMBench 另加的组合根、执行器与计算层测试(导入、评测配置、运行器、装配、准入检查注入、显存核算)共 83 个用例,两者有重叠,按文件计。

| 层次 | 对象 | 替身 | 测试文件与用例数 |
|---|---|---|---|
| 数据模型 | 九个实体的往返 | 无 | `tests/test_diag_datamodel.py` 10 |
| 统计 | Wilson 区间、通过率、方差、区间相交 | 无 | `tests/test_diag_stats.py` 9 |
| 版本 | 五段式版本与可比判定 | 无 | `tests/test_diag_benchmark_version.py` 4 |
| 路径与读写 | 路径函数;报告信封往返与幂等 | 临时 git 仓库 | `tests/test_diag_paths.py` 4;`tests/test_diag_store_adapter.py` 4 |
| 选题 | 档位、按 L2 取题、完整变体族 | 无 | `tests/test_diag_taxonomy_loader.py` 6 |
| 执行 | 静态轨适配;动态轨早停 | `MockSoMEvalRunner`;`ScriptedBackend` | `tests/test_diag_someval_adapter.py` 4;`tests/test_diag_dynamic_track.py` 3 |
| 打分 | 规则打分、judge 打分、自评偏差守卫 | `MockJudgeBackend` | `tests/test_diag_scorer.py` 7 |
| 归类与聚合 | 六种标签;逐层聚合与融合 | 构造好的通过模式与分数 | `tests/test_diag_error_classifier.py` 8;`tests/test_diag_aggregator.py` 4 |
| 判定与报告 | 退化、探针、reward hacking;报告组装 | 合成分数 | `tests/test_diag_regression.py` 8;`tests/test_diag_report.py` 1 |
| 隔离 | 相似与不相似样本对 | 无 | `tests/test_diag_isolation.py` 6 |
| 端到端 | 双轨跑完并写报告 | 上述全部 mock 与临时仓库 | `tests/test_diag_orchestrator.py` 3 |
| 资产与标记 | 资产信封读写、`backends` 参数、证据等级 | 临时仓库 | `tests/test_diag_assets.py` |
| 评估器 | 基线解析、注入调和循环、不导入 `mindtrainer` | mock 后端与 `reconcile_once` | `tests/test_diag_evaluator.py`;`tests/test_factory_evaluation.py` |
| SoMBench 导入 | 3 个 L1、17 个 L2、71 个 L3;题 id 全部取自样本;变体族 529 个;树内资产扫描不含任何题面片段 | 合成的小 parquet(只造 ID 结构与长文本) | 新增 |
| 真实运行器 | 配置与权限校验、SoMEval 子进程命令、结果解析成 `SampleOutcome`、`judge_error` 与无答案占比超阈值走延后、凭证扫描、数据集哈希对不上走 T2 | 假的 SoMEval 入口脚本(写固定的 `detailed_metrics.jsonl`) | 新增 |
| 污染准入检查 | 索引构建与读取、逐字拷贝命中、删一句仍命中、无关中文不命中、数据集哈希对不上拒绝装配、命中则整条指令失败且原因里只有数量 | 合成文本 | 新增 |

真实运行器的测试不起任何服务,SoMEval 入口换成一个读环境变量、写固定结果文件的脚本;服务生命周期已由 `compute` 自己的测试覆盖。

## 10. 现状、局限与未核实项

编排器、全部流水线模块、评估器与资产读写、SoMBench 导入、真实运行器、污染准入检查与 `--evaluator someval` 装配都已落地。`scripts/run_controlplane.py` 走 `mindtrainer.wire`,不注入评估器,所以默认入口的 `request_diagnostic` 仍由守护进程用 `simulator.stubs` 的占位桩写报告;闭环模式(`factory run` 带 `--enable`)注入 mock 评估器,`--evaluator someval` 注入真实评估器。真机只验证到预测阶段。

| 能力 | 状态 | 说明 |
|---|---|---|
| 静态轨、规则打分、错误归类、聚合、退化判定、报告写入 | [已落地] | 经编排器端到端测试 |
| 动态轨与 judge 打分 | [已落地] | 只有编排器测试调用;评估器不跑 |
| 隔离校验器(词元相似度)、G 组探针检测、reward hacking 检测 | [已落地] | 没有调用方;隔离校验器对中文无效,不作 SoMBench 的保证 |
| 评估器、资产读写、`backends` 标记、基线解析 | [已落地] | 评估器每次从树里读资产 |
| 默认资产 `factory-tax-v1`、`factory-pool-v1` 与 mock 装配 | [已落地] | 默认题库只有 2 道占位题 |
| vLLM 服务组件(命令拼装、SSH 隧道、生命周期) | [已落地] | 对替身与真实本机 socket 测过;真机端到端等 GPU 空闲后补 |
| SoMBench 导入、真实运行器、污染索引与准入检查、`--evaluator someval` | [已落地],未在真机验证 | 代码与单测在主干上;真实主机上的端到端等 GPU 空闲后补(设计 21 的未核实项)。导入已在真实数据集上跑过一次:3481 题、529 个变体族、71 个 L3、索引 5.82 MiB |
| 动态轨的真实 judge 后端 | [推迟] | 静态轨的 SoMEval 已含裁判(Q4),与这里的 `JudgeBackend` 无关 |

| 已知局限 | 影响 | 缓解 |
|---|---|---|
| 动态一致性的立场函数恒返回真 | 动态轨永远不会出 `inconsistency`,动态错误画像恒为空 | 接入真实立场判别前,动态画像不作依据 |
| 一致性信号恒为空 | 规则与 judge 分歧、诊断与奖励分歧都看不到 | 审核人不以此判断 reward hacking |
| G 组探针没有题目来源,检测函数也不被调用 | 报告里不会出现 `g_probe_failure`,OPD 是否失效无法从报告看出 | 下游已按"可能出现"处理,不依赖它出现 |
| 报告只带 `benchmark_version` 字符串,不带五段式版本 | 换题库或 judge 提示而不换该字符串,会跨版本比较 | 换任何资产都同时换 `benchmark_version` |
| RecipeStore 与 TrainPlanner 的施工说明对标记种类说法不一,一处两种、一处三种 | 下游按不同假设写读取逻辑 | 以代码为准:今天只产出 `social_regression`,下游对另两种按"可能出现"处理 |
| 一次下降在 L3、L2、L1 各标一条 | 标记数不等于独立问题数,单个 L3 下降会产出三条标记 | 消费侧按 `level` 筛选,不在产出侧去重 |
| `quick` 档只取每个 L3 的第一道题 | 变体族不完整,错误画像为空 | 审核门①前用 `full` 档;真实运行器只支持 `full` |
| 每题只采样一次 | 方差恒为 0,`unstable` 归类不出现;L3 只有 42 到 53 道题,区间宽 | 看 L1 与有足够题量的 L2 的退化,L3 的升降只作线索 |
| SoMEval 把「模型没给出答案」记成答错 | 服务偶发失败会被当成模型能力差 | 无答案样本占比超过阈值就按瞬态失败延后重跑,不出报告;阈值与计数写进报告的 `someval` 块 |
| 污染准入检查只抓逐字重叠 | 改写、释义、翻译后的 SoMBench 内容抓不到;短于 20 字的片段(某些选项)产生不了滑窗,单独出现时抓不到 | 不依赖准入检查以外的东西声称「已隔离」;基座预训练是否见过 SoMBench 不在我们控制内 |
| 评测用的留出集(`heldout_jsonl`)是人工填写的远端路径 | 准入检查检查不到它的来源;它只用来算训练期的验证损失,不产生梯度,但若它取自 SoMBench,检查点选择会间接受影响 | 填写端点配置的人确认它不取自 SoMBench |
| 首个候选没有基线 | 首个候选的报告没有退化标记 | 基座本身的评测不在 V1,第二个候选起才有对照 |
| 基座 335 GiB 权重在 8 张 79 GiB 卡上每卡约 42 GiB,权重之外还要约 26 GiB 的激活与通信缓冲:开图优化时 vLLM 的编译要额外约 12 GiB,实测在 0.78 与 0.85 的显存占用下都 OOM;关掉图优化(`--enforce-eager`)能起,但留给 KV 缓存的空间很小,单条解码实测约 4.6 token/s | 全量 3481 题按这个速度要上百小时,本系统本轮不指望跑完;并发一高还会因缓存不足起不来 | 这是推理部署本身的资源配置问题,不在组件集成的范围内。真正要跑评测前,由运维侧决定量化权重、调低上下文上限或改用别的服务形态;系统这一侧只需保证参数可配、失败时报得清(启动 OOM 抛 `ServeStartupError` 并带日志尾巴) |
| 同一检出目录里 SoMEval 的判分逻辑被改动 | 分数与之前不可比 | 报告记录 SoMEval 的提交与是否 `dirty`,换提交时同时换 `benchmark_version` |
| 判分用的裁判模型由 SoMEval 的任务配置写死:本地检出的工作树里是 gpt-5.5、地址取 `OPENAI_BASE_URL`;它的提交 `6453a83` 里是 qwen3.7-max、地址取 `SOMBENCH_JUDGE_API_URL`,改动未提交 | 换裁判要改 SoMEval 的配置,不在我们的配置里;评测的是工作树而不是提交,报告里 SoMEval 版本记为 `6453a83+dirty`,看不出具体是哪份改动 | 报告不记裁判型号;换裁判前先确认 SoMEval 配置并换 `benchmark_version`;SoMEval 的这两处改动提交之后,版本记录才能准确定位 |
| judge 模式的静态题按精确匹配打分 | 这类题的分数没有意义 | V1 题库只放规则式静态题 |
| 占位报告的信封 id 是 `diag-<run>`,真实报告是 `<run>` | 按信封 id 找报告的工具要兼容两种写法 | 按路径找报告,不按 id |
| 不检测目标维度的显著提升 | 增益由 RecipeStore 用 L1 分直接相减 [已落地],不看区间 | 审核人对照报告里的区间读增益 |

**vLLM 服务。** 在一台空闲的 8 卡 H800 主机上,用镜像自带的 vLLM 开发版 `0.1.dev20073+g8e685d198`,以原生 `vllm serve` 加载基座,并用 `--lora-modules` 挂上转换器导出的适配器(张量并行 8、开专家并行、`--enable-lora`、最大秩 64、最多 1 个适配器、`--enforce-eager`,上下文长度限 4096)。约 151 秒后健康检查返回 200,模型列表同时给出基座与适配器。同一条提示、温度 0,适配器与基座的逐 token 对数概率在 15 个 token 上全部不同,最大绝对差 0.487,文本也不同,说明适配器确实被加载并作用于推理,没有被静默忽略。所以 ROLL 的两个进程内补丁(替换 LoRA 管理器的适配器加载函数、把 Triton 的 split_k 固定为 1)对「能否加载」不是必需的,最初设想的启动包装脚本不需要;补丁里管可重复性的那一个没有应用,见未核实项。

服务起来之前要关掉 NVLS:这类主机的多播绑定会失败(NCCL 报 `Failed to bind NVLink SHARP (NVLS) Multicast memory … CUDA error 401`),进而使 vLLM 建立张量并行通信组时抛 `NCCL error: unhandled cuda error`,进程在加载模型之前就退出。把环境变量 `NCCL_NVLS_ENABLE` 设为 0 之后一次起来。同机另一个任务的全部训练脚本、ROLL 自己的 CUDA 平台代码都同样关掉了它;原因在主机的交换芯片配置,不在软件侧。起初把失败归因于容器的 IPC 模式,被日志否定。

**服务组件的接口 [已落地]。** 组件放在远端计算层包内(`compute.vllm_service`、`compute.tunnel`、`compute.service`),只依赖标准库与该包自己的模块,不认识评测与 SoMEval(依赖表规定评测包不得导入它,所以由评测的调用方在装配时注入)。分三块:一块按配置拼出 `vllm serve` 的命令与环境,配置项有基座路径、适配器名与路径、张量并行数、最大适配器秩、上下文长度上限、显存占用比例和远端端口;并行数不写死,因为它取决于主机的卡数。第二块是一条本机到远端回环端口的 SSH 隧道,选一个空闲本地端口,端口真能连上才算建好,退出时终止隧道进程。第三块是服务的生命周期:取租约、挑一个没用过的尝试序号起作业、轮询健康检查直到就绪或超时、交还一个可用的本地地址与密钥,退出时无论成败按固定顺序收尾。

起服务时固定带 `NCCL_NVLS_ENABLE=0`、只绑回环、接入密钥走 `secret_env`。

**起服务前的显存核算 [已落地]。** 起服务之前先做一次算术:每卡需求等于「权重除以卡数,加上非权重开销,开图优化时再加编译峰值」,与「单卡容量乘以显存占用比例」比较,超过就确定性失败,不占租约、不起作业。权重体积从基座目录的 safetensors 索引读,读不到就跳过这道预检,不拿猜测去拦正常启动。非权重开销默认 26 GiB,按真机实测标定:335 GiB 权重分到 8 张 79 GiB 的卡上,每卡 42 GiB,0.85 的可用量 67 GiB,此时连最小的 KV 缓存块都分不出来。它是启发式,随模型与并行方式变化,所以是配置项;最终裁决仍是 vLLM 自己的启动错误,日志尾巴随 `ServeStartupError` 一起抛出。就绪判定要同时看两件事:作业仍在运行,且健康检查返回 200;作业提前退出就立刻失败,不等到超时。启动失败(超时、提前退出、隧道建不起来)抛远端计算层异常的一个新子类,继承延后通道的基类,所以守护进程按退避重试、不写树;服务日志的尾巴放在异常的属性里,不进 `reason`,理由与作业失败相同:日志里可能有请求内容。

**SoMBench 评测。** SoMBench 的 test 切分有 3481 条样本,数据集卡片登记的样本数与文件大小(2969502 字节)都与本地文件一致。题型 Q1 713、Q2 626、Q3 1661、Q4 481;变体 `base` 1704、`hardest` 1209、`varA` 284、`varB` 284;3 个一级、17 个二级、71 个三级维度,每个三级维度 42 到 53 道题;样本 id 形如 `1.1.1_0_first_person|base|Q1`,全部唯一。`meta.review_pass` 只在 `base` 上为真,其余变体为假且 `review_fail_reason` 全空,SoMEval 的指标代码声明所有样本默认合格,所以这个字段不用来过滤,含义没有查清。

默认题库只有 2 道占位题,与 SoMBench 的 id、维度体系、题型都对不上,不存在能补出来的映射表,须从 SoMBench 重建。导入命令 `factory import-sombench` 读取 SoMEval 检出目录里的 parquet,写三样东西:分类表(编号做键)、题库(只存 id、三级维度、题型、变体族,打分模式 `someval`)、污染索引(树外)。版本号带数据集哈希前缀,例如 `sombench-5ace4bd4b872`,运行器与准入检查装配时重新算 parquet 的哈希并与版本比对,数据集变了就拒绝。导入命令需要读 parquet,依赖放在可选的 `sombench` 依赖组里,运行时的评测与准入检查只用标准库。

一次评测的流程:
1. 读候选的 `candidate_checkpoint.yaml`,取适配器导出的路径,校验端点与装配的一致。
2. 经 `compute.service.serve` 在该端点上起 vLLM(基座加适配器,上下文上限 40960,DX-21),开隧道,持有 GPU 租约。
3. 在本机写一份 SoMEval 实验配置到工作目录:数据集 SoMBench、协议 `cot`、模型地址指向隧道、模型密钥用环境变量占位;以工作目录为当前目录,分两次起子进程,先 `predict`,再 `metric`(出图阶段不跑:它不产生分数,而绘图失败会让子进程以非零码退出,此时分数其实已经写好)。
4. 评测凭证只经子进程环境变量传入(DX-20)。SoMEval 的任务配置里裁判端点与密钥是环境变量占位符,由它自己展开。
5. 读 `detailed_metrics.jsonl`,每条记录取 `is_correct`、`extracted`、`error_reason`,按样本 id 对回题库,组成 `SampleOutcome`。题库里有而结果里没有的,走确定性失败。
6. 扫描工作目录里所有文件,任一凭证字面值出现即删目录并让守护进程退出。收尾:关隧道、停服务、放租约。

SoMEval 的 `error_reason` 要分开对待:`wrong_answer`、`below_threshold`、`extraction_failed` 是模型的真实表现,计为错;`judge_error` 是裁判端点出了问题,整次评测作废并按瞬态失败延后;`content_none` 是模型客户端重试耗尽后没有任何内容,可能是模型没写完(思考耗尽长度)也可能是服务异常,单独计数,占比超过阈值(默认 1%)就按瞬态失败处理,不超过则计为错并把数量写进报告的 `someval` 块。阈值是估计值,需用真机数据校准。

进入分数的是 `is_correct` 的 0 或 1;开放题的 0 到 10 分总分不单独进报告,只用「是否过 7.0」这一位,因为 Wilson 区间要求二项结果。

SoMEval 的结果目录名由「数据集、模型名、时间戳」决定。模型名固定为 `forge`。时间戳由 `run_eval.py` 自己生成,它还会覆盖环境变量 `RUN_TIMESTAMP`,所以调用方事先猜不到目录名:运行器在 `predict` 前后各列一次 `results/SoMBench/forge/` 下的 `exp_*` 目录,新增的那一个就是本次的结果,再把它交给 `metric`。新增目录不是恰好一个就按确定性失败处理。每次运行使用全新的工作目录,不复用旧结果。工作目录放在树外,与 SoMEval 检出目录也不在一起:检出目录里有他人未提交的改动,我们不读写除数据集、任务配置和入口脚本之外的任何东西,也不把我们的输出写进去。

**污染索引与准入检查。** 索引由导入命令从 SoMBench 的故事、问题、全部选项和 Q4 参考答案生成:每段文本做 NFKC 规范化、转小写、只保留字母数字(含汉字),取每个 20 字符窗口的 8 字节 blake2b 哈希,去重排序后写成二进制文件(约 76 万个窗口,约 6 MiB),头部记数据集的完整 sha256。索引存哈希而不是原文,但窗口只有 20 字符,逐一穷举并不可行,所以文件放在树外,不进 git。训练执行器在上传 `train.jsonl` 之前,对每一行的 `instruction` 与 `output` 做同样的规范化与窗口哈希,在索引里二分查找,返回命中窗口数;命中数大于 0 就抛确定性异常,整条训练指令失败(T2),失败原因里只有命中行数与窗口数,不含文本。

准入检查装配在 `--executor roll` 的路径上,不依赖是否开了 `--evaluator someval`:只要会真的训练,就必须有索引,没有或哈希对不上就拒绝装配。mock 执行器不接触真实权重,不装准入检查。窗口长度 20 的取值依据是:SoMBench 自身 20 字窗口里,出现在 10 段以上不同文本中的只有 37 个,多是「判断哪些成立,选出所有正确项」这类题干模板,没有任何窗口出现在 50 段以上,说明模板重复很少;但对模拟器生成的对话是否会误命中,没有实测,要拿已有的 episode 跑一遍确认。

下表第 1、2、4 项都要接入真实后端才能核实,mock 输出给不出答案。

| 未核实项 | 影响 | 核实办法 |
|---|---|---|
| 真实 SoMEval 的输出能否按样本 id 完整对回题库 | 适配层要改 | 已在本机演练一次:SoMEval 检出目录的副本、9 道题的子集(Q1 到 Q4 各有、含一道 `varA`),模型与裁判换成本机的 OpenAI 兼容替身,运行器走真实的两次子进程。9 道题全部对回,裁判只收到 2 次调用(两道 Q4),两把密钥各自到了正确的端点,工作目录里没有任何密钥。还差真机上的一次 |
| 默认阈值 0.5、0.2、0.7、0.15 是否合适 | 归类与探针判定偏松或偏严 | 用真实报告分布与人工标注校准 |
| judge 与人工的一致率 | judge 漂移无监控 | 建小型人工标注校准集,定期比对 |
| k 为 1 时区间宽度是否足以检出真实退化 | 真实退化漏标 | 用真实后端对已知退化的 checkpoint 做灵敏度试验 |
| 不打 ROLL 的 `split_k` 补丁时,同一请求多次采样的结果是否可重复 | 评测跨轮比较可能带入浮点噪声 | 同一提示重复请求若干次,比较对数概率的方差;有差异再评估是否纳入该补丁 |
| 适配器在真实训练后的效果是否变好 | 只验证了「被加载且改变了输出」,训练只有两步,不说明变对 | 用更长的训练产出适配器,在固定题上比基座与适配器的正确率 |
| 上下文上限 40960 下的显存、吞吐与单次评测耗时 | 验证时上下文限 4096 且关了图捕获;SoMEval 历史上一次预测加判分约 2 小时,与我们的配置不同 | 在真机上整套跑一次,记录耗时、峰值显存、`content_none` 占比 |
| 服务端是否把思考内容与答案分开(`--reasoning-parser`) | 不分开时,开放题送给裁判的文本带着整段思考,分数与 SoMEval 历史结果可能不可比 | 起服务时带与不带该参数各跑一批 Q4,比较裁判分数 |
| 滑窗准入检查对模拟器生成的对话是否误命中 | 误命中会让正常训练指令整条失败 | 用已有 episode 跑一遍,统计命中率 |
| `content_none` 占比阈值 1% | 过严则频繁延后,过松则把服务异常算成模型差 | 用真机数据校准 |

明确不做:晋级、规划与配方决定;动态轨的情境与对手人格池生成;SoMBench 污染准入检查之外的数据去重;按目标维度定制题库;多租户与鉴权。

## 附 本文术语

| 术语 | 含义 |
|---|---|
| 静态轨 | 固定题库上的选择题评测,经 SoMEval 运行器执行 |
| 动态轨 | 多轮仿真里的过程性评测,复用 SocialSimulator 运行器,judge 打分 |
| 变体族 | 一道基题加若干扰动变体,按整族的通过模式归类错误 |
| 档位 | `full` 跑全部题,`quick` 每个 L3 取一道 |
| 轨道标注 | 分数的来源:`static`、`dynamic` 或两轨融合的 `fused` |
| Wilson 区间 | 二项比例的置信区间,小样本与 0、1 边界下不越界 |
| 错误画像 | 每个 L2 下各错误类型的占比 |
| 退化标记 | 候选相对基线在某维度上的显著下降,附证据 |
| 基线 | 被比较的已晋级模型所在 run 的报告 |
| 评估器 | 注入调和循环、执行 `request_diagnostic` 的可调用对象 |
| 自评偏差守卫 | 拒绝用候选本身或其近亲做 judge |
| 分类表、题库 | 诊断资产:维度树与题目池,都是版本化产物 |
