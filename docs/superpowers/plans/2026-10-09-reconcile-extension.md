# 调和循环扩展:闭环视图与动作、注入缝、真实诊断评估器、命令行泛化与 confirm Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让调和循环认识闭环的六个新动作(规划、提名、战绩、智能体轮次、两类收录),并能通过注入的处理器执行它们;让 `request_diagnostic` 可以换成真实的诊断编排器(V1 用 mock 后端,报告带 `backends` 标记);让命令行能审核配方候选与技能提案、确认待确认的指令。不注入任何东西时,调和循环的行为与现在逐字节相同。

**Architecture:** `reconcile` 仍是纯函数:`TreeState` 加 `runs / models / gates / enabled / accept_mock_evidence` 五个带默认值的字段,六个新动作只在动作名属于 `state.enabled` 时才产生。`controlplane.daemon` 开两条新缝:`handlers: Mapping[str, Handler]`(键就是启用的动作名)与 `evaluator: Evaluator`(只替换 `request_diagnostic`)。`read_tree_state` 只在 `enabled` 非空时才去读新视图。`controlplane` 不认识任何新子系统,处理器由调用方(计划 9 的 `factory`)组装。`diagnostic` 一侧补资产的信封读写与 `make_diagnostic_evaluator`。TypeScript 命令行与 Python 只靠树里的文件约定通信,约定集中列在「跨语言约定」一节。

**Tech Stack:** Python 3.11+ / uv / pytest;TypeScript / vitest / js-yaml;**无新增第三方依赖**。

**Spec:**
- `docs/superpowers/specs/2026-10-09-closed-loop-integration-design.md` §3.1–§3.3(视图、触发条件、注入方式)、§3.4(确认态在命令行一侧)、§4(产物与路径)、§10.1(命令行)、§10.2 最后一条(真实诊断接入)、§11(接口契约)、R1–R5、R10、R16、R17、R19(设计已定的 R1 至 R21 不推翻;R20 是计划 5 的提名签名扩展,R21 是计划 5 丢弃空标签,都不改本计划)
- 计划顺序:设计 §12 第 8 份,任务号起点 90。前置只有**计划 1**(`2026-10-09-bridge-and-foundations.md`)。
- 基线:`main` 在 `0385004`;Python 全量 **820 通过**、无警告;命令行 **18 通过**。计划 1 落地后 Python 基线会变大,见「测试计数」一节。

**依赖已有产出的名字**

| 来源 | 名字 |
|---|---|
| `controlplane.reconcile`(现有) | `DirectiveView`、`CandidateView`、`TreeState`、`Action`、`reconcile` |
| `controlplane.daemon`(现有) | `Dispatcher`、`DirectiveFailedError`、`stub_dispatcher`、`_iter_run_ids`、`read_tree_state`、`apply_action`、`reconcile_once`、`run_daemon` |
| `controlplane.paths`(现有) | `CONTROL_PLANE_ROOT`、`run_dir`、`model_file`、`diagnostic_report_file`、`candidate_checkpoint_file`、`taxonomy_file`、`item_pool_file` |
| `controlplane.paths`(**计划 1 产出**) | `recipe_candidate_file(run_id)`、`proposal_file(proposal_id)`、`agent_round_file(model_id)`、`planner_decision_file(run_id)`、`nomination_file(run_id)`、`recipe_track_note_file(run_id)` |
| `controlplane.states`(**计划 1 产出**) | 非审核类 `awaiting_confirmation: {pending, failed}` |
| `controlplane.evidence`(**计划 1 产出**) | `diagnostic_grade(payload) -> str`、`evidence_acceptable(grade, accept_mock) -> bool` |
| `diagnostic.orchestrator`(现有) | `DiagnosticConfig`、`run_diagnostic` |
| `diagnostic.store_adapter`(现有) | `write_report`、`read_report` |
| `diagnostic.datamodel` / `diagnostic.taxonomy_loader`(现有) | `DiagnosticTaxonomy`、`DimensionScore`、`DiagnosticReport` / `ItemPool` |
| `diagnostic.someval_adapter` / `diagnostic.scorer`(现有) | `SoMEvalRunner`、`MockSoMEvalRunner` / `JudgeBackend`、`MockJudgeBackend` |
| `simulator.stubs`(现有) | `diagnostic_evaluator_stub`(报告带 `stub: True`) |
| `cli/src/gittree.ts`(现有) | `readProduct`、`writeProduct`、`Envelope` |

**本计划的取舍(要改的话改这里,任务按此排)**

1. **只做调和循环与命令行的"接口一侧"。** 六个动作的处理器分别属于计划 3、5、7,本计划一个都不实现,测试里一律用假处理器。`factory` 组合根、只读子命令、全闭环端到端属于计划 9。
2. **`reconcile` 的新逻辑全在 `_closed_loop_actions` 里,追加在既有动作之后。** 既有动作的循环一行不改;`enabled` 为空时直接跳过,所以既有测试与返回值不变。
3. **视图读取按需。** `read_tree_state(store)` 不传 `enabled` 时走与现在完全相同的代码路径,不多读一个文件。
4. **评估器的 `backends` 标记由 `write_report` 的可选参数写入**(Ruling 9),报告与标记同一次提交落盘。
5. **命令行把"按 id 找产物"泛化成三类路径**,id 冲突时报歧义,不猜。

**已核实的事实(实现者不要再猜)**

- `promote_candidate` 写的模型产物 `links=[action.target_id, run_id]`,即 `[候选 id, run id]`(`src/controlplane/daemon.py`)。所以 `ModelView.run_id` 取 `links[1]`。计划 3 的 `recipestore.gain.baseline_run_of` 对同一问题取"`cand-<x>` 也在 `links` 里的那一项";两种判法在 `promote_candidate` 的产物上结果相同,晋级写法一变就可能分叉(见「已知限制」第 10 条)。
- `GitStore.list_products(rel_dir)` 只认 `rel_dir/*.yaml`,不递归。技能提案在 `skill_memory_proposals/<id>/proposal.yaml`,必须像 `_iter_run_ids` 那样自己遍历子目录。
- `GitStore.write_product` 内容不变时不提交(先 `git diff --cached --quiet`)。资产重复写入是无操作。
- `DiagnosticReport.from_dict` 只取已知键,多出来的 `backends` 不影响 `read_report`。
- `run_diagnostic` 的检测退化要求 `(dimension, level, track_provenance)` 三元组对上且置信区间不相交。同一题库、全对对全错时会触发。
- `simulator.stubs.diagnostic_evaluator_stub` 的载荷有 `stub: True`。计划 1 的 `diagnostic_grade` 对它返回 `stub`;对 `backends: "mock"` 返回 `mock`;对有非空 `dimension_scores` 且没有 `backends` 或 `backends: "real"` 返回 `real`;其余返回 `stub`。
- vitest 的 worker 里 `process.chdir()` 抛 `ERR_WORKER_UNSUPPORTED_OPERATION`。测 `main` 时改为 `vi.spyOn(process, "cwd")`。
- `git ls-files` 的路径模式里 `*` 会跨越 `/`(git pathspec 默认不是 glob 模式)。`control-plane/recipe_candidates/*.yaml` 因此也会匹配子目录里的 yaml。本计划的三类模式各自落在不同的顶层目录,互不串扰;同一目录内的误匹配见「已知限制」第 6 条。

## Global Constraints

每个任务的要求都隐含包含本节。

- **依赖方向不变**:`controlplane` 不导入 `diagnostic`、`mindtrainer` 或任何新顶层包;`diagnostic` 不导入 `mindtrainer`(任务 95 有测试)。处理器与评估器在 `controlplane` 里只是两个 `Callable` 类型别名。
- **默认行为逐字节不变**:不传 `handlers`、`evaluator`、`enabled` 时,调和循环写出的产物、提交日志、返回的动作列表与改动前完全相同。既有测试一行不改,尤其 `tests/test_reconcile.py`、`tests/test_daemon.py`、`tests/test_daemon_dispatcher.py`、`tests/test_e2e_loop.py`、`tests/test_diag_*.py`、`cli/test/commands.test.ts`。
- **新参数全部 keyword-only**:`read_tree_state`、`apply_action`、`reconcile_once`、`run_daemon`、`write_report`、`run_diagnostic`、`make_diagnostic_evaluator`。
- `src/` 下每个源文件以 `from __future__ import annotations` 开头(模块 docstring 之后的第一条语句也算)。
- **读产物走 `GitStore.read_product`,写产物用 `Envelope` + `GitStore.write_product`。** 不直接写文件系统。唯一的例外是目录枚举(`_iter_run_ids` 与本计划的 `_read_gate_views`),它只列目录,读内容仍走 `read_product`。
- **配置文件一律是信封**(`type: config`),读写走 `controlplane.config.read_config / write_config`(计划 1)。本计划不读写任何配置文件;`accept_mock_evidence` 与 `backends` 都是调用方传入的参数,由计划 9 的组合根从配置或开关里取。
- **证据等级只在 `controlplane.evidence` 里实现。** 本计划调用 `diagnostic_grade` 与 `evidence_acceptable`,不另写判断。增益只在 `recipestore.gain.compute_gain` 里实现,本计划不涉及。
- **无新增第三方依赖;凭证绝不进 git 树。**
- **`git add` 逐个文件**;**不要碰** `cli/package.json`、`cli/package-lock.json`(工作区里它们有与本计划无关的改动)。
- **提交信息不加 `Co-Authored-By` 行**(用户明确要求)。下文每个提交命令都照此写。
- 测试命令统一为 `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error ...`(任何警告都会变成失败);命令行为 `cd cli && npx vitest run`,类型检查 `cd cli && npx tsc --noEmit`。

## 对外契约(其他计划必须遵守)

本节是本计划交给计划 3、5、7、9 的接口。名字与形状照抄,不得改名。

**类型与入口(`controlplane.daemon`)**

```python
Handler   = Callable[[GitStore, Action], None]
Evaluator = Callable[[GitStore, str, str], None]   # (store, run_id, candidate_id)

def read_tree_state(store, *, enabled: frozenset[str] = frozenset(), accept_mock_evidence: bool = False) -> TreeState
def apply_action(store, action, *, dispatcher=None, handlers=None, evaluator=None) -> None
def reconcile_once(store, *, dispatcher=None, handlers=None, evaluator=None, accept_mock_evidence=False) -> list[Action]
def run_daemon(store, interval_s, max_ticks=None, *, dispatcher=None, handlers=None, evaluator=None, accept_mock_evidence=False) -> None
```

- `Handler` 别名由本计划在 `controlplane.daemon` 里定义;计划 3、5、7 排在本计划之前执行,它们的处理器签名一律写字面的 `Callable[[GitStore, Action], None]`,与这个别名同构。`Action` 来自 `controlplane.reconcile`,现在就存在。
- `handlers` 的键只能是 `controlplane.reconcile.CLOSED_LOOP_ACTIONS` 里的六个名字,其余键在任何动作执行之前抛 `ValueError`(Ruling 7)。
- 处理器拿到的 `Action` 来自 tick 开头的快照。**处理器必须自己重读树**,不能只凭 `detail` 行事。
- 处理器异常原样上抛,调和循环不吞、不记;守护循环因此退出(与派发器同一已知限制)。确定性失败由处理器自己写自己产物的 `failed` 与失败记录(计划 1 的 `record_failure`),然后正常返回。
- 处理器**必须写下自己的幂等键**(下表最后一列),否则每个 tick 都会再次触发。

**六个动作(`controlplane.reconcile`)**

| 动作 | `target_id` | `detail` | 触发条件 | 幂等键(处理器负责写) |
|---|---|---|---|---|
| `admit_recipe` | 配方候选 id(`recipe-<run>`) | `{}` | `GateView.kind == "recipe_candidate"` 且状态 `approved` | 配方候选变为 `effective` |
| `admit_proposal` | 提案 id | `{}` | `GateView.kind == "skill_memory_proposal"` 且状态 `approved` | 提案变为 `effective` |
| `record_track` | run id | `{"run_id", "recipe_ref"}` | `recipe_ref` 非空;候选 `effective` 或 `rejected`;证据可接受;没有战绩标记 | `recipe_track_note_file(run)` |
| `nominate_recipe` | run id | `{"run_id"}` | 候选 `effective`;证据可接受;没有提名标记 | `nomination_file(run)` |
| `plan_next_round` | run id | `{"run_id"}` | 候选 `effective` 或 `rejected`;证据可接受;`planner_decision_status` 为空或 `pending`;**没有在途轮**;该 run 是 `run-<数字>` 里编号最大的 run(按整数比较,`_RUN_ID = ^run-(\d+)$`;不匹配的名字不参与比较;一个 `run-<数字>` 都没有时不触发,刻意失败关闭;Ruling 21) | `planner_decision_file(run)` 为 `executed` 或 `failed` |
| `start_agent_round` | 模型 id(`zengen-vN`) | `{"version", "run_id"}` | 版本号(按数字)最大的 `effective` 模型,且 `agent_round_status` 为空或 `pending`(没有轮次产物,或轮次产物读不出,或轮次仍是 `pending`;R19,Ruling 20) | `agent_round_file(model)` 为 `executed` 或 `failed` |

- "证据可接受":`diagnostic_grade` 为 `real`,或为 `mock` 且 `accept_mock_evidence` 为真。没有报告(`None`)与 `stub` 永不可接受。
- "在途轮"(R4):任何 run 的指令是 `pending` 或 `awaiting_confirmation`,或候选是 `pending_review` 或 `approved`。
- 输出顺序固定:既有动作在前(`dispatch_directive`、`request_diagnostic`、`await_review`、`promote_candidate`),然后 `admit_recipe → admit_proposal → record_track → nominate_recipe → plan_next_round → start_agent_round`;同一种动作内按 id 字典序(Ruling 3);`plan_next_round` 每个 tick 至多一条。
- `start_agent_round` 的 `detail["run_id"]` 取自模型产物 `links[1]`;`links` 不足两项时为 `""`。
- **轮次处理器的重入契约(给计划 7,R19)**:轮次产物写回终态(`executed` / `failed`)之前,每个 tick 都会再次产生 `start_agent_round`。处理器必须先写 `pending` 的轮次产物作为幂等键,重入时整份重写同一产物(不追加、不另起新 id);已在跑的长任务(部署、会话)要靠这份 `pending` 产物里记下的进度判断是续跑还是跳过,不能每次唤醒都从头跑一遍。轮次产物存在但读不出时视图按"没有轮次"处理,同样会触发;处理器重入时判定确定性损坏,自己写 `failed`。

**规划处理器的重入契约(给计划 5)**

规划处理器按设计 §7.2 的顺序写:①`planner_decision(status=pending, payload.next_run_id=<新 run>)` → ②`runs/<新 run>/directive.yaml` → ③把决策置 `executed`。`plan_next_round` 只看编号(`run-<数字>` 的整数)最大的 run(R5,Ruling 21),所以两种崩溃点的后果不同:

1. **崩在①之后、②之前("决策 pending、指令未写")**:新 run 目录还不存在,来源 run 仍是最大 run,决策是 `pending` → `plan_next_round` **会再次触发**,落在来源 run 上。处理器看到 `pending` 的决策,复用已记下的 `next_run_id` 续写指令,不重新分配 run id(任务 90 的 `test_reentry_after_a_crash_before_the_directive_retriggers_the_source_run`)。
2. **崩在②之后、③之前("指令已写、决策仍 pending")**:新指令所在的 run 成为最大 run。它在途时不规划;它走完后规划落在新 run 上,**来源 run 不再触发**(任务 90 的 `test_reentry_after_a_crash_after_the_directive_does_not_retrigger_the_source_run`)。来源 run 的决策永远停在 `pending`;装配上下文时(计划 5 的 `assemble_context` 读 `recent_decisions`),决策为 `pending` 而它的 `next_run_id` 指令已存在的,**按 `executed` 对待**。
3. **防御性检查保留**:处理器动手前仍要检查"决策是 `pending` 且 `run_dir(next_run_id)/directive.yaml` 已存在 → 只把决策置 `executed`,不再规划、不再分配 run id、不写第二条指令"。按第 2 点这条分支在正常树上走不到,但人手改树(例如删掉新 run 的候选)时它能防止重复指令。

**诊断评估器(给计划 9)**

```python
# diagnostic.evaluator
make_diagnostic_evaluator(*, taxonomy_version, item_pool_version, someval_runner, judge_backend,
                          benchmark_version, tier="full", sampling_k=4, backends="mock") -> Evaluator
resolve_baseline(store, run_id, benchmark_version) -> tuple[str, list[DimensionScore]]
NO_BASELINE = "none"
# diagnostic.assets
write_taxonomy(store, taxonomy) -> str ; read_taxonomy(store, version) -> DiagnosticTaxonomy
write_item_pool(store, pool) -> str    ; read_item_pool(store, version) -> ItemPool
TAXONOMY_TYPE = "diagnostic_taxonomy" ; ITEM_POOL_TYPE = "diagnostic_item_pool" ; DiagnosticAssetError
# diagnostic.store_adapter
write_report(store, run_id, report, *, backends=None) ; BACKENDS_KEY = "backends" ; BACKENDS_VALUES = ("mock", "real")
# diagnostic.orchestrator
run_diagnostic(..., *, baseline_scores, dynamic_specs, backends=None)
```

**命令行(给计划 9 的文档与端到端)**:`rismforge <review|approve|reject|confirm|status> [id] [reason]`。

## 跨语言约定

TypeScript 命令行与 Python 只经 git 树里的文件通信。下表每一项在两侧各写一次,改任何一项必须两侧同改。**TS 一侧每行都在注释里指明 Python 出处**(`commands.ts`);**Python 一侧只有本计划改动的文件指明 TS 出处**,实际是两个文件三处注释:`controlplane/daemon.py` 两处(`_read_run_views` 的指令路径、`_read_gate_views` 的两类审核产物路径),`diagnostic/store_adapter.py` 一处(`backends` 标记)。`states.py`、`paths.py`、`evidence.py`(计划 1)、`recipestore/admit.py`(计划 3)、`simulator/stubs.py`、`diagnostic/datamodel.py` 不在本计划的改动范围内,它们那一侧没有指回 TS 的注释,改这些文件时要自己查本表。

| 约定 | Python 一侧 | TypeScript 一侧 |
|---|---|---|
| 候选 checkpoint 路径 `control-plane/runs/<run>/candidate_checkpoint.yaml` | `controlplane/paths.py` `candidate_checkpoint_file`;`daemon.read_tree_state` | `commands.ts` `GATE_PATTERNS[0]` |
| 配方候选路径 `control-plane/recipe_candidates/<id>.yaml`(`<id>` = `recipe-<run>`) | `paths.recipe_candidate_file`(计划 1);`daemon._read_gate_views` | `commands.ts` `GATE_PATTERNS[1]` |
| 技能提案路径 `control-plane/skill_memory_proposals/<id>/proposal.yaml` | `paths.proposal_file`(计划 1);`daemon._read_gate_views` | `commands.ts` `GATE_PATTERNS[2]` |
| 指令路径 `control-plane/runs/<run>/directive.yaml`,指令 id = run 目录名 | `daemon.read_tree_state`、`daemon._read_run_views` | `commands.ts` `DIRECTIVE_PATTERN`(`confirm`、`status`) |
| 诊断报告路径 `control-plane/runs/<run>/diagnostic_report.yaml`(与候选同目录) | `paths.diagnostic_report_file` | `commands.ts` `diagnosticPathFor`(既有) |
| 审核类状态机 | `controlplane/states.py` `REVIEW_TRANSITIONS` | `commands.ts` `REVIEW_TRANSITIONS`(既有) |
| 非审核类状态机,含 `awaiting_confirmation: {pending, failed}` | `states.py` `NONREVIEW_TRANSITIONS`(计划 1) | `commands.ts` `NONREVIEW_TRANSITIONS` |
| 诊断载荷 `stub: true` → `[STUB]` | `simulator/stubs.py`;`evidence.diagnostic_grade` | `summarizeDiagnostic`(既有) |
| 诊断载荷 `backends: "mock" \| "real"` → `[MOCK BACKENDS]` | `diagnostic/store_adapter.py` `BACKENDS_KEY`、`BACKENDS_VALUES`;`evidence.diagnostic_grade` | `summarizeDiagnostic` |
| 诊断载荷 `dimension_scores[].{dimension, level, score, ci_low, ci_high, sample_size, track_provenance}`、`regression_flags.flags`、`benchmark_version`、`run_tier`、`sampling_k`、`candidate_ref` | `diagnostic/datamodel.py` `DiagnosticReport.to_dict` | `summarizeDiagnostic`(既有) |
| 配方候选载荷 `recipe_type, dimension, source_run_id, eval_anchor{...}, result{gain, baseline_ref, candidate_ref, promoted}, repro{errors, warnings}, nondeterministic` | 设计 §4.2;写方是计划 5 `build_recipe_candidate`,`repro` 来自计划 3 `validate_reproducibility` | `summarizeRecipeCandidate`;`approve` 的证据规则见下一行 |
| 审核门③"证据在场":`payload.repro` 是对象,且 `repro.errors`、`repro.warnings` 都是列表(可为空);`repro` 缺失、为 `null`、为数组或标量,或 `errors`/`warnings` 任一不是列表(例如是字典或字符串),都算缺证据;`errors` 非空仍允许批准 | `recipestore/admit.py` 的 `has_repro_evidence`(计划 3 任务 45,收录处理器用它兜底) | `commands.ts` 的 `hasReproEvidence`(`approve` 调它;注释指向 Python 一侧) |
| 提案载荷 `round_id, finding{id, pattern, support, ratio, episode_refs}, increments[{kind, definition}], verification{method, episodes, fired_on_motivating, fired_elsewhere, notes}` | 设计 §4.2;写方是计划 7 `package`。`increments[].definition` 为 `actio` 的 `Skill.to_dict()` / `MemoryEntry.to_dict()`(计划 6「对外契约:技能、记忆、推理经验的字典形状」),技能 id 以 `skill-` 开头、记忆 id 以 `mem-` 开头 | `summarizeProposal`;`approve` 要求 `verification` 是非空对象 |
| 驳回原因 `payload.reject_reason` | 计划 5 的上下文装配读它(`PlanningContext.candidate.reject_reason`) | `reject`(既有,三类产物通用) |

## Rulings

设计与现有代码冲突、或设计没规定的细节,在这里裁决。

