# factory 与命令行:把全部组件装起来,给人三个入口

| 项 | 内容 |
|---|---|
| 一句话 | 组合根 factory 读配置、按开关装配派发器、评估器与处理器后启动调和循环;两种语言的命令行分管门控写与只读观测 |
| 状态 | 部分落地。[已落地]:守护进程入口 `mindtrainer.wire` 与 `scripts/run_controlplane.py`,TypeScript 命令行四个命令 `review`、`approve`、`reject`、`status`,观测函数 `controlplane.observe.trace` 与 `controlplane.observe.cost`。[计划中]:新包 `src/factory`(组合根、开关、只读子命令、端到端),命令行泛化与 `confirm` |
| 所在层 | 控制层(入口与组合根) |
| 代码位置 | `src/mindtrainer/wire.py`、`scripts/run_controlplane.py`、`cli/src`、`src/controlplane/observe.py`;`src/factory`、`scripts/run_factory.py`、`scripts/run_factory_dev.py` [计划中] |
| 基线提交 | `8dd295b`(2026-10-10) |
| 依赖 | ControlPlane(调和循环、产物树、配置信封、观测函数)、MindTrainer 派发器;闭环模式下还依赖诊断评测、RecipeStore、TrainPlanner、TrainScheduler、MindAgent、外部推理桥接、SocialSimulator 的实时服务 [计划中] |
| 被依赖 | 无。factory 是依赖图的叶子,只有入口脚本与测试调用它 |
| 对应实施计划 | 命令行泛化与 `confirm`:工作包 8(任务 96 至 97)[计划中];组合根与端到端:工作包 9(任务 100 至 109)[计划中] |

## 0. 系统速览

<!-- shared:begin context-block -->
**系统是什么。** RismForge 是一套"AI 驱动的社会心智后训练工厂":输入通用开源基础模型,输出社会心智能力显著增强的模型,代号 **ZenGen**。工厂自己决定每一轮练什么、怎么练。训练决策可以交给外部更强的闭源模型(经命令行调用),关键决策点设人工审核门,其余环节自动。

**七个子系统。** 六个能力子系统加一个协调层 ControlPlane。

| 子系统 | 职责 | 所在层 | 现状 |
|---|---|---|---|
| ControlPlane | 协调层:git 版本化产物树、状态机、调和循环、三个审核门 | 控制层 | 核心 [已落地],闭环扩展 [计划中] |
| SocialSimulator | 情境与人格生成、多智能体仿真、实时环境服务、诊断评测 | 计算层 | [已落地],诊断用 mock 后端 |
| MindTrainer | 权重级训练:BatchTrain(SFT 后接 GRPO 加 OPD)与 InteractTrain | 计算层 | [已落地],训练执行器是 mock |
| MindAgent | 部署态智能体的技能与记忆自进化,不改权重 | 计算层 | [计划中] |
| RecipeStore | 验证有效的训练配方的版本化仓库 | 控制层 | [计划中] |
| TrainScheduler | 决策执行器:装配上下文、校验决策、构造训练指令、提名配方 | 控制层 | [计划中] |
| TrainPlanner | 决策学习者:经可插拔后端给出下一轮训练决策 | 控制层 | [计划中] |

共享基础设施有四个。**Actio** 是带技能库与显式记忆的智能体框架,每次响应附带激活轨迹。**外部推理桥接**用命令行调用外部强模型。**ROLL** 是分布式训练引擎,**vLLM** 是推理引擎。ROLL 与 vLLM 在 V1 里没有接入真实服务,训练与推理由确定性 mock 顶替。

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

factory 是组合根:系统里唯一知道所有包的地方。它把各子系统装成一个能转起来的闭环,自己不含业务逻辑。

它解决两个问题。第一,ControlPlane 不导入任何新子系统,闭环动作要靠外部注入处理器才会产生,总得有一处负责装配。第二,人要有固定的入口去审核、确认、启动和观测,入口要分清"会改树"与"只读"。

人从三个入口进来。TypeScript 命令行 `rismforge` 直接改产物状态并提交。Python 入口调组合根,装配后启动调和循环。已落地的守护进程入口只注入 MindTrainer 派发器,与 factory 并存。

<!-- figure:begin 90-factory-context -->
![factory 与命令行的上下文](diagrams/90-factory-context.svg)

**图 1　factory 与命令行的上下文**　人经两种语言的命令行进入系统。TypeScript 命令行直接改产物树并提交;Python 入口调用组合根 factory,由它装配各子系统并启动调和循环。原守护进程入口保留,只注入 MindTrainer 派发器。

<details>
<summary>文字版:图中的节点与连线</summary>

**节点**

- 人工:审核人 · 开发者
- TypeScript 命令行:rismforge review · approve / reject · status · confirm
- Python 入口:run_factory.py · run_factory_dev.py / run · reconcile · 只读子命令
- 守护进程入口:run_controlplane.py / → mindtrainer.wire
- factory 组合根:唯一知道所有包的地方 / 任何包都不导入它
- 被装配的子系统:派发器 · 诊断评估器 / 六个处理器 · 外部推理桥接
- git 产物树:control-plane/ / YAML · 每次变更提交
- ControlPlane:调和循环 run_daemon / reconcile_once

**连线**

- 人工 → TypeScript 命令行:审核 · 确认
- 人工 → Python 入口
- 人工 → 守护进程入口
- TypeScript 命令行 → git 产物树:门控写 · 提交
- Python 入口 → factory 组合根:调用
- factory 组合根 → 被装配的子系统:构造
- factory 组合根 → ControlPlane:注入并启动
- ControlPlane ↔ git 产物树:读写
- 守护进程入口 → ControlPlane:只注入 MindTrainer 派发器

</details>
<!-- figure:end 90-factory-context -->

审核人经 TypeScript 命令行做门控写,命令只改状态字段并提交。开发者经 Python 入口启动 factory,由它构造派发器、诊断评估器、六个处理器与外部推理桥接,再注入调和循环 [计划中]。已落地的守护进程入口走同一个调和循环,只注入 MindTrainer 派发器。两侧不互相调用,只经产物树通信。

## 2. 目标、非目标与约束

本子系统只做装配与入口,不做任何训练、评测、规划的判断。

