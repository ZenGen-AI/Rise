# Actio:带技能库与显式记忆的智能体框架

| 项 | 内容 |
|---|---|
| 一句话 | 把技能、推理经验与记忆渲染成指导语交给模型后端,每次响应附带激活轨迹 |
| 状态 | [已落地]:技能、推理经验、记忆、激活轨迹与协议打通,十一个源文件,185 个用例;MindAgent 部署驱动器已装配实例,但默认入口用的仍是罐头后端,真实模型是否遵循指导语未核实,见「现状」 |
| 所在层 | 共享基础设施 |
| 代码位置 | `src/actio` [已落地];协议改动在 `src/simulator/realtime` 与 `src/simulator/runner.py` |
| 基线提交 | `b826440`(2026-10-21) |
| 依赖 | `simulator.datamodel`、Python 标准库 |
| 被依赖 | SocialSimulator(运行器把实例当后端)、MindAgent(部署驱动器、分析器、收录处理器) |
| 对应实施计划 | 工作包 6(任务 70 至 78)[已落地],无待施工任务 |

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

Actio 是部署态智能体的运行框架:不改模型权重,靠技能库与记忆库改变模型的行为。

它解决两个问题。其一,让同一个模型在不同技能库下给出不同的回应,技能库可以审核、可以版本化。其二,每一轮都留下"为什么这样回应"的结构化记录,即激活轨迹,供 MindAgent 跨 episode 分析行为缺口。

Actio 不读写产物树。技能库与记忆库由 MindAgent 读出来传给它,轨迹由调用方带走。它站在共享基础设施层,上游是两个调用方,下游是模型后端。

<!-- figure:begin 40-actio-context -->
![Actio 的上下文](diagrams/40-actio-context.svg)

**图 1　Actio 的上下文**　SocialSimulator 的运行器与 MindAgent 的客户端循环都实例化 Actio 并调用 respond。Actio 把技能、经验与记忆渲染成指导语交给模型后端,包外只导入 simulator.datamodel。技能库与记忆库由 MindAgent 读入后传给 Actio,Actio 自己不读产物树。

<details>
<summary>文字版:图中的节点与连线</summary>

**节点**

- SocialSimulator:仿真运行器 run_episode / 实时环境服务 · episode 落盘
- MindAgent:部署驱动器的客户端循环 / 分析 · 提案 · 验证
- Actio:instantiate 组装 ActioInstance / respond 返回三元组与激活轨迹
- simulator.datamodel:Persona · Scenario · TurnRecord
- 模型后端:respond 或 respond_guided / 测试用罐头后端
- 技能库与记忆库:skill_library/ · memory_library/ / 由 MindAgent 读写

**连线**

- SocialSimulator → Actio:runner 把实例当后端
- MindAgent → Actio:instantiate · respond
- MindAgent ↔ SocialSimulator:② submit_turn 带轨迹
- Actio → 模型后端:指导语 + history 副本
- Actio → simulator.datamodel:唯一的包外导入
- 技能库与记忆库 → MindAgent:读入条目与库版本

</details>
<!-- figure:end 40-actio-context -->

读图说明:

- MindAgent 读入技能库与记忆库的条目和库版本,调用 `instantiate` 组装实例,再逐轮调用 `respond` [已落地]。
- SocialSimulator 的运行器 `run_episode` 可以直接把实例当后端,因为 `respond` 的参数与 `ModelBackend` 相同。
- Actio 只向模型后端输出指导语列表与 history 的深拷贝;包外导入只有 `simulator.datamodel` 一处。
- MindAgent 与 SocialSimulator 之间的受限直连接口②经 `submit_turn` 携带轨迹,不经过 Actio。

## 2. 目标、非目标与约束

Actio 的 V1 只做"按规则选技能、按词面取记忆、把选择写进轨迹",不做学习与语义检索。

### 2.1 目标

| 编号 | 目标 | 验收方式 |
|---|---|---|
| G1 | 实例的 `respond` 返回三元组 `(utterance, inner_thought, trace)` [已落地] | runner 直接把实例当后端跑完一个 episode |
| G2 | 选中的技能改变模型输出 | 罐头后端回显指导语,有技能与无技能的输出不同 |
| G3 | 激活轨迹经 gRPC 往返与落盘后逐字段、逐类型相等 | 端到端测试经真实 loopback 服务端比对 |
| G4 | 路由、快照、检索、三级回退都是纯函数 | 同输入同输出;结果与输入顺序无关 |
| G5 | 坏数据构造不出来 | 直接构造与 `from_dict` [已落地] 走同一套校验,报错指出字段 |
| G6 | 包的依赖边界可机器检查 | 静态测试用 `ast` 扫描全部导入 |

### 2.2 非目标与推迟项

| 项 | 状态 | V1 的处理 |
|---|---|---|
| 技能库与记忆库的产物读写、`library_state` | 不在本包 | 由 MindAgent 的收录处理器读写,本包只给字典形状 |
| 语义检索、近义词匹配 | [推迟] | 只做词面重叠,二字切分 |
| 记忆按主体过滤 | [推迟] | `retrieve` 支持 `subjects` 参数,实例不传 |
| `profile` 参与决策 | [推迟] | 只深拷贝保存 |
| 记录"后端是否照做指导语" | [推迟] | 轨迹只记录路由决定 |
| MindTrainer 的 InteractTrain 接 Actio 实例 | [推迟] | 它按二元组解包,本工作包不改 |

### 2.3 质量属性与硬约束