1. **Ruling: `GateView` 的 `id` 取产物信封的 `id`,不取文件名。** —— 原因:命令行按信封 id 找产物,两侧用同一个键才对得上;计划 1 约定 `proposal_file(proposal_id)` 的文件夹名就是产物 id,配方候选的文件名是 `recipe-<run>`,正常情况下两者一致。 —— 判错的代价:有人手写的产物文件名与 id 不一致时,收录处理器按 id 拼路径会找不到文件;处理器应按 `id` 用路径函数重新定位,找不到时判失败。
2. **Ruling: `admit_recipe` 与 `admit_proposal` 的 `detail` 为空字典。** —— 原因:设计只规定了 `target_id`;配方候选 id 与来源 run 的互转归计划 3 任务 40 的 `recipestore.ids.run_id_of`,`controlplane` 里再写一份前缀解析会出现第二处实现。 —— 判错的代价:处理器多一次函数调用。
3. **Ruling: 闭环动作排在既有动作之后,顺序为 `admit_recipe → admit_proposal → record_track → nominate_recipe → plan_next_round → start_agent_round`。** —— 原因:同一 tick 里先收录,规划处理器读配方库时就能看到刚收录的配方;战绩先于规划,`verdict` 降级的配方不会被同一 tick 的规划选中;提案收录先于智能体轮次,新轮次绑定最新库版本。 —— 判错的代价:只影响同一 tick 内的先后,下一 tick 会自然收敛;最坏是规划慢一个 tick 看到新配方。
4. **Ruling: `nominate_recipe` 与 `record_track` 不受在途轮限制,对每个满足条件的 run 都产生。** —— 原因:设计 §3.2 只给 `plan_next_round` 加了"没有在途轮";提名与战绩是对已结束 run 的记账,不启动新训练,不会失控。 —— 判错的代价:无。
5. **Ruling: `start_agent_round` 只看版本号最大的 `effective` 模型;它的轮次已到终态(`executed` / `failed`)时,较旧的模型即使没有轮次也不补跑。** —— 原因:设计 §3.2 的字面意思;补跑旧模型的轮次只会产出马上被 superseded 的提案(计划 7 也会把旧的 `pending` 轮次置 `failed`)。 —— 判错的代价:错过旧模型的经验分析,可由人手写轮次产物补。
6. **Ruling: `RunView` 对每个 run 目录都产生一条,包括没有指令的目录;`recipe_ref` 为 `None` 或非字符串时视为 `""`。** —— 原因:人工或旧代码可能只写了候选;把畸形的 `recipe_ref` 当成"没有配方"不会触发战绩回写,是保守的方向。 —— 判错的代价:畸形 `recipe_ref` 的 run 不回写战绩,而不是报错。
7. **Ruling: `handlers` 里出现六个动作之外的键,在执行任何动作之前抛 `ValueError`(`apply_action`、`reconcile_once`、`run_daemon` 都检查)。** —— 原因:`{"dispatch_directive": h}` 这类误配若被接受,会让人以为替换了派发器;拼错的键(`"nominate"`)若被静默忽略,对应动作永远不触发且没有任何提示。 —— 判错的代价:组合根不能用 `handlers` 覆盖既有动作;那本来就该用 `dispatcher` 和 `evaluator`。
8. **Ruling: `enabled` 为空时 `accept_mock_evidence` 不写进 `TreeState`。** —— 原因:任务要求"`enabled` 为空时返回值与现在逐字节相同";没有闭环动作时这个开关无处可用。 —— 判错的代价:无。
9. **Ruling: `backends` 标记通过扩展 `write_report(store, run_id, report, *, backends=None)` 与 `run_diagnostic(..., *, backends=None)` 写入,而不是在 `run_diagnostic` 之后重写信封。** —— 原因:重写信封会产生两次提交,两次之间树里有一份没有标记、按计划 1 的规则会被判为 `real` 的报告;若进程恰好崩在这里,一份 mock 报告会被当成真证据驱动规划。参数默认 `None` 时载荷不变,既有 `tests/test_diag_*.py` 不受影响。 —— 判错的代价:`run_diagnostic` 多一个参数。
10. **Ruling: 题库与分类表的产物类型名为 `diagnostic_taxonomy` 与 `diagnostic_item_pool`,状态 `effective`,读写函数放 `diagnostic/assets.py`,信封 id 为 `taxonomy-<version>` 与 `item-pool-<version>`。** —— 原因:`diagnostic.paths` 已经转出这两个路径函数,资产属于诊断包;`effective` 表示"登记即生效",题库变更本身不走审核门(设计没有给它门)。读取时校验信封类型与载荷里的版本号,路径上放错东西会明确报错。 —— 判错的代价:以后要给题库加审核门时,要改成审核类状态并迁移已有资产。
11. **Ruling: 评估器的基线 = 版本号最大的 `effective` 模型的 `links[1]` 那个 run 的报告;以下情况都视为"无基线",传空列表、`baseline_ref="none"`:没有模型、`links` 不足两项、基线 run 就是被评的 run、基线 run 没有报告、基线报告是 `stub`、基线报告的 `benchmark_version` 与本次不同。** —— 原因:设计 §10.2 只说"基线模型所在 run 的报告";`stub` 报告没有分数;跨版本分数不可比(诊断设计 §2.5.8),与其让 `detect_regressions` 在不可比的分数上打标记,不如明说没有基线。评估器不复用 `controlplane.models.latest_promoted_checkpoint`,因为那个函数在最新模型缺 `checkpoint_ref` 时会抛错,而选基线不需要 checkpoint。 —— 判错的代价:可比性只比较 `benchmark_version`,报告载荷里没有 `item_pool_version`;换题库必须同时换 `benchmark_version`,否则会跨题库比较、在不可比的分数上打退化标记。换了 `benchmark_version` 之后的第一轮没有退化标记,审核人在 `review` 里看到 `baseline_ref=none`。(另见 Ruling 22:没有分数、或证据等级与本次不同的基线也视为无基线。)
12. **Ruling: 评估器把 `candidate_model_id` 设为候选 id,只跑静态轨(`dynamic_specs=[]`)。** —— 原因:V1 没有动态轨的情境与绑定来源;`candidate_model_id` 只在动态轨里用于 judge 的自评偏差检查。 —— 判错的代价:接动态轨时要换成真正的模型 id,否则自评偏差检查形同虚设。
13. **Ruling: 评估器缺资产时抛 `DiagnosticAssetError`,不吞,不写报告。** —— 原因:与派发器"瞬态异常原样上抛"一致;缺资产是部署错误,补齐后重启守护进程即可恢复。若改成写一份空报告,那份报告会因为没有分数而被判 `stub`,候选卡在审核门前且看不出原因。 —— 判错的代价:资产缺失时守护进程每次启动都会在同一处退出,需要外部监督。
14. **Ruling: 命令行 `review/approve/reject` 按 id 在三类路径里找;同一 id 命中多个产物时报 `ambiguous`,不猜;找不到时错误信息保持原文 `candidate <id> not found`。** —— 原因:保持既有错误信息,不改既有测试的预期;歧义报错比"取第一个"安全。 —— 判错的代价:错误信息里的 "candidate" 对配方与提案不完全贴切。
15. **Ruling: `approve` 的证据检查:配方候选按 `hasReproEvidence`,与计划 3 的 Python 版 `recipestore.admit.has_repro_evidence` 逐条一致:`payload.repro` 是对象,且 `repro.errors` 与 `repro.warnings` 都是数组(可为空数组);`repro` 缺失、为 `null`、为数组或标量,或 `errors`/`warnings` 任一不是列表(例如是字典或字符串),都算缺证据,所以 `{}` 与 `{"x": 1}` 也被拒绝,`{"errors": [], "warnings": []}` 放行;`repro.errors` 非空也允许批准。提案要求 `payload.verification` 是非空对象(`null`、`{}`、数组、标量都算缺)。** —— 原因:设计 §6.1"校验结果写进候选,不硬拒绝,由审核门③的人参考",门只保证证据在场;命令行与收录处理器是同一道门的两个入口,规则不一致时会出现"命令行批准、收录处理器拒收"的卡死状态。 —— 判错的代价:有复现错误的配方可能被批准,`review` 会把错误逐条打印出来;两侧规则若有一侧单独改动,门会重新分裂。`commands.ts` 的 `hasReproEvidence` 注释指向 Python 一侧;`recipestore/admit.py` 归计划 3,本计划不能在那边加反向注释(见「跨语言约定」)。
16. **Ruling: `reject` 对三类产物都不要求证据。** —— 原因:没有证据恰恰是驳回的常见理由。 —— 判错的代价:无。
17. **Ruling: `confirm` 用 `NONREVIEW_TRANSITIONS` 表判定能否迁到 `pending`;只有 `awaiting_confirmation` 能确认。拒绝时不写文件、不提交。** —— 原因:设计 §3.4。表里 `pending → pending` 不存在,所以对已确认的指令再确认会被拒绝,而不是静默成功。 —— 判错的代价:重复确认要看报错信息才知道已经确认过。
18. **Ruling: `status` 的输出改为分节格式(`awaiting review:` / `awaiting confirmation ...:`),空时打印 `nothing awaiting review or confirmation`。** —— 原因:设计 §10.1 要求同时列四类东西,需要分组;既有用例只断言 `toContain("cand-run-001")`,不受影响。 —— 判错的代价:依赖旧输出格式的脚本要改;仓库里没有这样的脚本(`grep -rln "no candidates awaiting review" cli/src cli/test src tests scripts` 只命中 `cli/src/commands.ts`;另一处在已完成的旧计划文档里)。
19. **Ruling: `[MOCK BACKENDS]` 标在诊断行首,并另起一行解释;`backends` 取值既不是 `mock` 也不是 `real` 时打印 `backends: UNREADABLE`。** —— 原因:与 `[STUB]` 的处理方式一致,畸形值不能当成 `real`。 —— 判错的代价:无。
20. **Ruling: `start_agent_round` 在轮次产物不存在、读不出、或状态为 `pending` 时都触发;`ModelView` 因此带 `agent_round_status`(信封状态,没有可用产物时为 `None`),`has_agent_round` 降为派生属性。** —— 原因:设计 R19 与 §3.2。若只在"没有产物"时触发,一次瞬态失败(或进程被中途杀掉)留下的 `pending` 轮次永远不会再被派发,直到有更新的模型晋级把它置为 superseded,计划 7 承诺的"瞬态失败后下一 tick 重入"在守护进程里不成立;这与 `plan_next_round` 的"决策为空或 `pending`"同一模式。"文件在、读不出"(YAML 解析失败、信封校验失败、`schema_version` 无法迁移等)按"没有可用轮次"处理而不是抛异常,否则一份坏文件会让每个 tick 都在读视图时退出;`OSError` 不在其列,原样上抛。改动的代价:终态写回之前轮次处理器每个 tick 都会被唤醒,必须靠它自己的幂等键挡住重复的长任务——先写 `pending` 轮次产物,重入时整份重写同一产物,并按产物里的进度决定续跑还是跳过(见「对外契约」的轮次处理器重入契约)。 —— 判错的代价:若有轮次处理器不幂等,每个 tick 都会重复跑一次完整的部署/会话;反过来若本裁决不成立而维持旧条件,瞬态失败的轮次会静默卡死。
21. **Ruling: `plan_next_round` 的"最大 run"按 `run-<数字>` 的整数比较;不匹配 `^run-[0-9]+$` 的名字不参与;一个都没有时不触发。在途轮(R4)仍看全部 run。** —— 原因:设计 §3.2(已改为数值序)。字符串序下,计划 5 允许的不补零 `run-9` 会压过按数值分配的 `run-011`,规划静默停住;超过 999 轮(`run-1000` < `run-999`)、手工建的 `scratch` 目录同理。树里没有任何 `run-<数字>` 只可能是手工造的目录,此时宁可停住等人写指令,也不去规划一个名字不合约定的 run。`record_track` 与 `nominate_recipe` 的输出顺序仍按 run id 字典序(Ruling 3),它们不选"最大"。同编号只可能来自手工命名,按 `(编号, run_id)` 取最大使结果与输入顺序无关(`run-7` 胜 `run-007`)。 —— 判错的代价:若有人用别的命名(例如 `run_012`)写指令,它永远不会成为规划对象,闭环在它走完后停住;在途时它仍会挡住规划,所以不会并发多轮。
22. **Ruling: `resolve_baseline` 不假定基线报告有分数;基线报告的证据等级与本次评估的 `backends` 不同时也视为无基线。** —— 原因:`diagnostic_grade` 对 `backends: "mock"` 不看分数就返回 `mock`,一份只有标记、没有 `dimension_scores` 的报告原先会让 `payload["dimension_scores"]` 抛 `KeyError`,守护进程退出;现在 `dimension_scores` 不是非空列表就返回无基线。真分数对假分数(`real` 对 `mock`,或反过来)打退化标记没有意义,所以评估器把自己的 `backends` 以 keyword-only 参数 `backends=` 传给 `resolve_baseline`;不传时(计划 9 的一致性核对就这样调)不比较等级,行为与原来相同。 —— 判错的代价:从 mock 后端切到 real 后端后的第一轮没有退化标记,`baseline_ref=none`。
23. **Ruling: `_read_run_views` 定诊断等级时"判不出来按最弱算":载荷不是 dict、或报告文件读不出(与轮次产物同一组异常),都记为 `stub`。** —— 原因:`diagnostic_grade` 对非 dict 载荷抛 `TypeError`(计划 1 的有意设计,调用方负责),在读视图时抛出会让守护进程每个 tick 都退出。`stub` 永不可接受,所以一份坏报告只会让规划、提名、战绩不触发,不会被当成证据。正常信封经 `load_product` 的 schema 校验,载荷总是 dict;这条兜底针对校验之外的读法与将来的迁移器。`has_diagnostic` 仍按文件存在判断,`promote_candidate` 的"有诊断才晋级"不受影响。 —— 判错的代价:报告损坏时 `review` 仍会显示它(命令行另有自己的读法),而闭环一侧静默不动;要看 `diagnostic_grade` 为 `stub` 才知道原因。
24. **Ruling: `confirm` 与 `findGateProduct` 一样收集全部命中,多处命中报 `directive <id> is ambiguous`,不写文件、不提交。** —— 原因:原来用 `.find` 取第一个命中;人手复制 run 目录时同一信封 id 会出现两次,确认哪一条取决于 `git ls-files` 的顺序。与 Ruling 14 同一取舍:歧义报错比"取第一个"安全。 —— 判错的代价:无;歧义时由人删掉多余的那份再确认。

## 测试计数

Python 用"起点 B 加增量"记账。B 是本计划开始时(计划 1 已落地,也可能已有计划 2–7 落地)的 Python 全量通过数:任务 90 Step 1 先 `BASE=$(git rev-parse HEAD)`,再跑一次全量,把实测的 B 与 `BASE` 写进执行记录。本计划只保证增量;绝对数取决于在它之前落地了哪些计划。括号里是撰写时在 `main`(820)加计划 1 临时桩的拷贝里实测的数(临时桩不加测试,所以那里 B=820)。

命令行测试不受其他计划影响(其他计划都不碰 `cli/`),照写绝对数。

| 任务后 | 本任务 Python 增量 | Python 累计(820 起点实测) | 命令行累计(绝对数) |
|---|---|---|---|
| 90 | +56 | B+56(876) | 18 |
| 91 | +32 | B+88(908) | 18 |
| 92 | +20 | B+108(928) | 18 |
| 93 | +16 | B+124(944) | 18 |
| 94 | +15 | B+139(959) | 18 |
| 95 | +21 | B+160(980) | 18 |
| 96 | 0 | B+160(980) | 53(+35) |
| 97 | 0 | B+160(980) | 64(+11) |
| 98 | 0 | B+160(980) | 64 |

## File Structure

| 文件 | 动作 | 职责 |
|---|---|---|
| `src/controlplane/reconcile.py` | 修改(77 → 218 行,+141 −0) | `RunView`、`ModelView`(含 `agent_round_status` 与派生属性 `has_agent_round`)、`GateView`;`TreeState` 五个新字段;`CLOSED_LOOP_ACTIONS`、`has_round_in_flight`、`_latest_numbered_run`、`_closed_loop_actions` |
| `src/controlplane/daemon.py` | 修改(178 → 359 行,+192 −11) | `Handler`、`Evaluator`;`read_tree_state` 的 `enabled`、`accept_mock_evidence` 与三个视图读取函数;`_agent_round_status`(轮次产物的信封状态,读不出时为 `None`);`_report_grade`(诊断等级,判不出来记 `stub`);`_check_handlers`;三个入口的新参数 |
| `src/diagnostic/store_adapter.py` | 修改(30 → 44 行) | `BACKENDS_KEY`、`BACKENDS_VALUES`;`write_report` 的 `backends` |
| `src/diagnostic/orchestrator.py` | 修改(240 → 244 行) | `run_diagnostic` 的 `backends` |
| `src/diagnostic/assets.py` | 新建(74 行) | 分类表与题库的信封读写 |
| `src/diagnostic/evaluator.py` | 新建(111 行) | `make_diagnostic_evaluator`、`resolve_baseline`、`NO_BASELINE` |
| `cli/src/commands.ts` | 修改(143 → 347 行) | 三类路径查找、按类型的 `review` 与 `approve` 证据规则、`confirm`、新 `status` |
| `cli/src/index.ts` | 修改(31 → 37 行) | `USAGE`、`confirm` 分发 |
| `tests/test_reconcile_closed_loop.py` | 新建(88 个用例) | 六个动作的触发与不触发、在途轮、重入(含 `pending` 轮次再次触发)、最大 run 的数值序 |
| `tests/test_daemon_closed_loop_views.py` | 新建(20 个用例) | 视图读取(含轮次产物的信封状态与读不出的轮次产物、非 dict 或读不出的诊断报告);`enabled` 为空时与旧形状相同 |
| `tests/test_daemon_closed_loop_seams.py` | 新建(16 个用例) | 处理器与评估器的注入;不注入时整棵树与提交日志相同 |
| `tests/test_diag_assets.py` | 新建(15 个用例) | 资产读写、`backends` 标记、`stub` 与 `mock` 的等级 |
| `tests/test_diag_evaluator.py` | 新建(21 个用例) | 评估器、基线解析(含无分数与证据等级不同的基线)、注入调和循环、`diagnostic` 不导入 `mindtrainer` |
| `cli/test/gates.test.ts` | 新建(46 个用例) | 新命令行行为 |

**不改的文件:** `tests/` 与 `cli/test/` 下所有既有文件;`src/simulator/stubs.py`;`src/controlplane/paths.py`、`states.py`、`evidence.py`、`config.py`(归计划 1);`schemas/`;`pyproject.toml`(没有新增顶层包:`diagnostic` 已在 wheel 列表里);`cli/package.json`、`cli/package-lock.json`。

---

### Task 90: 前置检查、闭环视图与 `plan_next_round`

**Files:**
- Modify: `src/controlplane/reconcile.py`(77 → 178 行,+101 −0)
- Test: `tests/test_reconcile_closed_loop.py`(新建,本任务 56 个用例)

**Interfaces:**
- Consumes: 计划 1 产出的 `controlplane.evidence.evidence_acceptable`;计划 1 产出的非审核类状态 `awaiting_confirmation`(本任务只把它当字符串比较)。
- Produces(任务 91–93 与计划 3、5、7、9 依赖):
  - `RunView(run_id, directive_status, recipe_ref, candidate_status, has_diagnostic, diagnostic_grade, planner_decision_status, has_nomination_note, has_track_note)`,冻结。
  - `ModelView(model_id, version, run_id, status, agent_round_status)`,冻结。`agent_round_status: str | None` 是 `agent_round_file(model_id)` 的信封状态,没有产物(或产物读不出)时为 `None`;`has_agent_round` 是只读派生属性,恒等于 `agent_round_status is not None`,保留给只关心"有没有轮次"的读者(不是构造参数)。
  - `GateView(id, kind, status)`,冻结;`kind` 取 `"recipe_candidate"` 或 `"skill_memory_proposal"`。
  - `TreeState` 新字段:`runs: list[RunView] = []`、`models: list[ModelView] = []`、`gates: list[GateView] = []`、`enabled: frozenset[str] = frozenset()`、`accept_mock_evidence: bool = False`。
  - `CLOSED_LOOP_ACTIONS: frozenset[str]`(六个名字)、`has_round_in_flight(state) -> bool`;私有辅助 `_latest_numbered_run(runs) -> RunView | None`(只给本模块用)。
  - `plan_next_round` 动作:`Action("plan_next_round", <run id>, {"run_id": <run id>})`。

**设计要点:**
- **前置检查先跑。** 计划 1 没有落地时本计划的代码导入即失败;先用一条 `python -c` 断言把这一点挑明,而不是让实施者对着一堆 `ImportError` 猜原因。
- **新字段全部带默认值,追加在 `TreeState` 末尾。** `tests/test_reconcile.py` 用关键字或前三个位置参数构造 `TreeState`,不受影响。
- **新逻辑只在 `state.enabled` 非空时运行**,而且追加在既有动作之后。既有动作的产生逻辑一行不改。
- **`_acceptable` 调 `evidence_acceptable`**,不另写"real 或者 mock 且开关打开"的判断。等级为 `None`(没有报告)时直接判不可接受,因为 `evidence_acceptable` 只认三个等级名。
- **`plan_next_round` 只看编号最大的那个 run**(R5,设计 §3.2,Ruling 21):`_latest_numbered_run` 只让匹配 `^run-([0-9]+)$` 的 run 参与比较,按整数取最大;`scratch` 之类不匹配的名字排最后、不参与;一个 `run-<数字>` 都没有时返回 `None`,不触发(刻意失败关闭)。同编号(`run-7` 与 `run-007`)时 `max` 的键是 `(编号, run_id)`,按 run id 字典序确定性平局,与输入顺序无关(有测试)。只检查这个 run 的条件,满足才产生动作。较早 run 即使满足全部条件也不规划;最大 run 的指令 `failed`、没有候选时,闭环停住等人(有测试)。**在途轮(R4)仍看全部 `RunView`**,包括不匹配的名字(有测试)。不按字符串序:计划 5 允许不补零的 `run-9` 并按数值分配 `run-011`,字符串序下 `run-9` 永远"最大",规划会静默停住;超过 999 轮同理(有测试)。`controlplane` 不能导入 `trainscheduler`,所以这个小辅助在 `reconcile.py` 里自己写。
- **重入行为由纯函数测试钉住**(见「对外契约」的规划处理器重入契约):崩在写指令之前会再次触发,崩在写指令之后不会。
- **`ModelView` 存轮次产物的信封状态,不存布尔**(R19,Ruling 20):`agent_round_status: str | None` 是第五个字段;`has_agent_round` 改成只读 `@property`,恒等于 `agent_round_status is not None`(有参数化测试),旧读者照用,但它不再是构造参数。
- 本任务的 `_closed_loop_actions` 只产生 `plan_next_round`;任务 91 换成六个动作的完整版。

- [ ] **Step 1: 前置检查(计划 1 是否已落地)**

Run:

```bash
uv run python -c "
from controlplane.paths import recipe_candidate_file, proposal_file, agent_round_file, planner_decision_file, nomination_file, recipe_track_note_file
from controlplane.evidence import diagnostic_grade, evidence_acceptable
from controlplane.states import can_transition
assert can_transition('nonreview', 'awaiting_confirmation', 'pending')
print('plan 1 prerequisites present')"
```

Expected: `plan 1 prerequisites present`。

然后记下起点:

```bash
BASE=$(git rev-parse HEAD); echo "BASE=$BASE"
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error | tail -1
```

Expected: 第一行打印 `BASE=<提交号>`;第二行是 `<B> passed`,无警告。把 `BASE` 与 B 写进执行记录,后面每个任务的全量预期都是 B 加「测试计数」表里的累计增量;任务 98 Step 3 用 `BASE` 核对改动范围。

**若前置检查报 `ImportError` 或 `AssertionError`:停下,报告"计划 1 尚未落地",不要继续本计划。** 在当前 `main`(`0385004`)上实测的输出是 `ImportError: cannot import name 'recipe_candidate_file' from 'controlplane.paths'`。

> 本计划撰写时计划 1 尚未落地。为了在临时拷贝里真实验证,撰写者在拷贝里放了下面三处**临时桩,它们由计划 1 取代**,实施者不要把它们提交进仓库:
> - `src/controlplane/paths.py` 末尾追加六个函数,返回的路径与计划 1 任务 19 的同名函数相同(docstring 略)(`recipe_candidate_file`、`proposal_file`、`agent_round_file`、`planner_decision_file`、`nomination_file`、`recipe_track_note_file`);
> - `src/controlplane/states.py` 的 `NONREVIEW_TRANSITIONS` 加一行 `"awaiting_confirmation": {"pending", "failed"},`;
> - `src/controlplane/evidence.py` 取计划 1 任务 24 的全文(37 行)。
>
> 这三处不新增也不删除任何测试,所以验证时的基数仍是 820。

- [ ] **Step 2: 写失败测试**

新建 `tests/test_reconcile_closed_loop.py`:

```python
"""reconcile 扩展(闭环集成设计 §3.1、§3.2):六个新动作的触发与不触发。全部是纯函数测试。"""
import dataclasses

import pytest

from controlplane.reconcile import (
    CLOSED_LOOP_ACTIONS,
    Action,
    CandidateView,
    DirectiveView,
    GateView,
    ModelView,
    RunView,
    TreeState,
    has_round_in_flight,
    reconcile,
)

ALL = frozenset(CLOSED_LOOP_ACTIONS)


def _run(run_id="run-001", **kw) -> RunView:
    base = dict(
        run_id=run_id, directive_status="executed", recipe_ref="", candidate_status="effective",
        has_diagnostic=True, diagnostic_grade="real", planner_decision_status=None,
        has_nomination_note=False, has_track_note=False,
    )
    base.update(kw)
    return RunView(**base)


def _kinds(actions, kind):
    return [a for a in actions if a.kind == kind]


def _plan(state):
    return _kinds(reconcile(state), "plan_next_round")


# ---- 视图与默认值 ----

def test_new_tree_state_fields_all_default_to_empty():
    s = TreeState()
    assert (s.runs, s.models, s.gates, s.enabled, s.accept_mock_evidence) == ([], [], [], frozenset(), False)


def test_the_six_action_names_are_fixed():
    assert CLOSED_LOOP_ACTIONS == {
        "plan_next_round", "nominate_recipe", "record_track",
        "start_agent_round", "admit_recipe", "admit_proposal",
    }


def test_views_are_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        _run().run_id = "x"
    with pytest.raises(dataclasses.FrozenInstanceError):
        ModelView("zengen-v1", 1, "run-001", "effective", None).status = "x"
    with pytest.raises(dataclasses.FrozenInstanceError):
        GateView("recipe-run-001", "recipe_candidate", "approved").status = "x"


@pytest.mark.parametrize("round_status,has_round", [(None, False), ("pending", True), ("executed", True), ("failed", True)])
def test_has_agent_round_is_derived_from_the_round_status(round_status, has_round):
    assert ModelView("zengen-v1", 1, "run-001", "effective", round_status).has_agent_round is has_round


def test_without_enabled_names_the_views_produce_nothing():
    state = TreeState(
        runs=[_run()],
        models=[ModelView("zengen-v1", 1, "run-001", "effective", None)],
        gates=[GateView("recipe-run-001", "recipe_candidate", "approved"),
               GateView("proposal-r-1", "skill_memory_proposal", "approved")],
    )
    assert reconcile(state) == []


def test_an_awaiting_confirmation_directive_is_never_dispatched():
    state = TreeState(directives=[DirectiveView("run-002", "awaiting_confirmation", "tom", "")], enabled=ALL)
    assert reconcile(state) == []


# ---- 在途轮(R4) ----

@pytest.mark.parametrize("directive_status", ["pending", "awaiting_confirmation"])
def test_a_pending_or_unconfirmed_directive_is_a_round_in_flight(directive_status):
    assert has_round_in_flight(TreeState(runs=[_run("run-002", directive_status=directive_status, candidate_status=None)]))


@pytest.mark.parametrize("candidate_status", ["pending_review", "approved"])
def test_an_unreviewed_or_unpromoted_candidate_is_a_round_in_flight(candidate_status):
    assert has_round_in_flight(TreeState(runs=[_run("run-002", candidate_status=candidate_status)]))


@pytest.mark.parametrize("directive_status", ["executed", "failed", "consumed", None])
@pytest.mark.parametrize("candidate_status", ["effective", "rejected", None])
def test_finished_rounds_are_not_in_flight(directive_status, candidate_status):
    assert not has_round_in_flight(
        TreeState(runs=[_run(directive_status=directive_status, candidate_status=candidate_status)])
    )


# ---- plan_next_round ----

@pytest.mark.parametrize("candidate_status", ["effective", "rejected"])
def test_plan_triggers_on_a_terminal_candidate_with_real_evidence(candidate_status):
    state = TreeState(runs=[_run(candidate_status=candidate_status)], enabled=frozenset({"plan_next_round"}))
    assert reconcile(state) == [
        Action(kind="plan_next_round", target_id="run-001", detail={"run_id": "run-001"})
    ]


@pytest.mark.parametrize("candidate_status", ["pending_review", "approved", "draft", "deprecated", None])
def test_plan_does_not_trigger_before_the_candidate_is_terminal(candidate_status):
    state = TreeState(runs=[_run(candidate_status=candidate_status)], enabled=ALL)
    assert _plan(state) == []


def test_a_failed_directive_does_not_trigger_planning():
    # R5:指令失败,没有候选,闭环停住等人
    state = TreeState(runs=[_run(directive_status="failed", candidate_status=None,
                                 has_diagnostic=False, diagnostic_grade=None)], enabled=ALL)
    assert reconcile(state) == []


@pytest.mark.parametrize("candidate_status", ["effective", "rejected"])
@pytest.mark.parametrize("grade", ["stub", None])
def test_plan_never_triggers_on_stub_or_missing_evidence(grade, candidate_status):
    for accept in (False, True):
        state = TreeState(runs=[_run(diagnostic_grade=grade, has_diagnostic=grade is not None,
                                     candidate_status=candidate_status)],
                          enabled=ALL, accept_mock_evidence=accept)
        assert _plan(state) == []


def test_mock_evidence_needs_the_explicit_switch():
    run = _run(diagnostic_grade="mock")
    assert _plan(TreeState(runs=[run], enabled=ALL)) == []
    assert [a.target_id for a in _plan(TreeState(runs=[run], enabled=ALL, accept_mock_evidence=True))] == ["run-001"]


@pytest.mark.parametrize("decision", ["executed", "failed"])
def test_a_finished_planner_decision_is_the_idempotency_key(decision):
    state = TreeState(runs=[_run(planner_decision_status=decision)], enabled=ALL)
    assert _plan(state) == []


def test_a_pending_planner_decision_still_triggers():
    state = TreeState(runs=[_run(planner_decision_status="pending")], enabled=ALL)
    assert _plan(state) == [Action(kind="plan_next_round", target_id="run-001", detail={"run_id": "run-001"})]


def test_only_the_largest_run_id_is_planned():
    state = TreeState(runs=[_run("run-003"), _run("run-001"), _run("run-002")], enabled=ALL)
    assert [a.target_id for a in _plan(state)] == ["run-003"]


def test_an_earlier_finished_run_is_not_planned_when_a_larger_run_exists():
    """较早 run 的候选已终态、证据可接受,但存在 run id 更大的 run:不产生 plan_next_round。"""
    newer_without_evidence = _run("run-002", candidate_status="rejected", has_diagnostic=False, diagnostic_grade=None)
    newer_already_planned = _run("run-002", planner_decision_status="executed")
    for newer in (newer_without_evidence, newer_already_planned):
        assert _plan(TreeState(runs=[_run("run-001"), newer], enabled=ALL)) == []


def test_the_largest_run_is_chosen_by_numeric_order():
    # 数值序:run-1000 大于 run-999(字符串序会选 run-999)
    state = TreeState(runs=[_run("run-999"), _run("run-1000"), _run("run-010")], enabled=ALL)
    assert [a.target_id for a in _plan(state)] == ["run-1000"]


def test_an_unpadded_run_id_does_not_shadow_a_larger_padded_one():
    # 计划 5 允许不补零的 run-9,并按数值分配 run-011;字符串序下 run-9 永远"最大",规划会静默停住
    state = TreeState(runs=[_run("run-9", planner_decision_status="executed"), _run("run-011")], enabled=ALL)
    assert [a.target_id for a in _plan(state)] == ["run-011"]


@pytest.mark.parametrize("order", [("run-7", "run-007"), ("run-007", "run-7")])
def test_a_tie_on_the_run_number_is_broken_by_run_id_regardless_of_order(order):
    # 手工命名才会出现的同编号:按 run id 字典序取最大("run-7" > "run-007"),不看输入顺序
    state = TreeState(runs=[_run(r) for r in order], enabled=ALL)
    assert [a.target_id for a in _plan(state)] == ["run-7"]


def test_a_non_numbered_directory_does_not_block_planning():
    state = TreeState(runs=[_run("run-001"), _run("scratch", candidate_status=None, has_diagnostic=False,
                                                  diagnostic_grade=None)], enabled=ALL)
    assert [a.target_id for a in _plan(state)] == ["run-001"]


def test_no_numbered_run_means_no_planning():
    # 刻意失败关闭:树里一个 run-<数字> 都没有(只可能是手工造的目录)时不规划
    state = TreeState(runs=[_run("scratch"), _run("run-abc"), _run("run-")], enabled=ALL)
    assert _plan(state) == []


def test_a_non_numbered_directory_still_counts_as_a_round_in_flight():
    # R4 仍看全部 run:不参与"最大"的比较,不等于不算在途
    state = TreeState(runs=[_run("run-001"), _run("scratch", directive_status="pending", candidate_status=None,
                                                  has_diagnostic=False, diagnostic_grade=None)], enabled=ALL)
    assert _plan(state) == []


def test_a_failed_largest_directive_without_a_candidate_stops_planning():
    """R5:最大 run 的指令 failed、没有候选 → 不触发,即使较早的 run 满足全部条件。"""
    failed = _run("run-002", directive_status="failed", candidate_status=None,
                  has_diagnostic=False, diagnostic_grade=None)
    assert _plan(TreeState(runs=[_run("run-001"), failed], enabled=ALL)) == []


def test_a_round_in_flight_blocks_planning_anywhere():
    state = TreeState(
        runs=[_run("run-001"), _run("run-002", directive_status="pending", candidate_status=None,
                                    has_diagnostic=False, diagnostic_grade=None)],
        enabled=ALL,
    )
    assert _plan(state) == []


def test_reentry_after_a_crash_before_the_directive_retriggers_the_source_run():
    """规划处理器先写 planner_decision(pending, next_run_id),再写指令,最后置 executed。
    崩在"决策 pending、指令未写":来源 run 仍是最大 run,决策 pending → 再次触发;
    处理器复用已记下的 next_run_id 续写指令(对外契约,见计划正文)。"""
    src = _run("run-001", planner_decision_status="pending")
    assert _plan(TreeState(runs=[_run("run-000", planner_decision_status="executed"), src], enabled=ALL)) == [
        Action(kind="plan_next_round", target_id="run-001", detail={"run_id": "run-001"})
    ]


def test_reentry_after_a_crash_after_the_directive_does_not_retrigger_the_source_run():
    """崩在"指令已写、决策仍 pending":新指令所在 run 成为最大 run。
    1) 它在途(awaiting_confirmation)时,不规划;
    2) 它走完后,规划落在新 run 上,来源 run 不再触发;装配上下文时把那条 pending 决策按 executed 对待。"""
    src = _run("run-001", planner_decision_status="pending")
    in_flight = _run("run-002", directive_status="awaiting_confirmation", candidate_status=None,
                     has_diagnostic=False, diagnostic_grade=None)
    assert _plan(TreeState(runs=[src, in_flight], enabled=ALL)) == []

    finished = _run("run-002", candidate_status="rejected")
    assert _plan(TreeState(runs=[src, finished], enabled=ALL)) == [
        Action(kind="plan_next_round", target_id="run-002", detail={"run_id": "run-002"})
    ]


def test_plan_needs_its_own_name_enabled():
    state = TreeState(runs=[_run()], enabled=ALL - {"plan_next_round"})
    assert _plan(state) == []


def test_reconcile_with_closed_loop_views_is_still_pure():
    state = TreeState(runs=[_run("run-002"), _run("run-001")], enabled=ALL)
    first, second = reconcile(state), reconcile(state)
    assert first == second
    assert [a.target_id for a in _plan(state)] == ["run-002"]
    assert [r.run_id for r in state.runs] == ["run-002", "run-001"]  # 入参未被排序
```

- [ ] **Step 3: 运行,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_reconcile_closed_loop.py`
Expected: 收集阶段报错 `ImportError: cannot import name 'CLOSED_LOOP_ACTIONS' from 'controlplane.reconcile'`,`1 error`。

- [ ] **Step 4: 写实现**

在仓库根目录运行下面的命令,把差异从标准输入交给 `git apply`(不落盘补丁文件,`git status` 里不会多出东西);差异应用到 `src/controlplane/reconcile.py`,共 +101 −0 行:

```bash
git apply <<'PATCH'
--- a/src/controlplane/reconcile.py
+++ b/src/controlplane/reconcile.py
@@ -1,7 +1,10 @@
 from __future__ import annotations
 
+import re
 from dataclasses import dataclass, field
 
+from controlplane.evidence import evidence_acceptable
+
 
 @dataclass(frozen=True)
 class DirectiveView:
@@ -21,11 +24,54 @@
     has_diagnostic: bool = False
 
 
+@dataclass(frozen=True)
+class RunView:
+    """闭环集成设计 §3.1:一个 run 目录在闭环动作眼里的样子。只在有处理器被注入时才填。"""
+
+    run_id: str
+    directive_status: str | None
+    recipe_ref: str
+    candidate_status: str | None
+    has_diagnostic: bool
+    diagnostic_grade: str | None  # "stub" | "mock" | "real" | None(没有报告)
+    planner_decision_status: str | None
+    has_nomination_note: bool
+    has_track_note: bool
+
+
+@dataclass(frozen=True)
+class ModelView:
+    model_id: str
+    version: int
+    run_id: str
+    status: str
+    # 轮次产物(agent_round_file(model_id))的信封状态;None 表示没有可用的轮次产物
+    agent_round_status: str | None
+
+    @property
+    def has_agent_round(self) -> bool:
+        """派生量,恒等于 agent_round_status is not None;保留给只关心"有没有轮次"的读者。"""
+        return self.agent_round_status is not None
+
+
+@dataclass(frozen=True)
+class GateView:
+    id: str
+    kind: str  # "recipe_candidate" | "skill_memory_proposal"
+    status: str
+
+
 @dataclass
 class TreeState:
     directives: list[DirectiveView] = field(default_factory=list)
     candidates: list[CandidateView] = field(default_factory=list)
     model_count: int = 0
+    # 闭环集成设计 §3.1:以下字段全部带默认值,既有构造不受影响
+    runs: list[RunView] = field(default_factory=list)
+    models: list[ModelView] = field(default_factory=list)
+    gates: list[GateView] = field(default_factory=list)
+    enabled: frozenset[str] = frozenset()
+    accept_mock_evidence: bool = False
 
 
 @dataclass(frozen=True)
@@ -35,6 +81,59 @@
     detail: dict
 
 
+# 闭环集成设计 §3.2 的六个新动作。只有动作名在 state.enabled 里才会产生(R2)。
+CLOSED_LOOP_ACTIONS: frozenset[str] = frozenset({
+    "plan_next_round", "nominate_recipe", "record_track",
+    "start_agent_round", "admit_recipe", "admit_proposal",
+})
+
+# 在途轮(R4):这些状态说明某一轮还没走完
+_IN_FLIGHT_DIRECTIVE = frozenset({"pending", "awaiting_confirmation"})
+_IN_FLIGHT_CANDIDATE = frozenset({"pending_review", "approved"})
+_TERMINAL_CANDIDATE = frozenset({"effective", "rejected"})
+
+def has_round_in_flight(state: TreeState) -> bool:
+    """任何 run 的指令是 pending/awaiting_confirmation,或候选是 pending_review/approved。"""
+    return any(
+        r.directive_status in _IN_FLIGHT_DIRECTIVE or r.candidate_status in _IN_FLIGHT_CANDIDATE
+        for r in state.runs
+    )
+
+
+# R5 / 设计 §3.2:"最大 run"按 run-<数字> 的数值序比较,不按字符串序(run-9 与 run-011、
+# 超过 999 轮都会让字符串序选错)。不匹配的名字(scratch 之类)不参与比较。
+# 与计划 5 分配 run id 的规则同一口径;controlplane 不导入 trainscheduler,所以这里自己写。
+_RUN_ID = re.compile(r"^run-([0-9]+)$")   # [0-9] 而不是 \d:与计划 5 的 run_number 同一口径,避免 Unicode 数字被一边认、一边不认
+
+
+def _latest_numbered_run(runs: list[RunView]) -> RunView | None:
+    """run-<数字> 里数值最大的那个 run;一个都没有时为 None(刻意失败关闭:不规划)。
+    同编号(只可能是手工命名,例如 run-7 与 run-007)时按 run id 字典序取最大,结果与输入顺序无关。"""
+    numbered = [(int(m.group(1)), r) for r in runs if (m := _RUN_ID.match(r.run_id))]
+    return max(numbered, key=lambda pair: (pair[0], pair[1].run_id))[1] if numbered else None
+
+
+def _acceptable(state: TreeState, run: RunView) -> bool:
+    return run.diagnostic_grade is not None and evidence_acceptable(
+        run.diagnostic_grade, state.accept_mock_evidence
+    )
+
+
+def _closed_loop_actions(state: TreeState) -> list[Action]:
+    """闭环动作。本任务只有 plan_next_round;其余五个由任务 91 加入。"""
+    on = state.enabled
+    out: list[Action] = []
+    if "plan_next_round" in on and not has_round_in_flight(state):
+        # R5:只看 run-<数字> 里数值最大的那个 run(在途轮仍看全部 run)。较早 run 的候选即使
+        # 到了终态、证据可接受,也不会被(重复)规划;最大 run 的指令 failed、没有候选时,
+        # 闭环停住等人写新指令;一个 run-<数字> 都没有时也不规划。
+        latest = _latest_numbered_run(state.runs)
+        if (latest is not None and latest.candidate_status in _TERMINAL_CANDIDATE
+                and _acceptable(state, latest) and latest.planner_decision_status in (None, "pending")):
+            out.append(Action(kind="plan_next_round", target_id=latest.run_id, detail={"run_id": latest.run_id}))
+    return out
+
+
 def reconcile(state: TreeState) -> list[Action]:
     """纯函数：给定 git 树当前状态，返回下一步动作列表。不做任何 IO、不修改入参。
 
@@ -74,4 +173,6 @@
                     detail={"run_id": c.run_id, "model_version": next_model_version},
                 )
             )
+    if state.enabled:
+        actions.extend(_closed_loop_actions(state))
     return actions
PATCH
```

- [ ] **Step 5: 运行,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_reconcile_closed_loop.py tests/test_reconcile.py`
Expected: `70 passed`(新 56 个,既有 14 个)。

- [ ] **Step 6: 全量回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: `B+56 passed`(820 起点实测 `876 passed`),无警告。再跑 `git status --short`:除原本就有的 `cli/package*.json` 与未跟踪的文档外,只应有 `src/controlplane/reconcile.py` 和 `tests/test_reconcile_closed_loop.py`(补丁走标准输入,仓库根目录不会多出补丁文件)。

- [ ] **Step 7: 提交**

```bash
git add src/controlplane/reconcile.py
git add tests/test_reconcile_closed_loop.py
git commit -m "feat: closed-loop views in reconcile and the plan_next_round trigger (one round in flight, latest run only)"
```

---

### Task 91: 其余五个闭环动作

**Files:**
- Modify: `src/controlplane/reconcile.py`(178 → 218 行,+41 −1)
- Test: `tests/test_reconcile_closed_loop.py`(追加,本任务 32 个用例,文件共 88 个)

**Interfaces:**
- Consumes: 任务 90 的视图与 `has_round_in_flight`、`_acceptable`。
- Produces: `admit_recipe`、`admit_proposal`、`record_track`、`nominate_recipe`、`start_agent_round` 五个动作,形状见「对外契约」的动作表。

**设计要点:**
- **输出顺序**:`admit_recipe → admit_proposal → record_track → nominate_recipe → plan_next_round → start_agent_round`(Ruling 3)。`gates` 按 id 排序、`runs` 按 run id 排序,都是对副本排序,不改入参(纯函数测试检查)。
- **`record_track` 与 `nominate_recipe` 不受在途轮限制**,对每个满足条件的 run 都产生(Ruling 4)。它们按 run id 字典序输出(`runs = sorted(...)` 只管输出顺序);`plan_next_round` 选"最大 run"仍用任务 90 的 `_latest_numbered_run`(数值序),不用这份排序。
- **`start_agent_round` 按数字比较版本号**,用 `ModelView.version`(读取侧从文件名解析为整数),不按字符串比 id。
- **`start_agent_round` 的触发条件是 `agent_round_status in (None, "pending")`**(R19,Ruling 20):没有轮次产物、或轮次仍是 `pending`(瞬态失败、进程被杀)都会再次触发;只有 `executed` / `failed` 挡住。测试分别钉住 `pending` 触发、`executed` 与 `failed` 不触发。
- **`admit_*` 的 `detail` 为空**(Ruling 2)。
- **组合测试**确认既有动作排在前面、且打开 `enabled` 不改变既有动作的内容与顺序。

- [ ] **Step 1: 写失败测试**

在 `tests/test_reconcile_closed_loop.py` 末尾追加(文件开头的导入与 `_run`、`_kinds`、`_plan`、`ALL` 已由任务 90 定义):

```python
# ---- nominate_recipe ----

def test_nominate_triggers_on_an_effective_candidate_with_acceptable_evidence():
    state = TreeState(runs=[_run()], enabled=frozenset({"nominate_recipe"}))
    assert reconcile(state) == [
        Action(kind="nominate_recipe", target_id="run-001", detail={"run_id": "run-001"})
    ]


@pytest.mark.parametrize("candidate_status", ["rejected", "pending_review", "approved", None])
def test_nominate_needs_an_effective_candidate(candidate_status):
    state = TreeState(runs=[_run(candidate_status=candidate_status)], enabled=ALL)
    assert _kinds(reconcile(state), "nominate_recipe") == []


def test_nominate_is_idempotent_on_the_nomination_note():
    state = TreeState(runs=[_run(has_nomination_note=True)], enabled=ALL)
    assert _kinds(reconcile(state), "nominate_recipe") == []


def test_nominate_respects_the_evidence_grade():
    for grade, accept, expected in [("stub", True, 0), ("mock", False, 0), ("mock", True, 1), ("real", False, 1)]:
        state = TreeState(runs=[_run(diagnostic_grade=grade)], enabled=ALL, accept_mock_evidence=accept)
        assert len(_kinds(reconcile(state), "nominate_recipe")) == expected, (grade, accept)


def test_nominate_is_not_blocked_by_a_round_in_flight_and_covers_every_run():
    state = TreeState(
        runs=[_run("run-002"), _run("run-001"),
              _run("run-003", directive_status="pending", candidate_status=None,
                   has_diagnostic=False, diagnostic_grade=None)],
        enabled=ALL,
    )
    assert [a.target_id for a in _kinds(reconcile(state), "nominate_recipe")] == ["run-001", "run-002"]


# ---- record_track ----

@pytest.mark.parametrize("candidate_status", ["effective", "rejected"])
def test_record_track_triggers_for_a_recipe_backed_run(candidate_status):
    state = TreeState(runs=[_run(recipe_ref="recipe-run-000", candidate_status=candidate_status)],
                      enabled=frozenset({"record_track"}))
    assert reconcile(state) == [
        Action(kind="record_track", target_id="run-001",
               detail={"run_id": "run-001", "recipe_ref": "recipe-run-000"})
    ]


def test_record_track_needs_a_recipe_ref():
    state = TreeState(runs=[_run(recipe_ref="")], enabled=ALL)
    assert _kinds(reconcile(state), "record_track") == []


@pytest.mark.parametrize("candidate_status", ["pending_review", "approved", None])
def test_record_track_needs_a_terminal_candidate(candidate_status):
    state = TreeState(runs=[_run(recipe_ref="recipe-run-000", candidate_status=candidate_status)], enabled=ALL)
    assert _kinds(reconcile(state), "record_track") == []


def test_record_track_is_idempotent_on_the_track_note():
    state = TreeState(runs=[_run(recipe_ref="recipe-run-000", has_track_note=True)], enabled=ALL)
    assert _kinds(reconcile(state), "record_track") == []


def test_record_track_respects_the_evidence_grade():
    run = _run(recipe_ref="recipe-run-000", diagnostic_grade="mock")
    assert _kinds(reconcile(TreeState(runs=[run], enabled=ALL)), "record_track") == []
    assert len(_kinds(reconcile(TreeState(runs=[run], enabled=ALL, accept_mock_evidence=True)), "record_track")) == 1


# ---- start_agent_round ----

def _model(n, status="effective", round_status=None):
    return ModelView(f"zengen-v{n}", n, f"run-{n:03d}", status, round_status)


def test_agent_round_starts_for_the_highest_effective_model_only():
    state = TreeState(models=[_model(2), _model(10), _model(9)], enabled=frozenset({"start_agent_round"}))
    assert reconcile(state) == [
        Action(kind="start_agent_round", target_id="zengen-v10",
               detail={"version": 10, "run_id": "run-010"})
    ]


def test_agent_round_skips_non_effective_models():
    state = TreeState(models=[_model(1), _model(2, status="deprecated")], enabled=ALL)
    assert [a.target_id for a in _kinds(reconcile(state), "start_agent_round")] == ["zengen-v1"]


@pytest.mark.parametrize("round_status", ["executed", "failed"])
def test_agent_round_is_idempotent_on_a_terminal_round_file(round_status):
    state = TreeState(models=[_model(1), _model(2, round_status=round_status)], enabled=ALL)
    assert _kinds(reconcile(state), "start_agent_round") == []  # 旧模型没有轮次也不补跑


def test_a_pending_round_file_retriggers_the_agent_round():
    # R19:瞬态失败或进程被杀留下的 pending 轮次,下一 tick 必须再次派发
    state = TreeState(models=[_model(1), _model(2, round_status="pending")], enabled=ALL)
    assert _kinds(reconcile(state), "start_agent_round") == [
        Action(kind="start_agent_round", target_id="zengen-v2", detail={"version": 2, "run_id": "run-002"})
    ]


def test_no_effective_model_no_agent_round():
    state = TreeState(models=[_model(1, status="deprecated")], enabled=ALL)
    assert _kinds(reconcile(state), "start_agent_round") == []


# ---- admit_recipe / admit_proposal ----

def test_admit_recipe_on_an_approved_recipe_candidate():
    state = TreeState(gates=[GateView("recipe-run-003", "recipe_candidate", "approved")],
                      enabled=frozenset({"admit_recipe"}))
    assert reconcile(state) == [
        Action(kind="admit_recipe", target_id="recipe-run-003", detail={})
    ]


def test_admit_proposal_on_an_approved_proposal():
    state = TreeState(gates=[GateView("proposal-agent-round-zengen-v1-1", "skill_memory_proposal", "approved")],
                      enabled=frozenset({"admit_proposal"}))
    assert reconcile(state) == [
        Action(kind="admit_proposal", target_id="proposal-agent-round-zengen-v1-1", detail={})
    ]


@pytest.mark.parametrize("status", ["pending_review", "rejected", "effective", "deprecated", "draft"])
def test_admission_needs_the_approved_status(status):
    state = TreeState(gates=[GateView("recipe-run-003", "recipe_candidate", status),
                             GateView("proposal-x-1", "skill_memory_proposal", status)], enabled=ALL)
    assert reconcile(state) == []


def test_admission_kinds_do_not_cross():
    state = TreeState(gates=[GateView("recipe-run-003", "recipe_candidate", "approved"),
                             GateView("proposal-x-1", "skill_memory_proposal", "approved")],
                      enabled=frozenset({"admit_recipe"}))
    assert [a.target_id for a in reconcile(state)] == ["recipe-run-003"]


# ---- 组合:顺序与既有动作 ----

def test_existing_actions_come_first_then_closed_loop_actions_in_fixed_order():
    state = TreeState(
        directives=[DirectiveView("run-009", "pending", "tom", "")],
        candidates=[CandidateView("cand-run-008", "approved", "run-008", has_diagnostic=True)],
        model_count=1,
        runs=[_run("run-001", recipe_ref="recipe-run-000"),
              _run("run-009", directive_status="pending", candidate_status=None,
                   has_diagnostic=False, diagnostic_grade=None),
              _run("run-008", candidate_status="approved")],
        models=[_model(1)],
        gates=[GateView("proposal-x-1", "skill_memory_proposal", "approved"),
               GateView("recipe-run-000", "recipe_candidate", "approved")],
        enabled=ALL,
    )
    assert [(a.kind, a.target_id) for a in reconcile(state)] == [
        ("dispatch_directive", "run-009"),
        ("promote_candidate", "cand-run-008"),
        ("admit_recipe", "recipe-run-000"),
        ("admit_proposal", "proposal-x-1"),
        ("record_track", "run-001"),
        ("nominate_recipe", "run-001"),
        # plan_next_round 被在途轮挡住(run-009 pending、run-008 approved)
        ("start_agent_round", "zengen-v1"),
    ]


def test_enabling_names_does_not_change_the_existing_actions():
    base = dict(
        directives=[DirectiveView("run-001", "pending", "tom", "")],
        candidates=[CandidateView("cand-run-002", "pending_review", "run-002")],
        model_count=0,
    )
    legacy = reconcile(TreeState(**base))
    extended = reconcile(TreeState(**base, enabled=ALL))
    assert extended[: len(legacy)] == legacy
```

- [ ] **Step 2: 运行,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_reconcile_closed_loop.py`
Expected: `13 failed, 75 passed`。失败的是各动作"会触发"的用例(含 `pending` 轮次再次触发)与组合顺序用例;"不触发"的用例在只有 `plan_next_round` 的实现上本来就成立。

- [ ] **Step 3: 写实现**

在仓库根目录运行(差异经标准输入交给 `git apply`,同任务 90);差异应用到 `src/controlplane/reconcile.py`,共 +41 −1 行:

```bash
git apply <<'PATCH'
--- a/src/controlplane/reconcile.py
+++ b/src/controlplane/reconcile.py
@@ -120,9 +120,37 @@
 
 
 def _closed_loop_actions(state: TreeState) -> list[Action]:
-    """闭环动作。本任务只有 plan_next_round;其余五个由任务 91 加入。"""
+    """六个新动作。输出顺序固定:admit_recipe → admit_proposal → record_track →
+    nominate_recipe → plan_next_round → start_agent_round;同一种动作内按 id 字典序
+    (plan_next_round 至多一条,只针对编号最大的 run-<数字>,见 _latest_numbered_run)。
+    detail 只是提示,处理器必须自己重读树(动作列表来自 tick 开头的快照)。"""
     on = state.enabled
     out: list[Action] = []
+    gates = sorted(state.gates, key=lambda g: g.id)
+    # 只决定 record_track / nominate_recipe 的输出顺序(Ruling 3:按 id 字典序);
+    # 不用来选"最大 run",那个按数值序,见 _latest_numbered_run。
+    runs = sorted(state.runs, key=lambda r: r.run_id)
+    if "admit_recipe" in on:
+        for g in gates:
+            if g.kind == "recipe_candidate" and g.status == "approved":
+                out.append(Action(kind="admit_recipe", target_id=g.id, detail={}))
+    if "admit_proposal" in on:
+        for g in gates:
+            if g.kind == "skill_memory_proposal" and g.status == "approved":
+                out.append(Action(kind="admit_proposal", target_id=g.id, detail={}))
+    if "record_track" in on:
+        for r in runs:
+            if (r.recipe_ref and r.candidate_status in _TERMINAL_CANDIDATE
+                    and _acceptable(state, r) and not r.has_track_note):
+                out.append(Action(
+                    kind="record_track", target_id=r.run_id,
+                    detail={"run_id": r.run_id, "recipe_ref": r.recipe_ref},
+                ))
+    if "nominate_recipe" in on:
+        for r in runs:
+            if (r.candidate_status == "effective" and _acceptable(state, r)
+                    and not r.has_nomination_note):
+                out.append(Action(kind="nominate_recipe", target_id=r.run_id, detail={"run_id": r.run_id}))
     if "plan_next_round" in on and not has_round_in_flight(state):
         # R5:只看 run-<数字> 里数值最大的那个 run(在途轮仍看全部 run)。较早 run 的候选即使
         # 到了终态、证据可接受,也不会被(重复)规划;最大 run 的指令 failed、没有候选时,
@@ -131,6 +159,18 @@
         if (latest is not None and latest.candidate_status in _TERMINAL_CANDIDATE
                 and _acceptable(state, latest) and latest.planner_decision_status in (None, "pending")):
             out.append(Action(kind="plan_next_round", target_id=latest.run_id, detail={"run_id": latest.run_id}))
+    if "start_agent_round" in on:
+        effective = [m for m in state.models if m.status == "effective"]
+        if effective:
+            top = max(effective, key=lambda m: m.version)
+            # R19:没有轮次产物,或轮次产物仍是 pending(上一次瞬态失败或进程被杀留下的),
+            # 都要(再次)触发;只有终态 executed / failed 才算这一轮已结束。与 plan_next_round
+            # 的"决策为空或 pending"同一模式,重入的幂等由轮次处理器负责。
+            if top.agent_round_status in (None, "pending"):
+                out.append(Action(
+                    kind="start_agent_round", target_id=top.model_id,
+                    detail={"version": top.version, "run_id": top.run_id},
+                ))
     return out
 
 
PATCH
```

- [ ] **Step 4: 运行,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_reconcile_closed_loop.py tests/test_reconcile.py`
Expected: `102 passed`。

- [ ] **Step 5: 全量回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: `B+88 passed`(820 起点实测 `908 passed`),无警告。

- [ ] **Step 6: 提交**

```bash
git add src/controlplane/reconcile.py
git add tests/test_reconcile_closed_loop.py
git commit -m "feat: the remaining closed-loop actions in reconcile (admit, track, nominate, agent round) in a fixed order"
```

---

### Task 92: `read_tree_state` 读闭环视图

**Files:**
- Modify: `src/controlplane/daemon.py`(178 → 309 行,+135 −4)
- Test: `tests/test_daemon_closed_loop_views.py`(新建,20 个用例)

**Interfaces:**
- Consumes: 任务 90–91 的视图;计划 1 的六个路径函数与 `controlplane.evidence.diagnostic_grade`;现有 `_iter_run_ids`。
- Produces: `read_tree_state(store, *, enabled=frozenset(), accept_mock_evidence=False) -> TreeState`。

**设计要点:**
- **`enabled` 为空时提前返回**,构造方式与改动前一模一样(只传三个旧字段),不多读一个文件。测试用等式比较证明返回值与旧形状相同。
- **诊断等级一律调 `controlplane.evidence.diagnostic_grade`。** 没有报告时是 `None`,不是 `"stub"`:两者在 `reconcile` 里都不可接受,但语义不同,`review` 也区分 MISSING 与 STUB。**判不出来按最弱算**(Ruling 23):`_report_grade` 在载荷不是 dict、或报告文件读不出时返回 `"stub"`,不让 `diagnostic_grade` 的 `TypeError` 或读取异常在读视图时把守护进程带走(有测试)。
- **`ModelView.agent_round_status` 读轮次产物的信封状态**(`_agent_round_status`,R19,Ruling 20):文件不存在为 `None`;存在就 `read_product(...).status`;文件在但读不出(YAML 解析失败,含合并冲突标记;`KeyError`/`TypeError`/`AttributeError`;`EnvelopeValidationError`;`UnknownSchemaVersionError`)也是 `None`,让 `start_agent_round` 再次触发,由轮次处理器重入时判定损坏并自己写 `failed`。`OSError` 不捕获,原样上抛(瞬态)。`controlplane` 里没有现成的"读状态、读不出给 `None`"工具(`_status_or_none` 遇到坏文件会抛),所以新写一个,只用于轮次产物。
- **`ModelView.run_id` 取 `links[1]`**(已核实 `promote_candidate` 写的是 `[候选 id, run id]`);不足两项为 `""`。版本号从文件名 `zengen-v(\d+).yaml` 解析成整数,不认的文件名跳过(与 `controlplane.models` 同一规则)。
- **提案在子目录里**,`list_products` 不递归,所以自己 `iterdir()` 子目录,再用 `store.exists` 判断 `proposal.yaml` 在不在;不是 `proposal.yaml` 的文件和空子目录都不算。配方候选是顶层文件,`list_products` 够用。
- **新目录不存在时视图为空**:计划 1 把四个新目录加进了 `SUBDIRS`,但旧仓库可能没有;两条读取路径都先判目录存在。
- 本任务的 `apply_action`、`reconcile_once`、`run_daemon` 不变;任务 93 再加注入缝。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_daemon_closed_loop_views.py`:

```python
"""read_tree_state 的闭环视图(闭环集成设计 §3.1、§3.3):enabled 为空时与现在逐字节相同,
非空时才读 runs / models / gates。"""
import pytest

from controlplane.daemon import read_tree_state, reconcile_once
from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import (
    agent_round_file,
    candidate_checkpoint_file,
    diagnostic_report_file,
    model_file,
    nomination_file,
    planner_decision_file,
    proposal_file,
    recipe_candidate_file,
    recipe_track_note_file,
    run_dir,
)
from controlplane.reconcile import CLOSED_LOOP_ACTIONS, GateView, ModelView, RunView, TreeState

ALL = frozenset(CLOSED_LOOP_ACTIONS)


def _env(id_, type_, status, payload=None, links=None):
    return Envelope(
        id=id_, type=type_, version=1, created_at="2026-10-09T00:00:00Z", created_by=OwnerRef(),
        status=status, schema_version=1, links=links or [], payload=payload or {},
    )


def _put(store, rel, env):
    store.write_product(rel, env, f"seed {rel}")


def _directive(store, run_id, status="pending", recipe_ref=""):
    _put(store, f"{run_dir(run_id)}/directive.yaml",
         _env(run_id, "directive", status, {"target_dimension": "tom", "recipe_ref": recipe_ref}))


def _candidate(store, run_id, status):
    _put(store, candidate_checkpoint_file(run_id), _env(f"cand-{run_id}", "candidate_checkpoint", status, links=[run_id]))


def _report(store, run_id, payload):
    _put(store, diagnostic_report_file(run_id), _env(f"diag-{run_id}", "diagnostic_report", "executed", payload, [run_id]))


REAL = {"dimension_scores": [{"dimension": "tom", "level": "l1", "score": 0.5}], "regression_flags": {"flags": []}}
MOCK = {**REAL, "backends": "mock"}
STUB = {"candidate_ref": "cand", "benchmark_version": "bench-v1", "dimension_count": 0, "stub": True}


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    return s


def _seed_everything(store):
    _directive(store, "run-001", "executed", "recipe-run-000")
    _candidate(store, "run-001", "effective")
    _report(store, "run-001", REAL)
    _put(store, planner_decision_file("run-001"), _env("pd-run-001", "planner_decision", "pending", {"next_run_id": "run-002"}))
    _put(store, nomination_file("run-001"), _env("nomination-run-001", "nomination_note", "executed"))
    _put(store, recipe_track_note_file("run-001"), _env("track-run-001", "recipe_track_note", "executed"))
    _directive(store, "run-002", "awaiting_confirmation")
    _put(store, model_file(1), _env("zengen-v1", "zengen_model", "effective", {"promoted_from": "cand-run-001"},
                                    ["cand-run-001", "run-001"]))
    _put(store, agent_round_file("zengen-v1"), _env("agent-round-zengen-v1", "agent_round", "executed"))
    _put(store, recipe_candidate_file("run-001"), _env("recipe-run-001", "recipe_candidate", "approved"))
    _put(store, proposal_file("proposal-agent-round-zengen-v1-1"),
         _env("proposal-agent-round-zengen-v1-1", "skill_memory_proposal", "pending_review"))


def test_with_nothing_enabled_the_state_equals_the_legacy_shape(store):
    _seed_everything(store)
    state = read_tree_state(store)
    assert state == TreeState(directives=state.directives, candidates=state.candidates, model_count=1)
    assert (state.runs, state.models, state.gates) == ([], [], [])
    assert read_tree_state(store, enabled=frozenset(), accept_mock_evidence=True) == state


def test_with_nothing_enabled_the_legacy_fields_match_what_the_full_read_sees(store):
    _seed_everything(store)
    legacy, full = read_tree_state(store), read_tree_state(store, enabled=ALL)
    assert (legacy.directives, legacy.candidates, legacy.model_count) == (
        full.directives, full.candidates, full.model_count)


def test_run_views_project_every_marker(store):
    _seed_everything(store)
    state = read_tree_state(store, enabled=ALL, accept_mock_evidence=True)
    assert state.enabled == ALL and state.accept_mock_evidence is True
    assert state.runs == [
        RunView(run_id="run-001", directive_status="executed", recipe_ref="recipe-run-000",
                candidate_status="effective", has_diagnostic=True, diagnostic_grade="real",
                planner_decision_status="pending", has_nomination_note=True, has_track_note=True),
        RunView(run_id="run-002", directive_status="awaiting_confirmation", recipe_ref="",
                candidate_status=None, has_diagnostic=False, diagnostic_grade=None,
                planner_decision_status=None, has_nomination_note=False, has_track_note=False),
    ]


@pytest.mark.parametrize("payload,grade", [(REAL, "real"), (MOCK, "mock"), (STUB, "stub")])
def test_the_grade_comes_from_controlplane_evidence(store, payload, grade):
    _candidate(store, "run-001", "effective")
    _report(store, "run-001", payload)
    assert read_tree_state(store, enabled=ALL).runs[0].diagnostic_grade == grade


def test_a_non_dict_report_payload_grades_stub(store, monkeypatch):
    # 判不出来按最弱算:载荷不是 dict 时不抛 TypeError,记为 stub
    _candidate(store, "run-001", "effective")
    _report(store, "run-001", REAL)
    real_read = store.read_product

    def read(rel):
        env = real_read(rel)
        if rel == diagnostic_report_file("run-001"):
            env.payload = ["not", "a", "mapping"]
        return env

    monkeypatch.setattr(store, "read_product", read)
    assert read_tree_state(store, enabled=ALL).runs[0].diagnostic_grade == "stub"


def test_an_unreadable_report_file_grades_stub(store):
    _candidate(store, "run-001", "effective")
    path = store.repo_root / diagnostic_report_file("run-001")
    path.write_text("id: diag-run-001\npayload: [1, 2]\n", encoding="utf-8")  # 信封校验失败
    view = read_tree_state(store, enabled=ALL).runs[0]
    assert (view.has_diagnostic, view.diagnostic_grade) == (True, "stub")


def test_a_daemon_stub_report_is_graded_stub(store):
    _directive(store, "run-001")
    reconcile_once(store)  # 派发:桩候选
    reconcile_once(store)  # 桩诊断
    assert read_tree_state(store, enabled=ALL).runs[0].diagnostic_grade == "stub"


def test_a_run_dir_without_a_directive_still_has_a_view(store):
    _candidate(store, "run-001", "rejected")
    view = read_tree_state(store, enabled=ALL).runs[0]
    assert (view.directive_status, view.recipe_ref, view.candidate_status) == (None, "", "rejected")


def test_a_non_string_recipe_ref_reads_as_no_recipe(store):
    _put(store, f"{run_dir('run-001')}/directive.yaml",
         _env("run-001", "directive", "executed", {"target_dimension": "tom", "recipe_ref": None}))
    assert read_tree_state(store, enabled=ALL).runs[0].recipe_ref == ""


def test_model_views_take_the_run_id_from_the_promotion_links(store):
    _seed_everything(store)
    _put(store, model_file(10), _env("zengen-v10", "zengen_model", "deprecated", links=["cand-run-010", "run-010"]))
    _put(store, model_file(2), _env("zengen-v2", "zengen_model", "effective", links=[]))
    state = read_tree_state(store, enabled=ALL)
    assert sorted(state.models, key=lambda m: m.version) == [
        ModelView("zengen-v1", 1, "run-001", "effective", "executed"),
        ModelView("zengen-v2", 2, "", "effective", None),
        ModelView("zengen-v10", 10, "run-010", "deprecated", None),
    ]


@pytest.mark.parametrize("round_status", ["pending", "executed", "failed"])
def test_the_model_view_carries_the_round_envelope_status(store, round_status):
    _put(store, model_file(1), _env("zengen-v1", "zengen_model", "effective", links=["cand-run-001", "run-001"]))
    _put(store, agent_round_file("zengen-v1"), _env("agent-round-zengen-v1", "agent_round", round_status))
    (view,) = read_tree_state(store, enabled=ALL).models
    assert (view.agent_round_status, view.has_agent_round) == (round_status, True)


@pytest.mark.parametrize("text", [
    "<<<<<<< HEAD\nstatus: pending\n=======\nstatus: executed\n>>>>>>> other\n",  # 合并冲突标记
    "id: agent-round-zengen-v1\nstatus: pending\n",                                 # 信封缺必填字段
])
def test_an_unreadable_round_file_reads_as_no_round(store, text):
    _put(store, model_file(1), _env("zengen-v1", "zengen_model", "effective", links=["cand-run-001", "run-001"]))
    path = store.repo_root / agent_round_file("zengen-v1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    (view,) = read_tree_state(store, enabled=ALL).models
    assert (view.agent_round_status, view.has_agent_round) == (None, False)


def test_the_daemon_promotion_writes_links_the_model_view_understands(store):
    _directive(store, "run-001")
    reconcile_once(store)
    reconcile_once(store)
    cand = store.read_product(candidate_checkpoint_file("run-001"))
    cand.status = "approved"
    store.write_product(candidate_checkpoint_file("run-001"), cand, "human: approve")
    reconcile_once(store)
    assert read_tree_state(store, enabled=ALL).models == [ModelView("zengen-v1", 1, "run-001", "effective", None)]


def test_gate_views_cover_recipe_candidates_and_nested_proposals(store):
    _seed_everything(store)
    _put(store, proposal_file("proposal-agent-round-zengen-v1-2"),
         _env("proposal-agent-round-zengen-v1-2", "skill_memory_proposal", "approved"))
    # 不是 proposal.yaml 的文件、空子目录都不算
    _put(store, "control-plane/skill_memory_proposals/proposal-x/notes.yaml", _env("n", "note", "executed"))
    (store.repo_root / "control-plane/skill_memory_proposals/empty").mkdir()
    assert read_tree_state(store, enabled=ALL).gates == [
        GateView("recipe-run-001", "recipe_candidate", "approved"),
        GateView("proposal-agent-round-zengen-v1-1", "skill_memory_proposal", "pending_review"),
        GateView("proposal-agent-round-zengen-v1-2", "skill_memory_proposal", "approved"),
    ]


def test_missing_new_directories_read_as_empty(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    import shutil
    for sub in ("recipe_candidates", "skill_memory_proposals", "agent_rounds"):
        shutil.rmtree(store.repo_root / "control-plane" / sub, ignore_errors=True)
    state = read_tree_state(store, enabled=ALL)
    assert (state.runs, state.models, state.gates) == ([], [], [])
```

- [ ] **Step 2: 运行,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_daemon_closed_loop_views.py`
Expected: `20 failed`(都是 `TypeError: read_tree_state() got an unexpected keyword argument 'enabled'`)。

- [ ] **Step 3: 写实现**

在仓库根目录运行(差异经标准输入交给 `git apply`,同任务 90);差异应用到 `src/controlplane/daemon.py`,共 +135 −4 行:

```bash
git apply <<'PATCH'
--- a/src/controlplane/daemon.py
+++ b/src/controlplane/daemon.py
@@ -1,19 +1,39 @@
 from __future__ import annotations
 
+import re
 import time
 from collections.abc import Callable
 
+import yaml
+
 from controlplane.envelope import Envelope, OwnerRef
+from controlplane.evidence import diagnostic_grade
 from controlplane.gitstore import GitStore
-from controlplane.paths import CONTROL_PLANE_ROOT, run_dir, model_file, diagnostic_report_file
+from controlplane.migration import UnknownSchemaVersionError
+from controlplane.paths import (
+    CONTROL_PLANE_ROOT,
+    agent_round_file,
+    candidate_checkpoint_file,
+    diagnostic_report_file,
+    model_file,
+    nomination_file,
+    planner_decision_file,
+    proposal_file,
+    recipe_track_note_file,
+    run_dir,
+)
 from controlplane.reconcile import (
     DirectiveView,
     CandidateView,
+    GateView,
+    ModelView,
+    RunView,
     TreeState,
     Action,
     reconcile,
 )
 from controlplane.states import assert_transition
+from controlplane.validate import EnvelopeValidationError
 from simulator.stubs import diagnostic_evaluator_stub, execute_dispatch
 
 _NOW = "2026-09-26T00:00:00Z"
@@ -22,6 +42,8 @@
 # 把 directive 置 executed。状态迁移归调和循环,派发器不碰 directive 的状态。
 Dispatcher = Callable[[GitStore, Envelope], None]
 
+_MODEL_FILE = re.compile(r"zengen-v(\d+)\.yaml$")
+
 
 class DirectiveFailedError(Exception):
     """派发器用它说"这条指令确定性地做不成,重试无益"。调和循环把 directive 置 failed
