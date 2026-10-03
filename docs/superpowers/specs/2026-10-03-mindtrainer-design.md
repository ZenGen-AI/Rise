# MindTrainer 设计:权重级训练引擎(BatchTrain + InteractTrain)

- **状态**:待用户审阅
- **日期**:2026-10-03
- **前置文档**:
  - `2026-09-22-rismforge-architecture-design.md`(总体架构,§4.2、§6)
  - `2026-09-24-rismforge-subsystem-design.md`(§3 MindTrainer、§5.1 配方锚定、补充 D 成本与可观测、补充 E 失败与断点恢复)
- **范围**:MindTrainer 的模块划分、数据模型、与已落地子系统的接缝、测试策略,以及明确不做的事。本文不含实现代码,接口草图只用来钉死字段和签名。
- **与上一版口头设计的关系**:本文是评审走查(三个端到端场景)之后的定稿,吸收了其中发现的全部缺口。

---

## 0. 范围与已定决策

| 决策点 | 结论 | 理由 |
|---|---|---|
| 交付形态 | 独立库 `src/mindtrainer`,入口 `run_training(...)`;**不接入**调和循环,不替换 `mind_trainer_stub` | 沿用 Plan 3 诊断评测的做法;接线留给后续计划 |
| 训练执行器 | `TrainingExecutor` 协议 + 确定性 mock;真实 ROLL 后置 | 与仓库"脱离 GPU/外部服务可测"的硬约束一致 |
| 轨迹源 | BatchTrain 与 InteractTrain **都做** | 下游"模式无关"这条核心设计原则,要两种实现共用同一套下游才算验证 |
| 训练配置 | 内联在训练指令的 payload 里,带 `config_hash`;RecipeStore 落地后改成 `recipe_ref` 引用,配置结构不变 | RecipeStore 还不存在,不提前设计它的一部分 |
| 数据集界定 | `dataset_manifest`(sha256 锚定,按阶段各一份) | spec §5.1 要求配方精确锚定数据集 |
| 断点续训 | 做(补充 E.4/E.5);**不做** `CostRecord` 成本记账(补充 D.4) | 成本是横切设计,不提前落到 MindTrainer 里 |

**两条边界**

1. **MindTrainer 不导入 `diagnostic.*`。** spec 只开了两处受限例外(接口①②,都指向 SocialSimulator),没有 MindTrainer↔诊断评测这条。OPD 是否失效由诊断侧的 G 组探针判定;MindTrainer 只负责把 OPD 系数和蒸馏损失如实记进训练曲线,供后续配方调整,**不回头读诊断结果**。
2. **复用 `simulator.datamodel`、`simulator.store_adapter`、`simulator.dataset` 是允许的。** 受限的是子系统间的运行时调用,不是共享数据结构(Plan 3 的动态轨已直接调 `run_episode`)。`Episode`/`TurnRecord`/`RoleBinding` 直接复用,不另造轨迹模型。

---

## 1. 已落地的前置修复(本设计依赖)

评审走查三个场景时发现的缺口,已在分支 `fix/design-review-gaps`(提交 `928e88e`)修复,本设计直接建立在其上:

| 修复 | 位置 | MindTrainer 怎么用 |
|---|---|---|
| `RoleBinding.model_ref`(可选):调用方显式指定对手是哪个冻结模型 | `simulator.datamodel`、proto、codec、校验 | Interact 的 `bindings` 里非训练角色位必须带它;落盘的 Episode 因此记着对手是谁 |
| `inner_thought`/`activation_trace` 不跨角色:后端只能看到自己角色位的这两项;服务端也不把对手的下行给客户端 | `simulator.runner`、`realtime.servicer` | 策略看到的历史天然符合该约定;落盘的 Episode 两边都保留 |
| 审核门证据化:候选无诊断报告时 `reconcile` 产出 `request_diagnostic`,且不放行晋级、不占 `zengen` 版本号;CLI `approve` 无报告则拒绝,`review` 打印证据 | `controlplane.reconcile/daemon`、`cli/src/commands.ts` | MindTrainer 注册的候选走同一道门 |
| 晋级时把候选的 `checkpoint_ref` 带进 `zengen_model`;`latest_promoted_checkpoint(store)` 解析下一轮基座 | `controlplane.daemon`、`controlplane.models` | 下一轮 directive 的 `base_checkpoint_ref` 由它解析,不靠人手填 |
| 数据集清单与分组数据集生产方:`DatasetManifest`、`build_manifest`/`verify_manifest`、`episode_group_id`、`produce_grouped_dataset` | `simulator.dataset` | Batch 轨迹源消费清单;GRPO 分组键来自 `episode_group_id` |

另有 Plan 4 已合入 `main` 的实时环境服务(`EnvironmentServiceClient`、创建校验、资源上限)是 Interact 的基础。

---

## 2. 数据模型