| 约束 | 内容 | 影响 |
|---|---|---|
| 依赖边界 | 只导入 `actio` 自身、`simulator.datamodel` 与标准库 | 后端协议在本包按结构重新声明 |
| 轨迹无数字 | 轨迹叶子只有 `str`、`None`、`bool` | 库版本整数在入口处转成十进制字符串 |
| 无全局可变状态 | 信号注册表是实例,`default_registry()` [已落地] 每次新建 | 并发会话互不影响 |
| 新参数 keyword-only | `instantiate` 除后端外、三处 `activation_trace` 参数 | 位置传入抛 `TypeError` |
| 既有测试不改 | 二元组后端与既有用例照旧通过 | 不带轨迹时 `ClientDrivenBackend` 仍返回二元组 |
| 脱离真实环境可测 | 不需要 GPU、外部网络、真实模型 | 用罐头后端与 loopback gRPC |

## 3. 架构

Actio 是十一个源文件的纯 Python 包,`instance` 在顶,`text` 在底,依赖只朝下。

<!-- figure:begin 40-actio-architecture -->
![Actio 的模块组成](diagrams/40-actio-architecture.svg)

**图 2　Actio 的模块组成**　instance 在顶层编排技能路由、推理经验、记忆检索与轨迹组装。技能与经验共用触发谓词语言,谓词语言从信号注册表取信号,信号与记忆共用一处词面切分。箭头指向被导入的模块。errors 与 _check 被各模块导入,不画线。

<details>
<summary>文字版:图中的节点与连线</summary>

**节点**

- instance.py:instantiate · ActioInstance.respond / decide / GuidedBackend · IgnoreGuidance · render_guidance
- skills.py:Skill · route · check_library / 排序键 (-priority, -version, id)
- experience.py:ReasoningExperience / activate · select_tier 三级回退
- memory.py:MemoryEntry · snapshot / retrieve 词面重叠检索
- trace.py:build_trace · validate_trace / normalize_library_version
- triggers.py:五个算子 · 校验 · 求值 / referenced_signals
- signals.py:SignalRegistry / default_registry · 三个内置信号
- errors.py · _check.py:ActioDataError · 字段校验 / 被各模块导入
- text.py:tokens:ASCII 整词 / 汉字相邻二字切分
- simulator.datamodel:TurnRecord · Persona / Scenario · 唯一的包外导入

**连线**

- instance.py → skills.py
- instance.py → experience.py
- instance.py → memory.py
- instance.py → trace.py
- skills.py → triggers.py
- experience.py → triggers.py
- triggers.py → signals.py
- signals.py → text.py
- memory.py → text.py
- signals.py → simulator.datamodel:TurnRecord

</details>
<!-- figure:end 40-actio-architecture -->

读图说明:

- `instance.py` 编排一次响应,它导入技能、经验、记忆、轨迹四个模块。
- 技能与经验共用 `triggers.py` 的谓词语言,谓词里的信号来自 `signals.py`。
- `signals.py` 与 `memory.py` 都用 `text.py` 切词面。
- `errors.py` 与 `_check.py` 被各模块导入,图里不画连线;唯一的包外依赖 `simulator.datamodel` 已落地。`signals`、`triggers`、`skills`、`experience`、`instance` 五个模块都导入它,图里只画了 `signals` 一条,其余从略。

### 3.1 模块表

| 模块 | 职责 | 依赖 | 状态 |
|---|---|---|---|
| `actio/errors.py` | `ActioDataError`,本包数据错误的基类 | 无 | [已落地] |
| `actio/_check.py` | 构造与 `from_dict` 共用的字段校验 | `errors` | [已落地] |
| `actio/text.py` | 词面切分 `tokens` | 标准库 `re` | [已落地] |
| `actio/signals.py` | `SignalRegistry` 与三个内置信号 | `text`、`simulator.datamodel` | [已落地] |
| `actio/triggers.py` | 五算子谓词语言:校验、求值、`referenced_signals` | `signals`、`simulator.datamodel` | [已落地] |
| `actio/skills.py` | `Skill`、`route`、`check_library` | `triggers`、`_check`、`simulator.datamodel` | [已落地] |
| `actio/memory.py` | `MemoryEntry`、`snapshot`、`retrieve` | `text`、`_check` | [已落地] |
| `actio/experience.py` | `ReasoningExperience`、三级回退 | `triggers`、`_check`、`simulator.datamodel` | [已落地] |
| `actio/trace.py` | 轨迹模式:`build_trace`、`validate_trace` | `errors` | [已落地] |
| `actio/instance.py` | `instantiate`、`ActioInstance`、后端协议与适配器 | 以上全部、`simulator.datamodel` | [已落地] |

### 3.2 协议改动涉及的已落地文件

| 文件 | 现状 | 改动 | 状态 |
|---|---|---|---|
| `src/simulator/realtime/proto/realtime_env.proto` | `SubmitTurn` 有 `utterance`、`inner_thought` 与可选的 `Struct activation_trace = 3` | 加 `optional google.protobuf.Struct activation_trace = 3` | [已落地] |
| `src/simulator/runner.py` | 115 行,`_unpack_reply` 接受二元组或三元组,轨迹深拷贝进 `TurnRecord` | 新增 `_unpack_reply` 接受二元组或三元组 | [已落地] |
| `src/simulator/realtime/backend.py`、`driver.py`、`client.py` | 73、168、256 行,提交入口都收 keyword-only 的 `activation_trace` | 各加 keyword-only 的 `activation_trace` | [已落地] |
| `src/simulator/realtime/servicer.py` | 344 行,`_drain_ready_events` 已只抹对手轮次;`submit_turn` 分支按 HasField 解出轨迹 | `submit_turn` 分支解出轨迹;回显规则不改 | [已落地] |
| `src/simulator/realtime/codec.py` | 编码时先 SetInParent 再写入,保留空轨迹;解码用 MessageToDict 转回 dict | 不改 | [已落地] |

三个生成文件 `realtime_env_pb2.py`、`realtime_env_pb2.pyi`、`realtime_env_pb2_grpc.py` 只由 `scripts/gen_realtime_proto.sh` 产生,不手改。

### 3.3 依赖方向规则

