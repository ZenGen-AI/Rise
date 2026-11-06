# 闭环集成设计:把 RecipeStore、TrainScheduler、TrainPlanner、SimpleActio、MindAgent 接进调和循环

> 状态:设计稿。日期 2026-10-09。基线:`main` 在 `0385004`,全量测试 820 通过。
> 上游设计:`2026-09-22-rise-architecture-design.md`、`2026-09-24-rise-subsystem-design.md`、`2026-10-03-mindtrainer-design.md`。
> 修订(2026-10-11):八项设计决策已写进 `docs/design/`(SYS-14 至 SYS-21),本稿下列各处随之改正,其余不变;与本稿冲突时以 `docs/design/` 为准。证据等级失败关闭,`backends` 缺失判 `stub`,`run_diagnostic`、`write_report`、`resolve_baseline` 的 `backends` 必填(DX-12,见 R17、§3.1);`plan_next_round` 在启用提名时还要等配方入库门(CP-11,见 §3.2);晋级幂等、仓库文件锁、提交只带本文件(CP-7、CP-12);桥接子进程默认隔离(BR-8,见 §5.1)。

## 0. 这份文档做什么

七个子系统的详细设计已经写完。ControlPlane、SocialSimulator(含诊断评测与实时服务)、MindTrainer 已落地,MindTrainer 也已接进调和循环。RecipeStore、TrainScheduler、TrainPlanner、MindAgent,以及它们依赖的共享组件 SimpleActio 与外部推理桥接,既没有计划,也没有代码。

本文不重写旧设计。它只做三件事:

1. 把旧设计逐项放到现有代码上核对,列出落不了地的缺口(§1)。
2. 对每个缺口做裁决并写明代价(§2 是总表,§3 至 §10 是展开)。
3. 钉死各份实施计划之间的接口契约和执行顺序(§11、§12),让每份计划可以独立撰写、依次实施。

## 1. 现状与缺口

### 1.1 已落地与未落地

| 子系统 | 现状 | 证据 |
|---|---|---|
| ControlPlane | 已落地。纯函数 `reconcile`、守护循环、派发缝、审核门①、`trace`/`cost` 只读函数 | `src/controlplane`、`cli/src/commands.ts` |
| SocialSimulator | 已落地。批量模块、实时 gRPC 服务、诊断评测编排器 | `src/simulator`、`src/diagnostic` |
| MindTrainer | 已落地并接线,batch 端到端可跑 | `src/mindtrainer`、`scripts/run_controlplane.py` |
| 外部推理桥接 | 只有配置加载与脱敏函数,没有桥接本身 | `src/controlplane/config.py` |
| SimpleActio | 没有。`activation_trace` 字段在协议里,runner 写死 `None` | `src/simulator/runner.py` |
| MindAgent、RecipeStore、TrainScheduler、TrainPlanner | 没有 | 仓库内无对应包 |

### 1.2 缺口

- **G1 没有"事件"。** 调和循环只轮询,纯函数 `reconcile` 只认 directive 与候选 checkpoint。MindAgent 的"晋级事件订阅"、TrainScheduler 的"诊断报告触发"、配方提名的"轮次结果触发"都没有载体。
- **G2 激活轨迹没有通路。** 协议里 `SubmitTurn` 只有 `utterance` 与 `inner_thought`,runner 对所有轮次写 `activation_trace=None`。完整 SimpleActio 扮演的角色,其技能路由信息进不了落盘的 episode,经验分析器就没有输入。
- **G3 正式技能/记忆库没有存放位置。** 目录树只有 `skill_memory_proposals/`,没有"已纳入的库"。
- **G4 指令确认态不存在。** 非审核类状态机只有 `pending / executed / consumed / failed`,旧设计 6.3 要求的"待确认"没有对应状态。
- **G5 审核命令只认候选 checkpoint。** 命令行用 `candidate_checkpoint.yaml` 的路径模式找产物,技能提案与配方候选无法审核。
- **G6 成本、追踪、失败记录没有写入方。** `observe.trace`、`observe.cost` 只读,没有任何代码写 `run_trace.yaml`;`FailureRecord` 完全没有。
- **G7 桥接配置过窄。** `BackendConfig` 要求 `token_env` 必填,命令行形态若用工具自带的登录态就没有环境变量可填。
- **G8 诊断是桩,且没有注入缝。** `request_diagnostic` 在 `apply_action` 里直接调桩,不能像派发器那样替换。
- **G9 没有谁驱动数据合成。** MindTrainer 派发器要求数据集清单已在树里,否则指令判 `failed`。调度器自动构造的指令没有清单,必有一步先合成数据,现有代码里没有这一步。
- **G10 命令清单分在两种语言。** 旧设计 G.5 把全部命令列在 `rise` 下。现状是 TypeScript 命令行只有 `review/approve/reject/status`,Python 只有 `observe` 里的函数,没有对应的命令。

## 2. 裁决总表

每条裁决都写明"判错的代价",便于用户事后推翻。

| 编号 | 裁决 | 判错的代价 |
|---|---|---|
| R1 | 事件就是树里的状态事实。每个触发对应一个新动作,幂等键是 run 目录内的标记产物 | 每个触发多一个小文件 |
| R2 | 新动作只有在对应处理器被注入时才会产生。`controlplane` 不导入任何新子系统 | 入口要自己组装处理器 |
| R3 | 指令确认默认开启,新增 `awaiting_confirmation` 状态 | 闭环每轮停一次等人;关掉要显式配置 |
| R4 | 同一时间只允许一轮在途 | 不能并行多条训练线;换来防止失控 |
| R5 | 规划在候选到达终态(`effective` 或 `rejected`)且有可接受的诊断证据时触发。只对 run 编号最大的 run 触发(按数值序),所以较早 run 的候选不会被重复规划;指令 `failed` 不触发 | 指令失败后闭环停住,等人写新指令 |
| R6 | 配方候选放 `recipe_candidates/`,批准后由 RecipeStore 写入 `recipes/` | 多一个目录;换来"`recipes/` 只有两个写方"的边界 |
| R7 | 提名阈值默认 `min_gain=0.01` 且无退化标记,可配置 | 阈值是拍的,审核门③是兜底 |
| R8 | 配方生命周期 V1 只自动 `deprecated`(战绩出现退化),`superseded` 推迟 | 被更优配方取代的旧配方仍显示有效 |
| R9 | RecipeStore 不建 SQLite 索引,按目录扫描 | 配方到数百条以上才会慢 |
| R10 | 审核门记录就是产物自己的 `status` 与原因,不另建 `gate_*.yaml` | 与旧设计 1.2 目录树不同,但与已落地的候选 checkpoint 一致 |
| R11 | 正式技能库放 `skill_library/`,记忆库放 `memory_library/`,库版本记在 `control-plane/library_state.yaml` | 无 |
| R12 | 打通激活轨迹:`SubmitTurn` 加可选字段,runner 接受后端返回三元组 | 改协议、重生成已入库的 pb2 文件 |
| R13 | 提案验证器 V1 度量"行为是否按预期改变",不度量对话质量;质量评审可选接桥接 judge | 审核人看到的是行为证据,不是质量证据 |
| R14 | 指令携带 `data_spec`,MindTrainer 派发器缺清单时调用注入的数据生产器。V1 模板生成器与目标维度无关 | 闭环训练的数据与"目标维度"无关,只是流程跑通 |
| R15 | 桥接 V1 只实现命令行形态,API 形态只留协议。命令行参数模板放配置;`token_env` 允许显式 `null` | 没有 API 形态可用 |
| R16 | 命令拆两个入口:TypeScript 管门控写命令,Python 管调和与只读可观测 | 用户要记两个入口 |
| R17 | 诊断报告带 `backends` 标记(`mock` 或 `real`),缺标记判 `stub`(设计 DX-12)。默认不接受 `mock` 证据驱动规划与提名,开发入口用显式开关打开 | 默认配置下闭环不会自己转,开发时要加一个开关 |
| R18 | 桥接失败一律升级为 T3:`BridgeError`(T1 已用尽重试、T2 确定性)使规划决策或 agent 轮次置 `failed` 并写失败记录;非桥接的瞬态异常原样上抛 | 外部工具整体不可用时,闭环停在 `failed`,要人写新指令或手工重置;换来不会无限空转外部推理 |
| R19 | `start_agent_round` 的触发条件是"没有轮次产物**或**产物为 `pending`",与 `plan_next_round` 同一模式;轮次处理器负责把终态(`executed` / `failed`)写回,写回之前每一轮 tick 都会重新触发它,重入必须幂等;产物在但读不出的同样视同不存在,由处理器重入时写 `failed` | 若只在"没有产物"时触发,一次瞬态失败(或进程被中途杀掉)留下的 `pending` 轮次将永远不再被派发,直到有更新的模型晋级把它置为 superseded;代价是轮次处理器可能被连续唤醒,必须靠幂等键挡住重复长任务 |
| R20 | 设计与计划 5 的签名差异:计划 5 给 `nomination_check` 增加了 keyword-only 的 `accept_mock_evidence`,给 `build_recipe_candidate` 增加了 keyword-only 的 `repo_commit` 与 `accept_mock_evidence`(本节的签名已同步) | 这两项是提名时的运行期输入(是否接受 mock 证据、仓库提交号),不是配置内容,不该塞进 `config`;集成时若照旧只传 `config`,候选的 `repro` 会缺 `repo_commit`,复现校验记一条错误 `code_version.repo_commit`(计划 3 的校验器把它判为错误),代价是每个候选的 `repro` 都带上它;组合根取不到 HEAD 时更明显 |
| R21 | 诊断报告里 `error_type_distribution` 的空标签 `""` 表示"没有错误"(基题与变体都答对的变体族,`classify_static_family` 的第 6 种返回)。`assemble_context` 构造 `PlanningContext` 前把空键丢掉,不重新归一化;全是空标签时分布为空映射 | 丢掉之后分布的和可以小于 1,规划器看不到"这个二级维度答对了多少",该信息仍在 `l1_scores` 里;若不丢,`PlanningContext` 会拒绝空键,规划决策判 `failed`,于是在全部变体族上答对的候选反而让闭环停在规划这一步 |

