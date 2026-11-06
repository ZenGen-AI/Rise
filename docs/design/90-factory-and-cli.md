# factory 与命令行:把全部组件装起来,给人三个入口

| 项 | 内容 |
|---|---|
| 一句话 | 组合根 factory 读配置、按开关装配派发器、评估器与处理器后启动调和循环;两种语言的命令行分管门控写与只读观测 |
| 状态 | 主体落地,默认入口仍走 mock。组合根包 `src/factory`(开关、装配、六个处理器、桥接、评估器、只读子命令)、入口脚本 `scripts/run_factory.py` 与 `scripts/run_factory_dev.py`、TypeScript 命令行五个命令 `review`、`approve`、`reject`、`confirm`、`status`,以及守护进程入口 `mindtrainer.wire` 与 `scripts/run_controlplane.py` [已落地]。限制:两个默认入口只注入 mock 执行器与 mock 诊断;真实 ROLL LoRA SFT 与真实 SoMEval 由 `--executor roll`、`--evaluator someval` 显式装配,真机只各跑通一次,且两者暂不能与 `--enable` 同用;闭环端到端只在 mock 证据、`HeuristicBackend` 与罐头后端上跑过。部署会话超时的可配置与两侧共用读写 [计划中]。`run` 与 `reconcile` 的 `--interact` 装配开关 [已落地]:让 interact 指令从真实入口跑完,训练位由 vLLM 上的真实模型说话,训练仍是 mock,与 `--executor roll` 互斥;端到端测试用本机 HTTP 替身代替 vLLM,真机未跑。记忆装配(树外记忆配置、Starling 可选 extra、每条 rollout 的 Actio 实例、部署路的记忆管理面与实例工厂)[已落地]。场景二的每个角色位都是带记忆的 Actio 智能体,组合根负责装配它们的记忆。记忆按情境世界组织 [已落地]:部署一个情境一个世界,训练一条 rollout 一个世界。真实 GRPO 加 OPD 训练与真实 judge [推迟] |
| 所在层 | 控制层(入口与组合根) |
| 代码位置 | `src/mindtrainer/wire.py`、`scripts/run_controlplane.py`、`cli/src`、`src/controlplane/observe.py`、`src/factory`、`scripts/run_factory.py`、`scripts/run_factory_dev.py`、`src/factory/memory_config.py`、`src/factory/memory_runtime.py`、`src/factory/memory_translate.py` [已落地] |
| 基线提交 | `b826440`(2026-10-21) |
| 依赖 | ControlPlane(调和循环、产物树、配置信封、观测函数)、MindTrainer 派发器;闭环模式下还依赖诊断评测、RecipeStore、TrainPlanner、TrainScheduler、MindAgent、外部推理桥接、SocialSimulator 的实时服务;`--interact` 的记忆装配另依赖 Actio 的记忆端口与可选 extra Starling,`--enable agent` 的部署记忆另依赖 Actio 的部署记忆管理面 [已落地] |
| 被依赖 | 无。factory 是依赖图的叶子,只有入口脚本与测试调用它 |
| 对应实施计划 | 命令行泛化与 `confirm`:工作包 8(任务 96 至 97);组合根与端到端:工作包 9(任务 100 至 109);设计二的组合根部分:替身路径施工计划(agent-memory-standin,第 3、4、5 步里的装配)[已落地];Starling 路径施工计划(agent-memory-starling,第 7 步:本机记忆配置、Starling 解析器、清扫、extra)[已落地];记忆按情境世界组织的组合根部分:`TrainingWorlds`、按情境的解析器、共用一个部署管理面、`deployment_store` 注入、第 18、19 条拒绝项 [已落地] |

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

factory 是组合根:系统里唯一知道所有包的地方。它把各子系统装成一个能转起来的闭环,自己不含业务逻辑。

它解决两个问题。第一,ControlPlane 不导入任何新子系统,闭环动作要靠外部注入处理器才会产生,总得有一处负责装配。第二,人要有固定的入口去审核、确认、启动和观测,入口要分清"会改树"与"只读"。

人从三个入口进来。TypeScript 命令行 `rise` 直接改产物状态并提交。Python 入口调组合根,装配后启动调和循环。已落地的守护进程入口只注入 MindTrainer 派发器,与 factory 并存。

<!-- figure:begin 90-factory-context -->
![factory 与命令行的上下文](diagrams/90-factory-context.svg)

**图 1　factory 与命令行的上下文**　人经两种语言的命令行进入系统。TypeScript 命令行直接改产物树并提交;Python 入口调用组合根 factory,由它装配各子系统并启动调和循环。原守护进程入口保留,只注入 MindTrainer 派发器。

<details>
<summary>文字版:图中的节点与连线</summary>

**节点**

- 人工:审核人 · 开发者
- TypeScript 命令行:rise review · approve / reject · status · confirm
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

审核人经 TypeScript 命令行做门控写,命令只改状态字段并提交。开发者经 Python 入口启动 factory,由它构造派发器、诊断评估器、六个处理器与外部推理桥接,再注入调和循环 [已落地]。守护进程入口走同一个调和循环,只注入 MindTrainer 派发器。两侧不互相调用,只经产物树通信。

## 2. 目标、非目标与约束

本子系统只做装配与入口,不做任何训练、评测、规划的判断。

| 编号 | 目标 | 验收方式 |
|---|---|---|
| G1 | 不传 `--enable`、执行器为默认 mock 时,与现有守护进程入口行为相同 | 两棵相同的仓库分别跑两个入口,整棵 `control-plane/` 与全部提交说明逐字节相同 [已落地]。`--interact` 加入后,此条件改为 `--enable` 与 `--interact` 都不传,新开关默认关,不影响这一比较 [已落地] |
| G2 | 五个开关恰好覆盖六个闭环处理器键 | 开关映射表的单测;每个键接到真实处理器 [已落地] |
| G3 | 闭环模式的装配全部在第一个 tick 之前完成 | 缺配置、资产、角色、凭证时退出码 2、一行错误、树里不写东西 [已落地] |
| G4 | 门控写命令证据不在场就拒绝 | 候选缺诊断、配方候选缺 `repro`、提案缺 `verification` 时 `approve` 抛错且不提交 [已落地] |
| G5 | 只读命令不改 `HEAD` 与工作区 | 前后比较 `HEAD` 与工作区快照 [已落地] |
| G6 | 闭环能从一条人工指令转到第二轮训练 | 端到端测试 `test_the_whole_loop_turns_twice` [已落地] |
| G7 | `--interact` 配置不齐就不装配 | 已落地的十一条拒绝项里,第 3、9 条合用一组装配测试,第 10 条从命令行够不到、没有装配测试(测试钉住的是派发器自己抛的 `ValueError`),其余八条各有装配测试:退出码 2、一行错误、树里不写东西 [已落地] |
| G8 | `--interact` 要求记忆配置齐全:要么不设,走替身记忆;要么合规。部署路 `--enable agent` 与收录 `admit` 同一口径 | 配置缺键或权限不对,`OPENAI_API_KEY` 为空,`memory_library/` 非空(只在 `agent`、`admit` 构造器里查),Starling 是 editable 安装,配置要求 Starling 却未安装或 C++ 扩展载不进来,记忆根目录属于别的仓库,或其下 `runs/` 是符号链接或不是目录:退出码 2、一行错误、树里不写东西。不设配置时启动日志写 `memory backend: stand-in` [已落地] |

| 非目标 | 状态 | V1 的处理 |
|---|---|---|
| 真实 GRPO 加 OPD 训练、真实 judge | [推迟] | 仍用 mock |
| 真实 SoMEval、真实 vLLM(经远端计算层) | [已落地] | 由 `--evaluator someval` 显式启用(FC-16),默认仍是 mock 后端;真机只验证到预测阶段。MindAgent 仍用罐头后端 |
| 真实 ROLL LoRA SFT(经远端计算层) | [已落地] | 由 `--executor roll` 显式启用(FC-15);默认仍是 mock 执行器。装配与配置错误路径有测试;已在真实主机上从训练指令端到端跑到待审候选一次,只覆盖单段两步 |
| interact 指令从真实入口跑通(`--interact`) | [已落地] | 真推理加 mock 训练:设计要证明真模型能进会话、发言能落盘、奖励管线对真实发言成立,不证明模型变好。目前只用本机 HTTP 替身代替 vLLM 验证了会话、落盘与 `policy_backend` 记录,没有在真机上跑过,端到端测试也不断言奖励数值;真实 GRPO 落地后换执行器 |
| 桥接的 API 形态 | [推迟] | `invocation_form` 不是 `cli` 时装配报错 |
| 运行中热加载配置 | [推迟] | 改 `evaluator.yaml`、`backends.yaml`、`agent.yaml` 要重启 |
| 进程自我重启与重试 | [推迟] | 瞬态异常让进程退出,由外部监督者重启 |
| 图形界面 | [推迟] | 只有命令行 |
| 一个命令行统管两种语言 | 不做 | 门控写在 TypeScript,调和与只读在 Python(R16) |
| 把记忆库放进产物树 | 不做 | 库是带 WAL 的 SQLite,字节不稳定,放在树外;追溯靠记忆运行时身份与起点摘要(FC-20) |
| 迁移旧的 `memory_library/` | 不做 | 目录现在为空(MindAgent 从不生成记忆提案),非空则装配期拒绝启动 [已落地] |
| 迁移按人格布局的记忆根目录 | 不做 | 旧数据只来自验证。装配期拒绝启动,信息列出可疑条目,提示移回配置或移出记忆根(FC-37) |
| 共享感知:在场的人都记住同一句话 | [推迟] | 每个实例只写他人的发言,写在自己的主体名下 |
| 跨情境共享一个人格的记忆 | [推迟] | 同一人格在两个情境里,是两个世界里的两个主体 |
| 接 Starling 记忆服务 | [推迟] | 世界协议已在替身与内嵌 Starling 两个后端上做完;服务落地后只换后端实现 |

| 约束 | 内容 | 影响 |
|---|---|---|
| 依赖方向 | factory 可以导入任何包,任何包都不导入 factory | factory 不导出名字,`__init__.py` 只有说明;守卫测试 `test_no_other_package_imports_factory` 扫全部源码 [已落地] |
| 原入口冻结 | `mindtrainer.wire` 与 `scripts/run_controlplane.py` 不删、不改 | 两个入口并存;改动范围用 `git diff` 证明,两个文件相对基线提交无改动 [已落地] |
| 无新第三方依赖,可选 extra 除外 | 本包只用标准库 `argparse`、`hashlib`、`urllib.parse`、`inspect`;读本机主机映射用已有的 `yaml`。新第三方依赖只有可选 extra `memory`(`starling-memory`,钉 git 提交)[已落地] | `pyproject.toml` 的 `dependencies` 不动;`optional-dependencies` 里 `memory = ["starling-memory @ git+https://github.com/ruijieguo/starling@<合并提交>"]`;不装 extra 的环境记忆用替身 |
| 凭证不外露 | 凭证不进 git 树,值不进标准输出、标准错误、异常信息、日志 | 凭证相关的异常一律断开异常链 |
| 只走信封 | 读写产物走 `GitStore` 与 `Envelope`,配置走配置信封 | 直接文件读取只有两处:`logs` 读日志文件;真实后端读本机主机映射文件(路径来自环境变量 `RISE_COMPUTE_HOSTS`,仓库之外) |
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

左边是 TypeScript 命令行,五个命令已落地,其中 `review`、`approve` 按产物类型处理三类门控产物。右边是 Python 入口 factory 的子命令,全部 [已落地];只读子命令只读树,不提交。

| 模块 | 职责 | 依赖 | 状态 |
|---|---|---|---|
| `cli/src/index.ts` | 命令分发,未知命令打印用法、退出码 1 | `commands.ts` | [已落地] |
| `cli/src/commands.ts` | `review`、`approve`、`reject`、`status`,需审核类迁移表 `REVIEW_TRANSITIONS`;`approved → rejected` 不进这张表,它只在 Python 的 `controlplane.states`,只许收录处理器走,人不能把已批准的提案改回拒绝 [已落地] | `gittree.ts`、git 命令行 | [已落地] |
| `cli/src/gittree.ts` | `readProduct`、`writeProduct`(写文件、`git add`、`git commit`) | js-yaml、git 命令行 | [已落地] |
| `src/mindtrainer/wire.py` | 守护进程入口逻辑:`run`、`main` | `controlplane.daemon`、`mindtrainer.dispatch` | [已落地] |
| `scripts/run_controlplane.py` | `mindtrainer.wire.main` 的薄壳 | `mindtrainer.wire` | [已落地] |
| `src/controlplane/observe.py` | `trace`、`cost`、`sum_costs`,只读 | `controlplane.gitstore`、`controlplane.paths` | [已落地] |
| `factory.switches` | `ENABLE_MAP`、`parse_enable`、`handler_keys` | 无 | [已落地] |
| `factory.wire` | `FactoryOptions`、`BuildContext`、`assemble`、`run`、`reconcile_one_tick`;`--interact` 的互斥检查与装配 [已落地];`memory_wiring` 是薄转发(惰性导入,`import factory.cli` 不载入 mindagent),`deploy_turn_timeout_s` 按开关与后端取部署会话的等提交上限;`--interact` 与记忆处理器的分支调 `memory_wiring` 装配记忆;`run` 把清扫函数作为 `on_locked` 传给 `run_daemon`,`reconcile_one_tick` 在锁内自己调 [已落地] | 全部包 | [已落地] |
| `factory.cli` | argparse 子命令、开关、退出码、`main`、`dev_main`;`--interact` 开关 [已落地] | `factory.wire`、`factory.readonly` | [已落地] |
| `factory.readonly` | `trace`、`cost`(即观测函数)、`collect_log_refs`、`render_logs` | `controlplane.observe` | [已落地] |
| `factory.bridges` | `BRIDGE_ROLES`、`build_bridge` | `inference_bridge`、`controlplane.config` | [已落地] |
| `factory.evaluation` | `EVALUATOR_CONFIG`、`read_evaluator_config`、`build_mock_evaluator`;真实 SoMEval 运行器与污染准入检查的装配 | `diagnostic`;真实运行器另加 `compute` | [已落地] |
| `factory.handlers` | `DEFAULT_BUILDERS`、`scheduler_config_loader`;`agent` 与 `admit_proposal` 构造器取装配选好的部署记忆管理面,`agent` 再把它交给轮次处理器,由后者造出 make_instance 给部署驱动器(FC-29);两个构造器装配时都查 `memory_library/` 为空;同一个管理面还交给服务端的 `AgentRuntime`,三方共用;管理面的方法按世界取键 [已落地] | `recipestore`、`trainscheduler`、`trainplanner`、`mindagent` | [已落地] |
| `factory.agent` | `agent.yaml` 读取、`AgentRuntime`、`DeadlineStub`、`CannedGuidedBackend`;interact 用法下带落盘钩子、按角色位选对手驱动。后端工厂收会话信息并造 SessionResources,落盘钩子与登记表按 `session_id` 归属。会话名额准入与滞留表在服务端 servicer 里,`AgentRuntime` 只持 `session_id` 到 run 的登记。流 deadline 由调用方经 `client_factory(deadline_s=)` 传,`interact_deadline_s` 只是没给时的缺省 15 秒。等提交的 `turn_timeout_s` 由 `deploy_turn_timeout_s` 定:`--interact` 开着取 150 秒(后端单次调用另传 `backend_timeout_s` 90 秒,部署路不传、保持服务端的 60 秒),只有 `--enable agent` 且是 Starling 时取 40 秒,替身保持 5 秒。对手位 Actio 实例的 `episode_id` 取会话信息里的逻辑 episode id [已落地]。启用部署轮次时运行时多收可选的 `deployment_store`,部署会话的对手位开部署世界的端口;另持一张部署登记表(部署 episode id 到 round_id);`training_worlds` 让训练会话的对手位加入 rollout 世界;`read_agent_settings` 拒绝同一人格占两个位。 | `mindagent`、`simulator.realtime` | [已落地] |
| `factory.defaults` | 默认诊断资产、评估器配置、部署素材、`install_defaults` | `diagnostic`、`simulator`、`trainplanner`、`trainscheduler` | [已落地] |
| `factory.sombench` | `import-sombench` 子命令:读 SoMEval 检出目录里的 parquet,写分类表、题库与污染索引;pyarrow 在函数内惰性导入 | `diagnostic.assets`、`diagnostic.contamination` | [已落地] |
| `factory.compute_config` | 读树内 `compute/endpoints.yaml` 出 `ComputeEndpoint` 列表,读本机主机映射出 `ComputeClient`;缺文件、键缺失或多余、取值不合、映射里查不到 `endpoint_id` 一律 `FactoryConfigError`,退出码 2,只写键名与路径,不回显取值。`host_deps_root` 是唯一的可选键,旧文件不写它照常读 | `compute`、`factory.configio` | [已落地] |
| `factory.interact_policy` | `--interact` 的策略工厂与对手切换 `OpponentSwitch`(`make_policy_factory`)。每条 rollout 经 `TrainingWorlds.open_rollout` 建一个世界、造一个训练位 Actio 实例,退出时 `close_rollout` 关端口、等服务端端口关完再删世界。对手工厂给每个对手位包一层 Actio 实例。客户端等相邻两条服务端消息 120 秒(`_next_msg`),整条流按 `max_turns × 150` 兜底(常量 `PER_TURN_DEADLINE_S` 取 150,在轨迹源,经 `client_factory(deadline_s=)` 传)。服务端等客户端一次提交的 `turn_timeout_s` 取 150 秒,单次后端调用 90 秒。本模块不再改写运行时的 `interact_deadline_s`,两开关同开时部署会话不借用训练的 deadline [已落地]。训练位与对手位都是 rollout 世界的主体。 | `factory.opponents`、`factory.vllm_policy`、`factory.agent`、`actio`、`mindtrainer` | [已落地] |
| `factory.memory_config` | 读树外记忆配置(`read_memory_config`),检查 `OPENAI_API_KEY` 非空(`check_api_key`),认领记忆根目录的 `.owner`(`claim_memory_root`),取 Starling 的安装来源并拒绝 editable 安装(`starling_install_source`);变量没设返回 `None`。不导入 `starling` | `factory.configio`、`factory.errors` | [已落地] |
| `factory.memory_runtime` | 记忆运行时身份(`STAND_IN_MEMORY_RUNTIME`、`starling_memory_runtime`),起点记忆解析函数 `memory_resolver`(`stand_in_memory_resolver`、`starling_memory_resolver`),记忆根目录布局 `StarlingLayout`,训练世界 `TrainingWorlds` 协议与 `StandInTrainingWorlds`、`StarlingTrainingWorlds` 两个实现(Starling 一个含清扫 `sweep`,FC-27、FC-35),旧布局检查 `check_memory_layout`;`memory_wiring` 按配置在场与否选后端、检查 `runs/`,交出 `MemoryWiring`(FC-29)。替身部署根目录 `stand_in_memory_root`。没装 Starling 也能导入。解析器按情境出摘要。 | `factory.memory_config`、`factory.errors`、`actio`、`mindagent`、`controlplane`、`simulator` | [已落地] |
| factory.memory_translate | 转换后端:包在每个 Actio 实例外面,接住 actio 的 MemoryFailureError,按 `transient` 改抛本模块的两个异常。FactoryMemoryUnavailableError 继承 RoleMemoryUnavailableError(瞬态),FactoryMemoryFailureError 继承 RoleMemoryFailureError(确定性)。两个基类在 `src/simulator/realtime/backend_timeout.py`,所以 simulator 与 mindtrainer 不导入 actio(FC-30)。查询词嵌入失败与 `embed_pending` 有嵌入失败都由 Starling 适配器译成瞬态 MemoryFailureError,不静默;`close` 里的记忆失败一律按确定性处理 [已落地] | `actio`、`simulator.realtime`、`factory.errors` | [已落地] |
| `factory.vllm_policy`、`factory.opponents` | 现有 `VllmPolicy` 与 `ExternalApiBackend`;各补一个 `respond_guided`:指导语非空时追加到唯一的 system 消息末尾,以“当前要点：”起头,一行一条;连续两条 system 消息不是所有兼容端点都接受。训练位策略 HTTP 上限取 120 秒(`POLICY_TIMEOUT_S`,2026-11-02 按真机延迟从 60 秒放宽)[已落地]。`VllmPolicy`(训练位与站内 vLLM 对手)的请求体带 `chat_template_kwargs: {enable_thinking: false}`,关掉基座聊天模板的思考段:开着时推理与回复一起进 `content`、长度不可控,真机单次 6.4 到 208.3 秒并有空回复;关掉 4.6 到 17.3 秒。外部 API 对手不带这个字段,别的兼容端点不一定认 [已落地] | `compute`、`simulator` | [已落地] |