| 包 | 可以导入 | 不得导入 |
|---|---|---|
| `actio` [已落地] | 自身、`simulator.datamodel`、标准库 `__future__`、`copy`、`dataclasses`、`re`、`typing` | `controlplane`、`simulator.backends`、`simulator.realtime`、任何第三方包 |
| `simulator` | 不变 | `actio`(运行器按结构调用后端,不导入) |

## 4. 数据模型与状态机

Actio 管四类实体:技能、记忆条目、推理经验、激活轨迹。前三者是冻结数据类,后者是纯字典。

### 4.1 实体表

| 实体 | 字段与含义 | 约束 |
|---|---|---|
| `Skill` [已落地] | `id`、`version`、`level`(`macro` 宏观 / `micro` 微观)、`priority`、`trigger` 触发条件、`policy_instructions` 指令、`description` | id 以 `skill-` 开头且前缀后非空;`version` 是不小于 1 的整数;`priority` 默认 0,越大越优先 |
| `MemoryEntry` [已落地] | `id`、`version`、`subject` 主体、`content`、`timestamp`、`confidence`、`source_episode`、`keywords` | id 以 `mem-` 开头;时间戳严格为 `YYYY-MM-DDTHH:MM:SSZ`;置信度在 [0, 1],整数转 `float` |
| `ReasoningExperience` [已落地] | `id`、`version`、`pattern`、`applies_when`、三级 `primary`、`secondary`、`fallback` | `fallback.when` 必须为 `None`,另两级必须有触发条件;id 不受前缀约束 |
| `Tier` [已落地] | `when` 触发条件、`instructions` | 指令是非空字符串元组 |
| 触发条件 | 恰好一个键的字典 | 算子只有 `signal`、`all`、`any`、`not`、`turn_at_least` 五种 |
| 激活轨迹 | 八个键,形状见 4.3 | 叶子没有数字 |
| `RouteResult` [已落地] | `candidates`(技能 id 元组,已按路由次序排好)、`selected`(`Skill` 或空)、`signals`;`conflict` 是由 `candidates` 数量派生的布尔属性,不是构造字段,与激活轨迹里的同名字典键不同 | `selected` 恒为 `candidates[0]` |
| `ExperienceActivation` [已落地] | `experience_id`、`tier`、`instructions` | `tier` 取 `primary`、`secondary`、`fallback` 之一 |

所有数据类 `frozen=True`,校验放在 `__post_init__`。字符串序列字段只接受 `tuple`,拒绝 `str`,否则一个字符串会被逐字当成多条指令。整数字段拒绝 `bool` 与浮点数。

### 4.2 触发条件的谓词语言

| 算子 | 写法 | 为真的条件 |
|---|---|---|
| `signal` | `{"signal": "emotion_high"}` | 该已注册信号此刻为真 |
| `all` | `{"all": [节点, ...]}`,非空 | 全部子节点为真 |
| `any` | `{"any": [节点, ...]}`,非空 | 任一子节点为真 |
| `not` | `{"not": 节点}` | 子节点为假 |
| `turn_at_least` | `{"turn_at_least": n}`,n 为非负整数 | `len(history) >= n` |

三个内置信号的签名都是 `(history, speaker_slot) -> bool`,只看对方说的话:

| 信号 | 为真的条件 |
|---|---|
| `emotion_high` | 对方最后一句含情绪词典任一词,或含两个及以上感叹号(半角或全角) |
| `no_new_information` | 对方至少说过两句,且最后一句的词面全都在它此前的话里出现过 |
| `first_turn` | 说话者此前一句都没说过 |

词面切分 `tokens` 只有一处定义:ASCII 词按小写整词;连续汉字按相邻二字切分;单字汉字串保留单字;标点丢弃。

### 4.3 激活轨迹模式

轨迹的 `schema` 固定为字符串 actio.trace.v1,键集合恰好八个,不多不少。

```yaml
schema: actio.trace.v1
signals: [emotion_high, first_turn]      # 此刻为真的信号名,严格升序
candidates: [skill-calm, skill-firm]     # 命中的技能,按路由次序
selected: skill-calm                     # == candidates[0];无候选时为 null
conflict:                                # 候选多于一个时为字典,否则为 null
  skills: [skill-calm, skill-firm]
  rule: priority_desc,version_desc,id_asc
memory_hits: [mem-temper]                # 检索到的记忆 id
experience: null                         # 或 {id, tier}
library_version: {skill: "4", memory: mem-lib-1}  # 两个非空字符串
```

| 规则 | 内容 |
|---|---|
| 无数字 | 叶子只有 `str`、`None`、`bool`;`bool` 目前没有用到,预留 |
| 不重复 | 三个列表都不得有重复项 |
| 前缀 | `candidates` 每项以 `skill-` 开头,`memory_hits` 每项以 `mem-` 开头 |
| 自洽 | `selected == candidates[0]`;`conflict.skills == candidates` |
| 没有轨迹 | `TurnRecord.activation_trace is None`:裸模型、对手轮次、对手的回显 |
| 空轨迹 | Actio 的每一轮都有完整轨迹;一个技能都没命中时 `candidates == []`、`selected is None` |

### 4.4 产物与路径

| 产物类型 | 路径 | 状态机种类 | 写方 | 状态 |
|---|---|---|---|---|
| 激活轨迹 | episode 里每个 `TurnRecord` 的 `activation_trace` 字段 | 无,随 episode 一次写入 | SocialSimulator 的运行器 | [已落地] |
| 技能条目 | `skill_library/` 下的 `skill` 产物 | 由 MindAgent 管 | MindAgent 收录处理器 | [已落地] |
| 记忆条目 | `memory_library/` 下的 `memory` 产物 | 由 MindAgent 管 | MindAgent 收录处理器 | [已落地] |

Actio 自身不写任何产物。技能与记忆的 `to_dict()` 原样作为产物的 `payload`。

### 4.5 状态机