| 编号 | 目标 | 验收方式 |
|---|---|---|
| G1 | 不传 `--enable` 时与现有守护进程入口行为相同 | 两棵相同的仓库分别跑两个入口,整棵 `control-plane/` 与全部提交说明逐字节相同 [计划中] |
| G2 | 五个开关恰好覆盖六个闭环处理器键 | 开关映射表的单测;每个键接到真实处理器 [计划中] |
| G3 | 闭环模式的装配全部在第一个 tick 之前完成 | 缺配置、资产、角色、凭证时退出码 2、一行错误、树里不写东西 [计划中] |
| G4 | 门控写命令证据不在场就拒绝 | 候选缺诊断时 `approve` 抛错且不提交 [已落地];配方候选与提案的证据规则 [计划中] |
| G5 | 只读命令不改 `HEAD` 与工作区 | 前后比较 `HEAD` 与工作区快照 [计划中] |
| G6 | 闭环能从一条人工指令转到第二轮训练 | 端到端测试 `test_the_whole_loop_turns_twice` [计划中] |

| 非目标 | 状态 | V1 的处理 |
|---|---|---|
| 真实 ROLL、真实 SoMEval 与 judge、真实 vLLM | [推迟] | 训练用 mock 执行器,诊断用 mock 后端,MindAgent 用罐头后端 |
| 桥接的 API 形态 | [推迟] | `invocation_form` 不是 `cli` 时装配报错 |
| 运行中热加载配置 | [推迟] | 改 `evaluator.yaml`、`backends.yaml`、`agent.yaml` 要重启 |
| 进程自我重启与重试 | [推迟] | 瞬态异常让进程退出,由外部监督者重启 |
| 图形界面 | [推迟] | 只有命令行 |
| 一个命令行统管两种语言 | 不做 | 门控写在 TypeScript,调和与只读在 Python(R16) |

| 约束 | 内容 | 影响 |
|---|---|---|
| 依赖方向 | factory 可以导入任何包,任何包都不导入 factory | factory 不导出名字,`__init__.py` 只有说明 [计划中] |
| 原入口冻结 | `mindtrainer.wire` 与 `scripts/run_controlplane.py` 不删、不改 | 两个入口并存;改动范围用 `git diff` 证明 [计划中] |
| 无新第三方依赖 | 只用标准库 `argparse`、`hashlib`、`urllib.parse`、`inspect` | `pyproject.toml` 的 `dependencies` 不动 |
| 凭证不外露 | 凭证不进 git 树,值不进标准输出、标准错误、异常信息、日志 | 凭证相关的异常一律断开异常链 |
| 只走信封 | 读写产物走 `GitStore` 与 `Envelope`,配置走配置信封 | 唯一的直接文件读取是 `logs` 读日志文件 |
| mock 证据默认关 | 默认入口不接受 mock 证据,开发入口显式打开(R17) | 默认配置下闭环不会自己转 |

## 3. 架构

入口分两种语言、三个入口,组合根在 Python 一侧的最右端。

<!-- figure:begin 90-factory-commands -->
![命令地图](diagrams/90-factory-commands.svg)

**图 2　命令地图**　TypeScript 命令行管门控写命令,直接改产物状态并提交。Python 一侧管调和与只读可观测,只读子命令不提交。两侧只经产物树通信。

<details>
<summary>文字版:图中的节点与连线</summary>

**节点**

- review <id>:按类型打印证据 / 只读
- status:列出待审与待确认 / 只读
- approve <id>:证据不在场就拒绝 / pending_review → approved
- reject <id> [原因]:写 reject_reason / pending_review → rejected
- confirm <directive-id>:awaiting_confirmation → pending
- run:一直跑调和循环 / 退出码 0 或 2
- reconcile --once:跑一轮并打印动作 / 退出码 0 或 2
- install-defaults:显式写默认资产与配置 · run 与 reconcile 从不自动写
- trace · cost · logs <run>:只读 · 不提交 · 缺失时退出码 1(cost 除外)
- git 产物树:control-plane/ · 两侧唯一的通信面

**连线**

- approve <id> → git 产物树:写状态并提交
- confirm <directive-id> → git 产物树
- trace · cost · logs <run> → git 产物树:只读

</details>
<!-- figure:end 90-factory-commands -->

左边是 TypeScript 命令行,四个命令已落地,`confirm` 与按类型审核 [计划中]。右边是 Python 入口 factory 的子命令,全部 [计划中];其中 `trace`、`cost` 的函数已在 `controlplane.observe` 落地。只读子命令只读树,不提交。

| 模块 | 职责 | 依赖 | 状态 |
|---|---|---|---|
| `cli/src/index.ts` | 命令分发,未知命令打印用法、退出码 1 | `commands.ts` | [已落地] |
| `cli/src/commands.ts` | `review`、`approve`、`reject`、`status`,需审核类迁移表 | `gittree.ts`、git 命令行 | [已落地] |
| `cli/src/gittree.ts` | `readProduct`、`writeProduct`(写文件、`git add`、`git commit`) | js-yaml、git 命令行 | [已落地] |
| `src/mindtrainer/wire.py` | 守护进程入口逻辑:`run`、`main` | `controlplane.daemon`、`mindtrainer.dispatch` | [已落地] |
| `scripts/run_controlplane.py` | `mindtrainer.wire.main` 的薄壳 | `mindtrainer.wire` | [已落地] |
| `src/controlplane/observe.py` | `trace`、`cost`、`sum_costs`,只读 | `controlplane.gitstore`、`controlplane.paths` | [已落地] |
| `factory.switches` | `ENABLE_MAP`、`parse_enable`、`handler_keys` | 无 | [计划中] |
| `factory.wire` | `FactoryOptions`、`BuildContext`、`assemble`、`run`、`reconcile_one_tick` | 全部包 | [计划中] |
| `factory.cli` | argparse 子命令、开关、退出码、`main`、`dev_main` | `factory.wire`、`factory.readonly` | [计划中] |
| `factory.readonly` | `trace`、`cost`(即观测函数)、`collect_log_refs`、`render_logs` | `controlplane.observe` | [计划中] |
| `factory.bridges` | `BRIDGE_ROLES`、`build_bridge` | `inference_bridge`、`controlplane.config` | [计划中] |
| `factory.evaluation` | `EVALUATOR_CONFIG`、`read_evaluator_config`、`build_mock_evaluator` | `diagnostic` | [计划中] |
| `factory.handlers` | `DEFAULT_BUILDERS`、`scheduler_config_loader` | `recipestore`、`trainscheduler`、`trainplanner`、`mindagent` | [计划中] |
| `factory.agent` | `agent.yaml` 读取、`AgentRuntime`、`DeadlineStub`、`CannedGuidedBackend` | `mindagent`、`simulator.realtime` | [计划中] |
| `factory.defaults` | 默认诊断资产、评估器配置、部署素材、`install_defaults` | `diagnostic`、`simulator`、`trainplanner`、`trainscheduler` | [计划中] |

两个入口脚本 `scripts/run_factory.py` 与 `scripts/run_factory_dev.py` 都是 `factory.cli` 的薄壳 [计划中]。前者默认不接受 mock 证据,后者对 `run`、`reconcile` 自动加 `--allow-mock-evidence`。