## 3. 事件与调和循环的扩展

### 3.1 视图与动作

`reconcile` 保持纯函数。`TreeState` 新增的字段全部带默认值,既有测试的构造不受影响。

```python
@dataclass(frozen=True)
class RunView:
    run_id: str
    directive_status: str | None
    recipe_ref: str
    candidate_status: str | None
    has_diagnostic: bool
    diagnostic_grade: str | None     # "stub" | "mock" | "real" | None
    planner_decision_status: str | None
    has_nomination_note: bool
    has_track_note: bool

@dataclass(frozen=True)
class ModelView:
    model_id: str
    version: int
    run_id: str
    status: str
    agent_round_status: str | None   # None 表示没有轮次产物,或产物在但读不出;否则是它的信封状态
    # has_agent_round 是只读派生属性(@property),恒等于 agent_round_status is not None;不是构造参数,保留给旧读者读取

@dataclass(frozen=True)
class GateView:
    id: str
    kind: str                        # "recipe_candidate" | "skill_memory_proposal"
    status: str

# TreeState 新增: runs, models, gates (默认空列表),
#                 enabled: frozenset[str] = frozenset(), accept_mock_evidence: bool = False
```

`diagnostic_grade` 的判定,按顺序:payload 里 `stub` 为真是 `stub`;`backends` 是 `mock` 是 `mock`;`backends` 是 `real` 且 `dimension_scores` 是非空列表才是 `real`;其余(含缺少 `backends`、取别的值包括 `null`、没有分数)一律判 `stub`。判不出来按最弱算(设计 DX-12,失败关闭)。

### 3.2 触发条件

只有动作名在 `state.enabled` 里,`reconcile` 才会产生它。"可接受证据"指等级为 `real`,或等级为 `mock` 且 `accept_mock_evidence` 为真。

| 动作 | `target_id` | 触发条件 | 幂等键 |
|---|---|---|---|
| `plan_next_round` | run id | 候选状态是 `effective` 或 `rejected`;证据可接受;`planner_decision_status` 是空或 `pending`;没有在途轮;该 run 是 run 编号最大的 run(**按 `run-<数字>` 的数值序**;不匹配 `^run-[0-9]+$` 的名字一律排最后,不参与"最大"的角逐;树里一个 `run-<数字>` 都没有时——只可能是手工造的目录——不触发规划,闭环停在等人写指令,这是刻意的失败关闭;编号相同(如 `run-7` 与 `run-007`)按 run id 字典序取较大者,结果与输入顺序无关);启用提名且候选为 `effective` 时,还要等配方入库门:来源 run 已有提名标记,且配方候选 `recipe-<run>` 不在 `pending_review` 或 `approved`(设计 CP-11;候选 `rejected` 不等) | `runs/<run>/planner_decision.yaml` 为 `executed` 或 `failed` |
| `nominate_recipe` | run id | 候选状态是 `effective`;证据可接受;没有提名标记 | `runs/<run>/nomination.yaml` |
| `record_track` | run id | `recipe_ref` 非空;候选状态是 `effective` 或 `rejected`;证据可接受;没有战绩标记 | `runs/<run>/recipe_track.yaml` |
| `start_agent_round` | model id | 版本号(按数字)最大的 `effective` 模型,且它的轮次产物**不存在,或状态为 `pending`**;产物在但读不出(YAML 解析失败、合并冲突标记、信封缺键)视同不存在,由轮次处理器重入时自己把它写成 `failed` | 轮次产物变为 `executed` 或 `failed` |
| `admit_recipe` | 配方候选 id | `GateView.kind == "recipe_candidate"` 且状态是 `approved` | 候选变为 `effective` |
| `admit_proposal` | 提案 id | `GateView.kind == "skill_memory_proposal"` 且状态是 `approved` | 提案变为 `effective` |

**在途轮(R4)**:任何 run 的 `directive_status` 是 `pending` 或 `awaiting_confirmation`,或候选状态是 `pending_review` 或 `approved`,就算有在途轮。`plan_next_round` 只在没有在途轮时产生。

### 3.3 注入方式(R2)

```python
Handler   = Callable[[GitStore, Action], None]
Evaluator = Callable[[GitStore, str, str], None]   # (store, run_id, candidate_id)

def reconcile_once(store, *, dispatcher=None, handlers: Mapping[str, Handler] | None = None,
                   evaluator: Evaluator | None = None, accept_mock_evidence: bool = False) -> list[Action]: ...
```

- `enabled` 取 `handlers` 的键。`read_tree_state(store, *, enabled=frozenset(), accept_mock_evidence=False)` 只在 `enabled` 非空时才去填 `runs / models / gates`,默认路径与现在逐字节相同。
- 没有处理器的动作种类永远不会出现在返回的动作列表里。已有的"稳定后返回空列表"的断言因此保持成立。
- `evaluator` 缺省是现有的桩路径。注入后只替换 `request_diagnostic` 这一步。
- 处理器自己负责写终态记录。确定性失败写自己产物的 `failed` 与失败记录;瞬态异常原样上抛,与派发器的约定一致,已知限制也相同(守护循环会退出,需要外部监督)。

### 3.4 指令确认(R3)

非审核类状态机新增 `awaiting_confirmation: {pending, failed}`。`reconcile` 对这个状态的指令不产生任何动作。命令行 `confirm <directive-id>` 把它改成 `pending`。人工直接写入的指令仍然写 `pending`,不受影响。没有任何状态迁入 `awaiting_confirmation`,它只作初始状态,由调度器按 `confirm_directives` 写入;`awaiting_confirmation → failed`(人放弃)在 V1 没有命令,只能手工改文件。确认模式由 `config/scheduler.yaml` 的 `confirm_directives`(默认 `true`)决定。

## 4. 新产物、路径与状态机