无,所以本文没有状态机图。Actio 实例是冻结的:组装后技能、记忆、经验、库版本都不变,换库就重新 `instantiate`。记忆的"版本演进"由快照规则表达,不是状态机:每个 id 取 `timestamp <= at` 的最高版本。

### 4.6 不变量

1. 同一实例、同一输入,`decide` [已落地] 的结果恒定,且与技能输入顺序无关。
2. 快照检测损坏(同 id 同版本内容不同)先于 `at` 过滤,所以与输入顺序无关。
3. 后端拿到的是 history 的深拷贝,改它不影响调用方。
4. 运行器写进 `TurnRecord` 的轨迹是深拷贝,后端事后改自己的字典不污染 episode。
5. 轨迹只能由 Actio 产生:带指导语的后端返回三元组时抛 `TypeError`。

## 5. 接口契约

本章的入口全部在主干上,签名以代码为准。

### 5.1 入口签名

```python
# actio.instance
def instantiate(model_backend, *, skills, memories, library_version, profile,
                experiences=(), registry=None, memory_limit=3) -> ActioInstance: ...

class ActioInstance:
    def respond(self, persona, scenario, history, speaker_slot
                ) -> tuple[str, str | None, dict]: ...
    def decide(self, history, speaker_slot
               ) -> tuple[RouteResult, ExperienceActivation | None, list[MemoryEntry]]: ...

class GuidedBackend(Protocol):
    def respond_guided(self, persona, scenario, history, speaker_slot,
                       guidance: list[str]) -> tuple[str, str | None]: ...
```

| 入口 | 行为 | 状态 |
|---|---|---|
| `instantiate` | 校验技能库、记忆与经验类型、经验 id 不重复、信号已注册;库版本规范成字符串;`profile` 深拷贝 | [已落地] |
| `ActioInstance.respond` | 七步流程,返回三元组 | [已落地] |
| `ActioInstance.decide` | 只做路由、经验、记忆,不调后端 | [已落地] |
| `as_guided` | 有 `respond_guided` 原样返回;只有 `respond` 套 `IgnoreGuidance` [已落地];都没有抛 `TypeError` | [已落地] |
| `render_guidance` | 固定顺序"技能 → 经验 → 记忆",每行带来源前缀 | [已落地] |
| `build_trace`、`validate_trace` | 组装后立即校验;MindAgent 读落盘轨迹时再校验一次 | [已落地] |
| `normalize_library_version` | 整数库版本转十进制字符串,`3 → "3"` | [已落地] |
| `route`、`retrieve`、`snapshot`、`activate` | 纯函数,供 MindAgent 离线重放 | [已落地] |

对外名字:

| 模块 | 名字 |
|---|---|
| `actio.errors` | `ActioDataError`,本包所有数据错误的基类 [已落地] |
| `actio.signals` | `SignalRegistry`(`register`、`get`、`names`、`fired`)、`default_registry`、`DEFAULT_EMOTION_LEXICON`、`make_emotion_high`、`no_new_information`、`first_turn` [已落地] |
| `actio.triggers` | `InvalidTriggerError`、`validate_trigger`、`evaluate_trigger`、`referenced_signals`(只计偶数层 `not` 之下的信号,所以 `{"not": {"signal": X}}` 不算针对 X,分析器据此判断技能是否针对某信号) [已落地] |
| `actio.skills` | `SKILL_ID_PREFIX`、`Skill`、`RouteResult`、`route_order`、`check_library`、`route` [已落地] |
| `actio.memory` | `MEMORY_ID_PREFIX`、`MemoryEntry`、`snapshot`、`overlap`、`retrieve` [已落地] |
| `actio.experience` | `ReasoningExperience`、`Tier`、`ExperienceActivation`、`select_tier`、`activate` [已落地] |
| `actio.trace` | `TRACE_SCHEMA`、`TIE_BREAK_RULE`、`TRACE_KEYS`、`build_trace`、`validate_trace`、`normalize_library_version` [已落地] |
| `simulator.realtime.client` | `EnvironmentServiceClient.submit_turn(utterance, inner_thought=None, *, activation_trace=None)` [已落地] |

指导语每行的前缀:

| 来源 | 前缀格式 |
|---|---|
| 技能 | `[skill:<id>] <指令>` |
| 推理经验 | `[experience:<id>/<tier>] <指令>` |
| 记忆 | `[memory:<id>] <subject>: <content>` |

### 5.2 协议改动的接口

| 接口 | 改动 | 状态 |
|---|---|---|
| `SubmitTurn.activation_trace` | 新增可选 `Struct` 字段,编号 3 | [已落地] |
| `EnvironmentServiceClient.submit_turn` | 加 `*, activation_trace=None`;非 `None` 时先 SetInParent 再 update | [已落地] |
| `SessionDriver.submit_turn` | 加 `*, activation_trace=None`,原样转交 | [已落地] |
| `ClientDrivenBackend.submit` | 加 `*, activation_trace=None`;`respond` 带轨迹返回三元组,不带仍返回二元组 | [已落地] |
| `run_episode` | 签名不变;后端可返回长度 2 或 3 的 `tuple` 或 `list`,其余抛 `TypeError`;第三项必须是字典或空,写进 `TurnRecord` 的是深拷贝;协议层不对轨迹做数字规范化,取回什么存什么 | [已落地] |

### 5.3 产物读写约定

Actio 不读写产物树。技能、记忆、推理经验的 `to_dict()` 与 `from_dict()` [已落地] 是 MindAgent 写产物时的唯一形状。

```yaml
# Skill.to_dict()
id: skill-calm
version: 2
level: macro
priority: 5                  # 可省略,默认 0
trigger: {all: [{signal: emotion_high}, {not: {signal: first_turn}}]}
policy: {instructions: [先回应对方的情绪, 再谈事情本身]}
description: 情绪优先         # 可省略,默认 ""
```

