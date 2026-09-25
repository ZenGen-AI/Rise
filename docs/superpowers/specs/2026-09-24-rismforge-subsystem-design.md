# RismForge:七个子系统详细设计

- **状态**:待用户审阅
- **日期**:2026-09-24
- **前置文档**:`2026-09-22-rismforge-architecture-design.md`(总体架构)
- **范围**:七个能力子系统的内部模块划分、数据模型、模块间协作方式。不包含字段级schema、算法伪代码、任何代码实现。

---

## 补充:部署拓扑

七个子系统按是否需要 GPU 计算,分为两层:

| 层 | 子系统 | v1 运行位置 | v2 演进 |
|---|---|---|---|
| **控制层** | ControlPlane、TrainScheduler、TrainPlanner、RecipeStore | 本地开发机(Mac/Windows/WSL) | 迁移到统一服务器,本地降级为CLI客户端 |
| **计算层** | MindTrainer、SocialSimulator(批量+实时)、MindAgent | 远程GPU服务器,由控制层经SSH调度 | 服务器上的资源池,调度方由服务器端发起 |

**关键推论**:两处受限例外直连接口(接口①MindTrainer↔SocialSimulator、接口②MindAgent↔SocialSimulator)发生在**计算层内部**,不跨越SSH隧道——控制层只负责"发起一次训练/评测会话"这个决策性动作,会话建立后计算层内部子系统直接互联,不再回头经过ControlPlane。

TrainPlanner 调用外部CLI强模型的逻辑运行在**控制层**——CLI凭证、网络出口通常绑定在本地开发机上。

---

## 1. ControlPlane

### 1.1 架构选型

**Git版本化文件树(元数据唯一真相) + 可重建的SQLite索引(查询加速) + SSH(控制层↔计算层传输)**

- 元数据(产物状态、审核记录、训练指令、诊断报告、配方定义等结构化小文件)存为 git 追踪的目录树
- 大体量产物(checkpoint、训练数据集)留在计算层文件系统,git树里只存路径引用+sha256校验
- SQLite是从git树派生的可随时重建的缓存,服务RecipeStore的结构化查询,源真相始终是git树
- SSH承担控制层→计算层的任务下发和产物引用同步,认证方式(账密/免密SSH key)可配置

### 1.2 目录结构

```
control-plane/                              # git仓库根
├── runs/                                   # 每轮迭代的运行记录
│   └── run-<id>/
│       ├── manifest.yaml                   # 本轮元信息:目标维度、触发时间、当前状态
│       ├── directive.yaml                  # TrainScheduler写入的训练指令
│       ├── scenario_ref.yaml               # 情境池/数据集的引用(路径+sha256)
│       ├── checkpoint_candidate.yaml       # 候选checkpoint引用+训练曲线摘要
│       ├── diagnostic_report.yaml          # 诊断报告(维度×分数×错误定位)
│       ├── gate_checkpoint_promotion.yaml  # 审核门①记录
│       └── outcome.yaml                    # 本轮结果摘要,供下一轮决策读取
├── recipes/                                # RecipeStore的存储子树(读写权限按约定隔离)
│   └── recipe-<id>/
│       ├── recipe.yaml                     # 完整配方快照(三种类型之一)
│       ├── lineage.yaml                    # 生命周期状态迁移历史
│       └── track_record.yaml              # 历次复用效果回写记录
├── skill_memory_proposals/                 # MindAgent提交的增量提案
│   └── proposal-<id>/
│       ├── proposal.yaml
│       └── gate_skill_inclusion.yaml       # 审核门②记录
├── models/                                 # 已晋级ZenGen版本历史(仅引用)
│   └── zengen-v<N>.yaml
├── compute/
│   └── endpoints.yaml                      # 远程GPU服务器连接配置
└── index.sqlite                            # 从文件树重建的查询索引(.gitignore排除)
```

**注**:`recipes/` 子树物理上与 ControlPlane 其他产物同在一个 git 仓库,但读写权限按子树约定隔离——只有 ControlPlane(审核门③批准后)和 RecipeStore 自身被允许写该子树,其他子系统不写。

### 1.3 产物状态机

**需审核类产物**(checkpoint候选 / 技能记忆提案 / 配方候选):

```
草稿 → 待审 → 已批准 → 已生效 → (已废弃 | 被取代)
              ↓
            已拒绝 [保留记录,不删除]
```

**非审核类产物**(训练指令 / 情境池 / 诊断报告):

```
待执行 → 已执行 → 已消费
  ↓
 失败 [SSH超时/远程任务异常,保留记录]
```

**已拒绝/失败保留原则**:两类负面状态均保留在git历史中,`outcome.yaml` 汇总时包含这些负面信号,构成 TrainPlanner 下一轮决策的输入,不可丢弃。

### 1.4 事件分发:状态调和循环

采用类Kubernetes controller的**调和循环(Reconciliation Loop)**,运行在控制层:

1. 扫描git树(文件监听或轮询),比对当前状态与期望状态
2. 发现"directive=待执行" → 通过SSH向计算层下发任务
3. 发现"candidate=待审" → 等待人工CLI命令(`rismforge approve/reject <id>`)
4. 任务运行完毕 → 产物/结果经SSH同步回git树,状态更新

**幂等可恢复**:调和循环进程崩溃重启、SSH连接中断后,下次扫描重新评估当前git树状态并正确续接,不丢事件,不重复执行已完成动作。

### 1.5 三个审核门(统一交互流程,以门①为例)

1. 调和循环检测到候选产物"待审"且依赖产物已就位,停止等待人工命令
2. 人工执行 `rismforge review <id>` — CLI读取产物+对比摘要打印
3. 人工执行 `rismforge approve/reject <id> [--reason "..."]` — 修改状态字段,提交git commit
4. 调和循环下次扫描读到新状态,执行后续动作

**v2升级路径**:CLI从"直接读本地文件"改为"通过RPC问远程ControlPlane服务",交互命令不变。

### 1.6 产物通用信封

所有产物文件遵循通用字段:
- 标识:`id / type / version / created_at / created_by`
- 状态:`status`(对应状态机)
- 溯源:`links` — 引用产出它的上游产物id(任一ZenGen版本都能沿links追溯到配方/决策/数据)
- 大体量引用:`{uri, sha256, size_bytes}` — 不将二进制放入git树

### 1.7 可测试性

调和循环核心逻辑(给定git树状态→决定下一步动作)是纯函数式的——测试时构造fixture形式的git树状态,断言下一步动作,完全不需要真实SSH/GPU/子系统。

---

## 2. SocialSimulator

### 2.1 内部模块划分

```
┌─────────────────────────────────────────────────────────┐
│ 批量生成/评测模块(异步,计算层)                           │
│  情境生成器 ─┐                                           │
│  人格生成器 ─┤→ [共享核心:仿真执行器(Simulation Runner)] │
│  诊断评测器 ←┘      ↑                                    │
│  打分器(规则式/LLM) ─┘                                   │
├─────────────────────────────────────────────────────────┤
│ 实时环境服务模块(同步,低延迟,计算层)                     │
│  环境服务网关 ─→ [同一套Simulation Runner的实时包装]      │
│    ├─ 受限接口①:MindTrainer rollout调用                  │
│    ├─ 受限接口②:MindAgent 经验采集调用                   │
│    └─ 开发者工具接口:人工CLI交互式调试观察(非流水线)       │
└─────────────────────────────────────────────────────────┘
```

**批量模块与实时服务模块共享同一个 Simulation Runner 核心**,只是外面包了两种调用方式——批量模块异步落盘,实时服务网关维持单次会话的多步状态并同步响应。

### 2.2 各模块职责

**情境生成器**:生成社会情境(场景背景、角色关系结构、核心冲突/目标设定、社会规范约束)。支持两路输入:
- 自动生成(后端可插拔:内部模板参数化,或调用外部推理桥接做更有创意的生成)
- 外部配置数据(读取人工预置的情境定义)

两路输入产出格式一致,下游 Simulation Runner 不关心情境从哪来。

**人格生成器**:生成人格模板(性格特质、背景动机、说话风格、价值观/目标倾向)。同样支持自动生成+外部配置两路输入,后端可插拔。

**诊断评测器**:加载候选checkpoint,跑标准化多维度细粒度诊断基准,产出结构化诊断报告。依赖独立的**打分器**子模块(规则式打分用于有客观答案的题目;LLM-as-judge用于开放式社会交互题目),打分器质量是诊断报告质量的关键。

**环境服务网关**:对外暴露三类调用场景——MindTrainer rollout(受限接口①)、MindAgent经验采集(受限接口②)、以及开发者工具接口(人工CLI直接发起交互式会话,实时观察模型表现,不产出标准化诊断报告,仅用于开发迭代调试,不是流水线的一环)。

**关键接口约定**:环境服务接口必须显式接受"本轮每个角色由哪个模型后端驱动"参数(自博弈/冻结参考模型/外部API),由调用方(具体训练配方)指定,不由 SocialSimulator 内部写死。

### 2.3 数据模型

| 实体 | 概念内容 | 关键设计点 |
|---|---|---|
| **Persona(人格模板)** | 性格特质、背景动机、说话风格、价值观/目标倾向、来源标记 | 与Scenario完全解耦,可自由组合 |
| **Scenario(社会情境)** | 场景背景、角色关系结构、核心冲突/目标设定、社会规范约束、来源标记、**purpose标记**(training/deployment_simulation) | purpose标记决定倾向的难度分布和多样性要求 |
| **RoleAssignment(角色分配)** | 把Scenario里的抽象角色位绑定到具体Persona+具体模型后端 | Recipe的"情境生成参数"实际指定的内容;环境服务接口"角色-模型后端映射"参数的落地位置 |
| **Episode(对话轨迹)** | 一次完整多轮交互记录:每轮的角色、话语、可选内部推理过程 | BatchTrain的训练数据来源;诊断评测的中间产物 |
| **DiagnosticReport(诊断报告)** | 维度×分数×错误类型定位的结构化报告 | 交给ControlPlane供TrainScheduler决策的关键产物 |