### 4.1 产物清单

| 类型 | 路径 | 种类 | 状态 | 写方 |
|---|---|---|---|---|
| `planner_decision` | `runs/<run>/planner_decision.yaml` | 非审核 | `pending → executed \| failed`(规划前就失败时直接写 `failed`,不经 `pending`) | TrainScheduler 处理器 |
| `nomination_note` | `runs/<run>/nomination.yaml` | 非审核 | `executed` | TrainScheduler 处理器 |
| `recipe_track_note` | `runs/<run>/recipe_track.yaml` | 非审核 | `executed` | RecipeStore 处理器 |
| `recipe_candidate` | `recipe_candidates/recipe-<run>.yaml` | 审核 | `pending_review → approved → effective`;或 `rejected` | 提名器写;命令行改状态;RecipeStore 收录 |
| `recipe` | `recipes/recipe-<run>/recipe.yaml` | 审核 | `effective → deprecated` | RecipeStore |
| `recipe_lineage` | `recipes/recipe-<run>/lineage.yaml` | 非审核 | `executed` | RecipeStore |
| `recipe_track_record` | `recipes/recipe-<run>/track_record.yaml` | 非审核 | `executed` | RecipeStore |
| `agent_round` | `agent_rounds/agent-round-<model>.yaml` | 非审核 | `pending → executed \| failed` | MindAgent 处理器 |
| `skill_memory_proposal` | `skill_memory_proposals/proposal-<round>-<n>/proposal.yaml` | 审核 | 同候选 | MindAgent 写;命令行改状态;收录处理器收录 |
| `skill` | `skill_library/skill-<id>.yaml` | 审核 | `effective`(`deprecated` 在 V1 没有自动写方,只能手工) | 收录处理器 |
| `memory` | `memory_library/mem-<id>.yaml` | 审核 | `effective`(`deprecated` 在 V1 没有自动写方,只能手工) | 收录处理器 |
| `library_state` | `control-plane/library_state.yaml` | 非审核 | `executed` | 收录处理器 |
| `failure_record` | `runs/<run>/failure-<task>.yaml` | 非审核 | `executed` | 各处理器,经统一写入函数 |
| `run_trace` | `runs/<run>/run_trace.yaml` | 非审核 | `executed` | 统一的成本写入函数 |
| `config`(`scheduler.yaml`、`training_defaults.yaml`、`backends.yaml`、`evaluator.yaml`、`agent.yaml`) | `config/<name>.yaml` | 非审核 | `effective` | 计划 1 的 `write_config`;各自的安装函数 |
| `prompt_template` | `config/prompts/planner-<version>.yaml`(不进 `SUBDIRS`) | 非审核 | `executed` | 计划 4 的 `install_template` |
| `diagnostic_taxonomy`、`diagnostic_item_pool` | `diagnostics/taxonomy_<version>.yaml`、`diagnostics/item_pool_<version>.yaml` | 非审核 | `effective` | 计划 8 的 `diagnostic.assets` |

新增目录加进 `SUBDIRS`,追加在末尾,顺序为 `recipe_candidates`、`skill_library`、`memory_library`、`agent_rounds`。`test_subdirs_match_spec` 断言目录列表完全相等,必须同步改。

`run id` 的分配:规划处理器先把下一条指令的 run id 写进 `planner_decision`(状态 `pending`),格式 `run-NNN`(至少三位补零;超过 999 轮时仍按数值序比较,补零只为可读),取现有 `run-<数字>` 里的最大序号加一;再写指令;最后把决策置 `executed`。崩在写指令之前时,该 run 仍是最大 run,规划会再次触发,处理器看到 `pending` 的决策就按已记下的 run id 续写,不会重复分配。崩在指令已写、决策仍 `pending` 之间时,新指令成为最大 run,来源 run 不会再触发规划;装配上下文时,决策为 `pending` 而它的 `next_run_id` 指令已存在的,按 `executed` 对待。

### 4.2 关键载荷

**指令(directive)新增键**:`data_spec`(§9)、`planner_decision_ref`(决策 id,人工指令没有)。`recipe_ref` 从现在的恒空变成克隆或复现时的配方 id。

**规划决策(planner_decision)**:

```yaml
payload:
  source_run_id: run-003
  next_run_id: run-004          # 先于指令写入
  template_version: v1
  backend: bridge          # 后端的 name:bridge | heuristic | scripted
  decision: {target_dimension, action, mode, source_recipe_id, perturbation, data_synthesis_hint}
  rationale: "..."
  failure: null                 # 失败时 {category, reason, attempts}
```

**配方(recipe 与 recipe_candidate 共用)**:

```yaml
payload:
  recipe_type: batch | interact | agent_rsi
  dimension: tom
  source_run_id: run-003
  eval_anchor: {taxonomy_version, item_pool_version, benchmark_version, judge_prompt_version, scoring_config_version}  # 暂无的填 null
  result: {gain, baseline_ref, candidate_ref, promoted}
  repro: {errors: [], warnings: []}
  nondeterministic: false       # interact 恒为 true
  # batch:    training_config, stage_manifests {stage: {id, manifest_hash}}, code_version {repo_commit}
  # interact: training_config, scenario_ids, bindings, code_version
  # agent_rsi: bound_model_id, scenario_ids, persona_ids, analyzer_config, verifier_config, generator_version, prompt_template_version, uses_bridge(布尔;为真时 prompt_template_version 必填)
```

**战绩条目**:`{run_id, gain, promoted, regressed, used_as: "clone" | "replay"}`。`gain` 可能为空(没有可比的基线报告)。

**技能提案**:

```yaml
payload:
  round_id: agent-round-zengen-v3
  finding: {id, pattern, support, ratio, episode_refs: [...]}
  increments: [{kind: skill | memory, definition: {...}}]
  verification: {method: "replay_behavior", episodes: [...], fired_on_motivating: 0.9, fired_elsewhere: 0.0, notes: "..."}
```

审核门的"证据在场"规则沿用候选 checkpoint 的做法:配方候选必须带 `repro`,提案必须带 `verification`,缺了命令行拒绝批准。


**agent_round 载荷**(以计划 7 的 Ruling 14 为准):`agent-round-<model>`,`links = [model_id, run_id]`,载荷键 `model_id、model_version、run_id、checkpoint_ref(模型有时)、library_version{skill, memory}、scenario_ids、persona_ids、bindings、analyzer_config{min_support, min_ratio}、verifier_config{method, min_fired_on_motivating, max_fired_elsewhere}、generator_version、uses_bridge、prompt_template_version(仅用桥接时)、episode_ids、findings、proposals、dropped、failure`。重入时整份按当前绑定重写,保留首次的 `created_at`。被取代的旧轮次写成 `failed`,`failure.category` 为 `superseded`,不新增状态。
**其余小载荷**(设计不给示例,以各计划为准):`nomination_note` 是 `{result, reason, gain, baseline_run_id, recipe_candidate_id}`(计划 5);`recipe_track_note` 是 `{recipe_id, result, reason, entry, verdict, deprecated}`(计划 3);`library_state` 是 `{skill_version, memory_version, admitted: [...]}`,版本存成字符串(计划 7 Ruling 15)。
## 5. 外部推理桥接与横切记账

### 5.1 接口(R15、G7)

新增顶层包 `inference_bridge`,依赖 `controlplane`。

```python
@dataclass(frozen=True)
class BridgeResult:
    text: str
    input_tokens: int | None
    output_tokens: int | None
    model: str
    log_ref: BigRef | None

class BridgeError(Exception):
    category: str            # "T1" 瞬态 | "T2" 确定性

class InferenceBridge(Protocol):
    def complete(self, prompt: str, *, run_id: str, task_id: str) -> BridgeResult: ...
```