```yaml
# MemoryEntry.to_dict()
id: mem-budget                       # 以 mem- 开头,前缀后非空
version: 2                           # 整数,不小于 1;同一 id 可有多个版本
subject: pb                          # 非空字符串
content: 对方担心预算超支
keywords: [预算, 成本]                # 可省略,默认空列表
timestamp: "2026-10-01T00:00:00Z"    # 严格的 YYYY-MM-DDTHH:MM:SSZ
source_episode: ep-sess-1            # 非空字符串或空,可省略,默认空
confidence: 0.8                      # 0 到 1 的数;整数会被转成浮点
```

```yaml
# ReasoningExperience.to_dict()
id: exp-deescalate
version: 1
pattern: 情绪高时先降温
applies_when: {turn_at_least: 1}
tiers:
  primary:   {when: {signal: emotion_high}, instructions: [先共情]}
  secondary: {when: {signal: no_new_information}, instructions: [换个角度提问]}
  fallback:  {when: null, instructions: [复述对方的要点]}   # 兜底层的 when 必须为空
```

技能字典里的指令放在 `policy.instructions` 下。三种字典共用的规则:id 前缀大小写敏感,前缀之后必须还有内容;整数字段排除布尔与浮点。

`from_dict` 严格:缺键或多一个键都抛 `ActioDataError` [已落地],信息指出是哪个键。YAML 的 `list` 在 `from_dict` 里换成 `tuple`。

### 5.4 触发条件

| 调用 | 谁触发 | 时机 |
|---|---|---|
| `instantiate` | MindAgent 部署驱动器 | 每个部署轮次开始时,按绑定的库版本读好条目后 |
| `respond` | MindAgent 客户端循环,或 SocialSimulator 运行器 | 轮到实例所占角色位说话时 |
| `validate_trace` | MindAgent 经验分析器 | 读落盘 episode,对每个 `activation_trace is not None` 的轮次 |

### 5.5 对邻居的假设

| 邻居 | 本文依赖它什么 | 它依赖本文什么 |
|---|---|---|
| `simulator.datamodel` | `Persona`、`Scenario`、`TurnRecord`(`activation_trace: dict \| None` 已存在) | 无 |
| SocialSimulator 运行器 | 按 `ModelBackend` 的参数调用 `respond`;接受三元组 | `respond` 返回三元组,第三项是 `dict` 或 `None` |
| SocialSimulator 实时环境服务 | 不对轨迹做数字规范化;对手轮次的轨迹与内心推理不下行 | 轨迹没有数字,往返后相等 |
| MindAgent | 按库版本读好条目再传入;每轮带轨迹调用 `submit_turn` | 轨迹模式、条目字典形状、id 前缀、`normalize_library_version`;Actio 只要求库版本是非空字符串,但 MindAgent 的收录要按整数加一,只接受十进制数字串 |
| 模型后端 | 实现 `respond_guided`,或只实现 `respond` | 指导语列表与 history 深拷贝 |

## 6. 关键流程

两条主流程:一次 `respond` 的内部七步,与一条轨迹从客户端到落盘的九步。

### 6.1 一次 respond

<!-- figure:begin 40-actio-respond -->
![一次 respond 的内部流程](diagrams/40-actio-respond.svg)

**图 3　一次 respond 的内部流程**　前三步是纯函数 decide:求信号并路由技能、激活推理经验、检索记忆。第四步按固定顺序渲染指导语,第五步把 history 副本与指导语交给后端,第六步组装并校验激活轨迹,第七步返回三元组。后端返回值不合规时抛 TypeError。

<details>
<summary>文字版:图中的节点与连线</summary>

**节点**

- 调用方:respond(persona, scenario, / history, speaker_slot)
- 信号与技能路由:registry.fired 求信号 / route 排序后选第一个
- 推理经验:activate:applies_when / 主 → 次 → 保底
- 记忆检索:查询词 = 对方最后一句 / retrieve 取前 memory_limit 条
- 渲染指导语:技能 → 经验 → 记忆 / 每行带来源前缀
- 后端 respond_guided:传 history 深拷贝 / 必须返回二元组
- 组装激活轨迹:build_trace 组装 / 随即 validate_trace
- 返回三元组:(utterance, / inner_thought, trace)
- TypeError:后端返回值不是二元组

**连线**

- 调用方 → 信号与技能路由
- 信号与技能路由 → 推理经验
- 推理经验 → 记忆检索
- 记忆检索 → 渲染指导语
- 渲染指导语 → 后端 respond_guided
- 后端 respond_guided → 组装激活轨迹
- 组装激活轨迹 → 返回三元组
- 后端 respond_guided → TypeError:不合规

</details>
<!-- figure:end 40-actio-respond -->

| 步 | 动作 | 产出 |
|---|---|---|
| 1 | 信号与技能路由:`registry.fired` 求此刻为真的信号;`route` 对命中的技能按 `(-priority, -version, id)` 排序,取第一个 | `RouteResult` [已落地] |
| 2 | 推理经验:`activate` 按 `applies_when` 筛出适用经验,取 `(版本降序, id)` 第一条,再按主 → 次 → 保底选一级 | `ExperienceActivation` [已落地] 或 `None` |
| 3 | 记忆检索:查询词是对方最后一句;`retrieve` 先快照,再按词面重叠打分,丢掉 0 分,取前 `memory_limit` 条 | 记忆条目列表 |
| 4 | 渲染指导语:`render_guidance` 按技能 → 经验 → 记忆的顺序拼行,每行带来源前缀 | 指导语列表 |
| 5 | 后端 `respond_guided`:传 history 深拷贝与指导语 | 二元组 |
| 6 | 组装激活轨迹:`build_trace` 组装,随即 `validate_trace` | 八键轨迹 |
| 7 | 返回 `(utterance, inner_thought, trace)` | 三元组 |