所有产物走 `Envelope` + `GitStore.write_product`,读取走 `read_product`(内部经 `migration.load_product`)。新增产物类型 `dataset_manifest`、`training_run` 无需改 schema(`type`/`status` 是开放字符串,已核实)。

### 2.1 `TrainingConfig`(不可变,内联进 directive payload)

```python
@dataclass(frozen=True)
class RewardConfig:
    components: list[str]                       # 奖励分量,如 ["goal", "norm"]
    attribution: str                            # "outcome_split" | "judge"
    combination: str                            # "sum" | "product"
    outcome_rules: dict[str, list[str]] = field(default_factory=dict)  # outcome_split 专用:分量 -> 关键词列表
    gamma: float = 1.0                          # outcome_split 专用:1.0 各训练轮同分;<1 越靠结尾越高
    judge_model_id: str | None = None           # judge 专用
    rubric_version: str | None = None           # judge 专用,必填(rubric prompt 版本化)

@dataclass(frozen=True)
class TrainingConfig:
    mode: str                                   # "batch" | "interact"
    trained_slot: str                           # 被训练的角色位;两种模式都必须明确
    base_checkpoint_ref: BigRef                 # 起点权重
    teacher_model_ref: BigRef                   # OPD 教师;必须是带 sha256 的 BigRef,不是字符串
    stage_manifest_refs: dict[str, str]         # {"sft": manifest id, "grpo_opd": manifest id};interact 下可为空
    # interact 专用
    scenario_ids: list[str]
    bindings: list[RoleBinding]                 # 复用 simulator.datamodel.RoleBinding
    max_turns_per_episode: int
    max_retries_per_episode: int                # 默认 2
    # 训练阶段
    sft_steps: int                              # 0 = 跳过冷启动
    grpo_steps: int
    sft_batch_size: int
    groups_per_step: int                        # 每个 GRPO step 吃几个完整组
    group_size: int                             # 每组样本数,>=2;interact 下即每个 scenario 连续 rollout 条数
    min_group_size: int                         # 默认 2,GRPO 阶段启动前校验用
    grpo_config: dict                           # 学习率等,透传给执行器
    opd_coefficient: float                      # >=0,spec §3.2 明确要求可调
    checkpoint_every_steps: int
    allow_resume: bool
    max_resume_attempts: int                    # 默认 3
    reward: RewardConfig
```

**说明**:注释里写"默认"的字段(`max_retries_per_episode`=2、`min_group_size`=2、`max_resume_attempts`=3)在实现里给默认值并排到字段末尾(数据类要求有默认值的字段在后),草图为可读性按语义分组。配置放在 directive payload 的 `training_config` 键下——现有 `read_tree_state` 只读 `target_dimension` 与 `recipe_ref`,多一个键不受影响。

**纯函数**

- `validate_training_config(cfg) -> str | None`,返回第一条违规原因。检查:
  - `mode` 合法;`trained_slot` 必须在 `bindings` 中且其 `model_backend == "self_play"`(interact);
  - interact 下非训练角色位的 `model_backend` 必须是 `frozen_reference` 或 `external_api`,且**必须带 `model_ref`**(`self_play` 不得带 `model_ref`,Plan 4 的校验已强制;一个会话只有一个客户端驱动的角色位)——事后追查 reward hacking 要靠它;
  - `sft_steps`/`grpo_steps` 非负且和 ≥1;`group_size >= min_group_size >= 2`;
  - `sft_steps > 0` 必须有 `stage_manifest_refs["sft"]`;`mode == "batch"` 且 `grpo_steps > 0` 必须有 `stage_manifest_refs["grpo_opd"]`;
  - `opd_coefficient >= 0`;奖励配置自洽(`outcome_split` 的 `outcome_rules` 覆盖全部分量;`judge` 必须有 `judge_model_id` 与 `rubric_version`)。
- `config_hash(cfg) -> str`:对规范化 JSON(键排序、无空白)取 sha256。
- `phase_at(step, cfg) -> "sft" | "grpo_opd"`:全局 step 计数,阶段边界在 `sft_steps`;`sft_steps == 0` 时从第一步起就是 GRPO。阶段**不另存**,由它推出,避免两处不一致。

### 2.2 运行期数据

```python
@dataclass
class TrajectoryBatch:
    episodes: list[Episode]                     # 直接复用 simulator.datamodel.Episode
    metadata: dict                              # batch: manifest_id/manifest_hash/episode_count
                                                # interact: episode_ids/failed_attempts

@dataclass
class RewardedTrajectory:
    episode: Episode
    group_id: str                               # = simulator.dataset.episode_group_id(episode)
    per_turn_rewards: list[float | None]        # 与 episode.turns 对齐;None = 非训练角色位的轮次
    combined_reward: float                      # 训练轮逐轮奖励的均值

@dataclass(frozen=True)
class StepResult:
    step: int
    phase: str                                  # "sft" | "grpo_opd"
    metrics: dict[str, float]

@dataclass
class TrainingRun:                              # 产物 type="training_run"
    run_id: str
    config_hash: str
    checkpoint_step: int                        # 最近中间 checkpoint 对应的全局 step;从未存点则为 0
    last_checkpoint_ref: BigRef | None
    resume_attempts: int
    curve_summary: dict                         # 摘要;逐步序列不进 git(补充 D.2)
    metric_stream_refs: list[BigRef]            # 键名必须是 metric_stream_refs,rismforge trace 读的就是它
    # status: pending(运行中)| executed(成功)| failed(确定性失败,保留);失败时 payload 带
    # failure: {category, point, detail}。完整 FailureRecord 产物(补充 E.5)不在本计划。
```