<!-- figure:begin 90-factory-layers -->
![依赖方向与分层守卫](diagrams/90-factory-layers.svg)

**图 3　依赖方向与分层守卫**　factory 在依赖图最右端,可以导入任何包,任何包都不导入它。红色虚线是守卫测试禁止的导入边。

<details>
<summary>文字版:图中的节点与连线</summary>

**节点**

- factory:组合根 · 叶子
- controlplane:协调层核心
- simulator:仿真 · 实时服务
- mindtrainer:训练 · 派发器
- diagnostic:诊断评测
- 闭环新包:recipestore · trainplanner / trainscheduler · actio / mindagent · inference_bridge

**连线**

- simulator → controlplane:导入
- mindtrainer → controlplane:导入
- diagnostic → controlplane:导入
- mindtrainer → simulator:导入
- factory → mindtrainer:导入任何包
- factory → simulator
- factory → diagnostic
- factory → 闭环新包
- mindtrainer ↔ diagnostic:互不导入(虚线)
- controlplane → mindtrainer:禁止(虚线)
- controlplane → 闭环新包:禁止(虚线)

</details>
<!-- figure:end 90-factory-layers -->

依赖方向规则:

| 包 | 可以导入 | 不得导入 | 状态 |
|---|---|---|---|
| `factory` | 任何包 | 无 | [计划中] |
| 其余任何包 | 各自的规则 | `factory` | 守卫测试 [计划中] |
| `controlplane` | `simulator.stubs`(骨架桩,唯一回指) | `diagnostic`、`mindtrainer` 与七个新包 | 今天成立;常驻守卫 [计划中] |
| `mindtrainer` | `controlplane`、`simulator` | `diagnostic` | 今天成立;常驻守卫 [计划中] |
| `diagnostic` | `controlplane`、`simulator` | `mindtrainer` | 今天成立;常驻守卫 [计划中] |

"今天成立"是在基线提交上用 grep 查的:三个已落地包里没有越界的绝对导入。常驻守卫 `test_layer_rules_hold` 用语法树扫真实源码,只看绝对导入的顶层包名 [计划中]。

## 4. 数据模型与状态机

本子系统不拥有产物状态机。它读写配置信封,并经命令行驱动别人的状态迁移。

| 实体 | 字段与含义 | 约束 |
|---|---|---|
| `FactoryOptions` | `switches`、`allow_mock_evidence`、`confirm_directives`、`planner_backend`、`repo_commit` | 冻结数据类;`confirm_directives` 三态,`None` 表示用配置 [计划中] |
| `BuildContext` | 仓库、证据开关、确认覆盖、规划后端、环境变量、日志目录、`repo_commit`、收尾函数列表 | 交给处理器构造器;`bridge(role)` 每次新建一个桥接 [计划中] |
| `Assembly` | 派发器、处理器字典、评估器、证据开关、收尾函数 | 处理器为空即默认路径;`close()` 逆序收尾 [计划中] |
| `ENABLE_MAP` | 开关到处理器键的固定表 | 五个开关、六个键,见下表 [计划中] |

| 开关 | 处理器键 | 处理器来自 |
|---|---|---|
| `plan` | `plan_next_round` | TrainScheduler 的规划处理器 [计划中] |
| `nominate` | `nominate_recipe` | TrainScheduler 的提名处理器 [计划中] |
| `track` | `record_track` | RecipeStore 的战绩处理器 [计划中] |
| `agent` | `start_agent_round` | MindAgent 的轮次处理器 [计划中] |
| `admit` | `admit_recipe`、`admit_proposal` | RecipeStore 与 MindAgent 的收录处理器 [计划中] |

| 产物类型 | 路径(`control-plane/` 下) | 状态机种类 | 写方 | 状态 |
|---|---|---|---|---|
| 评估器配置 | `config/evaluator.yaml` | 配置信封 | `install-defaults`,之后由人改 | [计划中] |
| MindAgent 部署素材 | `config/agent.yaml` | 配置信封 | `install-defaults`,之后由人改 | [计划中] |
| 规划提示模板 | `config/prompts/planner-<版本>.yaml`,默认模板 `PLANNER_V1` | 提示模板信封 | `install-defaults`,之后由人改 | [计划中] |
| 调度配置与训练默认值 | `config/scheduler.yaml`、`config/training_defaults.yaml` | 配置信封 | `install-defaults`,之后由人改 | [计划中] |
| 默认部署情境与人格 | `scenarios/` 下 `factory-deploy-s1` 至 `s3`,`personas/` 下两个人格 | 情境与人格产物 | `install-defaults` | [计划中] |
| 桥接配置 | `config/backends.yaml` | 配置信封 | 人 | 路径函数 [已落地],角色形状 [计划中] |
| 默认分类表与题库 | `diagnostics/` 下 `factory-tax-v1`、`factory-pool-v1` | 登记即 `effective` | `install-defaults` | [计划中] |
| 候选 checkpoint | `runs/<run>/candidate_checkpoint.yaml` | 需审核 | 命令行改 `approved` 或 `rejected` | [已落地] |
| 配方候选、技能提案 | `recipe_candidates/`、`skill_memory_proposals/` | 需审核 | 命令行改状态 | [计划中] |
| 训练指令 | `runs/<run>/directive.yaml` | 非审核 | `confirm` 改 `pending` | [计划中] |

`evaluator.yaml` 的载荷恰好五个键,`backends` 不可配置,恒为 `mock` [计划中]:

```yaml
taxonomy_version: factory-tax-v1
item_pool_version: factory-pool-v1
benchmark_version: factory-bench-v1
tier: full          # full 或 quick
sampling_k: 4       # 整数,至少 1
```

`agent.yaml` 的载荷必填三个键:`scenario_ids`、`bindings`(角色绑定字典的列表)、`opponent_lines`(对手台词列表)。可选键是 `AgentConfig` 的其余八个字段,缺省取它的默认值。未知键与类型错误都是装配错误,退出码 2 [计划中]。`install-defaults` 写入的默认值:三个部署情境 `factory-deploy-s1` 至 `s3`;`user` 位绑定 `frozen_reference`(`model_ref` 为 `zengen-v1`),`agent` 位绑定 `self_play`;对手台词三句;`max_turns` 为 4,`min_support` 为 3,`min_ratio` 为 0.5 [计划中]。

命令行驱动的迁移:

| 命令 | 产物 | 迁移 | 状态 |
|---|---|---|---|
| `approve <id>` | 候选 checkpoint | `pending_review → approved` | [已落地] |
| `reject <id> [原因]` | 候选 checkpoint | `pending_review → rejected`,写 `reject_reason` | [已落地] |
| `approve`、`reject` | 配方候选、技能提案 | 同上 | [计划中] |
| `confirm <directive-id>` | 训练指令 | `awaiting_confirmation → pending` | [计划中] |