第 1 至 3 步就是 `decide` [已落地],不调后端。第 5 步后端返回值不是二元组时抛 `TypeError`,图中的红色分支即此。

### 6.2 激活轨迹经协议落盘

<!-- figure:begin 40-actio-trace-path -->
![激活轨迹如何经协议落盘](diagrams/40-actio-trace-path.svg)

**图 4　激活轨迹如何经协议落盘**　客户端把 respond 产出的轨迹放进 SubmitTurn 的 Struct 字段。服务端解出字典,经驱动器与客户端驱动后端交给 runner,runner 写进 TurnRecord,会话结束时整份 episode 落盘。逐轮回显时只抹掉对手轮次的轨迹与内心推理,客户端自己的轮次原样回显。

<details>
<summary>文字版:图中的节点与连线</summary>

**节点**

- ActioInstance.respond:返回三元组 / 轨迹叶子没有数字
- client.submit_turn:activation_trace 关键字参数 / SetInParent 后 update
- 客户端本地历史:自己的轮次带轨迹 / 下一轮作 history
- EnvironmentServicer:HasField 才 MessageToDict / 得 dict,否则传 None
- SessionDriver:submit_turn 原样转交轨迹
- ClientDrivenBackend:带轨迹时返回三元组 / 不带时仍是二元组
- run_episode:_unpack_reply 收二元组或三元组 / 深拷贝写进 TurnRecord
- episode 落盘:会话结束时写入完整记录 / 对手轮次的轨迹也在
- _drain_ready_events:逐轮事件:对手轮次抹掉两项 / 自己的轮次原样回显

**连线**

- ActioInstance.respond → client.submit_turn
- client.submit_turn → EnvironmentServicer:SubmitTurn.activation_trace
- EnvironmentServicer → SessionDriver
- SessionDriver → ClientDrivenBackend
- ClientDrivenBackend → run_episode:三元组
- run_episode → episode 落盘
- run_episode → _drain_ready_events
- _drain_ready_events → 客户端本地历史:TurnUpdate

</details>
<!-- figure:end 40-actio-trace-path -->

| 步 | 位置 | 动作 | 状态 |
|---|---|---|---|
| 1 | 客户端进程 | `ActioInstance.respond` 返回三元组,轨迹叶子没有数字 | [已落地] |
| 2 | 客户端进程 | `client.submit_turn` 收 keyword-only 的轨迹,SetInParent 后 update 进 `SubmitTurn.activation_trace` | [已落地] |
| 3 | 服务进程 | `EnvironmentServicer` 在 HasField 为真时用 MessageToDict 得 dict,否则传 `None` | [已落地] |
| 4 | 服务进程 | `SessionDriver.submit_turn` 原样转交轨迹 | [已落地] |
| 5 | 服务进程 | `ClientDrivenBackend` 带轨迹时返回三元组,不带时仍是二元组 | [已落地] |
| 6 | 服务进程 | `run_episode` 的 `_unpack_reply` 收二元组或三元组,深拷贝写进 `TurnRecord` | [已落地] |
| 7 | 服务进程 | 会话结束时,驱动器的完整记录落盘为 episode,对手轮次的轨迹也在 | [已落地] |
| 8 | 服务进程 | `_drain_ready_events` 逐轮下发事件:对手轮次抹掉轨迹与内心推理,自己的轮次原样回显 | [已落地] |
| 9 | 客户端进程 | 客户端本地历史收到 `TurnUpdate`,自己的轮次带轨迹,下一轮作 history | [已落地] |

第 8 步的抹除规则已在代码里,本工作包不改,只用测试锁住。服务端落盘来自驱动器自己的记录,不受抹除影响。

### 6.3 失败与恢复路径

| 失败点 | 表现 | 恢复 |
|---|---|---|
| 条目字段不合规 | 构造或 `from_dict` 抛 `ActioDataError` [已落地],指出字段 | 改条目;收录前就会暴露 |
| 技能引用未注册信号 | `instantiate` 抛错 | 传入含该信号的注册表,或改技能 |
| 带指导语的后端返回三元组或其他形状 | 第 5 步抛 `TypeError` | 修后端 |
| 运行器收到长度不是 2 或 3、或第三项不是 `dict` 或 `None` 的返回值 | 抛 `TypeError`,信息分别含 `2- or 3-tuple`、`activation_trace` | 修后端 |

Actio 内部没有重试。它的失败全是确定性的,向上抛给调用方。

### 6.4 走查例子

场景取自工作包 6 端到端测试:对手 `b` 先说,Actio 扮演 `a` 后说,共 4 轮,对手台词依次是"我真的很生气!!"与"好吧"。

实例的输入:

| 项 | 值 |
|---|---|
| 技能 `skill-calm` | 版本 2,`micro`,优先级 1,触发 `{"signal": "emotion_high"}`,指令"先回应对方的情绪" |
| 技能 `skill-firm` | 版本 1,`macro`,优先级 1,同一触发条件,指令"坚持立场" |
| 记忆 `mem-temper` | 主体 `pb`,内容"对方一急就会生气",置信度 0.9,关键词"生气" |
| 库版本 | `{"skill": 4, "memory": "mem-lib-1"}` |
| 后端 | 罐头后端,话语是指导语用 `\|` 拼接 |

第 1 轮 Actio 说话时的七步:

| 步 | 计算 | 结果 |
|---|---|---|
| 1 | 对方最后一句是「我真的很生气!!」,含情绪词「生气」与两个半角感叹号,`emotion_high` 为真;两条技能都命中 | 排序键 `skill-calm` 为 `(-1, -2, skill-calm)`,`skill-firm` 为 `(-1, -1, skill-firm)` |
| 1 | 同优先级比版本,版本 2 先于版本 1 | `candidates = [skill-calm, skill-firm]`,`selected = skill-calm`,写入 `conflict` |
| 2 | 没有推理经验 | `experience = null` |
| 3 | 查询词面:"我真""真的""的很""很生""生气" | 与记忆共有的只有"生气",重叠数 1 |
| 3 | 重叠数大于 0 | `memory_hits = [mem-temper]` |
| 4 | 渲染 | 两行:`[skill:skill-calm] 先回应对方的情绪`、`[memory:mem-temper] pb: 对方一急就会生气` |
| 6 | 库版本整数 4 规范成字符串 | `library_version = {"skill": "4", "memory": "mem-lib-1"}` |