@@ -46,8 +68,16 @@
     return sorted(p.name for p in runs_dir.iterdir() if p.is_dir())
 
 
-def read_tree_state(store: GitStore) -> TreeState:
-    """扫 git 树，投影成 reconcile() 需要的纯视图。"""
+def read_tree_state(
+    store: GitStore,
+    *,
+    enabled: frozenset[str] = frozenset(),
+    accept_mock_evidence: bool = False,
+) -> TreeState:
+    """扫 git 树，投影成 reconcile() 需要的纯视图。
+
+    enabled 为空时只读既有的三项,返回值与引入闭环视图之前逐字节相同(accept_mock_evidence
+    此时不起作用:没有闭环动作,它无处可用)。非空时才去填 runs / models / gates。"""
     directives: list[DirectiveView] = []
     candidates: list[CandidateView] = []
     for run_id in _iter_run_ids(store):
@@ -72,7 +102,108 @@
                 )
             )
     model_count = len(store.list_products(f"{CONTROL_PLANE_ROOT}/models"))
-    return TreeState(directives=directives, candidates=candidates, model_count=model_count)
+    if not enabled:
+        return TreeState(directives=directives, candidates=candidates, model_count=model_count)
+    return TreeState(
+        directives=directives, candidates=candidates, model_count=model_count,
+        runs=_read_run_views(store), models=_read_model_views(store), gates=_read_gate_views(store),
+        enabled=frozenset(enabled), accept_mock_evidence=accept_mock_evidence,
+    )
+
+
+def _status_or_none(store: GitStore, rel: str) -> str | None:
+    return store.read_product(rel).status if store.exists(rel) else None
+
+
+def _read_run_views(store: GitStore) -> list[RunView]:
+    # directive.yaml 的路径 ↔ cli/src/commands.ts 的 DIRECTIVE_PATTERN(confirm 与 status 读它)
+    views: list[RunView] = []
+    for run_id in _iter_run_ids(store):
+        d_path = f"{run_dir(run_id)}/directive.yaml"
+        directive = store.read_product(d_path) if store.exists(d_path) else None
+        recipe_ref = directive.payload.get("recipe_ref") if directive is not None else None
+        report_path = diagnostic_report_file(run_id)
+        has_diagnostic = store.exists(report_path)
+        views.append(RunView(
+            run_id=run_id,
+            directive_status=directive.status if directive is not None else None,
+            # 人写的指令里 recipe_ref 恒为 "";None 或非字符串一律当作"没有配方"
+            recipe_ref=recipe_ref if isinstance(recipe_ref, str) else "",
+            candidate_status=_status_or_none(store, candidate_checkpoint_file(run_id)),
+            has_diagnostic=has_diagnostic,
+            diagnostic_grade=_report_grade(store, report_path) if has_diagnostic else None,
+            planner_decision_status=_status_or_none(store, planner_decision_file(run_id)),
+            has_nomination_note=store.exists(nomination_file(run_id)),
+            has_track_note=store.exists(recipe_track_note_file(run_id)),
+        ))
+    return views
+
+
+def _report_grade(store: GitStore, rel: str) -> str:
+    """诊断报告的证据等级。等级只有一处实现(controlplane.evidence),这里不另写判断;
+    但"判不出来按最弱算":载荷不是 dict、或报告文件读不出(与 _UNREADABLE_ROUND 同一组异常),
+    都记为 stub(永不可接受),不让一份坏报告在读视图时把守护进程带走。"""
+    try:
+        payload = store.read_product(rel).payload
+    except _UNREADABLE_ROUND:
+        return "stub"
+    return diagnostic_grade(payload) if isinstance(payload, dict) else "stub"
+
+
+def _read_model_views(store: GitStore) -> list[ModelView]:
+    views: list[ModelView] = []
+    for rel in store.list_products(f"{CONTROL_PLANE_ROOT}/models"):
+        match = _MODEL_FILE.search(rel)
+        if match is None:
+            continue
+        env = store.read_product(rel)
+        # promote_candidate 写的 links 是 [候选 id, run id]:第二项就是产生该模型的 run。
+        # 计划 3 的 recipestore.gain.baseline_run_of 对同一问题取"cand-<x> 也在 links 里的那一项"
+        # (以该函数为准,不按文档行号引用);两种判法目前一致,
+        # 改晋级写法时两处要一起改,计划 9 的一致性核对会加测试钉住。
+        run_id = env.links[1] if len(env.links) >= 2 else ""
+        views.append(ModelView(
+            model_id=env.id, version=int(match.group(1)), run_id=run_id, status=env.status,
+            agent_round_status=_agent_round_status(store, env.id),
+        ))
+    return views
+
+
+# 轮次产物"文件在、内容读不出"时的异常:YAML 解析失败(含合并冲突标记)、缺键、类型不对、
+# 信封校验失败、schema_version 无法迁移。OSError 不在其中,原样上抛(瞬态,守护进程退出)。
+_UNREADABLE_ROUND = (KeyError, TypeError, AttributeError, yaml.YAMLError,
+                     EnvelopeValidationError, UnknownSchemaVersionError)
+
+
+def _agent_round_status(store: GitStore, model_id: str) -> str | None:
+    """轮次产物的信封状态;没有产物或产物读不出时为 None(R19:按"没有可用轮次"处理,让
+    start_agent_round 再次触发,由轮次处理器重入时判定损坏并自己写 failed)。"""
+    rel = agent_round_file(model_id)
+    if not store.exists(rel):
+        return None
+    try:
+        return store.read_product(rel).status
+    except _UNREADABLE_ROUND:
+        return None
+
+
+def _read_gate_views(store: GitStore) -> list[GateView]:
+    views: list[GateView] = []
+    # 配方候选:recipe_candidates/<id>.yaml,顶层文件,list_products 够用
+    for rel in store.list_products(f"{CONTROL_PLANE_ROOT}/recipe_candidates"):
+        env = store.read_product(rel)
+        views.append(GateView(id=env.id, kind="recipe_candidate", status=env.status))
+    # 技能提案在子目录里(skill_memory_proposals/<id>/proposal.yaml)。list_products 不递归,
+    # 所以像 _iter_run_ids 那样自己遍历目录;路径仍由 proposal_file 拼(文件夹名就是产物 id)。
+    # 两类路径模式 ↔ cli/src/commands.ts 的 GATE_PATTERNS。
+    proposals_dir = store.repo_root / CONTROL_PLANE_ROOT / "skill_memory_proposals"
+    if proposals_dir.is_dir():
+        for sub in sorted(p for p in proposals_dir.iterdir() if p.is_dir()):
+            rel = proposal_file(sub.name)
+            if store.exists(rel):
+                env = store.read_product(rel)
+                views.append(GateView(id=env.id, kind="skill_memory_proposal", status=env.status))
+    return views
 
 
 def apply_action(
PATCH
```

- [ ] **Step 4: 运行,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_daemon_closed_loop_views.py tests/test_daemon.py tests/test_daemon_dispatcher.py tests/test_e2e_loop.py`
Expected: `42 passed`(新 20 个,既有 22 个)。

- [ ] **Step 5: 全量回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: `B+108 passed`(820 起点实测 `928 passed`),无警告。

- [ ] **Step 6: 提交**

```bash
git add src/controlplane/daemon.py
git add tests/test_daemon_closed_loop_views.py
git commit -m "feat: read_tree_state projects runs, models and gates only when closed-loop actions are enabled"
```

---

### Task 93: 处理器与评估器的注入缝

**Files:**
- Modify: `src/controlplane/daemon.py`(309 → 359 行,+57 −7)
- Test: `tests/test_daemon_closed_loop_seams.py`(新建,16 个用例)

**Interfaces:**
- Consumes: 任务 92 的 `read_tree_state`;`CLOSED_LOOP_ACTIONS`。
- Produces: `Handler`、`Evaluator`;`apply_action(..., *, dispatcher=None, handlers=None, evaluator=None)`、`reconcile_once(..., *, dispatcher=None, handlers=None, evaluator=None, accept_mock_evidence=False)`、`run_daemon(..., *, dispatcher=None, handlers=None, evaluator=None, accept_mock_evidence=False)`。

**设计要点:**
- **`enabled` 取 `handlers` 的键**(设计 §3.3)。所以没有处理器的动作种类永远不会被 `reconcile` 产生,也就不会出现在返回列表里,`tests/test_e2e_loop.py` 的"稳定后返回空列表"保持成立。
- **处理器路由放在 `apply_action` 开头**:`action.kind` 在 `handlers` 里就调处理器并返回;否则走既有分支。闭环动作没有处理器时(只可能是直接调 `apply_action`)落到末尾的"无自动动作",什么都不写。
- **`evaluator` 只替换 `request_diagnostic`**:注入时调 `evaluator(store, run_id, candidate_id)` 后返回,不再调桩。`promote_candidate` 的"有诊断才晋级"不变,所以评估器若没写报告,候选会停在门前(下一 tick 重新请求诊断)。
- **处理器异常原样上抛**,不 `try`。测试连续两 tick 都看到同一个异常,证明没有被吞,也没有写任何"已处理"标记。
- **未知键在执行任何动作之前抛 `ValueError`**(Ruling 7),三个入口都检查;`run_daemon` 在第一 tick 之前就检查。
- **"不注入时逐字节相同"的证明**:两棵树,一棵什么都不传,另一棵显式传 `dispatcher=stub_dispatcher, handlers={}, evaluator=None, accept_mock_evidence=False`,从指令一路走到晋级,比较整棵 `control-plane/` 的所有产物与 `git log` 的全部提交说明。另一条用例注入了两个永远不触发的处理器(桩诊断是 `stub` 证据),同样比较两棵树与日志。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_daemon_closed_loop_seams.py`:

```python
"""注入缝(闭环集成设计 §3.3):handlers 与 evaluator。不注入时与现在逐字节相同。"""
import pytest

from controlplane.daemon import apply_action, reconcile_once, run_daemon, stub_dispatcher
from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import candidate_checkpoint_file, diagnostic_report_file, model_file, nomination_file, run_dir
from controlplane.reconcile import Action


def _put_directive(store, run_id):
    env = Envelope(
        id=run_id, type="directive", version=1, created_at="2026-09-26T00:00:00Z",
        created_by=OwnerRef(), status="pending", schema_version=1,
        payload={"target_dimension": "tom", "recipe_ref": ""},
    )
    store.write_product(f"{run_dir(run_id)}/directive.yaml", env, "human: write directive")


def _approve(store, run_id):
    cand = store.read_product(candidate_checkpoint_file(run_id))
    cand.status = "approved"
    store.write_product(candidate_checkpoint_file(run_id), cand, "human: approve")


def _whole_tree(store):
    root = store.repo_root / "control-plane"
    return {
        str(p.relative_to(store.repo_root)): store.read_product(str(p.relative_to(store.repo_root))).to_dict()
        for p in sorted(root.rglob("*.yaml"))
    }


def _git_log(store):
    return store._git("log", "--format=%s").splitlines()


def _mock_report(run_id, candidate_id):
    return Envelope(
        id=f"diag-{run_id}", type="diagnostic_report", version=1, created_at="2026-10-09T00:00:00Z",
        created_by=OwnerRef(), status="executed", schema_version=1, links=[run_id],
        payload={"candidate_ref": candidate_id, "backends": "mock",
                 "dimension_scores": [{"dimension": "tom", "level": "l1", "score": 0.6}],
                 "regression_flags": {"flags": []}},
    )


def _fake_evaluator(calls):
    def evaluate(store, run_id, candidate_id):
        calls.append((run_id, candidate_id))
        store.write_product(diagnostic_report_file(run_id), _mock_report(run_id, candidate_id), "fake: diag")
    return evaluate


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    return s


def _drive_to_promotion(store, **kw):
    """派发 → 诊断 → 待审 → 批准 → 晋级 → 稳定,每一 tick 的动作种类。"""
    _put_directive(store, "run-001")
    kinds = [[a.kind for a in reconcile_once(store, **kw)] for _ in range(3)]
    _approve(store, "run-001")
    kinds += [[a.kind for a in reconcile_once(store, **kw)] for _ in range(2)]
    return kinds


def test_explicit_empty_injection_leaves_tree_log_and_actions_identical(tmp_path):
    a, b = GitStore(str(tmp_path / "a")), GitStore(str(tmp_path / "b"))
    for s in (a, b):
        s.init()
    ka = _drive_to_promotion(a)
    kb = _drive_to_promotion(b, dispatcher=stub_dispatcher, handlers={}, evaluator=None,
                             accept_mock_evidence=False)
    assert ka == kb == [["dispatch_directive"], ["request_diagnostic"], ["await_review"],
                        ["promote_candidate"], []]
    assert _whole_tree(a) == _whole_tree(b)
    assert _git_log(a) == _git_log(b)


def test_handlers_for_actions_that_never_trigger_change_nothing(tmp_path):
    """有处理器但触发条件不满足(桩诊断是 stub 证据):树与提交日志都与不注入时一致。"""
    a, b = GitStore(str(tmp_path / "a")), GitStore(str(tmp_path / "b"))
    for s in (a, b):
        s.init()
    calls = []
    handlers = {k: (lambda s, act, k=k: calls.append(k)) for k in ("plan_next_round", "nominate_recipe")}
    ka = _drive_to_promotion(a)
    kb = _drive_to_promotion(b, handlers=handlers)
    assert ka == kb
    assert calls == []
    assert _whole_tree(a) == _whole_tree(b)
    assert _git_log(a) == _git_log(b)


def test_an_injected_evaluator_replaces_only_the_diagnostic_step(store):
    calls = []
    kinds = _drive_to_promotion(store, evaluator=_fake_evaluator(calls))
    assert kinds == [["dispatch_directive"], ["request_diagnostic"], ["await_review"],
                     ["promote_candidate"], []]
    assert calls == [("run-001", "cand-run-001")]
    assert store.read_product(diagnostic_report_file("run-001")).payload["backends"] == "mock"
    assert "stub" not in store.read_product(diagnostic_report_file("run-001")).payload
    assert store.exists(model_file(1))  # 晋级路径不变


def test_the_evaluator_also_covers_an_approved_candidate_without_evidence(store):
    _put_directive(store, "run-001")
    reconcile_once(store)
    _approve(store, "run-001")
    calls = []
    assert [a.kind for a in reconcile_once(store, evaluator=_fake_evaluator(calls))] == ["request_diagnostic"]
    assert calls == [("run-001", "cand-run-001")]


def test_a_handler_receives_the_store_and_the_action(store):
    seen = []
    handlers = {"nominate_recipe": lambda s, a: seen.append((s, a))}
    _drive_to_promotion(store, evaluator=_fake_evaluator([]))  # 跑到晋级,mock 证据
    actions = reconcile_once(store, handlers=handlers, accept_mock_evidence=True)
    assert actions == [Action(kind="nominate_recipe", target_id="run-001", detail={"run_id": "run-001"})]
    assert seen == [(store, actions[0])]


def test_mock_evidence_does_not_trigger_without_the_switch(store):
    _drive_to_promotion(store, evaluator=_fake_evaluator([]))
    seen = []
    assert reconcile_once(store, handlers={"nominate_recipe": lambda s, a: seen.append(a)}) == []
    assert seen == []


def test_a_handler_that_writes_its_marker_runs_once(store):
    _drive_to_promotion(store, evaluator=_fake_evaluator([]))
    calls = []

    def nominate(s, action):
        calls.append(action.target_id)
        note = Envelope(id=f"nomination-{action.target_id}", type="nomination_note", version=1,
                        created_at="2026-10-09T00:00:00Z", created_by=OwnerRef(), status="executed",
                        schema_version=1, links=[action.target_id], payload={"result": "skipped"})
        s.write_product(nomination_file(action.target_id), note, "fake: nomination")

    for _ in range(3):
        reconcile_once(store, handlers={"nominate_recipe": nominate}, accept_mock_evidence=True)
    assert calls == ["run-001"]


def test_handler_exceptions_propagate_unchanged(store):
    _drive_to_promotion(store, evaluator=_fake_evaluator([]))

    class Boom(Exception):
        pass

    def boom(s, a):
        raise Boom("transient")

    for _ in range(2):  # 不吞、不记;下一 tick 再次触发
        with pytest.raises(Boom, match="transient"):
            reconcile_once(store, handlers={"nominate_recipe": boom}, accept_mock_evidence=True)


def test_an_action_kind_without_a_handler_never_appears(store):
    _drive_to_promotion(store, evaluator=_fake_evaluator([]))
    actions = reconcile_once(store, handlers={"start_agent_round": lambda s, a: None},
                             accept_mock_evidence=True)
    # nominate_recipe / plan_next_round 的条件都满足,但没有处理器,所以不出现
    assert [a.kind for a in actions] == ["start_agent_round"]


def test_unknown_handler_keys_are_rejected_before_anything_runs(store):
    _put_directive(store, "run-001")
    with pytest.raises(ValueError, match="dispatch_directive"):
        reconcile_once(store, handlers={"dispatch_directive": lambda s, a: None})
    assert store.read_product(f"{run_dir('run-001')}/directive.yaml").status == "pending"
    with pytest.raises(ValueError, match=r"\['nominate'\]"):
        run_daemon(store, 0, max_ticks=1, handlers={"nominate": lambda s, a: None})
    with pytest.raises(ValueError):
        apply_action(store, Action("await_review", "x", {}), handlers={"promote_candidate": lambda s, a: None})


def test_apply_action_routes_closed_loop_kinds_to_their_handler(store):
    seen = []
    action = Action(kind="admit_recipe", target_id="recipe-run-001", detail={})
    apply_action(store, action, handlers={"admit_recipe": lambda s, a: seen.append(a)})
    assert seen == [action]


def test_apply_action_ignores_a_closed_loop_kind_without_a_handler(store):
    _put_directive(store, "run-001")
    before = _git_log(store)
    apply_action(store, Action(kind="admit_recipe", target_id="recipe-run-001", detail={}))
    assert _git_log(store) == before


def test_run_daemon_passes_handlers_evaluator_and_switch_to_every_tick(store):
    _put_directive(store, "run-001")
    calls, nominated = [], []
    run_daemon(store, 0, max_ticks=3, evaluator=_fake_evaluator(calls))
    _approve(store, "run-001")
    run_daemon(store, 0, max_ticks=2, evaluator=_fake_evaluator(calls),
               handlers={"nominate_recipe": lambda s, a: nominated.append(a.target_id)},
               accept_mock_evidence=True)
    assert calls == [("run-001", "cand-run-001")]
    assert nominated == ["run-001"]  # 第一 tick 晋级,第二 tick 才看到 effective 候选


@pytest.mark.parametrize("fn,args", [
    (reconcile_once, (None,)),
    (apply_action, (Action("noop", "x", {}), None)),
    (run_daemon, (0, 1, None)),
])
def test_the_new_parameters_are_keyword_only(store, fn, args):
    with pytest.raises(TypeError):
        fn(store, *args, {})
```

- [ ] **Step 2: 运行,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_daemon_closed_loop_seams.py`
Expected: `12 failed, 4 passed`。失败的都是 `TypeError: ... got an unexpected keyword argument`(`handlers` 或 `evaluator`);通过的 4 条是 `test_apply_action_ignores_a_closed_loop_kind_without_a_handler`(现有代码对未知动作本来就什么都不做)与三条 keyword-only 用例(现有签名同样拒绝多余的位置参数)。

- [ ] **Step 3: 写实现**

在仓库根目录运行(差异经标准输入交给 `git apply`,同任务 90);差异应用到 `src/controlplane/daemon.py`,共 +57 −7 行:

```bash
git apply <<'PATCH'
--- a/src/controlplane/daemon.py
+++ b/src/controlplane/daemon.py
@@ -2,7 +2,7 @@
 
 import re
 import time
-from collections.abc import Callable
+from collections.abc import Callable, Mapping
 
 import yaml
 
@@ -23,6 +23,7 @@
     run_dir,
 )
 from controlplane.reconcile import (
+    CLOSED_LOOP_ACTIONS,
     DirectiveView,
     CandidateView,
     GateView,
@@ -42,6 +43,12 @@
 # 把 directive 置 executed。状态迁移归调和循环,派发器不碰 directive 的状态。
 Dispatcher = Callable[[GitStore, Envelope], None]
 
+# 闭环集成设计 §3.3(R2):闭环动作的处理器。处理器自己负责写终态记录;确定性失败写自己产物的
+# failed 与失败记录,瞬态异常原样上抛(与派发器同一约定,守护循环会退出)。
+Handler = Callable[[GitStore, Action], None]
+# 诊断评估器:(store, run_id, candidate_id),把诊断报告写到 diagnostic_report_file(run_id)。
+Evaluator = Callable[[GitStore, str, str], None]
+
 _MODEL_FILE = re.compile(r"zengen-v(\d+)\.yaml$")
 
 
@@ -206,9 +213,31 @@
     return views
 
 
+def _check_handlers(handlers: Mapping[str, Handler] | None) -> frozenset[str]:
+    if not handlers:
+        return frozenset()
+    unknown = set(handlers) - CLOSED_LOOP_ACTIONS
+    if unknown:
+        raise ValueError(
+            f"handlers only cover closed-loop actions {sorted(CLOSED_LOOP_ACTIONS)}; "
+            f"got unknown kinds {sorted(unknown)}"
+        )
+    return frozenset(handlers)
+
+
 def apply_action(
-    store: GitStore, action: Action, *, dispatcher: Dispatcher | None = None
+    store: GitStore,
+    action: Action,
+    *,
+    dispatcher: Dispatcher | None = None,
+    handlers: Mapping[str, Handler] | None = None,
+    evaluator: Evaluator | None = None,
 ) -> None:
+    _check_handlers(handlers)
+    if handlers and action.kind in handlers:
+        # 闭环动作:处理器异常原样上抛,不吞
+        handlers[action.kind](store, action)
+        return
     if action.kind == "dispatch_directive":
         run_id = action.target_id
         path = f"{run_dir(run_id)}/directive.yaml"
@@ -272,6 +301,10 @@
         store.write_product(f"{run_dir(run_id)}/outcome.yaml", outcome, f"reconcile: outcome {run_id}")
     elif action.kind == "request_diagnostic":
         run_id = action.detail["run_id"]
+        if evaluator is not None:
+            # 注入的评估器只替换这一步:它自己把报告写到 diagnostic_report_file(run_id)
+            evaluator(store, run_id, action.target_id)
+            return
         # 骨架里计算层由 stub 顶替（同 execute_dispatch）。stub 报告带 stub 标记，
         # 审核门 review 会醒目标出，不会被误当成真诊断。真实诊断见 diagnostic.orchestrator。
         report = diagnostic_evaluator_stub(run_id, action.target_id)
@@ -282,13 +315,23 @@
     # await_review / noop：无自动动作，等待人工
 
 
-def reconcile_once(store: GitStore, *, dispatcher: Dispatcher | None = None) -> list[Action]:
+def reconcile_once(
+    store: GitStore,
+    *,
+    dispatcher: Dispatcher | None = None,
+    handlers: Mapping[str, Handler] | None = None,
+    evaluator: Evaluator | None = None,
+    accept_mock_evidence: bool = False,
+) -> list[Action]:
     """一次性调和：读状态 → reconcile → 执行非人工动作。返回本轮动作列表。
-    dispatcher 缺省为桩;注入后只替换"下发 directive"这一步,其余动作不变。"""
-    state = read_tree_state(store)
+    dispatcher 缺省为桩;注入后只替换"下发 directive"这一步,其余动作不变。
+    handlers 的键就是启用的闭环动作(闭环集成设计 §3.3);没有处理器的动作种类不会出现在
+    返回列表里。evaluator 缺省为诊断桩;注入后只替换 request_diagnostic 这一步。"""
+    enabled = _check_handlers(handlers)
+    state = read_tree_state(store, enabled=enabled, accept_mock_evidence=accept_mock_evidence)
     actions = reconcile(state)
     for action in actions:
-        apply_action(store, action, dispatcher=dispatcher)
+        apply_action(store, action, dispatcher=dispatcher, handlers=handlers, evaluator=evaluator)
     return actions
 
 
@@ -298,11 +341,18 @@
     max_ticks: int | None = None,
     *,
     dispatcher: Dispatcher | None = None,
+    handlers: Mapping[str, Handler] | None = None,
+    evaluator: Evaluator | None = None,
+    accept_mock_evidence: bool = False,
 ) -> None:
     """轮询守护进程（spec 补充 B.1：轮询而非 inotify）。max_ticks 供测试限次。"""
+    _check_handlers(handlers)
     ticks = 0
     while max_ticks is None or ticks < max_ticks:
-        reconcile_once(store, dispatcher=dispatcher)
+        reconcile_once(
+            store, dispatcher=dispatcher, handlers=handlers, evaluator=evaluator,
+            accept_mock_evidence=accept_mock_evidence,
+        )
         ticks += 1
         if max_ticks is not None and ticks >= max_ticks:
             break
PATCH
```

- [ ] **Step 4: 运行,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_daemon_closed_loop_seams.py tests/test_daemon_closed_loop_views.py tests/test_daemon.py tests/test_daemon_dispatcher.py tests/test_e2e_loop.py tests/test_mt_wire.py`
Expected: `62 passed`。

- [ ] **Step 5: 全量回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: `B+124 passed`(820 起点实测 `944 passed`),无警告。

- [ ] **Step 6: 提交**

```bash
git add src/controlplane/daemon.py
git add tests/test_daemon_closed_loop_seams.py
git commit -m "feat: inject closed-loop handlers and a diagnostic evaluator into the reconcile loop (nothing injected, nothing changes)"
```

---

### Task 94: 诊断资产的信封读写与 `backends` 标记

**Files:**
- Create: `src/diagnostic/assets.py`(74 行)
- Modify: `src/diagnostic/store_adapter.py`(30 → 44 行)、`src/diagnostic/orchestrator.py`(240 → 244 行)(合计 +22 −4)
- Test: `tests/test_diag_assets.py`(新建,15 个用例)

**Interfaces:**
- Consumes: `diagnostic.paths.taxonomy_file`、`item_pool_file`(现有,转出自 `controlplane.paths`);`DiagnosticTaxonomy.to_dict/from_dict`、`ItemPool.to_dict/from_dict`;计划 1 的 `diagnostic_grade`(只在测试里用)。
- Produces:
  - `diagnostic.assets`:`TAXONOMY_TYPE = "diagnostic_taxonomy"`、`ITEM_POOL_TYPE = "diagnostic_item_pool"`、`DiagnosticAssetError`、`write_taxonomy(store, taxonomy) -> str`、`read_taxonomy(store, version) -> DiagnosticTaxonomy`、`write_item_pool(store, pool) -> str`、`read_item_pool(store, version) -> ItemPool`。
  - `diagnostic.store_adapter`:`BACKENDS_KEY = "backends"`、`BACKENDS_VALUES = ("mock", "real")`、`write_report(store, run_id, report, *, backends=None)`。
  - `diagnostic.orchestrator.run_diagnostic(..., *, baseline_scores, dynamic_specs, backends=None)`。

**设计要点:**
- **资产是信封产物**(Ruling 10):状态 `effective`,载荷就是数据类的 `to_dict()`。写函数返回路径,方便调用方记录;重复写入相同内容不产生提交(`GitStore.write_product` 的既有行为,有测试)。
- **读取时校验**:文件不存在、信封类型不对、载荷里的版本号与路径里的版本号不一致,都抛 `DiagnosticAssetError` 并写明路径。
- **`backends` 进 `write_report` 的可选参数**(Ruling 9):`None` 时载荷与以前逐字节相同(有测试直接比较 `payload == report.to_dict()`);非法取值在写之前抛 `ValueError`,树里什么都不留。
- **`read_report` 不变**:`DiagnosticReport.from_dict` 只取已知键,多出的 `backends` 被忽略。
- **两个等级测试**:`run_diagnostic` 不传 `backends` 时报告按计划 1 的规则是 `real`(有分数、无标记);传 `mock` 是 `mock`;既有桩报告是 `stub`。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_diag_assets.py`:

```python
"""诊断资产(分类表与题库)的信封读写,以及 write_report / run_diagnostic 的 backends 标记(R17)。"""
import shutil

import pytest

from controlplane.envelope import Envelope, OwnerRef
from controlplane.evidence import diagnostic_grade
from controlplane.gitstore import GitStore
from controlplane.paths import diagnostic_report_file
from diagnostic.assets import (
    ITEM_POOL_TYPE,
    TAXONOMY_TYPE,
    DiagnosticAssetError,
    read_item_pool,
    read_taxonomy,
    write_item_pool,
    write_taxonomy,
)
from diagnostic.datamodel import (
    ConsistencySignals,
    DiagnosticItem,
    DiagnosticReport,
    DiagnosticTaxonomy,
    RegressionFlags,
    VariantFamily,
)
from diagnostic.orchestrator import DiagnosticConfig, run_diagnostic
from diagnostic.paths import item_pool_file, taxonomy_file
from diagnostic.scorer import MockJudgeBackend
from diagnostic.someval_adapter import MockSoMEvalRunner
from diagnostic.store_adapter import BACKENDS_KEY, BACKENDS_VALUES, read_report, write_report
from diagnostic.taxonomy_loader import ItemPool
from simulator.stubs import diagnostic_evaluator_stub

TAX = DiagnosticTaxonomy(
    "tax-v1", ["mental_representation"], {"mental_representation": ["belief"]},
    {"belief": ["false_belief_transfer"]}, ["format_compliance"],
)
POOL = ItemPool(
    "pool-v1",
    [DiagnosticItem("i1", "false_belief_transfer", "mcq", "rule", "static", "B", None, "vf1"),
     DiagnosticItem("i1v", "false_belief_transfer", "mcq", "rule", "static", "C", None, "vf1")],
    [VariantFamily("vf1", "i1", ["i1v"])],
)


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    return s


def _report_obj():
    return DiagnosticReport("diag-1", "cand-1", "zengen-v3", "bench-v1", "full", 4,
                            [], [], RegressionFlags([]), ConsistencySignals({}, 0.0), ["run-1"])


# ---- 资产 ----

def test_assets_round_trip_as_envelopes_at_the_versioned_paths(store):
    assert write_taxonomy(store, TAX) == taxonomy_file("tax-v1")
    assert write_item_pool(store, POOL) == item_pool_file("pool-v1")
    assert store.read_product(taxonomy_file("tax-v1")).type == TAXONOMY_TYPE == "diagnostic_taxonomy"
    assert store.read_product(item_pool_file("pool-v1")).type == ITEM_POOL_TYPE == "diagnostic_item_pool"
    assert store.read_product(taxonomy_file("tax-v1")).status == "effective"
    assert read_taxonomy(store, "tax-v1") == TAX
    assert read_item_pool(store, "pool-v1").to_dict() == POOL.to_dict()


def test_rewriting_the_same_asset_is_a_no_op(store):
    write_taxonomy(store, TAX)
    head = store._git("rev-parse", "HEAD")
    write_taxonomy(store, TAX)
    assert store._git("rev-parse", "HEAD") == head


def test_a_missing_asset_fails_loudly(store):
    with pytest.raises(DiagnosticAssetError, match="taxonomy_tax-v9"):
        read_taxonomy(store, "tax-v9")
    with pytest.raises(DiagnosticAssetError, match="item_pool_pool-v9"):
        read_item_pool(store, "pool-v9")


def test_a_wrong_product_type_at_the_asset_path_is_refused(store):
    env = Envelope(id="x", type="config", version=1, created_at="t", created_by=OwnerRef(),
                   status="effective", schema_version=1, payload=TAX.to_dict())
    store.write_product(taxonomy_file("tax-v1"), env, "seed wrong type")
    with pytest.raises(DiagnosticAssetError, match="not 'diagnostic_taxonomy'"):
        read_taxonomy(store, "tax-v1")


def test_a_version_mismatch_inside_the_asset_is_refused(store):
    write_taxonomy(store, DiagnosticTaxonomy("tax-v2", [], {}, {}, []))
    shutil.copy(store.repo_root / taxonomy_file("tax-v2"), store.repo_root / taxonomy_file("tax-v1"))
    with pytest.raises(DiagnosticAssetError, match="'tax-v2', not 'tax-v1'"):
        read_taxonomy(store, "tax-v1")


# ---- write_report 的 backends 参数 ----

def test_write_report_without_backends_is_unchanged(store):
    write_report(store, "run-1", _report_obj())
    payload = store.read_product(diagnostic_report_file("run-1")).payload
    assert payload == _report_obj().to_dict()
    assert "backends" not in payload


@pytest.mark.parametrize("backends", ["mock", "real"])
def test_write_report_records_the_backends_marker_and_still_reads_back(store, backends):
    write_report(store, "run-1", _report_obj(), backends=backends)
    assert store.read_product(diagnostic_report_file("run-1")).payload["backends"] == backends
    assert read_report(store, "run-1") == _report_obj()  # from_dict 忽略多出的键


def test_write_report_rejects_an_unknown_marker(store):
    with pytest.raises(ValueError, match="backends"):
        write_report(store, "run-1", _report_obj(), backends="fake")
    assert not store.exists(diagnostic_report_file("run-1"))


def test_the_backends_marker_lands_in_the_same_commit_as_the_report(store):
    write_report(store, "run-1", _report_obj(), backends="mock")
    assert store._git("log", "--format=%s").splitlines()[0] == "diagnostic_report: diag-1 for run-1"
    assert len(store._git("log", "--format=%s").splitlines()) == 1


def test_the_marker_key_and_values_are_fixed():
    assert (BACKENDS_KEY, BACKENDS_VALUES) == ("backends", ("mock", "real"))


@pytest.mark.parametrize("backends,grade", [(None, "real"), ("mock", "mock"), ("real", "real")])
def test_run_diagnostic_passes_the_marker_through(store, backends, grade):
    someval = MockSoMEvalRunner(fixed={"i1": ["B"] * 4, "i1v": ["C"] * 4})
    cfg = DiagnosticConfig("run-1", "cand-1", "cand-model", "none", "full", 4, "bench-v1")
    kw = {} if backends is None else {"backends": backends}
    run_diagnostic(cfg, TAX, POOL, someval, MockJudgeBackend("external-judge", {}), store,
                   baseline_scores=[], dynamic_specs=[], **kw)
    payload = store.read_product(diagnostic_report_file("run-1")).payload
    assert payload.get("backends") == backends
    assert diagnostic_grade(payload) == grade


def test_the_existing_stub_report_still_grades_stub():
    assert diagnostic_grade(diagnostic_evaluator_stub("run-001", "cand-run-001").payload) == "stub"
```

- [ ] **Step 2: 运行,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_diag_assets.py`
Expected: 收集阶段报错 `ModuleNotFoundError: No module named 'diagnostic.assets'`,`1 error`。

- [ ] **Step 3: 写实现**

新建 `src/diagnostic/assets.py`:

```python
"""诊断资产(分类表与题库)作为信封产物的读写。

controlplane.paths 只给了 taxonomy_file / item_pool_file 两个路径函数,没有读写代码;这里补上。
产物类型:diagnostic_taxonomy、diagnostic_item_pool;状态 effective(登记即生效,不走审核门)。
载荷就是 DiagnosticTaxonomy.to_dict() / ItemPool.to_dict()。
"""
from __future__ import annotations

from controlplane.envelope import CURRENT_SCHEMA_VERSION, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from diagnostic.datamodel import DiagnosticTaxonomy
from diagnostic.paths import item_pool_file, taxonomy_file
from diagnostic.taxonomy_loader import ItemPool

TAXONOMY_TYPE = "diagnostic_taxonomy"
ITEM_POOL_TYPE = "diagnostic_item_pool"

_NOW = "2026-10-09T00:00:00Z"


class DiagnosticAssetError(Exception):
    """诊断资产缺失,或那个路径上放的不是对应类型的产物。"""


def _envelope(id_: str, type_: str, payload: dict) -> Envelope:
    return Envelope(
        id=id_, type=type_, version=1, created_at=_NOW, created_by=OwnerRef(),
        status="effective", schema_version=CURRENT_SCHEMA_VERSION, links=[], payload=payload,
    )


def _read(store: GitStore, rel: str, expected_type: str) -> Envelope:
    if not store.exists(rel):
        raise DiagnosticAssetError(f"diagnostic asset missing: {rel}")
    env = store.read_product(rel)
    if env.type != expected_type:
        raise DiagnosticAssetError(f"{rel} is a {env.type!r} product, not {expected_type!r}")
    return env


def write_taxonomy(store: GitStore, taxonomy: DiagnosticTaxonomy) -> str:
    rel = taxonomy_file(taxonomy.version)
    store.write_product(
        rel, _envelope(f"taxonomy-{taxonomy.version}", TAXONOMY_TYPE, taxonomy.to_dict()),
        f"diagnostic: taxonomy {taxonomy.version}",
    )
    return rel


def read_taxonomy(store: GitStore, version: str) -> DiagnosticTaxonomy:
    taxonomy = DiagnosticTaxonomy.from_dict(_read(store, taxonomy_file(version), TAXONOMY_TYPE).payload)
    if taxonomy.version != version:
        raise DiagnosticAssetError(
            f"{taxonomy_file(version)} carries taxonomy version {taxonomy.version!r}, not {version!r}"
        )
    return taxonomy


def write_item_pool(store: GitStore, pool: ItemPool) -> str:
    rel = item_pool_file(pool.version)
    store.write_product(
        rel, _envelope(f"item-pool-{pool.version}", ITEM_POOL_TYPE, pool.to_dict()),
        f"diagnostic: item pool {pool.version}",
    )
    return rel


def read_item_pool(store: GitStore, version: str) -> ItemPool:
    pool = ItemPool.from_dict(_read(store, item_pool_file(version), ITEM_POOL_TYPE).payload)
    if pool.version != version:
        raise DiagnosticAssetError(
            f"{item_pool_file(version)} carries item pool version {pool.version!r}, not {version!r}"
        )
    return pool
```

再在仓库根目录运行(差异经标准输入交给 `git apply`,同任务 90);差异应用到 `src/diagnostic/store_adapter.py` 与 `src/diagnostic/orchestrator.py`,共 +22 −4 行:

```bash
git apply <<'PATCH'
--- a/src/diagnostic/store_adapter.py
+++ b/src/diagnostic/store_adapter.py
@@ -7,8 +7,22 @@
 
 _NOW = "2026-09-27T00:00:00Z"
 
+# 诊断报告载荷里的后端标记(闭环集成设计 R17)。controlplane.evidence.diagnostic_grade 读它定等级;
+# cli/src/commands.ts 的 summarizeDiagnostic 读它打印 [MOCK BACKENDS]。两侧各写一次。
+BACKENDS_KEY = "backends"
+BACKENDS_VALUES = ("mock", "real")
 
-def write_report(store: GitStore, run_id: str, report: DiagnosticReport) -> None:
+
+def write_report(
+    store: GitStore, run_id: str, report: DiagnosticReport, *, backends: str | None = None
+) -> None:
+    """backends 为 None 时载荷与以前逐字节相同;否则在载荷里加 backends 键("mock" 或 "real")。
+    标记与报告在同一次写入里落盘,不存在"报告已写、标记未写"的窗口。"""
+    payload = report.to_dict()
+    if backends is not None:
+        if backends not in BACKENDS_VALUES:
+            raise ValueError(f"backends must be one of {BACKENDS_VALUES}, got {backends!r}")
+        payload[BACKENDS_KEY] = backends
     env = Envelope(
         id=report.id,
         type="diagnostic_report",
@@ -18,7 +32,7 @@
         status="executed",
         schema_version=CURRENT_SCHEMA_VERSION,
         links=report.links,
-        payload=report.to_dict(),
+        payload=payload,
     )
     store.write_product(
         diagnostic_report_file(run_id), env, message=f"diagnostic_report: {report.id} for {run_id}"
--- a/src/diagnostic/orchestrator.py
+++ b/src/diagnostic/orchestrator.py
@@ -60,9 +60,13 @@
     *,
     baseline_scores: list[DimensionScore],
     dynamic_specs: list[dict],
+    backends: str | None = None,
 ) -> DiagnosticReport:
-    """Run one full diagnostic pass and persist the resulting report."""
+    """Run one full diagnostic pass and persist the resulting report.
 
+    backends ("mock" | "real" | None) is written into the persisted payload by
+    write_report in the same write (R17); None keeps the payload unchanged."""
+
     # 1. Select items for this run's tier.
     selected_items = select_items(pool, cfg.tier)
     static_items = [item for item in selected_items if item.track == "static"]
@@ -195,7 +199,7 @@
         consistency_signals=consistency_signals,
         run_id=cfg.run_id,
     )