不变量:

1. 不传 `--enable` 时,factory 不读任何配置文件,不注入派发器以外的任何东西 [计划中]。
2. 开了任一闭环开关,就同时注入数据生产器与 mock 后端的真实评估器 [计划中]。
3. 证据开关只有一个来源:命令行 `--allow-mock-evidence`。同一个值交给调和循环与提名处理器 [计划中]。
4. 确认覆盖不写回 `scheduler.yaml`,只改 loader 的返回值 [计划中]。
5. 门控写命令在迁移不合法或证据不在场时,不写文件、不提交。
6. 已存在的配置文件 `install-defaults` 不覆盖,人改过的配置优先 [计划中]。

## 5. 接口契约

对外只承诺入口函数与几个数据类,其余模块都是内部实现。

已落地的守护进程入口:

```python
# mindtrainer.wire
def run(repo_root: str, *, interval_s: float = 2.0, max_ticks: int | None = None) -> None
def main(argv: list[str] | None = None) -> int
# 命令行:scripts/run_controlplane.py <repo_root> [--interval-s S] [--max-ticks N]
```

计划中的组合根,签名来自施工计划,施工时可能微调 [计划中]:

```python
# factory.wire
def assemble(store, options, *, env=None, log_dir=None, builders=None) -> Assembly
def run(repo_root, options=FactoryOptions(), *, interval_s=2.0, max_ticks=None,
        env=None, log_dir=None, builders=None) -> None
def reconcile_one_tick(repo_root, options=FactoryOptions(), *, env=None,
                       log_dir=None, builders=None) -> list[Action]
# factory.cli
def main(argv=None, *, env=None, builders=None) -> int
def dev_main(argv=None, **kw) -> int
```

Python 子命令 [计划中]。每个子命令的第一个位置参数都是控制面仓库根目录:

| 子命令 | 作用 | 退出码 |
|---|---|---|
| `run <repo>` | 一直跑调和循环,带 `--interval-s`、`--max-ticks` | 0;装配错误 2 |
| `reconcile <repo> --once` | 跑一轮,每个动作打印一行 `<kind> <target_id>`,没有动作打印 `no actions` | 0;不带 `--once` 或装配错误 2 |
| `install-defaults <repo>` | 显式写默认资产与配置,打印涉及的路径 | 0 |
| `trace <repo> <run>` | 原样打印观测函数 `trace` 的输出 | 0;run 不存在 1 |
| `cost <repo> <run>` | 原样打印观测函数 `cost` 的输出 | 0,没有 `run_trace` 也是 0 |
| `logs <repo> <run> [--task <id>]` | 按 run 目录产物的 `log_refs` 打印日志 | 0;run 不存在、有引用不可达或哈希不符 1 |

`run` 与 `reconcile` 共用的开关 [计划中]:

| 开关 | 取值 | 含义 |
|---|---|---|
| `--enable` | `plan,nominate,track,agent,admit` 的子集 | 未知开关与空项是用法错误,退出码 2 |
| `--allow-mock-evidence` | 布尔 | 接受 mock 证据驱动规划、提名与战绩 |
| `--confirm-directives` / `--no-confirm-directives` | 三态 | 不传就用 `scheduler.yaml` 的 `confirm_directives` |
| `--planner-backend` | `bridge`(默认)、`heuristic` | `heuristic` 是确定性后端,不需要外部工具 |
| `--repo-commit` | 提交号 | 不传取代码仓库根的 `git rev-parse HEAD`,取不到为 `None` |

TypeScript 命令行:`rismforge <review|approve|reject|status> [id] [reason]` [已落地],工作包 8 加入 `confirm` [计划中]。

| 命令 | 今天的行为 [已落地] | 泛化后 [计划中] |
|---|---|---|
| `review <id>` | 打印候选头行与诊断摘要;缺报告标 `MISSING`,桩报告标 `[STUB]` | 三类产物按类型打印证据;mock 报告标 `[MOCK BACKENDS]` |
| `approve <id>` | 先判迁移,再要求同 run 目录有诊断报告 | 配方候选要有 `repro`,提案要有 `verification` |
| `reject <id> [原因]` | 写 `reject_reason`,原因缺省为 `no reason` | 三类产物通用 |
| `status` | 列出 `pending_review` 的候选 | 同时列出三类待审产物与待确认指令 |
| `confirm <id>` | 不存在 | 只认 `awaiting_confirmation`;多处命中报歧义 |

对邻居的假设:

| 邻居 | 本文依赖它什么 | 它依赖本文什么 |
|---|---|---|
| ControlPlane | `run_daemon`、`reconcile_once` 接受 `dispatcher`;闭环模式还接受 `handlers`、`evaluator`、`accept_mock_evidence` [计划中] | 有人注入处理器,闭环动作才会产生(R2) |
| MindTrainer | `make_mindtrainer_dispatcher()`;闭环模式传 `data_producer` [计划中] | 无 |
| 诊断评测 | `make_diagnostic_evaluator` 与资产读写 [计划中] | 无 |
| TrainScheduler | `make_plan_handler`、`make_nominate_handler`、`install_default_configs` [计划中] | 传入 `accept_mock_evidence`、`repo_commit` 与带覆盖的 loader |
| TrainPlanner | `install_default_template`、`BridgeDecisionBackend`、`HeuristicBackend` [计划中] | 选后端、安装默认模板由本文负责 |
| SocialSimulator | `create_server`、`EnvironmentServiceClient`、`read_persona`、`read_scenario`、`write_persona`、`write_scenario` | 无 |
| RecipeStore | `handle_admit_recipe`、`handle_record_track` [计划中] | 无 |
| MindAgent | `make_start_round_handler`、`handle_admit_proposal` [计划中] | 实时客户端工厂、罐头后端、人格 loader |
| 外部推理桥接 | `CliBridge` [计划中] | `backends.yaml` 的 `planner`、`agent` 两个角色 |

## 6. 关键流程

主流程是装配:解析开关后分两条路径,闭环路径在第一个 tick 之前装配完毕。

### 6.1 装配

<!-- figure:begin 90-factory-assembly -->
![组合根的装配流程](diagrams/90-factory-assembly.svg)

**图 4　组合根的装配流程**　不传 --enable 时只构造 MindTrainer 派发器,与现有入口逐字节相同。传了 --enable 时读配置、校验资产与凭证、构造评估器与处理器,全部在第一个 tick 之前完成;任一步出错打印一行错误、退出码 2,树里不写任何东西。

<details>
<summary>文字版:图中的节点与连线</summary>