第 3 轮(`turn_index` 3,第二条客户端轨迹)前对方说"好吧",不触发 `emotion_high`,`selected` 为 `null`;第 2 轮是对手轮,没有客户端轨迹。换成空技能库时,第 1 轮话语只剩记忆那一行,证明输出随技能改变。

同一个测试还走完 6.2 的落盘半程:服务端落盘的 episode 里,对手轮的轨迹是 `[None, None]`,客户端轮的轨迹经 gRPC 往返后与提交时逐字段相等、类型相同,客户端本地重建的 episode 与服务端落盘的一致。

轨迹不放数字的原因可以实测:`Struct.update({"skill": 3})` 经 MessageToDict 取回是 `{"skill": 3.0}`。若库版本保持整数,往返后 `4` 变成 `4.0`,与客户端提交的值类型不同。

`SubmitTurn` 现在有 `utterance = 1`、`inner_thought = 2` 与可选的 `activation_trace = 3`;轨迹取 3 号字段,因为前两个编号已被占用。

## 7. 关键决策

| 编号 | 决策 | 备选 | 理由 | 判错的代价 |
|---|---|---|---|---|
| R12 | 打通激活轨迹:`SubmitTurn` 加可选字段,运行器接受后端返回三元组 | 轨迹走单独的产物通道 | 轨迹与轮次一起走,落盘的 episode 自带证据 | 改协议,重新生成已入库的 pb2 文件 |
| AC-1 | 轨迹里不放任何数字,库版本改成字符串 | 协议层把 `3.0` 规范回 `3` | Struct 只有 double;规范化会误伤真正的浮点数 `1.0` | 非 Actio 客户端带数字的轨迹,落盘为浮点数 |
| AC-2 | 轨迹加 `schema`、`signals`、`conflict` 三键,`experience` 记 `{id, tier}` | 只记候选与选中 | 能看出按什么规则裁决;分析器不必重放信号;三级回退落在哪级是经验唯一有信息量的输出 | 模式多三个键,消费方用 `validate_trace` 一次校验 |
| AC-3 | `selected` 恒等于 `candidates[0]` | 允许"命中但不选" | 选中谁与为什么在数据上自洽 | 将来引入冷却等机制要先升 `schema` 版本 |
| AC-4 | 路由排序键 `(-priority, -version, id)`,优先级是整数、越大越优先 | 只按版本 | 人工可显式压过新版本;最后按 id 保证确定 | 无 |
| AC-5 | 词面切分只有一处:ASCII 整词、汉字二字切分 | 引入分词库 | 无新依赖,V1 的检索与"无新信息"判断够用 | 近义表达检不到 |
| AC-6 | 记忆查询词是对方最后一句,`limit` 默认 3,0 分不返回 | 全部历史作查询 | 与当前话题相关;指导语长度可控 | 无关的高置信记忆检不出 |
| AC-7 | 信号注册表是实例,`default_registry()` [已落地] 每次新建 | 全局注册表 | 并发会话不共享可变状态 | 追加信号要把注册表传进 `instantiate` |
| AC-8 | 指导语固定"技能 → 经验 → 记忆",每行带来源前缀 | 无前缀纯文本 | 罐头后端回显即可断言;人工看 episode 知道哪句受哪条影响 | 真实模型的提示里出现前缀,去掉只改 `render_guidance` |
| AC-9 | 带指导语的后端只能返回二元组 | 允许后端附加轨迹 | 轨迹只能由 Actio 产生,后端不得冒充 | 无 |
| AC-10 | 数据类冻结,校验在 `__post_init__`,`from_dict` 只查键 | 宽松解析 | 技能库与记忆库是审核过的产物,坏数据不能悄悄进库 | 手写 YAML 多一个键就被拒 |
| AC-11 | id 前缀 `skill-`、`mem-` 强制 | 不约束 | id 直接作产物文件名,不带前缀会落到库目录规则之外 | 无 |
| AC-12 | 回显规则不改代码,测试锁住 | 在 Actio 一侧再过滤 | 现有 `_drain_ready_events` 已是"对手抹掉、自己原样" | 无 |

## 8. 失败处理与可观测

Actio 的失败全部是 T2 确定性:同样输入必然再失败,不重放。

| 失败点 | 类 | 处理 | 留痕产物 |
|---|---|---|---|
| 条目构造或 `from_dict` 不合规 | T2 | 抛 `ActioDataError` [已落地],信息含 id 与字段 | 无;由调用方 MindAgent 记失败 |
| `instantiate` 校验失败 | T2 | 抛 `ActioDataError` [已落地] | 同上 |
| 后端返回值形状不对 | T2 | 抛 `TypeError` | 同上 |
| 轨迹不合模式 | T2 | `build_trace` 内的 `validate_trace` 抛错 | 同上 |
| 后端超时、网络错误、实时会话断开 | 不在 Actio | 原样上抛,由调用方分类 | 由调用方决定 |

只读观测手段:

| 手段 | 能看到什么 |
|---|---|
| 读落盘 episode 的 `activation_trace` | 每轮命中的信号、候选技能、选中技能、冲突规则、记忆、经验级别、库版本 |
| 读 episode 的话语 | 指导语前缀表明哪句受了哪条技能或记忆影响(仅当后端保留前缀) |
| `ActioInstance.decide` | 离线重放:不调后端,算出某段 history 下的选择 |
| `validate_trace` | 判断一条落盘轨迹是否合模式 |