**Persona与Scenario解耦的意义**:两者通过RoleAssignment自由组合,多样性防护机制(BLEU聚类惩罚、多样性奖励)可以分别在Persona维度和Scenario维度独立计算。

**关于情境/人格模板层的复用策略**:SocialSimulator只维护一套模板池。MindTrainer(训练rollout)和MindAgent(经验采集)都从同一个模板池中按各自的筛选条件选取模板;Episode层(实际的逐轮对话内容)永远实时生成,任何已存在的Episode不可被替代一次新的live会话。

### 2.4 可测试性

批量模块与实时服务模块因非功能性需求完全不同(异步批量 vs 同步低延迟),需要分别压测、分别扩容,互相隔离。情境生成器、人格生成器、打分器均可独立fixture测试。

### 2.5 诊断评测体系(DiagnosticReport)详细设计

诊断评测是全工厂的导航仪——TrainPlanner 靠它决定练什么、审核门①靠它判断是否晋级、RecipeStore 靠它回写战绩。本节展开"诊断评测器 + 打分器"这两个模块的完整设计。

#### 2.5.1 能力坐标系:继承知境三层体系 + 正交通用探针组

| 层 | 内容 | 来源 |
|---|---|---|
| L1 一级维度 | 心智表征 / 社会交互 / 规范内化 | 继承 SoMBench |
| L2 二级维度 | 17 个二级维度 | 继承 SoMBench,配置灌入(`taxonomy.yaml`) |
| L3 细粒度任务 | 71 项任务范式,每项挂题目池 | 继承 SoMBench,配置灌入 |
| **G 通用能力探针**(正交) | 基础推理、指令遵循、格式合规 | 本项目新增 |

维度枚举本身不写死在设计里,作为版本化配置产物(`taxonomy.yaml`)管理,由团队已有的完整 SoMBench 体系灌入。

**G 组的必要性**:MindTrainer 的 OPD 约束是为了防止"练社会心智把通用能力练丢",但若诊断只测社会维度,OPD 有没有起作用根本看不出来。G 组不参与社会心智能力得分,唯一用途是退化监控。

#### 2.5.2 双轨诊断架构:静态轨(集成 SoMEval)+ 动态轨(自建)