两个入口脚本 `scripts/run_factory.py` 与 `scripts/run_factory_dev.py` 都是 `factory.cli` 的薄壳 [已落地]。前者默认不接受 mock 证据,后者对 `run`、`reconcile` 自动加 `--allow-mock-evidence`。

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
| `factory` | 任何包 | 无 | [已落地] |
| 其余任何包 | 各自的规则 | `factory` | 守卫测试 [已落地] |
| `controlplane` | `simulator.stubs`(骨架桩,唯一回指) | `diagnostic`、`mindtrainer` 与七个新包 | 今天成立;常驻守卫 [已落地] |
| `mindtrainer` | `controlplane`、`simulator` | `diagnostic` | 今天成立;常驻守卫 [已落地] |
| `diagnostic` | `controlplane`、`simulator` | `mindtrainer` | 今天成立;常驻守卫 [已落地] |
| `simulator`、`mindtrainer` | 各自的规则 | `actio`、`starling` | 今天成立;`test_only_actio_memory_starling_imports_starling` 扫全 `src/` 钉住 `starling`,`actio` 一侧没有常驻守卫 [已落地] |
| `actio` | `simulator` | `starling`,只有 `memory_starling` 一个模块除外 | 守卫测试 `test_actio_only_imports_itself_simulator_datamodel_and_the_stdlib` [已落地] |

"今天成立"是在基线提交上用 grep 查的:三个已落地包里没有越界的绝对导入。常驻守卫 `test_layer_rules_hold` 用语法树扫真实源码,只看绝对导入的顶层包名 [已落地]。

## 4. 数据模型与状态机

本子系统不拥有产物状态机。它读写配置信封,并经命令行驱动别人的状态迁移。

| 实体 | 字段与含义 | 约束 |
|---|---|---|
| `FactoryOptions` | `switches`、`allow_mock_evidence`、`confirm_directives`、`planner_backend`、`repo_commit`、`executor`、`endpoint`、`evaluator` | 冻结数据类;`confirm_directives` 三态,`None` 表示用配置 [已落地]。新增布尔字段 `interact`,默认 `False` [已落地] |
| `BuildContext` | 仓库、证据开关、确认覆盖、规划后端、环境变量、日志目录、`repo_commit`、收尾函数列表 | 交给处理器构造器;`bridge(role)` 每次新建一个桥接 [已落地] |
| `Assembly` | 派发器、处理器字典、评估器、证据开关、收尾函数 | 处理器为空即默认路径;`close()` 逆序收尾 [已落地] |
| `ENABLE_MAP` | 开关到处理器键的固定表 | 五个开关、六个键,见下表 [已落地] |

| 开关 | 处理器键 | 处理器来自 |
|---|---|---|
| `plan` | `plan_next_round` | TrainScheduler 的规划处理器 [已落地] |
| `nominate` | `nominate_recipe` | TrainScheduler 的提名处理器 [已落地] |
| `track` | `record_track` | RecipeStore 的战绩处理器 [已落地] |
| `agent` | `start_agent_round` | MindAgent 的轮次处理器 [已落地] |
| `admit` | `admit_recipe`、`admit_proposal` | RecipeStore 与 MindAgent 的收录处理器 [已落地] |

| 产物类型 | 路径(`control-plane/` 下) | 状态机种类 | 写方 | 状态 |
|---|---|---|---|---|
| 评估器配置 | `config/evaluator.yaml` | 配置信封 | `install-defaults`,之后由人改 | [已落地] |
| MindAgent 部署素材 | `config/agent.yaml` | 配置信封 | `install-defaults`,之后由人改 | [已落地] |
| 规划提示模板 | `config/prompts/planner-<版本>.yaml`,默认模板 `PLANNER_V1` | 提示模板信封 | `install-defaults`,之后由人改 | [已落地] |
| 调度配置与训练默认值 | `config/scheduler.yaml`、`config/training_defaults.yaml` | 配置信封 | `install-defaults`,之后由人改 | [已落地] |
| 默认部署情境与人格 | `scenarios/` 下 `factory-deploy-s1` 至 `s3`,`personas/` 下两个人格 | 情境与人格产物 | `install-defaults` | [已落地] |
| 桥接配置 | `config/backends.yaml` | 配置信封 | 人 | [已落地] |
| 默认分类表与题库 | `diagnostics/` 下 `factory-tax-v1`、`factory-pool-v1`;SoMBench 导入的 `sombench-<哈希前缀>` | 登记即 `effective` | `install-defaults`;`import-sombench` | [已落地] |
| 本机评测配置 | 树外,路径由环境变量 `RISE_EVAL_CONFIG` 给出,权限 0600 | 无;不进树 | 人 | [已落地] |
| 本机记忆配置 | 树外,路径由环境变量 `RISE_MEMORY_CONFIG` 给出,权限 0600 | 无;不进树 | 人 | [已落地] |
| 记忆库目录 | 树外,根目录由本机记忆配置的 `root` 给出,布局见下 | 无;不进树 | 部署与训练的 Actio 实例 | [已落地] |
| 候选 checkpoint | `runs/<run>/candidate_checkpoint.yaml` | 需审核 | 命令行改 `approved` 或 `rejected` | [已落地] |
| 配方候选、技能提案 | `recipe_candidates/`、`skill_memory_proposals/` | 需审核 | 命令行改状态 | [已落地] |
| 训练指令 | `runs/<run>/directive.yaml` | 非审核 | `confirm` 改 `pending` | [已落地] |

`evaluator.yaml` 的载荷恰好五个键,`backends` 不可配置:mock 评估器恒为 `mock`,`--evaluator someval` 恒为 `real`:

```yaml
taxonomy_version: factory-tax-v1
item_pool_version: factory-pool-v1
benchmark_version: factory-bench-v1
tier: full          # full 或 quick
sampling_k: 4       # 整数,至少 1
```

`agent.yaml` 的载荷必填三个键:`scenario_ids`、`bindings`(角色绑定字典的列表)、`opponent_lines`(对手台词列表)。可选键是 `AgentConfig` 的其余八个字段,缺省取它的默认值。未知键与类型错误都是装配错误,退出码 2 [已落地]。`install-defaults` 写入的默认值:三个部署情境 `factory-deploy-s1` 至 `s3`;`user` 位绑定 `frozen_reference`(`model_ref` 为 `zengen-v1`),`agent` 位绑定 `self_play`;对手台词三句;`max_turns` 为 4,`min_support` 为 3,`min_ratio` 为 0.5 [已落地]。

`training_defaults.yaml` 的 `interact` 段有必填键 `memory`,形如 `{seed}` [已落地]。`seed` 必填,可为 `null`,表示每个情境从空世界出发。调度器把它写进指令配置时,`digests` 与 `runtime` 留空,派发时才算(称未解析;校验放行未解析的段,只填一半拒绝;解析后再校验一次)。

`digests` 是 `{scenario_id: digest}`,键必须恰好是配置的情境 [已落地]。它进 `config_hash`;按人格记键的 interact run 续训时哈希不同,判 T3(FC-39)。

所有 interact 角色位都是带记忆的 Actio 实例,每轮读写。所以没有"不用记忆"和"不写记忆"的开关(FC-26)。`interact` 段缺 `memory`,装配期由严格解析拒绝,与其他必填键同一口径。指令配置里缺 `memory` 的 interact 指令,在派发器的配置校验处判 T2,原因点名缺 `memory`;batch 配置带 `memory` 同样拒绝 [已落地]。

本机记忆配置是 YAML,文件必须在仓库外、权限 0600,校验照本机评测配置另写一份;报错只写键名与路径,不回显取值 [已落地]:

| 键 | 必填 | 含义 |
|---|---|---|
| `root` | 是 | 记忆库根目录,绝对路径,在仓库外 |
| `extraction_base_url`、`extraction_model` | 是 | 抽取用的 OpenAI 兼容端点与模型 |
| `embedding_base_url`、`embedding_model`、`embedding_dim` | 是 | 嵌入用的端点、模型与维度 |
| `timeout_ms` | 否 | 抽取与嵌入的单次上限,默认 4000 |
| `max_retries` | 否 | 抽取与嵌入的重试次数,默认 0 |

配置里不放密钥:Starling 固定读环境变量 `OPENAI_API_KEY`,抽取与嵌入共用这一把,不能分开配。地址不许带 `user:password@`,读取时拒绝。`timeout_ms` 与 `max_retries` 都进记忆运行时身份。

记忆按世界组织 [已落地]:一个世界是一个库,人格是其中的主体(holder)。端口绑定 `(世界, 主体)`,召回只返回本主体名下的记忆。同一情境的几个人在同一个世界里,"B 在场听到 A 说话"才有地方表达(FC-33)。记忆库目录布局:

| 路径(`root` 下) | 内容 | 生命周期 |
|---|---|---|
| `deploy/<scenario_id>.db` | 部署世界:这个情境绑定的全部人格都是主体,智能体位与对手位都在里面积累 | 持久 |
| `deploy/<scenario_id>.lock` | 世界锁,取代人格锁;部署轮次与门② 按情境取 | 轮次或收录内持有 |
| `seeds/<seed_id>/<scenario_id>.db` | 种子世界,人放进去,本仓库只读 | 只读 |
| `runs/<run_id>/<episode_id>.db` | rollout 世界:训练位与对手位共用,`episode_id` 是逻辑 episode id | 客户端建;服务端端口全关后客户端删 |
| `snapshots/<round_id>/deploy-<episode_id>.db` | 部署 episode 的起点快照,整个世界;旁边同名 `.world` 文件记 `scenario_id`。episode id 自己也以 `deploy-` 开头,双前缀让新旧文件名分得开 | 跨重启保留;episode 落盘后删除,删除就是这条 episode 的提交 |
| `snapshots/<round_id>/verify-<scenario_id>.db` | 验证快照,一个情境世界一份 | 轮次结束即删 |
| `snapshots/<round_id>/verify/` | 验证临时世界 | 轮次结束即删 |

部署 episode id 形如 `deploy-<model>-<scenario>-<k>`,只属于一个情境。回滚读 `.world` 找到世界,不靠解析 episode id。替身部署世界的后缀是 `.json`;替身训练世界是内存里的对象,不落盘,起点是空世界。

一个记忆根目录只属于一个控制面仓库:根目录下的 `.owner` 文件记该仓库的绝对路径,首次使用时先写临时文件、fsync,再用硬链接发布成 `.owner`,`FileExistsError` 时读回再比。装配期读到的路径与当前仓库不同,就退出码 2,信息写两个路径;两个仓库同时首次使用,只有一个能发布成功,发布出来的就是完整内容,另一个读到对方的路径而退出。读到 0 字节的 `.owner`(旧做法被打断留下的)也拒绝,信息写出该删哪个文件。否则两个仓库共用一个根目录时,部署记忆会混写,启动清扫也会删掉对方在途的 rollout 世界 [已落地]。

同一个人格在两个角色位同时出现时拒绝:两个位共用一个主体,就共用记忆与事件 key(FC-36)[已落地]。库必须与 Actio 实例同机,否则每轮 recall 都跨机往返;现状是全部组件一台机器,模拟器移到计算层之后,库跟着实例落在 GPU 主机上 `root` 指的目录。

记忆运行时身份是一个字典,由 factory.memory_runtime 在装配期算出,所有训练记录与哈希都用它 [已落地]:

| 键 | 取值 |
|---|---|
| `backend` | `starling` 或 `stand-in` |
| `starling_version` | `starling.__version__` 加安装来源,见下 |
| `extraction` | 抽取端点的规范化地址、模型、`timeout_ms`、`max_retries`、Starling 抽取提示配置的摘要 |
| `embedding` | 嵌入端点的规范化地址、模型、`embedding_dim`、`timeout_ms`、`max_retries` |
| `trace_schema` | 激活轨迹模式 actio.trace.v2 |