**节点**

- 解析开关:--enable → ENABLE_MAP / 未知开关与空项拒绝
- 有处理器键吗:handler_keys 非空?
- 默认路径:只构造 / make_mindtrainer_dispatcher()
- run_daemon:只传 dispatcher / 不读任何配置
- 读评估器配置:config/evaluator.yaml / 恰好五个键
- 校验诊断资产:分类表与题库在树里 / 评估后端注入
- 构造处理器:DEFAULT_BUILDERS / 桥接 · 凭证 · agent.yaml
- 注入并启动:dispatcher + 数据生产器 / handlers · evaluator
- FactoryConfigError:标准错误打印 factory: error: ... / 退出码 2 · 树里不写任何东西

**连线**

- 解析开关 → 有处理器键吗
- 有处理器键吗 → 默认路径:否
- 默认路径 → run_daemon
- 有处理器键吗 → 读评估器配置:是
- 读评估器配置 → 校验诊断资产
- 校验诊断资产 → 构造处理器
- 构造处理器 → 注入并启动
- 读评估器配置 → FactoryConfigError:缺文件或键
- 校验诊断资产 → FactoryConfigError:缺资产
- 构造处理器 → FactoryConfigError:缺角色或凭证

</details>
<!-- figure:end 90-factory-assembly -->

| 步 | 动作 | 失败时 |
|---|---|---|
| 1 | 解析 `--enable`,经 `ENABLE_MAP` 得到处理器键 | 未知开关或空项,退出码 2 |
| 2 | 处理器键为空:只构造 `make_mindtrainer_dispatcher()`,调 `run_daemon` 时只传派发器 | 无 |
| 3 | 读 `evaluator.yaml`,要求恰好五个键 | `FactoryConfigError`,退出码 2 [计划中] |
| 4 | 校验分类表与题库在树里,构造 mock 评估器 | 同上 |
| 5 | 按 `DEFAULT_BUILDERS` 逐个构造处理器,需要时经 `build_bridge(store, role, *, env=None, log_dir=None)` 建桥接、读凭证、读 `agent.yaml`。`planner` 角色只在 `--planner-backend bridge` 时必需,`agent` 角色可选,缺了就不建桥接,提案生成回到规则模板 [计划中] | 同上;已登记的收尾函数逆序调用 |
| 6 | 注入派发器(带数据生产器)、处理器、评估器、证据开关,启动调和循环 | 运行中的异常原样上抛,进程退出 |

以上步骤全部 [计划中]。第 2 步与今天的守护进程入口走同一条代码路径,所以两者逐字节相同。任一步出错,标准错误打印一行 `factory: error: ...`,树里不写任何东西。

### 6.2 失败与恢复

| 场景 | 发生什么 | 恢复 |
|---|---|---|
| 闭环模式缺评估器配置、诊断资产、桥接角色、凭证或 `agent.yaml` | 启动即退出码 2,指出缺哪份文件 | 跑一次 `install-defaults` 或补文件,再启动 [计划中] |
| 缺规划提示模板或调度配置 | 启动不拦,第一次规划时守护进程退出;重启后同一处再退出 | 先跑 `install-defaults`,再重启 [计划中] |
| 缺凭证或凭证写进了树 | 退出码 2,信息只含变量名或键路径 | 设好环境变量,从树里删掉凭证 [计划中] |
| 规划桥接失败 | 决策 `failed`,写 T3 失败记录,闭环停住(R18) | 人写新指令或手工重置 [计划中] |
| 处理器或派发器抛瞬态异常 | 进程退出 | 外部监督者重启,调和循环从树续接 |
| 运行中删了诊断资产 | 评估器在调用处抛 `DiagnosticAssetError` | 补齐后重启 [计划中] |
| `approve` 时诊断报告缺失 | 抛错,不改文件、不提交 [已落地] | 等报告出来,先 `review` 再批准 |

### 6.3 走查:端到端测试转两圈

<!-- figure:begin 90-factory-e2e -->
![端到端测试走的闭环](diagrams/90-factory-e2e.svg)

**图 5　端到端测试走的闭环**　测试先手工登记全错的基线 run-000 与 zengen-v1,再从人工写的 run-001 出发,每一步都经 factory reconcile --once,人工动作用写状态模拟。主链转到第二轮训练与第三条指令;MindAgent 链在同一套装配下并行验证。

<details>
<summary>文字版:图中的节点与连线</summary>

**节点**

- 登记基线:run-000 全错报告 / zengen-v1
- run-001 训练:派发 · mock 诊断
- 门①批准:写状态模拟 approve
- 晋级 zengen-v2:promote_candidate
- 提名配方:增益 > min_gain
- 门③批准并收录:recipe-run-001
- 规划 run-002:HeuristicBackend clone
- 确认指令:awaiting → pending
- 第二轮训练:晋级 zengen-v3
- 战绩 · 再规划:run-003 待确认
- 两次轮次 start_agent_round:zengen-v2 空库零提案 · 放基线技能 / zengen-v3 产出 pending_review 提案
- 门②批准并收录:admit_proposal / 库版本是字符串

**连线**

- 登记基线 → run-001 训练
- run-001 训练 → 门①批准
- 门①批准 → 晋级 zengen-v2
- 晋级 zengen-v2 → 提名配方
- 提名配方 → 门③批准并收录
- 门③批准并收录 → 规划 run-002
- 规划 run-002 → 确认指令
- 确认指令 → 第二轮训练
- 第二轮训练 → 战绩 · 再规划
- 两次轮次 start_agent_round → 门②批准并收录

</details>
<!-- figure:end 90-factory-e2e -->

主链对应 `test_the_whole_loop_turns_twice` [计划中]。每一步都经 `factory reconcile --once`,人工动作用写状态模拟,与已落地的 `tests/test_mt_wire.py` 同一手法。数值都取自施工计划里的测试断言。

| 步 | 图中节点 | 动作或结果 |
|---|---|---|
| 1 | 登记基线 | 人工登记 `run-000` 的全错 mock 报告与 `zengen-v1`,`links = [cand-run-000, run-000]` |
| 2 | run-001 训练 | 开关 `nominate,track,admit`;依次打印 `dispatch_directive run-001`、`request_diagnostic cand-run-001`、`await_review cand-run-001` |
| 3 | 门①批准 | 写状态模拟 `approve`,候选置 `approved` |
| 4 | 晋级 zengen-v2 | `promote_candidate cand-run-001`;报告证据等级 `mock` |
| 5 | 提名配方 | 增益大于 `0.01`;`nominate_recipe run-001`,配方候选 `pending_review` |
| 6 | 门③批准并收录 | 写状态模拟批准;`admit_recipe recipe-run-001`,配方 `effective` |
| 7 | 规划 run-002 | 开关加上 `plan`、`--planner-backend heuristic`;`plan_next_round run-001`,决策 `clone recipe-run-001`;新指令停在 `awaiting_confirmation`,下一 tick 没有动作 |
| 8 | 确认指令 | 写状态模拟 `confirm run-002`,指令置 `pending` |
| 9 | 第二轮训练 | 训练、诊断、批准,晋级为 `zengen-v3`;复用 `run-001` 的清单,不为 `run-002` 合成 |
| 10 | 战绩 · 再规划 | 同一 tick 打印 `record_track run-002`、`nominate_recipe run-002`(增益 0,跳过)、`plan_next_round run-002`;`run-003` 的指令停在 `awaiting_confirmation` |