### 2.3 产物与路径

| 产物 | 路径 | 状态 |
|---|---|---|
| `dataset_manifest`(已落地) | `{run_dir}/dataset_manifest_{name}.yaml`,`name ∈ {sft, grpo_opd}` | `executed`;**MindTrainer 只读,不改状态**——同一清单被多次训练是预期用法(配方要靠它精确复现),标成 consumed 会误导,状态迁移留给 ControlPlane |
| `training_run` | `{run_dir}/training_run.yaml` | `pending` → `executed` \| `failed`,沿用非审核类状态机,负面状态保留 |
| `candidate_checkpoint` | `{run_dir}/candidate_checkpoint.yaml` | `pending_review`(沿用现有形状) |

**为什么放 run 目录顶层**:`GitStore.list_products` 只扫 `*.yaml`、不递归,`rismforge trace` 靠它,放进 `episodes/` 这类子目录就看不到了。`controlplane/paths.py` 需新增 `training_run_file(run_id)`、`candidate_checkpoint_file(run_id)`(后者目前在 daemon 里是内联字符串)。

---

## 3. 轨迹源

下游只依赖一个方法:

```python
class TrajectorySource(Protocol):
    def collect(self) -> TrajectoryBatch: ...
```

### 3.1 `BatchTrajectorySource(store, manifest)`

- 直接调 `simulator.dataset.verify_manifest`:逐条读 episode,用规范化 JSON 重算 sha256 比对清单,再重算整体 `manifest_hash`。**不依赖 YAML 排版。**
- 任何不一致抛 `DatasetIntegrityError`,不静默跳过;空清单也抛。
- `metadata` 带 `manifest_id`、`manifest_hash`、`episode_count`,让一次训练能追溯到精确数据集。

### 3.2 `InteractTrajectorySource(client_factory, policy, store, config)`

- Plan 4 约定一条 gRPC 流只承载一次会话,所以**每条 rollout 用 `client_factory()` 新建客户端**。
- `policy` 复用 `simulator.backends.ModelBackend` 协议,不新造接口。V1 用 `ScriptedBackend` 测试,真实模型后置。
- 单条 rollout:

```
client.create_session(...)                    # 客户端后说时 opening_turns 已收到
while not client.closed:
    utt, thought = policy.respond(persona, scenario, client.turns, trained_slot)
    client.submit_turn(utt, thought)
episode = client.drain_until_end()
```

- scenario、personas 经 `read_scenario`/`read_persona` 从 ControlPlane 取。
- **分组**:每个 scenario 连续 rollout `group_size` 条,共享同一个 `RoleAssignment`,故 `group_id` 相同。每个 GRPO step 收集 `groups_per_step` 个 scenario 的组,scenario 按 `scenario_ids` 轮转。V1 顺序执行(服务端已支持并发,并行留给后续)。
- `episode` 由服务端的 `on_session_closed` 钩子落盘(调用方配置);MindTrainer 自身不写 episode,只在 `metadata` 里记 episode id 列表。

**对 `EnvironmentServiceClient` 的两处增量(不动现有签名;目前尚未实现)**

- 只读属性 `turns`:返回当前已收到的完整历史的副本。策略需要它。
- 公开方法 `abort()`:不发 `EndSession`,直接放弃会话并释放请求流(即现有 `_abandon` 的公开形式)。服务端会按断连判 FAILED、不落盘。**策略抛异常时必须走它**——若改发 `end_session`,服务端会把这条半截 episode 当正常结束,经 `on_session_closed` 写进 ControlPlane,污染训练数据。

**失败处理**(补充 E.4:失败会话整条重跑,不续接)

- 只有 `terminated_reason ∈ {max_turns, goal_reached}` 的 episode 进 batch。超时、后端失败、断连产生的半截 episode 一律不进。
- T1 瞬态原样重跑,上限 `max_retries_per_episode`。T1 指 `timeout`、`role_backend_timeout` 和传输层 `UNAVAILABLE`。
- 其余(`invalid_session`、`role_backend_failure`)不盲重试,抛 `RolloutFailedError`,带 rollout 序号和原因。
- **`role_backend_failure` 在服务端同时覆盖"后端确定性异常"和"工厂/解析器故障",光看 reason 分不出 T1 还是 T2**,所以保守地不重试。这是已知的含糊点。
- **策略自己抛的异常不重试**:`abort()` 后直接向上抛——那是被训练模型的 bug,不是瞬态服务问题。
- 每次失败尝试写入 `metadata["failed_attempts"]`,失败是数据,不丢。