- 安装来源:git 安装取 `direct_url.json` 里 `vcs_info.commit_id`;非 editable 的源码目录安装取已安装包 `RECORD` 里 `starling/` 下带哈希的行,按路径排序后哈希(`INSTALLER`、`direct_url.json` 等随安装器变的文件不进,pip 与 uv 装同一个 wheel 得到同一个身份);`direct_url.json` 有 `dir_info.editable` 的装配期拒绝。
- 规范化地址:小写 scheme 与主机,去掉末尾斜杠,保留路径,不含 userinfo。嵌入适配器用地址加 `/embeddings` 拼请求,路径有意义。
- 替身时 `extraction` 与 `embedding` 为 `null`。

命令行驱动的迁移:

| 命令 | 产物 | 迁移 | 状态 |
|---|---|---|---|
| `approve <id>` | 候选 checkpoint | `pending_review → approved` | [已落地] |
| `reject <id> [原因]` | 候选 checkpoint | `pending_review → rejected`,写 `reject_reason` | [已落地] |
| `approve`、`reject` | 配方候选、技能提案 | 同上 | [已落地] |
| `confirm <directive-id>` | 训练指令 | `awaiting_confirmation → pending` | [已落地] |

不变量:

1. 不传 `--enable`、且执行器是默认 mock 时,factory 不读任何配置文件,不注入派发器以外的任何东西 [已落地]。`--executor roll` 是例外:它读端点与本机主机映射,并注入执行器与转换器(`src/factory/wire.py:141-160`)。传了 `--interact` 就要读端点与本机主机映射、`training_defaults.yaml` 与点名的情境、人格,并给派发器多注入实时客户端与策略工厂;不传 `--interact` 时这条不变量原样成立 [已落地]。`--interact` 还要读本机记忆配置(第 11 条)[已落地]。
2. 开了任一闭环开关,就同时注入数据生产器与 mock 后端的真实评估器 [已落地]。
3. 证据开关只有一个来源:命令行 `--allow-mock-evidence`。同一个值交给调和循环与提名处理器 [已落地]。
4. 确认覆盖不写回 `scheduler.yaml`,只改 loader 的返回值 [已落地]。
5. 门控写命令在迁移不合法或证据不在场时,不写文件、不提交。
6. 已存在的配置文件 `install-defaults` 不覆盖,人改过的配置优先 [已落地]。已存在的分类表、题库、部署情境与人格,内容与默认值相同就不动,不同就拒绝:一行错误、退出码 2,指出哪个文件。同一版本号、同一 id 必须对应同一份内容,题库与分类表的版本进诊断报告,静默还原会让前后两份报告的分数不可比;要换内容就升版本号或换 id。先把这几类资产全部检查完再写任何东西,冲突时树里不留半截安装 [已落地]。
7. `--executor roll` 与任一 `--enable` 开关同时给是用法错误,退出码 2,树里不写东西 [已落地]。第 8 到 10 条是本轮新增的组合规则 [已落地]。真训练会同步占住调和循环数小时(CP-15),闭环处理器的排队语义是按 mock 训练的前提写的;先让一条链路跑通真机,再决定叠加。以后放开比以后收紧容易。
8. `--evaluator someval` 必须同时给 `--executor roll`,且暂不能与 `--enable` 同用;任一不满足是用法错误,退出码 2(FC-16)。
9. `--executor roll` 且不传 `--evaluator someval` 时,仍然要求污染索引在场且数据集哈希对得上,否则拒绝装配。真训练必须过关,不论是否评测(DX-19)。
10. `--evaluator someval` 要求本机评测配置在场、权限 0600、不在仓库目录里,且 SoMEval 检出目录与数据集哈希都对得上;缺一项退出码 2,不回显任何凭证取值(DX-20)。
11. 只在需要记忆的入口读本机记忆配置:`--interact`,或 `--enable` 含 `agent`、`admit`;两者同开只读一次。只开 `plan`、`nominate`、`track` 时不读,坏配置也不影响它们。环境变量 `RISE_MEMORY_CONFIG` 未设置或为空串即配置不在场(第 13 条)。已设置就必须指向仓库外的 0600 普通文件,键恰好是上表那几个;指向不存在的文件是装配错误,不静默退回替身 [已落地]。
12. 装配期还查六件事 [已落地]:
    - 配置在场时,记忆根目录的 `.owner` 记的仓库路径与当前仓库相同;首次使用时先写临时文件再以硬链接发布,`FileExistsError` 时读回再比。不同就退出码 2,信息写两个路径(FC-28)。`runs/` 是符号链接或不是目录,同样拒绝。
    - 产物树里的 `memory_library/` 为空,配置在不在场都查,但只在 `agent`、`admit` 构造器里查(`--interact` 单独用不读它)。非空拒绝启动并要求人工处理:它现在为空(MindAgent 从不生成记忆提案),`load_library` 不再读它,所以不做迁移。
    - 配置在场时,`OPENAI_API_KEY` 非空,报错只写变量名。
    - 配置在场时,Starling 不是 editable 安装:editable 的源码目录随时在变,运行时身份无从钉住。
    - 记忆根目录不是按人格的旧布局:`deploy/` 下没有非已知情境名的库,`snapshots/` 下没有缺 `.world` 旁注的起点快照,有旁注的快照旁注可读(FC-37)。配置在场时在导入 Starling 之前查;替身只在造部署记忆时查。
    - `agent.yaml` 的 `bindings` 里没有重复的 `persona_id`(FC-36)。
13. 记忆配置不在场(变量未设置)时,训练路与部署路都用替身记忆,并在启动日志写 `memory backend: stand-in`(只在 `memory_wiring` 里记一次)。替身路径的摘要是替身摘要、运行时身份的 `backend` 是 `stand-in`,所以之后换成真 Starling 也会改 `config_hash`,不会静默续训。部署路的替身是 FileTupleStore,条目与事件表存成 JSON 文件。它也落盘,根目录是 `<git 目录>/rise-memory`(与 `rise.lock` 同处,由 `controlplane.lock.git_dir` 给出,不入树,跨重启保留)。不用临时目录:每次重启换一个空库,崩溃重入与门② 收录都会失效 [已落地]。
14. 记忆配置要求 Starling 但环境没装它时,装配期退出码 2,信息说明 `pip install '.[memory]'`。装了包但 C++ 扩展载不进来(`ImportError`)同样译成这条提示。不装 extra 的环境只跑替身路径,不会半路缺依赖 [已落地]。

## 5. 接口契约

对外只承诺入口函数与几个数据类,其余模块都是内部实现。

已落地的守护进程入口:

```python
# mindtrainer.wire
def run(repo_root: str, *, interval_s: float = 2.0, max_ticks: int | None = None) -> None
def main(argv: list[str] | None = None) -> int
# 命令行:scripts/run_controlplane.py <repo_root> [--interval-s S] [--max-ticks N]
```

组合根的对外签名,已落地:

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

Python 子命令 [已落地]。每个子命令的第一个位置参数都是控制面仓库根目录:

| 子命令 | 作用 | 退出码 |
|---|---|---|
| `run <repo>` | 一直跑调和循环,带 `--interval-s`、`--max-ticks` | 0;装配错误 2 |
| `reconcile <repo> --once` | 跑一轮,每个动作打印一行 `<kind> <target_id>`,没有动作打印 `no actions` | 0;不带 `--once` 或装配错误 2 |
| `install-defaults <repo>` | 显式写默认资产与配置,打印涉及的路径 | 0;仓库被另一个进程锁住时 2,树里不写任何东西 |
| `import-sombench <repo> --someval-root <路径> --index <树外路径>` | 从 SoMEval 检出目录导入 SoMBench:写分类表与题库(版本带数据集哈希前缀),写污染索引;内容相同不提交 [已落地] | 0;parquet 缺失或 pyarrow 未装,退出码 2 |
| `trace <repo> <run>` | 原样打印观测函数 `trace` 的输出 | 0;run 不存在 1 |
| `cost <repo> <run>` | 原样打印观测函数 `cost` 的输出 | 0,没有 `run_trace` 也是 0 |
| `logs <repo> <run> [--task <id>]` | 按 run 目录产物的 `log_refs` 打印日志 | 0;run 不存在、有引用不可达或哈希不符、有产物读不出或引用畸形 1 |

`run` 与 `reconcile` 共用的开关 [已落地]:

| 开关 | 取值 | 含义 |
|---|---|---|
| `--enable` | `plan,nominate,track,agent,admit` 的子集 | 未知开关与空项是用法错误,退出码 2。`agent` 或 `admit` 开着时,部署路的记忆与 `--interact` 共用同一份本机记忆配置 [已落地] |
| `--allow-mock-evidence` | 布尔 | 接受 mock 证据驱动规划、提名与战绩 |
| `--confirm-directives` / `--no-confirm-directives` | 三态 | 不传就用 `scheduler.yaml` 的 `confirm_directives` |
| `--planner-backend` | `bridge`(默认)、`heuristic` | `heuristic` 是确定性后端,不需要外部工具 |
| `--repo-commit` | 提交号 | 不传取代码仓库根的 `git rev-parse HEAD`,取不到为 `None` |
| `--executor` | `mock`(默认)、`roll` | `roll` 装配真实 ROLL LoRA SFT 执行器与适配器导出转换器 [已落地],装配步骤见下文的 4a,取舍见 FC-15 |
| `--endpoint` | 端点 id | 只在 `--executor roll` 或 `--interact` 时有意义,其余组合装配期拒绝;点名 `compute/endpoints.yaml` 里的哪个端点;只有一个端点时可省 [已落地] |
| `--evaluator` | `mock`(默认)、`someval` | `someval` 装配真实评估器:起 vLLM 服务、跑 SoMEval 子进程、把结果转成诊断报告。必须同时给 `--executor roll`,暂不能与 `--enable` 同用 [已落地] |
| `--interact` | 布尔,默认关 | 装配实时客户端与策略工厂,让 interact 指令在真实入口跑完。与 `--executor roll` 互斥;要求 `training_defaults.yaml` 有 `interact` 段(缺段退出码 2,见拒绝项 2);不要求 `allow_interact`,它只给调度器构造 `new` interact 用 [已落地]。环境变量 `RISE_MEMORY_CONFIG` 未设置用替身记忆;设置了而文件或依赖不合规,退出码 2(第 4 章不变量 11 至 14)[已落地] |

记忆装配新增或改动的跨包签名 [已落地]。组合根造出它们,别的包只认这些形状:

```python
# 策略工厂交给轨迹源:每条 rollout 进出一次,进入时建 rollout 世界、造训练位 Actio 实例,退出时关闭并删世界
policy_for_rollout(episode_id: str, scenario_id: str) -> ContextManager[ModelBackend]
# 服务端后端工厂:SessionInfo 含 scenario、assignment、personas、client_slot、session_id、episode_id
BackendsFactory = Callable[[SessionInfo], SessionResources]
# 落盘钩子按 session_id 查归属
on_session_closed(session_id: str, episode: Episode) -> None
# 对手位与训练位的指导语入口,指导语并入唯一的 system 消息
respond_guided(persona, scenario, history, slot, guidance) -> tuple[str, str | None]
# 组合根注入派发器:种子 id 与情境 id 进,每个情境世界的起点摘要与记忆运行时身份出
memory_resolver: Callable[[str | None, tuple[str, ...]], tuple[dict[str, str], dict]]
# 部署驱动器每次尝试经它新造实例;memory 是已打开的 (情境世界, 智能体位人格) 端口,holder 是该人格
make_instance(episode_id: str, memory: MemoryBackend, holder: str) -> ActioInstance
```

记忆世界的签名 [已落地]。`MemoryBackend` 的五个方法不变,端口打开时绑定世界与主体:

```python
# factory.memory_runtime;进程内一张 episode_id → 世界的表,一把锁
class TrainingWorlds(Protocol):
    def bind(self, run_id: str, seed: str | None) -> None: ...
    def unbind(self, run_id: str) -> None: ...
    def seed_of(self, run_id: str) -> str | None: ...
    def open_rollout(self, run_id: str, scenario_id: str, episode_id: str, holder: str) -> MemoryBackend: ...
    def join_rollout(self, episode_id: str, holder: str) -> MemoryBackend: ...
    def close_rollout(self, episode_id: str, ports: Sequence[object], *,
                      wait_s: float = SESSION_CLOSE_WAIT_S) -> bool: ...
    def release_session(self, episode_id: str, ports: Sequence[object]) -> None: ...
    def sweep(self) -> int: ...
SESSION_CLOSE_WAIT_S = 70.0   # mindagent.deployment;服务端 join 上限 60 秒加 10 秒余量
```

| 名字 | 谁调 | 语义 |
|---|---|---|
| `open_rollout` | 客户端 `policy_for_rollout` 进入时 | 先删后建:从 `seeds/<seed>/<scenario>.db` 复制出世界(没有就空世界),登记进表,开训练位端口;同名世界在保留表里还被端口占着时先有界等待(默认 70 秒)端口关上,仍没关上才抛瞬态 `MemoryFailureError` |
| `join_rollout` | 服务端后端工厂 | 按 `info.episode_id` 查表,开对手位端口;查不到抛非瞬态 `MemoryFailureError`,造后端失败,会话被拒 |
| `close_rollout` | 客户端退出 `policy_for_rollout` 时 | 关训练位端口,等服务端端口关完(上限 `SESSION_CLOSE_WAIT_S`),摘表、删世界,返回真;等不到时世界进保留表、记 warning、返回假 |
| `release_session` | 服务端会话资源的 closer | 会话终态关本会话开的对手位端口,每关一个计数减一 |
| `wait_closed(world_id, timeout_s)` | 部署驱动器、轮次回滚 | 部署管理面的方法:等这个世界打开的端口数降到 0,超时返回假 |

TypeScript 命令行:`rise <review|approve|reject|confirm|status> [id] [reason]` [已落地],五个命令都在 `cli/src`。

| 命令 | 泛化前的行为 [已落地] | 泛化后即今天的行为 [已落地] |
|---|---|---|
| `review <id>` | 打印候选头行与诊断摘要;缺报告标 `MISSING`,桩报告标 `[STUB]` | 三类产物按类型打印证据;mock 报告标 `[MOCK BACKENDS]` |
| `approve <id>` | 先判迁移,再要求同 run 目录有诊断报告 | 配方候选要有 `repro`,提案要有 `verification` |
| `reject <id> [原因]` | 写 `reject_reason`,原因缺省为 `no reason` | 三类产物通用 |
| `status` | 列出 `pending_review` 的候选 | 同时列出三类待审产物与待确认指令 |
| `confirm <id>` | 不存在 | 只认 `awaiting_confirmation`;多处命中报歧义 |

对邻居的假设:

| 邻居 | 本文依赖它什么 | 它依赖本文什么 |
|---|---|---|
| ControlPlane | `run_daemon`、`reconcile_once` 接受 `dispatcher`;闭环模式还接受 `handlers`、`evaluator`、`accept_mock_evidence`;非闭环的 `--evaluator someval` 也传 `evaluator` | 有人注入处理器,闭环动作才会产生(R2) |
| MindTrainer | `make_mindtrainer_dispatcher()`;闭环模式传 `data_producer` [已落地];`--interact` 时另传 `client_factory` 与 `policy_factory` [已落地]。记忆装配后,策略工厂交出 `policy_for_rollout`:每条 rollout 进出一次的上下文,进入时造训练位 Actio 实例,退出时关闭。轨迹源在 `create_session` 之前进入它;策略返回三元组时,第三项作为激活轨迹随 `submit_turn` 上行。派发器另收组合根注入的 `memory_resolver`(签名见上),interact 指令没注入它就判 T2 [已落地] | 无 |
| 诊断评测 | `make_diagnostic_evaluator` 与资产读写;`ScoredSoMEvalRunner` 协议、`write_failure_report` | 无 |
| TrainScheduler | `make_plan_handler`、`make_nominate_handler`、`install_default_configs` [已落地] | 传入 `accept_mock_evidence`、`repo_commit` 与带覆盖的 loader |
| TrainPlanner | `install_default_template`、`BridgeDecisionBackend`、`HeuristicBackend` [已落地] | 选后端、安装默认模板由本文负责 |
| SocialSimulator | `create_server`、`EnvironmentServiceClient`、`read_persona`、`read_scenario`、`write_persona`、`write_scenario`;`--interact` 另用 `on_session_closed` 落盘 episode [已落地]。会话身份进入服务端接口:`BackendsFactory` 改为 `(SessionInfo) -> SessionResources`,会话信息含情境、角色分配、人格、客户端位、`session_id`、逻辑 episode id;`on_session_closed` 改收 `session_id` 与 episode;`CreateSession` 有可选 `episode_id` [已落地] | 无;后端工厂返回 SessionResources,落盘按 `session_id` 查归属 [已落地] |
| RecipeStore | `handle_admit_recipe`、`handle_record_track` [已落地] | 无 |
| MindAgent | `make_start_round_handler`、`make_admit_proposal_handler` [已落地] | 实时客户端工厂、罐头后端、人格 loader;另要部署记忆管理面 DeploymentMemoryStore 与记忆运行时身份。配置在场用 StarlingDeploymentStore,不在场用 FileTupleStore(FC-29)。轮次处理器造出 make_instance,交给部署驱动器。组合根只造一个管理面,交给轮次处理器、门② 与服务端三方;部署驱动器每次尝试前向 `AgentRuntime` 登记 episode id 到 round_id,尝试结束注销,登记与注销的回调由组合根注入轮次处理器(`mindagent` 不导入 `factory`);验证实例工厂收 `(extra_skills, scenario_id)`;门② 处理器判 `(scenario_id, persona_id)` 是否是部署配置里绑定的一对,绑定对由组合根从 `agent.yaml` 读出后经 `bound_pairs` 注入 [已落地] |
| Actio | `instantiate(memory=..., replay_read_only=..., episode_id=..., holder=...)`:构造带记忆端口的实例,`episode_id` 是逻辑 episode id、事件 key 的前缀,`holder` 是事件 key 的 `@` 后缀,可写端口不给就抛 `ValueError`;`memories=` 保留为替身记忆的简写;部署记忆管理面 DeploymentMemoryStore,放在 `actio`,所以 MindAgent 不导入 `starling`;`memory_starling`:唯一导入 `starling` 的模块;激活轨迹模式 actio.trace.v2;部署管理面按世界取键,`open(world_id, holder)`,外加 `wait_closed`;事件 key 是 `<前缀>#<turn_index>@<主体>` [已落地] | 本机记忆配置、运行时身份、库目录与每条 rollout 一个世界,由组合根装配 [已落地] |
| Starling | 可选 extra `memory`(`starling-memory`,钉 git 提交)[已落地]。缺的公开接口经 PR 补进 Starling:`Memory.recall` 的 `holder`、`strict`(查询词嵌入失败抛 RecallDegradedError)、`include_volatile`(召回放宽到未巩固的 `volatile` 行);`Memory.open(embedder=)`、`make_openai_embedder`(带 `max_retries`);`embed_pending` 返回 EmbedStats;`digest`、`backup_to`、`close(timeout=)`;`remember_event` 带 `interlocutor` 与 `epoch`;事件表 `events` 加 `tenant_id`、`failure_category`,主键 `(tenant_id, key)`,配套 `next_seq`、`pending_events`、`abandon_event`(未知 key 抛 `KeyError`)。三段写入、摘要、备份、嵌入循环进 C++ 内核,Python 门面只转发;`[key] ` 文本前缀在 Starling 一侧加。训练副本不经 `backup_to`,由 `copy_library` 复制种子的 `.db` 连同 `-wal` [已落地] | 无 |
| 外部推理桥接 | `CliBridge` [已落地] | `backends.yaml` 的 `planner`、`agent` 两个角色 |
| 远端计算层 | `ComputeClient`;`--executor roll` 与 `--evaluator someval` 已在用 [已落地];`--interact` 的策略工厂经 `serve` 按指令起停 vLLM [已落地] | 无 |

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
| 0 | 取仓库排他文件锁(`run`、`reconcile`、`install-defaults`、`import-sombench` 取,只读子命令不取;`reconcile` 在装配前取,`run` 在装配后、第一个 tick 前取) | 锁已被占用,退出码 2 [已落地] |
| 1 | 解析 `--enable`,经 `ENABLE_MAP` 得到处理器键 | 未知开关或空项,退出码 2 |
| 2 | 处理器键为空:只构造 `make_mindtrainer_dispatcher()`,调 `run_daemon` 时只传派发器 | 无 |
| 3 | 读 `evaluator.yaml`,要求恰好五个键 | `FactoryConfigError`,退出码 2 |
| 4 | 校验分类表与题库在树里,构造 mock 评估器;`--evaluator someval` 时改为校验本机评测配置与 SoMEval 检出目录,构造真实运行器与真实评估器 | 同上;凭证只写键名,不回显取值 |
| 4a | `--executor roll` 时:读 `compute/endpoints.yaml` 与本机主机映射,选定端点,造 `ComputeClient`,再造执行器工厂与转换器 [已落地]。ROLL 的部署与训练容器不在装配期做(要碰远端、耗时),在执行器第一次 `preflight` 时做,执行器与转换器共用同一份(见 30 号文档"ROLL 的来源与运行环境") | `FactoryConfigError`,退出码 2;缺文件、键缺失或多余、映射里查不到端点、别名形态不合、`--endpoint` 点名了不存在的端点、有多个端点却没点名、端点没有 `host_deps_root`,全在第一个 tick 之前报。部署期的确定性失败判指令 `failed`(T2) |
| 4b | `--executor roll` 时:读污染索引,校验数据集哈希,把准入检查注入执行器;`--evaluator someval` 时另把真实运行器注入评估器 | `FactoryConfigError`,退出码 2;索引缺失、哈希对不上、stub 前端断言失败 |
| 4c 前 | 记忆装配 [已落地],`--interact`,或 `--enable` 含 `agent`、`admit` 时先于 4c 做,只在这些入口读变量。按序检查:读配置(第 12 条)、安装来源(第 15、16 条,先于任何导入与对记忆根目录的写)、`OPENAI_API_KEY`(第 13 条)、`runs/`、认领根目录(第 17 条),再导入 `actio.memory_starling`,算记忆运行时身份。配置不在场用替身记忆;启动日志只在 `memory_wiring` 里记一次,写 `memory backend: stand-in` 或 `memory backend: starling`。第 14 条不在这里,在 `agent`、`admit` 处理器构造器里查。配置与身份交给运行时的后端工厂和策略工厂,`memory_resolver` 交给派发器。部署记忆库放 `deploy/`,组合根选出部署记忆管理面,在第 5 步交给 `agent` 与 `admit_proposal` 处理器构造器(FC-29)。两个开关同开只装配一次。装配造出 `runs/` 的清扫函数但不调用:装配之后由持锁的一侧调,`reconcile` 在锁内第一个动作之前,`run` 经启动钩子(FC-27)。认领根目录之后、导入 Starling 之前查旧布局(第 18 条);启用部署轮次时,把同一个部署管理面经 `deployment_store` 交给 `AgentRuntime`(FC-34)。 | `FactoryConfigError`,退出码 2,指出缺哪一项;记忆项只写键名、变量名与路径 |
| 4c | `--interact` 时 [已落地]:先查与 `--executor roll` 互斥。再读 `training_defaults.yaml` 的 `interact` 段,校验点名的情境与人格。最后构造带落盘钩子的 `AgentRuntime` 与策略工厂。已因 `--enable agent` 构造了运行时就复用同一个。 | `FactoryConfigError`,退出码 2,指出缺哪一项 [已落地] |
| 4d | 没传 `--interact` 且 `--enable` 含 `plan` 时 [已落地]:读 `scheduler.yaml` 的 `allow_interact`,为真就拒绝装配。此时规划器可能写出 interact 指令,而没有 `--interact` 派发必然判 `failed` | `FactoryConfigError`,退出码 2,点名缺 `--interact` |
| 5 | 按 `DEFAULT_BUILDERS` 逐个构造处理器,需要时经 `build_bridge(store, role, *, env=None, log_dir=None)` 建桥接、读凭证、读 `agent.yaml`。`planner` 角色只在 `--planner-backend bridge` 时必需,`agent` 角色可选,缺了就不建桥接,提案生成回到规则模板 [已落地] | 同上;已登记的收尾函数逆序调用 |
| 6 | 注入派发器(带数据生产器)、处理器、评估器、证据开关,启动调和循环 | 运行中的异常原样上抛,进程退出 |

以上步骤全部 [已落地]。`--executor roll` 带任何 `--enable` 都是配置错误,装配走完 4a、4b 就返回,没有第 5 步;`--evaluator someval` 同样不能与 `--enable` 同用。第 2 步与守护进程入口走同一条代码路径,所以两者逐字节相同。任一步出错,标准错误打印一行 `factory: error: ...`,树里不写任何东西。

**`--interact` 装配 [已落地]。** `--interact` 让 interact 指令在真实入口跑完:组合根给派发器注入实时客户端与策略工厂,派发器据此走实时会话,训练位经 vLLM 由真模型说话。它是 `run` 与 `reconcile` 共用的布尔开关,默认关,不属于 `--enable` 的五个短名:`--enable` 管调和循环的闭环动作,`--interact` 管派发器的装配。

| 项 | 语义 |
|---|---|
| 注入什么 | `client_factory`:进程内实时服务端的客户端工厂,服务端带落盘钩子;`policy_factory`:按 run id 与基座引用构造训练位策略。两者交给 `make_mindtrainer_dispatcher` |
| 落盘钩子 | 会话结束时由服务端的 `on_session_closed` 把 episode 写进产物树。轨迹源不写 episode,只在 metadata 里记 id |
| 对手位 | 组合根按角色位选驱动:固定对话、站内 vLLM 上的对手模型、外部 OpenAI 兼容 API。模拟器本身只认后端协议 |
| 运行时复用 | 已因 `--enable agent` 构造了 `AgentRuntime` 就复用同一个,不起第二个服务端;运行时登记收尾函数,与处理器同一机制逆序收尾。共用的影响面不止落盘钩子:训练登记表按会话记录该会话属于哪个 run,落盘钩子只处理训练登记过的会话,把 episode 写进对应 run;部署会话没有训练登记,仍由 MindAgent 驱动器自己落盘,两边的 episode 不混写。判错的代价是部署 episode 被误写进 interact 的数据,或同一条 episode 被两边各写一次 [已落地] |
| 不打开时 [已落地] | 不注入这两样,interact 指令仍判 `failed`(T2),原因点名 `--interact`:`interact mode needs a client_factory and a policy or policy_factory; start factory run or factory reconcile with --interact to run interact directives`。这个无参派发器与冻结入口用的是同一个,两边写出的文字相同。`--enable agent` 构造过运行时也不改变这一点 |
| 与 `--evaluator someval` | `someval` 要求 `--executor roll`,而 `--interact` 与 `--executor roll` 互斥,所以两者无法同用 |
| 策略工厂的形态 [已落地] | 交给派发器的策略工厂改为 `policy_for_rollout`:每条 rollout 进出一次,进入时建 rollout 世界、造训练位 Actio 实例,退出时关闭并删世界。轨迹源在创建会话之前进入它,世界按逻辑 episode id 命名;策略返回三元组时,第三项作为激活轨迹随提交上行 |
| 会话登记 [已落地] | 策略上下文登记的“情境 id 到 run id”只用于判定会话归属。后端工厂造资源时另把 `session_id` 到 run id 记进第二张表,终态时删除,落盘只查第二张表。所以策略上下文注销情境时,仍在跑的会话照样落到自己的 run。“情境 id 不得同时属于训练与部署”的检查保留 |
| 记忆运行时身份 [已落地] | 装配期读本机记忆配置,算出记忆运行时身份(第 4 章)。派发时,策略上下文之前,按 `memory.seed` 为每个配置的情境打开种子世界、算起点摘要;合成情境时解析在合成之后。摘要与身份一起写进本次派发的配置(不回写 `directive.yaml`),之后才算 `config_hash`。训练记录的 `policy_backend.memory` 带同一个块,读者由此看到记忆从哪来 |
| 续训与替身 [已落地] | 续训时重算摘要与身份;种子世界、Starling 版本、抽取或嵌入模型任一变了,哈希不同,升 T3。替身路径的 `backend` 是 `stand-in`,换成真 Starling 同样改哈希。判错的代价:哈希不变,续训的后半程与前半程就不是同一个实验 |
| 每条 rollout 一个世界 [已落地] | 整条 rollout 从情境的种子世界复制出一个世界(文件复制,连同 `-wal`),训练位与对手位都是其中的主体,互不召回,用完即删,见下表。同组 `group_size` 条 rollout 从同一个种子世界出发、互相看不见,组内优势只反映策略;代价是记忆不在一条 run 内累积(FC-35) |
| 时间预算:等待上限 [已落地] | `--interact` 下客户端等相邻两条服务端消息 120 秒(`_next_msg`,`INTERACT_MESSAGE_TIMEOUT_S`),超时视作 `timeout`(T1,按条重跑)并 abort 会话。整条流按 `max_turns × 150` 兜底,由轨迹源经 `client_factory(deadline_s=)` 传(`PER_TURN_DEADLINE_S` 取 150 秒)。服务端等一次提交的 `turn_timeout_s` 取 150 秒(`INTERACT_TURN_TIMEOUT_S`)。这两个方向相反、互不嵌套(FC-32):服务端等一次提交,客户端等相邻两条消息的间隔,一轮里有几个对手都不叠加。两个开关同开时共用一个运行时,等提交取较大的 150 秒。2026-11-02 之前是 90 秒与 90 秒,按真机延迟放宽(见 FC-32)。只有 `--enable agent` 且是 Starling 时,服务端等提交取 40 秒,整条流按 `max_turns × 40` 秒折算(FC-31);替身时保持现值 5 秒与 15 秒 |
| 时间预算:内层与记忆 [已落地] | `--interact` 下训练位策略 HTTP 上限 120 秒(`POLICY_TIMEOUT_S`),服务端后端调用 90 秒(`INTERACT_BACKEND_TIMEOUT_S`,经 `AgentRuntime(backend_timeout_s=)` 传;部署路不传,仍是 60 秒),对手 HTTP 60 秒(`OPPONENT_TIMEOUT_S`)。服务端收尾的 join 仍是 60 秒:准入退避预算 `ADMISSION_WAIT_S` 65 秒以它为前提。记忆配置的 `timeout_ms` 默认 4000,`max_retries` 默认 0,两者都进运行时身份。一轮五次记忆网络调用,最坏合计 20 秒:召回嵌入一次,写入抽取三次,嵌入一批。对手一轮约 80 秒(HTTP 60 秒加 20 秒),低于服务端 90 秒;训练位一轮约 140 秒(HTTP 120 秒加 20 秒),低于 150 秒。外层上限高于内层最坏耗时,内层先超时,外层才看得到分类过的失败 |
| 对手位包 Actio [已落地] | 三种驱动的对手后端都由对手工厂包进 Actio 实例;部署会话的对手位同样包(FC-25)。部署对手位的记忆是部署世界的端口,跨会话积累(FC-34)[已落地]。对手位的技能与经验为空:现阶段只有 MindAgent 产出技能,训练路不读技能库。对手的激活轨迹随落盘 episode 保存,不下发给训练位 |
| 启动清扫 [已落地] | 清扫删记忆根目录下 `runs/` 的每个子项,普通文件与符号链接只删本身、目录整个删,不跟进符号链接,不碰 `deploy/`、`seeds/`、`snapshots/`。`assemble` 只交出清扫函数、不调它。`reconcile` 在仓库排他锁内、装配之后、第一个动作之前调。`run` 的装配在锁外,取锁发生在 `run_daemon` 里,所以清扫经它的参数 `on_locked` 传入,在取锁之后、第一个 tick 之前调用;`on_locked` 抛异常不接,守护进程不开跑。此刻本进程还没有会话,实时服务端在第一次需要会话时才起;别的进程被仓库锁挡在外面,根目录又只属于本仓库(FC-28),所以没有在途世界会被误删。运行中不扫目录:rollout 世界由客户端在服务端端口关完后删(FC-27、FC-35) |

每条 rollout 一个世界,按时刻 [已落地]:

| 时刻 | 动作 |
|---|---|
| 客户端进入 `policy_for_rollout` | `open_rollout` 从种子世界复制出 `runs/<run>/<episode>.db`(`copy_library`,连同 `-wal`;没有种子文件就新建空库),登记进表,开训练位端口,构造训练位 Actio 实例 |
| 服务端造会话资源 | `join_rollout` 按 `info.episode_id` 查表,为每个对手位开端口;查不到则会话被拒 |
| 每轮 | 各位实例 recall、回复、remember、`embed_pending`,不做后台维护 |
| 会话终态 | 服务端先停新调用,再等在途线程,再经 `release_session` 关对手位端口 |
| 客户端退出 | 关训练位端口,等服务端端口关完,上限 `SESSION_CLOSE_WAIT_S`;再摘表、删世界 |