默认 mock 评估器对变体族的基题答参考答案、对变体题恒答错,各 `sampling_k` 次。所以每个候选的一级分数严格在 0 与 1 之间,错误画像是 `{"surface_match": 1.0}`。基线全错,第一轮才有正增益;第二轮与第一轮同分,增益为 0。

MindAgent 链对应 `test_the_mindagent_line_runs_inside_the_reconcile_loop` [计划中],图中第 11、12 步:`zengen-v2` 的轮次空库零提案,放入基线技能后 `zengen-v3` 的轮次产出 `pending_review` 提案,批准收录后库版本是 `{"skill": "2", "memory": "0"}`,两个值都是字符串。

### 6.4 九个工作包的顺序与依赖

factory 是最后一个工作包,前八个全部落地后才能施工。

| 序 | 工作包 | 任务范围 | 前置 | 与本文的关系 |
|---|---|---|---|---|
| 1 | 地基 | 任务 19 至 27 | 无 | 配置信封、证据等级、桥接、凭证检查 |
| 2 | 数据合成接线 | 任务 30 至 34 | 1 | 派发器的 `data_producer` 参数 |
| 3 | RecipeStore | 任务 40 至 48 | 1 | 收录与战绩处理器 |
| 4 | TrainPlanner | 任务 50 至 56 | 1 | 两种规划后端、默认提示模板 |
| 5 | TrainScheduler | 任务 60 至 68 | 2、3、4 | 规划与提名处理器、默认调度配置 |
| 6 | Actio | 任务 70 至 78 | 无 | 经 MindAgent 间接使用 |
| 7 | MindAgent | 任务 80 至 89 | 1、6 | 轮次与收录处理器 |
| 8 | 调和循环扩展 | 任务 90 至 98 | 1 | 注入缝、真实诊断评估器、命令行泛化与 `confirm` |
| 9 | 组合根与端到端 | 任务 100 至 109 | 1 至 8 | 本文的 factory 全部内容 |

### 6.5 首次部署

一个新的控制面仓库从零到闭环转起来,按下表的顺序做 [计划中]。

| 步 | 动作 | 谁做 | 说明 |
|---|---|---|---|
| 1 | 建仓库:`GitStore.init()` 建 git 仓库、写本地提交身份、建全部产物目录 | 人 | 之后每次状态变更都是一次提交 |
| 2 | `install-defaults <repo>` | 人 | 写默认分类表与题库、`evaluator.yaml`、规划提示模板、两份调度配置、默认部署情境与人格、`agent.yaml`;已存在的配置不覆盖 |
| 3 | 登记基线 | 人 | 一个已晋级的基线模型,`links` 为 `[候选 id, run id]`,那个 run 里放一份诊断报告。报告分数要低于后续候选,否则增益恒为 0,配方永远不被提名 |
| 4 | 设凭证环境变量,写 `backends.yaml` | 人 | 只有 `--planner-backend bridge` 才需要 `planner` 角色,`agent` 角色可选;用 `heuristic` 规划后端时可以不写 |
| 5 | 写首条训练指令,启动 `factory run` | 人 | 开发入口 `run_factory_dev.py` 自动打开 `--allow-mock-evidence`;默认入口不接受 mock 证据,闭环不会自己转 |

第 3 步没有命令,本文列出的命令都不写基线,只能手写产物;第一条训练指令也要手写完整信封,含二十多个训练配置字段。MindAgent 的空库不会自己长出第一条技能,要人手写技能产物与库状态产物。为这几件事加子命令目前没有排进任何工作包。真实部署前先查 `models/` 下是否已有晋级模型。

## 7. 关键决策

决策来自闭环集成阶段的裁决与工作包 9 的细则。

| 编号 | 决策 | 备选 | 理由 | 判错的代价 |
|---|---|---|---|---|
| R2 | 新动作只有对应处理器被注入时才产生,`controlplane` 不导入新包 | 调和循环直接调各子系统 | 保住依赖方向;不注入时行为不变 | 入口要自己装配处理器 |
| R16 | 命令拆两个入口:TypeScript 管门控写,Python 管调和与只读 | 一个入口 | 门控写已在 TypeScript 落地;调和本来在 Python | 用户要记两个入口 |
| R17 | 默认不接受 mock 证据,开发入口显式打开 | 默认接受 | 防止把假分数当真证据驱动闭环 | 默认配置下闭环不会自己转 |
| FC-1 | 五个开关到六个处理器键是一张固定表,`admit` 同开两类收录 | 每个键一个开关 | 两类收录都是"门已批准就写进正式库";拼错的开关若被忽略,动作永不触发且无提示 | 想只收录配方、不收录提案做不到 |
| FC-2 | 不传 `--enable` 时只构造派发器,不读任何配置 | 默认也换评估器 | 与已落地入口逐字节相同,迁移零风险 | 想只换评估器、不开闭环做不到 |
| FC-3 | 开了任一开关就同时注入数据生产器与 mock 评估器,评估器不单设开关 | 评估器单独开关 | 桩诊断证据等级恒为 `stub`,闭环一个动作都不会触发 | 闭环模式的报告恒为 mock,审核门醒目标出 |
| FC-4 | 评估器参数放 `evaluator.yaml`,恰好五个键,`backends` 恒为 `mock` | 可配置成 `real` | V1 只有 mock 运行器,允许配成 `real` 等于谎报证据等级 | 换题库必须同时换 `benchmark_version`,否则跨题库比较 |
| FC-5 | 默认资产由 `install-defaults` 显式写入,`run` 与 `reconcile` 从不自动写 | 启动时自动补 | 组合根不应静默往树里写东西 | 第一次用闭环模式要多跑一条命令 |
| FC-6 | 装配全部在第一个 tick 之前完成,出错退出码 2,树里不写东西 | 运行到用时才报错 | 启动即失败比跑到第一个候选才崩好查 | 资产在运行中被删时仍在调用处抛错 |
| FC-7 | 凭证错误一律断开异常链,信息只含变量名与键路径 | 保留原异常 | traceback 里也不会出现凭证值 | 排查时看不到原始异常类型 |
| FC-8 | 退出码:0 成功,1 只读命令发现缺失,2 用法或装配错误;`cost` 没记账时仍是 0 | 统一非 0 | 没有记账是正常状态 | 用 `cost` 判断 run 是否存在会判错,要用 `trace` |
| FC-9 | `--allow-mock-evidence` 是证据开关的唯一来源 | 各处读各自配置 | 两处各读各的会出现"有提名动作而处理器跳过",跳过还会写幂等标记 | 只改 `scheduler.yaml` 打开 mock 证据没有效果 |
| FC-10 | 确认覆盖在 loader 里替换返回值,不写回文件 | 写回 `scheduler.yaml` | 人看到的配置就是默认行为 | 事后看配置看不出指令为什么没停在确认态 |
| FC-11 | `repo_commit` 取组合根所在代码仓库的 HEAD,启动时取一次 | 取控制面仓库的 HEAD | 控制面仓库不是代码仓库 | 长跑跨提交时记的是启动时的提交 |
| FC-12 | 端到端用确定性的 `HeuristicBackend` 规划,桥接只单测正向与失败路径 | 罐头桥接 | 每一步都是真实代码,命令行一个开关选得到 | 桥接往返的格式问题要到接真实后端才暴露 [计划中] |
| FC-13 | MindAgent 用罐头后端与进程内 loopback 实时服务端,首次需要时才起 | 起外部服务端 | V1 没有加载 checkpoint 的推理后端 | 部署 episode 只证明路由与轨迹,不代表模型能力 |