**集成注意**:服务端的 `scenario_resolver` 必须读与客户端同一个 ControlPlane(例如 `lambda sid: read_scenario(store, sid)`),否则客户端与服务端对 scenario 的理解会分歧。这属于接线计划,不在 MindTrainer 里。

---

## 4. 奖励管线

批到批的变换,每次训练迭代都可重跑(Interact 每轮取新 rollout 就重算;Batch 重读同一数据集也一样)。

### 4.1 归因阶段

```python
class AttributionBackend(Protocol):
    def attribute(self, episode, trained_slot, components) -> dict[str, list[float | None]]: ...
```

返回每个分量逐轮的分数,取值 [0,1],非训练角色位的轮次为 `None`。

**`OutcomeSplitAttribution`(规则式)**

- 先按声明式规则给每个分量算一个结果分 `o`:训练角色位任一发言含关键词则 1.0,否则 0.0(沿用 Plan 3 动态轨的 `goal_keyword` 约定)。
- 第 t 个训练轮的得分是 `o · γ^(距结尾的训练轮数)`,**不归一化**,所以仍在 [0,1]。不归一化是因为"相乘"要成立:归一化后的份额相乘没有意义,逐轮分数相乘才是门控。
- **刻意不做** `terminated_reason == goal_reached` 的规则:Plan 4 里 driver 接管了 `stop_predicate`,Interact 的 episode 永远不会以 `goal_reached` 结束,这条规则在 Interact 下恒为 0,是个陷阱。
- **也不做**客观答案类(如 HiToM):`Episode` 没有参考答案字段,这是 SocialSimulator 侧的数据问题。

**`JudgeAttribution(judge, trained_model_id)`**

- `judge` 是 `TurnJudge` 协议:`model_id` 加 `score_turn(episode, turn_index, component) -> float`。
- 只对训练角色位的轮次调用,其余填 `None`。测试用 `MockTurnJudge`,固定表缺键直接 `KeyError`,与 Plan 3 的 mock 一致。

**自评偏差守卫**:构造 `JudgeAttribution` 时,若 judge 与被训模型相同或互为前缀(双向)就拒绝。spec §3 没有明文要求,是**我加的**:训练奖励比诊断更危险,模型会被直接优化去讨好自己的 judge。这个函数只有三行,MindTrainer 里自己写一份,不 import `diagnostic.scorer` 的私有函数;两份可能漂移(Plan 3 的那份就因评审才改成双向),所以测试用同一组双向用例。它只是保守的前缀匹配,不是真正的模型血缘判断。

### 4.2 校验与组合

- `validate_attribution`(纯函数,不通过抛 `AttributionError`):分量名与配置完全一致;每个列表长度等于 `len(turns)`;`None` 恰好落在非训练角色位的轮次上;数值在 [0,1] 且非 NaN;训练角色位一个发言都没有的 episode 抛错,不静默跳过。
- `combine`(纯函数):逐轮把各分量按 `sum` 或 `product` 合成得到 `per_turn_rewards`。不做权重——分量都在 [0,1] 上,保持最小。
- `combined_reward` 取训练轮的**均值**,不取总和。总和随 episode 变长而变大,会奖励拖长对话。**这是我的选择,spec 没规定。**

### 4.3 入口

```python
def run_reward_pipeline(batch, config, backend, *, trained_slot) -> list[RewardedTrajectory]
def summarize_rewards(rewarded) -> {"mean": float, "n": int}
```

`summarize_rewards` 的结果写进训练曲线摘要。spec §2.5.6 的"训练奖励 vs 诊断分背离"检测靠这条曲线,MindTrainer 只记录,不读诊断。

---

## 5. 训练执行器

包放在 `src/mindtrainer/`,沿用 `src/diagnostic`、`src/simulator` 的命名。执行器**有状态**,对应 ROLL 里持有权重的训练进程:

```python
class TrainingExecutor(Protocol):
    def load(self, checkpoint_ref: BigRef) -> None: ...
    def sft_step(self, episodes: list[Episode], *, trained_slot: str) -> StepResult: ...
    def grpo_opd_step(self, rewarded: list[RewardedTrajectory], *,
                      opd_coefficient: float, teacher_model_ref: BigRef) -> StepResult: ...
    def save_checkpoint(self, tag: str) -> BigRef: ...
    def checkpoint_intact(self, checkpoint_ref: BigRef) -> bool: ...   # 能否重建该 checkpoint;供 decide_resume
```