- 训练位的实例在客户端(轨迹源一侧),对手位的实例在服务端,由后端工厂每个会话新造一个。替身的 rollout 世界在内存里,从空世界起。
- 训练位端口 `close` 超时则这条 rollout 判确定性记忆失败,不重试。
- 等不到服务端端口关完时,世界进保留表:端口还开着时同名重试先有界等待(默认 70 秒)端口关上,仍没关上才判瞬态,端口全关后同名重试先删后建;没有同名重试的,文件留给启动清扫;这条 rollout 照常结束,轨迹已经拿到。
- 重试同一个 episode id 时先删后建。表里还登记着同名世界,说明服务端端口没关上。
- 这时抛瞬态的 `FactoryMemoryUnavailableError`,记为 `role_memory_unavailable` 重跑(FC-38)。
- 重试用尽抛 `RolloutFailedError`,由 `factory.interact_policy` 译成 `RolloutDeferredError`,指令留 `pending`。
- 同一个人格占两个角色位时拒绝,见第 19 条拒绝项。
- 两端在同一个进程里,一张进程内的表够用;模拟器搬到 GPU 主机后,改由 Starling 服务按世界名找世界 [推迟]。

装配期的拒绝项共十九条 [已落地]。第 1 至 11 条:前十条在 `--interact` 打开时检查(第 10 条除外,见表),第 11 条在没传 `--interact` 时检查。第 12 至 18 条是记忆装配的检查,在 `--interact`,或 `--enable` 含 `agent`、`admit` 时检查;第 19 条由 `read_agent_settings` 查。检查全部在第一个 tick 之前,出错都是退出码 2、一行错误、树里不写东西:

| 序 | 检查 | 失败原因 |
|---|---|---|
| 1 | 不与 `--executor roll` 同用 | 真实执行器只支持 SFT(`supported_phases` 是 `{"sft"}`,`src/mindtrainer/roll_executor.py:87`),开不了 interact 的实时会话。选项层面的检查,与 `--evaluator someval` 的互斥检查同处(`src/factory/wire.py:227` 起) |
| 2 | `training_defaults.yaml` 有 `interact` 段 | 缺段。错误信息点名缺的是 `interact` 段 |
| 3 | 有角色位用 `external_api` 时,其凭证环境变量在场 | 与 `factory.bridges` 同一口径:凭证在装配期解析,缺就起不来;异常只报变量名。`allow_interact` 不在装配期检查,它只管调度器构造 `new` interact |
| 4 | 点名了 `scenario_ids` 就必须点 `persona_ids`,反之亦然 | 只写了一半 |
| 5 | 点名的情境与人格都在产物树里 | 不在树里 |
| 6 | 点名的情境 `purpose` 是 `training` | `install-defaults` 写入的三个部署情境 `purpose` 是 `deployment_simulation`,拿它们训练等于污染;人格不受此限 |
| 7 | 点名路不给 `sft_steps > 0` | 点名路不带 `data_spec`,没有合成,`sft` 阶段会因缺清单判 `failed`;点名路不做冷启动 |
| 8 | 情境数不少于 `groups_per_step`(默认 2) | 一个 GRPO step 要取 `groups_per_step` 个情境,不够时同一情境会在一步里出现两次、两组并成一组 |
| 9 | 对手用 `external_api` 时凭证环境变量在场 | 与 `factory.bridges` 同一口径:凭证在装配期解析,缺就起不来;异常只报变量名 |
| 10 | `policy` 与 `policy_factory` 不同时给 | 两个来源说不清谁生效,宁可报错。组合根从不给具体的 `policy`,所以这一条从命令行够不到,装配期没有可检查的:把关的是派发器自己抛的 `ValueError`,测试钉住的也是它 |
| 11 | `--enable` 含 `plan`、`scheduler.yaml` 的 `allow_interact` 为真时,必须同时传 `--interact` [已落地] | 规划器可能写出 interact 指令,而没有 `--interact` 时派发必然判 `failed`,闭环停住。这是启动时就看得出的配置矛盾,不留到第一条指令。错误信息点名缺 `--interact`,并说明二选一:加 `--interact`,或把 `allow_interact` 关掉 |
| 12 | 环境变量 `RISE_MEMORY_CONFIG` 一旦设置,就指向仓库外的 0600 普通文件,键恰好合规,地址不带 `user:password@` | 文件不存在、缺键、多余键、权限过宽、路径在仓库里、地址带 userinfo、`timeout_ms × (max_retries + 1)` 超过 4000 毫秒(外层时限按每次记忆调用至多 4 秒折算)。报错只写键名与路径,不回显取值 |
| 13 | 记忆配置设置后,`OPENAI_API_KEY` 非空 | Starling 固定读这个变量,空则抽取与嵌入必败。报错只写变量名 |
| 14 | 产物树里的 `memory_library/` 为空,配置在不在场都查(只在 `agent`、`admit` 构造器里) | 旧目录不迁移,`load_library` 不再读它;非空就有记忆条目无人认领,拒绝启动并要求人工处理 |
| 15 | 记忆配置设置后,Starling 不是 editable 安装 | editable 安装的源码目录随时在变,运行时身份无从钉住 |
| 16 | 记忆配置设置后,Starling 已安装且载得进来 | 配置要求 Starling 却没装它,或装了而 C++ 扩展载不进来,信息都说明 `pip install '.[memory]'`。不装 extra 的环境不设 `RISE_MEMORY_CONFIG` 就走替身路径 |
| 17 | 记忆配置设置后,记忆根目录的 `.owner` 记的是当前仓库,且 `runs/` 不是符号链接或文件;首次使用时先写临时文件再以硬链接发布,两个仓库同时首次使用只有一个发布得成 | 根目录属于别的仓库:两个仓库共用时部署记忆会混写,启动清扫会删掉对方在途的 rollout 世界。信息写两个路径。`runs/` 是符号链接时清扫会删到记忆根之外,文字只写相对名 |
| 18 | 记忆根目录不是按人格的旧布局,且起点快照的 `.world` 旁注都可读 | `deploy/` 下有非已知情境名的库文件,或 `snapshots/` 下有缺旁注的起点快照。配置在场时在导入 Starling 之前查;替身只在造部署记忆时查。旧库不会被任何世界读到,悄悄留着会让人以为它还在用。信息列出可疑条目(至多 5 条),提示移回配置或移出记忆根(FC-37) |
| 19 | `agent.yaml` 的 `bindings` 里 `persona_id` 不重复 | 两个位共用一个主体就共用记忆与事件 key。由 `read_agent_settings` 查,它有三个调用方:轮次处理器与运行时装配在装配期调,出错退出码 2;`interact_policy` 每次派发时调,转成 `DirectiveFailedError` 判 T2。训练配置与合成绑定的同类重复也由派发器判 T2(FC-36) |

没点名时按 `data_spec` 的 `seed` 生成情境与人格,生成发生在派发时,装配期不检查。开关名、检查顺序与错误文字以 `src/factory/interact_checks.py` 为准;第 4、8 条由 `training_defaults.yaml` 的严格解析一并把关,先于第 2 条。

### 6.2 失败与恢复

| 场景 | 发生什么 | 恢复 |
|---|---|---|
| 闭环模式缺评估器配置、诊断资产、桥接角色、凭证或 `agent.yaml` | 启动即退出码 2,指出缺哪份文件 | 跑一次 `install-defaults` 或补文件,再启动 [已落地] |
| 缺规划提示模板或调度配置 | 装配期就拦下:`--enable plan` 预检 `scheduler.yaml`、`training_defaults.yaml` 与提示模板,只开 `nominate` 时预检 `scheduler.yaml`;缺任一项即以退出码 2 结束,不会起守护进程 [已落地] | 先跑 `install-defaults`,再启动 |
| `--interact` 缺 `interact` 段、点名的情境与人格对不上,或与 `--executor roll` 同用 [已落地] | 启动即退出码 2,指出缺哪一项 | 补 `training_defaults.yaml`,或去掉 `--executor roll`,再启动 |
| `--enable plan` 开着、`allow_interact` 为真,但没传 `--interact` [已落地] | 启动即退出码 2,错误信息点名缺 `--interact`。不拦的话规划器可能写出 interact 指令,派发时必然判 `failed`,闭环停住 | 加 `--interact`,或把 `scheduler.yaml` 的 `allow_interact` 关掉,再启动 |
| 记忆配置缺键或多余键、权限不对、路径在仓库里,`OPENAI_API_KEY` 为空,`memory_library/` 非空,Starling 是 editable 安装,配置要求 Starling 却未安装,`runs/` 是符号链接或文件,或记忆根目录属于别的仓库 [已落地] | 启动即退出码 2,信息只含键名、变量名与路径 | 修好配置、设好变量、清理目录、改成非 editable 安装、执行 `pip install '.[memory]'`、把 `runs/` 换成普通目录,或给本仓库换一个记忆根目录,再启动 |
| 记忆根目录是按人格的旧布局,或 `agent.yaml` 里同一人格占两个位 [已落地] | 启动即退出码 2,信息列出可疑条目或重复的人格 | 已从 `agent.yaml` 移除的情境的世界移回配置或移出记忆根,确认是旧的按人格库才删;或改绑定,再启动 |
| 部署轮次要恢复世界,但服务端端口 `SESSION_CLOSE_WAIT_S` 内没关完 [已落地] | 按 `RestoreFailedError` 处理,库与快照都保留 | 人查明滞留的会话后恢复快照 |
| 部署世界里有别的轮次留下的未提交起点快照 [已落地] | 轮次抛 `DeferredError`,原因写出那个轮次 id;连续 20 次后守护进程带原因退出 | 人工恢复或删除那份快照,再启动 |
| 缺凭证或凭证写进了树 | 退出码 2,信息只含变量名或键路径 | 设好环境变量,从树里删掉凭证 [已落地] |
| 规划桥接失败 | 决策 `failed`,写 T3 失败记录,闭环停住(R18) | 人写新指令或手工重置 [已落地] |
| 处理器或派发器抛 `DeferredError` | 不写树,同一进程按退避重试;连续延后超过上限才退出 | 无需人工 [已落地] |
| 处理器或派发器抛其他瞬态异常 | 进程退出 | 外部监督者重启,调和循环从树续接 |
| 运行中删了诊断资产 | 评估器在调用处抛 `DiagnosticAssetError` | 补齐后重启 [已落地] |
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

主链对应 `test_the_whole_loop_turns_twice` [已落地]。每一步都经 `factory reconcile --once`,人工动作用写状态模拟,与已落地的 `tests/test_mt_wire.py` 同一手法。从步骤 2 起每个 tick 都带 `--allow-mock-evidence --planner-backend heuristic`:mock 证据默认不可接受,不带它时晋级之后没有提名也没有规划(`test_without_mock_evidence_the_loop_does_not_turn`)。动作行与状态取自该测试的断言;分数与画像由 `factory/evaluation.py` 的 mock 答法决定,测试只断言增益大于 `0.01`。

| 步 | 图中节点 | 动作或结果 |
|---|---|---|
| 1 | 登记基线 | 人工登记 `run-000` 的全错 mock 报告与 `zengen-v1`,`links = [cand-run-000, run-000]` |
| 2 | run-001 训练 | 开关 `nominate,track,admit`;数据集清单由夹具预先写好,不走合成;依次打印 `dispatch_directive run-001`、`request_diagnostic cand-run-001`、`await_review cand-run-001` |
| 3 | 门①批准 | 写状态模拟 `approve`,候选置 `approved` |
| 4 | 晋级 zengen-v2 | `promote_candidate cand-run-001`;报告证据等级 `mock` |
| 5 | 提名配方 | 增益大于 `0.01`;`nominate_recipe run-001`,配方候选 `pending_review` |
| 6 | 门③批准并收录 | 写状态模拟批准;`admit_recipe recipe-run-001`,配方 `effective` |
| 7 | 规划 run-002 | 开关加上 `plan`;`plan_next_round run-001`,决策 `clone recipe-run-001`;新指令停在 `awaiting_confirmation`,下一 tick 没有动作 |
| 8 | 确认指令 | 写状态模拟 `confirm run-002`,指令置 `pending` |
| 9 | 第二轮训练 | 训练、诊断、批准,晋级为 `zengen-v3`;复用 `run-001` 的清单,不为 `run-002` 合成 |
| 10 | 战绩 · 再规划 | 先一个 tick 打印 `record_track run-002`、`nominate_recipe run-002`(增益 0,跳过,写 `skipped` 标记);提名标记在场后,下一个 tick 才打印 `plan_next_round run-002`;`run-003` 的指令停在 `awaiting_confirmation` |

默认 mock 评估器对变体族的基题答参考答案、对变体题恒答错,各 `sampling_k` 次。所以每个候选的一级分数严格在 0 与 1 之间,错误画像是 `{"surface_match": 1.0}`。基线全错,第一轮才有正增益;第二轮与第一轮同分,增益为 0。

MindAgent 链对应 `test_the_mindagent_line_runs_inside_the_reconcile_loop` [已落地],是图中第 11、12 步,上表只列主链的 1 到 10 步。这个测试里 `zengen-v1` 是人工登记的基线,所以空库轮绑定 `zengen-v2`、出提案的轮次绑定 `zengen-v3`:`zengen-v2` 的轮次空库零提案,放入基线技能后 `zengen-v3` 的轮次产出 `pending_review` 提案,批准收录后库版本是 `{"skill": "2", "memory": "0"}`,两个值都是字符串。

### 6.4 九个工作包的顺序

factory 是第九个工作包,前八个全部落地后才施工。前置关系:1 先于 2、3、4、7、8;5 在 2、3、4 之后;6 先于 7;9 在 1 至 8 之后。各包的任务范围与它们在本篇用到的接缝见总览的路线图。

### 6.5 首次部署

一个新的控制面仓库从零到闭环转起来,按下表的顺序做 [已落地]。

| 步 | 动作 | 谁做 | 说明 |
|---|---|---|---|
| 1 | 建仓库:`GitStore.init()` 建 git 仓库、写本地提交身份、建全部产物目录 | 人 | 之后每次状态变更都是一次提交 |
| 2 | `install-defaults <repo>` | 人 | 写默认分类表与题库、`evaluator.yaml`、规划提示模板、两份调度配置、默认部署情境与人格、`agent.yaml`;已存在的配置不覆盖,已存在而内容不同的资产拒绝并以退出码 2 退出。不写 `compute/endpoints.yaml`:远端路径没有合理默认值 |
| 3 | 登记基线 | 人 | 一个已晋级的基线模型,`links` 为 `[候选 id, run id]`,那个 run 里放一份诊断报告。报告分数要低于后续候选,否则增益恒为 0,配方永远不被提名 |
| 4 | 设凭证环境变量,写 `backends.yaml` | 人 | 只有 `--planner-backend bridge` 才需要 `planner` 角色,`agent` 角色可选;用 `heuristic` 规划后端时可以不写 |
| 5 | 写首条训练指令,启动 `factory run` | 人 | 开发入口 `run_factory_dev.py` 自动打开 `--allow-mock-evidence`;默认入口不接受 mock 证据,闭环不会自己转 |

第 3 步没有命令,本文列出的命令都不写基线,只能手写产物;第一条训练指令也要手写完整信封,含二十多个训练配置字段。MindAgent 的空库不会自己长出第一条技能,要人手写技能产物与库状态产物。为这几件事加子命令目前没有排进任何工作包。真实部署前先查 `models/` 下是否已有晋级模型。

## 7. 关键决策