## 8. 失败处理与可观测

装配错误一律在启动时暴露;运行中的失败沿用各子系统的分类,factory 不再加一层。

| 失败点 | 类 | 处理 | 留痕产物 |
|---|---|---|---|
| 开关拼错、`reconcile` 缺 `--once` | 用法错误 | 退出码 2,一行错误 | 无,树里不写 [计划中] |
| 缺配置、资产、桥接角色或凭证 | 装配错误,发生在启动前,不属于 T1 至 T3 | 退出码 2,`FactoryConfigError` | 无,树里不写 [计划中] |
| 规划桥接失败 | T3 | 决策 `failed`,不写新指令(R18) | 决策与失败记录,带桥接日志的 `log_refs` [计划中] |
| 轮次产物读不出 | T3 | 轮次处理器重写为 `failed`,循环继续(R19) | 轮次产物与失败记录 [计划中] |
| 轮次中途瞬态异常或被杀 | T1 | 轮次留在 `pending`,下一 tick 复用已落盘的 episode 续上(R19) | 已落盘的 episode [计划中] |
| 派发器或处理器抛瞬态异常 | T1 | 进程退出,外部监督者重启 | 树里已提交的部分 |
| 门控写证据不在场或迁移非法 | 用法错误 | 命令抛错,不写文件、不提交 | 无 [已落地] |

只读观测手段:

| 想知道 | 用什么 | 状态 |
|---|---|---|
| 一轮的全部产物、状态与溯源链 | `controlplane.observe.trace`;`factory trace <repo> <run>` | 函数 [已落地],子命令 [计划中] |
| 一轮的成本明细与汇总 | `controlplane.observe.cost`;`factory cost <repo> <run>` | 函数 [已落地],子命令 [计划中] |
| 失败记录登记的日志 | `factory logs <repo> <run> [--task <id>]` | [计划中] |
| 待审候选 | `rismforge status` | [已落地] |
| 候选的诊断证据 | `rismforge review <id>` | [已落地] |
| 本 tick 做了什么 | `factory reconcile <repo> --once` 的输出 | [计划中] |

`logs` 对不可达的引用标 `[unreachable]` 并写明不显示内容,哈希不符标 `[sha256 mismatch]` 但照样打印 [计划中]。成功调用的桥接日志不进树,只在控制面仓库的 `.rismforge/logs/<run>/` 目录里。

## 9. 测试策略

已落地部分用真实的 git 仓库与 mock 执行器;计划中部分在此之上加真实的闭环处理器,外部工具一律用替身。

| 层次 | 对象 | 替身 | 测试文件 | 用例数 |
|---|---|---|---|---|
| 入口 | 守护进程入口单 tick、参数解析、到晋级、重复 tick 幂等 | `MockExecutor`、`ScriptedBackend` | `tests/test_mt_wire.py` | 4 [已落地] |
| 观测 | `trace`、`cost`、`sum_costs` | 临时仓库 | `tests/test_observe.py` | 5 [已落地] |
| 命令行 | `review`、`approve`、`reject`、`status` | 临时 git 仓库 | `cli/test/commands.test.ts` | 17 [已落地] |
| 命令行 | 命令名常量 | 无 | `cli/test/smoke.test.ts` | 1 [已落地] |
| 开关与边界 | 映射表;无包导入 factory | 无 | `tests/test_factory_switches.py`、`tests/test_factory_boundaries.py` | 17、2 [计划中] |
| 只读 | `trace`、`cost`、`logs` | 临时仓库 | `tests/test_factory_readonly.py` | 10 [计划中] |
| 桥接装配 | 角色、凭证不外露 | `sys.executable -c` 假可执行体 | `tests/test_factory_bridges.py` | 14 [计划中] |
| 评估器装配 | 配置校验、默认资产 | `MockSoMEvalRunner`、`MockJudgeBackend` | `tests/test_factory_evaluation.py` | 12 [计划中] |
| 组合根与命令行 | 逐字节相同、注入、装配错误、退出码 | 记录调用的假处理器 | `tests/test_factory_wire.py`、`tests/test_factory_cli.py` | 14、8 [计划中] |
| 端到端框架 | 空闭环一个 tick、mock 证据默认不转 | 假处理器 | `tests/test_factory_e2e_frame.py` | 4 [计划中] |
| 处理器构造 | 六个构造器、证据开关单一来源、Interact 流超时 | 罐头后端 | `tests/test_factory_handlers.py` | 18 [计划中] |
| 端到端 | 完整闭环、R5、R18、R19、MindAgent 链 | `HeuristicBackend`、罐头后端 | `tests/test_factory_e2e_loop.py` | 9 [计划中] |
| 跨工作包 | 共享用例表、分层规则、run 编号数值序 | 无 | `tests/test_factory_cross_plan.py`、`tests/test_factory_run_order.py` | 31、3 [计划中] |

计划中的用例共 142 个,按 pytest 收集的参数化展开计。工作包 9 另有一个前置核对脚本 `scripts/check_factory_assumptions.py`,19 行全部 `OK` 才能施工 [计划中]。