- **GRPO 与 OPD 只有一个方法**:spec §3.2 明说两者作用在同一个训练 step 里,不是先后阶段。
- **SFT 可跳过**:`sft_steps=0` 就不调 `sft_step`。SFT 的目标是 `trained_slot` 的 `utterance`;`inner_thought` **V1 不作为训练目标**(spec 没说,我的选择)。
- **指标名是契约**,后续做"训练奖励 vs 诊断分"背离检测靠它们稳定:
  - SFT:`sft_loss`;
  - GRPO+OPD:`grpo_loss`、`opd_loss`、`opd_weight`、`total_loss`、`mean_reward`、`n_groups`、`n_degenerate_groups`。
- **`opd_weight` 记的是实际生效的系数。** MindTrainer 无法自证 OPD 是否起作用,那是诊断侧 G 组探针的事,它只负责如实记录。

### 5.1 分组与优势(纯函数,`grouping.py`)

- 分组键是 `RewardedTrajectory.group_id`(来自 `simulator.dataset.episode_group_id`:scenario + 每个 binding 的 `(slot, persona, backend, model_ref)`,**换了对手模型就不是同一组**;`max_turns` 不入键,它是预算不是上下文)。
- `group_advantages`:组基线取 `combined_reward` 的组内均值,优势 `reward − mean`。**不除标准差**——那是真实执行器通过 `grpo_config` 自己决定的事。
- 两种退化要分开对待:**组太小**(成员数 < `min_group_size`,含只有 1 条)时优势恒为 0,训练会静默地什么都学不到,所以是**错误**;**组内零方差**(成员数够、但奖励全相同)是数据的正常情形,**不报错**,只计入 `n_degenerate_groups` 供观察。
- `validate_groups(groups, min_group_size)`:有组太小就抛 `DegenerateGroupError`,**列出这些组的 id**,不静默训练;零方差组不触发它。调用时机:Batch 在 GRPO 阶段启动前对整份数据集校验一次;Interact 每步对刚收集的组校验(`group_size >= min_group_size` 已由配置校验保证,这里只是兜底)。

### 5.2 Batch 模式下的 GRPO:离线分组(已选方案 A)

GRPO 本来要用当前策略对同一上下文采样一组;静态数据集不是当前策略采的。选定**离线分组 GRPO**:

- 数据集里同一上下文要有 ≥2 条 episode,按 `group_id` 分组算优势;GRPO 阶段启动前过 `validate_groups`。
- 好处:与 spec"SFT→GRPO→OPD"字面一致;两种模式共用同一套 `group_id` 契约;失败时报错而不是白训。
- **代价一**:这对批量数据的生产方是新要求。SFT 要"每 scenario 一条好样本",GRPO 要"每 scenario ≥2 条且有差异",所以 SocialSimulator 要为同一 run 产出**两份**数据集,清单按阶段分开给。生产方已作为 `simulator.dataset.produce_grouped_dataset` 落地,但**目前没有任何编排调用它**。
- **代价二**:严格说这不是 on-policy。ROLL 怎么处理离线分组数据(比如重要性比率裁剪还有没有意义),我**没有核实**,真实接入前必须查(见 §10)。

被否决的两条:B(训练时在数据集上下文重采样)会把 Interact 搬进 Batch,模式边界就塌了;C(退化成奖励加权 SFT 加 OPD)不是 GRPO,配方里的 `grpo_config` 对它没有意义。

### 5.3 Mock 执行器

确定性,不训练任何东西。

- **来自输入的量是真的**:组统计、`mean_reward`、`opd_weight`。
- **损失值是 step 的合成函数**,代码里直接注明"不是学习证据"。
- **checkpoint 是哈希链**:每步 `sha256(上一步哈希 ‖ 本步数据)`,`BigRef` 的 sha256 就是当前状态。所以 `load(ref)` 无需任何文件就能还原状态,续训可以精确测试。
- 没 `load` 就训练抛 `ExecutorNotLoadedError`。

---

## 6. Checkpoint 管理器、TrainingRun 与续训

### 6.1 Checkpoint 管理器

- **格式转换**:`CheckpointConverter` 协议(`convert(ref) -> BigRef`)加确定性 mock。mock 只改 uri 后缀,sha256 取 `sha256(原哈希 ‖ "hf")`,体现转换后是另一份制品。真实的 ROLL→HF/vLLM 转换后置。
- **注册**:`register_candidate(...)` 写 `candidate_checkpoint.yaml`,形状沿用现有 `mind_trainer_stub`:
  - `type="candidate_checkpoint"`,`status="pending_review"`,`id="cand-{run_id}"`;
  - payload:`checkpoint_ref`(**转换后的**引用,晋级时会被带进 `zengen_model`)、`train_curve_summary`,另加 `training_run_ref`、`base_checkpoint_ref`、`config_hash`;
  - `links = [run_id, 各阶段数据集清单 id]`。