决策来自闭环集成阶段的裁决与工作包 9 的细则。下表 FC-20 起的十三条是场景二记忆装配的决策,全部 [已落地]。FC-33 起的七条是记忆按情境世界组织的决策,全部 [已落地]。

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
| FC-12 | 端到端用确定性的 `HeuristicBackend` 规划,桥接只单测正向与失败路径 | 罐头桥接 | 每一步都是真实代码,命令行一个开关选得到 | 桥接往返的格式问题要到接真实后端才暴露 |
| FC-13 | MindAgent 用罐头后端与进程内 loopback 实时服务端,首次需要时才起 | 起外部服务端 | V1 没有加载 checkpoint 的推理后端 | 部署 episode 只证明路由与轨迹,不代表模型能力。记忆装配后部署会话的对手位也包 Actio 实例,记忆是每会话一份空的替身记忆。对手记忆不跨会话留存,部署里演化的只有客户端位的智能体。对手记忆一项已由 FC-34 取代 |
| FC-14 | `run` 与 `reconcile` 在第一个 tick 之前取仓库排他文件锁,`install-defaults` 在写第一个产物之前取,只读子命令不取 [已落地] | 只靠只带本文件提交;把锁放进每次写入 | 挡住两个守护进程重复派发与记账互相覆盖,开销只在启动时一次 | 锁被占用时退出码 2,第二个守护进程起不来 |
| FC-15 | 真实 ROLL 执行器由显式的 `--executor roll` 启用,默认 `mock`;`--endpoint` 点名端点 [已落地]。执行器工厂接收 `run_id`:派发器按指令 id 调 `executor_factory(run_id)`,`MockExecutor` 一侧用 `lambda _: MockExecutor()` 适配 | 看树里有没有 `compute/endpoints.yaml` 就自动启用;环境变量启用 | 一次训练会同步占住调和循环数小时并真的动远端 GPU(CP-15),这个后果值得在命令行上明写;自动启用会让「提交一份配置」悄悄改变入口行为;环境变量在 `--help` 里看不见,也与已有五个开关风格不一致 | 要多输入一个参数;派发器的 `executor_factory` 签名从无参改成带 `run_id`,既有五处测试调用要跟着改 |
| FC-16 | 真实评估器由显式的 `--evaluator someval` 启用,默认 `mock`;它要求 `--executor roll`,暂不能与 `--enable` 同用;真训练一侧不论是否评测都要求污染索引在场并通过哈希校验 [已落地] | 让 `--executor roll` 自动带上真实评估器;把评估并入 `--enable` | 评测要真实存点才有意义,所以依赖真实执行器;但真训练与真评测是两个独立后果,各自一个开关才看得清;污染索引跟着真训练走,是因为「评测题不进训练」是硬约束,与这次开不开评测无关 | 想只换评估器不开真训练做不到;索引缺失时连不开评测的真训练也起不来,要先跑一次导入 |
| FC-17 | 默认不接真推理:不传 `--interact` 时不注入实时客户端与策略工厂,interact 指令判 `failed`;测试与库级调用方自己注入替身策略;`--interact` 才接真推理 [已落地] | 默认就接真推理 | 默认跑测试不碰远端、不占卡 | 打开时要在装配期确认配置齐全,缺项就退出码 2。记忆装配后,配置齐全还含记忆配置:配置齐全,或不设 `RISE_MEMORY_CONFIG` 走替身记忆;配置缺键、权限不对等一律退出码 2 [已落地] |
| FC-18 | `--interact` 是 `run` 与 `reconcile` 的独立装配开关,与 `--executor roll` 互斥 [已落地] | 并进 `--enable` | `--enable` 管调和动作的处理器,不管派发器装配;真实执行器只支持 SFT,interact 指令带 `grpo_steps` 大于 0,同用必然在训练前被判 `failed`,不如装配期拒绝 | 漏开时 interact 指令判 `failed`,所以失败原因必须点名这个开关 |
| FC-19 | `--enable` 含 `plan`、`scheduler.yaml` 的 `allow_interact` 为真、却没传 `--interact` 时,装配期拒绝并点名缺 `--interact` [已落地] | 只打一行警告后继续;不检查,等第一条 interact 指令判 `failed` | 这是启动时就能知道的配置矛盾:规划器可能写出 interact 指令,派发时必然 `failed`,闭环停住。与既有拒绝项同一口径(FC-6),宁可启动失败也不留到运行中 | 想让规划器先写 interact 指令、之后再单独带 `--interact` 派发的用法做不到,要先关掉 `allow_interact` 或补上开关 |
| FC-20 | 记忆库放树外,根目录由本机记忆配置的 `root` 给;路径来自环境变量 `RISE_MEMORY_CONFIG`,文件在仓库外、权限 0600 [已落地] | 记忆库进 git 产物树;记忆根目录写进树内配置 | 记忆库是带 WAL 的 SQLite,不适合当 git 产物;校验口径照本机评测配置,同一套做法 | 不能从产物树追溯某条记忆,靠训练记录里的起点摘要与运行时身份补 |
| FC-21 | 抽取与嵌入都走 OpenAI 兼容端点,地址与模型名写进记忆配置,配置里不放密钥;密钥只读 Starling 固定读的 `OPENAI_API_KEY`,装配期检查非空且只报变量名 [已落地] | 抽取与嵌入各配一把密钥;密钥写进配置 | Starling 的抽取与嵌入都只认 `OPENAI_API_KEY`;密钥不进文件,与全仓库的凭证口径一致(FC-7) | 不能为抽取和嵌入配不同的密钥 |
| FC-22 | Starling 作为可选 extra `memory`,依赖钉 git 提交;记忆配置不在场用替身记忆并在启动日志写 `memory backend: stand-in`;配置要求 Starling 却未装,装配期退出码 2 [已落地] | Starling 进必装依赖;未装时静默退回替身 | Starling 是 C++ 构建,门槛高,仓库未上 PyPI;静默退回会让人以为在用真记忆 | 未装 extra 的环境只测替身路径 |
| FC-23 | Starling 缺的公开接口在 Starling 里补,经 PR 合入,本仓库钉合并提交;本仓库不碰 Starling 的私有接口与表结构 [已落地] | 本仓库直接调私有接口或读它的表 | 私有接口随 Starling 改动而失效,表结构不是契约 | 依赖 Starling 先合 PR;extra 钉的是合并提交,Starling 升级要改钉 |
| FC-24 | 记忆运行时身份是一个字典:后端种类、Starling 版本加安装来源、抽取与嵌入的完整端点、模型、超时、重试次数,以及激活轨迹模式;editable 安装的 Starling 装配期拒绝 [已落地] | 只记 Starling 版本号 | 换 Starling 或换抽取模型,同一起点记忆下的行为也变;editable 安装的源码目录随时在变,身份钉不住。地址只在规范化后比较,不含 userinfo | 身份字段多,任一变了续训就升 T3 |
| FC-25 | 对手位的三种驱动都由对手工厂包进 Actio 实例,`ExternalApiBackend` 改为每个会话新造一个;部署会话的对手位同样包,记忆是每会话一份空的替身记忆 | 对手位保持裸后端;只给 `vllm` 驱动包 | 对手要靠记忆扮演某种人格的个体;共享的外部后端实例现在无害,因为它无状态。会话资源要有归属,所以每个会话各持一个 | 每轮多一次 recall 与 remember,会话变慢;`scripted` 的后端只有 `respond`,指导语被忽略,台词与现在逐字相同。部署对手位的空记忆已由 FC-34 取代 |
| FC-26 | interact 配置的 `memory` 必填,没有"不用记忆"和"不写记忆"的开关;记忆支持落地前写下的 pending interact run 在派发器的配置校验处判 T2,由人重开 [已落地] | `memory` 可选;留一个关写开关 | 记忆变了行为就变,可选就等于留了绕过记忆的口子;旧 run 的 rollout 没有记忆,续上去的有,不是同一个实验 | 落地前的 pending interact run 要人工重开 |
| FC-27 | 每条 rollout 在起点库的私有副本上跑,用完即弃;清扫记忆根目录下 `runs/` 的每个子项,只删这一处,不碰 `deploy/`、`seeds/`、`snapshots/`,不跟进符号链接。`reconcile` 在锁内、装配之后、第一个动作之前清扫,`run` 把清扫作为 `on_locked` 传给 `run_daemon`,在它取到仓库排他锁之后、第一个 tick 之前调用。运行中只删已释放会话的目录 | 同一 run 内共用一个库、记忆累积;运行中周期性扫目录;`reconcile` 与 `run` 都在装配里清扫 | 同组 rollout 的起点相同,组内优势才只反映策略。`run` 的装配在取锁之前,装配里清扫会和另一个持锁进程抢删在途副本;`assemble` 因此只交出清扫函数、不调它。钩子在锁内,本进程还没有会话,别的进程被仓库锁挡在外面,扫目录才安全。运行中扫会误删在用的库 | 记忆不在一条 run 内累积;`run_daemon` 多一个启动钩子参数 `on_locked`;崩溃留下的副本到下一次 `run` 或 `reconcile` 取到锁时才清。每位一份副本已由 FC-35 取代 |
| FC-28 | 一个记忆根目录只属于一个控制面仓库:根目录下 `.owner` 文件记该仓库的绝对路径,首次使用时先写临时文件再以硬链接发布;装配期读到的路径与当前仓库不同,就退出码 2,信息写两个路径 [已落地] | 不记归属,靠使用者自觉;每个仓库在根目录下建自己的子目录;直接以 `O_EXCL` 建 `.owner`(被打断会留下 0 字节文件) | 两个仓库共用一个根目录时,部署记忆会混写,启动清扫又会删掉对方在途会话的副本;仓库锁只挡同一个仓库的进程,挡不住另一个仓库的进程 | 换仓库路径(移动或重新克隆)后要人工改 `.owner` |
| FC-29 | 组合根按记忆配置在场与否选部署记忆管理面:在场用 StarlingDeploymentStore,不在场用 FileTupleStore。FileTupleStore 把数据存成 JSON 文件,根目录是 `<git 目录>/rise-memory`,不是临时目录(重启要保留)。部署驱动器改收实例工厂 make_instance(episode_id, memory, holder),由轮次处理器用组合根给的模型后端工厂造出它,每次尝试新造一个实例 [已落地] | MindAgent 自己读配置、自己选实现;替身不落盘;驱动器仍收一个现成的实例 | 只有组合根读记忆配置;管理面在 `actio`,MindAgent 不导入 `starling`,依赖边界仍可扫描。替身也落盘,备份起点、成功后删快照的提交点协议在两种实现上一样成立。实例按 episode 新造,事件 key 才带各自的逻辑 episode id | 覆盖部署世界必须先关实例与端口,驱动器要管实例的完整生命周期;替身路径在 git 目录里多一个目录 |
| FC-30 | 组合根给每个 Actio 实例外套转换后端(模块 factory.memory_translate),接住 actio 的 MemoryFailureError,按 `transient` 改抛两个 factory 异常子类。FactoryMemoryUnavailableError 继承 simulator 的 RoleMemoryUnavailableError(瞬态),FactoryMemoryFailureError 继承 RoleMemoryFailureError(确定性)。两个基类在 `src/simulator/realtime/backend_timeout.py` [已落地] | simulator 导入 actio 的异常;把记忆失败并进 `role_backend_failure` | simulator 与 mindtrainer 不导入 actio,只有组合根知道两边。并进 `role_backend_failure` 会被译成延后,确定性的故障反复重试,抽取端点的网络抖动又不该升人工 | 组合根多一层包装;分类依赖 actio 对 `transient` 的穷举映射 |
| FC-31 | 部署路(`--enable agent`)的时间预算随记忆配置放宽。配置在场时,服务端等提交取 40 秒,整条流按 `max_turns × 40` 秒折算。替身时保持现值 5 秒与 15 秒;`--interact` 同开时共用一个运行时,等提交取较大的 150 秒、后端单次调用取 90 秒 [已落地] | 部署路一律放宽;一律不变 | 部署现值是按罐头后端定的,罐头在 1 秒内返回;接上真记忆后每轮最坏多 20 秒。替身读本地文件、没有网络调用,放宽只会让超时检测变钝 | 真实模型接进部署路时要再按它的延迟加;不放宽,含网络记忆的部署会话会撞 5 秒超时,轮次判 `failed` |
| FC-32 | `--interact` 的客户端新增消息间隔上限:等相邻两条服务端消息 120 秒(`_next_msg`),超时视作 `timeout`(T1,按条重跑)并 abort;整条流按 `max_turns × 150` 兜底,由轨迹源经 `client_factory(deadline_s=)` 传。服务端等客户端一次提交的 `turn_timeout_s` 取 150 秒,单次后端调用 90 秒。等提交与等相邻消息方向相反、互不嵌套 [已落地] | 只靠整条流 deadline;两个超时取同一个计时器;保持 2026-11-02 之前的 90 秒与 90 秒 | 现在客户端只有整条流的 deadline,服务端失联时要等满整条流。训练位一轮最坏约 140 秒(HTTP 120 秒加记忆 20 秒),低于 150 秒;对手一轮约 80 秒,低于后端的 90 秒,后端又低于相邻消息的 120 秒;内层先超时,外层才看得到分类过的失败。每条对手发言都是一条消息,一轮里有几个对手都不叠加。真机(eager、两个适配器)每轮 20 到 65 秒,原先训练位 60 秒、等提交 90 秒没有余量 | 真模型一轮超过 150 秒会判 `timeout` 重跑;卡死的会话发现得更晚;客户端要用带超时的队列在接收线程里取消息 |
| FC-33 | 记忆以世界为单位:部署一个情境一个世界,训练一条 rollout 一个世界,种子按情境。端口绑定 `(世界, 主体)`;快照、恢复、摘要按整个世界;锁按情境,取代人格锁 | 一个人格一个库;所有情境共处一个世界;按主体局部恢复 | 情境是模拟世界的单位,在场与共识发生在情境里。所有情境一个世界,回滚一个情境的 episode 会退掉别的情境。局部恢复要在多主体共享的表里逐行剔除 | 同一人格在两个情境里记忆不相通;回滚会退掉同世界里别人在快照之后的写入,靠"别的轮次有未提交快照就延后"挡住 |
| FC-34 | 部署对手位进部署世界,跨会话积累。组合根只造一个部署管理面,交给轮次处理器、门② 与服务端三方;`AgentRuntime` 多收可选的 `deployment_store`,只在启用部署轮次时传。对手位经运行时里的部署登记表拿到 round_id | 每会话一份空记忆;放宽 `CreateSession.episode_id` 的白名单,允许 `/` | 对手人格也是这个世界里的人,应当记得以前的相遇。`wait_closed` 的端口计数在管理面实例内,两个管理面互相看不到对方开的端口。episode id 要拼进落盘路径,白名单不能放宽 | 对手行为随部署次数漂移,轮次之间比较多一个变量;运行时多一张登记表;驱动器恢复前最多等 `SESSION_CLOSE_WAIT_S` |
| FC-35 | 每条 rollout 一个世界:客户端 `policy_for_rollout` 建、删,服务端按 episode id 经进程内的表加入;`TrainingWorlds` 取代旧的 TrainingCopies,替身与 Starling 各一个实现 | 服务端建;两端各建各的 | 客户端先于服务端知道 episode id,且 `policy_for_rollout` 包住整条 rollout。服务端建,客户端就要等会话建成才能开训练位端口 | 服务端开端口依赖进程内的表,模拟器搬走后要换成按世界名找;客户端退出时最多多等 70 秒 |
| FC-36 | 同一人格在一个情境里占两个角色位一律拒绝:训练配置 T2,`agent.yaml` 退出码 2,合成的绑定 T2 | 允许,两个位共用一个主体 | 共用主体就共用记忆与事件 key,与"两个位各开一份、互不可见"不同,也没有使用场景。现有配置与测试里没有这种绑定 | 想让一个人格同时扮两个位的配置写不出来 |
| FC-37 | 记忆根目录是按人格的旧布局时拒绝启动,不迁移,信息列出可疑条目,提示移回配置或移出记忆根 | 自动迁移;静默忽略 | 旧数据只来自验证。旧库不会被任何世界读到,静默忽略会让人以为它还在用 | 升级时要手动清空记忆根 |
| FC-38 | 训练重试时同名世界还被服务端端口占着,判瞬态 `FactoryMemoryUnavailableError`,不判确定性 | 沿用保留目录的确定性类别 | 两端改用同一个世界后,服务端收尾最长 60 秒,滞留的更久。重试常紧跟一次瞬态失败,正是收尾最慢的时候。判确定性,一次慢收尾就让整条 run 判 T2 | 真卡死的服务端要等重试用尽、走延后通道,到续训次数上限才升 T3 |
| FC-39 | `memory.digests` 是 `{scenario_id: digest}`,键必须恰好是配置的情境;解析器收情境 id。按人格记键的 interact run 续训判 T3 | 保留人格键;为旧记录做哈希兼容 | 起点的身份从每个人格的库变成每个情境的世界;静默续训会把两种起点混进一个实验 | 已有 interact run 要人工重开 |