- **命令行形态**:`CliBridge` 用子进程调外部工具,参数模板、超时、重试策略来自 `BackendConfig`,不在代码里写死厂商参数。子进程默认隔离(设计 BR-8):环境变量只传白名单与 `token_env`,工作目录是每次调用新建的空临时目录;默认参数模板(关闭工具或开只读沙箱)只是常量,不自动写进配置。进程退出码非零、超时、空输出算 T1,按 `retry_policy`(最大次数、退避基数)指数退避重试;耗尽后抛 `BridgeError(category="T1")`,由调用方升级为 T3。输出格式不合法是调用方的 T2,桥接只负责取回文本。子进程启动阶段的任何 `OSError`(找不到可执行文件、无权限、不可执行)算桥接自己的 T2,不重试;凭证缺失在构造时以 `MissingCredentialError` 失败,不是 `BridgeError`。
- **测试替身**:`ScriptedBridge`,按顺序吐罐头回答,可注入失败序列。
- **配置**:`BackendConfig` 增加可选 `command`(参数列表模板)和 `timeout_s`;`token_env` 允许显式 `null`,表示该工具用自带登录态,此时不解析凭证。其余约束不变:出现凭证值字段仍然报错。
- **调用方的升级规则(R18)**:`BridgeError` 的 T1 表示桥接已用尽自己的重试,T2 表示确定性故障。TrainScheduler 的规划处理器与 MindAgent 的轮次处理器遇到任何 `BridgeError` 都升级为 T3:决策或轮次置 `failed`,经 `record_failure` 留痕,不再自动重试,等人处理。非桥接的瞬态异常(如 `OSError`)才原样上抛、保持 `pending`。原因:桥接已经退避重试过,把耗尽的 T1 留作 `pending` 会让守护循环重启后无限空转外部推理。
- **日志(L3,计划 1 的桥接日志 Ruling)**:请求与响应过 `redact` 之后写进 `<store.repo_root>/.rise/logs/<run_id>/<task_id>-<k>.log`,`k` 从 1 递增、不覆盖;日志根目录里放一份内容为 `*` 的 `.gitignore`,所以这些文件对 git 不可见,`git status` 干净,不必改动控制面仓库里任何被追踪的文件(本代码仓库的 `.gitignore` 会加一行 `.rise/`;构造 `CliBridge` 时可用 `log_dir` 改到任意位置;既没有 `store` 也没有 `log_dir` 时不写日志)。树里只留 `BigRef`(`file://` 地址、sha256 与大小);`trace` 已能识别 `log_refs`。**这是对"仓库外"的收窄**:默认值改在仓库工作区内,靠自带的 `.gitignore` 隔离。残留风险:`git add -f` 或 `git clean -fdx` 会动到它,设计接受这一点。

### 5.2 统一的记账与失败写入

```python
# controlplane/ledger.py
def record_cost(store, run_id, *, task_id, cost_type, gpu_hours=None, tokens=None, calls=None, usd=None) -> None
def record_failure(store, run_id, *, task_id, category, point, retries, reason, log_ref=None) -> str
```

- `record_cost` 向 `runs/<run>/run_trace.yaml` 的 `cost_records` 追加一条,重复的 `(task_id, cost_type)` 不重复追加。
- 值未知时**省略该键**,不写 `null`。原因:`observe.sum_costs` 用 `r.get("tokens", 0)` 求和,遇到 `null` 会抛 `TypeError`。
- 桥接每次成功调用后记一条 `cost_type="external_inference"`;`task_id` 由调用方传入。
- `record_failure` 写 `failure_record` 产物,并返回路径。

## 6. RecipeStore

新增顶层包 `recipestore`,依赖 `controlplane`。

### 6.1 可复现性校验(纯函数)

`validate_reproducibility(recipe_payload) -> ReproReport(errors, warnings)`。**错误**是未锚定的软引用或缺失的必填项;**警告**是已知的复现限制。校验结果写进候选的 `repro`,不硬拒绝,由审核门③的人参考。

| 类型 | 必须锚定 | 警告 |
|---|---|---|
| batch | `training_config` 完整;每个阶段的清单 id 与清单哈希;`code_version.repo_commit`;`eval_anchor` 的分类表与题库版本 | 无 |
| interact | `training_config`(完整性检查与 batch 相同);`scenario_ids`;`bindings` 非空,且对手 `model_ref` 都是具体 id;`code_version` | `nondeterministic` 为真,复现只能高度相似 |
| agent_rsi | `bound_model_id`;`scenario_ids`;`persona_ids`;分析器与验证器配置;生成器版本;使用桥接(`uses_bridge` 为真)时的 `prompt_template_version` | 无 |

所有引用必须是具体值。形如 `latest`、`current_best` 的相对引用算错误。

### 6.2 检索、战绩与生命周期

```python
find_recipes(store, *, dimension=None, recipe_type=None, states=("effective",), order="track") -> list[RecipeView]
get_recipe(store, recipe_id) -> Envelope
compare(store, a, b) -> dict                     # 配置差异 + 战绩对比
append_track_entry(store, recipe_id, entry) -> None   # 按 run_id 幂等
verdict(entries) -> str                          # "keep" | "deprecate"
```

- `RecipeView` 带 `id, recipe_type, dimension, status, payload, mean_gain, n_reuse`。`mean_gain` 只统计 `gain` 非空的条目;`n_reuse` 是战绩条目总数,含 `gain` 为空的条目;没有一条 `gain` 非空时 `mean_gain` 为 `None`,排在最后。
- 排序 `order="track"` 按 `mean_gain` 降序,再按 `n_reuse` 降序,再按 id。
- `verdict`(R8):只要最近一条战绩的 `regressed` 为真就返回 `deprecate`。`deprecate` 时把配方置 `deprecated`,在 `lineage` 里记原因与触发的 run。`superseded` 不自动产生。
- 不建索引(R9),直接扫描 `recipes/` 目录。

### 6.3 提名与收录

- **提名判定**(R7,在 TrainScheduler 的处理器里调用):对非 `real` 证据且未开启 `accept_mock_evidence` 的 run 不提名。增益取目标维度的一级分数,减去基线 run 同维度的分数;基线 run 从 `models/zengen-v<N>.yaml` 的 `links` 里找到。找不到可比基线(`compute_gain` 返回 `gain=None` 并写明原因:缺一环、stub 报告、跨 `benchmark_version`、real 候选对非 real 基线,见 §14 第 10 条)、增益低于 `min_gain`、或报告有退化标记,都写 `nomination_note`(`result: skipped`,带原因)而不写候选。
- **收录**(`handle_admit_recipe`):候选 `approved` 时,写 `recipe`(`effective`)、`lineage`、空的 `track_record`,再把候选置 `effective`。每一步重入安全。
- **战绩回写**(`handle_record_track`):读该 run 的诊断与决策,追加战绩条目,写 `recipe_track` 标记,再按 `verdict` 决定是否 `deprecate`。

## 7. TrainPlanner 与 TrainScheduler

### 7.1 TrainPlanner(新增顶层包 `trainplanner`)

结构沿用旧设计 7.1:上下文打包、决策后端、输出解析,三者分离。

```python
@dataclass
class PlanningContext:            # 纯数据,可序列化
    run_id: str
    candidate: {id, status, reject_reason|None}
    diagnostic: {l1_scores, regression_flags, error_profiles, benchmark_version, grade}
    recipes: list[dict]           # RecipeView 的摘要
    recent_decisions: list[dict]  # 决策 + 其后续 run 的结果
    cost_gain: list[dict]         # 近 K 轮的成本与增益

@dataclass(frozen=True)
class PlannerDecision:
    target_dimension: str
    action: str                   # "new" | "clone" | "replay"
    mode: str | None              # "batch" | "interact",仅 new 需要
    source_recipe_id: str | None
    perturbation: dict
    data_synthesis_hint: str
    rationale: str

render_prompt(ctx, template_text, template_version) -> PackedContext
parse_decision(text) -> PlannerDecision              # 严格,失败抛 DecisionParseError
class DecisionBackend(Protocol): def decide(self, packed, *, run_id, task_id) -> str
plan(ctx, template_text, template_version, backend, *, run_id, max_parse_retries=2,
     validator=None) -> PlannerResult   # validator: Callable[[PlannerDecision], str | None],违规原因拼进重试提示,与解析失败共用重试上限
```