- daemon 读候选的 `id`、`status`、`run_id`;晋级时还会把候选 payload 里的 `checkpoint_ref` 原样带进 `zengen_model`(缺失时不编造,`latest_promoted_checkpoint` 解析时才明确报错)。所以候选 payload 的 `checkpoint_ref` 键名与形状是**契约**,不能改;daemon 和现有端到端测试都不用为它改。
- **未决:该键放哪种格式。** 候选的 `checkpoint_ref` 按本设计是转换后的推理格式(诊断评测和 MindAgent 要加载的就是它),而下一轮训练的基座又是从它解析出来的。ROLL 能否直接加载该格式作为训练起点,我没核实(§10 第 9 条)。若不能,候选需要同时带一个 ROLL 格式的 `training_checkpoint_ref`,晋级与 `latest_promoted_checkpoint` 要多带、多解析一个键。
- `write_product` 对逐字节相同的内容是空操作,崩溃后重放注册既不报错也不产生新提交。

### 6.2 续训判定(纯函数)

```python
decide_resume(run, config, *, checkpoint_intact: bool | None) -> ResumeDecision
# action ∈ {"noop", "resume", "restart", "escalate"}      escalate 对应 T3(需人工)
```

按顺序判定:

1. `executed` → `noop`。
2. `failed` → `escalate`。失败已落盘,不自动重跑(补充 E.3 机制三)。
3. `resume_attempts >= max_resume_attempts` → `escalate`。否则每次续训都在同一步崩溃,会无限循环。
4. `allow_resume` 为假 → `escalate`。
5. 有 `last_checkpoint_ref`:`checkpoint_intact` 为 True → `resume`(带引用和 `checkpoint_step`);为 False 或 `None`(无法确认)→ `escalate`,按中间态损坏处理。
6. 没有任何 checkpoint(崩在第一次存点之前)→ `restart`,从 `base_checkpoint_ref` 的 step 0 重来。

说明:

- **第 6 条是我的解读。** spec 写的是"允许就续,不允许或损坏就 T3",没覆盖"还没存过点"。这时什么都没丢,从头跑等价于新运行,判 T3 反而让人去处理一件没坏的事。
- **补充 E.2 与 E.4 有一处张力**:E.2 把"训练崩溃"列为 T3、不自动重跑,E.4 又说允许就从中间 checkpoint 续。我按"E.4 是训练崩溃的专门规则,T3 是续不了时的结局"来读。
- **`failed` 只由确定性失败写入**:数据集哈希不符、组不足、归因不合法这类直接写 `failed`(`category="T2"`),不进续训判定,因为同样的输入还会再失败。
- **`checkpoint_intact` 由调用方算**:计算层算好传进来,控制层只判"该不该续、从哪续",对应 E.4 的分工。V1 里它来自 mock 执行器能否重建该哈希。

### 6.3 确定性分批

`batch_for_step(items, step, size)`:按固定顺序轮转取,`[items[(step*size + i) % n] for i in range(size)]`,回绕时同一批内样本可能重复。要让"续训等价"可测,每个 step 用的数据必须是 step 的纯函数。

- **这只对 Batch 成立。** Interact 的数据是实时 rollout,不是 step 的函数,spec §5.1 说的"高度相似而非字节一致"才是它的上限。续训等价测试只对 Batch 做,**不声称 Interact 也成立**。

---

## 7. 编排器、存储适配与打包

### 7.1 `run_training`

薄编排,不含判定逻辑,只调下层纯函数和协议(沿用 Plan 3 `run_diagnostic` 的做法):

```python
def run_training(cfg, *, store, run_id, make_sources, attribution, executor, converter,
                 resume_from: TrainingRun | None = None,
                 metric_stream_refs: list[BigRef] | None = None,
                 on_step: Callable[[StepResult], None] | None = None) -> TrainingRun
```

**实现时的两处调整(写实施计划时发现)**

- `source` 改为 **`make_sources` 工厂**:`Callable[[int], dict[str, TrajectorySource]]`,参数是已完成的 GRPO step 数,返回 `{"sft": 源, "grpo_opd": 源}`。原因是 Interact 的轨迹源续训时要从正确的 scenario 轮转位置起步(`InteractTrajectorySource` 的 `start_step`),而 SFT 与 GRPO 本来就不是同一个源。
- 多了 `metric_stream_refs`(调用方写好指标流文件,只把引用交给编排器记录)和 `on_step`(逐步指标不进 git,这是调用方观察每一步的唯一出口)。

1. **起点**:先 `validate_training_config`。新运行 `load(base_checkpoint_ref)` 从 step 0 起;续训 `load(last_checkpoint_ref)` 从 `checkpoint_step` 起。写 `training_run`(`pending`)。
2. **SFT 阶段**(步数不足 `sft_steps` 时):取 `batch_for_step` 的数据,调 `sft_step`。
3. **GRPO+OPD 阶段**:`validate_groups` 不通过直接抛 `DegenerateGroupError`(时机见 §5.1:Batch 阶段启动前校验整份数据集,Interact 每步校验刚收集的组);每步取 `groups_per_step` 个完整组,经奖励管线得到 `RewardedTrajectory`,调 `grpo_opd_step`。
4. **周期存点**:每 `checkpoint_every_steps` 步 `save_checkpoint`,把引用和 `checkpoint_step` 回写 `training_run`。
5. **收尾**:最后一步 `save_checkpoint`,经转换器转格式,`register_candidate`,`training_run` 置 `executed`。