## 9. 测试策略

全部测试脱离真实环境:罐头后端替代模型,gRPC 用 `127.0.0.1` 上 `port=0` 的真实 loopback 服务端,落盘用 `tmp_path` 隔离的 `GitStore`。不依赖 GPU、外部网络、真实模型与外部命令行强模型。

| 层次 | 对象 | 替身 | 测试文件 | 用例数 | 状态 |
|---|---|---|---|---|---|
| 协议 | `SubmitTurn.activation_trace` 序列化往返 | 无 | `tests/test_realtime_proto_submit_trace.py` | 5 | [已落地] |
| 运行器 | 收三元组、深拷贝、拒绝坏形状 | 返回三元组的测试后端 | `tests/test_runner_activation_trace.py` | 10 | [已落地] |
| 实时链路 | 后端、驱动器、loopback 往返、回显规则 | `ScriptedBackend` | `tests/test_realtime_activation_trace.py` | 11 | [已落地] |
| 信号与谓词 | `tokens`、三个内置信号、五个算子 | 无 | `tests/test_actio_signals.py` | 38 | [已落地] |
| 技能与路由 | `Skill` 校验、`route`、`check_library` | 无 | `tests/test_actio_skills.py` | 39 | [已落地] |
| 记忆与经验 | 快照、检索、三级回退 | 无 | `tests/test_actio_memory_experience.py` | 50 | [已落地] |
| 实例 | 轨迹模式、`respond`、依赖边界与 `__future__` 静态检查 | 回显指导语的罐头后端 | `tests/test_actio_instance.py` | 30 | [已落地] |
| 端到端 | Actio 作客户端经真实 gRPC 跑会话并落盘 | `ScriptedBackend` 作对手 | `tests/test_actio_realtime_e2e.py` | 2 | [已落地] |

合计 185 个用例,按上表的文件逐个实测照数。既有测试一行不改;`tests/test_mt_packaging.py` 要求新包同时加进 `pyproject.toml` 的 wheel 包列表。

## 10. 现状、局限与未核实项

`src/actio` 十一个源文件全部在主干上,协议一侧的 `SubmitTurn.activation_trace`、客户端与驱动器的参数、运行器的三元组解包也已落地;第 9 章列出的 185 个用例在 `b826440` 上实测照数。装配位置是 MindAgent 部署驱动器的轮次处理器,它经 `factory` 的 `agent.yaml` 取罐头后端 `CannedGuidedBackend`;真实模型后端还没有接上,真实模型是否遵循指导语仍未核实,见 10.3。

### 10.1 已落地与计划中

| 能力 | 状态 | 说明 |
|---|---|---|
| `TurnRecord.activation_trace` 字段与 proto 编解码 | [已落地] | `dict \| None`;`codec` 已区分 `None` 与 `{}` |
| 对手轮次抹除规则 | [已落地] | `_drain_ready_events` 与运行器的 history 同一规则 |
| `SubmitTurn.activation_trace` | [已落地] | 可选 `Struct`,字段编号 3;HasField 区分空轨迹与没有轨迹 |
| 客户端、驱动器、客户端驱动后端带轨迹 | [已落地] | keyword-only 参数 |
| `actio` 包全部模块 | [已落地] | 十一个源文件,含 `__init__.py` |

### 10.2 已知局限

| 局限 | 影响 | 缓解 |
|---|---|---|
| 信号词典、阈值、检索参数无数据支持;`emotion_high` 是子串匹配,"不生气"也命中 | 命中率不可知 | 词典与注册表可按实例替换 |
| 二字切分检不到近义表达 | 相关记忆漏检 | V2 换语义检索 |
| 记忆检索不按主体过滤 | 别人的记忆会被检出 | 主体与 persona 的对应约定后再传 `subjects` |
| 轨迹只记录路由决定 | `IgnoreGuidance` [已落地] 包装的后端忽略指导语,轨迹照写 `selected` | 分析器把轨迹当"决定",不当"行为" |
| InteractTrain 按二元组解包 | Actio 实例不能直接当训练策略 | 改 `trajectory_source.py` 两行即可接 |
| 重新生成依赖锁定的 `grpcio-tools 1.84.0` | 升级会改生成物版本头 | 升级时先做不改 proto 的重新生成并比对 |

### 10.3 未核实项

| 项 | 影响 | 核实办法 |
|---|---|---|
| 真实模型是否遵循指导语 | 技能对真实输出的效果未知 | 真实后端上对比有无技能的输出 |
| 轨迹模式是否满足 MindAgent 经验分析器 | 分析器施工时可能要加键 | MindAgent 工作包施工时用 `validate_trace` 读真实 episode |
| 其他机器上重新生成 pb2 是否逐字节一致 | 生成物出现无关差异 | 不改 proto 时重新生成并 `diff` |

### 10.4 明确不做

Actio 不读写产物树,不决定用哪个库版本,不对轨迹做数字规范化,不重试后端调用,不改 MindTrainer。

## 附 本文术语

| 术语 | 含义 |
|---|---|
| 技能 | 带触发条件、优先级、版本与指令的条目;命中且排第一时其指令进入指导语 |
| 推理经验 | 带主、次、保底三级指令的条目,三级回退按触发条件逐级尝试 |
| 显式记忆 | 带主体、内容、关键词、时间戳与置信度的条目,按词面重叠检索 |
| 信号 | 注册表里的纯函数 `(history, speaker_slot) -> bool` |
| 触发条件 | 五个算子组成的谓词,引用信号或轮次数 |
| 指导语 | Actio 渲染给模型后端的文本行列表,每行带来源前缀 |
| 冲突 | 命中的技能多于一个;轨迹写明裁决规则 |
| 快照 | 某一时刻每个记忆 id 的最高可见版本 |
| 罐头后端 | 测试用的固定输出后端,回显指导语以证明技能改变输出 |