- 后端模型必须只输出一个 JSON 对象,字段与 `PlannerDecision` 同名。解析规则:`new` 必须有 `mode`;`clone` 与 `replay` 必须有 `source_recipe_id`;`replay` 不允许 `perturbation`;`rationale` 不能为空。违反即 T2,带着错误信息重新采样,上限 `max_parse_retries`。耗尽抛 `PlannerFailedError(reason, attempts)`,不做宽容猜测。
- 提示模板是信封产物(`type: prompt_template`,载荷 `{version, text}`),放 `control-plane/config/prompts/planner-<version>.yaml`,版本号进配置并写进每个决策产物。包内带默认模板 `PLANNER_V1` 与 `install_default_template(store)`。
- 两个后端:`BridgeDecisionBackend`(用 `InferenceBridge`)和 `HeuristicBackend`。后者纯确定性:取最低的一级维度;有该维度的有效配方就 `clone` 战绩最好的一条,否则 `new` 一条 `batch`。罐头后端用于测试。

### 7.2 TrainScheduler(新增顶层包 `trainscheduler`)

```python
assemble_context(store, run_id, *, config=None) -> PlanningContext   # config 缺省时读 scheduler.yaml
validate_decision(store, decision, *, config) -> str | None      # 领域有效性,返回第一条违规
resolve_action(store, decision, *, config, defaults) -> ResolvedAction
construct_directive(store, resolved, *, new_run_id, confirm, planner_decision_id) -> str
nomination_check(store, run_id, *, config, accept_mock_evidence: bool | None = None) -> NominationVerdict
install_default_configs(store) -> list[str]                  # 缺失才写,返回写过的相对路径
build_recipe_candidate(store, run_id, *, config, repo_commit: str | None = None, accept_mock_evidence: bool | None = None) -> Envelope
make_plan_handler(*, backend, template_loader, config_loader, defaults_loader) -> Handler   # 三个 loader 每次调用各读一次,人改了配置下一次规划就生效
make_nominate_handler(*, config_loader, accept_mock_evidence=None, repo_commit=None) -> Handler   # accept_mock_evidence 非 None 时覆盖 scheduler.yaml 同名键;repo_commit 写进配方的代码版本锚
```

- **领域校验**:`clone` 与 `replay` 引用的配方必须存在且 `effective`;`perturbation` 的键必须在 `config/scheduler.yaml` 的 `perturbable` 白名单内;`new` 指令只构造 batch,`new` 加 `interact` 一律算领域违规;`allow_interact: true` 只放行 interact 配方的 `clone` 与 `replay`(默认关,避免规划器把闭环引向入口跑不了的指令)。校验不通过,把违规原因带回规划器重试,计入重试上限;耗尽则 `planner_decision` 置 `failed`,写失败记录,类别 T3。
- **动作解析**:`new` 用 `config/training_defaults.yaml` 里的完整默认 `training_config`,`base_checkpoint_ref` 取 `latest_promoted_checkpoint`;`clone` 取配方的 `training_config` 套上扰动;`replay` 原样取配方,并复用它锚定的数据集清单。
- **只构造 batch 或 interact 指令**,不构造 AgentRSI 指令,与旧设计一致。
- **规划处理器的流程**:装配上下文 → 规划 → 校验 → 先写 `planner_decision(pending, next_run_id)` → 解析(`resolve_action` 与 `construct_directive`)→ 写指令(`confirm_directives` 为真则 `awaiting_confirmation`,否则 `pending`)→ 决策置 `executed`。解析放在写出 pending 决策之后,是为了让崩在解析或构造阶段的那一轮也用同一条重入路径续写(计划 5 的 Ruling 14 记了后果:失败决策可能带着已分配但没用的 `next_run_id`)。
- **规划是同步的**。外部推理期间调和循环被阻塞,与派发器同属已知限制。

## 8. SimpleActio 与 MindAgent

### 8.1 SimpleActio(新增顶层包 `simple_actio`,只依赖 `simulator.datamodel`)

```python
instance = instantiate(model_backend, *, skills, memories, library_version, profile)   # 完整签名以计划 6 为准:另有 keyword-only 的 experiences=()、registry=None、memory_limit=3
utterance, inner_thought, trace = instance.respond(persona, scenario, history, speaker_slot)
```

- `respond` 的参数与 `ModelBackend` 相同,返回三元组,因此它既能被 runner 直接当后端用(R12),也能被 MindAgent 的客户端循环调用。
- **技能**是结构化数据:触发条件、响应策略、层级(`macro` 或 `micro`)、版本。触发条件是一个小型谓词语言:`{"signal": name}`、`{"all": [...]}`、`{"any": [...]}`、`{"not": {...}}`、`{"turn_at_least": n}`。信号是注册的纯函数,V1 内置 `emotion_high`、`no_new_information`、`first_turn`,词典是本设计拟的,可替换。
- **路由**是纯函数:多个技能同时命中时按优先级,再按版本号较新者,再按 id 排序,选一个。冲突与最终选择都写进激活轨迹。
- **记忆**:条目含主体、内容、时间戳、来源 episode、置信度、版本。V1 检索用主体、内容与关键词的词面重叠(查询词取对方最后一句话,按重叠数、置信度、id 排序,重叠为 0 的不返回),支持按时间点取快照。
- **推理经验**:只做数据模型与三级回退的纯函数,不做复杂路由。
- **与模型后端的接合**:被选中的技能策略渲染成"指导语"列表,传给一个带指导语的后端协议 `respond_guided(persona, scenario, history, speaker_slot, guidance)`。普通 `ModelBackend` 用适配器包装,适配器忽略指导语。测试用能回显指导语的罐头后端,证明技能确实改变了输出。
- **激活轨迹**:

```python
{"schema": "simple_actio.trace.v1",
 "signals": [signal_name, ...],                 # 此刻为真的已注册信号,按名字升序
 "candidates": [skill_id, ...],                 # 命中的技能,按路由次序
 "selected": skill_id | None,                   # == candidates[0];没有命中为 None
 "conflict": {"skills": [...], "rule": "..."} | None,   # 命中多个时才有
 "memory_hits": [mem_id, ...],
 "experience": {"id": exp_id, "tier": "primary|secondary|fallback"} | None,
 "library_version": {"skill": "<str>", "memory": "<str>"}}
# 叶子只有 str、None、bool,没有数字:protobuf 的 Struct 把数字存成 double,整数经往返会变成浮点。
# 精确模式以计划 6 的"对外契约:激活轨迹的精确模式"为准;库版本一律是字符串。
```

### 8.2 协议改动(R12)

- `realtime_env.proto` 的 `SubmitTurn` 增加 `optional google.protobuf.Struct activation_trace = 3`。
- 客户端 `submit_turn(utterance, inner_thought=None, *, activation_trace=None)`;服务端经驱动器与客户端驱动后端把它带到 runner。
- runner 接受后端返回二元组或三元组。三元组的第三项写入该轮的 `activation_trace`。既有二元组后端不受影响。
- 服务端回显客户端自己那一轮时保留轨迹;对手轮次的轨迹仍然被抹掉,规则不变。
- 用 `scripts/gen_realtime_proto.sh` 重新生成三个生成文件并入库。

### 8.3 MindAgent(新增顶层包 `mindagent`,依赖 `simple_actio`、`inference_bridge`、实时客户端)