**失败处理**

- **确定性失败**(数据集哈希不符、组不足、归因不合法、配置非法):写 `training_run` 为 `failed`(`failure.category="T2"`),然后把原异常重新抛出。
- **其余异常**:不吞。`training_run` 保持 `pending`,让 `decide_resume` 在下次进入时判定。MindTrainer 不替它假装知道原因。

**入口的两种形态**:`run_training` 本身不判断"要不要续"。另给 `resume_or_run(...)`:读已有 `training_run`,调 `decide_resume`,`noop` 直接返回、`resume` 带记录进入 `run_training`、`restart` 当新运行、`escalate` 抛 `NeedsHumanError`(带原因)。

### 7.2 存储适配

`write_training_run`/`read_training_run`、`register_candidate`。全部走 `Envelope` + `GitStore.write_product`,读取走 `read_product`,不直接反序列化;时间戳沿用既有的固定常量写法。`dataset_manifest` 的读写已在 `simulator.dataset`,不重复。

### 7.3 打包

`pyproject.toml` 的 wheel 包列表加 `src/mindtrainer`。**这里 Plan 3 漏过一次,而且那次修复只改了一行 pyproject、没留任何测试。** 所以必须加防回归测试:用 `tomllib` 读 `pyproject.toml`,断言 `src/` 下每个带 `__init__.py` 的顶层目录都在包列表里,以后任何新增顶层包漏登记都会直接失败。

---

## 8. 场景走查结论

对架构文档的三个场景逐步对照设计与代码,发现的问题及处置:

| 编号 | 场景 | 问题 | 处置 |
|---|---|---|---|
| A | 一 | Batch 链路无数据集生产方,SFT 与 GRPO 的数据要求互相冲突 | **已落地**:`simulator.dataset`;清单按阶段分开(§5.2) |
| B | 一 | 候选出现后没有任何事件触发诊断评测 | **部分落地**:`request_diagnostic` 动作与 stub 报告已有;**真实诊断的调用(`run_diagnostic`)未接**,属接线计划 |
| C/5 | 二 | 对手"冻结 zengen-v1"在协议里无处表达;`model_backend` 服务端从不读;事后无法追查对手是谁 | **已落地**:`RoleBinding.model_ref` |
| 3 | 一/二 | 审核门看不到诊断证据,`review` 只打印一行 | **已落地**:`review` 打印证据,`approve` 无报告拒绝,`reconcile` 兜底 |
| 4 | 一/三 | 晋级后无法从 `zengen-vN` 解析出 checkpoint,下一轮基座只能人手填 | **已落地**:晋级带 `checkpoint_ref` + `latest_promoted_checkpoint` |
| 6 | 横切 | `inner_thought` 泄漏给对手 | **已落地**:约定为"角色只读自己的" |
| 7 | 横切 | OPD 教师模型是字符串,配方不可复现 | **本文落地**:`TrainingConfig.teacher_model_ref: BigRef`(§2.1) |

---

## 9. 测试策略

沿用"一模块一文件",全部 `tmp_path` 隔离,不需要 GPU、ROLL、网络。

| 文件 | 内容 |
|---|---|
| `test_mt_datamodel` | 往返序列化 |
| `test_mt_config` | `validate_training_config` 各违规分支;`config_hash` 随配置变;`phase_at` 边界(含 `sft_steps=0`) |
| `test_mt_trajectory_batch` / `test_mt_trajectory_interact` | Batch:篡改、整体哈希不符、空清单;Interact:与直接 `run_episode` parity、客户端后说、策略抛异常后探针没收到半截 episode、T1 重试与 `failed_attempts` |
| `test_mt_reward_pipeline` | §4 全部用例:校验各拒绝分支;规则归因 γ=1/γ<1/不命中/对手轮为 `None`;judge 归因含缺键会响与自评守卫的相同/judge 是前缀/被训模型是前缀/独立 id 四种;`sum`/`product` 数值;长短两条 episode 的 `combined_reward` 无长度偏置 |
| `test_mt_grouping` | `group_advantages` 已知数值;单条组与零方差组计为退化;`validate_groups` 列出组 id |
| `test_mt_executor` | 联合 step 同时报告 `grpo_loss` 与 `opd_loss`;系数为 0 时 `opd_weight` 为 0 但 `opd_loss` 仍报告;checkpoint 哈希链;未 `load` 抛错;**续训等价(Batch)** |
| `test_mt_checkpoint` | 注册后能被 daemon 读成 `CandidateView`;重放注册不产生新提交;转换 mock |
| `test_mt_resume` | `decide_resume` 全部分支(含 `resume_attempts` 刚好等于上限);`batch_for_step` |
| `test_mt_orchestrator` | 端到端 Batch;端到端 Interact(沿用 Plan 4 的真实 loopback 服务端);崩溃后续训;确定性失败写 `failed` |
| `test_mt_packaging` | wheel 包列表防回归 |