## 8. 失败处理与可观测

装配错误一律在启动时暴露;运行中的失败沿用各子系统的分类,factory 不再加一层。

| 失败点 | 类 | 处理 | 留痕产物 |
|---|---|---|---|
| 开关拼错、`reconcile` 缺 `--once` | 用法错误 | 退出码 2,一行错误 | 无,树里不写 [已落地] |
| 缺配置、资产、桥接角色或凭证 | 装配错误,发生在启动前,不属于 T1 至 T3 | 退出码 2,`FactoryConfigError` | 无,树里不写 [已落地] |
| `--interact` 与 `--executor roll` 同用 | 用法错误 | 退出码 2,一行错误,树里不写 | 无 [已落地] |
| `--interact` 打开但缺 `interact` 段 | 装配错误,发生在启动前 | 退出码 2,`FactoryConfigError`,指出缺哪一项 | 无,树里不写 [已落地] |
| `--enable plan` 开着、`allow_interact` 为真,但没传 `--interact` | 装配错误,发生在启动前 | 同上,错误信息点名缺 `--interact` | 同上 |
| 记忆配置缺键或多余键、权限不对、路径在仓库里、地址带 `user:password@` | 装配错误,发生在启动前 | 退出码 2,`FactoryConfigError`,只写键名与路径,不回显取值 | 无,树里不写 [已落地] |
| `OPENAI_API_KEY` 为空 | 装配错误 | 同上,只写变量名 | 同上 [已落地] |
| 产物树里的 `memory_library/` 非空 | 装配错误 | 同上,要求人工处理 | 同上 [已落地] |
| Starling 是 editable 安装 | 装配错误 | 同上,运行时身份钉不住 | 同上 [已落地] |
| 记忆配置要求 Starling 但未安装,或装了而扩展载不进来 | 装配错误 | 同上,信息说明 `pip install '.[memory]'` | 同上 [已落地] |
| 记忆根目录的 `.owner` 记的是别的仓库,或 `runs/` 是符号链接或文件 | 装配错误 | 同上,`.owner` 信息写两个路径,`runs/` 只写相对名 | 同上 [已落地] |
| 记忆根目录是按人格的旧布局 | 装配错误 | 同上,信息列出可疑条目,提示移回配置或移出记忆根 | 同上 [已落地] |
| 绑定里同一人格占两个位 | `agent.yaml` 是装配错误;训练配置与合成绑定判 T2 | 退出码 2;指令 `failed` | 无;指令 `payload["failure"]` [已落地] |
| 点名的情境 `purpose` 不是 `training` | 装配错误 | 同上 | 同上 |
| 点名的情境与人格只写了一半 | 装配错误 | 同上 | 同上 |
| 点名的情境或人格不在树里 | 装配错误 | 同上 | 同上 |
| 没传 `--interact` 却有 interact 指令 | T2 | 指令判 `failed`,原因点名 `--interact`:`interact mode needs a client_factory and a policy or policy_factory; start factory run or factory reconcile with --interact to run interact directives` | 指令 `payload["failure"]` [已落地] |
| 种子世界打不开、迁移失败、摘要算不出 | T2 | 在派发器进入策略上下文之前判:指令 `failed`,原因只报类名 | 指令 `payload["failure"]` [已落地] |
| 续训时起点摘要或记忆运行时身份变了 | T3 | `config_hash` 不同,现有的门升 `NeedsHumanError` | 训练记录与失败记录 [已落地] |
| 对手位记忆调用失败,瞬态(召回的查询词嵌入失败,或 `embed_pending` 有嵌入失败;抽取失败不抛,只计数) | T1 | 终止原因 `role_memory_unavailable`;轨迹源按条重跑 | `failed_attempts` [已落地] |
| 对手位记忆调用失败,非瞬态(Starling 抛的其余异常、`sqlite3` 异常、`OSError`、非 Starling 异常;映射按异常类型穷举,新情形先落确定性) | T2 | 终止原因 `role_memory_failure`;轨迹源不重跑;策略上下文把它加进确定性集合,指令判 `failed`,不延后 | 指令 `payload["failure"]` [已落地] |
| 训练位记忆调用失败 | 瞬态按条重跑,其余 T2 | 轨迹源在 `_attempt` 里接住,瞬态译成可重试的失败,其余是策略异常,会话 abort;策略上下文译成 `DirectiveFailedError`,不延后 | 同上 [已落地] |
| 训练重试时同名 rollout 世界仍被服务端端口占着 | T1 | 瞬态 `FactoryMemoryUnavailableError`,记为 `role_memory_unavailable` 重跑;重试用尽走延后通道,指令留 `pending` | `failed_attempts` [已落地] |
| 服务端按 episode id 找不到 rollout 世界 [已落地] | 延后,续训上限用完升 T3 | 造后端失败,会话以 `role_backend_failure` 被拒;轨迹源不重跑,`interact_policy` 译成 `RolloutDeferredError`,指令留 `pending` | 守护进程延后日志 |
| 部署世界锁被占,或世界里有别的轮次的未提交起点快照 | 资源暂不可用 | `DeferredError`,原因写出快照所在的轮次 id;连续 20 次后 `DeferredExhaustedError` 退出,这正是要人看到 | 一行结构化日志 [已落地] |
| 部署驱动器等服务端端口关完超过 `SESSION_CLOSE_WAIT_S` | 恢复前:需人工;开下一次尝试或提交前:资源暂不可用 | 恢复前按 `RestoreFailedError`,库与快照保留;其余轮次留 `pending`,重入时先恢复快照 | 轮次产物与失败记录 [已落地] |
| rollout 世界删除失败 | 不分类 | 记日志;同名重试先删后建,或下一次 `run` 或 `reconcile` 取到仓库锁后的清扫兜底 | 一行日志 [已落地] |
| 会话终态时在途后端线程在 60 秒内没退出 | 不分类 | 资源不关不删,进滞留表并继续占一个会话名额;每 60 秒重试一次,成功后才关端口 | 守护进程日志每分钟报滞留数 [已落地] |
| 准入时活动会话数加滞留数达到 `max_workers` | T1 | 拒绝 `CreateSession`,终止原因 `role_backend_unavailable`,客户端按条重跑;挂死的后端要人重启进程才释放 | 同上 [已落地] |
| 规划桥接失败 | T3 | 决策 `failed`,不写新指令(R18) | 决策与失败记录,带桥接日志的 `log_refs` [已落地] |
| 轮次产物读不出 | T3 | 轮次处理器重写为 `failed`,循环继续(R19) | 轮次产物与失败记录 [已落地] |
| 轮次中途瞬态异常或被杀 | T1 | 轮次留在 `pending`,守护进程退出;重启后的 tick 复用已落盘的 episode 续上(R19) | 已落盘的 episode [已落地] |
| 派发器或处理器抛 `DeferredError` | 资源暂不可用 | 不写树,按退避重试;连续延后超过上限抛 `DeferredExhaustedError` 退出 | 一行结构化日志 [已落地] |
| interact 指令起 vLLM 服务:租约被占 `ComputeBusyError`,或起服务失败 `ServeStartupError` | 资源暂不可用 | 同上 | 同上 [已落地] |
| interact 指令里真推理的 rollout 失败:对手 API 或 vLLM 服务连不上、角色位后端出错、重试用尽的超时、落盘失败(`invalid_session` 除外,它仍判 T2) | 资源暂不可用 | 策略上下文译成延后类:不写树,按退避重试,进程不退出;每次重派耗一次 `resume_attempts`,到上限升级为 T3。interact 的配置校验要求 `max_resume_attempts`(默认 3)小于守护进程的连续延后上限 `MAX_CONSECUTIVE_DEFERRALS`(20,`DeferralBook` 的缺省值),所以一直失败的 run 先到续训上限、判 T3,不会先撞上 `DeferredExhaustedError` 让守护进程退出 | 一行结构化日志;终局时指令 `failure.category = T3` [已落地] |
| 派发器或处理器抛其他瞬态异常 | T1 | 进程退出,外部监督者重启 | 树里已提交的部分 |
| 门控写证据不在场或迁移非法 | 用法错误 | 命令抛错,不写文件、不提交 | 无 [已落地] |

只读观测手段:

| 想知道 | 用什么 | 状态 |
|---|---|---|
| 一轮的全部产物、状态与溯源链 | `controlplane.observe.trace`;`factory trace <repo> <run>` | [已落地] |
| 一轮的成本明细与汇总 | `controlplane.observe.cost`;`factory cost <repo> <run>` | [已落地] |
| 失败记录登记的日志 | `factory logs <repo> <run> [--task <id>]` | [已落地] |
| 待审候选 | `rise status` | [已落地] |
| 候选的诊断证据 | `rise review <id>` | [已落地] |
| 本 tick 做了什么 | `factory reconcile <repo> --once` 的输出 | [已落地] |

`logs` 对不可达的引用标 `[unreachable]` 并写明不显示内容(本机读文件时的权限等 `OSError` 同样算不可达),哈希不符标 `[sha256 mismatch]` 但照样打印 [已落地]。它是排查问题时最需要的命令,所以树里有坏东西时自己不崩:读不出的产物标 `[unreadable]` 并写明异常类名,`log_refs` 不是列表或引用缺字符串 `uri` 时标 `[malformed]`,其余引用照常打印;`file://` 地址按百分号编码解码 [已落地]。成功调用的桥接日志不进树,只在控制面仓库的 `.rise/logs/<run>/` 目录里。

## 9. 测试策略

已落地部分用真实的 git 仓库与 mock 执行器;闭环部分在此之上加真实的闭环处理器,外部工具一律用替身。

| 层次 | 对象 | 替身 | 测试文件 | 用例数 |
|---|---|---|---|---|
| 入口 | 守护进程入口单 tick、参数解析、到晋级、重复 tick 幂等 | `MockExecutor`、`ScriptedBackend` | `tests/test_mt_wire.py` | 4 [已落地] |
| 观测 | `trace`、`cost`、`sum_costs` | 临时仓库 | `tests/test_observe.py` | 5 [已落地] |
| 命令行 | `review`、`approve`、`reject`、`status` | 临时 git 仓库 | `cli/test/commands.test.ts` | 19 [已落地] |
| 命令行 | 命令名常量 | 无 | `cli/test/smoke.test.ts` | 1 [已落地] |
| 开关与边界 | 映射表;无包导入 factory | 无 | `tests/test_factory_switches.py`、`tests/test_factory_boundaries.py` | 17、2 [已落地] |
| 只读 | `trace`、`cost`、`logs` | 临时仓库 | `tests/test_factory_readonly.py` | 16 [已落地] |
| 桥接装配 | 角色、凭证不外露 | `sys.executable -c` 假可执行体 | `tests/test_factory_bridges.py` | 14 [已落地] |
| 评估器装配 | 配置校验、默认资产 | `MockSoMEvalRunner`、`MockJudgeBackend` | `tests/test_factory_evaluation.py` | 16 [已落地] |
| 组合根与命令行 | 逐字节相同、注入、装配错误、退出码、仓库锁被占用时的退出与只读照常 | 记录调用的假处理器 | `tests/test_factory_wire.py`、`tests/test_factory_cli.py` | 40、13 [已落地] |
| 端到端框架 | 空闭环一个 tick、mock 证据默认不转 | 假处理器 | `tests/test_factory_e2e_frame.py` | 4 [已落地] |
| 处理器构造 | 六个构造器、证据开关单一来源、Interact 流超时 | 罐头后端 | `tests/test_factory_handlers.py` | 93 [已落地] |
| 端到端 | 完整闭环、R5、规划等配方入库门、R18、R19、MindAgent 链 | `HeuristicBackend`、罐头后端 | `tests/test_factory_e2e_loop.py` | 10 [已落地] |
| 跨工作包 | 共享用例表、分层规则、run 编号数值序 | 无 | `tests/test_factory_cross_plan.py`、`tests/test_factory_run_order.py` | 32、3 [已落地] |
| `--interact` 装配 | 十一条拒绝项:第 3、9 条合用一组测试,第 10 条没有装配测试(钉住的是派发器的 `ValueError`),其余八条各有测试;打开后派发器带上实时客户端与策略工厂;真推理的 rollout 失败走延后通道、一直失败升 T3;不打开时 interact 指令的失败原因点名开关;收尾时服务停不下来判 T2;外部 API 对手超时或回 503 一次后整条重跑、一直回 503 时指令保持 `pending`、回 404 判 `failed`(T2);scripted 对手位没有台词时派发期判 T2;`opponent` 段在装配之后改了,下一条指令用新值,读不到或写坏了判 T2 且不带地址 | 假 serve、本机 HTTP 桩 | `tests/test_factory_interact_wire.py` | 111 [已落地] |
| `--interact` 端到端 | `factory reconcile --once --interact` 走到候选 `pending_review`,episode 都在产物树里 | 假 vLLM(本地 HTTP 回固定文本)、mock 执行器 | `tests/test_factory_interact_e2e.py` | 2 [已落地] |
| 记忆配置读取与安装来源 | 文件不存在、权限过宽、路径在仓库里、缺键或多余键、地址带 userinfo;`OPENAI_API_KEY` 为空、editable 安装、要求 Starling 却未装、`.owner` 属于别的仓库,各自退出码 2、不回显取值;`.owner` 先写临时文件再以硬链接发布,并发首次使用只有一个成功,读到 0 字节的旧 `.owner` 拒绝;`RECORD` 只哈希 `starling/` 下带哈希的行。需要真 Starling 的用例在 extra 未装时跳过,真抽取与真嵌入不进测试 | 临时目录、临时仓库、假的安装元数据 | `tests/test_factory_memory_config.py` | 73 [已落地] |
| 记忆后端选择与清扫时机 | 没设变量走替身并只记一次启动日志;只开 `plan`、`nominate`、`track` 不读变量;`--interact`、`agent`、`admit` 任一在场就读且坏配置退出码 2;`runs/` 是符号链接或文件装配期退出码 2;`assemble` 不调清扫,`reconcile` 在锁内、第一个动作之前调,`run` 经 `on_locked` 传;清扫失败时关掉装配、不跑 tick;部署路等提交取 40 秒、替身保持 5 秒、同开取 150 秒 | 假的安装元数据、假 Starling、FileTupleStore、临时仓库 | `tests/test_factory_memory_wiring.py` | 38 [已落地] |
| 身份、布局与解析器 | 替身运行时身份、Starling 运行时身份的五个键、布局路径与不安全名字、起点摘要按情境解析(种子目录不在或库损坏判 T2,不当成空库;种子里没有某情境的世界则该情境从空世界起,以人格命名的旧种子库不读)、`memory` 块进 `config_hash`;`import factory.memory_runtime` 不需要 Starling | 假 Starling | `tests/test_factory_memory_runtime.py` | 43 [已落地] |
| 训练世界 | 同一 rollout 的训练位与对手位在同一世界、互不召回;同组两条 rollout 从同一种子出发、互不可见;服务端找不到世界时会话被拒;客户端在服务端端口关完之后才删世界,端口晚关时等着;同名世界仍被占时重试判瞬态;种子只读也能复制(连 `-wal`);清扫只删 `runs/` 的子项且不跟进符号链接。替身与 Starling 两个 `TrainingWorlds` 过同一套契约 | 替身世界;真 Starling 加本机回环的假抽取与嵌入端点 | `tests/test_factory_training_worlds.py` | 55 [已落地] |
| 世界装配 | 配置在场时,旧布局在每个需要记忆的入口拒绝启动,先于导入 Starling;替身在造部署记忆时拒绝;`agent.yaml` 人格重复退出码 2;部署管理面只造一个,交给轮次处理器、门② 与 `AgentRuntime`;真实服务端上跑一轮部署,对手位写进情境世界 | 临时目录、替身部署世界 | `tests/test_factory_memory_wiring.py`、`tests/test_factory_handlers.py`(见上) | 已计入上表 [已落地] |
| 转换后端 | 瞬态记忆失败改抛 `FactoryMemoryUnavailableError`,其余改抛 `FactoryMemoryFailureError`,`close` 里的失败按确定性;非记忆异常原样透传;文字只含类名 | 假记忆端口 | `tests/test_factory_memory_translate.py` | 14 [已落地] |
| 替身路径、部署路与对手包 Actio | `--interact` 一条 run 的训练记录有 `memory` 块、启动日志写 `memory backend: stand-in`、每个对手位是带记忆的 Actio 实例(三种驱动,`ExternalApiBackend` 每个会话新造,`scripted` 台词与未包时逐字相同);部署路的轮次与收录处理器共用同一个替身部署记忆、重装配后根目录仍在 | 本机 HTTP 替身 vLLM、假 serve、替身记忆 | 并在 `tests/test_factory_interact_wire.py`、`tests/test_factory_handlers.py`、`tests/test_factory_interact_e2e.py`(见上) | 已计入上表 [已落地] |