```python
# 完整签名以计划 7 为准;这里只列设计层面的形状,以及计划 7 比最初草图多出的 keyword-only 参数。
run_deployment_episodes(store, instance, client_factory, *, run_id, scenario_ids, personas, bindings,
                        model_id, max_turns, episodes_per_scenario=1, max_retries_per_episode=1) -> list[Episode]
analyze(episodes, *, min_support, min_ratio, skills=(), registry=None, signals=None) -> list[BehaviorFinding]
propose(findings, *, bridge=None, run_id="", task_id="mindagent-propose", max_parse_retries=2) -> list[ProposalDraft]
verify(draft, findings, episodes, *, base_instance_factory, personas, scenarios,
       judge=None, run_id="", task_id="mindagent-judge") -> Verification
package(store, round_id, findings, drafts, verifications, *, thresholds, run_id, bound_model_id) -> list[Envelope]
make_start_round_handler(*, config, client_factory, backend_factory, personas_loader, bridge=None) -> Handler   # 工厂,不是处理器本身
handle_admit_proposal(store, action) -> None                                                                   # Handler
load_library(store) -> LibraryState                                  # 读 skill_library/、memory_library/ 里 effective 的条目,版本取 library_state 并规范成字符串
AgentConfig(scenario_ids, bindings, max_turns=6, episodes_per_scenario=1, max_retries_per_episode=1,
            min_support=3, min_ratio=0.5, min_fired_on_motivating=0.8, max_fired_elsewhere=0.2, max_parse_retries=2)   # 数据类,构造时校验;计划 9 的 agent.yaml 读它
```

- **部署驱动器**:`purpose` 固定为 `deployment_simulation`,SimpleActio 实例占客户端位,每轮 `respond` 后带轨迹提交。episode 落在产生它所绑定模型的那个 run 下,id 以 `deploy-<model>-` 开头。
- **经验分析器**:对每个已注册信号,统计"对方最后一句触发了该信号,而被选中的技能并不针对该信号"的 episode 比例。episode 数不足 `min_support` 或比例低于 `min_ratio` 不产生发现,体现"不基于单一个案"。
- **技能提案生成器**:默认规则模板(信号 → 一条"先处理该信号"的技能);传入桥接时,让外部模型生成策略文本。解析耗尽在生成器层是 T2(`ProposalParseError`);轮次处理器遇到它按 R18 把轮次置 `failed`、记 T3(见 §11 的失败类别约定)。
- **提案验证器(R13)**:用保存下来的对方原话离线重放动机 episode,分别用基线实例和加载候选技能后的实例驱动,度量候选技能在动机轮次上的命中比例,以及在其余轮次上的误触发比例。它**不**度量对话质量。可选传入桥接 judge 做质量评分,结果单列。验证用的是触发提案的同一批数据,过拟合风险写进提案的 `notes`,与旧设计一致。
- **轮次处理器**:开始前,把模型已不是最新的 `pending` 轮次置 `failed`(原因 superseded);再绑定最新的有效模型和当前库版本,跑部署 episode、分析、提案、验证,写提案产物,最后把轮次置 `executed`。
- **收录处理器**:提案 `approved` 时,把技能与记忆写进正式库,更新 `library_state`,提案置 `effective`。`library_state` 里技能库与记忆库的版本一律存成字符串,与激活轨迹里的 `library_version` 同型;交给 SimpleActio 实例前经 `simple_actio` 的 `normalize_library_version` 规范化。没有任何技能时,第一轮"沉默"是预期行为。

## 9. 数据合成接线(R14、G9)

指令可携带 `data_spec`:

```yaml
data_spec: {scenario_count: 4, seed: 0, purpose: training, stages: {sft: {group_size: 1}, grpo_opd: {group_size: 2}}}
```

- 调度器构造 `new` 指令时,同时写 `stage_manifest_refs`(清单 id 按 `dsm-<run>-<stage>` 的既有规则确定)和 `data_spec`。`clone` 复用配方锚定的清单,不带 `data_spec`。`replay` 同理。
- MindTrainer 派发器在读清单前检查:清单文件缺失,且指令带 `data_spec`,且清单的数据所在 run 就是本 run,则调用注入的 `DataProducer`;其余情况行为与现在完全相同(缺清单判 `failed`)。
- 没有注入 `DataProducer` 而指令需要它,判 `failed` 并写明缺什么,与"interact 缺 client_factory"同一模式。
- 清单文件已存在则不重新合成;episode 写完而清单未写时重跑覆盖,清单最后生成,所以通常最终一致。例外:清单文件写到一半、或写完但还没提交就崩时,`exists` 已为真,下一轮不会重合成,读取失败会让指令判 `failed`,需要人工删除该文件(计划 2 已知限制 10)。
- 默认的数据生产器用 `TemplateGenerationBackend` 生成情境与人格,再调用既有的 `produce_grouped_dataset`。rollout 后端由调用方注入;默认的 rollout 是 `keyword_alternating_rollouts`,训练位的发言在命中奖励关键词与中性话之间交替,组内奖励的差异是人为造出来的,只为让流程不被退化组挡住(计划 2 Ruling 9)。`data_spec` 的取值范围由计划 2 的 Ruling 规定(场景数、种子、组大小都有上限)。**V1 的模板生成器与目标维度无关**,所以闭环能跑通,但训练数据并不针对规划器选的维度。真正按维度生成需要桥接后端的生成器,不在本期。

## 10. 命令行与入口

### 10.1 TypeScript 命令行(R16)

- `findCandidatePath` 泛化为在三类路径里找产物:`runs/*/candidate_checkpoint.yaml`、`recipe_candidates/*.yaml`、`skill_memory_proposals/*/proposal.yaml`。
- `review` 按类型打印证据:候选看诊断(并对 `backends: mock` 的报告醒目标出 `[MOCK BACKENDS]`);配方候选看锚定内容与 `repro`;提案看发现、技能定义与验证结果。
- `approve` 对各类型的证据规则:候选要有诊断;配方候选要有 `repro`;提案要有 `verification`。缺证据拒绝批准且不改任何文件。
- 新增 `confirm <directive-id>`:`awaiting_confirmation → pending`。
- `status` 同时列出待审的三类产物和待确认的指令。

### 10.2 Python 入口与组合根

- 新增顶层包 `factory`。`factory.wire` 是组合根:读配置,按开关组装 `handlers`、`dispatcher`、`evaluator`,调用 `run_daemon`;`factory.cli` 放命令行(子命令、开关、退出码);`factory.readonly` 放只读子命令的实现。入口脚本 `scripts/run_factory.py`(默认,不接受 mock 证据)与 `scripts/run_factory_dev.py`(开发,自动加 `--allow-mock-evidence`)都是薄壳。
- 子命令:`run`(一直跑)、`reconcile --once`(只跑一轮并打印动作)、`install-defaults`(显式写入默认诊断资产、评估器配置、提示模板与 MindAgent 部署素材,`run` 与 `reconcile` 从不自动写)、`trace <run>`、`cost <run>`、`logs <run> [--task <id>]`。每个子命令的第一个位置参数都是控制面仓库根目录 `repo_root`。
- 开关:`--enable plan,nominate,track,agent,admit`(`admit` 同时开 `admit_recipe` 与 `admit_proposal`;未知开关与空项一律是用法错误);`--allow-mock-evidence`(唯一来源,同一个布尔值既给调和循环也给提名处理器);`--confirm-directives/--no-confirm-directives`(三态,不传就用 `scheduler.yaml`,经 loader 覆盖、不写回);`--planner-backend bridge|heuristic`(默认 `bridge`,`heuristic` 不需要外部工具);`--repo-commit`(不传取代码仓库根的 `git rev-parse HEAD`,取不到传 `None`)。不传 `--enable` 时与现在的 `scripts/run_controlplane.py` 行为相同,不读任何配置。
- 退出码:0 成功;1 只读子命令发现缺失(`trace` 的 run 不存在、`logs` 的 run 不存在或有引用不可达);2 用法错误或装配错误(缺配置、缺资产、缺凭证,装配在第一个 tick 之前完成,树里不写任何东西)。
- 开了任一闭环开关就同时注入默认数据生产器与 mock 评估器(桩诊断的证据等级恒为 `stub`,闭环一个动作都不会触发)。评估器参数在配置信封 `config/evaluator.yaml`(五个键),MindAgent 的部署素材在 `config/agent.yaml`,桥接配置 `config/backends.yaml` 的角色是 `planner` 与 `agent`。
- `mindtrainer.wire` 与 `scripts/run_controlplane.py` 保持原样,不删除。
- 真实诊断接入:`diagnostic.evaluator.make_diagnostic_evaluator(...)` 返回一个 `Evaluator`。它从 `control-plane/diagnostics/` 读分类表与题库,用注入的 SoMEval 运行器与 judge 跑 `run_diagnostic`,再给报告写上 `backends` 标记。基线分数取基线模型所在 run 的报告。V1 注入的是 `MockSoMEvalRunner` 与 `MockJudgeBackend`,所以标记为 `mock`(R17)。