另有 `test_mt_training_run`(训练记录往返、非审核类状态机、`trace` 读得到指标流引用)与 `test_mt_integration`(下面两条集成测试)。

**续训等价(Batch)**:固定数据下,一次跑 N 步,与跑 k 步、存点、新执行器 `load` 后再跑 N−k 步,最终 checkpoint 的 sha256 相同;再加一种情形——在 checkpoint 步之后又多跑几步才崩,续训应回到 checkpoint 步重做,最终哈希仍相同。

**两条集成测试**

1. 用 MindTrainer 注册的候选跑 `reconcile_once`:确认无需改动调和循环,就能依次走 `request_diagnostic` → `await_review` → 批准 → 晋级,且 `zengen_model` 带上 `checkpoint_ref`。
2. 晋级后调 `latest_promoted_checkpoint`,把结果作为下一轮 `base_checkpoint_ref` 起一次新训练。

**变异验证**:沿用本仓库在 Plan 4 收尾时的做法——对每条关键修复植入一个回归,确认对应测试会失败,避免空转的断言。

**对 Plan 4 客户端的改动**(`turns`、`abort()`)另带各自的测试,并含一条"策略抛异常 → `on_session_closed` 探针没收到半截 episode"的回归。

---

## 10. 未核实项与已知含糊点

这几条我没法在本机核实,设计里按下面的假设写,**真实接入前必须查**:

1. **离线分组 GRPO 在 ROLL 上的语义**(排第一)。ROLL 对非 on-policy 的分组数据怎么处理,我没有核实。本文只保证分组契约和退化组的显式报错,**不保证算法正确性**。
2. **`role_backend_failure` 分不出 T1 和 T2**(§3.2)。重试因此保守。
3. **`combined_reward` 取均值**、**自评偏差守卫**、**`inner_thought` 不作 SFT 目标且"角色只读自己的"**:三处都是我的选择,spec 没规定。
4. **补充 E.2 与 E.4 的张力**,我按 E.4 读(§6.2)。
5. **keepalive 没验证**(Plan 4 遗留):Interact 长跑 rollout 时,死连接能否被及时发现没有测试证明。
6. **审核门证据只保证在场,不保证被读过、也不保证可信**:`has_diagnostic` 只检查文件是否存在;骨架里写的是带 `stub: true` 的占位报告,`review` 会标 `[STUB]`,但批准它的候选仍会放行;手写一个空壳报告同样能放行。真实诊断接入前,这道门是"形式在场"。
7. **Interact 的 episode 永远不会以 `goal_reached` 结束**(driver 接管了 `stop_predicate`),所以没有任何依赖它的奖励规则。如果将来要让实时会话支持目标达成早停,是对 Plan 4 的改动。
8. **对手模型的版本只通过 `model_ref` 字符串记录,没有制品锚定**(没有 sha256)。RecipeStore 落地时应改成可校验的引用。
9. **晋级链路里 checkpoint 的格式**(§6.1)。候选带的是转换后的推理格式引用,下一轮训练的基座由它解析而来。ROLL 能否直接加载推理格式权重作为训练起点,我没核实;若不能,要加 `training_checkpoint_ref`,并改 daemon 晋级与 `latest_promoted_checkpoint`。mock 执行器不区分格式,所以测试暴露不出这个问题。

---

## 11. 明确不做(推迟)

- 接入 ControlPlane 调和循环、替换 `mind_trainer_stub`(接线后置,含"候选出现 → 调真实诊断"和"`scenario_resolver` 读同一 ControlPlane")。
- 真实 ROLL 训练执行器、真实 LLM 归因、真实格式转换。
- RecipeStore 与 `recipe_ref`;完整 `FailureRecord` 产物;`CostRecord` 成本记账。
- 编排层调用 `produce_grouped_dataset`(生产方函数已有,没有谁在驱动它)。
- Interact 的并行 rollout;真实策略模型作为 `policy`。

---

## 12. 规模与拆分

约 10 个源文件(`datamodel`、`training_config`、`trajectory_source`、`reward_pipeline`、`grouping`、`executor`、`checkpoint`、`resume`、`store_adapter`、`orchestrator`;`TrainingRun` 放在 `datamodel`,`batch_for_step` 放在 `trajectory_source`)、约 10 个测试文件,另有对 Plan 4 客户端的两处增量改动,实际写进两份计划的新测试约 331 条(上半段 223、下半段 108;设计阶段的估计 80 到 110 条偏低,因为每条违规分支各占一个参数化用例)。

这是个大计划,建议执行时拆成两段,中间过一次评审:

- **第一段**:数据模型、配置校验、轨迹源(含客户端 `turns`/`abort()`)、奖励管线、分组(§2–§5.1)。
- **第二段**:执行器、checkpoint、续训、编排、打包、集成测试(§5.2–§7)。