不依赖的外部服务:GPU、外部网络、真实模型、真实 `claude` 与 `codex` 命令行。实时服务端只绑 `127.0.0.1`,在进程内起停。

## 10. 现状、局限与未核实项

已落地的只有原有入口、四个命令与两个观测函数,组合根整个包尚未存在。

| 能力 | 状态 | 说明 |
|---|---|---|
| 守护进程入口 `mindtrainer.wire` 与 `scripts/run_controlplane.py` | [已落地] | 只注入 MindTrainer 派发器;interact 指令因缺实时客户端判 `failed` |
| TypeScript 命令行 `review`、`approve`、`reject`、`status` | [已落地] | 只认候选 checkpoint |
| 观测函数 `trace`、`cost` | [已落地] | 没有命令行入口,只能在 Python 里调用 |
| 命令行泛化与 `confirm` | [计划中] | 工作包 8 任务 96 至 97 |
| 包骨架、开关、只读、桥接、评估器装配 | [计划中] | 工作包 9 任务 100 至 103 |
| 组合根、命令行、入口脚本 | [计划中] | 任务 104 |
| 端到端框架、前置核对、处理器构造 | [计划中] | 任务 105 至 107 |
| 完整闭环与跨工作包核对 | [计划中] | 任务 108 至 109 |

| 局限 | 影响 | 缓解 |
|---|---|---|
| 闭环模式的诊断恒为 mock 证据 | 默认入口下闭环永远不会自己转 | 开发入口打开 `--allow-mock-evidence` |
| mock 诊断对每个候选打同样的分 | 首轮之后增益恒为 0,不再提名新配方,退化标记不会出现 | 首次闭环人工登记更低分的基线 |
| 装配是一次性的 | 改 `evaluator.yaml`、`backends.yaml`、`agent.yaml` 要重启 | `scheduler.yaml` 与模板每次调用重读 |
| 外部推理与部署会话同步执行 | 调用期间调和循环不转,审核被饿住 | 记录调用时长,必要时改异步 |
| 瞬态异常让进程退出 | 需要外部监督者,但没有任何工作包交付监督脚本、重启退避或崩溃计数;规划与轮次处理器在非桥接瞬态异常下没有重入次数上限,只有 MindTrainer 的续训次数有上限 | 调和循环无状态,重启从树续接 |
| `logs` 看不到成功调用的桥接日志 | 只能看到失败记录登记的引用 | 直接看 `.rismforge/logs/<run>/` |
| 默认诊断资产是一个一级维度、两道题的最小夹具 | 只证明流程,规划器只看到一个维度 | 换真实题库 |
| 一条部署会话超过 15 秒即 `DEADLINE_EXCEEDED` | 轮次 `failed` | 调 `interact_deadline_s`,目前不在 `agent.yaml` 里 [计划中] |
| TypeScript 的 `writeProduct` 无条件提交,读取不经迁移与 Schema 校验 | 两侧校验强度不同;空提交会抛错 | 命令行只改状态字段;根治要两侧共用读写 |

| 未核实项 | 影响 | 核实办法 |
|---|---|---|
| 本文计划中部分的签名与用例数(共 142)未实跑,只来自施工计划文本及其在集成拷贝上的重放 | 施工时签名可能微调,用例数可能变化 [计划中] | 工作包 9 落地后,按代码与 `uv run pytest --collect-only -q` 复核 |
| 前八个工作包的符号名最终是否与本文同名 | 组合根引用的名字可能对不上 [计划中] | 施工前跑 `scripts/check_factory_assumptions.py`,19 行全部 `OK` [计划中] |
| 外部命令行的参数模板与用量输出 | 成本数字可能缺失,模板可能要调 | 用真实 `claude`、`codex` 各调一次,看输出里有没有用量 |
| 规划经真实桥接的正向路径 | 带围栏、带说明文字、被截断的输出只在接真实后端时暴露 | 接真实后端,用 `--planner-backend bridge` 跑一圈 |
| 规划与提案的质量 | 罐头与启发式后端只证明流程与解析 | 接真实后端后人工评审一批决策 |
| 外部推理成本少记 | 重入时同一组 `(task_id, cost_type)` 被去重,`cost` 输出是下限 | 构造一次规划重入,对比桥接日志条数与账本条数 |
| 成本增益上下文大多为空 | 全仓没有 GPU 小时的写入方,规划器只看得到 token 数 | 真实执行器接入后检查 `run_trace` 的 `cost_records` |
| 配方候选的 `repro` 恒有两条缺口 | 审核人容易对错误列表脱敏 | 让评估器把分类表与题库版本写进报告后,看候选的 `repro.errors` 是否清空 |
| 后端从 mock 切到 real 后的头几轮 | real 候选对非 real 基线没有可比增益,提名跳过 | 切换后观察提名标记的 `reason` |
| 已提交产物损坏时进程退出,重启后同一处再抛 | 循环卡死,需人工修树 | 构造一个带合并冲突标记的指令,观察守护进程行为 |
| 第一次闭环没有基线模型 | 没有可比增益就不会提名 | 真实部署前检查 `models/` 下是否已有晋级模型 |
| `factory run` 长跑 | 只用 `--max-ticks` 跑过几轮;端口、通道占用与 15 秒超时都没实测 | 跑数小时长循环,监控进程的端口与文件句柄 |
| TypeScript 泛化只在临时拷贝里跑过 | 真实仓库的 `cli/` 没有动过 [计划中] | 工作包 8 落地后在仓库里跑 `npx vitest run` |

明确不做:真实 ROLL、SoMEval、judge、vLLM 的接入;桥接的 API 形态;运行中热加载配置;进程内自动重试与自我重启;两种语言合并成一个命令行;图形界面。

## 附 本文术语

| 术语 | 含义 |
|---|---|
| 组合根 | 唯一知道所有包、负责把它们装成一个进程的地方,即 factory |
| 装配 | 读配置、构造派发器、评估器、处理器与桥接,注入调和循环 |
| 默认路径 | 不传 `--enable` 时只注入派发器、与守护进程入口逐字节相同的路径 |
| 闭环模式 | 传了任一 `--enable` 开关,注入处理器、数据生产器与 mock 评估器的模式 |
| 开关 | `--enable` 接受的五个短名之一:`plan`、`nominate`、`track`、`agent`、`admit` |
| 处理器键 | 调和循环闭环动作的六个名字,开关经 `ENABLE_MAP` 映射到它们 |
| 门控写命令 | 改产物状态并提交的 TypeScript 命令:`approve`、`reject`、`confirm` |
| 开发入口 | 自动打开 `--allow-mock-evidence` 的 Python 入口脚本 |
| 收尾函数 | 构造器登记的资源释放函数,装配失败或运行结束时逆序调用 |