上表的 pytest 用例数由 `pytest --collect-only -q` 逐个文件实测,按参数化展开计,含运行期跳过的用例(不装 extra `memory` 时依赖 Starling 的参数化用例运行期跳过);TypeScript 的两个数按测试文件的 `it(` 计,未跑 vitest 收集。工作包 9 另有一个前置核对脚本 `scripts/check_factory_assumptions.py`:它逐行核对各包的符号名与签名,本次运行有 20 行 `OK`、1 行 `MISMATCH`,缺的是 `SchedulerConfig` 上新增的 `strategy_experience` 字段。

不依赖的外部服务:GPU、外部网络、真实模型、真实 `claude` 与 `codex` 命令行。实时服务端只绑 `127.0.0.1`,在进程内起停。

## 10. 现状、局限与未核实项

组合根、两个入口脚本、命令行泛化与端到端都已落地;闭环只在 mock 证据、`HeuristicBackend` 与罐头后端上跑过,真实 ROLL 执行器与真实 SoMEval 是显式开关且暂不能与闭环同开。`--interact` 让 interact 指令在真实入口跑通,训练仍是 mock [已落地]。场景二的记忆装配 [已落地]:本机记忆配置、Starling 可选 extra、每条 rollout 的 Actio 实例与六条新增拒绝项。记忆按情境世界组织的组合根部分也已落地:每条 rollout 一个世界、一个情境一个部署世界、第 18、19 条拒绝项。真抽取与真嵌入没有在真端点上跑过(见未核实项)。

| 能力 | 状态 | 说明 |
|---|---|---|
| 守护进程入口 `mindtrainer.wire` 与 `scripts/run_controlplane.py` | [已落地] | 只注入 MindTrainer 派发器;interact 指令因缺实时客户端判 `failed`。`factory run` 与 `factory reconcile` 的 `--interact` 补上实时客户端与策略工厂 [已落地] |
| TypeScript 命令行 `review`、`approve`、`reject`、`confirm`、`status` | [已落地] | 门控写覆盖三类需审核产物与待确认指令 |
| 组合根包 `src/factory`、入口脚本 `scripts/run_factory.py` 与 `scripts/run_factory_dev.py` | [已落地] | 不传 `--enable` 时与守护进程入口逐字节相同 |
| 开关、只读子命令、桥接、评估器装配、六个处理器构造 | [已落地] | 工作包 9 任务 100 至 103、105 至 107 |
| 完整闭环与跨工作包核对 | [已落地] | 任务 108 至 109;端到端只用 mock 证据、`HeuristicBackend` 与罐头后端 |
| 真实 ROLL LoRA SFT(经远端计算层) | [已落地] | 由 `--executor roll` 装配,暂不能与 `--enable` 同用;真机只跑通单段两步 |
| 真实 SoMEval 与 `import-sombench` | [已落地] | 由 `--evaluator someval` 装配,必须同时给 `--executor roll`;真机只验证到预测阶段 |
| `--interact`:interact 指令在真实入口跑通 | [已落地] | 真推理加 mock 训练,与 `--executor roll` 互斥;装配期十一条拒绝项。不打开时 interact 指令的失败原因点名该开关 [已落地] |
| 记忆装配:本机记忆配置读取与运行时身份、`--interact` 与 `--enable agent` 的记忆装配、部署记忆管理面的选择与 make_instance 注入、记忆异常转换后端、第 12 至 17 条拒绝项、`policy_for_rollout`、对手工厂包 Actio、`runs/` 启动清扫、`.owner` 归属检查、`memory` extra | [已落地] | 替身与 Starling 两路都有装配测试;真抽取与真嵌入端点没有跑过 |
| 会话资源:`BackendsFactory` 改收会话信息并返回会话资源对象、`on_session_closed` 改收 `session_id`、客户端等相邻两条服务端消息 120 秒(`--interact`)、整条流按 `max_turns × 150` 兜底 | [已落地] | 组合根一侧的登记表按 `session_id` 归属;服务端一侧的实现在 SocialSimulator |
| 记忆按情境世界组织的组合根部分:`TrainingWorlds`、按情境的解析器与 `memory.digests`、共用一个部署管理面、`deployment_store` 注入、第 18、19 条拒绝项 | [已落地] | 替身与内嵌 Starling 两个后端都已接通;Starling 记忆服务落地后只换后端实现 |
| 真实 GRPO 加 OPD 训练与真实 judge | [推迟] | 仍用 mock |

| 局限 | 影响 | 缓解 |
|---|---|---|
| 闭环模式的诊断恒为 mock 证据 | 默认入口下闭环永远不会自己转 | 开发入口打开 `--allow-mock-evidence` |
| mock 诊断对每个候选打同样的分 | 首轮之后增益恒为 0,不再提名新配方,退化标记不会出现 | 首次闭环人工登记更低分的基线 |
| 装配是一次性的 | 改 `evaluator.yaml`、`backends.yaml`、`agent.yaml` 要重启 | `scheduler.yaml` 与模板每次调用重读;`training_defaults.yaml` 的 `opponent` 段在每条 interact 指令进入策略上下文时重读(装配期的检查仍是启动门槛) |
| 外部推理与部署会话同步执行 | 调用期间调和循环不转,审核被饿住 | 记录调用时长,必要时改异步 |
| 瞬态异常让进程退出 | 需要外部监督者,但没有任何工作包交付监督脚本、重启退避或崩溃计数;规划与轮次处理器在非桥接瞬态异常下没有重入次数上限,只有 MindTrainer 的续训次数有上限 | 调和循环无状态,重启从树续接 |
| `logs` 看不到成功调用的桥接日志 | 只能看到失败记录登记的引用 | 直接看 `.rise/logs/<run>/` |
| 默认诊断资产是一个一级维度、两道题的最小夹具 | 只证明流程,规划器只看到一个维度 | 换真实题库 |
| 一条部署会话超过缺省的 15 秒即 `DEADLINE_EXCEEDED`;`--interact` 下客户端等相邻两条服务端消息 120 秒、整条流按 `max_turns × 150` 兜底 [已落地] | 轮次 `failed` | `AgentRuntime` 的 `interact_deadline_s` 只是没给流 deadline 时的缺省,不在 `agent.yaml` 里;流 deadline 由调用方经 `client_factory(deadline_s=)` 传,没有人再改写这个属性 [已落地]。部署路:只有 `--enable agent` 且是 Starling 时服务端等提交 40 秒、整条流 `max_turns × 40` 秒,替身时保持 5 秒与 15 秒;`--interact` 开着取 150 秒(FC-31)[已落地] |
| Starling 未上 PyPI,只能源码或 git 安装 [计划中] | 源码构建依赖 C++ 工具链,本机一次约 179 秒(环境测量,不作预算依据);extra 钉的是 Starling 的合并提交,仍未上 PyPI | 不装 extra 的环境走替身记忆,只测替身路径 |
| 记忆库必须与 Actio 实例同机 [计划中] | 否则每轮 recall 都跨机往返;模拟器移到计算层后,记忆根目录在 GPU 主机上 | 记忆配置的 `root` 指本机目录 |
| 部署对手位的记忆随部署次数漂移 [已落地] | 一个情境部署得越多,对手越熟悉智能体;轮次之间比较行为时多一个变量 | 验证重放不受影响:重放只跑智能体位,对手的话按存档原样回放 |
| rollout 世界靠进程内的表在两端之间共享 [已落地] | 客户端与服务端必须在同一个进程里 | 模拟器搬到 GPU 主机后,改由 Starling 服务按世界名找世界 [推迟] |
| 挂死的对手后端会占住会话名额 [已落地] | 滞留数达 `max_workers` 时拒绝所有新会话;Python 的 daemon 线程杀不掉 | 日志每分钟报滞留数,由人重启进程 |
| 训练里没有 TTL、回放与巩固 [已落地] | 每轮只嵌入、不跑后台维护;后台维护有随机性与负载门控,同一起点会得到不同状态 | 无;为了可复现 |
| TypeScript 的 `writeProduct` 无条件提交,读取不经迁移与 Schema 校验 | 两侧校验强度不同;空提交会抛错。提交已只带本文件 | 命令行只改状态字段;根治要两侧共用读写 [计划中] |

| 未核实项 | 影响 | 核实办法 |
|---|---|---|
| `cli/` 侧用例数与真实运行结果 | 用例数按源码计,未实跑 | 在 `cli/` 目录跑一次 `npx vitest run` |
| 前八个工作包的符号名与本文是否同名 | 组合根引用的名字可能对不上 | `scripts/check_factory_assumptions.py` 本次 20 行 `OK`、1 行 `MISMATCH`:`SchedulerConfig` 已多出 `strategy_experience` 字段,脚本的期望列表还没跟上 |
| 外部命令行的参数模板与用量输出 | 成本数字可能缺失,模板可能要调 | 用真实 `claude`、`codex` 各调一次,看输出里有没有用量 |
| 规划经真实桥接的正向路径 | 带围栏、带说明文字、被截断的输出只在接真实后端时暴露 | 接真实后端,用 `--planner-backend bridge` 跑一圈 |
| 规划与提案的质量 | 罐头与启发式后端只证明流程与解析 | 接真实后端后人工评审一批决策 |
| 外部推理成本少记 | 重入时同一组 `(task_id, cost_type)` 被去重,`cost` 输出是下限 | 构造一次规划重入,对比桥接日志条数与账本条数 |
| 成本增益上下文大多为空 | 全仓没有 GPU 小时的写入方,规划器只看得到 token 数 | 真实执行器接入后检查 `run_trace` 的 `cost_records` |
| 配方候选的 `repro` 恒有两条缺口 | 审核人容易对错误列表脱敏 | 让评估器把分类表与题库版本写进报告后,看候选的 `repro.errors` 是否清空 |
| 后端从 mock 切到 real 后的头几轮 | real 候选对非 real 基线没有可比增益,提名跳过 | 切换后观察提名标记的 `reason` |
| 已提交产物损坏时进程退出,重启后同一处再抛 | 循环卡死,需人工修树 | 构造一个带合并冲突标记的指令,观察守护进程行为 |
| 第一次闭环没有基线模型 | 没有可比增益就不会提名 | 真实部署前检查 `models/` 下是否已有晋级模型 |
| `factory run` 长跑 | 只用 `--max-ticks` 跑过几轮;端口与 15 秒超时都没实测。通道数有界由用例证明:会话结束的客户端,通道在下一次发客户端时关掉,在途只留尚未结束的那几条 [已落地];长跑下的文件句柄未实测 | 跑数小时长循环,监控进程的端口与文件句柄 |
| `--interact` 与 `--enable agent` 共用一个运行时时,落盘钩子是否也收到部署会话的 episode,是否与轮次处理器自己的落盘重复 [已落地] | 部署 episode 重复落盘或被误写进 interact 的数据 | 已定:钩子只写训练登记过的情境 id;本 run 的情境与 `agent.yaml` 的部署情境相交时指令判 `failed`;`tests/test_factory_handlers.py` 与 `tests/test_factory_interact_wire.py` 各有用例 |
| `--interact` 打开时注入数据生产器 [已落地] | 「按种子生成」一路要靠生产器合成,不注入则这类指令判 `failed` | 已定:`--interact` 一律注入 `TemplateDataProducer`;`tests/test_factory_interact_e2e.py` 用带 `data_spec` 的 interact 指令走 `reconcile --once --interact` |
| 策略工厂连哪个计算端点 [已落地] | 端点来源不明,`policy_backend` 的 `trained.endpoint` 无从取值 | 已定:`--interact` 允许与 `--endpoint` 同用;只有一个端点时自动选,多个时必须用 `--endpoint` 点名(`select_endpoint`)。`--endpoint` 只在 `--executor roll` 或 `--interact` 时有意义,其余组合装配期拒绝(`src/factory/wire.py`) |
| 对手用 `external_api` 时缺凭证环境变量 [已落地] | 已定:装配期拒绝(第 9 条拒绝项),不是派发期失败 | 与 `factory.bridges` 同一口径:凭证在装配期解析,缺就起不来;异常只报变量名。补一条装配测试 |
| 真抽取与真嵌入的单次延迟是否在 4 秒内 [计划中] | 超时的抽取落成抽取失败;超时的召回让对手位一轮失败,按瞬态重跑 | 接真端点跑 10 轮,记每次召回、三段抽取与嵌入的耗时 |
| 每条 rollout 开一个世界对 GRPO 一步总时长的影响 [计划中] | 一步 `groups_per_step × group_size` 条 rollout 各复制种子、等服务端端口关完、删世界 | 跑一步,计复制、等端口关完、删除的总耗时 |
| 外部端点同一模型名换了向量,摘要会变、续训升 T3,但旧向量不可复现 [计划中] | 要复现需缓存向量 | 观察一个月内同一端点的向量是否稳定,再决定要不要加向量缓存 |
| 两个开关同开时服务端等提交取 150 秒、后端单次调用取 90 秒 [已落地] | 部署会话只是多等,超时检测变钝。整条流 deadline 按调用方给:`client_factory(*, deadline_s=)`,轨迹源与部署驱动器各自传 `max_turns × 150` 与 `max_turns × 40` | 装配测试断言同开时运行时的 `turn_timeout_s` 是 150 秒、`message_timeout_s` 是 120 秒、后端上限是 90 秒(`test_both_switches_share_the_larger_turn_timeout`) |
| 起点库由旧版本 Starling 写下、新版本打开时能否打开 [计划中] | 种子库打不开则每条指令判 T2 | 在 Starling 的 PR 里加用例:旧版本库备份后用新版本打开 |
| 服务端关端口的耗时是否远低于 70 秒 [计划中] | 超过 `SESSION_CLOSE_WAIT_S` 时,部署恢复要人工处理,训练世界进保留表、留给清扫 | 跑 100 条部署 episode,记每条从客户端结束到服务端端口关完的耗时 |
| 部署对手位积累记忆后,智能体位的行为是否随部署次数漂移 [计划中] | 轮次之间的发现集合不可比 | 同一模型连续部署 5 轮,比较每轮的发现集合 |
| TypeScript 泛化只按测试文件的用例清单核对过 | 本次没有在 `cli/` 仓库里实跑 vitest,用例数按源码里的 `it(` 计 | 在仓库里跑一次 `npx vitest run` |

明确不做:真实 GRPO 加 OPD 训练与真实 judge 的接入;桥接的 API 形态;运行中热加载配置;进程内自动重试与自我重启;两种语言合并成一个命令行;图形界面;把记忆库放进产物树;迁移旧的 `memory_library/`;跨租户共享记忆;迁移按人格布局的记忆根目录;跨情境共享一个人格的记忆。

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
| `--interact` | `run` 与 `reconcile` 的装配开关:给派发器注入实时客户端与策略工厂,让 interact 指令在真实入口跑完;与 `--executor roll` 互斥 [已落地] |
| 记忆配置 | 树外的本机 YAML,路径由环境变量 `RISE_MEMORY_CONFIG` 给出,权限 0600;给出记忆根目录与抽取、嵌入端点,不放密钥 [已落地] |
| 替身记忆 | 记忆配置不在场时用的替身,启动日志写 `memory backend: stand-in`。训练路与对手位用 TupleMemory,包现有的记忆条目元组。部署路的管理面替身是 FileTupleStore,JSON 落盘 [已落地] |
| 记忆 extra | `pyproject.toml` 的可选依赖 `memory`,即钉 git 提交的 `starling-memory`;不装它就只能用替身记忆 [已落地] |
| 记忆运行时身份 | 装配期算出的字典:后端种类、Starling 版本与安装来源、抽取与嵌入的端点和模型、轨迹模式;与起点摘要一起进 `config_hash` [已落地] |
| 世界 | 一个记忆库或一份替身账本,里面有多个主体。部署按情境,训练按 rollout,种子按情境 |
| 主体(holder) | 世界里的一个人格;端口绑定 `(世界, 主体)`,召回只返回本主体名下的记忆 |