-    write_report(store, cfg.run_id, report)
+    write_report(store, cfg.run_id, report, backends=backends)
     return report
 
 
PATCH
```

- [ ] **Step 4: 运行,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_diag_assets.py`
Expected: `15 passed`。

再跑既有诊断测试,确认一行不改仍通过:

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_diag_*.py`
Expected: `96 passed`(既有 81 个,新 15 个)。

- [ ] **Step 5: 全量回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: `B+139 passed`(820 起点实测 `959 passed`),无警告。

- [ ] **Step 6: 提交**

```bash
git add src/diagnostic/assets.py
git add src/diagnostic/store_adapter.py
git add src/diagnostic/orchestrator.py
git add tests/test_diag_assets.py
git commit -m "feat: diagnostic taxonomy and item pool as envelope products; diagnostic reports can carry a backends marker"
```

---

### Task 95: 真实诊断评估器 `make_diagnostic_evaluator`

**Files:**
- Create: `src/diagnostic/evaluator.py`(111 行)
- Test: `tests/test_diag_evaluator.py`(新建,21 个用例)

**Interfaces:**
- Consumes: 任务 94 的 `read_taxonomy`、`read_item_pool`、`BACKENDS_VALUES`、`run_diagnostic(..., backends=...)`;任务 93 的 `reconcile_once(..., evaluator=...)`(只在测试里用);计划 1 的 `diagnostic_grade`。
- Produces: `make_diagnostic_evaluator(*, taxonomy_version, item_pool_version, someval_runner, judge_backend, benchmark_version, tier="full", sampling_k=4, backends="mock") -> Callable[[GitStore, str, str], None]`(形状即 `controlplane.daemon.Evaluator`);`resolve_baseline(store, run_id, benchmark_version, *, backends=None) -> tuple[str, list[DimensionScore]]`(前三个位置参数与原来相同,`backends` 是新增的 keyword-only 参数,默认不比较证据等级);`NO_BASELINE = "none"`。

**设计要点:**
- **返回值的类型写成 `Callable[[GitStore, str, str], None]`**,不导入 `controlplane.daemon.Evaluator`:`controlplane.daemon` 导入 `simulator.stubs`,`diagnostic` 本来就依赖 `simulator`,导入并不会成环;但只为一个类型别名把整个守护模块拉进来不值得。形状相同即可注入。
- **资产每次调用时读**,不在构造时读:守护进程长跑期间资产可能被更新;构造时没有 `store`。
- **基线规则见 Ruling 11 与 Ruling 22**:`dimension_scores` 用 `.get` 读,不是非空列表就无基线(`backends: "mock"` 的报告不看分数就判 `mock`,原先会 `KeyError`);评估器把自己的 `backends` 传给 `resolve_baseline`,基线报告的等级不同(`real` 对 `mock`)时无基线,两处都有测试;`baseline_ref` 是模型 id(例如 `zengen-v1`),与现有 `DiagnosticConfig.baseline_ref` 的用法一致(既有测试里是 `"zengen-v3"`),也是 `detect_regressions` 写进退化标记的 `baseline_version`。
- **`backends` 在构造时校验**,非法值立刻 `ValueError`,不等到第一次评估。
- **`diagnostic` 不导入 `mindtrainer`**:用 `ast` 扫 `src/diagnostic/*.py` 的全部导入语句,有测试。
- **端到端用例**把评估器注入 `reconcile_once`:派发(桩)→ 诊断(评估器,mock)→ 待审。证明它与调和循环的缝对得上,且报告里不再有 `stub` 键。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_diag_evaluator.py`:

```python
"""真实诊断评估器(闭环集成设计 §10.2、R17):backends 标记、基线解析、注入调和循环。"""
import ast
from pathlib import Path

import pytest

from controlplane.daemon import reconcile_once
from controlplane.envelope import Envelope, OwnerRef
from controlplane.evidence import diagnostic_grade
from controlplane.gitstore import GitStore
from controlplane.paths import candidate_checkpoint_file, diagnostic_report_file, model_file, run_dir
from diagnostic.assets import DiagnosticAssetError, write_item_pool, write_taxonomy
from diagnostic.datamodel import DiagnosticItem, DiagnosticTaxonomy, VariantFamily
from diagnostic.evaluator import NO_BASELINE, make_diagnostic_evaluator, resolve_baseline
from diagnostic.scorer import MockJudgeBackend
from diagnostic.someval_adapter import MockSoMEvalRunner
from diagnostic.taxonomy_loader import ItemPool
from simulator.stubs import diagnostic_evaluator_stub

TAX = DiagnosticTaxonomy(
    "tax-v1", ["mental_representation"], {"mental_representation": ["belief"]},
    {"belief": ["false_belief_transfer"]}, ["format_compliance"],
)
POOL = ItemPool(
    "pool-v1",
    [DiagnosticItem("i1", "false_belief_transfer", "mcq", "rule", "static", "B", None, "vf1"),
     DiagnosticItem("i1v", "false_belief_transfer", "mcq", "rule", "static", "C", None, "vf1")],
    [VariantFamily("vf1", "i1", ["i1v"])],
)


def _someval(right=4):
    wrong = 4 - right
    return MockSoMEvalRunner(fixed={"i1": ["B"] * right + ["X"] * wrong, "i1v": ["C"] * right + ["X"] * wrong})


def _judge():
    return MockJudgeBackend(model_id="external-judge", fixed={})


def _evaluator(right=4, **kw):
    return make_diagnostic_evaluator(
        taxonomy_version="tax-v1", item_pool_version="pool-v1", someval_runner=_someval(right),
        judge_backend=_judge(), benchmark_version="bench-v1", **kw,
    )


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    return s


@pytest.fixture
def seeded(store):
    write_taxonomy(store, TAX)
    write_item_pool(store, POOL)
    return store


# ---- 评估器 ----

def test_the_evaluator_writes_a_mock_graded_report(seeded):
    _evaluator()(seeded, "run-001", "cand-run-001")
    payload = seeded.read_product(diagnostic_report_file("run-001")).payload
    assert payload["backends"] == "mock"
    assert payload["candidate_ref"] == "cand-run-001"
    assert payload["baseline_ref"] == NO_BASELINE == "none"
    assert payload["regression_flags"] == {"flags": []}
    assert any(d["level"] == "l1" for d in payload["dimension_scores"])
    assert diagnostic_grade(payload) == "mock"


def test_a_stub_report_from_the_daemon_and_a_mock_report_from_the_evaluator_grade_differently(seeded):
    _evaluator()(seeded, "run-001", "cand-run-001")
    seeded.write_product(diagnostic_report_file("run-002"), diagnostic_evaluator_stub("run-002", "cand-run-002"), "stub")
    assert diagnostic_grade(seeded.read_product(diagnostic_report_file("run-001")).payload) == "mock"
    assert diagnostic_grade(seeded.read_product(diagnostic_report_file("run-002")).payload) == "stub"


def test_a_real_marker_grades_real(seeded):
    _evaluator(backends="real")(seeded, "run-001", "cand-run-001")
    assert diagnostic_grade(seeded.read_product(diagnostic_report_file("run-001")).payload) == "real"


def test_the_marker_is_validated_when_the_evaluator_is_built():
    with pytest.raises(ValueError, match="backends"):
        _evaluator(backends="pretend")


def test_the_evaluator_fails_loudly_without_assets(store):
    with pytest.raises(DiagnosticAssetError):
        _evaluator()(store, "run-001", "cand-run-001")
    assert not store.exists(diagnostic_report_file("run-001"))


def _promote(store, n, run_id, status="effective", links=None):
    env = Envelope(id=f"zengen-v{n}", type="zengen_model", version=1, created_at="t",
                   created_by=OwnerRef(), status=status, schema_version=1,
                   links=links if links is not None else [f"cand-{run_id}", run_id],
                   payload={"promoted_from": f"cand-{run_id}"})
    store.write_product(model_file(n), env, f"seed zengen-v{n}")


def test_the_baseline_is_the_latest_promoted_models_run(seeded):
    _evaluator(right=4)(seeded, "run-001", "cand-run-001")
    _promote(seeded, 1, "run-001")
    _evaluator(right=0)(seeded, "run-002", "cand-run-002")  # 全错:相对基线明显退化
    payload = seeded.read_product(diagnostic_report_file("run-002")).payload
    assert payload["baseline_ref"] == "zengen-v1"
    assert payload["regression_flags"]["flags"], "a disjoint-CI drop against the baseline must be flagged"
    assert all(f["baseline_version"] == "zengen-v1" for f in payload["regression_flags"]["flags"])


@pytest.mark.parametrize("case", ["no_model", "not_effective", "no_links", "same_run", "no_report",
                                  "stub_report", "other_benchmark"])
def test_no_comparable_baseline_reads_as_none(seeded, case):
    if case == "no_model":
        pass
    elif case == "not_effective":
        _evaluator()(seeded, "run-001", "cand-run-001")
        _promote(seeded, 1, "run-001", status="deprecated")
    elif case == "no_links":
        _evaluator()(seeded, "run-001", "cand-run-001")
        _promote(seeded, 1, "run-001", links=[])
    elif case == "same_run":
        _evaluator()(seeded, "run-002", "cand-run-002")
        _promote(seeded, 1, "run-002")
    elif case == "no_report":
        _promote(seeded, 1, "run-001")
    elif case == "stub_report":
        seeded.write_product(diagnostic_report_file("run-001"), diagnostic_evaluator_stub("run-001", "c"), "stub")
        _promote(seeded, 1, "run-001")
    elif case == "other_benchmark":
        make_diagnostic_evaluator(taxonomy_version="tax-v1", item_pool_version="pool-v1",
                                  someval_runner=_someval(), judge_backend=_judge(),
                                  benchmark_version="bench-v0")(seeded, "run-001", "cand-run-001")
        _promote(seeded, 1, "run-001")
    assert resolve_baseline(seeded, "run-002", "bench-v1") == (NO_BASELINE, [])


@pytest.mark.parametrize("payload", [
    {"backends": "mock", "benchmark_version": "bench-v1"},                           # 只有标记,没有分数
    {"backends": "mock", "benchmark_version": "bench-v1", "dimension_scores": []},   # 分数为空
    {"backends": "mock", "benchmark_version": "bench-v1", "dimension_scores": {}},   # 分数不是列表
])
def test_a_baseline_report_without_scores_reads_as_none(seeded, payload):
    # diagnostic_grade 对 backends:"mock" 不看分数就返回 mock;这里不能因 KeyError 让守护进程退出
    env = Envelope(id="diag-run-001", type="diagnostic_report", version=1, created_at="t",
                   created_by=OwnerRef(), status="executed", schema_version=1, links=["run-001"], payload=payload)
    seeded.write_product(diagnostic_report_file("run-001"), env, "seed odd baseline")
    _promote(seeded, 1, "run-001")
    assert resolve_baseline(seeded, "run-002", "bench-v1") == (NO_BASELINE, [])
    assert resolve_baseline(seeded, "run-002", "bench-v1", backends="mock") == (NO_BASELINE, [])


@pytest.mark.parametrize("baseline,current", [("real", "mock"), ("mock", "real")])
def test_a_baseline_of_another_evidence_grade_reads_as_none(seeded, baseline, current):
    _evaluator(backends=baseline)(seeded, "run-001", "cand-run-001")
    _promote(seeded, 1, "run-001")
    assert resolve_baseline(seeded, "run-002", "bench-v1", backends=current) == (NO_BASELINE, [])
    assert resolve_baseline(seeded, "run-002", "bench-v1", backends=baseline)[0] == "zengen-v1"
    _evaluator(right=0, backends=current)(seeded, "run-002", "cand-run-002")  # 全错,但不与另一种等级比
    payload = seeded.read_product(diagnostic_report_file("run-002")).payload
    assert (payload["baseline_ref"], payload["regression_flags"]) == (NO_BASELINE, {"flags": []})


def test_the_highest_effective_version_wins_numerically(seeded):
    _evaluator()(seeded, "run-001", "cand-run-001")
    _evaluator()(seeded, "run-009", "cand-run-009")
    _promote(seeded, 9, "run-009")
    _promote(seeded, 10, "run-001")
    assert resolve_baseline(seeded, "run-011", "bench-v1")[0] == "zengen-v10"


def test_injected_into_the_loop_it_replaces_the_stub_diagnostic(seeded):
    env = Envelope(id="run-001", type="directive", version=1, created_at="t", created_by=OwnerRef(),
                   status="pending", schema_version=1, payload={"target_dimension": "tom", "recipe_ref": ""})
    seeded.write_product(f"{run_dir('run-001')}/directive.yaml", env, "human: directive")
    ev = _evaluator()
    assert [a.kind for a in reconcile_once(seeded, evaluator=ev)] == ["dispatch_directive"]
    assert [a.kind for a in reconcile_once(seeded, evaluator=ev)] == ["request_diagnostic"]
    payload = seeded.read_product(diagnostic_report_file("run-001")).payload
    assert "stub" not in payload and payload["backends"] == "mock"
    assert payload["candidate_ref"] == seeded.read_product(candidate_checkpoint_file("run-001")).id
    assert [a.kind for a in reconcile_once(seeded, evaluator=ev)] == ["await_review"]


def test_diagnostic_never_imports_mindtrainer():
    root = Path(__file__).resolve().parents[1] / "src" / "diagnostic"
    for path in sorted(root.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            assert not any(n == "mindtrainer" or n.startswith("mindtrainer.") for n in names), path.name
```

- [ ] **Step 2: 运行,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_diag_evaluator.py`
Expected: 收集阶段报错 `ModuleNotFoundError: No module named 'diagnostic.evaluator'`,`1 error`。

- [ ] **Step 3: 写实现**

新建 `src/diagnostic/evaluator.py`:

```python
"""真实诊断评估器(闭环集成设计 §10.2):调和循环 request_diagnostic 这一步的可注入替身。

形状与 controlplane.daemon.Evaluator 相同:(store, run_id, candidate_id) -> None,
把报告写到 diagnostic_report_file(run_id)。diagnostic 只依赖 controlplane 与 simulator,
不导入 mindtrainer。V1 只跑静态轨(dynamic_specs 恒为空)。
"""
from __future__ import annotations

import re
from collections.abc import Callable

from controlplane.evidence import diagnostic_grade
from controlplane.gitstore import GitStore
from controlplane.paths import CONTROL_PLANE_ROOT
from diagnostic.assets import read_item_pool, read_taxonomy
from diagnostic.datamodel import DimensionScore
from diagnostic.orchestrator import DiagnosticConfig, run_diagnostic
from diagnostic.paths import diagnostic_report_file
from diagnostic.scorer import JudgeBackend
from diagnostic.someval_adapter import SoMEvalRunner
from diagnostic.store_adapter import BACKENDS_VALUES

NO_BASELINE = "none"

_MODEL_FILE = re.compile(r"zengen-v(\d+)\.yaml$")


def _latest_effective_model(store: GitStore):
    """版本号(按数字)最大、状态 effective 的 zengen 模型信封;没有则 None。"""
    found = []
    for rel in store.list_products(f"{CONTROL_PLANE_ROOT}/models"):
        match = _MODEL_FILE.search(rel)
        if match:
            found.append((int(match.group(1)), rel))
    for _, rel in sorted(found, reverse=True):
        env = store.read_product(rel)
        if env.status == "effective":
            return env
    return None


def resolve_baseline(
    store: GitStore, run_id: str, benchmark_version: str, *, backends: str | None = None
) -> tuple[str, list[DimensionScore]]:
    """基线 = 最新已晋级模型所在 run 的诊断报告。返回 (baseline_ref, baseline_scores)。

    以下情况都没有可比基线,返回 ("none", []):没有已晋级模型;模型的 links 里没有 run id
    (promote_candidate 写的是 [候选 id, run id]);那个 run 就是本次被评的 run;那个 run 没有报告;
    报告等级是 stub(没有分数);报告的 dimension_scores 不是非空列表(mock 报告可能只带 backends
    标记而没有分数);报告的 benchmark_version 与本次不同(跨版本分数不可比);给了 backends 时,
    报告的证据等级与它不同(real 对 mock,真分数与假分数不可比)。
    可比性只看 benchmark_version:报告载荷里没有题库版本,换题库必须同时换 benchmark_version。"""
    model = _latest_effective_model(store)
    if model is None or len(model.links) < 2:
        return NO_BASELINE, []
    # links = [候选 id, run id](promote_candidate);取第二项。计划 3 的 recipestore.gain.baseline_run_of
    # 取"cand-<x> 也在 links 里的那一项"(以该函数为准,不按文档行号引用),两种判法目前一致,
    # 改晋级写法时两处要一起改,计划 9 的一致性核对会加测试钉住。
    base_run = model.links[1]
    rel = diagnostic_report_file(base_run)
    if base_run == run_id or not store.exists(rel):
        return NO_BASELINE, []
    payload = store.read_product(rel).payload
    grade = diagnostic_grade(payload)
    if grade == "stub" or payload.get("benchmark_version") != benchmark_version:
        return NO_BASELINE, []
    if backends is not None and grade != backends:
        return NO_BASELINE, []
    # diagnostic_grade 对 backends:"mock" 不看分数就返回 mock,所以这里不能假定键在
    scores = payload.get("dimension_scores")
    if not isinstance(scores, list) or not scores:
        return NO_BASELINE, []
    return model.id, [DimensionScore.from_dict(d) for d in scores]


def make_diagnostic_evaluator(
    *,
    taxonomy_version: str,
    item_pool_version: str,
    someval_runner: SoMEvalRunner,
    judge_backend: JudgeBackend,
    benchmark_version: str,
    tier: str = "full",
    sampling_k: int = 4,
    backends: str = "mock",
) -> Callable[[GitStore, str, str], None]:
    """返回 Evaluator。分类表与题库每次调用时从树里读(diagnostic.assets),缺了抛
    DiagnosticAssetError(瞬态语义:原样上抛,守护循环退出,补齐资产后重启即可)。
    backends 由调用方声明("mock" 或 "real"),写进报告载荷,controlplane.evidence 据此定等级。"""
    if backends not in BACKENDS_VALUES:
        raise ValueError(f"backends must be one of {BACKENDS_VALUES}, got {backends!r}")

    def evaluate(store: GitStore, run_id: str, candidate_id: str) -> None:
        taxonomy = read_taxonomy(store, taxonomy_version)
        pool = read_item_pool(store, item_pool_version)
        baseline_ref, baseline_scores = resolve_baseline(store, run_id, benchmark_version, backends=backends)
        cfg = DiagnosticConfig(
            run_id=run_id,
            candidate_ref=candidate_id,
            candidate_model_id=candidate_id,
            baseline_ref=baseline_ref,
            tier=tier,
            sampling_k=sampling_k,
            benchmark_version=benchmark_version,
        )
        run_diagnostic(
            cfg, taxonomy, pool, someval_runner, judge_backend, store,
            baseline_scores=baseline_scores, dynamic_specs=[], backends=backends,
        )

    return evaluate
```

- [ ] **Step 4: 运行,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_diag_*.py`
Expected: `117 passed`(既有 81 个,任务 94 的 15 个,本任务 21 个)。

- [ ] **Step 5: 全量回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: `B+160 passed`(820 起点实测 `980 passed`),无警告。再跑 `grep -rn "mindtrainer" src/diagnostic`:只命中 `evaluator.py` 模块说明里的一句"不导入 mindtrainer"。

- [ ] **Step 6: 提交**

```bash
git add src/diagnostic/evaluator.py
git add tests/test_diag_evaluator.py
git commit -m "feat: a real diagnostic evaluator for the reconcile loop (assets from the tree, baseline from the latest promoted run, backends marker)"
```

---

### Task 96: 命令行按类型审核三类产物

**Files:**
- Modify: `cli/src/commands.ts`(143 → 303 行,+181 −21)
- Test: `cli/test/gates.test.ts`(新建,本任务 35 个用例)

**Interfaces:**
- Consumes: `cli/src/gittree.ts` 的 `readProduct`、`writeProduct`、`Envelope`(现有);「跨语言约定」表里的路径与载荷键。
- Produces: `export type GateKind`;`review`、`approve`、`reject` 对三类产物生效;既有导出的签名不变。

**设计要点:**
- **`findCandidatePath` 泛化为 `findGateProduct`**:对 `GATE_PATTERNS` 里的三个模式各跑一次 `git ls-files`,按信封 `id` 匹配。仍然只看已跟踪的文件(有测试);没找到时错误信息保持 `candidate <id> not found`;多处命中报 `ambiguous`(Ruling 14)。
- **`review` 分三种打印**:候选沿用 `summarizeDiagnostic`,新增 `[MOCK BACKENDS]` 与 `backends: UNREADABLE`;配方候选打印类型、锚定、结果与 `repro` 的错误和警告;提案打印发现、每个增量的定义、验证结果与 `notes`。
- **"读不出来"与"没有"严格区分**:字段缺失或类型不对一律打印 `UNREADABLE (field missing or malformed)`;空数组打印 `none`;值为 `null` 的锚定项与增益照实打印 `null`(设计 §4.2 规定暂无的填 `null`)。
- **`approve` 的证据规则**(Ruling 15):先查迁移是否合法(与既有顺序一致),再按类型查证据。配方候选调导出的 `hasReproEvidence`,它与计划 3 的 `recipestore.admit.has_repro_evidence` 逐条一致(`repro` 是对象,`errors` 与 `warnings` 都是数组,可为空),测试覆盖 `{"errors": [], "warnings": []}` 放行、`errors` 非空放行、缺 `warnings` / `errors` 是字典 / `repro` 是 `{}` 或 `{"x": 1}` 拒绝;缺证据时抛错,不写文件、不提交(测试比较 `HEAD` 并检查 `git status --porcelain` 为空)。
- **`reject` 不要求证据**(Ruling 16)。
- 本任务 `status` 不变,`confirm` 不存在;任务 97 再加。

- [ ] **Step 1: 写失败测试**

新建 `cli/test/gates.test.ts`:

```typescript
import { describe, it, expect, beforeEach } from "vitest";
import { execFileSync } from "node:child_process";
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { dump } from "js-yaml";
import { approve, reject, review } from "../src/commands.js";
import { readProduct } from "../src/gittree.js";

function initRepo(): string {
  const root = mkdtempSync(join(tmpdir(), "cp-"));
  execFileSync("git", ["-C", root, "init"]);
  execFileSync("git", ["-C", root, "config", "user.email", "t@e.com"]);
  execFileSync("git", ["-C", root, "config", "user.name", "t"]);
  return root;
}

// 路径与 Python controlplane/paths.py 一致（见计划「跨语言约定」）：
// recipe_candidate_file → control-plane/recipe_candidates/recipe-<run>.yaml
// proposal_file         → control-plane/skill_memory_proposals/<proposal-id>/proposal.yaml
function seed(root: string, rel: string, env: Record<string, unknown>): string {
  mkdirSync(dirname(join(root, rel)), { recursive: true });
  writeFileSync(join(root, rel), dump(env));
  execFileSync("git", ["-C", root, "add", "."]);
  execFileSync("git", ["-C", root, "commit", "-m", `seed ${rel}`]);
  return rel;
}

function envelope(id: string, type: string, st: string, payload: Record<string, unknown>) {
  return {
    id, type, version: 1, created_at: "2026-10-09T00:00:00Z",
    created_by: { owner: "default", visibility: "shared" },
    status: st, schema_version: 1, links: [], payload,
  };
}

const RECIPE_PAYLOAD = {
  recipe_type: "batch",
  dimension: "tom",
  source_run_id: "run-003",
  eval_anchor: {
    taxonomy_version: "tax-v1", item_pool_version: "pool-v1", benchmark_version: "bench-v1",
    judge_prompt_version: null, scoring_config_version: null,
  },
  result: { gain: 0.04, baseline_ref: "zengen-v1", candidate_ref: "cand-run-003", promoted: true },
  repro: { errors: [], warnings: ["interact runs are only highly similar"] },
  nondeterministic: false,
};

const PROPOSAL_PAYLOAD = {
  round_id: "agent-round-zengen-v1",
  finding: { id: "f-1", pattern: "emotion_high ignored", support: 6, ratio: 0.83, episode_refs: ["e1", "e2"] },
  // definition 是 actio 的 Skill.to_dict()(计划 6「对外契约:技能、记忆、推理经验的字典形状」),键顺序照抄
  increments: [{
    kind: "skill",
    definition: {
      id: "skill-calm-first", version: 1, level: "macro", priority: 0,
      trigger: { signal: "emotion_high" },
      policy: { instructions: ["先回应对方的情绪"] },
      description: "",
    },
  }],
  verification: {
    method: "replay_behavior", episodes: ["e1", "e2"], fired_on_motivating: 0.9, fired_elsewhere: 0.0,
    notes: "verified on the motivating episodes; overfit risk",
  },
};

const RECIPE_REL = "control-plane/recipe_candidates/recipe-run-003.yaml";
const PROPOSAL_ID = "proposal-agent-round-zengen-v1-1";
const PROPOSAL_REL = `control-plane/skill_memory_proposals/${PROPOSAL_ID}/proposal.yaml`;

function seedRecipe(root: string, st = "pending_review", payload: Record<string, unknown> = RECIPE_PAYLOAD) {
  return seed(root, RECIPE_REL, envelope("recipe-run-003", "recipe_candidate", st, payload));
}

function seedProposal(root: string, st = "pending_review", payload: Record<string, unknown> = PROPOSAL_PAYLOAD) {
  return seed(root, PROPOSAL_REL, envelope(PROPOSAL_ID, "skill_memory_proposal", st, payload));
}

function seedCandidate(root: string, runId: string, st: string) {
  return seed(root, `control-plane/runs/${runId}/candidate_checkpoint.yaml`,
    envelope(`cand-${runId}`, "candidate_checkpoint", st, {}));
}

function head(root: string): string {
  return execFileSync("git", ["-C", root, "rev-parse", "HEAD"], { encoding: "utf-8" }).trim();
}

function dirty(root: string): string {
  return execFileSync("git", ["-C", root, "status", "--porcelain"], { encoding: "utf-8" });
}

describe("generalized gate lookup", () => {
  let root: string;
  beforeEach(() => {
    root = initRepo();
  });

  it("finds a recipe candidate and a nested proposal by id", () => {
    seedRecipe(root);
    seedProposal(root);
    expect(review(root, "recipe-run-003")).toContain("recipe-run-003 [pending_review] type=recipe_candidate");
    expect(review(root, PROPOSAL_ID)).toContain(`${PROPOSAL_ID} [pending_review] type=skill_memory_proposal`);
  });

  it("still says not found for an unknown id", () => {
    seedRecipe(root);
    expect(() => review(root, "nope")).toThrow(/candidate nope not found/);
  });

  it("refuses an id that two products share instead of guessing", () => {
    seedRecipe(root);
    seed(root, "control-plane/runs/run-003/candidate_checkpoint.yaml",
      envelope("recipe-run-003", "candidate_checkpoint", "pending_review", {}));
    expect(() => approve(root, "recipe-run-003")).toThrow(/ambiguous/);
  });

  it("ignores untracked files, like the original lookup", () => {
    seedCandidate(root, "run-001", "pending_review");
    mkdirSync(dirname(join(root, RECIPE_REL)), { recursive: true });
    writeFileSync(join(root, RECIPE_REL), dump(envelope("recipe-run-003", "recipe_candidate", "pending_review", RECIPE_PAYLOAD)));
    expect(() => review(root, "recipe-run-003")).toThrow(/not found/);
  });
});

describe("review prints evidence per kind", () => {
  let root: string;
  beforeEach(() => {
    root = initRepo();
  });

  it("recipe candidate: anchor, result and repro", () => {
    seedRecipe(root);
    const out = review(root, "recipe-run-003");
    expect(out).toContain("recipe: type=batch dimension=tom source_run=run-003 nondeterministic=false");
    expect(out).toContain("eval_anchor: taxonomy_version=tax-v1 item_pool_version=pool-v1 benchmark_version=bench-v1 judge_prompt_version=null");
    expect(out).toContain("result: gain=0.04 baseline_ref=zengen-v1 candidate_ref=cand-run-003 promoted=true");
    expect(out).toContain("repro errors: none");
    expect(out).toContain("repro warnings (1):");
    expect(out).toContain("interact runs are only highly similar");
    expect(out).toContain(`full record: ${RECIPE_REL}`);
  });

  it("recipe candidate: a null gain is printed as null, not as unreadable", () => {
    seedRecipe(root, "pending_review", { ...RECIPE_PAYLOAD, result: { ...RECIPE_PAYLOAD.result, gain: null } });
    expect(review(root, "recipe-run-003")).toContain("result: gain=null");
  });

  it("recipe candidate: missing or malformed fields say UNREADABLE", () => {
    const { repro: _r, eval_anchor: _a, result: _res, ...rest } = RECIPE_PAYLOAD;
    seedRecipe(root, "pending_review", rest);
    const out = review(root, "recipe-run-003");
    expect(out).toContain("eval_anchor: UNREADABLE");
    expect(out).toContain("result: UNREADABLE");
    expect(out).toContain("repro: UNREADABLE");
    expect(out).not.toContain("repro errors: none");
  });

  it("recipe candidate: repro lists that are not arrays say UNREADABLE", () => {
    seedRecipe(root, "pending_review", { ...RECIPE_PAYLOAD, repro: { errors: "oops", warnings: [] } });
    const out = review(root, "recipe-run-003");
    expect(out).toContain("repro errors: UNREADABLE");
    expect(out).toContain("repro warnings: none");
  });

  it("proposal: finding, skill definitions and verification", () => {
    seedProposal(root);
    const out = review(root, PROPOSAL_ID);
    expect(out).toContain("proposal: round=agent-round-zengen-v1");
    expect(out).toContain("finding: f-1 pattern=emotion_high ignored support=6 ratio=0.83 episodes=2");
    expect(out).toContain("increments (1):");
    expect(out).toContain(
      'skill {"id":"skill-calm-first","version":1,"level":"macro","priority":0,' +
        '"trigger":{"signal":"emotion_high"},"policy":{"instructions":["先回应对方的情绪"]},"description":""}',
    );
    expect(out).toContain("verification: method=replay_behavior fired_on_motivating=0.90 fired_elsewhere=0.00 episodes=2");
    expect(out).toContain("notes: verified on the motivating episodes; overfit risk");
  });

  it("proposal: missing or malformed fields say UNREADABLE", () => {
    seedProposal(root, "pending_review", { round_id: "r", finding: "oops", increments: [{ kind: "skill" }] });
    const out = review(root, PROPOSAL_ID);
    expect(out).toContain("finding: UNREADABLE");
    expect(out).toContain("increments (1):");
    expect(out).toContain('UNREADABLE (field missing or malformed): {"kind":"skill"}');
    expect(out).toContain("verification: UNREADABLE");
  });

  it("proposal: an empty increments list says none", () => {
    seedProposal(root, "pending_review", { ...PROPOSAL_PAYLOAD, increments: [] });
    expect(review(root, PROPOSAL_ID)).toContain("increments: none");
  });

  it("candidate: a mock-backend report is flagged loudly", () => {
    seedCandidate(root, "run-001", "pending_review");
    seed(root, "control-plane/runs/run-001/diagnostic_report.yaml", envelope("diag-run-001", "diagnostic_report", "executed", {
      candidate_ref: "cand-run-001", benchmark_version: "bench-v1", run_tier: "full", sampling_k: 4, backends: "mock",
      dimension_scores: [{ dimension: "tom", level: "l1", score: 0.5, ci_low: 0.4, ci_high: 0.6, sample_size: 8, track_provenance: "static" }],
      regression_flags: { flags: [] },
    }));
    const out = review(root, "cand-run-001");
    expect(out).toContain("diagnostic: [MOCK BACKENDS] diag-run-001");
    expect(out).toContain("not model quality");
    expect(out).toContain("l1 tom score=0.50");
  });

  it("candidate: a real-backend report carries no mock marker", () => {
    seedCandidate(root, "run-001", "pending_review");
    seed(root, "control-plane/runs/run-001/diagnostic_report.yaml", envelope("diag-run-001", "diagnostic_report", "executed", {
      backends: "real", dimension_scores: [], regression_flags: { flags: [] },
    }));
    const out = review(root, "cand-run-001");
    expect(out).not.toContain("MOCK");
    expect(out).not.toContain("backends: UNREADABLE");
  });

  it("candidate: an unknown backends value says UNREADABLE", () => {
    seedCandidate(root, "run-001", "pending_review");
    seed(root, "control-plane/runs/run-001/diagnostic_report.yaml", envelope("diag-run-001", "diagnostic_report", "executed", {
      backends: "fake", dimension_scores: [], regression_flags: { flags: [] },
    }));
    expect(review(root, "cand-run-001")).toContain('backends: UNREADABLE (expected "mock" or "real", got "fake")');
  });
});

describe("approve requires per-kind evidence", () => {
  let root: string;
  beforeEach(() => {
    root = initRepo();
  });

  it("approves a recipe candidate that carries repro", () => {
    seedRecipe(root);
    approve(root, "recipe-run-003");
    expect(readProduct(root, RECIPE_REL).status).toBe("approved");
    expect(execFileSync("git", ["-C", root, "log", "-1", "--format=%s"], { encoding: "utf-8" }).trim()).toBe("approve recipe-run-003");
  });

  it("approves a proposal that carries verification", () => {
    seedProposal(root);
    approve(root, PROPOSAL_ID);
    expect(readProduct(root, PROPOSAL_REL).status).toBe("approved");
  });

  it("approves a recipe candidate whose repro has empty errors and an array of warnings", () => {
    seedRecipe(root, "pending_review", { ...RECIPE_PAYLOAD, repro: { errors: [], warnings: [] } });
    approve(root, "recipe-run-003");
    expect(readProduct(root, RECIPE_REL).status).toBe("approved");
  });

  it("approves a recipe candidate even when repro.errors is non-empty (the gate only checks presence)", () => {
    seedRecipe(root, "pending_review", { ...RECIPE_PAYLOAD, repro: { errors: ["unanchored ref"], warnings: [] } });
    approve(root, "recipe-run-003");
    expect(readProduct(root, RECIPE_REL).status).toBe("approved");
  });

  // 与 Python recipestore/admit.py has_repro_evidence 逐条一致：缺 warnings、errors 是字典、
  // repro 是 {} 或任意不带两个列表的对象，都算缺证据
  for (const repro of [undefined, null, {}, [], "ok", { x: 1 }, { errors: [] }, { errors: {}, warnings: [] },
    { errors: [], warnings: null }, { errors: [], warnings: "w" }]) {
    it(`refuses a recipe candidate whose repro is ${JSON.stringify(repro) ?? "missing"} and changes nothing`, () => {
      const { repro: _drop, ...rest } = RECIPE_PAYLOAD;
      seedRecipe(root, "pending_review", repro === undefined ? rest : { ...rest, repro });
      const before = head(root);
      expect(() => approve(root, "recipe-run-003")).toThrow(/no reproducibility check/);
      expect(readProduct(root, RECIPE_REL).status).toBe("pending_review");
      expect(head(root)).toBe(before);
      expect(dirty(root)).toBe("");
    });
  }

  for (const verification of [undefined, null, {}, [], 1]) {
    it(`refuses a proposal whose verification is ${JSON.stringify(verification) ?? "missing"} and changes nothing`, () => {
      const { verification: _drop, ...rest } = PROPOSAL_PAYLOAD;
      seedProposal(root, "pending_review", verification === undefined ? rest : { ...rest, verification });
      const before = head(root);
      expect(() => approve(root, PROPOSAL_ID)).toThrow(/no verification result/);
      expect(readProduct(root, PROPOSAL_REL).status).toBe("pending_review");
      expect(head(root)).toBe(before);
      expect(dirty(root)).toBe("");
    });
  }

  it("the transition check comes first for every kind", () => {
    seedRecipe(root, "effective");
    seedProposal(root, "rejected");
    expect(() => approve(root, "recipe-run-003")).toThrow(/not allowed/);
    expect(() => approve(root, PROPOSAL_ID)).toThrow(/not allowed/);
  });

  it("reject works for the new kinds and keeps the reason", () => {
    seedRecipe(root);
    seedProposal(root, "pending_review", { round_id: "r" });  // 驳回不需要证据
    reject(root, "recipe-run-003", "gain is noise");
    reject(root, PROPOSAL_ID, "overfit");
    expect(readProduct(root, RECIPE_REL).payload.reject_reason).toBe("gain is noise");
    expect(readProduct(root, PROPOSAL_REL).status).toBe("rejected");
  });
});
```

- [ ] **Step 2: 运行,确认失败**

Run: `cd cli && npx vitest run`
Expected: `32 failed | 21 passed (53)`。既有 18 个全过;新文件里通过的 3 个是"未知 id 报 not found"、"未跟踪文件不算"和"候选的真实后端报告不带 MOCK"(现有实现本来就满足)。

- [ ] **Step 3: 写实现**

在仓库根目录运行(差异经标准输入交给 `git apply`,同任务 90);差异应用到 `cli/src/commands.ts`,共 +181 −21 行:

```bash
git apply <<'PATCH'
--- a/cli/src/commands.ts
+++ b/cli/src/commands.ts
@@ -20,21 +20,48 @@
   }
 }
 
-function findCandidatePath(repoRoot: string, id: string): string {
-  // 用 git ls-files 定位含该 id 的候选产物，避免绑定语言 fs 遍历库
-  const listed = execFileSync(
-    "git",
-    ["-C", repoRoot, "ls-files", "control-plane/runs/*/candidate_checkpoint.yaml"],
-    { encoding: "utf-8" },
-  )
+// ---- 跨语言约定：路径模式。两种语言只经 git 树里的文件通信，所以每个模式在两侧各写一次 ----
+// candidate_checkpoint  ↔ Python controlplane/paths.py candidate_checkpoint_file
+// recipe_candidate      ↔ Python controlplane/paths.py recipe_candidate_file（daemon._read_gate_views 扫同一目录）
+// skill_memory_proposal ↔ Python controlplane/paths.py proposal_file（daemon._read_gate_views 逐个子目录读 proposal.yaml）
+export type GateKind = "candidate_checkpoint" | "recipe_candidate" | "skill_memory_proposal";
+
+const GATE_PATTERNS: ReadonlyArray<{ kind: GateKind; pattern: string }> = [
+  { kind: "candidate_checkpoint", pattern: "control-plane/runs/*/candidate_checkpoint.yaml" },
+  { kind: "recipe_candidate", pattern: "control-plane/recipe_candidates/*.yaml" },
+  { kind: "skill_memory_proposal", pattern: "control-plane/skill_memory_proposals/*/proposal.yaml" },
+];
+
+function lsFiles(repoRoot: string, pattern: string): string[] {
+  // 用 git ls-files 定位产物，避免绑定语言 fs 遍历库
+  return execFileSync("git", ["-C", repoRoot, "ls-files", pattern], { encoding: "utf-8" })
     .split("\n")
     .filter((line) => line.length > 0);
-  for (const rel of listed) {
-    if (readProduct(repoRoot, rel).id === id) {
-      return rel;
+}
+
+interface Located {
+  kind: GateKind;
+  rel: string;
+  env: Envelope;
+}
+
+function findGateProduct(repoRoot: string, id: string): Located {
+  const hits: Located[] = [];
+  for (const { kind, pattern } of GATE_PATTERNS) {
+    for (const rel of lsFiles(repoRoot, pattern)) {
+      const env = readProduct(repoRoot, rel);
+      if (env.id === id) {
+        hits.push({ kind, rel, env });
+      }
     }
   }
-  throw new Error(`candidate ${id} not found`);
+  if (hits.length === 0) {
+    throw new Error(`candidate ${id} not found`);
+  }
+  if (hits.length > 1) {
+    throw new Error(`id ${id} is ambiguous: ${hits.map((h) => h.rel).join(", ")}`);
+  }
+  return hits[0];
 }
 
 // 诊断报告与候选同在一个 run 目录，布局与 Python controlplane/paths.py 的
@@ -52,11 +79,29 @@
   return typeof n === "number" ? n.toFixed(2) : "?";
 }
 
+function isRecord(v: unknown): v is Record<string, unknown> {
+  return typeof v === "object" && v !== null && !Array.isArray(v);
+}
+
+// 审核门③"证据在场"规则 ↔ Python recipestore/admit.py 的 has_repro_evidence（计划 3 任务 45）。
+// 两侧逐条一致：repro 是对象，且 errors 与 warnings 都是数组（可为空）；errors 非空仍允许批准。
+export function hasReproEvidence(payload: Record<string, unknown>): boolean {
+  const repro = payload.repro;
+  return isRecord(repro) && Array.isArray(repro.errors) && Array.isArray(repro.warnings);
+}
+
+function isNonEmptyRecord(v: unknown): v is Record<string, unknown> {
+  return isRecord(v) && Object.keys(v).length > 0;
+}
+
+const UNREADABLE = "UNREADABLE (field missing or malformed)";
+
 function summarizeDiagnostic(report: Envelope | null, rel: string): string[] {
   if (report === null) {
     return [`diagnostic: MISSING (${rel}) — approve will refuse until a diagnostic report exists`];
   }
   const p = report.payload;
+  // stub 标记 ↔ Python simulator/stubs.py 的 diagnostic_evaluator_stub；等级判定 ↔ controlplane/evidence.py diagnostic_grade
   if (p.stub === true) {
     // 占位报告没有分数也没有退化检查：醒目标出，别让人把它当真评测
     return [
@@ -64,10 +109,19 @@
       `  candidate_ref=${String(p.candidate_ref)}`,
     ];
   }
+  // 后端标记（闭环集成设计 R17）↔ Python diagnostic/store_adapter.py 的 BACKENDS_KEY，
+  // 等级判定 ↔ controlplane/evidence.py diagnostic_grade
+  const mock = p.backends === "mock";
   const lines = [
-    `diagnostic: ${report.id} benchmark=${String(p.benchmark_version)} tier=${String(p.run_tier)} ` +
-      `k=${String(p.sampling_k)} candidate_ref=${String(p.candidate_ref)}`,
+    `diagnostic: ${mock ? "[MOCK BACKENDS] " : ""}${report.id} benchmark=${String(p.benchmark_version)} ` +
+      `tier=${String(p.run_tier)} k=${String(p.sampling_k)} candidate_ref=${String(p.candidate_ref)}`,
   ];
+  if (mock) {
+    lines.push("  [MOCK BACKENDS] scores come from mock SoMEval/judge backends — they show the pipeline ran, not model quality");
+  } else if ("backends" in p && p.backends !== "real") {
+    lines.push(`  backends: UNREADABLE (expected "mock" or "real", got ${JSON.stringify(p.backends)})`);
+  }
+  // 载荷键 ↔ Python diagnostic/datamodel.py 的 DiagnosticReport.to_dict
   const dims = Array.isArray(p.dimension_scores) ? (p.dimension_scores as Record<string, unknown>[]) : [];
   const l1 = dims.filter((d) => d.level === "l1");
   if (l1.length === 0) {
@@ -95,34 +149,140 @@
   return lines;
 }
 
+function listLines(label: string, v: unknown): string[] {
+  if (!Array.isArray(v)) {
+    return [`  ${label}: ${UNREADABLE}`];
+  }
+  if (v.length === 0) {
+    return [`  ${label}: none`];
+  }
+  return [`  ${label} (${v.length}):`, ...v.map((x) => `    ${typeof x === "string" ? x : JSON.stringify(x)}`)];
+}
+
+// 配方候选载荷键 ↔ 闭环集成设计 §4.2「配方」；Python 写方是 trainscheduler.build_recipe_candidate（计划 5），
+// 校验结果 repro 来自 recipestore.validate_reproducibility（计划 3）。
+function summarizeRecipeCandidate(env: Envelope, rel: string): string[] {
+  const p = env.payload;
+  const lines = [
+    `recipe: type=${String(p.recipe_type)} dimension=${String(p.dimension)} ` +
+      `source_run=${String(p.source_run_id)} nondeterministic=${String(p.nondeterministic)}`,
+  ];
+  const anchor = p.eval_anchor;
+  if (!isRecord(anchor)) {
+    lines.push(`  eval_anchor: ${UNREADABLE}`);
+  } else {
+    // 值为 null 是"暂无"（设计 §4.2 规定暂无的填 null），照实打印，不当成读不出来
+    const parts = Object.entries(anchor).map(([k, v]) => `${k}=${v === null ? "null" : String(v)}`);
+    lines.push(`  eval_anchor: ${parts.length > 0 ? parts.join(" ") : "(empty)"}`);
+  }
+  const result = p.result;
+  if (!isRecord(result)) {
+    lines.push(`  result: ${UNREADABLE}`);
+  } else {
+    lines.push(
+      `  result: gain=${result.gain === null ? "null" : fmt(result.gain)} baseline_ref=${String(result.baseline_ref)} ` +
+        `candidate_ref=${String(result.candidate_ref)} promoted=${String(result.promoted)}`,
+    );
+  }
+  const repro = p.repro;
+  if (!isRecord(repro)) {
+    lines.push(`  repro: ${UNREADABLE} — approve will refuse until the reproducibility check is recorded`);
+  } else {
+    lines.push(...listLines("repro errors", repro.errors), ...listLines("repro warnings", repro.warnings));
+  }
+  lines.push(`  full record: ${rel}`);
+  return lines;
+}
+
+// 技能提案载荷键 ↔ 闭环集成设计 §4.2「技能提案」；Python 写方是 mindagent.package（计划 7）。
+function summarizeProposal(env: Envelope, rel: string): string[] {
+  const p = env.payload;
+  const lines: string[] = [`proposal: round=${String(p.round_id)}`];
+  const finding = p.finding;
+  if (!isRecord(finding)) {
+    lines.push(`  finding: ${UNREADABLE}`);
+  } else {
+    const refs = finding.episode_refs;
+    lines.push(
+      `  finding: ${String(finding.id)} pattern=${String(finding.pattern)} support=${String(finding.support)} ` +
+        `ratio=${fmt(finding.ratio)} episodes=${Array.isArray(refs) ? refs.length : "UNREADABLE"}`,
+    );
+  }
+  const increments = p.increments;
+  if (!Array.isArray(increments)) {
+    lines.push(`  increments: ${UNREADABLE}`);
+  } else if (increments.length === 0) {
+    lines.push("  increments: none");
+  } else {
+    lines.push(`  increments (${increments.length}):`);
+    for (const inc of increments) {
+      if (!isRecord(inc) || !isRecord(inc.definition)) {
+        lines.push(`    ${UNREADABLE}: ${JSON.stringify(inc)}`);
+      } else {
+        lines.push(`    ${String(inc.kind)} ${JSON.stringify(inc.definition)}`);
+      }
+    }
+  }
+  const v = p.verification;
+  if (!isNonEmptyRecord(v)) {
+    lines.push(`  verification: ${UNREADABLE} — approve will refuse until the proposal is verified`);
+  } else {
+    lines.push(
+      `  verification: method=${String(v.method)} fired_on_motivating=${fmt(v.fired_on_motivating)} ` +
+        `fired_elsewhere=${fmt(v.fired_elsewhere)} episodes=${Array.isArray(v.episodes) ? v.episodes.length : "UNREADABLE"}`,
+    );
+    if (typeof v.notes === "string" && v.notes.length > 0) {
+      lines.push(`  notes: ${v.notes}`);
+    }
+  }
+  lines.push(`  full record: ${rel}`);
+  return lines;
+}
+
 export function review(repoRoot: string, id: string): string {
-  const rel = findCandidatePath(repoRoot, id);
-  const env = readProduct(repoRoot, rel);
+  const { kind, rel, env } = findGateProduct(repoRoot, id);
   const header = `${env.id} [${env.status}] type=${env.type} links=${env.links.join(",")}`;
+  if (kind === "recipe_candidate") {
+    return [header, ...summarizeRecipeCandidate(env, rel)].join("\n");
+  }
+  if (kind === "skill_memory_proposal") {
+    return [header, ...summarizeProposal(env, rel)].join("\n");
+  }
   return [header, ...summarizeDiagnostic(loadDiagnostic(repoRoot, rel), diagnosticPathFor(rel))].join("\n");
 }
 
 export function approve(repoRoot: string, id: string): void {
-  const rel = findCandidatePath(repoRoot, id);
-  const env = readProduct(repoRoot, rel);
+  const { kind, rel, env } = findGateProduct(repoRoot, id);
   assertTransition(env.status, "approved");
-  // 审核门是证据驱动的（spec：诊断报告附于 checkpoint 晋级门）：没有诊断报告就不能批准。
+  // 审核门是证据驱动的：证据不在场就不能批准，且什么都不写、不提交。
   // 这只保证证据在场，不证明审批人读过——所以 review 才会把证据直接打印出来。
-  if (loadDiagnostic(repoRoot, rel) === null) {
+  if (kind === "candidate_checkpoint" && loadDiagnostic(repoRoot, rel) === null) {
     throw new Error(
       `approve: ${id} has no diagnostic report (${diagnosticPathFor(rel)}); ` +
         `the review gate is evidence-driven — wait for the diagnostic to be produced, then run "review ${id}"`,
     );
   }
+  if (kind === "recipe_candidate" && !hasReproEvidence(env.payload)) {
+    throw new Error(
+      `approve: ${id} has no reproducibility check (payload.repro must be an object whose errors and warnings are both lists); ` +
+        `run "review ${id}" to see what the record carries`,
+    );
+  }
+  if (kind === "skill_memory_proposal" && !isNonEmptyRecord(env.payload.verification)) {
+    throw new Error(
+      `approve: ${id} has no verification result (payload.verification missing, empty or malformed); ` +
+        `run "review ${id}" to see what the proposal carries`,
+    );
+  }
   env.status = "approved";
   writeProduct(repoRoot, rel, env, `approve ${id}`);
 }
 
 export function reject(repoRoot: string, id: string, reason: string): void {
-  const rel = findCandidatePath(repoRoot, id);
-  const env = readProduct(repoRoot, rel);
+  const { rel, env } = findGateProduct(repoRoot, id);
   assertTransition(env.status, "rejected");
   env.status = "rejected";
+  // reject_reason ↔ Python 读方是计划 5 的上下文装配（PlanningContext.candidate.reject_reason）
   env.payload.reject_reason = reason;
   writeProduct(repoRoot, rel, env, `reject ${id}`);
 }
PATCH
```

- [ ] **Step 4: 运行,确认通过**

Run: `cd cli && npx tsc --noEmit && npx vitest run`
Expected: `tsc` 无输出;`Tests  53 passed (53)`。

- [ ] **Step 5: 全量回归**

Python 侧没有改动,不必重跑。命令行:Step 4 已是全量。`git status --short` 除原有 `cli/package*.json` 与未跟踪的文档外只应有 `cli/src/commands.ts` 和 `cli/test/gates.test.ts`(补丁走标准输入,没有补丁文件)。

- [ ] **Step 6: 提交**

```bash
git add cli/src/commands.ts
git add cli/test/gates.test.ts
git commit -m "feat: cli reviews and approves recipe candidates and skill proposals with per-kind evidence rules"
```

---

### Task 97: `confirm`、新的 `status` 与入口分发

**Files:**
- Modify: `cli/src/commands.ts`(303 → 347 行)、`cli/src/index.ts`(31 → 37 行)(合计 +63 −13)
- Test: `cli/test/gates.test.ts`(修改,本任务 +11 个用例,文件共 46 个)

**Interfaces:**
- Consumes: 计划 1 的非审核类状态 `awaiting_confirmation`(TS 侧在 `NONREVIEW_TRANSITIONS` 里抄一份);指令路径约定。
- Produces: `export function confirm(repoRoot: string, id: string): void`;新的 `status(repoRoot): string`;`export const USAGE`;`main` 分发 `confirm`。

**设计要点:**
- **`confirm` 只认 `awaiting_confirmation`**(Ruling 17),用 `NONREVIEW_TRANSITIONS` 判定;其余状态抛错且不提交(测试比较 `HEAD`)。指令 id 就是 run 目录名(既有约定),在 `control-plane/runs/*/directive.yaml` 里按信封 `id` 找;与 `findGateProduct` 一样收集全部命中,多处命中报 `directive <id> is ambiguous`,不写文件、不提交(Ruling 24,有测试)。
- **`status` 分两节**(Ruling 18),每行带产物种类;两节都空时打印 `nothing awaiting review or confirmation`。
- **`USAGE` 导出成常量**,用例直接断言其文本;`main` 的 `confirm` 分支用 `vi.spyOn(process, "cwd")` 测(vitest 的 worker 里不能 `chdir`)。

- [ ] **Step 1: 写失败测试**

在仓库根目录运行(差异经标准输入交给 `git apply`,同任务 90);差异应用到 `cli/test/gates.test.ts`,+102 −2 行,新增 `confirm`(含多处命中报歧义)、`status`、`index dispatch` 三组用例:

```bash
git apply <<'PATCH'
--- a/cli/test/gates.test.ts
+++ b/cli/test/gates.test.ts
@@ -1,11 +1,12 @@
-import { describe, it, expect, beforeEach } from "vitest";
+import { describe, it, expect, beforeEach, vi } from "vitest";
 import { execFileSync } from "node:child_process";
 import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
 import { tmpdir } from "node:os";
 import { dirname, join } from "node:path";
 import { dump } from "js-yaml";
-import { approve, reject, review } from "../src/commands.js";
+import { approve, confirm, reject, review, status } from "../src/commands.js";
 import { readProduct } from "../src/gittree.js";
+import { USAGE, main } from "../src/index.js";
 
 function initRepo(): string {
   const root = mkdtempSync(join(tmpdir(), "cp-"));
@@ -78,6 +79,11 @@
   return seed(root, PROPOSAL_REL, envelope(PROPOSAL_ID, "skill_memory_proposal", st, payload));
 }
 
+function seedDirective(root: string, runId: string, st: string) {
+  return seed(root, `control-plane/runs/${runId}/directive.yaml`,
+    envelope(runId, "directive", st, { target_dimension: "tom", recipe_ref: "" }));
+}
+
 function seedCandidate(root: string, runId: string, st: string) {
   return seed(root, `control-plane/runs/${runId}/candidate_checkpoint.yaml`,
     envelope(`cand-${runId}`, "candidate_checkpoint", st, {}));
@@ -298,3 +304,97 @@
     expect(readProduct(root, PROPOSAL_REL).status).toBe("rejected");
   });
 });
+
+describe("confirm", () => {
+  let root: string;
+  beforeEach(() => {
+    root = initRepo();
+  });
+
+  it("moves awaiting_confirmation to pending and commits", () => {
+    const rel = seedDirective(root, "run-004", "awaiting_confirmation");
+    confirm(root, "run-004");
+    const env = readProduct(root, rel);
+    expect(env.status).toBe("pending");
+    expect(env.payload.target_dimension).toBe("tom");
+    expect(execFileSync("git", ["-C", root, "log", "-1", "--format=%s"], { encoding: "utf-8" }).trim()).toBe("confirm run-004");
+  });
+
+  for (const st of ["pending", "executed", "failed", "consumed"]) {
+    it(`refuses a ${st} directive and changes nothing`, () => {
+      const rel = seedDirective(root, "run-004", st);
+      const before = head(root);
+      expect(() => confirm(root, "run-004")).toThrow(/only an awaiting_confirmation directive/);
+      expect(readProduct(root, rel).status).toBe(st);
+      expect(head(root)).toBe(before);
+    });
+  }
+
+  it("says not found for an unknown directive", () => {
+    seedDirective(root, "run-004", "awaiting_confirmation");
+    expect(() => confirm(root, "run-999")).toThrow(/directive run-999 not found/);
+  });
+
+  it("reports ambiguity instead of confirming the first of several matches", () => {
+    seedDirective(root, "run-004", "awaiting_confirmation");
+    // 同一信封 id 出现在第二个 run 目录(人手复制目录时会这样)
+    const other = seed(root, "control-plane/runs/run-005/directive.yaml",
+      envelope("run-004", "directive", "awaiting_confirmation", { target_dimension: "tom", recipe_ref: "" }));
+    const before = head(root);
+    expect(() => confirm(root, "run-004")).toThrow(/directive run-004 is ambiguous/);
+    expect(readProduct(root, other).status).toBe("awaiting_confirmation");
+    expect(head(root)).toBe(before);
+  });
+});
+
+describe("status", () => {
+  let root: string;
+  beforeEach(() => {
+    root = initRepo();
+  });
+
+  it("lists all three gate kinds awaiting review and directives awaiting confirmation", () => {
+    seedCandidate(root, "run-001", "pending_review");
+    seedCandidate(root, "run-002", "effective");
+    seedRecipe(root);
+    seedProposal(root);
+    seedDirective(root, "run-004", "awaiting_confirmation");
+    seedDirective(root, "run-005", "pending");
+    expect(status(root)).toBe([
+      "awaiting review:",
+      "  cand-run-001 [pending_review] candidate_checkpoint",
+      "  recipe-run-003 [pending_review] recipe_candidate",
+      `  ${PROPOSAL_ID} [pending_review] skill_memory_proposal`,
+      "awaiting confirmation (run: confirm <directive-id>):",
+      "  run-004 [awaiting_confirmation] directive",
+    ].join("\n"));
+  });
+
+  it("says so when nothing waits", () => {
+    seedCandidate(root, "run-001", "effective");
+    seedDirective(root, "run-001", "executed");
+    expect(status(root)).toBe("nothing awaiting review or confirmation");
+  });
+});
+
+describe("index dispatch", () => {
+  it("usage names every command", () => {
+    expect(USAGE).toBe("usage: rismforge <review|approve|reject|confirm|status> [id] [reason]");
+  });
+
+  it("confirm is routed through main", () => {
+    const root = initRepo();
+    const rel = seedDirective(root, "run-004", "awaiting_confirmation");
+    // vitest 的 worker 里不能 chdir;main 只经 process.cwd() 取仓库根,桩掉它即可
+    const cwd = vi.spyOn(process, "cwd").mockReturnValue(root);
+    const log = vi.spyOn(console, "log").mockImplementation(() => undefined);
+    try {
+      main(["confirm", "run-004"]);
+      expect(log).toHaveBeenCalledWith("confirmed run-004");
+    } finally {
+      cwd.mockRestore();
+      log.mockRestore();
+    }
+    expect(readProduct(root, rel).status).toBe("pending");
+  });
+});
PATCH
```

- [ ] **Step 2: 运行,确认失败**

Run: `cd cli && npx vitest run`
Expected: `11 failed | 53 passed (64)`。失败的是新增的 11 个:`confirm` 尚未导出,调用处报 `TypeError: confirm is not a function`,或断言抛出的错误信息对不上;`USAGE` 是 `undefined`;`status` 仍返回旧格式 `no candidates awaiting review`;`main` 走到默认分支,没有打印 `confirmed run-004`。

- [ ] **Step 3: 写实现**

在仓库根目录运行(差异经标准输入交给 `git apply`,同任务 90);差异应用到 `cli/src/commands.ts` 与 `cli/src/index.ts`,共 +63 −13 行:

```bash
git apply <<'PATCH'
--- a/cli/src/commands.ts
+++ b/cli/src/commands.ts
@@ -14,6 +14,16 @@
   deprecated: [],
 };
 
+// 非审核类合法迁移，与 Python controlplane/states.py 的 NONREVIEW_TRANSITIONS 保持一致
+// （awaiting_confirmation 由闭环集成设计 §3.4 引入）。命令行只用到 confirm 这一条。
+const NONREVIEW_TRANSITIONS: Record<string, string[]> = {
+  pending: ["executed", "failed"],
+  awaiting_confirmation: ["pending", "failed"],
+  executed: ["consumed"],
+  consumed: [],
+  failed: [],
+};
+
 function assertTransition(from: string, to: string): void {
   if (!(REVIEW_TRANSITIONS[from] ?? []).includes(to)) {
     throw new Error(`review: ${from} -> ${to} not allowed`);
@@ -32,6 +42,9 @@
   { kind: "skill_memory_proposal", pattern: "control-plane/skill_memory_proposals/*/proposal.yaml" },
 ];
 
+// 指令 ↔ Python controlplane/daemon.py read_tree_state 读的 f"{run_dir(run_id)}/directive.yaml"
+const DIRECTIVE_PATTERN = "control-plane/runs/*/directive.yaml";
+
 function lsFiles(repoRoot: string, pattern: string): string[] {
   // 用 git ls-files 定位产物，避免绑定语言 fs 遍历库
   return execFileSync("git", ["-C", repoRoot, "ls-files", pattern], { encoding: "utf-8" })
@@ -287,17 +300,48 @@
   writeProduct(repoRoot, rel, env, `reject ${id}`);
 }
 
+export function confirm(repoRoot: string, id: string): void {
+  // 闭环集成设计 §3.4：调度器写的指令先是 awaiting_confirmation，人确认后才变 pending、被派发
+  // 与 findGateProduct 同一规则：收集全部命中，多处命中报歧义，不取第一个
+  const hits = lsFiles(repoRoot, DIRECTIVE_PATTERN).filter((r) => readProduct(repoRoot, r).id === id);
+  if (hits.length === 0) {
+    throw new Error(`directive ${id} not found`);
+  }
+  if (hits.length > 1) {
+    throw new Error(`directive ${id} is ambiguous: ${hits.join(", ")}`);
+  }
+  const rel = hits[0];
+  const env = readProduct(repoRoot, rel);
+  if (!(NONREVIEW_TRANSITIONS[env.status] ?? []).includes("pending")) {
+    throw new Error(`confirm: ${id} is ${env.status}; only an awaiting_confirmation directive can be confirmed`);
+  }
+  env.status = "pending";
+  writeProduct(repoRoot, rel, env, `confirm ${id}`);
+}
+
 export function status(repoRoot: string): string {
-  const listed = execFileSync(
-    "git",
-    ["-C", repoRoot, "ls-files", "control-plane/runs/*/candidate_checkpoint.yaml"],
-    { encoding: "utf-8" },
-  )
-    .split("\n")
-    .filter((line) => line.length > 0);
-  const lines = listed
+  const review: string[] = [];
+  for (const { kind, pattern } of GATE_PATTERNS) {
+    for (const rel of lsFiles(repoRoot, pattern)) {
+      const env = readProduct(repoRoot, rel);
+      if (env.status === "pending_review") {
+        review.push(`  ${env.id} [${env.status}] ${kind}`);
+      }
+    }
+  }
+  const confirmation = lsFiles(repoRoot, DIRECTIVE_PATTERN)
     .map((rel) => readProduct(repoRoot, rel))
-    .filter((env) => env.status === "pending_review")
-    .map((env) => `${env.id} [${env.status}]`);
-  return lines.length > 0 ? lines.join("\n") : "no candidates awaiting review";
+    .filter((env) => env.status === "awaiting_confirmation")
+    .map((env) => `  ${env.id} [${env.status}] directive`);
+  if (review.length === 0 && confirmation.length === 0) {
+    return "nothing awaiting review or confirmation";
+  }
+  const lines: string[] = [];
+  if (review.length > 0) {
+    lines.push("awaiting review:", ...review);
+  }
+  if (confirmation.length > 0) {
+    lines.push("awaiting confirmation (run: confirm <directive-id>):", ...confirmation);
+  }
+  return lines.join("\n");
 }
--- a/cli/src/index.ts
+++ b/cli/src/index.ts
@@ -1,7 +1,9 @@
-import { review, approve, reject, status } from "./commands.js";
+import { review, approve, reject, confirm, status } from "./commands.js";
 
 export const CLI_NAME = "rismforge";
 
+export const USAGE = `usage: ${CLI_NAME} <review|approve|reject|confirm|status> [id] [reason]`;
+
 export function main(argv: string[]): void {
   const [cmd, ...rest] = argv;
   const repoRoot = process.cwd();
@@ -17,11 +19,15 @@
       reject(repoRoot, rest[0], rest.slice(1).join(" ") || "no reason");
       console.log(`rejected ${rest[0]}`);
       break;
+    case "confirm":
+      confirm(repoRoot, rest[0]);
+      console.log(`confirmed ${rest[0]}`);
+      break;
     case "status":
       console.log(status(repoRoot));
       break;
     default:
-      console.error(`usage: ${CLI_NAME} <review|approve|reject|status> [id] [reason]`);
+      console.error(USAGE);
       process.exitCode = 1;
   }
 }
PATCH
```

- [ ] **Step 4: 运行,确认通过**

Run: `cd cli && npx tsc --noEmit && npx vitest run`
Expected: `tsc` 无输出;`Tests  64 passed (64)`(既有 18 个,`gates.test.ts` 46 个)。

- [ ] **Step 5: 提交**

```bash
git add cli/src/commands.ts
git add cli/src/index.ts
git add cli/test/gates.test.ts
git commit -m "feat: cli confirm for awaiting_confirmation directives; status lists every gate kind and unconfirmed directives"
```

---

### Task 98: 全量回归与改动范围检查

**Files:** 无改动。

- [ ] **Step 1: Python 全量**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: `B+160 passed`(820 起点实测 `980 passed`),无警告。

- [ ] **Step 2: 命令行全量与类型检查**

Run: `cd cli && npx tsc --noEmit && npx vitest run`
Expected: `tsc` 无输出;`Test Files  3 passed (3)`、`Tests  64 passed (64)`。

- [ ] **Step 3: 改动范围**

Run: `git diff --stat "$BASE"..HEAD`(`BASE` 是任务 90 Step 1 记下的提交号)。若执行中途换了 shell、`BASE` 丢失,先从执行记录里取回;记录也没有时退路是 `git diff --stat HEAD~8..HEAD`(本计划恰好 8 个提交,前提是期间没有别的提交混进来)。

Expected: 恰好 14 个文件,都在 File Structure 表里(撰写时在拷贝里实测的输出;R19 与审计修订后各文件的数字重新实测,`+` 号条的长度未重画):

```
 cli/src/commands.ts                    | 268 +++++++++++++++++++---
 cli/src/index.ts                       |  10 +-
 cli/test/gates.test.ts                 | 400 ++++++++++++++++++++++++++++++++
 src/controlplane/daemon.py             | 203 +++++++++++++-
 src/controlplane/reconcile.py          | 141 ++++++++++
 src/diagnostic/assets.py               |  74 +++++++
 src/diagnostic/evaluator.py            | 111 +++++++++
 src/diagnostic/orchestrator.py         |   8 +-
 src/diagnostic/store_adapter.py        |  18 +-
 tests/test_daemon_closed_loop_seams.py | 217 +++++++++++++++++++
 tests/test_daemon_closed_loop_views.py | 221 +++++++++++++++
 tests/test_diag_assets.py              | 145 +++++++++++++
 tests/test_diag_evaluator.py           | 199 +++++++++++++++
 tests/test_reconcile_closed_loop.py    | 432 +++++++++++++++++++++++++++++++++
 14 files changed, 2398 insertions(+), 49 deletions(-)
```

**不得**出现 `cli/package.json`、`cli/package-lock.json`、`src/controlplane/paths.py`、`states.py`、`evidence.py`、`pyproject.toml`、`schemas/`、任何既有测试文件。

- [ ] **Step 4: 依赖方向**

Run: `grep -rnE "^(from|import) (diagnostic|mindtrainer|recipestore|trainplanner|trainscheduler|mindagent|actio|inference_bridge|factory)" src/controlplane; grep -rnE "^(from|import) mindtrainer" src/diagnostic`
Expected: 无输出。

- [ ] **Step 5: `from __future__` 检查**

Run: `for f in src/diagnostic/assets.py src/diagnostic/evaluator.py; do python3 -c "import ast,sys; t=ast.parse(open('$f').read()); b=t.body; i=1 if isinstance(b[0],ast.Expr) and isinstance(b[0].value,ast.Constant) else 0; assert isinstance(b[i],ast.ImportFrom) and b[i].module=='__future__', '$f'; print('$f ok')"; done`
Expected: 两行 `... ok`。

本任务不提交。

## 完成标准

- Python 全量 `B+160 passed`(820 起点实测 `980 passed`),无警告;命令行 `64 passed`,`tsc --noEmit` 无输出。
- `tests/test_reconcile.py`、`tests/test_daemon.py`、`tests/test_daemon_dispatcher.py`、`tests/test_e2e_loop.py`、`tests/test_diag_*.py`(既有 15 个文件)、`cli/test/commands.test.ts` 一行未改。
- 不注入任何东西时,从指令到晋级的整棵 `control-plane/` 与 `git log` 与改动前相同(`test_explicit_empty_injection_leaves_tree_log_and_actions_identical`)。
- 六个动作各有触发与不触发的纯函数测试;"决策 pending、指令已写"的重入行为有测试钉住;`start_agent_round` 对 `pending` 轮次再次触发、对 `executed` / `failed` 不触发(R19),`ModelView.agent_round_status` 读的是轮次产物的信封状态,读不出的轮次产物视为没有轮次;`plan_next_round` 按 `run-<数字>` 的数值序选最大 run(`run-9` 与 `run-011`、`scratch` 目录、没有任何编号 run 三种情形有测试,外加同编号 `run-7` / `run-007` 与输入顺序无关);`resolve_baseline` 对没有分数或证据等级不同的基线返回无基线;非 dict 的诊断载荷记为 `stub`;`confirm` 对多处命中报歧义。
- `make_diagnostic_evaluator` 写出的报告 `diagnostic_grade == "mock"`;既有桩报告仍是 `stub`。
- `git diff --stat` 只涉及 File Structure 表里的 14 个文件;工作区里没有遗留的补丁文件。

## 已知限制

1. **处理器与评估器的瞬态异常会让守护进程退出。** 与派发器相同:`run_daemon` 不捕获。需要外部进程管理,或在计划 9 的组合根里加重启。
2. **处理器拿到的是 tick 开头的快照。** 同一 tick 里先执行的动作(例如收录)改了树,后面的动作对应的 `Action` 不会更新;处理器必须自己重读。规划处理器若在同一 tick 里紧跟在晋级之后执行,看到的是晋级之后的树,这是期望的。
3. **评估器同步运行,只跑静态轨。** 动态轨需要情境、绑定与模型后端,V1 没有来源;真实 SoMEval 与真实 judge 也不在本期(设计 §15)。
4. **基线可比性只比较 `benchmark_version`。** 报告载荷里没有 `item_pool_version`,换题库必须同时换 `benchmark_version`,否则会跨题库比较;换了 `benchmark_version` 后第一轮总是"无基线",不会产生退化标记。基线报告的证据等级与本次 `backends` 不同(`real` 对 `mock`)时同样无基线(Ruling 22)。
5. **`approve` 只保证证据在场,不证明有人读过,也不检查 `repro.errors` 是否为空。** 这是设计 §6.1 的取舍。
6. **`git ls-files` 的 `*` 跨越 `/`。** `control-plane/recipe_candidates/*.yaml` 会匹配 `recipe_candidates/<子目录>/x.yaml`;`control-plane/runs/*/directive.yaml` 会匹配 `runs/<run>/episodes/directive.yaml` 这类更深的路径(实测确认)。命令行按信封 id 匹配并在多处命中时报歧义,所以误匹配只会变成报错,不会改错文件;Python 一侧用 `list_products` 与逐层遍历,不受影响。两侧的扫描范围因此不完全相同。
7. **`tsconfig.json` 的 `include` 只有 `src`。** `npx tsc --noEmit` 不检查 `cli/test/*.ts` 的类型;测试文件由 vitest 转译执行,类型错误不会被发现。既有配置,本计划不改。
8. **`status` 的输出格式变了。** 旧格式 `no candidates awaiting review` 不再出现。
9. **配方候选与提案的载荷形状由计划 5、7 写入。** 本计划按设计 §4.2 读;若那两份计划改了键名,`review` 会打印 `UNREADABLE`,`approve` 会拒绝,不会静默放行。
10. **"模型 → 产出 run"有两种判法。** 本计划的 `_read_model_views` 与 `resolve_baseline` 取 `links[1]`;计划 3 的 `recipestore.gain.baseline_run_of` 取"`cand-<x>` 也在 `links` 里的那一项"。两种判法目前一致(`promote_candidate` 写的是 `[cand-<run>, <run>]`),计划 9 的一致性核对会加测试钉住;改晋级写法时三处要一起改。
11. **同编号的 run 只按 run id 字典序平局。** `run-7` 与 `run-007` 都解析为编号 7;`_latest_numbered_run` 取 `(编号, run_id)` 最大的那个(这里是 `run-7`),结果确定、与输入顺序无关,但不代表哪一个"更新"。这种命名只会来自手工,计划 5 分配的 run id 不会撞号。

## 未验证项

1. **计划 1 的真实产物。** 验证时计划 1 尚未落地,用的是临时桩(任务 90 Step 1 所列)。`evidence.py` 的桩与计划 1 文档任务 24 的代码块逐字相同;六个路径函数的返回值与计划 1 文档任务 19 逐一核对过;`awaiting_confirmation` 那一行与计划 1 任务 20 相同。**绝对通过数依赖执行顺序**(哪些计划先落地),本计划只保证增量:820 起点下的绝对数是实测,B 起点下的绝对数没有实跑。
2. **与计划 1 完整改动的叠加运行没有做成。** 原打算把本计划的改动叠到计划 1 撰写者的临时拷贝上跑一次全量,但那份拷贝在撰写期间已被删除。计划 1 文档声明它不改 `reconcile.py`、`daemon.py`、`cli/`,`diagnostic` 也不在它的文件表里,所以预期没有文本冲突;语义上唯一的交点是 `evidence.py`,已逐字比对。
3. **计划 3、5、7 的真实处理器。** 本计划所有处理器都是假的;处理器是否真的写下了幂等键、是否遵守重入契约,要到计划 9 的端到端才能验证。
4. **真实 SoMEval、真实 judge 与 `backends: "real"`。** 只用 `MockSoMEvalRunner` 与 `MockJudgeBackend` 跑过;`backends="real"` 只验证了标记与等级,没有真实后端。
5. **命令行的进程级行为。** `main` 只在 vitest 里用桩掉的 `process.cwd()` 调过;没有构建 `dist/` 并以 `node dist/index.js confirm ...` 的形式实跑,也没有验证退出码。
6. **`git apply` 的补丁**:每份 `git apply <<'PATCH'` 块里的差异都在一份干净拷贝上按任务 90→97 依次应用过,应用后的树与验证用的树逐字节相同(`git diff` 为空)。补丁以 `a/`、`b/` 前缀生成,需在仓库根目录执行;差异从标准输入读,不在工作区留下补丁文件。

## 不在本计划内的事

- 六个动作的处理器:收录与战绩(计划 3)、规划与提名(计划 5)、智能体轮次与提案收录(计划 7)。
- `factory` 组合根、`--enable` 等开关、只读子命令 `trace/cost/logs`、`reconcile --once`、全闭环与 MindAgent 端到端、跨计划一致性核对:都是计划 9。
- 题库与分类表的初始内容(`install_default_assets` 之类):本计划只给读写函数,由计划 9 在组合根里写入。
- `request_diagnostic` 的默认实现仍是桩;切到真实评估器由组合根注入。