**背景**:SoMBench 本身是静态基准,评测维度覆盖也有限。团队的评测执行工具 [SoMEval](https://github.com/Zhijing-AI/SoMEval) 解决了"覆盖不够全面"的问题——它是一个统一评测协议执行工具(harness),对接 SoMBench 自身及 ToMBench、EmoBench、HiToM、FANToM 等多个公开基准,统一 MCQ 协议(CoT + boxed answer 抽取)、双轨评分(封闭题精确匹配 + 开放题双 judge 打分)。但 SoMEval 本质仍是**固定测试集上的静态评测**,不具备动态/交互式评测能力。

架构需求 1 明确要求"交互式动态评测",因此诊断体系设计为**双轨并行**,而非把动态评测降级为调试工具:

```
                    候选 checkpoint
                         │
          ┌──────────────┴──────────────┐
          ↓                              ↓
   ┌─────────────┐              ┌─────────────────┐
   │  静态评测轨   │              │   动态评测轨      │
   │ (集成SoMEval) │              │ (自建,基于        │
   │              │              │  Simulation Runner)│
   └──────┬──────┘              └────────┬─────────┘
          ↓                              ↓
    结构化原始分数                  多轮交互轨迹+目标达成
          ↓                              ↓
   ┌──────────────────────────────────────────┐
   │   错误归类器 + G组探针 + 隔离校验器          │  ← 本项目自建,横跨两轨
   └──────────────────┬───────────────────────┘
                       ↓
              统一 DiagnosticReport
```

**静态轨:集成 SoMEval,不重复造轮子**。按"只复用底层基础设施,业务逻辑自建"的既定原则,SoMEval 作为执行层基础设施直接集成:
- SoMEval 负责:统一 MCQ 协议执行、封闭题精确匹配评分、开放题双 judge 评分、跨基准(SoMBench + ToMBench + EmoBench + HiToM + FANToM)的题目加载与归一化
- 本项目自建、叠加在 SoMEval 输出之上:**错误归类器**(SoMEval 给对错,不给"推理链断裂 vs 表面模式匹配"的错误类型归类)、**G 组通用能力探针**(SoMEval 覆盖的都是社会心智类基准,不含通用能力监控)、**隔离校验器**(防止训练数据污染评测集)

**集成方式**:诊断评测器模块内部是一个薄适配层——调用 SoMEval 作为子进程/库,把其原始输出转换为本项目的 `ItemResult` 格式,再送入统一的错误归类和统计聚合流程。SoMEval 未来升级或替换,下游模块无感知。

**关于变体族扰动诊断的澄清**:选项扰动、捷径检测、受控改写这类诊断手段,是 **SoMBench 数据集构造阶段编码好的题目结构**,不是 SoMEval 运行时执行的动态操作——诊断价值来自题目本身的设计,不是评测执行引擎的能力。诊断的最小单元仍是变体族而非单题:基题过、变体挂 → 表面模式匹配;全挂 → 推理链断裂;随机通过 → 不稳定。

**动态轨:自建,复用既有 Simulation Runner,非新建能力**。动态轨覆盖 SoMEval 覆盖不到的部分:多轮社会交互中的目标达成、说服/立场维护、协作推理等**过程性**能力,天然要求真实多轮交互而非固定题目。技术上直接复用 SocialSimulator 已有的 Simulation Runner 核心,与 MindTrainer 的 InteractTrain rollout 走同一套底层执行机制,区别只在于角色分配是"候选 checkpoint vs 标准化对手人格池"。

动态轨的评测信号(区别于静态轨的"对错"):
- 目标达成度(类 SOTOPIA-Eval 的多维度打分:目标完成、关系维护、社会规范遵守)
- Actio 激活轨迹分析(若候选 checkpoint 已经过 MindAgent 包装,可检查技能路由是否合理——这是结构化技能表示带来的额外诊断能力,静态基准无法提供)
- 多轮一致性(同一人格设定下,前后轮次是否自相矛盾)

动态轨打分同样落在打分器(LLM-as-judge,rubric 版本化)之下,产出格式与静态轨对齐后一并送入统一统计聚合与退化判定流程。

**两轨分档策略**:动态轨的多轮交互成本远高于静态选择题,快速筛查档在动态轨上使用更短的对话轮次上限和更小的场景样本;完整诊断档在审核门①前各跑一次完整版本。

#### 2.5.3 题目模型:变体族(VariantFamily)

同一变体族的通过模式产出错误类型分类,单题对错本身诊断价值有限——这是静态轨的核心诊断逻辑,细节见 2.5.2。

#### 2.5.4 内部模块划分

```
taxonomy.yaml + 题目池(版本化) + SoMEval集成配置
        ↓
   基准加载器 ──→ 分档选择(快速筛查集 / 完整诊断集,两轨分别设档)
        ↓
┌───────────────────┬──────────────────────┐
↓ 静态轨              ↓ 动态轨
SoMEval适配层         Simulation Runner复用
(题目执行+抽取)       (角色分配+多轮驱动)
        ↓                      ↓
        └──────────┬───────────┘
                    ↓
   打分器 ─┬─ 规则式(客观答案题,静态轨)
           └─ LLM-as-judge(开放题+动态轨,rubric驱动,prompt版本化)
                    ↓
   错误归类器 ──→ 按变体族通过模式归类错误类型(静态轨)/多轮一致性分析(动态轨)
                    ↓
   统计聚合器 ──→ L3→L2→L1 逐层聚合,带置信区间,标注每维度得分来源轨道
                    ↓
   对比与退化判定器 ──→ 对照当前晋级基座,产出退化标记与一致性信号
                    ↓
   报告生成器 ──→ DiagnosticReport 写入 ControlPlane

   隔离校验器(独立暴露给情境生成器调用,横跨两轨)
```

**打分器的 judge 模型约束**:LLM-as-judge 的模型不能是被训练模型自身或其近亲版本(自评偏差),v1 用外部推理桥接调外部强模型,或一个冻结的独立本地 judge;judge 的 rubric prompt 版本化,配一个小型校准集(人工标注题)定期检查 judge 与人工的一致率——judge 漂移本身是需要被监控的退化。

#### 2.5.5 统计层:分数必须带不确定性

每题采样 k 次得通过率和方差,逐层聚合到 L2/L1 时带置信区间。**分数差异若在置信区间内,不能被当作"提升"**——这条规则直接约束 TrainPlanner 的决策上下文(不能拿噪声当信号)和审核门①(不能被小数点后的波动误导)。

#### 2.5.6 退化判定规则

对比对象:候选 checkpoint vs 当前已晋级的 ZenGen 基座。

| 信号 | 判定 | 用途 |
|---|---|---|
| 目标维度提升 | 超出置信区间才算有效提升 | 审核门①正向依据、RecipeStore 战绩 |
| 非目标社会维度下降 | 超过阈值 → 标记"社会能力退化" | 审核门①警示 |
| G 组探针下降 | 超过阈值 → 标记"通用能力退化" | 审核门①硬性警示,同时是 OPD 失效信号 |
| 规则分 vs judge 分分歧 | 同一 L2 下两种打分方式差异异常 | reward hacking 嫌疑 |
| 诊断分 vs 训练奖励分歧 | 训练奖励持续走高但诊断分不动 | reward hacking 嫌疑(呼应场景二已知局限的具体检测手段) |

退化判定器只产出标记和证据,不做晋级决定——决定仍归审核门①的人工,与"证据驱动的审核门"原则一致。

#### 2.5.7 评测集隔离:防止数据污染

FLARE 式飞轮的逻辑是"诊断定位弱维度 → 合成新样本去练",但若情境生成器有可能生成出与诊断题目高度相似的训练样本,评测分数就会虚高,导航仪失灵。**隔离校验器**作为独立模块暴露给情境生成器:训练情境批量生成后、写入 ControlPlane 前,必须过一次与诊断题目池的相似度校验(语义相似度 + 关键结构匹配),命中的样本剔除并记录。这是硬约束,不是可选优化。

#### 2.5.8 基准版本化

基准版本 = taxonomy 版本 + 题目池版本 + SoMEval 集成版本(工具版本 + 接入的公开基准子集版本)+ judge prompt 版本 + 打分配置版本,任一变动即升版。**跨版本的分数不可直接比较**——DiagnosticReport 必带版本号,TrainPlanner 上下文汇编器和 RecipeStore 战绩追踪器只在同版本内做对比,跨版本时明确标注"不可比"。这与 RecipeStore"评测标准精确版本锚定"的要求对应。

#### 2.5.9 数据模型

| 实体 | 概念内容 |
|---|---|
| **DiagnosticTaxonomy** | 版本化的 L1/L2/L3 维度树 + G 组探针定义 |
| **DiagnosticItem / VariantFamily** | 基题 + 扰动变体集合、所属 L3、题型、打分模式、参考答案或 rubric、所属轨道(静态/动态) |
| **ItemResult** | 每题 k 次采样的原始输出、各次得分、通过率、方差 |
| **ErrorProfile** | 按 L2 汇总的错误类型分布(推理链断裂 / 表面模式匹配 / 格式失败 / 拒答 / 不稳定,动态轨含多轮一致性) |
| **DimensionScore** | 各层级得分 + 置信区间 + 样本量 + **得分来源轨道标注**(静态/动态/融合) |
| **RegressionFlags** | 退化标记列表,每条附证据(哪个维度、下降多少、对照基座版本) |
| **ConsistencySignals** | 规则/judge 分歧、诊断/训练奖励分歧的量化指标 |
| **DiagnosticReport** | 以上全部 + 头部信息(候选 checkpoint 引用、对照基座引用、基准版本、运行档位、采样配置)+ links |

`DimensionScore` 的轨道标注是关键约束:两轨分数不能笼统合并而不留痕迹,否则只在动态交互中才暴露的缺陷可能被静态轨的高分掩盖。

#### 2.5.10 可测试性

题目执行器可用固定 fixture 题目和罐头模型输出测试;打分器规则式部分纯逻辑可单测,judge 部分整体 mock;错误归类器给定构造好的变体族通过模式即可验证归类逻辑;统计聚合器和退化判定器用合成分数分布测试——全部不需要 GPU。隔离校验器用构造的相似/不相似样本对验证。SoMEval 适配层的集成测试可用 SoMEval 的固定输出样本 mock,不依赖真实调用。

### 2.6 实时环境服务接口协议设计

该接口是全架构里被引用最频繁的一环——MindTrainer 的 InteractTrain(受限接口①)、MindAgent 的经验采集(受限接口②)都依赖它。本节把补充 C 里确定的 gRPC 双向流技术选型,展开为完整的消息结构与会话状态机。协议设计止于消息结构和会话状态机,不涉及具体字段编号或语言实现,那属于 writing-plans 之后的范畴。

#### 2.6.1 会话生命周期状态机

一个会话对应"一次完整的多轮交互"——可能是训练 rollout episode,也可能是经验采集 episode,两种用途共享同一套状态机:

```
   CREATED
      │  客户端发起 CreateSession(RoleAssignment)
      ↓
   ACTIVE ──────────────┐
      │  turn 循环        │  客户端主动 EndSession
      ↓                  │  (正常收尾/达到最大轮次/目标达成)
   COMPLETING            │
      │  服务端产出        │
      │  最终Episode摘要   │
      ↓                  ↓
   CLOSED ←──────────────┘
      │
      │ (异常路径)
   FAILED  ← 服务端/客户端任一侧异常
              (超时/连接中断/角色模型后端调用失败)
```

**关键设计点**:CREATED 到 ACTIVE 之间没有额外确认步骤——CreateSession 请求本身携带完整的 RoleAssignment,服务端校验通过即直接进入 ACTIVE,不做"预检+确认"两次往返,以压低会话建立延迟,呼应补充 C.1 里"高频、低延迟"的要求。

#### 2.6.2 消息类型

gRPC 双向流是一条连接上双向独立收发,不是"一问一答"。五类消息及流向:

| 消息类型 | 方向 | 内容 | 触发时机 |
|---|---|---|---|
| **CreateSession** | 客户端→服务端 | RoleAssignment(角色-模型后端映射)、Scenario 引用/筛选条件、purpose 标记、最大轮次上限 | 会话开始 |
| **SessionReady** | 服务端→客户端 | session_id、实例化的初始情境快照 | ACTIVE 确认 |
| **TurnRequest** | 客户端→服务端 | 请求"驱动第 N 轮"(仅当客户端本身是驱动方,如训练rollout主动拉取下一轮) | 每轮开始 |
| **TurnUpdate** | 服务端→客户端 | 本轮产出的话语、说话角色、(可选)激活轨迹 | 每轮完成后立即推送 |
| **EndSession** | 双向 | 终止原因(正常完成/超时/目标达成/客户端主动终止)、最终 Episode 摘要引用 | 会话结束 |

**TurnUpdate 每轮完成后立即推送,而非攒够整段对话再返回**:这是双向流相对 REST 的核心优势——MindTrainer 的奖励管线可以在对话进行中就开始逐轮归因,不需要等最后一轮结束才拿到完整 Episode,对长对话场景的训练吞吐量有实质影响。

#### 2.6.3 激活轨迹字段:协议层强制项

呼应补充 A.3 定义的 ActivationTrace——若 TurnUpdate 消息不为它专门留一个结构化、强类型的字段,而只是塞进自由文本备注,MindAgent 的经验分析器就无法可靠解析,归因分析会退化成猜测。协议层明确:**当该轮说话角色由完整 Actio 实例扮演时,TurnUpdate 必须携带结构化的 activation_trace 字段**(触发的技能候选、最终选中技能、检索到的记忆条目);当角色是裸模型(冻结参考模型/外部API)时,该字段为空——协议层需能区分"没有激活轨迹"和"激活轨迹为空列表"两种语义。

#### 2.6.4 并发会话隔离

计算层的实时环境服务模块是常驻 gRPC 服务进程(补充 C.2 已定),需同时服务多个并发会话——MindTrainer 一次训练 batch 通常并行跑多个 rollout,MindAgent 一批经验采集也是多个 episode 并行。每个会话有独立的 session_id 和独立服务端状态(当前对话历史、已用轮次计数),会话间状态严格隔离,不共享任何可变状态。这条看似平凡,但若实现时图省事让不同会话共享情境生成器的某个缓存对象,极易在高并发下出现串话的 bug——协议设计阶段明确写下,为 writing-plans 阶段的实现划一条硬红线。

#### 2.6.5 异常路径判定

- **服务端侧异常**(如角色绑定的外部API模型调用超时/失败):服务端主动发 EndSession(终止原因=角色后端异常),不是简单断连——断连会让客户端无法区分"网络问题"和"角色模型真的挂了",这个区别直接影响 MindTrainer 的重试策略(是否需要换个种子重跑该 episode)
- **客户端侧异常**(如 MindTrainer 进程崩溃):服务端通过 gRPC 层标准心跳/keepalive机制检测连接中断,把该会话标记为 FAILED 并释放资源,不需要客户端配合
- **超过最大轮次仍未终止**:服务端强制发 EndSession(终止原因=达到最大轮次),防止逻辑异常的对话无限拖长占用资源

#### 2.6.6 与既有设计的衔接点

- CreateSession 里的 RoleAssignment 参数,是 2.2 节"环境服务接口必须显式接受角色-模型后端映射"这条约束在协议层的落地
- purpose 标记(training/deployment_simulation)在 CreateSession 中传入,决定服务端从共享模板池按哪种倾向筛选情境,对应"MindAgent 复用模板层但不复用 episode 层"的既定设计
- 最终 Episode 摘要格式,与 2.5 节动态诊断轨复用的 Episode 格式是同一套,不重新定义

#### 2.6.7 数据模型

| 实体 | 概念内容 |
|---|---|
| **Session(会话)** | session_id、状态(CREATED/ACTIVE/COMPLETING/CLOSED/FAILED)、RoleAssignment、purpose、当前轮次计数、最大轮次上限 |
| **TurnRecord(轮次记录)** | 轮次序号、说话角色、话语内容、activation_trace(可为空) |
| **SessionOutcome(会话结果)** | 终止原因、最终 Episode 引用、若FAILED则附带失败详情 |

#### 2.6.8 可测试性

会话状态机是纯状态转移逻辑,可完全脱离真实 gRPC 连接单测(给定当前状态+收到的消息类型,断言下一状态和应发出的响应)。并发隔离可用构造的多会话并发场景测试状态不串。异常路径(超时/连接中断/角色后端失败)可用 mock 传输层触发,不需要真实拉起远程服务。

---



## 3. MindTrainer

### 3.1 内部模块划分

```
                 ┌─────────────────────────────────────────┐
                 │         Trajectory Source               │
                 │  ┌─────────────────┐                   │
                 │  │ InteractTrain   │← 受限接口①         │
                 │  │ (在线多轮RL)    │→ SocialSimulator   │
                 │  └────────┬────────┘                   │
                 │           │ TrajectoryBatch             │
                 │  ┌────────┴────────┐                   │
                 │  │  BatchTrain     │← ControlPlane      │
                 │  │  (读合成数据集)  │  (已有数据集)       │
                 │  └────────┬────────┘                   │
                 └───────────┼─────────────────────────────┘
                             ↓ TrajectoryBatch(统一格式)
                     ┌───────────────┐
                     │  奖励管线      │
                     │  归因阶段      │
                     │  组合阶段      │
                     └───────┬───────┘
                             ↓ RewardedTrajectory
                     ┌───────────────┐
                     │  训练执行器    │
                     │  SFT冷启动    │← ROLL
                     │  GRPO+OPD联合 │
                     └───────┬───────┘
                             ↓
                     ┌───────────────┐
                     │ Checkpoint管理器│
                     │ 格式转换+注册  │→ ControlPlane
                     └───────────────┘
```

**核心设计原则**:两种训练模式的差异被收窄到 Trajectory Source 这一个模块的两种实现里,奖励管线、训练执行器、checkpoint管理器完全共享、模式无关。

### 3.2 各模块职责

**Trajectory Source**:抽象"rollout从哪来",两种实现:
- **InteractTrain实现**:作为客户端调用 SocialSimulator 实时环境服务网关(受限接口①),内含角色-模型后端解析逻辑,根据Recipe的RoleAssignment配置决定每个角色由谁驱动
- **BatchTrain实现**:从 ControlPlane 读取 SocialSimulator 批量合成的 Episode 数据集

两种实现产出统一格式的 **TrajectoryBatch**,下游模块不关心轨迹来源。

**奖励管线**:两阶段,由Recipe配置驱动:
- **归因阶段**:把回合级/目标级信号分解到逐句/逐决策点的细粒度奖励(可调用LLM或专门的奖励模型做归因)
- **组合阶段**:按Recipe指定的组合方式(求和/相乘等)合成最终奖励

**训练执行器**:运行在ROLL上。冷启动阶段跑SFT,主训练阶段跑**GRPO+OPD联合更新**:
- GRPO负责策略梯度更新(探索新能力)
- OPD在同一批轨迹上同步施加教师分布约束(防止偏离已验证的推理模式)
- 两者作用在同一次训练step里,不是先后阶段

**OPD是否真正生效,由诊断评测体系的 G 组通用能力探针(2.5.1、2.5.6)验证**——训练执行器本身无法自证约束有没有起作用,若候选checkpoint的G组分数出现"通用能力退化"标记,说明OPD约束在本轮训练中失效,需要在下一轮配方里调整约束系数,这是诊断评测体系与训练执行器之间的核心反馈闭环。

**Checkpoint管理器**:处理ROLL训练格式与vLLM/HuggingFace推理格式之间的转换,把候选checkpoint连同训练曲线摘要按ControlPlane产物schema信封注册。

**明确边界**:MindTrainer只负责权重级训练,不产出技能/记忆/prompt类产物——"不改权重的智能体能力提升"完全由MindAgent独立负责。

### 3.3 数据模型

| 实体 | 概念内容 |
|---|---|
| **TrainingDirective(训练指令)** | 模式选择(InteractTrain/BatchTrain)、目标维度、Recipe引用、起始checkpoint |
| **TrajectoryBatch(轨迹批次)** | 统一格式的多轮交互序列,不区分来源 |
| **RewardedTrajectory(带奖励轨迹)** | TrajectoryBatch + 逐句归因奖励 + 组合后的最终奖励 |
| **TrainingRun(训练运行记录)** | 阶段进度、中间checkpoint、断点恢复所需状态(内部记账) |
| **CandidateCheckpoint(候选checkpoint)** | 引用+训练曲线摘要+links回对应Directive/Recipe |

### 3.4 可测试性

奖励管线和训练执行器可用合成的TrajectoryBatch fixture独立测试(不需要真实ROLL/GPU)。两种Trajectory Source实现可分别独立测试(InteractTrain实现可mock SocialSimulator响应)。

---

## 4. MindAgent

### 4.1 内部模块划分

```
ControlPlane("checkpoint已晋级"事件)
       ↓ 自主订阅,自动触发
┌──────────────────┐
│ Actio实例管理器   │  绑定最新晋级checkpoint + 当前技能/记忆库状态
│ (新版本到来→中止重启)│
└────────┬─────────┘
         ↓
┌──────────────────┐
│ 模拟部署驱动器   │──→ 受限接口②: SocialSimulator实时环境服务网关
└────────┬─────────┘    (MindAgent自身角色由完整Actio实例扮演,
         ↓              SocialSimulator提供对手人格)
┌──────────────────┐
│ 经验分析器       │  跨episode聚合,识别行为模式/能力缺口
│ (不基于单一个案)  │
└────────┬─────────┘
    ┌────┴────┐
    ↓         ↓
┌───────┐ ┌────────┐
│技能   │ │记忆增量│  (技能提案生成可选调用外部推理桥接)
│提案   │ │生成器  │
│生成器 │ └───┬────┘
└───┬───┘     │
    └────┬────┘
         ↓
┌──────────────────┐
│ 提案验证器       │  回放motivating episodes + 加载候选增量
│                  │  验证是否真的带来行为改善
└────────┬─────────┘
         ↓
┌──────────────────┐
│ 提案打包器       │  打包为标准化IncrementProposal
│                  │  links回BehaviorFinding与episode证据
└────────┬─────────┘
         ↓
    ControlPlane(审核门②)
```

### 4.2 各模块职责

**Actio实例管理器**:管理活跃Actio智能体实例生命周期。MindAgent的Actio实例是**长期存活**的(跨多个episode持续积累经验),与SocialSimulator里**单个episode即用即弃**的仿真人格语义不同——两者共用同一套Actio基础设施但生命周期语义不同。

**触发机制**:MindAgent自主订阅ControlPlane的"checkpoint已晋级"事件自动启动——不由TrainScheduler主动调度。

**版本绑定策略(v1)**:始终对齐最新晋级checkpoint;新版本晋级时当前轮次直接中止重启。已知的v2+优化方向:若浪费工作量显著,可升级为"标注版本+降级为需重新验证"的精细策略。

**模拟部署驱动器**:驱动一批部署交互episode。通过受限接口②调用SocialSimulator实时环境服务网关。调用时MindAgent自身角色由**完整Actio实例**(技能库+显式记忆+路由逻辑全部生效)扮演,不是裸模型——这是观察技能/记忆脚手架表现的必要条件。SocialSimulator提供对手人格(临时实例化的仿真人格)。

**经验来源(扩展点,v1不实现)**:当前仅"模拟部署交互";未来可扩展为"模拟+真实生产交互日志"两路输入,接口预留可扩展性。

**经验分析器**:跨多个episode聚合分析,识别具体行为模式或能力缺口。**核心原则:发现必须基于跨episode的聚合模式,不基于单一个案**——避免把噪声当成信号提案。

**技能提案生成器/记忆增量生成器**:基于行为发现,分别提出候选技能库变更和记忆库变更。技能提案生成器可选调用外部推理桥接辅助合成(与SocialSimulator的情境生成、TrainPlanner的决策后端遵循同一"后端可插拔"设计原则)。

**提案验证器**:打包提交前,回放当初触发提案的episodes,加载候选增量,验证是否确实改善行为。"先自证有实证支持,再提交人工把关"的原则——降低送到审核门的低质量提案比例。

**提案打包器**:打包为标准化IncrementProposal,links回BehaviorFinding与episode证据,提交ControlPlane审核门②。

### 4.3 数据模型

| 实体 | 概念内容 |
|---|---|
| **AgentInstance(智能体实例)** | 绑定的ZenGen版本、当前技能记忆库版本、实例生命周期状态(长期存活) |
| **InteractionEpisode(部署交互记录)** | 格式与SocialSimulator的Episode一致;purpose标记为deployment_simulation |
| **BehaviorFinding(行为发现)** | 跨episode聚合识别的具体行为模式/能力缺口(不基于单一个案) |
| **SkillIncrementProposal / MemoryIncrementProposal** | 经提案验证器验证前的候选状态 |
| **IncrementProposal(增量提案)** | 提交ControlPlane的顶层产物;links回BehaviorFinding与motivating episodes |

### 4.4 与SocialSimulator的交互关系澄清

- 交互路径:唯一一条——MindAgent作为客户端调用受限接口②
- 复用层次:复用**模板层**(情境/人格模板从共享模板池按purpose标记筛选);不复用**episode层**(对话内容永远实时生成,不可替代一次live会话)
- 调用目的:经验采集(观察Actio脚手架表现),与MindTrainer的训练rollout目的不同,数据流完全隔离

---

## 5. RecipeStore

### 5.1 三种配方类型

RecipeStore存储的配方按对应的训练路径分三种类型。每种类型有各自需要精确锚定的内容,以保证可复现性:

| | **BatchTrain配方** | **InteractTrain配方** | **AgentRSI配方** |
|---|---|---|---|
| 对应路径 | SFT→GRPO+OPD多阶段静态后训练 | 在线多轮交互式RL训练 | 不改权重的技能/记忆自进化 |
| 精确数据 | 每阶段Episode数据集引用(sha256锚定) | 情境/人格模板精确ID集合+RoleAssignment | 模拟部署会话用到的模板精确ID集合+绑定的ZenGen checkpoint版本 |
| 精确配置 | 每阶段超参(学习率/batch size/步数/OPD约束系数等) | 在线RL循环超参(rollout episode数、GRPO设置等) | 经验分析器的显著性阈值、提案验证器的通过标准、episode批次规模 |
| 精确代码 | Training Executor+Reward Pipeline的确切版本(commit哈希) | Trajectory Source的InteractTrain实现+Reward Pipeline的确切版本 | 技能/记忆生成器的确切版本+外部推理桥接的精确prompt模板版本(若使用) |
| 产出引用 | 候选checkpoint | 候选checkpoint | 技能/记忆增量提案 |

**三类共享的通用信封**:能力目标维度、评测标准与通过门槛(精确版本锚定,对应 2.5.8 基准版本化——须同时锚定 taxonomy/题目池/SoMEval集成/judge prompt/打分配置五项版本)、生命周期状态、战绩记录、实验元数据。

**InteractTrain配方的复现限制**:由于LLM推理服务的非确定性(采样、批处理调度等),即使把种子/模板ID/代码版本全部锚定,也无法保证字节级完全一致的复现结果。"可复现"对InteractTrain配方应理解为"忠实重放同一套设置,得到高度相似的实验",而非"保证完全相同的输出"。

**工程前提**:精确锚定训练代码版本,要求 MindTrainer 和 MindAgent 各自的内部逻辑走标准版本控制(git commit哈希可引用)——这是 MindTrainer/MindAgent 的工程责任,RecipeStore 只负责存储该引用。

### 5.2 内部模块划分

```
TrainScheduler(配方提名) → 可复现性校验器(生成校验报告)
                                    ↓
                        ControlPlane(审核门③,人工审核时参考校验报告)
                                    ↓ 批准
                              配方存储管理器
ControlPlane(轮次结果回写) ──────────────────→ 配方存储管理器
                                                     ↓
                                           ┌─────────┴────────┐
                                           ↓                   ↓
                                     生命周期管理器         战绩追踪器
                                           ↓                   ↓
                                           └─────────┬────────┘
                                                     ↓
                                               检索引擎
                                                     ↓
                                           TrainScheduler(查询)
```

**可复现性校验器**:配方候选写入前,检查每一项引用是否已锚定为精确值/ID,而非会随时间漂移的软引用(如"当前最优配方"这类相对引用)。校验不一定是强制硬拒绝——可作为审核门③人工审核时的参考信息。按配方类型分别校验(三种类型该锚定的字段不同)。

**配方存储管理器**:`recipes/` 子树实际读写。所有写入只来自两个触发源:审核门③批准的新配方入库,或某轮迭代结果对已有配方的回写。RecipeStore不主动发起任何写入。

**生命周期管理器**:维护配方状态迁移(有效→已废弃→被取代)。触发条件:新配方入库时初始为"有效";后续回写显示关联checkpoint退化或被更优配方替代时推进迁移。

**战绩追踪器**:每次某条配方被克隆复用后,把该次结果(所属run、诊断分数变化、是否晋级)累积进战绩记录——这是TrainPlanner决策依赖的"历史效果数据"的实际来源。

**检索引擎**:服务TrainScheduler的三类查询:
- 按能力维度检索(可附加生命周期状态筛选、按配方类型筛选、按战绩排序)
- 对比两条配方(配置差异+战绩对比)
- 取完整定义供克隆(原样返回完整定义,参数扰动由TrainScheduler负责)

**边界**:克隆操作产生的"新草案"不是RecipeStore内部的对象——检索引擎只返回原始定义,新草案的构造发生在TrainScheduler侧。RecipeStore维持"只有两个读写方"的边界。

### 5.3 数据模型

| 实体 | 概念内容 |
|---|---|
| **Recipe(配方)** | 三种类型之一的完整配方快照(含上述各类型所需的精确锚定内容+通用信封) |
| **RecipeLineage(生命周期记录)** | 状态迁移历史,每次迁移的触发原因、时间、触发方 |
| **TrackRecordEntry(战绩记录条目)** | 某次复用/克隆该配方产生的具体结果:所属run、诊断分数变化、是否晋级/入库 |

---

## 6. TrainScheduler

### 6.1 内部模块划分

```
ControlPlane(新诊断报告触发)
       ↓
上下文汇编器 → (查询RecipeStore)
       ↓ PlanningContext
决策请求器 ←→ TrainPlanner
       ↓ PlannerDecision
决策校验器 → (不通过→重试决策请求器)
       ↓
动作解析器 → [全新指令 / 克隆扰动 / 精确复现]
       ↓ ResolvedAction
指令构造器 → ControlPlane(写入TrainingDirective)

ControlPlane(轮次结果事件)
       ↓
配方提名器 → ControlPlane(审核门③)
```

### 6.2 各模块职责

**上下文汇编器**:响应触发事件,把当前诊断报告(含 2.5 节定义的 `DimensionScore` 置信区间、`RegressionFlags` 退化标记、`ConsistencySignals` 一致性信号)、从RecipeStore按维度查到的相关配方(含战绩)、近期决策结果历史,汇总打包成PlanningContext。只做收集,不做判断——判断分数差异是否超出置信区间、是否触发退化警示,已在诊断评测体系内部的退化判定器(2.5.6)完成,上下文汇编器直接消费其结论,不重复计算。

**TrainScheduler自身无状态**:决策所需的全部"记忆"外置——近期决策结果在ControlPlane的run历史里,配方长期战绩在RecipeStore里。上下文汇编器每次现读现汇总,TrainScheduler崩溃重启后从git树即可完整恢复。

**决策请求器**:把PlanningContext交给TrainPlanner,拿到PlannerDecision。薄模块,推理复杂度留在TrainPlanner内部。

**决策校验器**:对PlannerDecision做校验——不只是通用schema格式检查,更重要的是**领域有效性校验**:
- 指名克隆的配方id是否真实存在
- 该配方是否处于可用的生命周期状态(不能克隆"已废弃"的配方)
校验不通过则退回决策请求器重试,不硬失败。

**动作解析器**:把通过校验的决策解析成三种具体动作之一:
- **全新指令**:TrainPlanner直接给出的新参数,不依赖已有配方
- **克隆扰动指令**:取某条已有配方,在其基础上做参数扰动
- **精确复现指令**:取某条已有配方,原样锚定值不做任何扰动

**指令构造器**:构造正式TrainingDirective写入ControlPlane。**只构造BatchTrain或InteractTrain类型的指令**,不构造AgentRSI类型——MindAgent是自主订阅事件、自己触发的,TrainScheduler从不主动派发MindAgent的工作。

**配方提名器**:独立于主流程的动作,监听轮次结果事件(包括MindTrainer和MindAgent两种来源),达到入库标准时打包对应类型(包括AgentRSI类型)的配方候选提交审核门③。TrainScheduler是三种配方类型统一的提名入口——MindAgent不直接写RecipeStore,RecipeStore"只有两个读写方"的边界得以保持。

### 6.3 指令确认模式(人在环路)

在训练指令的状态机里加一个可配置的"待确认"状态:
- **手动模式**:TrainScheduler构造的指令进入"待确认"状态,人工执行 `rismforge confirm <directive-id>` 后才进入"待执行"
- **自动模式**:跳过"待确认"状态,信任建立后切换

另外,人工随时可以绕过TrainPlanner直接向ControlPlane写入指令——作为最强的人工干预手段(开发策略中"第一轮打通时决策人工写死"就是这个路径,它是正式能力而非临时脚手架)。

### 6.4 数据模型

| 实体 | 概念内容 |
|---|---|
| **PlanningContext(规划上下文)** | 当前诊断报告+相关配方查询结果+近期决策结果历史 |
| **PlannerDecision(决策结果)** | 目标维度、动作类型、源配方引用(若克隆/复现)、建议扰动参数(若克隆) |
| **ResolvedAction(已解析动作)** | 完整指定的指令内容 |
| **RecipeNomination(配方提名候选)** | 三类型配方候选之一,提交审核门③前的形态 |

### 6.5 可测试性

决策校验器和动作解析器可用合成的PlannerDecision fixture独立测试——不需要真实调用TrainPlanner。与TrainPlanner的"上下文打包解析与外部CLI调用物理分离"设计正好衔接:两边各自可独立测试,拼起来才需要一次真实端到端集成测试。

---

## 7. TrainPlanner

### 7.1 内部模块划分

```
TrainScheduler(PlanningContext)
       ↓
上下文打包器 → (套用版本化Prompt模板)
       ↓ PackedContext
决策后端适配层(可插拔)
  ├─ 外部强模型后端(v1推荐)
  │   └─ 经外部推理桥接(同时支持CLI进程和API两种形态)
  └─ 启发式/bandit后端(备选)
       ↓ 后端原始输出
输出解析器 → (严格结构校验,失败则重试有限次数)
       ↓ PlannerDecision + DecisionRationale
TrainScheduler
```

### 7.2 各模块职责

**上下文打包器**:把PlanningContext套入**版本化的Prompt模板**,渲染成交给后端的完整输入。Prompt模板必须版本化管理——它实质上是TrainPlanner的"决策代码",每个决策产物都记录它用的是哪个版本的模板,以保证决策质量的可追溯性。

**v2+ 的自然延伸(现在只留认知,不设计)**:让工厂自己实验和改进Planner的prompt模板,这将是元认知RSI的更深一层。

**决策后端适配层**:可插拔接口,两个实现:
- **外部强模型后端(v1推荐)**:经共享基础设施的外部推理桥接调用(CLI进程方式或API方式均支持,凭证和调用形态的差异被桥接层吸收,TrainPlanner不感知)
- **启发式/bandit备选后端**:留给不依赖外部服务的场景

**输出解析器**:把后端返回的自由文本严格解析为结构化PlannerDecision。解析失败时带着错误信息重试有限次数,重试耗尽则显式失败并留痕——**不做"尽力猜测"式的宽容解析**。这是纵深防御的第一层:输出解析器管结构合法 → TrainScheduler的决策校验器管领域有效 → ControlPlane管schema终检。

**DecisionRationale(决策依据)强制保留**:后端给出决策的同时必须给出理由说明,与决策一起持久化进ControlPlane。两个用途:
1. 人工在指令确认模式下看的就是它
2. 进入后续轮次的PlanningContext,让下一次决策能参考"上次为什么这么选、结果如何"

**这是v1元认知RSI循环的实际运作方式**:TrainPlanner自身无状态,其"变聪明"完全来自上下文里越积越厚的**决策-理由-结果三元组**,不依赖内部私有记忆。

**输出边界(v1明确约束)**:TrainPlanner输出**严格限定在结构化决策/配置范围内**(选维度、选配方、调超参、给数据合成方向的具体指令)——**不允许直接改写子系统实现代码**。因此ControlPlane不需要设计任何代码执行沙箱,只需对结构化输出做schema校验即可。这一约束被明确推迟到v2+:如果未来允许外部智能体直接改写pipeline代码(Darwin Gödel Machine式的能力),需要专门设计独立沙箱化执行环境。

### 7.3 数据模型

| 实体 | 概念内容 |
|---|---|
| **PromptTemplate(提示模板)** | 版本化的决策提示模板;决策产物引用其版本 |
| **PackedContext(打包上下文)** | 模板渲染后的完整后端输入,含所用模板版本引用 |
| **PlannerDecision(决策)** | 目标维度、动作类型(全新/克隆/复现)、源配方引用、扰动参数 |
| **DecisionRationale(决策依据)** | 后端给出的理由说明,与决策一起持久化 |

### 7.4 可测试性

上下文打包器用固定PlanningContext fixture测试模板渲染正确性;输出解析器用各类构造的后端返回(合法/缺字段/格式错乱)测试解析与重试逻辑。外部后端在测试中整体替换为罐头响应——"上下文打包/输出解析"与"外部CLI调用"物理分离,前者完全脱离外部服务可独立测试。

---

## 典型场景端到端流程验证

以下三个场景分别覆盖三种训练路径,用于验证整体架构与子系统设计的自洽性与可实现性。

---

### 场景一:BatchTrain 路径——首次训练"高阶信念推理"能力

**背景**:工厂刚启动,ZenGen 基座是 Qwen3-14B,没有任何历史配方,第一轮决策人工写死。

```mermaid
flowchart TD
    A(["\U0001f9d1 人工写入初始训练指令\nHiToM / BatchTrain"]) --> B

    subgraph CTL["控制层 (本地开发机)"]
        B["ControlPlane 调和循环\n写入 directive.yaml\n状态=待执行"]
        G["ControlPlane\n写入 diagnostic_report.yaml"]
        H{{"审核门①\nCheckpoint 晋级\n人工 rismforge approve"}}
        I["ControlPlane\n写入 zengen-v1.yaml\n状态=已晋级"]
        J["TrainScheduler\n配方提名器\n打包 BatchTrain 配方候选"]
        K{{"审核门③\n配方入库\n人工确认"}}
        L["RecipeStore\n入库 recipe-001\n可复现性校验通过"]
        M["TrainPlanner\n汇总诊断+战绩\n调用外部 CLI 强模型\n→ 下一轮决策:克隆 recipe-001"]
    end

    subgraph CALC["计算层 (远程 GPU 服务器)"]
        C["SocialSimulator 批量模块\n情境生成器(purpose=training)\n三阶/四阶 HiToM 嵌套场景\n人格生成器配套角色人格\n→ Episode 数据集"]
        D["MindTrainer BatchTrain\n读取 Episode 数据集\nSFT 冷启动(行为克隆)\n→ GRPO+OPD 联合更新\n(同一 step:GRPO 探索 / OPD 防偏离)"]
        E["候选 checkpoint"]
        G2["SocialSimulator 诊断评测器\n加载候选 checkpoint\n规则式打分(HiToM 有客观答案)\n三阶信念+12.3 / 四阶信念+8.7"]
        MA["MindAgent(异步)\n自主订阅 已晋级 事件\n绑定 zengen-v1\n经验采集(受限接口②)\npurpose=deployment_simulation\n⚠️ 第一轮技能库/记忆库为空\n→ 沉默(预期行为)"]
        C --> D --> E
    end

    B -->|"调和循环检测 待执行\nSSH 下发任务"| C
    E -->|"产物引用+sha256\nSSH 同步回"| G
    G -->|"调和循环检测 候选就绪\nSSH 触发诊断"| G2
    G2 -->|"诊断报告 SSH 同步回"| G
    H -->|approve| I
    I -->|"异步触发"| MA
    I --> J --> K -->|approve| L --> M

    note1["⚠️ 设计验证点\n情境生成参数此时是筛选条件\n精确锚定(模板ID集合)在配方打包阶段才确定\n先运行再锚定的时序合理 ✅"]
    C -.-> note1
```
**Step 1:初始指令**
人工直接向 ControlPlane 写入训练指令(TrainPlanner 决策的降级替代路径):目标维度=高阶信念推理(HiToM),训练模式=BatchTrain。TrainScheduler 的指令构造器把 ResolvedAction 写入 ControlPlane,状态=待执行。

**Step 2:情境合成(SocialSimulator 批量模块)**
调和循环检测到"训练指令=待执行",SSH 下发任务到计算层。情境生成器(purpose=training)按指令参数生成三阶/四阶信念推理嵌套场景,人格生成器配套生成角色人格。Episode 数据集写回 ControlPlane,状态=已生成。

**⚠️ 设计验证点**:指令里的"情境生成参数"此时是筛选条件,还没有"精确锚定的模板 ID 集合"。精确锚定发生在配方候选打包阶段——模板被生成后 ID 集合才确定,RecipeStore 可复现性校验器在入库时验证。先运行再锚定的时序是合理的。✅ 自洽。

**Step 3:BatchTrain 训练(MindTrainer)**
调和循环检测到数据集就绪,SSH 启动 MindTrainer。BatchTrain 实现读取数据集,走 SFT 冷启动 → GRPO+OPD 联合更新(GRPO 探索新推理能力,OPD 约束不偏离已有通用推理分布,作用于同一训练 step)。产出候选 checkpoint 写入 ControlPlane,状态=候选。

**Step 4:诊断评测(SocialSimulator 批量/评测模块)**
诊断评测器加载候选 checkpoint,打分器(规则式,HiToM 题有客观答案)产出诊断报告:三阶信念+12.3 分,四阶信念+8.7 分,通用推理无退化(-0.3 分,在 OPD 容忍范围内)。

**Step 5:审核门① Checkpoint 晋级(人工)**
人工执行 `rismforge review run-001` 看报告,执行 `rismforge approve run-001`。ControlPlane 状态变更提交 git commit,checkpoint 写入 `models/zengen-v1.yaml`,状态=已晋级。

**Step 6:MindAgent 并行自动触发**
MindAgent 自主订阅"checkpoint 已晋级"事件,绑定 zengen-v1,通过受限接口②调用 SocialSimulator 实时环境服务跑部署仿真 episode。**⚠️ 第一轮技能库/记忆库为空,沉默是预期行为**。✅ 自洽。

**Step 7:配方入库(RecipeStore)**
TrainScheduler 配方提名器打包 BatchTrain 类型配方候选:锁定模板 ID 集合、数据集引用(sha256)、超参精确值、Training Executor+Reward Pipeline 的 git commit 哈希、评测基准版本。审核门③批准后,RecipeStore 入库 recipe-001。

**Step 8:TrainPlanner 接手下一轮**
TrainPlanner 汇总 run-001 诊断报告+recipe-001 战绩,调用外部 CLI 强模型,决策:克隆 recipe-001,提高四阶信念推理场景比例。

---

### 场景二:InteractTrain 路径——训练"说服与立场维护"的多轮社交能力

**背景**:已有 zengen-v2(经两轮 BatchTrain 产出),RecipeStore 已有三条 BatchTrain 配方。TrainPlanner 决定切换 InteractTrain 模式——"说服与立场维护"需要多轮动态对抗,静态数据无法充分覆盖。

```mermaid
flowchart TD
    subgraph CTL["控制层 (本地开发机)"]
        A["TrainPlanner\n汇总诊断分数+配方战绩\n调用外部 CLI 强模型\n→ PlannerDecision:\n全新 InteractTrain 指令\n目标=说服与立场维护"]
        B["TrainScheduler\n动作解析器\n解析决策→全新指令\nRoleAssignment:\n  zengen-v2=坚守立场方\n  冻结 zengen-v1=试图说服方"]
        C["ControlPlane\n写入 directive.yaml\n状态=待执行"]
        H["ControlPlane\n写入 diagnostic_report.yaml\n状态=候选待审"]
        I{{"审核门①\nCheckpoint 晋级\n人工 rismforge approve"}}
        J["ControlPlane\n写入 zengen-v3.yaml\n状态=已晋级"]
        K["TrainScheduler\n配方提名器\n打包 InteractTrain 配方候选\n标注:含 LLM 非确定性组件\n复现=高度相似而非字节级一致"]
        L{{"审核门③\n配方入库\n人工确认"}}
        M["RecipeStore\n入库 recipe-004\nInteractTrain 类型"]
    end

    subgraph CALC["计算层 (远程 GPU 服务器)"]
        D["SocialSimulator\n实时环境服务网关\n接收 RoleAssignment 参数\n实例化:\n  zengen-v2(被训练方)\n  冻结 zengen-v1(对手方)"]
        E["MindTrainer InteractTrain\n通过受限接口①\n高频 rollout"]
        F["奖励管线\nLLM-as-judge 话语级归因\n(说服类无客观答案)\n→ 逐句奖励:\n  最终立场是否守住\n  每轮说服力评分\nGRPO+OPD 更新参数"]
        G["候选 checkpoint"]
        H2["SocialSimulator\n诊断评测器\n标准化基准(含多样人格说服者)\n不只是 zengen-v1 风格"]
        MA["MindAgent(异步)\n订阅 已晋级 事件\n绑定 zengen-v3\n经验采集(受限接口②)"]

        D <-->|"受限接口①\n(计算层内部直连,不经 ControlPlane)"| E
        E --> F --> G
    end

    A --> B --> C
    C -->|"调和循环检测 待执行\nSSH 下发 InteractTrain 会话"| D
    G -->|"候选 checkpoint 引用\nSSH 同步回"| H
    H -->|"调和循环检测 候选就绪\nSSH 触发诊断"| H2
    H2 -->|"诊断报告 SSH 同步回"| H
    I -->|approve| J
    J -->|"异步触发"| MA
    J --> K --> L -->|approve| M

    warn["🔶 已知局限:对手固定为 zengen-v1\n可能学到特定对抗套路(reward hacking)\n应对:诊断基准含多样人格 + RecipeStore 战绩回写\n若后续退化→生命周期迁移到 已废弃\n改进路径:加中间检查点诊断"]
    E -.-> warn
```

**Step 1:InteractTrain 指令生成**
TrainScheduler 动作解析器解析 PlannerDecision:全新 InteractTrain 指令,目标维度=说服与立场维护,RoleAssignment=被训练模型(zengen-v2)扮演"坚守立场方",对手角色由冻结的 zengen-v1 扮演"试图说服方"(类 R-Zero 的 Challenger-Solver 结构迁移到社会场景)。

**⚠️ 设计验证点**:"对手角色=冻结的 zengen-v1"通过环境服务接口显式传给 SocialSimulator,不由 SocialSimulator 内部写死。"角色-模型后端映射必须由调用方指定"这条约束得到验证。✅ 自洽。

**Step 2:实时 rollout(计算层内部直连)**
MindTrainer InteractTrain 实现通过受限接口①连接 SocialSimulator 实时环境服务网关,高频 rollout。奖励管线的归因阶段用 LLM-as-judge(说服类任务无客观答案,规则式打分不适用)做话语级归因,把"最终立场是否守住""每一轮回应的说服力"分解到逐句奖励。GRPO+OPD 更新参数。

**⚠️ 已知局限**:对手是 zengen-v1,被训练模型可能学到"专门对抗 zengen-v1 的套路"而非泛化说服能力(reward hacking)。当前架构的应对是延迟发现机制:①诊断评测用标准化基准(含各种不同人格的说服者,不只有 zengen-v1 风格);②RecipeStore 战绩回写若后续显示退化,生命周期管理器推进配方状态到"已废弃"。这是 v1 已接受的权衡,改进路径是加中间检查点诊断缩短发现周期。🔶 局限已知,有明确改进路径。

**Step 3-7:与场景一相同(诊断→审核门①→晋级→MindAgent 触发→配方入库)**

**InteractTrain 配方入库的特殊处理**:可复现性校验器在 recipe.yaml 元数据中标注"含 LLM 推理服务非确定性组件,复现结果为高度相似而非字节级一致",人工审核门③时可见此说明,不影响入库。✅ 自洽。

---

### 场景三:AgentRSI 路径——MindAgent 自主发现并提案"情绪感知技能"

**背景**:MindAgent 已积累三轮模拟部署交互经验(跨 zengen-v1/v2/v3 三个版本),当前绑定 zengen-v3,技能库已有两个技能:意图推断路由、信念状态追踪。

```mermaid
flowchart TD
    subgraph CALC["计算层 (远程 GPU 服务器)"]
        A["MindAgent\nActio实例管理器\n绑定 zengen-v3\n加载当前技能库:\n  意图推断路由\n  信念状态追踪"]
        B["模拟部署驱动器\n通过受限接口②\n跑 80 个 episode\n日常情感支持+家庭冲突场景"]
        SS["SocialSimulator\n实时环境服务网关\n实例化对手人格(临时)\nMindAgent 自身=完整 Actio 实例\n(技能库全部生效,不是裸模型)"]
        C["经验分析器\n跨 80 个 episode 聚合\n发现稳定模式:\n  情绪高激活时(但是/你不理解)\n  意图推断路由→目标冲突处理\n  忽略情绪状态识别\n  67% episode 受影响\n→ BehaviorFinding"]
        D["技能提案生成器\n调用外部推理桥接(Claude Code)\n生成候选技能:\n  情绪激活检测器\n  在意图路由前检测情绪信号\n  切换 情绪先于目标 策略"]
        E["提案验证器\n回放 15 个代表性 episode\n加载候选技能\n重新驱动 Actio 实例\n结果:情绪高激活轮次\n对话质量+23%\n无负向影响"]
        F["提案打包器\n打包 IncrementProposal:\n  BehaviorFinding(67%,80 episode)\n  候选技能定义\n  验证结果(+23%,15 episode)\n  触发 episode 引用列表"]

        A --> B
        B <-->|"受限接口②\n(计算层内部直连)"| SS
        B --> C --> D --> E --> F
    end

    subgraph CTL["控制层 (本地开发机)"]
        G{{"审核门②\n技能/记忆增量纳入\n人工 rismforge review proposal-007\n→ approve"}}
        H["ControlPlane\n情绪激活检测器\n纳入正式技能库"]
        I["TrainScheduler\n配方提名器\n打包 AgentRSI 类型配方:\n  锁定 zengen-v3 版本\n  模板精确 ID 集合\n  分析器阈值配置\n  Claude Code prompt 模板版本"]
        J{{"审核门③\n配方入库\n人工确认"}}
        K["RecipeStore\n入库 recipe-008\nAgentRSI 类型\n(无训练数据集/无训练超参)\n✅ 三类型配方分开设计的必要性得证"]
    end

    F -->|增量提案写入 ControlPlane| G
    G -->|approve| H
    H --> I --> J -->|approve| K

    warn["🔶 已知局限:验证器用同批数据\n有过拟合风险\n应对:审核门②后续轮次持续监控\n战绩追踪器记录新 episode 表现\n改进路径:验证器加留出集"]
    E -.-> warn
```

**Step 1:经验采集与分析**
本轮通过受限接口②跑了 80 个模拟部署 episode。经验分析器跨 80 个 episode 聚合分析,发现跨 episode 的稳定模式:当对话方情绪高度激动时,意图推断路由把话语全部路由到"目标冲突处理"技能,忽略了情绪状态识别,约 67% 的 episode 受影响。记录为 BehaviorFinding:能力缺口类型=情绪感知缺失。

**Step 2:技能提案生成**
技能提案生成器调用外部推理桥接(Claude Code)生成候选技能:"情绪激活检测器"——在意图推断路由之前检测高情绪激活信号,若检测到则切换"情绪先于目标"响应策略。

**Step 3:提案验证器**
回放 15 个代表性 episode,加载候选技能,重新驱动 Actio 实例。验证结果:情绪高激活轮次后续对话质量平均提升 23%,无负向影响。

**⚠️ 已知局限**:验证器回放的是"触发提案的同批数据",有过拟合风险。应对:审核门②后续轮次 MindAgent 经验采集会用新 episode 持续验证,战绩追踪器记录后续表现。审核门是第一道关,不是最后一道。🔶 局限已知,架构设计一致。

**Step 4:审核门②(人工)**
人工执行 `rismforge review proposal-007`,看到完整证据链后 `rismforge approve proposal-007`,情绪激活检测器纳入正式技能库。

**Step 5:AgentRSI 配方入库**
TrainScheduler 配方提名器打包 AgentRSI 类型配方:锁定 zengen-v3 版本、模拟部署用到的情境/人格模板精确 ID 集合、经验分析器显著性阈值配置、技能提案生成器调用的 Claude Code prompt 模板版本。审核门③批准后入库 recipe-008(AgentRSI 类型)。

**⚠️ 设计验证点**:AgentRSI 配方里没有训练数据集和训练超参——这两项是 BatchTrain/InteractTrain 配方的核心字段,对 AgentRSI 毫无意义。三类型配方分开设计而不是强行套同一个 schema 的必要性在此得到直接验证。✅ 设计必要且自洽。

---

### 场景验证总结

| 已知局限 | 暴露场景 | 当前应对机制 | v2+ 改进路径 |
|---|---|---|---|
| InteractTrain 的 reward hacking 延迟发现 | 场景二 Step 2 | RecipeStore 战绩回写+生命周期迁移到"已废弃" | 加中间检查点诊断,缩短发现周期 |
| 提案验证器有过拟合风险 | 场景三 Step 3 | 审核门②后续轮次持续监控+战绩回写 | 验证器加留出集(非 motivating episodes) |
| MindAgent 第一轮沉默 | 场景一 Step 6 | 预期行为,无需应对 | 冷启动阶段可人工预置少量初始技能 |

**三个场景均未发现结构性矛盾或不可实现环节**,整体架构可支撑一次完整的端到端 RSI 闭环。

---

## 附:七个子系统设计一致性对照

| 设计原则 | 在各子系统中的体现 |
|---|---|
| **后端可插拔** | SocialSimulator情境生成后端、MindAgent技能提案生成后端、TrainPlanner决策后端——三处遵循同一设计模式 |
| **数据流而非控制流** | 子系统只产出产物写入ControlPlane,不直接调用彼此(两处受限例外明确标注) |
| **证据驱动的审核门** | 诊断报告附于checkpoint晋级门、episode证据附于技能提案门、可复现性校验附于配方入库门 |
| **负面信号保留** | 被拒绝的checkpoint/提案、失败的任务——结果都保留,进入TrainPlanner的决策上下文 |
| **无状态+外置记忆** | TrainScheduler无状态(记忆外置于ControlPlane+RecipeStore)、TrainPlanner无状态(记忆外置于ControlPlane的决策-理由-结果三元组) |
| **可独立fixture测试** | 所有子系统的核心逻辑都可以不依赖GPU/外部服务独立测试 |

---

## 补充:三处设计细化

本节补充三处此前架构设计时有意延后的技术选型与实现细节,这些决策会直接影响后续实施计划的制定。

### 补充 A:Actio 模块详细设计

Actio 是共享基础设施层的核心组件,为 SocialSimulator 和 MindAgent 提供统一的"带技能库/记忆库的智能体实例"执行引擎。

#### A.1 内部模块划分

```
┌─────────────────────────────────────────────────────┐
│ 技能库(Skill Library)                                 │
│   Skill = {触发条件, 响应策略, 层级(宏观/微观), 版本}    │
│   路由器:按触发条件匹配对话状态,冲突时按优先级裁决       │
├─────────────────────────────────────────────────────┤
│ 显式记忆(Explicit Memory)                              │
│   MemoryEntry = {主体, 信念/事实, 时间戳, 来源episode,   │
│                   置信度, 版本}                         │
│   支持"某时刻信念状态"快照查询                          │
├─────────────────────────────────────────────────────┤
│ 推理经验(Reasoning Experience)                          │
│   跨任务可复用的推理模式(非完整episode,是提炼后的模式)   │
│   三级回退激活:主策略→次策略→保底策略                   │
├─────────────────────────────────────────────────────┤
│ 模型后端抽象(Model Backend Adapter)                     │
│   统一接口封装:ZenGen checkpoint / 冻结参考模型 / 外部API │
│   RoleAssignment 最终解析到的就是这一层                 │
└─────────────────────────────────────────────────────┘
              ↓
      ActioInstance(实例)
      instantiate(model_backend_ref, skill_library_version,
                   memory_library_version, profile)
      → respond(dialogue_history) → (utterance, activation_trace)
```

#### A.2 技能表示形式:结构化技能(触发条件+响应策略)

每个技能是一个显式结构:
- **触发条件**(结构化谓词,针对对话状态/信号):如"检测到高情绪激活信号"、"对方连续3轮未给出实质性信息"
- **响应策略**(结构化指令):如"优先级覆盖:情绪响应 > 目标响应"、"切换到情绪优先响应模式"、"启用澄清追问模板"
- **层级标记**(宏观/微观):宏观技能影响整体对话策略(如"坚守立场"),微观技能影响单句话术(如"委婉拒绝话术")
- **版本号**:技能库迭代时的版本标记

**对应调研中知境 Actio 的 PRISM 设计(分层技能,按需路由)**。

**设计意义**:
- 技能提案生成器产出的"新技能"是结构化字段,不是自由文本prompt片段
- 提案验证器可以直接对着结构化字段做验证,人工审核门②看到的也是结构化对比,而非一段自由文本
- 技能路由器按触发条件匹配对话状态,决定激活哪个技能——这是显式可追溯的决策,而不是隐式交给底层模型判断

#### A.3 激活轨迹(ActivationTrace)——关键补充接口

之前 MindAgent 设计里的**经验分析器**要"跨episode聚合分析行为模式",但如果 Actio 只吐出最终回复,分析器根本无法知道"这句回复是哪个技能路由决定的、是否检索了某条记忆"——没有这层信息,归因分析就是空中楼阁。

**因此 ActioInstance 每次响应都必须同时返回一份激活轨迹**:
- 本轮触发了哪些技能候选
- 路由器最终选中了哪个技能
- 是否检索了记忆库(检索到了什么条目)
- 推理经验激活情况(若使用)

经验分析器的行为发现(BehaviorFinding)会直接引用这份轨迹——这也让场景三里"意图推断路由忽略情绪状态"这类发现,从"猜测"变成"有据可查"。

#### A.4 Actio 的关键边界:自身无状态,绑定的版本由调用方决定

Actio 本身不决定"用哪个版本的技能库/记忆库"——这是调用方的职责:
- **SocialSimulator** 每次实例化都绑定一次性的人格快照,用完即弃,不产生长期更新
- **MindAgent** 绑定当前最新批准的技能/记忆库版本,是唯一能够**提案更新**这些版本的角色(通过它自己的分析-生成-验证流程)

Actio 只是执行引擎:给定绑定的版本快照,忠实地路由技能、检索记忆、调用模型后端、产出响应+激活轨迹。

#### A.5 数据模型

| 实体 | 概念内容 |
|---|---|
| **Skill(技能)** | 触发条件(结构化谓词)、响应策略(结构化指令)、层级(宏观/微观)、版本 |
| **MemoryEntry(记忆条目)** | 主体、信念/事实内容、时间戳、来源episode引用、置信度、版本 |
| **ReasoningExperience(推理经验)** | 提炼后的可复用推理模式、三级回退激活优先级 |
| **ActioInstance(智能体实例)** | 绑定的模型后端引用+技能库版本+记忆库版本+人格/agent画像 |
| **ActivationTrace(激活轨迹)** | 本轮响应实际触发的技能候选、最终选中的技能、检索到的记忆条目列表 |

---

### 补充 B:ControlPlane 调和循环技术选型

#### B.1 触发机制:轮询,而非文件系统原生监听

**约束**:本地开发机跨 Mac/Windows/WSL 部署,而 WSL 下 inotify 跨 Windows/Linux 边界监听文件变化历史上一直不稳定。

**观察**:状态变化的真实来源只有两类:
1. 人工执行 CLI 命令(即时,不需要监听,命令本身直接写 git 并 commit)
2. 远程计算层任务完成后经 SSH 同步回来(节奏是分钟到小时级,不是毫秒级)

**结论**:不需要毫秒级响应,**简单轮询**(可配置间隔,默认 5-30 秒)完全够用,且跨平台可靠性远高于原生文件监听。

#### B.2 Git 树访问方式:调用 git CLI,而非绑定某个语言的 git 库

调和循环对 git 树的所有操作(读状态、写状态、commit)通过**子进程调用系统 git 命令**,而不是引入语言特定的 git 库(如 GitPython/libgit2)。

**理由**:
- git CLI 在 Mac/Windows/WSL 上都是标准可用的,而语言库经常有版本兼容问题(尤其是 Windows 下)
- 调用 CLI 也让"调和循环的核心逻辑是纯函数"这个此前已确定的可测试性设计更容易落地——测试时可以直接 mock 子进程调用,不需要真的操作 git 仓库

#### B.3 调和循环的两种调用模式:一次性 vs 守护进程

人工命令(`rismforge approve <id>`)本身是同步、立即生效的——命令执行时直接写 git 树并 commit,不需要等待任何守护进程。守护进程模式(长期运行、定期轮询)只负责处理**不依赖人工触发**的那部分状态迁移(比如"数据集生成完毕,自动启动训练")。

**因此调和循环设计成两种调用模式共享同一个核心 `reconcile()` 函数**:
- **一次性模式**(`rismforge reconcile --once`):扫描一次,处理完当前所有待办状态迁移,退出
- **守护进程模式**(`rismforge daemon`):循环调用一次性模式,每次间隔可配置

这个设计直接延续了之前"调和循环核心逻辑是纯函数,可用fixture测试"的既定原则,现在补齐了"这个纯函数具体怎么被调用"这一层。

---

### 补充 C:SocialSimulator 两模块部署协议

#### C.1 实时环境服务网关协议:gRPC 双向流

**需求**:高频、低延迟、需要在多轮对话中维持会话状态。

**选型**:gRPC 的双向流(bidirectional streaming):一次连接建立后,MindTrainer/MindAgent 和 SocialSimulator 之间持续发送/接收多轮消息,不需要每轮重新建连、不需要每次把完整对话历史重新传一遍(这是 REST 无状态调用在这个场景下的主要缺点)。

**理由**:
- gRPC 的强类型 schema 天然契合"环境服务接口必须显式接受角色-模型后端映射参数"这个此前的既定约束——参数结构在 schema 里写死,调用方不能传糊涂账
- 双向流支持服务端主动推送事件(如仿真环境状态变化通知),为后续扩展预留可能性

**详细的消息结构与会话状态机设计,见 2.6 节。**

#### C.2 批量模块与实时模块:同一进程内的两个库 → 分别包装成两个独立部署单元

延续之前"两者共享同一个 Simulation Runner 核心"的设计,具体落地为:
- **批量模块**部署为按需启动的**任务脚本**(训练/评测请求触发时启动,跑完退出),没有常驻进程
- **实时模块**部署为**常驻 gRPC 服务进程**,在计算层持续运行,等待 MindTrainer/MindAgent 建立连接

**关键**:两者是**分离的操作系统进程**,不共享运行时状态,只共享同一份 Simulation Runner 代码库。

**理由**:批量模块偶发的崩溃(比如生成一个超大数据集时内存溢出)不会打断正在进行的 InteractTrain 实时训练会话——这是纯粹的故障隔离考虑,之前的设计里"批量模块与实时模块需要分别压测、分别扩容"这条已经暗示了这个方向,现在明确成具体的进程边界。