## 11. 接口契约与依赖方向

**依赖方向**(自左向右允许导入):

```
controlplane  ←  inference_bridge, recipestore, trainplanner(只用纯数据), trainscheduler, mindagent, factory
simulator     ←  simple_actio(datamodel 部分), mindagent, mindtrainer(已有)
recipestore   ←  trainscheduler, factory
trainplanner  ←  trainscheduler, factory
simple_actio         ←  mindagent, factory
inference_bridge ← trainplanner, trainscheduler(只取 `BridgeError`), mindagent, factory
mindtrainer.data_spec, mindtrainer.training_config ← trainscheduler      # 只限这两个模块
```

- `factory` 是组合根:可以导入任何包(装配派发器要用 `mindtrainer.dispatch` 与 `mindtrainer.data_producer`,装配评估器要用 `diagnostic` 与 `simulator`),任何包都不导入 `factory`。
- `controlplane` 不导入上述任何新包(计划 9 任务 109 有一条统一测试强制)。`mindtrainer` 不导入 `diagnostic`。`simple_actio` 只依赖 `simulator.datamodel`。`trainscheduler` 只从 `mindtrainer` 导入 `data_spec`(`required_stages`、`parse_data_spec`、`DataSpec` 等,完整名单以计划 5 的依赖声明为准)与 `training_config`(`TrainingConfig`、`validate_training_config`、`load_config_from_directive`,构造指令与校验要用),不导入 `mindtrainer` 的其他模块,也不导入 `diagnostic`、`simulator`、`simple_actio`、`mindagent`、`factory`。
- **计划 1 一次性定义全部新增路径函数、`SUBDIRS`、状态机改动、`ledger`、桥接**。后续计划只消费,不得再改这些文件(除非计划文本明确写出)。
- 新增顶层包必须加进 `pyproject.toml` 的 wheel 包列表,`test_mt_packaging` 会强制检查。
- 路径函数的名字(计划 1 产出):`recipe_candidate_file(run_id)`、`recipe_dir(run_id)`、`recipe_file(run_id)`、`recipe_lineage_file(run_id)`、`recipe_track_file(run_id)`、`proposal_file(proposal_id)`、`skill_file(skill_id)`、`memory_file(mem_id)`、`library_state_file()`、`agent_round_file(model_id)`、`planner_decision_file(run_id)`、`nomination_file(run_id)`、`recipe_track_note_file(run_id)`、`run_trace_file(run_id)`、`failure_record_file(run_id, task_id)`、`scheduler_config_file()`、`training_defaults_file()`、`planner_prompt_file(version)`。
- **指令文件的路径没有路径函数。** 现有 `controlplane.daemon` 把它写成 `f"{run_dir(run_id)}/directive.yaml"`,计划 3、计划 8 沿用同一写法;计划 5 在 `trainscheduler.assemble` 里包了一个同值的 `directive_file`。三处返回值相同,V1 不为它新增 `controlplane.paths` 函数(计划 1 已冻结路径函数集合);若以后要统一,改 `paths` 并让三处改调它。
- 各计划的处理器统一签名 `Handler = Callable[[GitStore, Action], None]`(`Action` 来自 `controlplane.reconcile`),处理器可以直接调用,不依赖调和循环,便于单测。
- **`Handler` 别名由计划 8 定义,而计划 3、5、7 排在它前面执行。** 这三份计划的处理器一律写成字面的 `Callable[[GitStore, Action], None]`,不从 `controlplane.daemon` 导入 `Handler`;`Action` 来自 `controlplane.reconcile`,它现在就存在。
- **失败记录的类别约定。** T3 表示闭环因此停在 `failed`、等人处理(规划决策、agent 轮次);T2 表示确定性数据错误,处理器空操作并只记一次(收录处理器);T1 只在桥接内部用于重试。调用方见到的任何桥接失败一律按 R18 升级为 T3。类别只是留痕分类,不改变处理器的行为。
- **技能 id 以 `skill-` 开头、记忆 id 以 `mem-` 开头**(计划 6 的 `from_dict` 校验),因为 `skill_file(id)`、`memory_file(id)` 的文件名就是 id,不带前缀的 id 会落到设计 §4.1 之外的文件名上。
- **配置文件一律是信封**(`type: config`,载荷是配置本体),读写走 `controlplane.config.read_config(store, rel_path) -> dict` 与 `write_config(store, rel_path, config_id, payload, message)`,计划 1 产出。配置文件包括 `backends.yaml`(角色形状 `{planner|agent: BackendConfig}`)、`scheduler.yaml`、`training_defaults.yaml`,以及计划 9 新增的 `evaluator.yaml`(诊断评估器的五个参数)与 `agent.yaml`(MindAgent 的部署素材)。
- **证据等级只有一处实现**:`controlplane.evidence.diagnostic_grade(payload: dict) -> str`(返回 `stub`、`mock`、`real`)与 `evidence_acceptable(grade: str, accept_mock: bool) -> bool`,计划 1 产出;`reconcile` 扩展、提名、规划处理器都调它。
- **增益只有一处实现**:`recipestore.gain.compute_gain(store, run_id, dimension) -> GainResult(gain: float | None, baseline_run_id: str | None, reason: str)`,计划 3 产出;提名与战绩回写都调它。

## 12. 计划拆分与顺序

| 顺序 | 计划文件(`docs/superpowers/plans/`) | 任务号起点 | 内容 | 前置 |
|---|---|---|---|---|
| 1 | `2026-10-09-bridge-and-foundations.md` | 19 | 路径与 `SUBDIRS`、`awaiting_confirmation`、`ledger`、桥接与配置扩展、配置信封读写、证据等级、`.gitignore` | 无 |
| 2 | `2026-10-09-data-synthesis-wiring.md` | 30 | `data_spec`、`DataProducer`、派发器改动 | 计划 1 |
| 3 | `2026-10-09-recipestore.md` | 40 | 校验器、检索、`compute_gain`、战绩、生命周期、收录与战绩处理器 | 计划 1 |
| 4 | `2026-10-09-trainplanner.md` | 50 | 上下文、提示渲染、解析、后端、`plan` | 计划 1 |
| 5 | `2026-10-09-trainscheduler.md` | 60 | 装配、校验、解析、构造、提名、两个处理器 | 计划 2、3、4 |
| 6 | `2026-10-09-simple-actio.md` | 70 | 技能、记忆、经验、实例、信号、协议改动 | 无(协议改动独立) |
| 7 | `2026-10-09-mindagent.md` | 80 | 驱动器、分析、提案、验证、打包、两个处理器 | 计划 1、6 |
| 8 | `2026-10-09-reconcile-extension.md` | 90 | `reconcile` 扩展、注入缝、真实诊断评估器、命令行泛化与 `confirm` | 计划 1 |
| 9 | `2026-10-09-factory-and-e2e.md` | 100 | `factory` 组合根、只读子命令、全闭环与 MindAgent 端到端、跨计划一致性核对 | 计划 1 至 8 |

任务号各留出间隔,避免并行撰写时编号冲突。每份计划按旧计划的格式写:头部、全局约束、文件表、带完整代码与命令的任务。

## 13. 测试策略

- 每份计划都不依赖 GPU、外部网络、真实模型。桥接测试用假的可执行脚本或注入的 `runner`。
- `reconcile` 扩展的测试沿用 fixture 化的 `TreeState`。必须有一条回归测试:不注入处理器时,既有动作列表与返回值与现在逐字节相同。
- 协议改动要有两类测试:新字段往返,以及旧的二元组后端不受影响。
- 闭环端到端测试(计划 9):用 `MockExecutor`、`Mock` 诊断后端,规划用罐头后端或计划 4 的 `HeuristicBackend`(整圈端到端用后者,命令行 `--planner-backend heuristic` 选得到),桥接另用假可执行体单独覆盖(`CliBridge` 取回文本、失败升级 T3),从一条人工写的指令出发,走完 训练 → 诊断 → 审核门① → 晋级 → 提名 → 门③ → 规划 → 下一条指令 → 确认 → 第二轮;并行验证 MindAgent 轮次 → 提案 → 门② → 收录。人工批准用写状态模拟,与现有端到端测试同一手法。
- 全量测试数写成"起点 B 加增量"(820 是撰写时 `main` 的实测数,前面的计划落地后起点会变大);各计划结束时必须保持无警告(`-W error`)。变异检查设计不作要求,各计划按需自做。

## 14. 未核实项

1. **命令行参数。** 本机 `claude --help` 确认存在 `-p/--print`、`--output-format`、`--json-schema`、`--model`、`--max-budget-usd`、`--no-session-persistence`、`--permission-mode`。`codex exec` 确认存在 `-m/--model`、`-s/--sandbox`,提示词可来自参数或标准输入。没有真实调用过,也不知道 JSON 输出里是否带用量。所以参数模板放配置,用量未知就省略键。
2. **协议重新生成。** 不知道当前 `grpcio-tools` 版本重新生成后,与已入库文件的版本头是否一致。计划 6 的第一步要先跑一次生成并确认差异只在新字段。
3. **信号词典、阈值、提示模板。** 都是本设计拟的默认值,没有数据支持;`data_spec` 的各项上限与合成时的 `max_turns` 也是拍的(计划 2 的 Ruling)。
4. **"可复现"的范围。** 配方锚定了数据清单和代码提交,但真实训练的随机性、优化器状态等见 MindTrainer 设计 §10 的第 13 条,V1 没有证据。
5. **规划与提案的质量。** 罐头后端只能证明流程与解析正确,不能证明外部模型给出的决策好。
6. **同步阻塞。** 外部推理期间调和循环不转,审批动作会被饿住;数据合成(每条 episode 一次 git 提交)与 MindAgent 的部署会话同样同步阻塞。已知限制,与派发器相同。
7. **成本增益上下文大多为空。** 全仓目前没有代码写 GPU 小时:`MockExecutor` 不占 GPU,真实执行器接入前没有写入方。`cost_gain` 因此只可能含外部推理的 token 数。两处未记录的写法不同,不要混:`run_trace` 里的 `cost_records` 由 `ledger.record_cost` 写,未记录的字段省略该键(`observe.sum_costs` 用 `r.get(key, 0)` 兜底),否则求和会抛 `TypeError`;`PlanningContext` 的 `cost_gain` 由 `CostGainEntry` 承载,`from_dict` 拒绝缺键,所以未记录的成本键必须在场、值为 `None`,由提示渲染成 "not recorded"。两条都不许填 0:0 会被规划器读成免费,而 `None` 明确表示没记录。

8. **写回超大整数的既有逃逸(跨切面,不属本次九份计划)。** 树里任何产物的 payload 若含超过 4300 位的整数(YAML 十六进制可以写出来),`gitstore.write_product` 里的 `yaml.safe_dump`(`src/controlplane/gitstore.py:43`)会抛 `ValueError` 并让调和循环退出,不是被记为确定性失败。这是 main 上就有的行为:把任意指令字段写成这种整数即可复现。计划 2 的解析器已在入口挡住它并判 `DirectiveFailedError`,但调和循环随后写回整份 payload 时仍会踩到,所以计划 2 把它记为已知限制 12、并把该用例改成直接调派发器、不经 `reconcile_once`。**真正的修法**在 `controlplane/gitstore.py` 或 `daemon.py`(写入前做一次安全的整数检查,超限就判 T2),不属于本次九份计划的范围——它们冻结了 controlplane。

9. **配方候选的 `repro` 在 V1 里恒有两条缺口。** `eval_anchor.taxonomy_version` 与 `eval_anchor.item_pool_version` 由计划 5 的提名处理器填 `None`,所以 V1 产出的每个批次候选都会带这两条 repro 错误(计划 5 的测试逐条钉住了它们)。`code_version.repo_commit` 由组合根按 `git rev-parse HEAD` 填,正常时合法;取不到 HEAD 时才多一条错误。这不是误判——它们是真缺口——但会让 repro 变成常驻噪声,审核人容易对错误列表脱敏。计划 8 的诊断评估器其实知道那两个版本号,后续迭代可以让它写进报告、由提名处理器回填。

10. **后端从 mock 切到 real 之后的头几轮没有可比增益。** `compute_gain` 对"real 候选、非 real 基线"返回 `gain=None` 并写明基线等级(分数不可比),所以切换后的头几轮提名会因增益为空而跳过,要等基线所在的 run 也有 real 报告。诊断评估器本身不挡 mock 基线:它只负责打退化标记,mock 基线仍要用来检测退化;只有会驱动提名的增益才排除跨等级比较。这是有意的取舍,换来的是"真分数对假分数"不会产生一个看起来正常的增益数字。

11. **重入时外部推理成本可能少记。** 计划 5 的规划处理器遇到非桥接瞬态异常、计划 7 的轮次处理器按 R19 重入时,都会从头重跑外部推理,而重跑沿用同一组 `(task_id, cost_type)`;计划 1 的账本先到者为准,会把这些成本静默去重。这是有意的取舍(要修需要给 `task_id` 加重入序号),V1 不做,成本数字因此是下限。

12. **树里已提交产物损坏时,守护进程会退出(跨切面,沿用现状)。** `controlplane.daemon` 读指令、候选与模型时直接 `read_product`,产物的 YAML 解析失败、含合并冲突标记或信封缺键时会抛异常并让调和循环退出,重启后同一处再抛。计划 8 只对轮次产物与诊断报告做了"读不出按没有"与"读不出按最弱"的兜底(计划 8 的 Ruling 20、23),计划 7 对轮次产物同样兜底;库条目、模型与提案等其余产物沿用现状。修法在 `controlplane` 的读取层,不属本次九份计划,与第 8 条同属 controlplane 既有行为。
13. **第一次闭环没有基线模型。** 没有已晋级模型时 `compute_gain` 返回空增益,提名不会发生;V1 的 mock 诊断又给每个候选打同样的分,所以要有一个更低分的基线,提名才有正增益。计划 9 的端到端手工登记了一个全错的基线 run 与已晋级模型。真实部署同样要先有一个已晋级的基线模型,首轮才可能提名。

## 15. 明确不做

- 真实 ROLL 训练、SSH 与计算层调度、真实 SoMEval 与真实 judge、真实 vLLM。
- 桥接的 API 形态实现(R15)。
- RecipeStore 的 SQLite 索引(R9)、`superseded` 自动迁移(R8)、配方导入导出、跨工厂流通。
- 按目标维度定制的情境与人格生成(R14);合成的情境与人格不落盘,interact 的 sft 数据不用 `bindings` 里的人格(计划 2 Ruling 11、已知限制 13)。
- 诊断的动态轨(V1 评估器只跑静态轨,计划 8 Ruling 12)。
- 多租户运行时、鉴权、图形界面、真实生产日志作为经验来源。
- 提案验证器的留出集与对话质量度量默认值(R13)。
- 并行多轮在途(R4)、失败指令的自动恢复(R5)。
- AgentRSI 类型配方的提名。旧设计 6.2 让 TrainScheduler 统一提名三类配方,但本设计的 `nominate_recipe` 只由训练 run 的候选触发,MindAgent 的轮次结果没有对应的提名触发。V1 的 `recipe_type: agent_rsi` 只在校验器与检索里存在,不会被自动提名,需要时人工写配方候选。
