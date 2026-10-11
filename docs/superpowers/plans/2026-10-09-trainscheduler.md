# TrainScheduler:规划上下文装配、领域校验、动作解析、指令构造、配方提名与两个处理器 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增顶层包 `trainscheduler`,把"一轮训练结束 → 下一条训练指令"与"一轮训练成功 → 配方候选"两件事做成可注入调和循环的处理器。规划处理器从树里装配 `PlanningContext`,交给 TrainPlanner 拿回决策(领域违规带着原因重采样),先写 `planner_decision`(pending,带 `next_run_id`),再写下一条指令(默认 `awaiting_confirmation`),最后把决策置 `executed`;崩在任何一步之后重跑都收敛。提名处理器按证据等级、增益与退化标记判定,写 `nomination_note`,合格时写 `recipe_candidate`(pending_review)。两份配置 `scheduler.yaml`、`training_defaults.yaml` 是信封,严格解析,带安装函数。全部测试脱离外部服务。

**Architecture:** 设计 §7.2 的八个名字分在七个模块里:`config`(两份配置的字段表、严格解析、安装)、`run_ids`(run id 分配)、`assemble`(`assemble_context`)、`resolve`(`validate_decision`、`resolve_action`、`ResolvedAction`)、`directive`(`construct_directive`)、`plan_handler`(`make_plan_handler`)、`nominate`(`nomination_check`、`build_recipe_candidate`、`make_nominate_handler`)。对 RecipeStore 的全部导入集中在 `recipes_port` 一个文件里。依赖方向:`trainscheduler → controlplane, recipestore, trainplanner, inference_bridge(只取 BridgeError), mindtrainer.training_config, mindtrainer.data_spec`;反方向一条都没有(任务 67 有测试)。处理器签名是字面的 `Callable[[GitStore, Action], None]`,可以脱离调和循环直接调用。

**Tech Stack:** Python 3.11+ / uv / pytest;**无新增第三方依赖**(只用标准库 `copy`、`dataclasses`、`math`、`re`、`collections.abc`)。

**Spec:**
- `docs/superpowers/specs/2026-10-09-closed-loop-integration-design.md` §7.2(本计划的范围)、§6.3 的提名部分、§9 调度器一侧(`data_spec` 与 `stage_manifest_refs` 的写法)、§3.2 与 §4.1 的重入规则、§4.2 的载荷、§11 的契约、§14 第 7 条、§15 最后一条
- 计划顺序:设计 §12 第 5 份,任务号 60 起。前置:**计划 1、2、3、4** 全部已合并
- 裁决 R1–R21 照用,不推翻

**依赖已有产出的名字**

| 来源 | 名字 | 状态 |
|---|---|---|
| `controlplane.envelope` / `gitstore` / `reconcile` | `Envelope`、`OwnerRef`、`CURRENT_SCHEMA_VERSION` / `GitStore` / `Action(kind, target_id, detail)` | 已有 |
| `controlplane.models` | `latest_promoted_checkpoint(store) -> PromotedBase \| None`(`model_id`、`version`、`checkpoint_ref: BigRef`)、`MissingCheckpointRefError` | 已有 |
| `controlplane.states` | `NONREVIEW`、`assert_transition`、`can_transition`;非审核类 `awaiting_confirmation: {pending, failed}` | 计划 1 任务 20 |
| `controlplane.paths` | `run_dir`、`candidate_checkpoint_file`、`dataset_manifest_file`、`model_file`(已有);`planner_decision_file`、`nomination_file`、`recipe_candidate_file`、`run_trace_file`、`failure_record_file`、`scheduler_config_file`、`training_defaults_file`、`recipe_file`、`recipe_track_file` | 计划 1 任务 19 |
| `controlplane.config` | `read_config(store, rel_path) -> dict`、`write_config(store, rel_path, config_id, payload, message)` | 计划 1 任务 23 |
| `controlplane.evidence` | `diagnostic_grade(payload) -> "stub"\|"mock"\|"real"`、`evidence_acceptable(grade, accept_mock) -> bool` | 计划 1 任务 24 |
| `controlplane.ledger` | `record_failure(store, run_id, *, task_id, category, point, retries, reason, log_ref=None) -> str` | 计划 1 任务 21 |
| `inference_bridge` | `BridgeError(category, reason="", *, attempts=0, log_ref=None)`,`category` 是 `"T1"` / `"T2"` | 计划 1 任务 25 |
| `mindtrainer.training_config` | `TrainingConfig`(`from_dict`、`to_dict`)、`validate_training_config`、`load_config_from_directive`、`InvalidTrainingConfigError` | 已有 |
| `mindtrainer.data_spec` | `DataSpec`(`to_dict`)、`parse_data_spec`、`InvalidDataSpecError`、`required_stages(cfg)`、`check_data_spec_against_config(spec, cfg, stages)`、`manifest_data_run(ref, stage)`、`data_spec_from_directive`、`stages_to_synthesize` | 计划 2 任务 30、31 |
| `mindtrainer.data_producer` / `mindtrainer.dispatch` | `TemplateDataProducer` / `make_mindtrainer_dispatcher(..., data_producer=None)`(**只在测试里用**) | 计划 2 任务 32、33 |
| `recipestore` | 见下一节"对外契约:对计划 3 的假设" | 计划 3 |
| `trainplanner.context` | `PlanningContext`、`CandidateSummary`、`DiagnosticSummary`、`RegressionMark`、`ErrorProfileSummary`、`RecipeSummary`、`DecisionOutcome`、`CostGainEntry`(`gpu_hours`、`tokens` 可为 `None`)、`InvalidContextError`、`CANDIDATE_TERMINAL_STATES`、`RECIPE_TYPES` | 计划 4 任务 50 |
| `trainplanner.examples` | `example_context_dict()`(只在测试里用) | 计划 4 任务 50 |
| `trainplanner.decision` | `PlannerDecision`(`from_dict`、`decision_payload`) | 计划 4 任务 51 |
| `trainplanner.templates` | `PromptTemplate(version, text)`、`load_template(store, version)`、`install_default_template(store)`、`PLANNER_V1_VERSION = "v1"` | 计划 4 任务 53 |
| `trainplanner.backends` | `DecisionBackend`(`name`、`decide`)、`HeuristicBackend`、`ScriptedDecisionBackend(responses)`(元素是异常实例时抛出) | 计划 4 任务 54 |
| `trainplanner.planner` | `plan(ctx, template_text, template_version, backend, *, run_id, max_parse_retries=2, validator=None) -> PlannerResult`(`record_fields()`)、`PlannerFailedError(reason, attempts, *, reasons)` | 计划 4 任务 55 |

## 对外契约:对计划 3 的假设

本计划对 RecipeStore 的全部导入都写在 `src/trainscheduler/recipes_port.py` 一个文件里(任务 67 有测试保证别的模块不直接导入 `recipestore`),计划 3 的导入路径若有出入只改这一个文件。逐条如下,每个任务第 1 步的前置检查都会断言它们可导入:

| 导入路径 | 本计划依赖的行为 |
|---|---|
| `recipestore.gain.compute_gain(store, run_id, dimension) -> GainResult` | `GainResult(gain: float \| None, baseline_run_id: str \| None, reason: str)`;缺报告、桩报告、没有基线、基准版本不同都返回 `gain=None` 并在 `reason` 写明 |
| `recipestore.gain.GainResult` | 同上 |
| `recipestore.gain.read_report_payload(store, run_id) -> dict \| None` | 报告文件不在返回 `None` |
| `recipestore.gain.regression_flags(payload) -> list[dict]` | 报告 `regression_flags.flags` 里的字典;桩报告返回 `[]` |
| `recipestore.ids.recipe_id_for(run_id) -> "recipe-<run>"`、`recipestore.ids.run_id_of(recipe_id)` | 形状不对抛 `ValueError` |
| `recipestore.library.find_recipes(store, *, dimension=None, recipe_type=None, states=("effective",), order="track") -> list[RecipeView]` | 按 `mean_gain` 降序(`None` 最后)、`n_reuse` 降序、id 升序 |
| `recipestore.library.RecipeView` | 字段 `id, recipe_type, dimension, status, payload, mean_gain, n_reuse` |
| `recipestore.library.get_recipe(store, recipe_id) -> Envelope` | 不在或 id 形状不对抛 `RecipeNotFoundError` |
| `recipestore.library.RecipeNotFoundError` | `LookupError` 子类 |
| `recipestore.validate.validate_reproducibility(payload) -> ReproReport` | `ReproReport.to_dict() == {"errors": [...], "warnings": [...]}`;纯函数 |
| `recipestore.validate.ReproReport` | 同上 |
| `recipestore.admit.handle_admit_recipe`、`recipestore.admit.has_repro_evidence`(**只在测试里用**) | 收录 approved 的候选;"证据在场"规则 |

测试里手工构造配方时按计划 3 收录的写法:`recipes/recipe-<run>/recipe.yaml`(type `recipe`)加 `track_record.yaml`(type `recipe_track_record`,载荷 `{recipe_id, entries: [...]}`)。

**核对结果:** 撰写时 `docs/superpowers/plans/2026-10-09-recipestore.md` 已在树里。上表每一条都与它的任务 41–46 的 Interfaces 与代码块逐字一致(`get_recipe` 在 `recipestore.library`,不在 `recipestore` 包根;`RecipeNotFoundError` 是 `LookupError` 子类;`read_report_payload`、`regression_flags` 是任务 42 列出的"计划 5 可复用"辅助)。临时拷贝里用的就是从该计划代码块原样抽出的 `recipestore`,不是桩。

## 本计划的取舍(要改的话改这里,任务按此排)

1. **处理器只管自己的产物。** 规划处理器写 `planner_decision`、下一条 `directive`、失败记录;提名处理器写 `nomination_note` 与 `recipe_candidate`。触发条件(候选终态、证据可接受、只对最大 run、没有在途轮)是计划 8 `reconcile` 的事,处理器不重复判断,只对自己幂等键之外的"过早"情形给出确定性失败或 skip。
2. **确定性失败与瞬态失败的分界照设计 §3.3 与 R18。** 确定性(决策 `failed` + T3 失败记录,不上抛):规划耗尽、上下文装配不出来(含 stub 证据)、解析与构造失败、**任何 `BridgeError`(T1 与 T2 都算)**。瞬态与"人改一下就好"的(原样上抛,决策保持原状):非桥接异常(`OSError`、子进程错误等)、配置写错、模板缺失。
3. **指令写出之前一定过 `validate_training_config` 与 `data_spec` 交叉校验**,不让坏指令进树。
4. **只构造 batch 与 interact 指令,只提名 batch 与 interact run**;`new` 只构造 batch(Ruling 3)。

## 已核实的事实(实现者不要再猜)

- 计划 1 的 `latest_promoted_checkpoint` 按版本号**数值**取最大的 `effective` 模型;最新模型缺 `checkpoint_ref` 抛 `MissingCheckpointRefError`,不回退。
- 计划 2 的 `required_stages(cfg)`:配了 `sft_steps` 要 `sft`;`batch` 且配了 `grpo_steps` 要 `grpo_opd`。它直接比较 `cfg.sft_steps > 0`,类型不对会抛 `TypeError`,所以默认配置要先过 `validate_training_config` 再调它(任务 60 有测试)。
- 计划 2 的派发器在指令带 `data_spec`、清单缺失、清单 id 的数据所在 run 就是本 run 时才合成。`clone` 的指令点名的是配方来源 run 的清单,`stages_to_synthesize` 返回空,不会再合成(任务 67 端到端证明)。
- 计划 2 的 `check_data_spec_against_config` 要求 `scenario_count >= groups_per_step`、`grpo_opd` 的 `group_size >= min_group_size`。默认 `data_spec` 是 4 个情境、`grpo_opd` 组大小 2;默认配置 `groups_per_step=2`、`min_group_size=2`(缺省),交叉校验通过。
- 计划 2 的 `keyword_alternating_rollouts` 只支持 `outcome_split` 归因,训练位在偶数序号 rollout 里说每个分量的第一个关键词。默认配置的 `reward.outcome_rules = {"goal": ["agreed", "同意"]}`,组内奖励是 {1, 0},`degenerate_groups_total == 0`(任务 67 断言)。
- 计划 4 的 `PlanningContext.from_dict` 对缺键、多键一律拒绝;`CostGainEntry` 的 `gpu_hours`、`tokens` 未记录时**必须是 `None`**,不是 0,也不能省略键。`DiagnosticSummary.grade` 只能是 `mock` / `real`;`CandidateSummary.status` 只能是 `effective` / `rejected`。
- 计划 4 的 `plan()` 只捕获 `DecisionParseError`;`validator` 返回非空字符串计入同一重试预算;后端抛的任何异常原样上抛。`PlannerFailedError.attempts` 是总尝试次数。`ScriptedDecisionBackend` 脚本用完再调用抛 `AssertionError`。
- 计划 4 的 `HeuristicBackend` 只 clone `batch` 配方(`CLONABLE_RECIPE_TYPES = ("batch",)`),没有可 clone 的就 `new` 一条 `batch`。
- 计划 4 的模板版本号是 `"v1"`,文件在 `control-plane/config/prompts/planner-v1.yaml`;`load_template` 找不到抛 `TemplateError`。
- 计划 8 的 `plan_next_round` 与 `nominate_recipe` 都是 `Action(kind, target_id=<run id>, detail={"run_id": <run id>})`;处理器"必须自己重读树,不能只凭 `detail` 行事"。
- 命令行 `reject` 把驳回原因写进候选的 `payload.reject_reason`(`cli/src/commands.ts`)。
- 晋级(`controlplane.daemon` 的 `promote_candidate`)写 `models/zengen-v<N>.yaml`,`links=[cand-<run>, <run>]`,载荷带候选的 `checkpoint_ref`。
- 诊断报告的退化标记有三种形状(`src/diagnostic/regression.py`):`social_regression`(`dimension`、`drop`)、`g_probe_failure`(`probe`、`score`)、`reward_hacking_suspected`(`diag_delta`、`reward_delta`)。
- `GitStore.write_product` 内容不变时不提交;`list_products` 不递归。
- `tests/` 没有 `conftest.py`;pytest 默认的 prepend 导入模式让测试文件可以 `from ts_fixtures import ...`(计划 3 的 `rs_fixtures.py` 同一手法)。

## Rulings

设计与现有代码冲突、或设计没规定的细节,在这里裁决。

1. **Ruling: 扰动键是 `training_config` 的点路径,每段是标识符;首段必须是 `TrainingConfig` 的字段;多段时首段只能是 `grpo_config` 或 `reward`;锚定一轮身份的字段(`mode`、`trained_slot`、`base_checkpoint_ref`、`teacher_model_ref`、`stage_manifest_refs`、`bindings`、`scenario_ids`)永远不可扰动。白名单默认 `["opd_coefficient", "sft_steps", "grpo_steps", "grpo_config.learning_rate"]`;值的类型与范围由 `validate_training_config` 兜底(扰动后的配置在校验阶段就跑一次)。** —— 原因:设计只说"键在白名单内";`grpo_config` 是透传给执行器的字典,学习率只能从那里调;换基座、换数据、换模式就不是"克隆加扰动"而是另一个实验。默认四个键是 V1 里最常调、又不会让数据清单失配的超参(`group_size` 与 `groups_per_step` 会让复用的清单组数不够,不放进默认) —— 判错的代价:规划器想调别的键时每次都被拒、耗一次重试,要人改 `scheduler.yaml`;`grpo_config` 的叶子允许新键,写错键名(如 `grpo_config.learnig_rate`)只要在白名单里就会被执行器静默忽略。
2. **Ruling: `clone` 把 `base_checkpoint_ref` 换成最新已晋级模型的 checkpoint(没有已晋级模型时保留配方原值);`replay` 一个字段都不换。** —— 原因:设计 §7.2 要"逐步迭代"——克隆的意思是"在当前最好的模型上再用一次这套配方",从配方当年的旧基座起跑只会重复训练一个已被超越的模型;`replay` 的意义是复现,换了基座就不是复现 —— 判错的代价:`clone` 的战绩(计划 3 的 `record_track`)混合了"配方好"与"基座更好"两种贡献,配方的 `mean_gain` 偏乐观;想在旧基座上做对照实验要用 `replay`。
3. **Ruling: `new` 只构造 `batch` 指令;`new` + `interact` 即使 `allow_interact` 为真也判领域违规,理由写明"`training_defaults` 不提供 `scenario_ids` 与 `bindings`"。`allow_interact` 放行的是 clone / replay 一条 `interact` 配方。** —— 原因:interact 指令必须点名情境与对手绑定,`training_defaults.yaml` 只有一份 batch 配置,凭空构造只会写出过不了 `validate_training_config` 的指令;违规返回给规划器还能重采样,比解析阶段才失败好 —— 判错的代价:V1 规划器无法自己开一条新的 interact 训练线,只能复用人工收录的 interact 配方。
4. **Ruling: `accept_mock_evidence` 有两个来源:`scheduler.yaml` 的同名键(默认 `false`),以及 `make_nominate_handler(accept_mock_evidence=...)` / `nomination_check(..., accept_mock_evidence=...)` 的参数,参数不为 `None` 时覆盖配置。规划处理器不判证据等级:是否可接受由计划 8 的 `reconcile` 判,`assemble_context` 只拒绝 `stub`。** —— 原因:设计 R17 说默认不接受、开发入口用显式开关打开;计划 8 的 `reconcile_once(..., accept_mock_evidence=...)` 是调用方参数,计划 9 的组合根拿同一个开关同时传给 `reconcile` 与提名处理器最直接;配置键让不经组合根的直接调用也能开 —— 判错的代价:两处取值不一致时(配置开、开关没开),`reconcile` 不产生动作,处理器的设置不起作用,结果仍是"不提名",方向安全。规划处理器被绕过 `reconcile` 直接调用时会接受 `mock` 证据。 这两个参数是设计 R20 规定的签名扩展(设计 §7.2:`nomination_check` 多 `accept_mock_evidence`,`build_recipe_candidate` 多 `accept_mock_evidence` 与 `repo_commit`)。
5. **Ruling: 配置错误 `SchedulerConfigError` 与模板错误 `TemplateError` 都不是 `SchedulerError`,处理器遇到它们原样上抛,不把决策判 `failed`;`scheduler.yaml` 缺失取全部默认值,`training_defaults.yaml` 缺失抛 `SchedulerConfigError`(信息里写"先跑 `install_default_configs`")。** —— 原因:R5 规定失败的规划不会自动恢复;配置写错是人改一下就好的事,把某个 run 的规划永久判死代价太大。`scheduler.yaml` 的每个键都有安全的默认值;默认训练配置决定了真实训练的基座与超参,不该在没人看过的情况下静默生效 —— 判错的代价:配置写错时守护循环每个 tick 都抛同一个异常并退出(与派发器的瞬态异常同一已知限制),要外部监督重启。
6. **Ruling: 照设计 R18,`BridgeError` 无论 `category` 是 `T1` 还是 `T2`,规划处理器一律升级为 T3:决策置 `failed`,`failure = {category: "T3", reason: "bridge <T1|T2> after <exc.attempts> bridge attempt(s): <exc.reason>", attempts: <规划已调用后端的次数>}`,`record_failure(task_id="plan-<run>", category="T3", point="trainscheduler.plan", retries=attempts-1, log_ref=exc.log_ref)`,正常返回,不上抛。只有非桥接的瞬态异常(`OSError`、子进程错误等)原样上抛,决策保持原状(没写过就不写,已是 `pending` 就仍是 `pending`)。** —— 原因:桥接已经按 `retry_policy` 退避重试过,T1 表示它用尽了自己的重试;把耗尽的 T1 留作 `pending` 会让守护循环重启后对同一来源 run 无限空转外部推理;T2 是确定性故障,重来也一样。`attempts` 记的是规划层的调用次数(处理器用一个只计数的薄包装数出来),桥接层的尝试次数写进 `reason`,两层不混。`log_ref` 原样带进失败记录,`rismforge trace` 能直接看到外部工具的日志 —— 判错的代价:外部工具短暂不可用(网络抖动超过桥接的重试窗口)时,这一轮的规划就停在 `failed`,闭环停住,要人写下一条指令或手工删掉决策重来(R5、R18 已接受)。
7. **Ruling: `scheduler.yaml` 的 `max_plan_attempts` 是总尝试次数(默认 3,范围 1–10),传给 `plan()` 的是 `max_parse_retries = max_plan_attempts - 1`。** —— 原因:计划 4 的 Ruling 把 `max_parse_retries` 定义为"重试"次数,`PlannerFailedError.attempts` 是总次数;配置里写总次数,人读起来不需要做减法,失败记录的 `failure.attempts` 与配置值相等 —— 判错的代价:无,换算只在 `plan_handler` 一处。
8. **Ruling: `scheduler.yaml` 字段表固定为九个键(下文任务 60),未知键、类型错、范围错一律 `SchedulerConfigError`(点名 `scheduler.<键>`);除任务说明列出的键外加两个:`recent_decision_window`(近期决策取几条,默认 5)与 `template_version`(规划模板版本,默认 `"v1"`)。** —— 原因:设计说上下文带"近期决策与其后续结果",没给条数;模板版本设计 §7.1 说"版本号进配置" —— 判错的代价:两个键是本计划拍的,计划 9 的组合根若另起名字要改这里。
9. **Ruling: 默认 `training_config`:`trained_slot: zengen`、`sft_steps: 2`、`grpo_steps: 2`、`sft_batch_size: 1`、`groups_per_step: 2`、`group_size: 2`、`grpo_config: {learning_rate: 1.0e-5}`、`opd_coefficient: 0.1`、`checkpoint_every_steps: 1`、`allow_resume: true`、`reward` 为单分量 `goal` 的 `outcome_split`(关键词 `agreed`、`同意`);初始基座与教师是 `file:///compute/checkpoints/initial-base.safetensors`、`opd-teacher.safetensors`,哈希为 64 个 0。默认 `data_spec`:4 个情境、种子 0、`training`、`sft` 组大小 1、`grpo_opd` 组大小 2。`training_defaults.training_config` 不得写 `stage_manifest_refs`(每轮由 `construct_directive` 按新 run id 写),`mode` 必须是 `batch`。** —— 原因:要能通过 `validate_training_config`,又要配合计划 2 的默认生产器真能跑通一轮(任务 67 端到端);步数取最小的非平凡值,让 `MockExecutor` 一轮几秒跑完;占位哈希能过 64 位十六进制校验,但一眼能认出是假的 —— 判错的代价:真实部署前必须把两个 BigRef 换成真实权重的地址与哈希,否则第一轮就在不存在的基座上训练(`MockExecutor` 不读文件,所以测试发现不了)。
10. **Ruling: `clone` / `replay` 的配方除了"存在且 `effective`",还必须:`recipe_type` 是 `batch` 或 `interact`(`agent_rsi` 构造不成训练指令);`interact` 配方要 `allow_interact`;配方的 `dimension` 等于决策的 `target_dimension`;配方带 `training_config` 映射;扰动后的配置能通过 `validate_training_config`。每条都作为领域违规返回规划器重采样。** —— 原因:这些在 `resolve_action` 里迟早会失败,放进 validator 就能在同一次规划里纠正,而不是把决策判 `failed`;维度不匹配会让战绩记到错误的维度上 —— 判错的代价:规划器想"用 tom 的配方去练 empathy"时会被拒。
11. **Ruling: `assemble_context` 的取材:`recipes` 是 `find_recipes(store)`(只 `effective`,track 序)去掉 `payload`,形状不对(`recipe_type` 不认识、`dimension` 为空)的配方跳过;`recent_decisions` 与 `cost_gain` 只看排序键不大于来源 run 的 run(`run-<数字>` 按数值排,其他名字排在最前),各取最后 K 条;一个 run 的成本是 `run_trace.cost_records` 里该键的和,一条记录都没有该键就是 `None`;`cost_gain` 只收有指令且 `target_dimension` 非空的 run;`gain` 一律用 `compute_gain`。决策为 `pending` 且 `next_run_id` 的指令已在,按 `executed` 收进 `recent_decisions`;`failed` 的和指令未写的不收。** —— 原因:设计 §4.1 末尾、§14 第 7 条;窗口不能包含来源 run 之后的轮次,否则同一来源 run 在重入时会看到不同的上下文 —— 判错的代价:手工起名的 run 永远排在窗口最前,窗口小时会被挤掉。
12. **Ruling: 退化标记归一化:`social_regression → (dimension, drop)`、`g_probe_failure → (probe, score)`、`reward_hacking_suspected → ("reward", reward_delta − diag_delta)`;不认识的 `kind` 取 `dimension` 或 `probe` 或 `kind` 作主体、`drop` 或 `score` 或 0.0 作幅度;认识的 `kind` 缺字段是 `ContextAssemblyError`。** —— 原因:计划 4 的契约只定义了前两种;第三种在 `diagnostic/regression.py` 里存在,丢掉它规划器就看不到"疑似奖励作弊" —— 判错的代价:未知标记的幅度可能是 0.0,规划器会低估它。
13. **Ruling: 被驳回候选的 `reject_reason` 取候选载荷的 `reject_reason`(命令行 `reject` 写入),为空或缺失时是 `None`;`effective` 候选恒为 `None`。** —— 原因:计划 8 的跨语言约定表就是这么写的 —— 判错的代价:无。
14. **Ruling: 决策产物 id 为 `planner-<来源 run>`、类型 `planner_decision`、`links=[来源 run, next_run_id]`;载荷恰为设计 §4.2 的七个键;规划前就失败时(此时还没写过 pending 决策)决策直接以 `failed` 写出,不经 `pending`(设计 §4.1 已注明),`next_run_id` 与 `decision` 为 `None`、`rationale` 为空串;失败记录的 `task_id` 为 `plan-<来源 run>`,`point` 为 `trainscheduler.plan`,`retries` 为尝试次数减一(不小于 0)。决策写出之后才在解析或构造阶段失败的,`next_run_id` 保留(留痕,该 run id 不会被复用,因为分配只看目录,而那个目录没有被创建)。** —— 原因:计划 4 的示例上下文用 `planner-run-002`;失败也要能从决策产物看出发生在哪一步;`resolve_action` 与 `construct_directive` 放在写出 pending 决策之后(与设计 §7.2 的原序不同),是为了让崩在解析或构造阶段的那一轮走同一条重入路径续写 —— 判错的代价:分配过但没用上的 run id 会被下一次规划再次分配(目录不存在),两条决策的 `next_run_id` 相同,读历史时要以指令的 `planner_decision_ref` 为准。
15. **Ruling: 指令 id 等于新 run id,`links=[决策 id] + ([配方 id] if clone/replay)`;`construct_directive` 遇到目标位置已有指令时,`planner_decision_ref` 相同视为已完成(原样返回,不改状态),不同则 `DirectiveConstructionError`,绝不覆盖。** —— 原因:重入时不写第二条指令;人工写在那个位置的指令不能被调度器盖掉 —— 判错的代价:无。
16. **Ruling: 配方候选的 `eval_anchor` 只填诊断报告里有的 `benchmark_version`,其余四个键为 `None`;`code_version.repo_commit` 由 `make_nominate_handler(repo_commit=...)` 传入,缺省 `None`;`stage_manifests` 的哈希从清单产物读,读不到为 `None`。这些缺口如实进入 `repro.errors`,不编造。** —— 原因:现有诊断报告不带分类表与题库版本;调度器不知道产出那一轮的代码提交(派发时没记);设计 §6.1 说校验结果"不硬拒绝,由审核门③的人参考" —— 判错的代价:V1 的每个 batch 配方候选都带两条 `eval_anchor` 错误,审核人要习惯它;组合根不传 `repo_commit` 时再多一条。 `repo_commit` 参数见设计 R20。
17. **Ruling: 提名判定的顺序与原因:没有候选、候选不是 `effective`、没有指令、指令不是 batch/interact 训练指令、没有 `target_dimension`、没有报告、证据不可接受、`compute_gain` 为 `None`、增益低于 `min_gain`、报告有退化标记;第一条命中的写进 `reason`。其中"没有候选""候选是 `pending_review` 或 `approved`""没有指令""没有报告"四种是**还没到**(`NominationVerdict.not_yet = True`):它们会自己变好,处理器直接返回、**不写标记**,下一次触发再判。其余(候选 `rejected`、指令不是训练指令、证据不可接受、没有可比增益、增益不够、有退化标记)是终局判定,写 `skipped` 标记。** —— 原因:标记就是幂等键,对"还没到"的状态写下 `skipped`,这个 run 就永远不会再被提名,哪怕候选后来晋级、报告后来补上;设计 §6.3 列了后四条,前几条防止处理器被直接调用(或计划 8 的触发条件与树不同步)时读到半成品 —— 判错的代价:"还没到"的 run 每次被触发都会重读几份产物再返回,不留痕;计划 8 的 `nominate_recipe` 本就要求候选 `effective`、证据可接受,正常树上走不到这些分支。证据等级不可接受(例如 `mock` 而没开开关)记为终局:之后打开 `accept_mock_evidence` 也不会回头补提名已判 skipped 的 run,要人删掉 `nomination.yaml` 重来。
18. **Ruling: 提名处理器的幂等键是 `nomination.yaml`;它不在而 `recipe_candidates/recipe-<run>.yaml` 已在时(崩在"候选已写、标记未写"之间,或人已经审核过),只补一条 `result: nominated` 的标记,原因写明候选的当前状态,候选一个字节都不改。** —— 原因:任务说明要求保护已审核的候选;重写会把 `approved` 改回 `pending_review` —— 判错的代价:候选是人手放的(并非本处理器写的)时,标记也写 `nominated`。
19. **Ruling: `assemble_context`、`validate_decision`、`resolve_action`、`nomination_check`、`build_recipe_candidate` 都是只读的(各有测试比对 HEAD);`assemble_context` 额外接受 keyword-only 的 `config: SchedulerConfig | None = None`,缺省时自己读 `scheduler.yaml`。** —— 原因:设计 §7.2 的签名 `assemble_context(store, run_id, *, config=None)` 照留,处理器已经读过配置时不再读一遍;keyword-only 的可选参数不改变两参数的调用 —— 判错的代价:无。
20. **Ruling: `make_plan_handler` 的四个参数照任务说明命名(`backend`、`template_loader`、`config_loader`、`defaults_loader`),后三个有缺省值(`load_template`、`load_scheduler_config`、`load_training_defaults`),每次处理器被调用时各调一次。`make_nominate_handler` 的 `config_loader` 缺省 `load_scheduler_config`,另加 keyword-only 的 `accept_mock_evidence` 与 `repo_commit`。** —— 原因:人改了树里的配置,下一次规划就生效,不用重启守护进程 —— 判错的代价:每次规划多读两份配置与一份模板,几个小文件。
21. **Ruling: `pyproject.toml` 用追加式脚本改(任务 60 第 2 步):正则读出 `packages = [...]` 那一行,按逗号拆成项;已含 `"src/trainscheduler"` 就打印 `already listed`、不写文件,否则追加在末尾再写回;不假设列表现在有几项、是什么顺序。** —— 原因:计划 1–4 各自往同一行末尾追加了自己的包,执行顺序不同时列表内容不同,写死"改成某一整行"的步骤在任何非撰写时的顺序下都会覆盖掉别人的包;与计划 1 任务 25、计划 4 任务 50 同一写法。重复运行不改文件,所以中途重跑这一步是安全的 —— 判错的代价:有人把 `packages` 改成多行数组时正则找不到那一行,脚本以 `wheel packages line not found` 断言失败,要手工追加。
22. **Ruling: run id 一律按数值语义,与设计 §3.2 的 `plan_next_round` 一致:只有匹配 `^run-[0-9]+$` 的目录参与比较,按编号取最大;其他名字(`test-x`、`zz`、`exp-a`)不参与;不补零的 `run-9` 按编号 9 参与;补零只为可读,`run-1000` 按数值排在 `run-999` 之后(设计 §7.2)。`allocate_run_id` 仍是一个确定性失败点,作为防御性断言:若存在 `run-<数字>` 目录,其编号不小于新 id 的编号(例如人手建了 `run-050`,而算出的新编号也是 050),抛 `RunIdOrderError`(`SchedulerError` 子类),信息里列出这些目录;规划处理器把它当确定性失败:决策以 `failed` 写出(`decision` 与 `rationale` 留痕,`next_run_id` 为 `None`),写 T3 失败记录,人手建的那个目录原样不动。** —— 原因:分配器与计划 8 用同一套数值语义,`run-9`、`test-x`、`zz` 在树里既不挡路也不会造成闭环静默停住;断言保留是因为分配之后写指令之前若被人手工插进同号目录,覆盖它比失败更糟 —— 判错的代价:正常路径走不到(`next_run_id` 取最大编号加一,新编号恒大于所有现有编号),走到说明树被人手工改过;测试只能靠 `monkeypatch` 让编号计算"看漏"一个目录来覆盖这条分支。
23. **Ruling: 照设计 R21,`assemble_context` 构造 `ErrorProfileSummary` 前把错误类型分布里的空标签 `""` 丢掉,不重新归一化:`{k: v for k, v in dist.items() if k != ""}`(`assemble.drop_no_error_label`);全是空标签时得到空映射。不改计划 4,也不改诊断模块。** —— 原因:真实代码 `diagnostic.error_classifier.classify_static_family` 对"基题与变体都答对"的变体族返回空串,`aggregate_error_profile` 把它原样数进分布,真实报告里会出现 `error_type_distribution == {"": 1.0}`;空标签的含义是"没有错误",不是一种错误类型;计划 4 的 `_check_score_map` 用 `_check_str` 拒绝空键,若原样交过去,`PlanningContext` 构造失败,规划决策判 `failed`(T3),而且恰恰是在全部变体族上都答对的最好候选会让闭环停在规划这一步。计划 4 接受空映射 —— 判错的代价:丢掉之后分布的和可以小于 1,规划器看不到"这个二级维度答对了多少";这一信息仍在 `l1_scores` 里(一级分数),二级的答对比例在上下文里不可见。若不丢,计划 4 会拒绝空键。

## Global Constraints

每个任务的要求都隐含包含本节。

- `src/` 下每个源文件以 `from __future__ import annotations` 开头(模块 docstring 之后的第一条语句也算);任务 67 有测试强制检查 `src/trainscheduler/` 的每个模块。
- **依赖方向**(设计 §11;设计已在依赖图里列出 `mindtrainer.data_spec` 与 `mindtrainer.training_config` 两条边)。`src/trainscheduler/` 实际导入的项目内包,全部列在这里:
  - `controlplane`:`config`、`envelope`、`evidence`、`gitstore`、`ledger`、`models`、`paths`、`reconcile`(只取 `Action`)、`states`;
  - `recipestore`:`gain`、`ids`、`library`、`validate`,**只经 `trainscheduler/recipes_port.py` 一个文件导入**;
  - `trainplanner`:`backends`(只取 `DecisionBackend` 类型)、`context`、`decision`、`planner`、`templates`;
  - `inference_bridge`:只取 `BridgeError`(`plan_handler` 按 R18 升级它);
  - `mindtrainer.data_spec`(`DataSpec`、`parse_data_spec`、`InvalidDataSpecError`、`required_stages`、`check_data_spec_against_config`、`manifest_data_run`),以及 `mindtrainer.training_config`(`TrainingConfig`、`validate_training_config`、`load_config_from_directive`、`InvalidTrainingConfigError`;构造与校验指令要用它,这一条也请协调方在设计依赖图里一并列上)。
  - 标准库只用 `__future__`、`collections`、`copy`、`dataclasses`、`math`、`re`。
  - **不导入** `mindtrainer` 的其他任何模块(`dispatch`、`orchestrator`、`executor`、`data_producer`……),**不导入** `diagnostic`、`simulator`、`actio`、`mindagent`、`factory`。
  - 反方向:`controlplane`、`recipestore`、`trainplanner`、`mindtrainer`、`inference_bridge` 都不导入 `trainscheduler`。
  - 以上每一条都由任务 67 的 `tests/test_ts_boundaries.py` 扫 `src/trainscheduler/*.py` 的导入语句钉住(含"`mindtrainer` 下恰好用到 `training_config` 与 `data_spec` 两个模块")。测试文件不受此限,可以导入 `mindtrainer.dispatch`、`mindtrainer.data_producer` 等。
- **不引入新第三方依赖**;`pyproject.toml` 只改 wheel 包列表这一行。
- **凭证绝不进 git 树**:两份配置都经 `write_config` 写,它递归拒绝凭证值字段;本计划的载荷里没有凭证。
- **读产物走 `GitStore.read_product`,写产物用 `Envelope` + `GitStore.write_product`**;配置走 `read_config` / `write_config`。目录扫描(`runs/`)只用来列 run id。
- **新增顶层包加进 `pyproject.toml` 的 wheel 包列表**(`tests/test_mt_packaging.py` 强制检查),在任务 60 做。
- **只改本计划文件表里的文件**:不碰 `src/controlplane`、`src/recipestore`、`src/trainplanner`、`src/mindtrainer`、`src/inference_bridge`、`schemas/`、`cli/`。
- **处理器的失败约定**(设计 §3.3):确定性失败写自己产物的 `failed` 与失败记录(经 `record_failure`),然后正常返回;瞬态异常原样上抛。
- **测试脱离外部服务**:规划后端只用 `ScriptedDecisionBackend` 与 `HeuristicBackend`,不用真实桥接;训练只用 `MockExecutor`;全部测试用 `tmp_path` 隔离。
- `git add` 逐个文件,**不要用 `git add .`**;**不要碰** `cli/package.json`、`cli/package-lock.json`;**提交信息不加 `Co-Authored-By` 行**(用户明确要求)。
- **测试命令**一律是 `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`(下文简称"全量命令",跑单个文件时在末尾加路径),不得出现警告。
- **测试数**:基线 **B** 是计划 1–4 全部落地后的全量数,与起点提交 `BASE` 一起在任务 60 第 2 步实测并写进执行记录。本计划各任务的增量依次为 **+54、+30、+34、+25、+12、+38、+34、+8、+0**,累计 **B+54 → B+84 → B+118 → B+143 → B+155 → B+193 → B+227 → B+235 → B+235**。撰写时在临时拷贝里真实跑出 B = **1543**(`main@0385004` 的 820,加上从计划 1–4 计划文本里原样抽出的全部代码与测试),对应 **1597 → 1627 → 1661 → 1686 → 1698 → 1736 → 1770 → 1778 → 1778**。**绝对通过数依赖各计划的执行顺序(谁先落地、各自加了多少测试),本计划只保证增量**;B 的实测值可能与 1543 不同(见"未验证项"第 1 条),以增量为准。

## File Structure

| 文件 | 动作 | 职责 |
|---|---|---|
| `pyproject.toml` | 修改(1 行) | wheel 包列表末尾追加 `"src/trainscheduler"`(追加式脚本,已有则不动,Ruling 21) |
| `src/trainscheduler/__init__.py` | 新建(6 行) | 包说明,不导出名字 |
| `src/trainscheduler/errors.py` | 新建(36 行) | `SchedulerError` 及四个子类;`SchedulerConfigError` |
| `src/trainscheduler/recipes_port.py` | 新建(22 行) | 对计划 3 的全部导入 |
| `src/trainscheduler/config.py` | 新建(304 行) | `SchedulerConfig`、`TrainingDefaults`、两个严格解析、两个读取、`install_default_configs`、`check_perturbable_key` |
| `src/trainscheduler/run_ids.py` | 新建(73 行) | `run_number`、`format_run_id`、`next_run_id`、`blocking_run_dirs`、`list_run_dirs`、`allocate_run_id`、`run_order_key` |
| `src/trainscheduler/assemble.py` | 新建(236 行) | `assemble_context`、`regression_mark`、`decision_is_executed`、`directive_file` |
| `src/trainscheduler/resolve.py` | 新建(155 行) | `ResolvedAction`、`validate_decision`、`resolve_action`、`apply_perturbation` |
| `src/trainscheduler/directive.py` | 新建(99 行) | `construct_directive`、`manifest_id`、`check_directive` |
| `src/trainscheduler/plan_handler.py` | 新建(217 行) | `make_plan_handler`、`decision_id`、`plan_task_id` |
| `src/trainscheduler/nominate.py` | 新建(226 行) | `NominationVerdict`、`nomination_check`、`build_recipe_candidate`、`make_nominate_handler` |
| `tests/ts_fixtures.py` | 新建(183 行) | 测试共用的构造函数(不被 pytest 收集) |
| `tests/test_ts_config.py` | 新建(54 个用例) | 两份配置 |
| `tests/test_ts_run_ids.py` | 新建(30 个用例) | run id 分配 |
| `tests/test_ts_assemble.py` | 新建(34 个用例) | 上下文装配 |
| `tests/test_ts_resolve.py` | 新建(25 个用例) | 领域校验与动作解析 |
| `tests/test_ts_directive.py` | 新建(12 个用例) | 指令构造 |
| `tests/test_ts_plan_handler.py` | 新建(38 个用例) | 规划处理器与重入 |
| `tests/test_ts_nominate.py` | 新建(34 个用例) | 提名 |
| `tests/test_ts_e2e.py` | 新建(2 个用例) | 调度器一侧端到端 |
| `tests/test_ts_boundaries.py` | 新建(6 个用例) | 依赖方向(扫 `src/trainscheduler` 的导入语句)与 `__future__` 头 |

(用例数按 pytest 收集的参数化展开计。)

**前置检查命令**(每个任务第 1 步都跑一次;任何一行报错就停下,把报错原样报告给计划作者,不要改本计划去迁就):

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev python -c "
from controlplane.paths import planner_decision_file, nomination_file, recipe_candidate_file, run_trace_file, failure_record_file, scheduler_config_file, training_defaults_file, recipe_file, recipe_track_file
from controlplane.config import read_config, write_config
from controlplane.evidence import diagnostic_grade, evidence_acceptable
assert diagnostic_grade({'dimension_scores': [1]}) == 'stub' and diagnostic_grade({'backends': 'real', 'dimension_scores': [1]}) == 'real'
from controlplane.ledger import record_failure
from controlplane.models import latest_promoted_checkpoint, MissingCheckpointRefError
from controlplane.states import can_transition
assert can_transition('nonreview', 'awaiting_confirmation', 'pending')
from inference_bridge import BridgeError
assert BridgeError('T1', 'x').category == 'T1'
from mindtrainer.data_spec import DataSpec, InvalidDataSpecError, parse_data_spec, required_stages, check_data_spec_against_config, manifest_data_run, data_spec_from_directive, stages_to_synthesize
from mindtrainer.data_producer import TemplateDataProducer
from mindtrainer.dispatch import make_mindtrainer_dispatcher
import inspect; assert 'data_producer' in inspect.signature(make_mindtrainer_dispatcher).parameters
from recipestore.gain import GainResult, compute_gain, read_report_payload, regression_flags
from recipestore.ids import recipe_id_for, run_id_of
from recipestore.library import RecipeNotFoundError, RecipeView, find_recipes, get_recipe
from recipestore.validate import ReproReport, validate_reproducibility
from recipestore.admit import handle_admit_recipe, has_repro_evidence
from trainplanner.context import PlanningContext, CostGainEntry, CANDIDATE_TERMINAL_STATES, RECIPE_TYPES
CostGainEntry(run_id='r', target_dimension='tom', gpu_hours=None, tokens=None, gain=None)
from trainplanner.examples import example_context_dict
from trainplanner.decision import PlannerDecision
from trainplanner.planner import plan, PlannerFailedError
from trainplanner.templates import PromptTemplate, load_template, install_default_template, PLANNER_V1_VERSION
assert PLANNER_V1_VERSION == 'v1'
from trainplanner.backends import DecisionBackend, HeuristicBackend, ScriptedDecisionBackend
print('preflight ok')
"
```

Expected: 最后一行是 `preflight ok`。

---
### Task 60: 包骨架、错误类型、计划 3 的导入口与两份配置

**Files:**
- Modify: `pyproject.toml`(wheel 包列表末尾追加 `"src/trainscheduler"`,不假设列表现有几项,Ruling 21)
- Create: `src/trainscheduler/__init__.py`(6 行)、`errors.py`(36 行)、`recipes_port.py`(22 行)、`config.py`(304 行)
- Create: `tests/ts_fixtures.py`(后续任务共用)
- Test: `tests/test_ts_config.py`(54 个用例)

**Interfaces:**
- Consumes: `controlplane.config.read_config` / `write_config`;`controlplane.paths.scheduler_config_file` / `training_defaults_file`;`mindtrainer.training_config.TrainingConfig` / `validate_training_config` / `InvalidTrainingConfigError`;`mindtrainer.data_spec.DataSpec` / `parse_data_spec` / `InvalidDataSpecError` / `required_stages` / `check_data_spec_against_config`;`trainplanner.templates.PLANNER_V1_VERSION`;计划 3 的全部名字(经 `recipes_port`)。
- Produces(任务 61–67、计划 9 依赖):
  - `SchedulerError(reason)`(属性 `reason`)及子类 `ContextAssemblyError`、`ResolveError`、`DirectiveConstructionError`、`RunIdOrderError`(任务 61 用);`SchedulerConfigError(ValueError)`(**不是** `SchedulerError`)。
  - `SchedulerConfig`(冻结)九个字段与默认值见下表;`to_dict()`;`DEFAULT_SCHEDULER_CONFIG`。
  - `parse_scheduler_config(raw) -> SchedulerConfig`、`load_scheduler_config(store) -> SchedulerConfig`(文件不在取默认)。
  - `TrainingDefaults(training_config: dict, data_spec: DataSpec)`(冻结),`to_dict()`;`parse_training_defaults(raw)`、`load_training_defaults(store)`(文件不在抛 `SchedulerConfigError`)。
  - `DEFAULT_TRAINING_CONFIG`、`DEFAULT_DATA_SPEC`、`default_training_defaults_payload()`、`install_default_configs(store) -> list[str]`(返回本次写入的路径,已存在的不动)。
  - `check_perturbable_key(key) -> str | None`、`NESTED_FIELDS = ("grpo_config", "reward")`;配置 id `SCHEDULER_CONFIG_ID = "scheduler-config"`、`TRAINING_DEFAULTS_ID = "training-defaults"`。

**设计要点:**
- **`scheduler.yaml` 字段表**(全部可省略,省略取默认;未知键拒绝):

  | 键 | 类型与范围 | 默认 | 含义 |
  |---|---|---|---|
  | `confirm_directives` | bool | `true` | 调度器写的指令是 `awaiting_confirmation`(真)还是 `pending`(R3) |
  | `allow_interact` | bool | `false` | 是否放行 interact 配方的 clone / replay(Ruling 3) |
  | `accept_mock_evidence` | bool | `false` | 提名是否接受 `mock` 证据(Ruling 4) |
  | `perturbable` | 点路径列表,不重复 | `[opd_coefficient, sft_steps, grpo_steps, grpo_config.learning_rate]` | 扰动白名单(Ruling 1) |
  | `min_gain` | 有限数 | `0.01` | 提名阈值(R7) |
  | `cost_gain_window` | int,1–100 | `5` | 上下文里近几轮的成本增益 |
  | `recent_decision_window` | int,1–100 | `5` | 上下文里近几条决策 |
  | `max_plan_attempts` | int,1–10 | `3` | 规划总尝试次数(Ruling 7) |
  | `template_version` | 非空 str | `"v1"` | 规划模板版本 |
- **`training_defaults.yaml` 字段表**:恰好两个键,都必填。`training_config` 是 `TrainingConfig.to_dict()` 的形状去掉 `stage_manifest_refs`,`mode` 必须是 `batch`,只允许 `TrainingConfig` 的字段名;`data_spec` 是计划 2 的格式。解析时先在没有清单 id 的配置上跑 `validate_training_config`(只容许"缺 `stage_manifest_refs[...]`"这一条违规,其余类型错、范围错立刻报),再补上占位清单 id 跑一遍完整校验,再与 `data_spec` 交叉校验。顺序不能反:`required_stages` 直接比较 `sft_steps > 0`,类型错会以裸 `TypeError` 漏出去(有测试)。
- 默认值见 Ruling 9;`install_default_configs` 对已存在的文件一律不动,哪怕内容与默认不同(有测试)。
- `ts_fixtures.py` 的造数函数按真实写方的形状写产物:诊断报告照 `DiagnosticReport.to_dict()`(多带一条 `l2` 分数,证明装配只取 `l1`)、模型照 `promote_candidate`、配方照计划 3 的收录。后续任务都从它导入。

- [ ] **Step 1: 前置检查**

运行上文"前置检查命令"。Expected: `preflight ok`。

- [ ] **Step 2: 记下基线并登记新包**

在动任何文件之前,先记下起点提交与基线通过数,两个值都写进执行记录(任务 68 要用 `BASE`):

```bash
BASE=$(git rev-parse HEAD); echo "BASE=$BASE"
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error | tail -1
```

Expected: 第一行是 `BASE=<40 位提交号>`;第二行形如 `NNNN passed in ...`,把 NNNN 记作 **B**(撰写时为 1543;绝对数依赖计划 1–4 的执行顺序,不必相同)。

然后用下面的脚本把 `"src/trainscheduler"` 追加到 `pyproject.toml` 的 wheel 包列表末尾(Ruling 21)。脚本读出 `packages = [...]` 那一行,不假设列表现在有几项;已含 `"src/trainscheduler"` 就打印 `already listed`、不改文件:

```bash
uv run python - <<'PY'
import re
from pathlib import Path

p = Path("pyproject.toml")
s = p.read_text(encoding="utf-8")
m = re.search(r'^packages = \[(.*)\]$', s, re.M)
assert m, "wheel packages line not found"
items = [x.strip() for x in m.group(1).split(",") if x.strip()]
if '"src/trainscheduler"' in items:
    print("already listed")
else:
    items.append('"src/trainscheduler"')
    s = s[: m.start()] + "packages = [" + ", ".join(items) + "]" + s[m.end():]
    p.write_text(s, encoding="utf-8")
print(re.search(r'^packages = .*$', s, re.M).group(0))
PY
```

Expected(打印出的 `packages` 行):
- 从基线四项开始(只有 `main` 原有的包):`packages = ["src/controlplane", "src/simulator", "src/diagnostic", "src/mindtrainer", "src/trainscheduler"]`。按本计划的前置(计划 1–4 已合并),不应出现这种情况;出现了就先停下,回头检查前置。
- 列表已含前面计划追加的包:那些包原样保留、顺序不变,`"src/trainscheduler"` 接在最后。计划 1–4 都落地后(撰写时的顺序)为 `packages = ["src/controlplane", "src/simulator", "src/diagnostic", "src/mindtrainer", "src/inference_bridge", "src/recipestore", "src/trainplanner", "src/trainscheduler"]`;计划 1–4 的追加顺序不同,前面几项的顺序也会不同。

重复运行不会重复追加:第二次只打印 `already listed` 和同一行 `packages`。最后 `grep -c '"src/trainscheduler"' pyproject.toml` 应输出 `1`。

- [ ] **Step 3: 写失败测试**

新建 `tests/ts_fixtures.py`:

```python
"""TrainScheduler 测试共用的构造函数。不以 test_ 开头,pytest 不收集;测试文件靠默认的 prepend
导入模式直接 `from ts_fixtures import ...`。"""
from __future__ import annotations

import copy
import json

from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import (
    candidate_checkpoint_file,
    diagnostic_report_file,
    model_file,
    planner_decision_file,
    recipe_file,
    recipe_track_file,
    run_dir,
    run_trace_file,
)
from trainscheduler.config import DEFAULT_TRAINING_CONFIG

NOW = "2026-10-09T00:00:00Z"
SHA = "a" * 64


def make_store(tmp_path, name: str = "repo") -> GitStore:
    store = GitStore(str(tmp_path / name))
    store.init()
    return store


def env(id_: str, type_: str, status: str, payload: dict, links: list[str] | None = None) -> Envelope:
    return Envelope(
        id=id_, type=type_, version=1, created_at=NOW, created_by=OwnerRef(), status=status,
        schema_version=1, links=list(links or []), payload=copy.deepcopy(payload),
    )


def ref(name: str) -> dict:
    return {"uri": f"file:///compute/{name}.safetensors", "sha256": SHA, "size_bytes": 1, "fetch_hint": None}


def batch_training_config(run_id: str = "run-001") -> dict:
    cfg = copy.deepcopy(DEFAULT_TRAINING_CONFIG)
    cfg["stage_manifest_refs"] = {"sft": f"dsm-{run_id}-sft", "grpo_opd": f"dsm-{run_id}-grpo_opd"}
    return cfg


def write_directive(store: GitStore, run_id: str, *, dimension: str = "tom", status: str = "executed",
                    training_config: dict | None = None, recipe_ref: str = "", extra: dict | None = None) -> None:
    payload = {"target_dimension": dimension, "recipe_ref": recipe_ref,
               "training_config": training_config if training_config is not None else batch_training_config(run_id)}
    payload.update(extra or {})
    store.write_product(f"{run_dir(run_id)}/directive.yaml", env(run_id, "directive", status, payload),
                        f"test: directive {run_id}")


def write_candidate(store: GitStore, run_id: str, status: str = "effective", *, reject_reason: str | None = None) -> None:
    payload = {"checkpoint_ref": ref(f"cand-{run_id}")}
    if reject_reason is not None:
        payload["reject_reason"] = reject_reason
    store.write_product(candidate_checkpoint_file(run_id),
                        env(f"cand-{run_id}", "candidate_checkpoint", status, payload, [run_id]),
                        f"test: candidate {run_id}")


def l1(dimension: str, score: float) -> dict:
    return {"dimension": dimension, "level": "l1", "score": score, "ci_low": max(0.0, score - 0.05),
            "ci_high": min(1.0, score + 0.05), "sample_size": 40, "track_provenance": "static"}


def write_report(store: GitStore, run_id: str, scores: dict[str, float] | None = None, *,
                 baseline_ref: str = "", flags: list[dict] | None = None, backends: str | None = "mock",
                 stub: bool = False, error_profiles: list[dict] | None = None,
                 benchmark_version: str = "bench-v1") -> None:
    """按 DiagnosticReport.to_dict() 的形状写报告;stub=True 时写成 diagnostic_evaluator_stub 的形状。
    backends=None 不写标记,controlplane.evidence 按失败关闭判 stub;要一份 real 报告传 backends="real"。"""
    if stub:
        payload = {"candidate_ref": f"cand-{run_id}", "benchmark_version": benchmark_version,
                   "dimension_count": 0, "stub": True}
    else:
        payload = {
            "id": run_id, "candidate_ref": f"cand-{run_id}", "baseline_ref": baseline_ref,
            "benchmark_version": benchmark_version, "run_tier": "quick", "sampling_k": 1,
            "dimension_scores": [l1(d, s) for d, s in (scores or {"tom": 0.5}).items()]
            + [{"dimension": "tom.false_belief", "level": "l2", "score": 0.4, "ci_low": 0.3,
                "ci_high": 0.5, "sample_size": 20, "track_provenance": "static"}],
            "error_profiles": list(error_profiles or []),
            "regression_flags": {"flags": list(flags or [])},
            "consistency_signals": {"rule_vs_judge_divergence": {}, "diagnostic_vs_reward_divergence": 0.0},
            "links": [run_id],
        }
        if backends is not None:
            payload["backends"] = backends
    store.write_product(diagnostic_report_file(run_id),
                        env(f"diag-{run_id}", "diagnostic_report", "executed", payload, [run_id]),
                        f"test: report {run_id}")


def write_model(store: GitStore, n: int, run_id: str, *, status: str = "effective") -> None:
    """与 controlplane.daemon 的 promote_candidate 一致:links=[候选 id, run id],带 checkpoint_ref。"""
    store.write_product(model_file(n),
                        env(f"zengen-v{n}", "zengen_model", status,
                            {"promoted_from": f"cand-{run_id}", "checkpoint_ref": ref(f"cand-{run_id}")},
                            [f"cand-{run_id}", run_id]),
                        f"test: model v{n}")


def write_round(store: GitStore, run_id: str, scores: dict[str, float], *, model_n: int | None = None,
                baseline_ref: str = "", candidate_status: str = "effective", backends: str | None = "mock",
                flags: list[dict] | None = None, dimension: str = "tom") -> None:
    """一整轮:指令(executed)、候选、诊断;model_n 不为 None 时再登记晋级模型。"""
    write_directive(store, run_id, dimension=dimension)
    write_candidate(store, run_id, candidate_status)
    write_report(store, run_id, scores, baseline_ref=baseline_ref, backends=backends, flags=flags)
    if model_n is not None:
        write_model(store, model_n, run_id)


def recipe_payload(run_id: str = "run-001", *, dimension: str = "tom", recipe_type: str = "batch") -> dict:
    cfg = batch_training_config(run_id)
    if recipe_type == "interact":
        cfg.update(mode="interact", stage_manifest_refs={"sft": f"dsm-{run_id}-sft"}, scenario_ids=["scen-1", "scen-2"],
                   bindings=[{"slot_id": "zengen", "persona_id": "p-1", "model_backend": "self_play", "model_ref": None},
                             {"slot_id": "other", "persona_id": "p-2", "model_backend": "frozen_reference",
                              "model_ref": "zengen-v1"}],
                   max_turns_per_episode=4)
    return {
        "recipe_type": recipe_type, "dimension": dimension, "source_run_id": run_id,
        "eval_anchor": {"taxonomy_version": "tax-v1", "item_pool_version": "pool-v1", "benchmark_version": "bench-v1",
                        "judge_prompt_version": None, "scoring_config_version": None},
        "result": {"gain": 0.05, "baseline_ref": "zengen-v1", "candidate_ref": f"cand-{run_id}", "promoted": True},
        "repro": {"errors": [], "warnings": []},
        "nondeterministic": recipe_type == "interact",
        "training_config": cfg,
        "stage_manifests": {s: {"id": i, "manifest_hash": "b" * 64} for s, i in cfg["stage_manifest_refs"].items()},
        "code_version": {"repo_commit": "0385004"},
    }


def write_recipe(store: GitStore, run_id: str, *, status: str = "effective", payload: dict | None = None,
                 entries: list[dict] | None = None) -> str:
    """按计划 3 的收录写法写一条配方(recipe.yaml + track_record.yaml),返回配方 id。"""
    rid = f"recipe-{run_id}"
    body = payload if payload is not None else recipe_payload(run_id)
    store.write_product(recipe_file(run_id), env(rid, "recipe", status, body, [run_id]), f"test: recipe {rid}")
    store.write_product(recipe_track_file(run_id),
                        env(f"track-{rid}", "recipe_track_record", "executed",
                            {"recipe_id": rid, "entries": list(entries or [])}, [rid]),
                        f"test: track {rid}")
    return rid


def write_trace(store: GitStore, run_id: str, records: list[dict]) -> None:
    store.write_product(run_trace_file(run_id),
                        env(f"trace-{run_id}", "run_trace", "executed", {"cost_records": records}, [run_id]),
                        f"test: trace {run_id}")


def write_decision(store: GitStore, source: str, next_run: str | None, *, status: str = "executed",
                   action: str = "new", dimension: str = "tom", source_recipe_id: str | None = None,
                   mode: str | None = "batch", perturbation: dict | None = None) -> None:
    payload = {
        "source_run_id": source, "next_run_id": next_run, "template_version": "v1", "backend": "scripted",
        "decision": {"target_dimension": dimension, "action": action, "mode": mode,
                     "source_recipe_id": source_recipe_id, "perturbation": dict(perturbation or {}),
                     "data_synthesis_hint": ""},
        "rationale": "test", "failure": None,
    }
    store.write_product(planner_decision_file(source),
                        env(f"planner-{source}", "planner_decision", status, payload, [source]),
                        f"test: decision {source}")


def decision_json(*, action: str = "new", dimension: str = "tom", mode: str | None = "batch",
                  source_recipe_id: str | None = None, perturbation: dict | None = None,
                  rationale: str = "because the evidence says so") -> str:
    if action != "new" and mode == "batch":
        mode = None
    return json.dumps({
        "target_dimension": dimension, "action": action, "mode": mode,
        "source_recipe_id": source_recipe_id, "perturbation": dict(perturbation or {}),
        "data_synthesis_hint": "", "rationale": rationale,
    })
```

新建 `tests/test_ts_config.py`:

```python
"""scheduler.yaml 与 training_defaults.yaml:默认值、严格解析、信封读写、安装不覆盖。"""
import copy

import pytest

from controlplane.config import read_config, write_config
from controlplane.paths import scheduler_config_file, training_defaults_file
from mindtrainer.data_spec import required_stages
from mindtrainer.training_config import TrainingConfig, validate_training_config
from trainscheduler.config import (
    DEFAULT_DATA_SPEC,
    DEFAULT_SCHEDULER_CONFIG,
    DEFAULT_TRAINING_CONFIG,
    SchedulerConfig,
    check_perturbable_key,
    default_training_defaults_payload,
    install_default_configs,
    load_scheduler_config,
    load_training_defaults,
    parse_scheduler_config,
    parse_training_defaults,
)
from trainscheduler.errors import SchedulerConfigError, SchedulerError
from ts_fixtures import make_store


def test_scheduler_defaults():
    c = DEFAULT_SCHEDULER_CONFIG
    assert (c.confirm_directives, c.allow_interact, c.accept_mock_evidence) == (True, False, False)
    assert c.perturbable == ("opd_coefficient", "sft_steps", "grpo_steps", "grpo_config.learning_rate")
    assert (c.min_gain, c.cost_gain_window, c.recent_decision_window, c.max_plan_attempts) == (0.01, 5, 5, 3)
    assert c.template_version == "v1"


def test_an_empty_mapping_parses_to_the_defaults_and_to_dict_round_trips():
    assert parse_scheduler_config({}) == DEFAULT_SCHEDULER_CONFIG
    assert parse_scheduler_config(DEFAULT_SCHEDULER_CONFIG.to_dict()) == DEFAULT_SCHEDULER_CONFIG


def test_given_keys_override_the_defaults():
    c = parse_scheduler_config({"confirm_directives": False, "min_gain": 0, "perturbable": ["reward.gamma"]})
    assert (c.confirm_directives, c.min_gain, c.perturbable) == (False, 0.0, ("reward.gamma",))
    assert isinstance(c.min_gain, float)


@pytest.mark.parametrize(
    "raw, fragment",
    [
        ({"confirm_directive": True}, "unknown key"),
        ({"confirm_directives": "yes"}, "scheduler.confirm_directives must be a bool"),
        ({"allow_interact": 1}, "scheduler.allow_interact must be a bool"),
        ({"accept_mock_evidence": None}, "scheduler.accept_mock_evidence must be a bool"),
        ({"min_gain": "0.01"}, "scheduler.min_gain must be a finite number"),
        ({"min_gain": True}, "scheduler.min_gain must be a finite number"),
        ({"min_gain": float("nan")}, "scheduler.min_gain must be a finite number"),
        ({"cost_gain_window": 0}, "scheduler.cost_gain_window must be in [1, 100]"),
        ({"cost_gain_window": 2.0}, "scheduler.cost_gain_window must be an int"),
        ({"recent_decision_window": 101}, "scheduler.recent_decision_window must be in [1, 100]"),
        ({"max_plan_attempts": 0}, "scheduler.max_plan_attempts must be in [1, 10]"),
        ({"max_plan_attempts": True}, "scheduler.max_plan_attempts must be an int"),
        ({"template_version": ""}, "scheduler.template_version must be a non-empty str"),
        ({"perturbable": "opd_coefficient"}, "scheduler.perturbable must be a list"),
        ({"perturbable": ["opd_coefficient", "opd_coefficient"]}, "duplicate"),
        ({"perturbable": ["nope"]}, "not a training_config field"),
        ({"perturbable": ["grpo_config..lr"]}, "dotted path"),
        ({"perturbable": [3]}, "dotted path"),
        ({"perturbable": ["sft_steps.x"]}, "can be descended into"),
        ({"perturbable": ["base_checkpoint_ref"]}, "never perturbable"),
        ({"perturbable": ["mode"]}, "never perturbable"),
        ({"perturbable": ["stage_manifest_refs"]}, "never perturbable"),
    ],
)
def test_bad_scheduler_config_names_the_field(raw, fragment):
    with pytest.raises(SchedulerConfigError, match=fragment.replace("[", r"\[").replace("]", r"\]")):
        parse_scheduler_config(raw)


def test_a_non_mapping_is_rejected():
    with pytest.raises(SchedulerConfigError, match="must be a mapping"):
        parse_scheduler_config(["confirm_directives"])


def test_config_errors_are_not_scheduler_errors():
    # 配置错不该把某个 run 的规划永久判 failed:处理器把 SchedulerError 记成 T3,SchedulerConfigError 原样上抛
    assert not issubclass(SchedulerConfigError, SchedulerError)


@pytest.mark.parametrize("key", ["grpo_config.learning_rate", "grpo_config.anything", "reward.gamma",
                                 "opd_coefficient", "max_resume_attempts"])
def test_accepted_perturbable_keys(key):
    assert check_perturbable_key(key) is None


def test_the_default_training_config_validates_with_per_run_manifest_ids():
    cfg = TrainingConfig.from_dict({**DEFAULT_TRAINING_CONFIG,
                                    "stage_manifest_refs": {"sft": "dsm-run-001-sft", "grpo_opd": "dsm-run-001-grpo_opd"}})
    assert validate_training_config(cfg) is None
    assert required_stages(cfg) == ["sft", "grpo_opd"]


def test_the_default_training_defaults_parse():
    d = parse_training_defaults(default_training_defaults_payload())
    assert d.training_config == DEFAULT_TRAINING_CONFIG
    assert d.data_spec.to_dict() == DEFAULT_DATA_SPEC
    assert d.to_dict() == default_training_defaults_payload()


def _defaults(**cfg_changes):
    raw = default_training_defaults_payload()
    raw["training_config"].update(cfg_changes)
    return raw


@pytest.mark.parametrize(
    "raw, fragment",
    [
        ({"training_config": {}}, "missing key"),
        ({**default_training_defaults_payload(), "extra": 1}, "unknown key"),
        ({"training_config": [], "data_spec": DEFAULT_DATA_SPEC}, "training_config must be a mapping"),
        (_defaults(stage_manifest_refs={"sft": "dsm-run-001-sft"}), "stage_manifest_refs must not be set"),
        (_defaults(learning_rate=1e-5), "unknown field"),
        (_defaults(mode="interact"), "mode must be 'batch'"),
        (_defaults(sft_steps="2"), "sft_steps must be an int"),
        (_defaults(group_size=1), "group_size 1 is below min_group_size 2"),
        (_defaults(reward={"components": ["goal"], "attribution": "judge", "combination": "sum"}),
         "judge_model_id is required"),
    ],
)
def test_bad_training_defaults_name_the_problem(raw, fragment):
    with pytest.raises(SchedulerConfigError, match=fragment):
        parse_training_defaults(raw)


def test_a_missing_required_field_is_named():
    raw = default_training_defaults_payload()
    del raw["training_config"]["teacher_model_ref"]
    with pytest.raises(SchedulerConfigError, match="missing field 'teacher_model_ref'"):
        parse_training_defaults(raw)


def test_a_bad_data_spec_is_named():
    raw = default_training_defaults_payload()
    raw["data_spec"]["scenario_count"] = "4"
    with pytest.raises(SchedulerConfigError, match="training_defaults.data_spec.scenario_count must be an int"):
        parse_training_defaults(raw)


def test_a_data_spec_too_small_for_the_config_is_rejected():
    raw = default_training_defaults_payload()
    raw["data_spec"]["scenario_count"] = 1  # groups_per_step 是 2
    with pytest.raises(SchedulerConfigError, match="below groups_per_step 2"):
        parse_training_defaults(raw)


def test_parse_does_not_alias_the_input():
    raw = default_training_defaults_payload()
    d = parse_training_defaults(raw)
    raw["training_config"]["sft_steps"] = 99
    assert d.training_config["sft_steps"] == 2


def test_install_writes_both_config_envelopes(tmp_path):
    store = make_store(tmp_path)
    assert install_default_configs(store) == [scheduler_config_file(), training_defaults_file()]
    for rel in (scheduler_config_file(), training_defaults_file()):
        e = store.read_product(rel)
        assert (e.type, e.status) == ("config", "effective")
    assert load_scheduler_config(store) == DEFAULT_SCHEDULER_CONFIG
    assert load_training_defaults(store).training_config == DEFAULT_TRAINING_CONFIG


def test_install_never_overwrites_a_config_a_human_changed(tmp_path):
    store = make_store(tmp_path)
    write_config(store, scheduler_config_file(), "scheduler-config", {"confirm_directives": False}, "human")
    assert install_default_configs(store) == [training_defaults_file()]
    assert read_config(store, scheduler_config_file()) == {"confirm_directives": False}
    assert install_default_configs(store) == []
    assert load_scheduler_config(store).confirm_directives is False


def test_a_missing_scheduler_config_means_the_defaults(tmp_path):
    assert load_scheduler_config(make_store(tmp_path)) == DEFAULT_SCHEDULER_CONFIG


def test_a_missing_training_defaults_is_an_error_that_says_how_to_fix_it(tmp_path):
    with pytest.raises(SchedulerConfigError, match="install_default_configs"):
        load_training_defaults(make_store(tmp_path))


def test_load_parses_strictly(tmp_path):
    store = make_store(tmp_path)
    write_config(store, scheduler_config_file(), "scheduler-config", {"min_gain": "high"}, "human")
    with pytest.raises(SchedulerConfigError, match="scheduler.min_gain"):
        load_scheduler_config(store)


def test_the_config_dataclass_is_frozen():
    with pytest.raises(Exception):
        SchedulerConfig().min_gain = 0.5  # type: ignore[misc]


def test_the_defaults_dict_is_not_mutated_by_callers():
    before = copy.deepcopy(DEFAULT_TRAINING_CONFIG)
    payload = default_training_defaults_payload()
    payload["training_config"]["reward"]["components"].append("x")
    assert DEFAULT_TRAINING_CONFIG == before
```

- [ ] **Step 4: 运行,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_ts_config.py`
Expected: 收集阶段报错 `ModuleNotFoundError: No module named 'trainscheduler'`,`1 error`。

- [ ] **Step 5: 写实现**

新建 `src/trainscheduler/__init__.py`:

```python
"""TrainScheduler:装配规划上下文、领域校验、动作解析、构造指令、配方提名(闭环集成设计 §7.2、§6.3)。

依赖方向:controlplane、recipestore、trainplanner、mindtrainer.training_config、mindtrainer.data_spec。
controlplane 不导入本包;本包不导入 diagnostic、simulator。
"""
from __future__ import annotations
```

新建 `src/trainscheduler/errors.py`:

```python
"""TrainScheduler 的错误类型。

SchedulerError 及其子类都是确定性失败:同样的树、同样的配置重来还会失败。规划处理器把它们
记成 planner_decision 的 failed 与一份 T3 失败记录。SchedulerConfigError 故意不继承 SchedulerError:
配置写错是人改一下就好的事,不该把某个 run 的规划永久判死(R5 没有自动恢复),所以它原样上抛。
"""
from __future__ import annotations


class SchedulerError(Exception):
    """确定性失败的基类。reason 是给人看的一句话。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ContextAssemblyError(SchedulerError):
    """源 run 的候选、诊断或指令缺失或形状不对,装配不出规划上下文。"""


class ResolveError(SchedulerError):
    """规划决策解析不成一份训练配置(配方不在、扰动套不上、没有可用的默认配置)。"""


class DirectiveConstructionError(SchedulerError):
    """构造出的指令过不了 validate_training_config 或 data_spec 的交叉校验,或目标位置已有指令。"""


class RunIdOrderError(SchedulerError):
    """防御性断言:树里已有 run-<数字> 目录,其编号不小于新分配的编号(例如人手建了 run-050,算出的新编号也是 050)。
    正常路径走不到(next_run_id 取最大编号加一);走到说明树被人手工改过,判确定性失败留痕,不覆盖那个目录。"""


class SchedulerConfigError(ValueError):
    """scheduler.yaml 或 training_defaults.yaml 的类型错、未知键、缺必填键。信息里点名字段。"""
```

新建 `src/trainscheduler/recipes_port.py`:

```python
"""本包对 RecipeStore(计划 3)的全部依赖都经这里导入。计划 3 的导入路径若有变化,只改这一个文件。"""
from __future__ import annotations

from recipestore.gain import GainResult, compute_gain, read_report_payload, regression_flags
from recipestore.ids import recipe_id_for, run_id_of
from recipestore.library import RecipeNotFoundError, RecipeView, find_recipes, get_recipe
from recipestore.validate import ReproReport, validate_reproducibility

__all__ = [
    "GainResult",
    "RecipeNotFoundError",
    "RecipeView",
    "ReproReport",
    "compute_gain",
    "find_recipes",
    "get_recipe",
    "read_report_payload",
    "recipe_id_for",
    "regression_flags",
    "run_id_of",
    "validate_reproducibility",
]
```

新建 `src/trainscheduler/config.py`:

```python
"""调度器的两份配置:config/scheduler.yaml 与 config/training_defaults.yaml(设计 §7.2、§11)。

两份都是 type: config 的信封,经 controlplane.config.read_config / write_config 读写。解析严格:
类型错、未知键一律抛 SchedulerConfigError,信息里点名字段。

- scheduler.yaml 的键都可省略,省略的取默认值;文件不在时整份取默认值。
- training_defaults.yaml 两个键(training_config、data_spec)都必填;文件不在抛 SchedulerConfigError。
"""
from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass, fields

from controlplane.config import read_config, write_config
from controlplane.gitstore import GitStore
from controlplane.paths import scheduler_config_file, training_defaults_file
from mindtrainer.data_spec import (
    DataSpec,
    InvalidDataSpecError,
    check_data_spec_against_config,
    parse_data_spec,
    required_stages,
)
from mindtrainer.training_config import (
    InvalidTrainingConfigError,
    TrainingConfig,
    validate_training_config,
)
from trainscheduler.errors import SchedulerConfigError
from trainplanner.templates import PLANNER_V1_VERSION

SCHEDULER_CONFIG_ID = "scheduler-config"
TRAINING_DEFAULTS_ID = "training-defaults"

# 扰动键语法:对 training_config 的点路径,每段是标识符,例如 opd_coefficient、grpo_config.learning_rate
_SEGMENT = r"[A-Za-z_][A-Za-z0-9_]*"
_PERTURB_KEY = re.compile(rf"^{_SEGMENT}(\.{_SEGMENT})*$")
# 可以往下走一层的 training_config 字段(值本身是映射)
NESTED_FIELDS = ("grpo_config", "reward")
_TOP_FIELDS = tuple(f.name for f in fields(TrainingConfig))


def check_perturbable_key(key: object) -> str | None:
    """扰动键的语法:点路径;首段必须是 TrainingConfig 的字段;多段时首段必须是 grpo_config 或 reward。"""
    if not isinstance(key, str) or not _PERTURB_KEY.match(key):
        return f"perturbable key {key!r} must be a dotted path of identifiers"
    head, _, rest = key.partition(".")
    if head not in _TOP_FIELDS:
        return f"perturbable key {key!r}: {head!r} is not a training_config field"
    if rest and head not in NESTED_FIELDS:
        return f"perturbable key {key!r}: only {list(NESTED_FIELDS)} can be descended into"
    if head in ("mode", "trained_slot", "base_checkpoint_ref", "teacher_model_ref",
                "stage_manifest_refs", "bindings", "scenario_ids"):
        return f"perturbable key {key!r}: {head!r} anchors the run and is never perturbable"
    return None


@dataclass(frozen=True)
class SchedulerConfig:
    confirm_directives: bool = True
    allow_interact: bool = False
    accept_mock_evidence: bool = False
    perturbable: tuple[str, ...] = (
        "opd_coefficient",
        "sft_steps",
        "grpo_steps",
        "grpo_config.learning_rate",
    )
    min_gain: float = 0.01
    cost_gain_window: int = 5
    recent_decision_window: int = 5
    max_plan_attempts: int = 3
    template_version: str = PLANNER_V1_VERSION

    def to_dict(self) -> dict:
        return {
            "confirm_directives": self.confirm_directives,
            "allow_interact": self.allow_interact,
            "accept_mock_evidence": self.accept_mock_evidence,
            "perturbable": list(self.perturbable),
            "min_gain": self.min_gain,
            "cost_gain_window": self.cost_gain_window,
            "recent_decision_window": self.recent_decision_window,
            "max_plan_attempts": self.max_plan_attempts,
            "template_version": self.template_version,
        }


DEFAULT_SCHEDULER_CONFIG = SchedulerConfig()
_SCHEDULER_KEYS = tuple(f.name for f in fields(SchedulerConfig))


def _label(key: str) -> str:
    return f"scheduler.{key}"


def _bool(raw: dict, key: str) -> bool:
    v = raw[key]
    if not isinstance(v, bool):
        raise SchedulerConfigError(f"{_label(key)} must be a bool, got {type(v).__name__}")
    return v


def _int(raw: dict, key: str, low: int, high: int) -> int:
    v = raw[key]
    if isinstance(v, bool) or not isinstance(v, int):
        raise SchedulerConfigError(f"{_label(key)} must be an int, got {type(v).__name__}")
    if not (low <= v <= high):
        raise SchedulerConfigError(f"{_label(key)} must be in [{low}, {high}], got {v}")
    return v


def parse_scheduler_config(raw: object) -> SchedulerConfig:
    if not isinstance(raw, dict):
        raise SchedulerConfigError(f"scheduler config must be a mapping, got {type(raw).__name__}")
    unknown = sorted(repr(k) for k in raw if k not in _SCHEDULER_KEYS)
    if unknown:
        raise SchedulerConfigError(
            f"scheduler config has unknown key(s) {unknown}; allowed {list(_SCHEDULER_KEYS)}"
        )
    values = DEFAULT_SCHEDULER_CONFIG.to_dict()
    values.update(raw)
    perturbable = values["perturbable"]
    if not isinstance(perturbable, list):
        raise SchedulerConfigError(
            f"{_label('perturbable')} must be a list, got {type(perturbable).__name__}"
        )
    for key in perturbable:
        reason = check_perturbable_key(key)
        if reason is not None:
            raise SchedulerConfigError(f"{_label('perturbable')}: {reason}")
    if len(set(perturbable)) != len(perturbable):
        raise SchedulerConfigError(f"{_label('perturbable')} has duplicate keys")
    min_gain = values["min_gain"]
    if isinstance(min_gain, bool) or not isinstance(min_gain, (int, float)) or not math.isfinite(min_gain):
        raise SchedulerConfigError(f"{_label('min_gain')} must be a finite number, got {min_gain!r}")
    template_version = values["template_version"]
    if not isinstance(template_version, str) or not template_version.strip():
        raise SchedulerConfigError(f"{_label('template_version')} must be a non-empty str")
    return SchedulerConfig(
        confirm_directives=_bool(values, "confirm_directives"),
        allow_interact=_bool(values, "allow_interact"),
        accept_mock_evidence=_bool(values, "accept_mock_evidence"),
        perturbable=tuple(perturbable),
        min_gain=float(min_gain),
        cost_gain_window=_int(values, "cost_gain_window", 1, 100),
        recent_decision_window=_int(values, "recent_decision_window", 1, 100),
        max_plan_attempts=_int(values, "max_plan_attempts", 1, 10),
        template_version=template_version,
    )


def load_scheduler_config(store: GitStore) -> SchedulerConfig:
    rel = scheduler_config_file()
    if not store.exists(rel):
        return DEFAULT_SCHEDULER_CONFIG
    return parse_scheduler_config(read_config(store, rel))


# ---- training_defaults.yaml ----

_ZERO_SHA = "0" * 64

DEFAULT_TRAINING_CONFIG: dict = {
    "mode": "batch",
    "trained_slot": "zengen",
    # 没有任何已晋级模型时的初始基座。真实部署前要换成开源底座权重的真实引用与哈希。
    "base_checkpoint_ref": {
        "uri": "file:///compute/checkpoints/initial-base.safetensors",
        "sha256": _ZERO_SHA, "size_bytes": 0, "fetch_hint": None,
    },
    "teacher_model_ref": {
        "uri": "file:///compute/checkpoints/opd-teacher.safetensors",
        "sha256": _ZERO_SHA, "size_bytes": 0, "fetch_hint": None,
    },
    "reward": {
        "components": ["goal"],
        "attribution": "outcome_split",
        "combination": "sum",
        "outcome_rules": {"goal": ["agreed", "同意"]},
        "gamma": 1.0,
        "judge_model_id": None,
        "rubric_version": None,
    },
    "sft_steps": 2,
    "grpo_steps": 2,
    "sft_batch_size": 1,
    "groups_per_step": 2,
    "group_size": 2,
    "grpo_config": {"learning_rate": 1.0e-5},
    "opd_coefficient": 0.1,
    "checkpoint_every_steps": 1,
    "allow_resume": True,
}

DEFAULT_DATA_SPEC: dict = {
    "scenario_count": 4,
    "seed": 0,
    "purpose": "training",
    "stages": {"sft": {"group_size": 1}, "grpo_opd": {"group_size": 2}},
}

_DEFAULTS_KEYS = ("training_config", "data_spec")
# 每轮各自的值,不能写进默认配置:清单 id 由 construct_directive 按新 run id 写
_PER_RUN_KEYS = ("stage_manifest_refs",)


@dataclass(frozen=True)
class TrainingDefaults:
    training_config: dict  # TrainingConfig.to_dict() 的形状,不含 stage_manifest_refs
    data_spec: DataSpec

    def to_dict(self) -> dict:
        return {"training_config": copy.deepcopy(self.training_config), "data_spec": self.data_spec.to_dict()}


def _placeholder_refs(cfg: TrainingConfig) -> dict[str, str]:
    return {stage: f"dsm-run-000-{stage}" for stage in required_stages(cfg)}


def parse_training_defaults(raw: object) -> TrainingDefaults:
    """严格解析。training_config 必须能被 TrainingConfig.from_dict 读出,补上占位清单 id 后过
    validate_training_config,且 data_spec 与它交叉校验通过:默认配置本身就跑不通的话,
    每一条 new 指令都会失败。"""
    if not isinstance(raw, dict):
        raise SchedulerConfigError(f"training defaults must be a mapping, got {type(raw).__name__}")
    missing = [k for k in _DEFAULTS_KEYS if k not in raw]
    unknown = sorted(repr(k) for k in raw if k not in _DEFAULTS_KEYS)
    if missing or unknown:
        raise SchedulerConfigError(
            f"training defaults: missing key(s) {missing}, unknown key(s) {unknown}"
        )
    cfg_raw = raw["training_config"]
    if not isinstance(cfg_raw, dict):
        raise SchedulerConfigError(
            f"training_defaults.training_config must be a mapping, got {type(cfg_raw).__name__}"
        )
    for key in _PER_RUN_KEYS:
        if key in cfg_raw:
            raise SchedulerConfigError(
                f"training_defaults.training_config.{key} must not be set; it is written per run"
            )
    unknown_fields = sorted(repr(k) for k in cfg_raw if k not in _TOP_FIELDS)
    if unknown_fields:
        raise SchedulerConfigError(
            f"training_defaults.training_config has unknown field(s) {unknown_fields}"
        )
    try:
        cfg = TrainingConfig.from_dict(cfg_raw)
    except KeyError as exc:
        raise SchedulerConfigError(f"training_defaults.training_config is missing field {exc}") from exc
    except (TypeError, AttributeError, InvalidTrainingConfigError) as exc:
        raise SchedulerConfigError(f"training_defaults.training_config is malformed: {exc}") from exc
    if cfg.mode != "batch":
        raise SchedulerConfigError(
            f"training_defaults.training_config.mode must be 'batch', got {cfg.mode!r}"
        )
    # 先在没有清单 id 的配置上校验:类型错(sft_steps 写成字符串之类)要在 required_stages 比较数值之前
    # 挡下,不能让 TypeError 漏出去。唯一允许的违规是"缺 stage_manifest_refs",它在下一步补上占位 id。
    reason = validate_training_config(cfg)
    if reason is not None and not reason.startswith("stage_manifest_refs["):
        raise SchedulerConfigError(f"training_defaults.training_config: {reason}")
    probe = TrainingConfig.from_dict({**cfg_raw, "stage_manifest_refs": _placeholder_refs(cfg)})
    reason = validate_training_config(probe)
    if reason is not None:
        raise SchedulerConfigError(f"training_defaults.training_config: {reason}")
    try:
        spec = parse_data_spec(raw["data_spec"])
    except InvalidDataSpecError as exc:
        raise SchedulerConfigError(f"training_defaults.{exc}") from exc
    reason = check_data_spec_against_config(spec, probe, required_stages(probe))
    if reason is not None:
        raise SchedulerConfigError(f"training_defaults: {reason}")
    return TrainingDefaults(training_config=copy.deepcopy(cfg_raw), data_spec=spec)


def load_training_defaults(store: GitStore) -> TrainingDefaults:
    rel = training_defaults_file()
    if not store.exists(rel):
        raise SchedulerConfigError(
            f"{rel} not found; run trainscheduler.config.install_default_configs(store) first"
        )
    return parse_training_defaults(read_config(store, rel))


def default_training_defaults_payload() -> dict:
    return {"training_config": copy.deepcopy(DEFAULT_TRAINING_CONFIG), "data_spec": copy.deepcopy(DEFAULT_DATA_SPEC)}


def install_default_configs(store: GitStore) -> list[str]:
    """把两份默认配置写进树。已存在的文件一律不动(哪怕内容与默认不同):人改过的配置不被覆盖。
    返回本次实际写入的相对路径。"""
    written: list[str] = []
    for rel, config_id, payload in (
        (scheduler_config_file(), SCHEDULER_CONFIG_ID, DEFAULT_SCHEDULER_CONFIG.to_dict()),
        (training_defaults_file(), TRAINING_DEFAULTS_ID, default_training_defaults_payload()),
    ):
        if store.exists(rel):
            continue
        write_config(store, rel, config_id, payload, f"trainscheduler: install default {rel.rsplit('/', 1)[-1]}")
        written.append(rel)
    return written
```

- [ ] **Step 6: 运行,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_ts_config.py`
Expected: `54 passed`,无警告。

- [ ] **Step 7: 全量回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: **B+54(1597)** passed,无警告(撰写时实测数见括号)。再跑 `git status --short`:除原本就有的 `cli/package*.json` 外,只应出现本任务 Files 里的文件。

- [ ] **Step 8: 提交**

```bash
git add pyproject.toml
git add src/trainscheduler/__init__.py
git add src/trainscheduler/errors.py
git add src/trainscheduler/recipes_port.py
git add src/trainscheduler/config.py
git add tests/ts_fixtures.py
git add tests/test_ts_config.py
git commit -m "feat: trainscheduler package skeleton, recipestore import port, strict scheduler.yaml and training_defaults.yaml"
```

---

### Task 61: run id 的分配

**Files:**
- Create: `src/trainscheduler/run_ids.py`(73 行)
- Test: `tests/test_ts_run_ids.py`(30 个用例)

**Interfaces:**
- Consumes: `controlplane.paths.CONTROL_PLANE_ROOT`;`GitStore.repo_root`;任务 60 的 `RunIdOrderError`。
- Produces(任务 62、65 依赖):`run_number(name) -> int | None`、`format_run_id(n) -> "run-NNN"`、`next_run_id(existing) -> str`(纯函数)、`blocking_run_dirs(existing, new_id) -> list[str]`(纯函数)、`list_run_dirs(store) -> list[str]`、`allocate_run_id(store) -> str`(树扫描;已有 `run-<数字>` 目录编号不小于新编号时抛任务 60 的 `RunIdOrderError`,正常路径走不到)、`run_order_key(name)`(窗口排序键)。

**设计要点:**
- **只数 `run-<数字>`**:正则 `^run-([0-9]+)$`;`run-1b`、`exp-001`、`RUN-001`、`run-+1` 都不参与(有测试)。`run-1` 这种不补零的老名字照样按数值参与,`next_run_id(["run-9", "run-10"]) == "run-011"`。
- **取最大序号加一**,不是"最后一个加一";空树为 `run-001`。至少三位补零,超过 999 自然变成四位(补零只为可读,最大 run 按数值判,四位与三位混排不影响)。
- **扫描的是 `runs/` 下的目录**,不是文件;不创建目录,目录随指令一起出现。
- **防御性断言**(Ruling 22):`allocate_run_id` 用纯函数 `blocking_run_dirs` 找出编号不小于新编号的 `run-<数字>` 目录,非空就抛 `RunIdOrderError`;其他名字永不挡路。测试:`blocking_run_dirs(["run-9", "run-010", "zz", "test-x"], "run-011") == []`、`blocking_run_dirs(["run-999"], "run-1000") == []`、`blocking_run_dirs(["run-050"], "run-050") == ["run-050"]`(编号相等就算挡);`allocate_run_id` 在 `run-9`、`run-9 + run-010 + test-x + zz`、`run-001 + test-x`、`run-999` 四种树上分别成功分配 `run-010`、`run-011`、`run-002`、`run-1000`;只有人手建了同号目录(用 `monkeypatch` 让 `next_run_id` 看漏它)时才抛。
- `run_order_key` 给装配上下文的窗口用:`run-<数字>` 按编号数值排在后面,其他名字排在前面、彼此按名字排(Ruling 11)。

- [ ] **Step 1: 前置检查**

运行上文"前置检查命令"。Expected: `preflight ok`。

- [ ] **Step 2: 写失败测试**

新建 `tests/test_ts_run_ids.py`:

```python
"""run id 的分配:run-NNN,至少三位补零,取现有最大序号加一;只数 run-<数字>。"""
import pytest

from controlplane.paths import run_dir
from trainscheduler.errors import RunIdOrderError, SchedulerError
from trainscheduler.run_ids import (
    allocate_run_id,
    blocking_run_dirs,
    format_run_id,
    list_run_dirs,
    next_run_id,
    run_number,
    run_order_key,
)
from ts_fixtures import make_store, write_directive


@pytest.mark.parametrize(
    "name, expected",
    [("run-001", 1), ("run-010", 10), ("run-1", 1), ("run-1000", 1000),
     ("run-", None), ("run-1b", None), ("exp-001", None), ("RUN-001", None), ("run-+1", None), ("run--1", None)],
)
def test_run_number_only_reads_run_dash_digits(name, expected):
    assert run_number(name) == expected


def test_format_pads_to_at_least_three_digits():
    assert [format_run_id(n) for n in (1, 9, 10, 999, 1000)] == [
        "run-001", "run-009", "run-010", "run-999", "run-1000"]


@pytest.mark.parametrize("bad", [0, -1, True, 1.0, "1"])
def test_format_rejects_non_positive_ints(bad):
    with pytest.raises(ValueError):
        format_run_id(bad)


def test_next_run_id_on_nothing_is_run_001():
    assert next_run_id([]) == "run-001"


def test_next_run_id_takes_the_largest_number_not_the_last_string():
    assert next_run_id(["run-001", "run-010", "run-002"]) == "run-011"
    assert next_run_id(["run-9", "run-10"]) == "run-011"


def test_next_run_id_ignores_hand_named_runs():
    assert next_run_id(["exp-a", "run-1b", "zzz"]) == "run-001"
    assert next_run_id(["run-003", "manual-099", "run-03x"]) == "run-004"


def test_allocate_scans_the_tree(tmp_path):
    store = make_store(tmp_path)
    assert allocate_run_id(store) == "run-001"
    for run_id in ("run-001", "run-010", "my-experiment"):
        write_directive(store, run_id)
    assert list_run_dirs(store) == ["my-experiment", "run-001", "run-010"]
    assert allocate_run_id(store) == "run-011"


def test_allocate_counts_directories_not_files(tmp_path):
    store = make_store(tmp_path)
    (store.repo_root / run_dir("run-005")).mkdir(parents=True)
    (store.repo_root / "control-plane/runs/run-900.yaml").write_text("x", encoding="utf-8")
    assert allocate_run_id(store) == "run-006"


def test_order_key_puts_numbered_runs_in_numeric_order_after_the_rest():
    names = ["run-010", "manual", "run-002", "run-1000", "run-999"]
    assert sorted(names, key=run_order_key) == ["manual", "run-002", "run-010", "run-999", "run-1000"]


def test_blocking_run_dirs_uses_numbers_and_ignores_other_names():
    assert blocking_run_dirs(["run-001", "manual", "exp-a"], "run-002") == []
    assert blocking_run_dirs(["run-9", "run-010", "zz", "test-x"], "run-011") == []
    assert blocking_run_dirs(["run-999"], "run-1000") == []  # 数值序:四位排在三位之后


def test_only_a_run_dir_numbered_at_least_the_new_id_blocks():
    assert blocking_run_dirs(["run-050"], "run-050") == ["run-050"]  # 编号相等就算挡
    assert blocking_run_dirs(["run-50", "run-051", "run-049"], "run-050") == ["run-051", "run-50"]


def test_blocking_run_dirs_needs_a_run_id():
    with pytest.raises(ValueError):
        blocking_run_dirs(["run-001"], "zz")


@pytest.mark.parametrize(
    "dirs, expected",
    [
        (["run-9"], "run-010"),                       # 不补零的老名字照样按数值参与
        (["run-9", "run-010", "test-x", "zz"], "run-011"),
        (["run-001", "test-x"], "run-002"),           # 手工起的名字不参与比较
        (["run-999"], "run-1000"),                    # 超过 999 轮按数值继续
    ],
)
def test_allocate_succeeds_on_trees_that_used_to_look_out_of_order(tmp_path, dirs, expected):
    store = make_store(tmp_path)
    for name in dirs:
        (store.repo_root / run_dir(name)).mkdir(parents=True)
    assert allocate_run_id(store) == expected


def test_allocate_refuses_when_a_hand_made_dir_has_the_same_number(tmp_path, monkeypatch):
    """正常路径走不到(新编号恒为最大编号加一)。这里让编号计算"看漏"一个人手建的 run-050,
    模拟树被手工改过:分配器不覆盖它,判确定性失败。"""
    import trainscheduler.run_ids as rid

    store = make_store(tmp_path)
    (store.repo_root / run_dir("run-050")).mkdir(parents=True)
    monkeypatch.setattr(rid, "next_run_id", lambda existing: "run-050")
    with pytest.raises(RunIdOrderError, match=r"not numerically greater than existing run dir\(s\) \['run-050'\]"):
        allocate_run_id(store)
    assert issubclass(RunIdOrderError, SchedulerError)
```

- [ ] **Step 3: 运行,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_ts_run_ids.py`
Expected: 收集阶段报错 `ModuleNotFoundError: No module named 'trainscheduler.run_ids'`,`1 error`。

- [ ] **Step 4: 写实现**

新建 `src/trainscheduler/run_ids.py`:

```python
"""run id 的分配(设计 §4.1):格式 run-NNN,至少三位补零,取现有最大序号加一。

只数形如 run-<数字> 的 run 目录;人工起的别的名字(exp-a、run-1b、zz)不参与。
计划 8 的 plan_next_round 与这里同一套数值语义(设计 §3.2):只有 run-<数字> 参与比较,按编号取最大;
补零只为可读,超过 999 轮(run-1000)仍按数值排在 run-999 之后。
"""
from __future__ import annotations

import re
from collections.abc import Iterable

from controlplane.gitstore import GitStore
from controlplane.paths import CONTROL_PLANE_ROOT
from trainscheduler.errors import RunIdOrderError

_RUN_ID = re.compile(r"^run-([0-9]+)$")


def run_number(name: str) -> int | None:
    """run-007 -> 7;不是 run-<数字> 的形状返回 None。"""
    match = _RUN_ID.match(name) if isinstance(name, str) else None
    return int(match.group(1)) if match else None


def format_run_id(n: int) -> str:
    if isinstance(n, bool) or not isinstance(n, int) or n < 1:
        raise ValueError(f"run number must be a positive int, got {n!r}")
    return f"run-{n:03d}"


def next_run_id(existing: Iterable[str]) -> str:
    """纯函数:已有名字里最大的 run 序号加一;一个都没有就是 run-001。"""
    numbers = [n for n in (run_number(name) for name in existing) if n is not None]
    return format_run_id(max(numbers, default=0) + 1)


def list_run_dirs(store: GitStore) -> list[str]:
    root = store.repo_root / CONTROL_PLANE_ROOT / "runs"
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def blocking_run_dirs(existing: Iterable[str], new_id: str) -> list[str]:
    """纯函数:编号不小于 new_id 编号的 run-<数字> 目录(按名字排序)。其他名字不参与,永不挡路。
    new_id 必须是 run-<数字>,否则 ValueError。"""
    new_n = run_number(new_id)
    if new_n is None:
        raise ValueError(f"new_id must be run-<digits>, got {new_id!r}")
    return sorted(name for name in existing if (n := run_number(name)) is not None and n >= new_n)


def allocate_run_id(store: GitStore) -> str:
    """扫描 runs/ 下的目录,返回下一个 run id。不创建目录:目录随指令一起出现。

    防御性检查:若已有 run-<数字> 目录的编号不小于新编号,抛 RunIdOrderError(确定性失败),信息里列出
    这些目录。next_run_id 取最大编号加一,所以正常路径走不到;走到说明树被人手工改过
    (例如有人绕过分配器手建了同号目录),宁可失败留痕也不覆盖。"""
    existing = list_run_dirs(store)
    new_id = next_run_id(existing)
    blocking = blocking_run_dirs(existing, new_id)
    if blocking:
        raise RunIdOrderError(
            f"next run id {new_id} is not numerically greater than existing run dir(s) {blocking}; "
            "the tree was edited by hand, refusing to allocate"
        )
    return new_id


def run_order_key(name: str) -> tuple[int, int, str]:
    """窗口取"最近 K 轮"时的排序键:run-<数字> 按编号数值排在后面;别的名字排在前面(彼此按名字排)。"""
    n = run_number(name)
    return (1, n, name) if n is not None else (0, 0, name)
```

- [ ] **Step 5: 运行,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_ts_run_ids.py`
Expected: `30 passed`,无警告。

- [ ] **Step 6: 全量回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: **B+84(1627)** passed,无警告(撰写时实测数见括号)。再跑 `git status --short`:除原本就有的 `cli/package*.json` 外,只应出现本任务 Files 里的文件。

- [ ] **Step 7: 提交**

```bash
git add src/trainscheduler/run_ids.py
git add tests/test_ts_run_ids.py
git commit -m "feat: trainscheduler run id allocation (run-NNN, largest number plus one, hand-named runs ignored)"
```

---

### Task 62: 装配规划上下文 `assemble_context`

**Files:**
- Create: `src/trainscheduler/assemble.py`(236 行)
- Test: `tests/test_ts_assemble.py`(34 个用例)

**Interfaces:**
- Consumes: `controlplane.evidence.diagnostic_grade`;`controlplane.paths.candidate_checkpoint_file` / `planner_decision_file` / `run_dir` / `run_trace_file`;`trainplanner.context` 的全部数据类与 `InvalidContextError`、`CANDIDATE_TERMINAL_STATES`、`RECIPE_TYPES`;`recipes_port.compute_gain` / `find_recipes` / `read_report_payload` / `regression_flags`;任务 60 的 `SchedulerConfig`、`load_scheduler_config`;任务 61 的 `list_run_dirs`、`run_order_key`。
- Produces(任务 64、65、66 依赖):
  - `assemble_context(store, run_id, *, config=None) -> PlanningContext`;确定性失败抛 `ContextAssemblyError`。
  - `directive_file(run_id) -> "control-plane/runs/<run>/directive.yaml"`。
  - `decision_is_executed(store, env) -> bool`:`executed`,或 `pending` 而 `next_run_id` 的指令已在。
  - `regression_mark(flag) -> RegressionMark`、`l1_scores(payload) -> dict[str, float]`、`drop_no_error_label(distribution)`(设计 R21,Ruling 23)、`NO_ERROR_LABEL = ""`。

**设计要点:**
- **输出契约是计划 4 的 `PlanningContext`**,逐字照用。测试把 `assemble_context(...).to_dict()` 的键结构与 `trainplanner.examples.example_context_dict()` 逐层比对(`l1_scores`、`error_type_distribution` 的键是数据,不比),并 JSON 往返后 `from_dict` 回来逐项相等。
- **证据等级一律调 `controlplane.evidence.diagnostic_grade`**;等级是 `stub`(含 `backends` 取值不认识的报告)抛 `ContextAssemblyError`,信息里有 `graded stub`。`mock` / `real` 原样进 `diagnostic.grade`。等级是否"可接受"不在这里判(Ruling 4)。
- **空错误标签丢掉**(设计 R21,Ruling 23):`error_profiles[].error_type_distribution` 里的 `""` 键在构造 `ErrorProfileSummary` 前丢掉,不重新归一化。测试:`{"": 1.0}` → `{}`;`{"": 0.5, "surface_match": 0.5}` → `{"surface_match": 0.5}`;没有空键时原样不变(且不改入参);对着真实 `diagnostic.error_classifier.aggregate_error_profile("l2.x", [""], "static").error_type_distribution` 造报告,装配后是空映射且能被 `PlanningContext.from_dict` 读回。任务 65 另有一条回归测试:报告带 `{"": 1.0}` 时规划照常 `executed`。测试文件导入 `diagnostic` 只为钉住真实形状;`src/trainscheduler` 仍不导入它。
- **成本未记录是 `None`**:一个 run 的 `gpu_hours`、`tokens` 是 `run_trace.cost_records` 里带该键的记录之和;一条都没带就是 `None`,不是 0(设计 §14 第 7 条,计划 4 的 Ruling)。`tokens` 求和后转 `int`。
- **增益一律用 `compute_gain`**(设计 §11 的唯一实现)。`recent_decisions[].gain` 只在后续 run 有候选时才算,否则 `None`。
- **窗口**:只看排序键不大于来源 run 的 run,各取最后 K 条;K 来自 `scheduler.yaml`(Ruling 11)。所以重入时同一来源 run 看到同样的上下文。
- **只读**:不写任何产物(有测试比对 HEAD 与工作区)。
- 计划 4 的数据类在构造时校验;它们抛的 `InvalidContextError` 统一包成 `ContextAssemblyError`,让处理器按确定性失败处理。

- [ ] **Step 1: 前置检查**

运行上文"前置检查命令"。Expected: `preflight ok`。

- [ ] **Step 2: 写失败测试**

新建 `tests/test_ts_assemble.py`:

```python
"""装配规划上下文:形状照计划 4 的契约,stub 证据拒绝,成本未记录为 None,窗口取最近 K 轮。"""
import json

import pytest

from controlplane.config import write_config
from controlplane.paths import scheduler_config_file
from trainplanner.context import PlanningContext
from trainplanner.examples import example_context_dict
from trainscheduler.assemble import (
    assemble_context,
    decision_is_executed,
    drop_no_error_label,
    regression_mark,
)
from trainscheduler.config import SchedulerConfig
from trainscheduler.errors import ContextAssemblyError, SchedulerError
from ts_fixtures import (
    make_store,
    write_candidate,
    write_decision,
    write_directive,
    write_model,
    write_recipe,
    write_report,
    write_round,
    write_trace,
)


@pytest.fixture
def store(tmp_path):
    return make_store(tmp_path)


# 这两个字段是"名字 → 数"的映射,键是数据不是结构
_DATA_MAPS = ("l1_scores", "error_type_distribution")


def _keys(d):
    """嵌套字典的键结构(列表取第一个元素),用来与计划 4 的示例比对形状。"""
    if isinstance(d, dict):
        return {k: (None if k in _DATA_MAPS else _keys(v)) for k, v in d.items()}
    if isinstance(d, list):
        return [_keys(d[0])] if d else []
    return None


def _full_tree(store):
    write_round(store, "run-001", {"tom": 0.40, "empathy": 0.70}, model_n=1)
    write_recipe(store, "run-001", entries=[{"run_id": "run-002", "gain": 0.02, "promoted": True,
                                             "regressed": False, "used_as": "clone"}])
    write_decision(store, "run-001", "run-002", action="clone", source_recipe_id="recipe-run-001", mode=None)
    write_round(store, "run-002", {"tom": 0.45, "empathy": 0.70}, baseline_ref="zengen-v1",
                flags=[{"dimension": "pragmatics", "kind": "social_regression", "drop": 0.06,
                        "baseline_version": "zengen-v1", "beyond_ci": True, "level": "l1", "track": "static"}])
    write_report(store, "run-002", {"tom": 0.45, "empathy": 0.70}, baseline_ref="zengen-v1",
                 flags=[{"dimension": "pragmatics", "kind": "social_regression", "drop": 0.06}],
                 error_profiles=[{"l2_dimension": "tom.false_belief", "track": "static",
                                  "error_type_distribution": {"literal_reading": 1.0}}])
    write_trace(store, "run-002", [{"run_id": "run-002", "task_id": "plan-1", "cost_type": "external_inference",
                                    "tokens": 1200, "calls": 1}])


def test_the_assembled_context_has_exactly_the_contract_shape(store):
    _full_tree(store)
    ctx = assemble_context(store, "run-002")
    d = ctx.to_dict()
    assert _keys(d) == _keys(example_context_dict())
    assert PlanningContext.from_dict(json.loads(json.dumps(d))) == ctx


def test_candidate_and_diagnostic_are_read_from_the_source_run(store):
    _full_tree(store)
    ctx = assemble_context(store, "run-002")
    assert ctx.run_id == "run-002"
    assert ctx.candidate.to_dict() == {"id": "cand-run-002", "status": "effective", "reject_reason": None}
    assert ctx.diagnostic.l1_scores == {"tom": 0.45, "empathy": 0.70}  # l2 分数不进 l1_scores
    assert ctx.diagnostic.grade == "mock"
    assert ctx.diagnostic.benchmark_version == "bench-v1"
    assert [f.to_dict() for f in ctx.diagnostic.regression_flags] == [
        {"kind": "social_regression", "subject": "pragmatics", "magnitude": 0.06}]
    assert [p.to_dict() for p in ctx.diagnostic.error_profiles] == [
        {"l2_dimension": "tom.false_belief", "error_type_distribution": {"literal_reading": 1.0}}]


def test_a_rejected_candidate_carries_its_reason(store):
    write_round(store, "run-001", {"tom": 0.4}, candidate_status="rejected")
    write_candidate(store, "run-001", "rejected", reject_reason="reward hacking suspected")
    assert assemble_context(store, "run-001").candidate.reject_reason == "reward hacking suspected"


def test_a_real_report_is_graded_real(store):
    write_round(store, "run-001", {"tom": 0.4}, backends="real")
    assert assemble_context(store, "run-001").diagnostic.grade == "real"


def test_recipes_are_the_effective_ones_in_track_order_without_payload(store):
    _full_tree(store)
    write_recipe(store, "run-000", status="deprecated")
    ctx = assemble_context(store, "run-002")
    assert [r.to_dict() for r in ctx.recipes] == [
        {"id": "recipe-run-001", "recipe_type": "batch", "dimension": "tom", "status": "effective",
         "mean_gain": 0.02, "n_reuse": 1}]


def test_recent_decisions_carry_the_outcome_of_the_round_they_produced(store):
    _full_tree(store)
    [d] = assemble_context(store, "run-002").recent_decisions
    assert d.to_dict() == {
        "decision_id": "planner-run-001", "next_run_id": "run-002", "target_dimension": "tom",
        "action": "clone", "source_recipe_id": "recipe-run-001", "candidate_status": "effective",
        "gain": pytest.approx(0.05),
    }


def test_a_pending_decision_whose_directive_exists_counts_as_executed(store):
    _full_tree(store)
    write_decision(store, "run-001", "run-002", status="pending", action="clone",
                   source_recipe_id="recipe-run-001", mode=None)
    assert decision_is_executed(store, store.read_product("control-plane/runs/run-001/planner_decision.yaml"))
    assert len(assemble_context(store, "run-002").recent_decisions) == 1


def test_pending_without_directive_and_failed_decisions_are_left_out(store):
    write_round(store, "run-001", {"tom": 0.4})
    write_decision(store, "run-001", "run-002", status="pending")
    assert assemble_context(store, "run-001").recent_decisions == []
    write_decision(store, "run-001", None, status="failed")
    assert assemble_context(store, "run-001").recent_decisions == []


def test_a_decision_whose_round_has_no_candidate_yet_has_null_outcome(store):
    write_directive(store, "run-001")
    write_decision(store, "run-001", "run-002", action="new")
    write_directive(store, "run-002")  # 下一轮已有指令,还没有候选
    write_round(store, "run-003", {"tom": 0.4})
    out = [d.to_dict() for d in assemble_context(store, "run-003").recent_decisions]
    assert [(d["next_run_id"], d["candidate_status"], d["gain"]) for d in out] == [("run-002", None, None)]


def test_decisions_recorded_after_the_source_run_are_left_out(store):
    write_round(store, "run-001", {"tom": 0.4})
    write_directive(store, "run-002")
    write_decision(store, "run-002", "run-003", action="new")
    write_directive(store, "run-003")
    assert assemble_context(store, "run-001").recent_decisions == []


def test_unrecorded_costs_are_none_not_zero(store):
    _full_tree(store)
    entries = [c.to_dict() for c in assemble_context(store, "run-002").cost_gain]
    assert entries[0] == {"run_id": "run-001", "target_dimension": "tom", "gpu_hours": None, "tokens": None,
                          "gain": None}
    assert entries[1]["tokens"] == 1200 and entries[1]["gpu_hours"] is None
    assert entries[1]["gain"] == pytest.approx(0.05)


def test_costs_sum_over_records_and_only_known_keys_count(store):
    write_round(store, "run-001", {"tom": 0.4})
    write_trace(store, "run-001", [
        {"task_id": "a", "cost_type": "gpu", "gpu_hours": 1.5},
        {"task_id": "b", "cost_type": "external_inference", "tokens": 100},
        {"task_id": "c", "cost_type": "external_inference", "tokens": 50, "gpu_hours": 0.5},
    ])
    [c] = assemble_context(store, "run-001").cost_gain
    assert (c.gpu_hours, c.tokens) == (2.0, 150)
    assert isinstance(c.tokens, int)


def test_cost_gain_keeps_only_the_last_k_runs_up_to_the_source(store):
    for n in range(1, 9):
        write_round(store, f"run-{n:03d}", {"tom": 0.4})
    write_directive(store, "hand-named")  # 非 run-<数字> 的排在最前,窗口足够大时也会出现
    write_config(store, scheduler_config_file(), "scheduler-config", {"cost_gain_window": 3}, "test")
    ctx = assemble_context(store, "run-007")
    assert [c.run_id for c in ctx.cost_gain] == ["run-005", "run-006", "run-007"]


def test_an_explicit_config_overrides_the_tree(store):
    for n in range(1, 4):
        write_round(store, f"run-{n:03d}", {"tom": 0.4})
    ctx = assemble_context(store, "run-003", config=SchedulerConfig(cost_gain_window=1))
    assert [c.run_id for c in ctx.cost_gain] == ["run-003"]


def test_runs_without_a_directive_are_left_out_of_cost_gain(store):
    write_round(store, "run-002", {"tom": 0.4})
    write_candidate(store, "run-001")
    assert [c.run_id for c in assemble_context(store, "run-002").cost_gain] == ["run-002"]


@pytest.mark.parametrize(
    "setup, fragment",
    [
        (lambda s: write_directive(s, "run-001"), "has no candidate"),
        (lambda s: (write_directive(s, "run-001"), write_candidate(s, "run-001", "pending_review")),
         "pending_review; planning needs"),
        (lambda s: (write_directive(s, "run-001"), write_candidate(s, "run-001")), "has no diagnostic report"),
        (lambda s: (write_directive(s, "run-001"), write_candidate(s, "run-001"),
                    write_report(s, "run-001", stub=True)), "graded stub"),
        (lambda s: (write_directive(s, "run-001"), write_candidate(s, "run-001"),
                    write_report(s, "run-001", backends="weird")), "graded stub"),
    ],
)
def test_missing_or_stub_evidence_is_a_deterministic_error(store, setup, fragment):
    setup(store)
    with pytest.raises(ContextAssemblyError, match=fragment):
        assemble_context(store, "run-001")


def test_assembly_errors_are_scheduler_errors():
    assert issubclass(ContextAssemblyError, SchedulerError)


def test_a_report_without_l1_scores_is_rejected(store):
    write_round(store, "run-001", {"tom": 0.4})
    payload = store.read_product("control-plane/runs/run-001/diagnostic_report.yaml")
    payload.payload["dimension_scores"] = [s for s in payload.payload["dimension_scores"] if s["level"] != "l1"]
    store.write_product("control-plane/runs/run-001/diagnostic_report.yaml", payload, "test")
    with pytest.raises(ContextAssemblyError, match="no l1 scores"):
        assemble_context(store, "run-001")


@pytest.mark.parametrize(
    "flag, expected",
    [
        ({"kind": "social_regression", "dimension": "tom", "drop": 0.1}, ("social_regression", "tom", 0.1)),
        ({"kind": "g_probe_failure", "probe": "g1", "score": 0.3}, ("g_probe_failure", "g1", 0.3)),
        ({"kind": "reward_hacking_suspected", "diag_delta": -0.1, "reward_delta": 0.2},
         ("reward_hacking_suspected", "reward", pytest.approx(0.3))),
        ({"kind": "something_new", "dimension": "tom"}, ("something_new", "tom", 0.0)),
    ],
)
def test_regression_flags_are_normalized(flag, expected):
    m = regression_mark(flag)
    assert (m.kind, m.subject, m.magnitude) == expected


@pytest.mark.parametrize("flag", [{"dimension": "tom"}, {"kind": "social_regression", "dimension": "tom"},
                                  {"kind": "g_probe_failure", "score": 0.1}])
def test_malformed_regression_flags_are_rejected(flag):
    with pytest.raises(ContextAssemblyError):
        regression_mark(flag)


def test_assembly_only_reads(store):
    _full_tree(store)
    write_model(store, 2, "run-002")
    head = store._git("rev-parse", "HEAD")
    assemble_context(store, "run-002")
    assert store._git("rev-parse", "HEAD") == head
    assert store._git("status", "--porcelain") == ""


# ---- 设计 R21:空错误标签("没有错误")在装配时丢掉,不重新归一化 ----

@pytest.mark.parametrize(
    "dist, expected",
    [
        ({"": 1.0}, {}),
        ({"": 0.5, "surface_match": 0.5}, {"surface_match": 0.5}),
        ({"surface_match": 0.25, "reasoning_break": 0.75}, {"surface_match": 0.25, "reasoning_break": 0.75}),
    ],
)
def test_the_no_error_label_is_dropped_without_renormalizing(dist, expected):
    before = dict(dist)
    assert drop_no_error_label(dist) == expected
    assert dist == before  # 不改入参


def test_the_real_classifier_output_for_an_all_correct_family_is_accepted(store):
    """真实形状:aggregate_error_profile 对全答对的族给 {"": 1.0}。装配后是空映射,计划 4 接受。"""
    from diagnostic.error_classifier import aggregate_error_profile

    profile = aggregate_error_profile("l2.x", [""], "static")
    assert profile.error_type_distribution == {"": 1.0}
    write_round(store, "run-001", {"tom": 0.9})
    write_report(store, "run-001", {"tom": 0.9},
                 error_profiles=[{"l2_dimension": profile.l2_dimension, "track": profile.track,
                                  "error_type_distribution": profile.error_type_distribution}])
    ctx = assemble_context(store, "run-001")
    assert [p.to_dict() for p in ctx.diagnostic.error_profiles] == [
        {"l2_dimension": "l2.x", "error_type_distribution": {}}]
    assert PlanningContext.from_dict(json.loads(json.dumps(ctx.to_dict()))) == ctx
```

- [ ] **Step 3: 运行,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_ts_assemble.py`
Expected: 收集阶段报错 `ModuleNotFoundError: No module named 'trainscheduler.assemble'`,`1 error`。

- [ ] **Step 4: 写实现**

新建 `src/trainscheduler/assemble.py`:

```python
"""装配规划上下文(设计 §7.2):从树里读出源 run 的候选、诊断、配方、近期决策与近 K 轮成本增益,
产出 trainplanner.context.PlanningContext。只读,不写树。"""
from __future__ import annotations

from controlplane.evidence import diagnostic_grade
from controlplane.gitstore import GitStore
from controlplane.paths import candidate_checkpoint_file, planner_decision_file, run_dir, run_trace_file
from trainplanner.context import (
    CANDIDATE_TERMINAL_STATES,
    RECIPE_TYPES,
    CandidateSummary,
    CostGainEntry,
    DecisionOutcome,
    DiagnosticSummary,
    ErrorProfileSummary,
    InvalidContextError,
    PlanningContext,
    RecipeSummary,
    RegressionMark,
)
from trainscheduler.config import SchedulerConfig, load_scheduler_config
from trainscheduler.errors import ContextAssemblyError
from trainscheduler.recipes_port import compute_gain, find_recipes, read_report_payload, regression_flags
from trainscheduler.run_ids import list_run_dirs, run_order_key


def directive_file(run_id: str) -> str:
    return f"{run_dir(run_id)}/directive.yaml"


def _candidate(store: GitStore, run_id: str) -> CandidateSummary:
    path = candidate_checkpoint_file(run_id)
    if not store.exists(path):
        raise ContextAssemblyError(f"{run_id} has no candidate checkpoint")
    env = store.read_product(path)
    if env.status not in CANDIDATE_TERMINAL_STATES:
        raise ContextAssemblyError(
            f"candidate {env.id} is {env.status}; planning needs one of {list(CANDIDATE_TERMINAL_STATES)}"
        )
    reason = env.payload.get("reject_reason") if env.status == "rejected" else None
    if not isinstance(reason, str) or not reason.strip():
        reason = None
    return CandidateSummary(id=env.id, status=env.status, reject_reason=reason)


def _number(v: object) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def regression_mark(flag: dict) -> RegressionMark:
    """把诊断报告的一条退化标记归一化成 {kind, subject, magnitude}(计划 4 的契约)。"""
    kind = flag.get("kind")
    if not isinstance(kind, str) or not kind:
        raise ContextAssemblyError(f"regression flag has no kind: {flag!r}")
    if kind == "social_regression":
        subject, magnitude = flag.get("dimension"), _number(flag.get("drop"))
    elif kind == "g_probe_failure":
        subject, magnitude = flag.get("probe"), _number(flag.get("score"))
    elif kind == "reward_hacking_suspected":
        reward, diag = _number(flag.get("reward_delta")), _number(flag.get("diag_delta"))
        subject = "reward"
        magnitude = reward - diag if reward is not None and diag is not None else None
    else:
        subject = flag.get("dimension") or flag.get("probe") or kind
        magnitude = next((m for m in (_number(flag.get(k)) for k in ("drop", "score")) if m is not None), 0.0)
    if not isinstance(subject, str) or not subject or magnitude is None:
        raise ContextAssemblyError(f"regression flag of kind {kind!r} is malformed: {flag!r}")
    return RegressionMark(kind=kind, subject=subject, magnitude=magnitude)


NO_ERROR_LABEL = ""


def drop_no_error_label(distribution: object) -> object:
    """设计 R21:diagnostic.error_classifier 对"基题与变体都答对"的变体族给空标签 "",
    aggregate_error_profile 把它照常数进分布。空标签的意思是"没有错误",不是一种错误类型;
    计划 4 的 PlanningContext 拒绝空键。这里把它丢掉,不重新归一化(剩下的和可以小于 1)。
    不是映射时原样返回,交给 ErrorProfileSummary 报形状错。"""
    if not isinstance(distribution, dict):
        return distribution
    return {k: v for k, v in distribution.items() if k != NO_ERROR_LABEL}


def l1_scores(payload: dict) -> dict[str, float]:
    scores: dict[str, float] = {}
    raw = payload.get("dimension_scores")
    if not isinstance(raw, list):
        raise ContextAssemblyError("diagnostic report has no dimension_scores list")
    for item in raw:
        if not isinstance(item, dict) or item.get("level") != "l1":
            continue
        dim, score = item.get("dimension"), _number(item.get("score"))
        if not isinstance(dim, str) or not dim or score is None:
            raise ContextAssemblyError(f"malformed l1 score in diagnostic report: {item!r}")
        if dim in scores:
            raise ContextAssemblyError(f"diagnostic report has more than one l1 score for {dim!r}")
        scores[dim] = score
    if not scores:
        raise ContextAssemblyError("diagnostic report has no l1 scores")
    return scores


def _diagnostic(store: GitStore, run_id: str) -> DiagnosticSummary:
    payload = read_report_payload(store, run_id)
    if payload is None:
        raise ContextAssemblyError(f"{run_id} has no diagnostic report")
    grade = diagnostic_grade(payload)
    if grade == "stub":
        raise ContextAssemblyError(f"diagnostic report for {run_id} is graded stub; stub evidence cannot drive planning")
    profiles = []
    raw_profiles = payload.get("error_profiles", [])
    if not isinstance(raw_profiles, list):
        raise ContextAssemblyError("diagnostic report error_profiles must be a list")
    for p in raw_profiles:
        if not isinstance(p, dict):
            raise ContextAssemblyError(f"malformed error profile: {p!r}")
        profiles.append(
            ErrorProfileSummary(
                l2_dimension=p.get("l2_dimension"),
                error_type_distribution=drop_no_error_label(p.get("error_type_distribution") or {}),
            )
        )
    return DiagnosticSummary(
        l1_scores=l1_scores(payload),
        regression_flags=[regression_mark(f) for f in regression_flags(payload)],
        error_profiles=profiles,
        benchmark_version=payload.get("benchmark_version"),
        grade=grade,
    )


def _recipes(store: GitStore) -> list[RecipeSummary]:
    out = []
    for view in find_recipes(store):
        if view.recipe_type not in RECIPE_TYPES or not view.dimension:
            continue  # 形状不对的配方 clone 不了,也不该让它挡住整轮规划
        out.append(
            RecipeSummary(
                id=view.id, recipe_type=view.recipe_type, dimension=view.dimension,
                status=view.status, mean_gain=view.mean_gain, n_reuse=view.n_reuse,
            )
        )
    return out


def decision_is_executed(store: GitStore, env) -> bool:
    """executed,或 pending 而它的 next_run_id 指令已在树里(崩在"指令已写、决策未标记"之间,设计 §4.1)。"""
    if env.status == "executed":
        return True
    next_run = env.payload.get("next_run_id")
    return (
        env.status == "pending"
        and isinstance(next_run, str)
        and bool(next_run)
        and store.exists(directive_file(next_run))
    )


def _ordered_runs_up_to(store: GitStore, run_id: str) -> list[str]:
    limit = run_order_key(run_id)
    return [r for r in sorted(list_run_dirs(store), key=run_order_key) if run_order_key(r) <= limit]


def _recent_decisions(store: GitStore, runs: list[str], window: int) -> list[DecisionOutcome]:
    out: list[DecisionOutcome] = []
    for source in runs:
        path = planner_decision_file(source)
        if not store.exists(path):
            continue
        env = store.read_product(path)
        if not decision_is_executed(store, env):
            continue
        decision = env.payload.get("decision") or {}
        next_run = env.payload["next_run_id"]
        cpath = candidate_checkpoint_file(next_run)
        candidate_status = store.read_product(cpath).status if store.exists(cpath) else None
        dimension = decision.get("target_dimension")
        gain = compute_gain(store, next_run, dimension).gain if candidate_status else None
        out.append(
            DecisionOutcome(
                decision_id=env.id, next_run_id=next_run, target_dimension=dimension,
                action=decision.get("action"), source_recipe_id=decision.get("source_recipe_id"),
                candidate_status=candidate_status, gain=gain,
            )
        )
    return out[-window:]


def _summed(records: list[dict], key: str, cast):
    """有记录才求和;一条都没有这个键就是 None(未记录),不是 0。"""
    values = [r[key] for r in records if key in r]
    return cast(sum(values)) if values else None


def _cost_gain(store: GitStore, runs: list[str], window: int) -> list[CostGainEntry]:
    out: list[CostGainEntry] = []
    for run_id in runs:
        dpath = directive_file(run_id)
        if not store.exists(dpath):
            continue
        dimension = store.read_product(dpath).payload.get("target_dimension")
        if not isinstance(dimension, str) or not dimension:
            continue
        records: list[dict] = []
        if store.exists(run_trace_file(run_id)):
            records = list(store.read_product(run_trace_file(run_id)).payload.get("cost_records", []))
        out.append(
            CostGainEntry(
                run_id=run_id, target_dimension=dimension,
                gpu_hours=_summed(records, "gpu_hours", float),
                tokens=_summed(records, "tokens", int),
                gain=compute_gain(store, run_id, dimension).gain,
            )
        )
    return out[-window:]


def assemble_context(
    store: GitStore, run_id: str, *, config: SchedulerConfig | None = None
) -> PlanningContext:
    """config 缺省时从树里读 scheduler.yaml(窗口大小 K 来自它)。"""
    config = config if config is not None else load_scheduler_config(store)
    try:
        runs = _ordered_runs_up_to(store, run_id)
        return PlanningContext(
            run_id=run_id,
            candidate=_candidate(store, run_id),
            diagnostic=_diagnostic(store, run_id),
            recipes=_recipes(store),
            recent_decisions=_recent_decisions(store, runs, config.recent_decision_window),
            cost_gain=_cost_gain(store, runs, config.cost_gain_window),
        )
    except InvalidContextError as exc:
        raise ContextAssemblyError(f"cannot assemble planning context for {run_id}: {exc}") from exc
```

- [ ] **Step 5: 运行,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_ts_assemble.py`
Expected: `34 passed`,无警告。

- [ ] **Step 6: 全量回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: **B+118(1661)** passed,无警告(撰写时实测数见括号)。再跑 `git status --short`:除原本就有的 `cli/package*.json` 外,只应出现本任务 Files 里的文件。

- [ ] **Step 7: 提交**

```bash
git add src/trainscheduler/assemble.py
git add tests/test_ts_assemble.py
git commit -m "feat: trainscheduler assembles the planning context (stub evidence refused, unrecorded costs are None, last-K windows)"
```

---

### Task 63: 领域校验 `validate_decision` 与动作解析 `resolve_action`

**Files:**
- Create: `src/trainscheduler/resolve.py`(155 行)
- Test: `tests/test_ts_resolve.py`(25 个用例)

**Interfaces:**
- Consumes: `controlplane.models.latest_promoted_checkpoint` / `MissingCheckpointRefError`;`mindtrainer.training_config.TrainingConfig` / `validate_training_config` / `InvalidTrainingConfigError`;`trainplanner.decision.PlannerDecision`;任务 60 的 `SchedulerConfig`、`TrainingDefaults`、`ResolveError`;`recipes_port.get_recipe` / `RecipeNotFoundError`。
- Produces(任务 64、65 依赖):
  - `validate_decision(store, decision, *, config) -> str | None`:返回第一条违规(非空字符串)或 `None`。计划 4 的 `plan()` 要求 validator 严格返回这两种之一。
  - `ResolvedAction(target_dimension, action, recipe_ref, training_config, data_spec, base_model_id)`(冻结)。`recipe_ref` 对 `new` 为空串;`data_spec` 只有 `new` 带;`base_model_id` 是换上的已晋级模型 id,没换为 `None`。
  - `resolve_action(store, decision, *, config, defaults) -> ResolvedAction`;做不成抛 `ResolveError`。
  - `apply_perturbation(training_config, perturbation) -> dict`(返回深拷贝)、`check_training_config(training_config, where) -> TrainingConfig`、`CONSTRUCTIBLE_RECIPE_TYPES = ("batch", "interact")`。

**设计要点:**
- **校验规则**(设计 §7.2,加 Ruling 3、10):`new` + `interact` 一律违规(`allow_interact` 关时说开关,开时说默认配置没有绑定);`clone` / `replay` 的配方必须存在、`effective`、类型可构造、interact 要开关、维度一致、带 `training_config`;扰动键必须在白名单里,扰动后的配置必须过 `validate_training_config`。扰动值的类型错(`sft_steps: "2"`)在这里就以违规返回,不会到解析阶段才炸。
- **解析规则**:`new` 取 `training_defaults` 的配置,基座换成 `latest_promoted_checkpoint`(没有则保留默认配置里的初始基座),带默认 `data_spec`。`clone` 取配方配置,套扰动,基座换成最新已晋级(Ruling 2)。`replay` 原样取配方配置,不做任何替换。三种都不改 `stage_manifest_refs`:`clone` / `replay` 复用配方锚定的清单,`new` 的清单 id 由任务 64 按新 run id 写。
- **解析前再校验一次**:`resolve_action` 先调 `validate_decision`,违规抛 `ResolveError`。重入路径(任务 65 的 (a))不经过 `plan()`,这一遍保证配方在决策写下之后被弃用时也不会写出坏指令。
- `apply_perturbation` 的规则:首段必须已在配置里;中间段必须是映射;叶子可以是新键(`grpo_config` 是透传字典)。按键排序套用,结果确定。
- 只读:`validate_decision` 与 `resolve_action` 都不写树(有测试)。

- [ ] **Step 1: 前置检查**

运行上文"前置检查命令"。Expected: `preflight ok`。

- [ ] **Step 2: 写失败测试**

新建 `tests/test_ts_resolve.py`:

```python
"""领域校验与动作解析:配方存在且有效、扰动白名单、interact 开关;new / clone / replay 各自取什么。"""
import copy

import pytest

from mindtrainer.training_config import TrainingConfig, validate_training_config
from trainplanner.decision import PlannerDecision
from trainscheduler.config import (
    DEFAULT_SCHEDULER_CONFIG,
    DEFAULT_TRAINING_CONFIG,
    SchedulerConfig,
    default_training_defaults_payload,
    parse_training_defaults,
)
from trainscheduler.errors import ResolveError, SchedulerError
from trainscheduler.resolve import apply_perturbation, resolve_action, validate_decision
from ts_fixtures import make_store, recipe_payload, ref, write_model, write_recipe

CFG = DEFAULT_SCHEDULER_CONFIG
DEFAULTS = parse_training_defaults(default_training_defaults_payload())


def _d(action="new", *, dimension="tom", mode="batch", source=None, perturbation=None):
    return PlannerDecision(
        target_dimension=dimension, action=action, mode=mode if action == "new" else None,
        source_recipe_id=source, perturbation=dict(perturbation or {}), data_synthesis_hint="",
        rationale="r",
    )


@pytest.fixture
def store(tmp_path):
    s = make_store(tmp_path)
    write_recipe(s, "run-001")
    return s


def test_a_new_batch_decision_is_valid(store):
    assert validate_decision(store, _d(), config=CFG) is None


def test_new_interact_is_refused_while_allow_interact_is_off(store):
    reason = validate_decision(store, _d(mode="interact"), config=CFG)
    assert "allow_interact" in reason


def test_new_interact_is_refused_even_when_allowed_because_defaults_have_no_bindings(store):
    reason = validate_decision(store, _d(mode="interact"), config=SchedulerConfig(allow_interact=True))
    assert "scenario_ids and bindings" in reason


def test_clone_of_an_effective_recipe_is_valid(store):
    assert validate_decision(store, _d("clone", source="recipe-run-001"), config=CFG) is None
    assert validate_decision(store, _d("replay", source="recipe-run-001"), config=CFG) is None


@pytest.mark.parametrize("action", ["clone", "replay"])
def test_a_missing_recipe_is_a_violation(store, action):
    assert "does not exist" in validate_decision(store, _d(action, source="recipe-run-404"), config=CFG)
    assert "does not exist" in validate_decision(store, _d(action, source="not-a-recipe"), config=CFG)


def test_a_deprecated_recipe_is_a_violation(store):
    write_recipe(store, "run-002", status="deprecated")
    assert "deprecated" in validate_decision(store, _d("clone", source="recipe-run-002"), config=CFG)


def test_an_agent_rsi_recipe_cannot_be_reused(store):
    payload = recipe_payload("run-003")
    payload["recipe_type"] = "agent_rsi"
    write_recipe(store, "run-003", payload=payload)
    assert "'agent_rsi'" in validate_decision(store, _d("clone", source="recipe-run-003"), config=CFG)


def test_an_interact_recipe_needs_allow_interact(store):
    write_recipe(store, "run-004", payload=recipe_payload("run-004", recipe_type="interact"))
    assert "allow_interact is off" in validate_decision(store, _d("replay", source="recipe-run-004"), config=CFG)
    allowed = SchedulerConfig(allow_interact=True)
    assert validate_decision(store, _d("replay", source="recipe-run-004"), config=allowed) is None


def test_the_recipe_must_target_the_decided_dimension(store):
    reason = validate_decision(store, _d("clone", dimension="empathy", source="recipe-run-001"), config=CFG)
    assert "targets 'tom', not 'empathy'" in reason


def test_perturbation_keys_must_be_whitelisted(store):
    ok = _d("clone", source="recipe-run-001", perturbation={"opd_coefficient": 0.2, "grpo_config.learning_rate": 2e-5})
    assert validate_decision(store, ok, config=CFG) is None
    bad = _d("clone", source="recipe-run-001", perturbation={"group_size": 4})
    assert "'group_size' is not perturbable" in validate_decision(store, bad, config=CFG)


def test_a_whitelisted_perturbation_that_breaks_the_config_is_a_violation(store):
    bad = _d("clone", source="recipe-run-001", perturbation={"opd_coefficient": -1})
    assert "opd_coefficient must be a finite number >= 0" in validate_decision(store, bad, config=CFG)
    bad_type = _d("clone", source="recipe-run-001", perturbation={"sft_steps": "2"})
    assert "sft_steps must be an int" in validate_decision(store, bad_type, config=CFG)


def test_a_perturbation_into_a_non_mapping_is_a_violation(store):
    cfg = SchedulerConfig(perturbable=("reward.gamma.x",))
    bad = _d("clone", source="recipe-run-001", perturbation={"reward.gamma.x": 1})
    assert "training_config.reward.gamma is not a mapping" in validate_decision(store, bad, config=cfg)


def test_apply_perturbation_copies_and_sets_nested_keys():
    cfg = copy.deepcopy(DEFAULT_TRAINING_CONFIG)
    out = apply_perturbation(cfg, {"grpo_config.learning_rate": 3e-5, "grpo_config.beta": 0.1, "sft_steps": 4})
    assert out["grpo_config"] == {"learning_rate": 3e-5, "beta": 0.1} and out["sft_steps"] == 4
    assert cfg == DEFAULT_TRAINING_CONFIG


def test_apply_perturbation_refuses_unknown_top_level_fields():
    with pytest.raises(ResolveError, match="has no field 'learning_rate'"):
        apply_perturbation(copy.deepcopy(DEFAULT_TRAINING_CONFIG), {"learning_rate": 1})


def test_new_without_a_promoted_model_uses_the_initial_base(store):
    r = resolve_action(store, _d(), config=CFG, defaults=DEFAULTS)
    assert (r.action, r.recipe_ref, r.target_dimension, r.base_model_id) == ("new", "", "tom", None)
    assert r.training_config == DEFAULT_TRAINING_CONFIG
    assert r.data_spec == DEFAULTS.data_spec


def test_new_takes_the_latest_promoted_checkpoint(store):
    write_model(store, 1, "run-001")
    write_model(store, 2, "run-002")
    r = resolve_action(store, _d(), config=CFG, defaults=DEFAULTS)
    assert r.training_config["base_checkpoint_ref"] == ref("cand-run-002")
    assert r.base_model_id == "zengen-v2"


def test_clone_takes_the_recipe_config_plus_perturbation_on_the_latest_base(store):
    write_model(store, 1, "run-005")
    d = _d("clone", source="recipe-run-001", perturbation={"opd_coefficient": 0.25})
    r = resolve_action(store, d, config=CFG, defaults=DEFAULTS)
    expected = copy.deepcopy(recipe_payload("run-001")["training_config"])
    expected["opd_coefficient"] = 0.25
    expected["base_checkpoint_ref"] = ref("cand-run-005")
    assert r.training_config == expected
    assert (r.action, r.recipe_ref, r.data_spec, r.base_model_id) == ("clone", "recipe-run-001", None, "zengen-v1")
    # 清单仍是配方锚定的那两份
    assert r.training_config["stage_manifest_refs"] == {"sft": "dsm-run-001-sft", "grpo_opd": "dsm-run-001-grpo_opd"}


def test_clone_without_a_promoted_model_keeps_the_recipe_base(store):
    r = resolve_action(store, _d("clone", source="recipe-run-001"), config=CFG, defaults=DEFAULTS)
    assert r.training_config == recipe_payload("run-001")["training_config"]


def test_replay_takes_the_recipe_verbatim_even_with_a_newer_base(store):
    write_model(store, 3, "run-009")
    r = resolve_action(store, _d("replay", source="recipe-run-001"), config=CFG, defaults=DEFAULTS)
    assert r.training_config == recipe_payload("run-001")["training_config"]
    assert (r.recipe_ref, r.data_spec, r.base_model_id) == ("recipe-run-001", None, None)


def test_resolved_configs_validate(store):
    write_model(store, 1, "run-001")
    for d in (_d("clone", source="recipe-run-001"), _d("replay", source="recipe-run-001")):
        cfg = resolve_action(store, d, config=CFG, defaults=DEFAULTS).training_config
        assert validate_training_config(TrainingConfig.from_dict(cfg)) is None


def test_a_violation_is_raised_as_a_resolve_error(store):
    with pytest.raises(ResolveError, match="does not exist"):
        resolve_action(store, _d("clone", source="recipe-run-404"), config=CFG, defaults=DEFAULTS)
    assert issubclass(ResolveError, SchedulerError)


def test_a_recipe_whose_config_does_not_validate_cannot_be_replayed(store):
    payload = recipe_payload("run-006")
    payload["training_config"]["group_size"] = 1
    write_recipe(store, "run-006", payload=payload)
    with pytest.raises(ResolveError, match="group_size 1 is below min_group_size 2"):
        resolve_action(store, _d("replay", source="recipe-run-006"), config=CFG, defaults=DEFAULTS)


def test_a_promoted_model_without_checkpoint_ref_is_a_resolve_error(store):
    from controlplane.paths import model_file
    from ts_fixtures import env

    store.write_product(model_file(1), env("zengen-v1", "zengen_model", "effective", {"promoted_from": "x"}), "t")
    with pytest.raises(ResolveError, match="no checkpoint_ref"):
        resolve_action(store, _d(), config=CFG, defaults=DEFAULTS)


def test_validation_and_resolution_only_read(store):
    head = store._git("rev-parse", "HEAD")
    resolve_action(store, _d("clone", source="recipe-run-001"), config=CFG, defaults=DEFAULTS)
    validate_decision(store, _d("replay", source="recipe-run-404"), config=CFG)
    assert store._git("rev-parse", "HEAD") == head
```

- [ ] **Step 3: 运行,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_ts_resolve.py`
Expected: 收集阶段报错 `ModuleNotFoundError: No module named 'trainscheduler.resolve'`,`1 error`。

- [ ] **Step 4: 写实现**

新建 `src/trainscheduler/resolve.py`:

```python
"""领域校验与动作解析(设计 §7.2):把一条 PlannerDecision 变成一份完整、通过校验的训练配置。"""
from __future__ import annotations

import copy
from dataclasses import dataclass

from controlplane.gitstore import GitStore
from controlplane.models import MissingCheckpointRefError, latest_promoted_checkpoint
from mindtrainer.data_spec import DataSpec
from mindtrainer.training_config import InvalidTrainingConfigError, TrainingConfig, validate_training_config
from trainplanner.decision import PlannerDecision
from trainscheduler.config import SchedulerConfig, TrainingDefaults
from trainscheduler.errors import ResolveError
from trainscheduler.recipes_port import RecipeNotFoundError, get_recipe

CONSTRUCTIBLE_RECIPE_TYPES = ("batch", "interact")
_MISSING = object()


@dataclass(frozen=True)
class ResolvedAction:
    target_dimension: str
    action: str  # "new" | "clone" | "replay"
    recipe_ref: str  # clone / replay 为配方 id;new 为空串
    training_config: dict  # TrainingConfig.to_dict() 的形状;new 不含 stage_manifest_refs
    data_spec: DataSpec | None  # 只有 new 带
    base_model_id: str | None  # 换上的已晋级基座(zengen-vN);没换为 None


def apply_perturbation(training_config: dict, perturbation: dict) -> dict:
    """把扰动套到 training_config 的深拷贝上。键是点路径;首段必须已在配置里,中间段必须是映射,
    叶子可以是新键(grpo_config 是透传给执行器的字典)。套不上抛 ResolveError。"""
    out = copy.deepcopy(training_config)
    for key in sorted(perturbation):
        parts = key.split(".")
        node = out
        for depth, part in enumerate(parts[:-1]):
            child = node.get(part, _MISSING) if isinstance(node, dict) else _MISSING
            if not isinstance(child, dict):
                where = ".".join(parts[: depth + 1])
                raise ResolveError(f"perturbation {key!r}: training_config.{where} is not a mapping")
            node = child
        if len(parts) == 1 and parts[0] not in out:
            raise ResolveError(f"perturbation {key!r}: training_config has no field {parts[0]!r}")
        node[parts[-1]] = copy.deepcopy(perturbation[key])
    return out


def check_training_config(training_config: dict, where: str) -> TrainingConfig:
    """from_dict + validate_training_config。任何不合格抛 ResolveError(信息带 where)。"""
    try:
        cfg = TrainingConfig.from_dict(training_config)
    except KeyError as exc:
        raise ResolveError(f"{where}: training_config is missing field {exc}") from exc
    except (TypeError, AttributeError, InvalidTrainingConfigError) as exc:
        raise ResolveError(f"{where}: training_config is malformed ({exc})") from exc
    reason = validate_training_config(cfg)
    if reason is not None:
        raise ResolveError(f"{where}: {reason}")
    return cfg


def _recipe_violation(store: GitStore, decision: PlannerDecision, config: SchedulerConfig):
    """返回 (违规原因, 配方信封)。"""
    try:
        recipe = get_recipe(store, decision.source_recipe_id)
    except RecipeNotFoundError:
        return f"recipe {decision.source_recipe_id!r} does not exist", None
    if recipe.status != "effective":
        return f"recipe {recipe.id} is {recipe.status}, only effective recipes can be reused", None
    recipe_type = recipe.payload.get("recipe_type")
    if recipe_type not in CONSTRUCTIBLE_RECIPE_TYPES:
        return f"recipe {recipe.id} is a {recipe_type!r} recipe; only batch and interact recipes can be reused", None
    if recipe_type == "interact" and not config.allow_interact:
        return f"recipe {recipe.id} is an interact recipe and allow_interact is off", None
    if recipe.payload.get("dimension") != decision.target_dimension:
        return (
            f"recipe {recipe.id} targets {recipe.payload.get('dimension')!r}, "
            f"not {decision.target_dimension!r}"
        ), None
    if not isinstance(recipe.payload.get("training_config"), dict):
        return f"recipe {recipe.id} has no training_config", None
    return None, recipe


def validate_decision(store: GitStore, decision: PlannerDecision, *, config: SchedulerConfig) -> str | None:
    """领域有效性。返回第一条违规(非空字符串),全部合格返回 None。只读树。"""
    if decision.action == "new":
        if decision.mode == "interact":
            if not config.allow_interact:
                return "mode 'interact' is not allowed (scheduler.allow_interact is false); use 'batch'"
            return (
                "a new interact round needs scenario_ids and bindings, which training_defaults "
                "does not provide; use mode 'batch' or clone an interact recipe"
            )
        return None
    reason, recipe = _recipe_violation(store, decision, config)
    if reason is not None:
        return reason
    for key in sorted(decision.perturbation):
        if key not in config.perturbable:
            return f"perturbation key {key!r} is not perturbable; allowed {list(config.perturbable)}"
    if decision.perturbation:
        try:
            perturbed = apply_perturbation(recipe.payload["training_config"], decision.perturbation)
            check_training_config(perturbed, f"perturbed {recipe.id}")
        except ResolveError as exc:
            return exc.reason
    return None


def _promoted_base(store: GitStore) -> tuple[dict | None, str | None]:
    try:
        base = latest_promoted_checkpoint(store)
    except MissingCheckpointRefError as exc:
        raise ResolveError(str(exc)) from exc
    if base is None:
        return None, None
    return base.checkpoint_ref.to_dict(), base.model_id


def resolve_action(
    store: GitStore,
    decision: PlannerDecision,
    *,
    config: SchedulerConfig,
    defaults: TrainingDefaults,
) -> ResolvedAction:
    """new:默认配置 + 最新已晋级基座(没有就用默认配置自带的初始基座)+ 默认 data_spec。
    clone:配方的配置 + 扰动 + 最新已晋级基座;复用配方锚定的清单,不带 data_spec。
    replay:配方的配置原样,不做任何替换,不带 data_spec。
    确定性做不成抛 ResolveError。"""
    violation = validate_decision(store, decision, config=config)
    if violation is not None:
        raise ResolveError(violation)
    if decision.action == "new":
        if decision.mode != "batch":
            raise ResolveError(
                f"new {decision.mode!r} rounds cannot be resolved: training_defaults only holds a batch config"
            )
        cfg = copy.deepcopy(defaults.training_config)
        base, model_id = _promoted_base(store)
        if base is not None:
            cfg["base_checkpoint_ref"] = base
        return ResolvedAction(decision.target_dimension, "new", "", cfg, defaults.data_spec, model_id)
    recipe = get_recipe(store, decision.source_recipe_id)
    cfg = copy.deepcopy(recipe.payload["training_config"])
    model_id = None
    if decision.action == "clone":
        cfg = apply_perturbation(cfg, decision.perturbation)
        base, model_id = _promoted_base(store)
        if base is not None:
            cfg["base_checkpoint_ref"] = base
    check_training_config(cfg, f"{decision.action} of {recipe.id}")
    return ResolvedAction(decision.target_dimension, decision.action, recipe.id, cfg, None, model_id)
```

- [ ] **Step 5: 运行,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_ts_resolve.py`
Expected: `25 passed`,无警告。

- [ ] **Step 6: 全量回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: **B+143(1686)** passed,无警告(撰写时实测数见括号)。再跑 `git status --short`:除原本就有的 `cli/package*.json` 外,只应出现本任务 Files 里的文件。

- [ ] **Step 7: 提交**

```bash
git add src/trainscheduler/resolve.py
git add tests/test_ts_resolve.py
git commit -m "feat: trainscheduler domain validation and action resolution (whitelisted perturbations, clone on the latest promoted base, replay verbatim)"
```

---

### Task 64: 构造指令 `construct_directive`

**Files:**
- Create: `src/trainscheduler/directive.py`(99 行)
- Test: `tests/test_ts_directive.py`(12 个用例)

**Interfaces:**
- Consumes: `mindtrainer.training_config.load_config_from_directive` / `validate_training_config` / `TrainingConfig`;`mindtrainer.data_spec.parse_data_spec` / `required_stages` / `check_data_spec_against_config`;任务 62 的 `directive_file`;任务 63 的 `ResolvedAction`。
- Produces(任务 65 依赖):`construct_directive(store, resolved, *, new_run_id, confirm, planner_decision_id) -> str`(返回相对路径)、`manifest_id(run_id, stage) -> "dsm-<run>-<stage>"`、`directive_payload(...)`、`check_directive(env)`。

**设计要点:**
- **载荷**(设计 §4.2、§9):`target_dimension`、`recipe_ref`、`training_config`、`planner_decision_ref`;`new` 另带 `data_spec`,并把 `training_config.stage_manifest_refs` 写成 `{阶段: dsm-<new_run_id>-<阶段>}`,只写 `required_stages` 里的阶段(有测试:`sft_steps: 0` 时只有 `grpo_opd`)。`clone` / `replay` 不带 `data_spec`。
- **状态**由 `confirm` 决定:`True` 写 `awaiting_confirmation`,`False` 写 `pending`。`confirm` 不是 `bool` 抛 `TypeError`。
- **写之前过两道校验**:`load_config_from_directive` + `validate_training_config`(就是 MindTrainer 派发器读指令的那条路径),以及 `data_spec` 的解析与交叉校验。不过就抛 `DirectiveConstructionError`,树里什么都不写(有测试比对 HEAD)。
- **能被 MindTrainer 读回**:测试对写出的指令跑 `load_config_from_directive`、`validate_training_config`、`data_spec_from_directive`、`stages_to_synthesize`,证明派发器会为 `new` 合成两个阶段、为 `clone` / `replay` 一个都不合成。
- **不覆盖**(Ruling 15)。

- [ ] **Step 1: 前置检查**

运行上文"前置检查命令"。Expected: `preflight ok`。

- [ ] **Step 2: 写失败测试**

新建 `tests/test_ts_directive.py`:

```python
"""构造指令:载荷形状、data_spec 与按新 run id 写好的清单 id、确认状态、能被 MindTrainer 读回。"""
import pytest

from controlplane.states import can_transition
from mindtrainer.data_spec import data_spec_from_directive, stages_to_synthesize
from mindtrainer.training_config import load_config_from_directive, validate_training_config
from trainplanner.decision import PlannerDecision
from trainscheduler.config import (
    DEFAULT_SCHEDULER_CONFIG,
    default_training_defaults_payload,
    parse_training_defaults,
)
from trainscheduler.directive import construct_directive, manifest_id
from trainscheduler.errors import DirectiveConstructionError, SchedulerError
from trainscheduler.resolve import ResolvedAction, resolve_action
from ts_fixtures import make_store, recipe_payload, write_directive, write_recipe

DEFAULTS = parse_training_defaults(default_training_defaults_payload())


def _decision(action="new", source=None, perturbation=None):
    return PlannerDecision("tom", action, "batch" if action == "new" else None, source,
                           dict(perturbation or {}), "", "r")


def _resolved(store, action="new", source=None):
    return resolve_action(store, _decision(action, source), config=DEFAULT_SCHEDULER_CONFIG, defaults=DEFAULTS)


@pytest.fixture
def store(tmp_path):
    s = make_store(tmp_path)
    write_recipe(s, "run-001")
    return s


def test_manifest_ids_follow_the_dataset_rule():
    assert manifest_id("run-004", "grpo_opd") == "dsm-run-004-grpo_opd"


def test_a_new_directive_carries_data_spec_and_its_own_manifest_ids(store):
    path = construct_directive(store, _resolved(store), new_run_id="run-002", confirm=True,
                               planner_decision_id="planner-run-001")
    assert path == "control-plane/runs/run-002/directive.yaml"
    env = store.read_product(path)
    assert (env.id, env.type, env.status) == ("run-002", "directive", "awaiting_confirmation")
    assert set(env.payload) == {"target_dimension", "recipe_ref", "training_config", "planner_decision_ref", "data_spec"}
    assert env.payload["target_dimension"] == "tom" and env.payload["recipe_ref"] == ""
    assert env.payload["planner_decision_ref"] == "planner-run-001"
    assert env.payload["data_spec"] == DEFAULTS.data_spec.to_dict()
    assert env.payload["training_config"]["stage_manifest_refs"] == {
        "sft": "dsm-run-002-sft", "grpo_opd": "dsm-run-002-grpo_opd"}
    assert env.links == ["planner-run-001"]


def test_the_directive_reads_back_through_mindtrainer(store):
    construct_directive(store, _resolved(store), new_run_id="run-002", confirm=False,
                        planner_decision_id="planner-run-001")
    env = store.read_product("control-plane/runs/run-002/directive.yaml")
    assert env.status == "pending"
    cfg = load_config_from_directive(env)
    assert validate_training_config(cfg) is None
    assert data_spec_from_directive(env) == DEFAULTS.data_spec
    assert stages_to_synthesize(store, "run-002", cfg) == ["sft", "grpo_opd"]


def test_only_required_stages_get_a_manifest_id(store):
    raw = default_training_defaults_payload()
    raw["training_config"]["sft_steps"] = 0
    defaults = parse_training_defaults(raw)
    resolved = resolve_action(store, _decision(), config=DEFAULT_SCHEDULER_CONFIG, defaults=defaults)
    construct_directive(store, resolved, new_run_id="run-002", confirm=False, planner_decision_id="planner-run-001")
    env = store.read_product("control-plane/runs/run-002/directive.yaml")
    assert env.payload["training_config"]["stage_manifest_refs"] == {"grpo_opd": "dsm-run-002-grpo_opd"}


@pytest.mark.parametrize("action", ["clone", "replay"])
def test_clone_and_replay_reuse_the_recipe_manifests_and_carry_no_data_spec(store, action):
    construct_directive(store, _resolved(store, action, "recipe-run-001"), new_run_id="run-002", confirm=True,
                        planner_decision_id="planner-run-001")
    env = store.read_product("control-plane/runs/run-002/directive.yaml")
    assert "data_spec" not in env.payload
    assert env.payload["recipe_ref"] == "recipe-run-001"
    assert env.payload["training_config"]["stage_manifest_refs"] == recipe_payload("run-001")["training_config"][
        "stage_manifest_refs"]
    assert env.links == ["planner-run-001", "recipe-run-001"]
    assert validate_training_config(load_config_from_directive(env)) is None
    assert stages_to_synthesize(store, "run-002", load_config_from_directive(env)) == []


def test_awaiting_confirmation_can_only_be_confirmed_or_abandoned():
    assert can_transition("nonreview", "awaiting_confirmation", "pending")
    assert can_transition("nonreview", "awaiting_confirmation", "failed")
    assert not can_transition("nonreview", "awaiting_confirmation", "executed")


def test_an_invalid_config_is_refused_and_nothing_is_written(store):
    bad = ResolvedAction("tom", "replay", "recipe-run-001",
                         {**recipe_payload("run-001")["training_config"], "group_size": 1}, None, None)
    head = store._git("rev-parse", "HEAD")
    with pytest.raises(DirectiveConstructionError, match="group_size 1 is below"):
        construct_directive(store, bad, new_run_id="run-002", confirm=True, planner_decision_id="planner-run-001")
    assert not store.exists("control-plane/runs/run-002/directive.yaml")
    assert store._git("rev-parse", "HEAD") == head
    assert issubclass(DirectiveConstructionError, SchedulerError)


def test_a_data_spec_too_small_for_the_config_is_refused(store):
    from mindtrainer.data_spec import parse_data_spec

    tiny = parse_data_spec({"scenario_count": 1, "seed": 0, "purpose": "training",
                            "stages": {"sft": {"group_size": 1}, "grpo_opd": {"group_size": 2}}})
    r = _resolved(store)
    bad = ResolvedAction(r.target_dimension, r.action, r.recipe_ref, r.training_config, tiny, None)
    with pytest.raises(DirectiveConstructionError, match="below groups_per_step"):
        construct_directive(store, bad, new_run_id="run-002", confirm=True, planner_decision_id="planner-run-001")


def test_rewriting_by_the_same_decision_is_a_no_op(store):
    r = _resolved(store)
    construct_directive(store, r, new_run_id="run-002", confirm=True, planner_decision_id="planner-run-001")
    head = store._git("rev-parse", "HEAD")
    construct_directive(store, r, new_run_id="run-002", confirm=False, planner_decision_id="planner-run-001")
    assert store._git("rev-parse", "HEAD") == head
    assert store.read_product("control-plane/runs/run-002/directive.yaml").status == "awaiting_confirmation"


def test_someone_elses_directive_is_never_overwritten(store):
    write_directive(store, "run-002", status="pending")
    with pytest.raises(DirectiveConstructionError, match="refusing to overwrite"):
        construct_directive(store, _resolved(store), new_run_id="run-002", confirm=True,
                            planner_decision_id="planner-run-001")
    assert store.read_product("control-plane/runs/run-002/directive.yaml").status == "pending"


def test_confirm_must_be_a_bool(store):
    with pytest.raises(TypeError):
        construct_directive(store, _resolved(store), new_run_id="run-002", confirm="yes",
                            planner_decision_id="planner-run-001")
```

- [ ] **Step 3: 运行,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_ts_directive.py`
Expected: 收集阶段报错 `ModuleNotFoundError: No module named 'trainscheduler.directive'`,`1 error`。

- [ ] **Step 4: 写实现**

新建 `src/trainscheduler/directive.py`:

```python
"""构造训练指令(设计 §4.2、§9):runs/<new_run_id>/directive.yaml。"""
from __future__ import annotations

import copy

from controlplane.envelope import CURRENT_SCHEMA_VERSION, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from mindtrainer.data_spec import (
    InvalidDataSpecError,
    check_data_spec_against_config,
    parse_data_spec,
    required_stages,
)
from mindtrainer.training_config import (
    InvalidTrainingConfigError,
    TrainingConfig,
    load_config_from_directive,
    validate_training_config,
)
from trainscheduler.assemble import directive_file
from trainscheduler.errors import DirectiveConstructionError
from trainscheduler.resolve import ResolvedAction

NOW = "2026-10-09T00:00:00Z"


def manifest_id(run_id: str, stage: str) -> str:
    """与 simulator.dataset.build_manifest 的 id 规则一致:dsm-<数据所在的 run>-<阶段>。"""
    return f"dsm-{run_id}-{stage}"


def directive_payload(resolved: ResolvedAction, *, new_run_id: str, planner_decision_id: str) -> dict:
    cfg = copy.deepcopy(resolved.training_config)
    payload: dict = {
        "target_dimension": resolved.target_dimension,
        "recipe_ref": resolved.recipe_ref,
        "training_config": cfg,
        "planner_decision_ref": planner_decision_id,
    }
    if resolved.action == "new":
        stages = required_stages(TrainingConfig.from_dict(cfg))
        cfg["stage_manifest_refs"] = {stage: manifest_id(new_run_id, stage) for stage in stages}
        payload["data_spec"] = resolved.data_spec.to_dict()
    return payload


def _envelope(new_run_id: str, payload: dict, status: str, links: list[str]) -> Envelope:
    return Envelope(
        id=new_run_id, type="directive", version=1, created_at=NOW, created_by=OwnerRef(),
        status=status, schema_version=CURRENT_SCHEMA_VERSION, links=links, payload=payload,
    )


def check_directive(env: Envelope) -> None:
    """指令必须能被 MindTrainer 派发器读回并通过校验;new 的 data_spec 必须够合成它点名的阶段。"""
    try:
        cfg = load_config_from_directive(env)
    except InvalidTrainingConfigError as exc:
        raise DirectiveConstructionError(str(exc)) from exc
    reason = validate_training_config(cfg)
    if reason is not None:
        raise DirectiveConstructionError(f"directive {env.id}: {reason}")
    if "data_spec" in env.payload:
        try:
            spec = parse_data_spec(env.payload["data_spec"])
        except InvalidDataSpecError as exc:
            raise DirectiveConstructionError(f"directive {env.id}: {exc}") from exc
        reason = check_data_spec_against_config(spec, cfg, required_stages(cfg))
        if reason is not None:
            raise DirectiveConstructionError(f"directive {env.id}: {reason}")


def construct_directive(
    store: GitStore,
    resolved: ResolvedAction,
    *,
    new_run_id: str,
    confirm: bool,
    planner_decision_id: str,
) -> str:
    """写指令并返回相对路径。confirm 为真写 awaiting_confirmation,否则 pending。
    校验不过抛 DirectiveConstructionError,树里什么都不写。目标位置已有指令:由同一决策写的
    视为已完成(原样返回路径),别人写的拒绝覆盖。"""
    if not isinstance(confirm, bool):
        raise TypeError(f"confirm must be a bool, got {type(confirm).__name__}")
    path = directive_file(new_run_id)
    if store.exists(path):
        existing = store.read_product(path)
        if existing.payload.get("planner_decision_ref") == planner_decision_id:
            return path
        raise DirectiveConstructionError(
            f"{path} already exists and was not written by {planner_decision_id}; refusing to overwrite"
        )
    payload = directive_payload(resolved, new_run_id=new_run_id, planner_decision_id=planner_decision_id)
    links = [planner_decision_id] + ([resolved.recipe_ref] if resolved.recipe_ref else [])
    env = _envelope(new_run_id, payload, "awaiting_confirmation" if confirm else "pending", links)
    check_directive(env)
    store.write_product(path, env, f"trainscheduler: directive {new_run_id} ({resolved.action})")
    return path
```

- [ ] **Step 5: 运行,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_ts_directive.py`
Expected: `12 passed`,无警告。

- [ ] **Step 6: 全量回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: **B+155(1698)** passed,无警告(撰写时实测数见括号)。再跑 `git status --short`:除原本就有的 `cli/package*.json` 外,只应出现本任务 Files 里的文件。

- [ ] **Step 7: 提交**

```bash
git add src/trainscheduler/directive.py
git add tests/test_ts_directive.py
git commit -m "feat: trainscheduler writes directives that MindTrainer reads back (data_spec and per-run manifest ids for new, recipe manifests for clone and replay)"
```

---

### Task 65: 规划处理器 `make_plan_handler`

**Files:**
- Create: `src/trainscheduler/plan_handler.py`(217 行)
- Test: `tests/test_ts_plan_handler.py`(38 个用例)

**Interfaces:**
- Consumes: `controlplane.ledger.record_failure`;`controlplane.paths.planner_decision_file`;`controlplane.reconcile.Action`;`controlplane.states.NONREVIEW` / `assert_transition`;`inference_bridge.BridgeError`;`trainplanner.planner.plan` / `PlannerFailedError`;`trainplanner.templates.load_template` / `PromptTemplate`;`trainplanner.decision.PlannerDecision` / `DecisionParseError`;`trainplanner.backends.DecisionBackend`;任务 60–64 的全部产出。
- Produces(计划 8 注入、计划 9 组装):
  - `make_plan_handler(*, backend, template_loader=load_template, config_loader=load_scheduler_config, defaults_loader=load_training_defaults) -> Handler`,`Handler = Callable[[GitStore, Action], None]`。
  - `decision_id(run_id) -> "planner-<run>"`、`plan_task_id(run_id) -> "plan-<run>"`、`FAILURE_CATEGORY = "T3"`、`FAILURE_POINT = "trainscheduler.plan"`。

**设计要点:**
- **流程**:装配上下文 → `plan(..., max_parse_retries=max_plan_attempts-1, validator=lambda d: validate_decision(store, d, config=config))` → `allocate_run_id` → 写 `planner_decision`(`pending`,载荷带 `next_run_id` 与 `record_fields()` 的四个键)→ `resolve_action` → `construct_directive` → 决策置 `executed`。`target_id` 是来源 run id。
- **重入规则**(设计 §4.1、§3.2,计划 8 的"规划处理器的重入契约"),各有测试:
  - (a) 决策 `pending`、带 `next_run_id`、那条指令不在:按决策里记下的内容续写,**不调后端、不重新分配**。续写前先防御被人改坏的载荷:`next_run_id` 必须是 `run-<数字>`(`None`、空串、`../escape`、`run-x` 都判 `ResolveError`,不会写出 `runs/None/directive.yaml`);`decision` 不是映射、或 `PlannerDecision.from_dict` 抛 `DecisionParseError`(缺键、取值非法、`rationale` 为空)都包成 `ResolveError`。两者都走 `_fail` 判 T3,下一个 tick 是空操作,不会每个 tick 抛异常逃出守护循环(各有参数化测试)。测试两种:直接放一条 pending 决策;以及用 `monkeypatch` 让第一次 `construct_directive` 抛 `OSError`,第二次调用时两次收到的 `new_run_id` 都是 `run-002`、后端只被调了一次。
  - (b) 决策 `pending`、`next_run_id` 的指令已在:只把决策置 `executed`(恰好一次提交),不写第二条指令。
  - (c) 决策 `executed` 或 `failed`:空操作,零提交。
- **失败**:`PlannerFailedError` → 决策 `failed`,`failure = {category: T3, reason, attempts}`,`record_failure(task_id=plan-<run>, category=T3, point=trainscheduler.plan, retries=attempts-1)`;`SchedulerError`(装配、解析、构造)同样处理(规划前失败的 `attempts` 记 0);**任何 `BridgeError` 同样处理,T1 与 T2 不区分**(设计 R18、Ruling 6),`log_ref` 带进失败记录。非桥接异常(`OSError`、`RuntimeError`、`SchedulerConfigError`、`TemplateError` 等)原样上抛,决策保持原状。测试:`BridgeError("T1", …)` 与 `BridgeError("T2", …)` 参数化,两者都使决策 `failed`、写失败记录、不抛;第二次尝试才抛 `BridgeError` 时 `attempts == 2`;判 `failed` 之后再调用处理器是空操作(不会再问外部推理);后端抛 `OSError` 时上抛、不写决策、不写失败记录;`construct_directive` 抛 `OSError` 时决策保持 `pending`、不写失败记录、下一次调用续写;`RuntimeError`、配置错各一条上抛测试。`TemplateError` 走同一条上抛路径,没有单独的测试。
- **校验违规进入重试**:测试让第一次回答引用不存在的配方,第二次合格;断言第二次的提示里含违规原因(计划 4 的 `ScriptedDecisionBackend.calls` 记下了提示)。
- **run id 按数值分配**(Ruling 22):树里有 `zz`、`test-x`、`run-9` 时照常规划,新 run 是 `run-010`(有测试)。防御性分支:`allocate_run_id` 抛 `RunIdOrderError` 时,决策以 `failed` 写出,`decision` 留痕、`next_run_id` 为 `None`,`attempts` 是规划已用的次数,人手建的同号目录原样保留(用 `monkeypatch` 覆盖,有测试)。
- **后端只用罐头与启发式**:`ScriptedDecisionBackend` 的脚本里放异常实例即可注入 `BridgeError` / `RuntimeError`;`HeuristicBackend` 跑一遍证明真实的确定性后端也能走通。

- [ ] **Step 1: 前置检查**

运行上文"前置检查命令"。Expected: `preflight ok`。

- [ ] **Step 2: 写失败测试**

新建 `tests/test_ts_plan_handler.py`:

```python
"""规划处理器:正常流程、校验违规进入重试、确定性失败记 T3、瞬态上抛、三条重入规则。"""
import pytest

from controlplane.config import write_config
from controlplane.paths import failure_record_file, model_file, planner_decision_file, scheduler_config_file
from controlplane.reconcile import Action
from inference_bridge import BridgeError
from trainplanner.backends import HeuristicBackend, ScriptedDecisionBackend
from trainplanner.templates import install_default_template
from trainscheduler import plan_handler as ph
from trainscheduler.config import install_default_configs
from trainscheduler.plan_handler import decision_id, make_plan_handler, plan_task_id
from ts_fixtures import (
    decision_json,
    env,
    make_store,
    write_decision,
    write_directive,
    write_model,
    write_recipe,
    write_report,
    write_round,
)

DIRECTIVE = "control-plane/runs/{}/directive.yaml"


@pytest.fixture
def store(tmp_path):
    s = make_store(tmp_path)
    install_default_configs(s)
    install_default_template(s)
    write_round(s, "run-001", {"tom": 0.40, "empathy": 0.70}, model_n=1)
    return s


def _act(run_id="run-001"):
    return Action(kind="plan_next_round", target_id=run_id, detail={})


def _decision(store, run_id="run-001"):
    return store.read_product(planner_decision_file(run_id))


def _commits(store):
    return int(store._git("rev-list", "--count", "HEAD"))


def test_a_new_decision_becomes_an_awaiting_directive_for_the_next_run(store):
    backend = ScriptedDecisionBackend([decision_json()])
    make_plan_handler(backend=backend)(store, _act())
    d = _decision(store)
    assert (d.id, d.type, d.status) == ("planner-run-001", "planner_decision", "executed")
    assert d.payload == {
        "source_run_id": "run-001", "next_run_id": "run-002", "template_version": "v1", "backend": "scripted",
        "decision": {"target_dimension": "tom", "action": "new", "mode": "batch", "source_recipe_id": None,
                     "perturbation": {}, "data_synthesis_hint": ""},
        "rationale": "because the evidence says so", "failure": None,
    }
    assert d.links == ["run-001", "run-002"]
    directive = store.read_product(DIRECTIVE.format("run-002"))
    assert directive.status == "awaiting_confirmation"
    assert directive.payload["planner_decision_ref"] == "planner-run-001"
    assert directive.payload["training_config"]["base_checkpoint_ref"]["uri"].endswith("cand-run-001.safetensors")
    assert [c[1:] for c in backend.calls] == [("run-001", "planner-attempt-1")]


def test_a_candidate_that_got_every_variant_family_right_is_still_planned(store):
    """设计 R21 的回归:真实诊断对全答对的族写 {"": 1.0}。以前装配原样交给计划 4、被拒、决策判 failed。"""
    from ts_fixtures import write_report

    write_report(store, "run-001", {"tom": 0.40, "empathy": 0.70},
                 error_profiles=[{"l2_dimension": "tom.false_belief", "track": "static",
                                  "error_type_distribution": {"": 1.0}}])
    make_plan_handler(backend=ScriptedDecisionBackend([decision_json()]))(store, _act())
    assert _decision(store).status == "executed"
    assert store.exists(DIRECTIVE.format("run-002"))


def test_confirm_directives_off_writes_a_pending_directive(store):
    write_config(store, scheduler_config_file(), "scheduler-config", {"confirm_directives": False}, "human")
    make_plan_handler(backend=ScriptedDecisionBackend([decision_json()]))(store, _act())
    assert store.read_product(DIRECTIVE.format("run-002")).status == "pending"


def test_the_next_run_id_skips_hand_named_runs_and_takes_the_largest_number(store):
    write_directive(store, "run-007")
    write_directive(store, "experiment-x")
    make_plan_handler(backend=ScriptedDecisionBackend([decision_json()]))(store, _act())
    assert _decision(store).payload["next_run_id"] == "run-008"
    assert store.exists(DIRECTIVE.format("run-008"))


def test_a_domain_violation_is_fed_back_and_the_retry_succeeds(store):
    backend = ScriptedDecisionBackend([
        decision_json(action="clone", source_recipe_id="recipe-run-404"),
        decision_json(),
    ])
    make_plan_handler(backend=backend)(store, _act())
    assert _decision(store).status == "executed"
    assert len(backend.calls) == 2
    assert "recipe 'recipe-run-404' does not exist" in backend.calls[1][0]


def test_a_clone_decision_reuses_the_recipe(store):
    write_recipe(store, "run-001")
    backend = ScriptedDecisionBackend([decision_json(action="clone", source_recipe_id="recipe-run-001",
                                                     perturbation={"opd_coefficient": 0.3})])
    make_plan_handler(backend=backend)(store, _act())
    directive = store.read_product(DIRECTIVE.format("run-002"))
    assert directive.payload["recipe_ref"] == "recipe-run-001"
    assert directive.payload["training_config"]["opd_coefficient"] == 0.3
    assert "data_spec" not in directive.payload


def test_the_heuristic_backend_plans_the_lowest_dimension(store):
    make_plan_handler(backend=HeuristicBackend())(store, _act())
    d = _decision(store)
    assert d.payload["backend"] == "heuristic"
    assert d.payload["decision"]["target_dimension"] == "tom"
    assert store.read_product(DIRECTIVE.format("run-002")).payload["target_dimension"] == "tom"


def test_planner_exhaustion_fails_the_decision_with_a_t3_record(store):
    backend = ScriptedDecisionBackend(["not json"] * 3)
    make_plan_handler(backend=backend)(store, _act())
    d = _decision(store)
    assert d.status == "failed"
    assert d.payload["next_run_id"] is None and d.payload["decision"] is None
    assert d.payload["failure"]["category"] == "T3" and d.payload["failure"]["attempts"] == 3
    assert "parse error" in d.payload["failure"]["reason"]
    rec = store.read_product(failure_record_file("run-001", plan_task_id("run-001")))
    assert rec.payload["task_id"] == "plan-run-001"
    assert (rec.payload["category"], rec.payload["point"], rec.payload["retries"]) == ("T3", "trainscheduler.plan", 2)
    assert not store.exists(DIRECTIVE.format("run-002"))


def test_max_plan_attempts_comes_from_the_scheduler_config(store):
    write_config(store, scheduler_config_file(), "scheduler-config", {"max_plan_attempts": 1}, "human")
    backend = ScriptedDecisionBackend(["nope", decision_json()])
    make_plan_handler(backend=backend)(store, _act())
    assert _decision(store).status == "failed" and len(backend.calls) == 1


def test_repeated_violations_exhaust_the_shared_budget(store):
    backend = ScriptedDecisionBackend([decision_json(mode="interact")] * 3)
    make_plan_handler(backend=backend)(store, _act())
    assert _decision(store).status == "failed"
    assert "allow_interact" in _decision(store).payload["failure"]["reason"]


def test_stub_evidence_fails_the_decision_without_calling_the_backend(store):
    write_report(store, "run-001", stub=True)
    backend = ScriptedDecisionBackend([])
    make_plan_handler(backend=backend)(store, _act())
    d = _decision(store)
    assert d.status == "failed" and "graded stub" in d.payload["failure"]["reason"]
    assert d.payload["failure"]["attempts"] == 0 and backend.calls == []
    assert store.exists(failure_record_file("run-001", "plan-run-001"))


def test_a_resolve_error_after_the_decision_was_recorded_fails_it(store):
    store.write_product(model_file(2), env("zengen-v2", "zengen_model", "effective", {"promoted_from": "x"}), "t")
    make_plan_handler(backend=ScriptedDecisionBackend([decision_json()]))(store, _act())
    d = _decision(store)
    assert d.status == "failed"
    assert d.payload["next_run_id"] == "run-002"  # 已分配,留痕
    assert "no checkpoint_ref" in d.payload["failure"]["reason"]
    assert not store.exists(DIRECTIVE.format("run-002"))
    assert store.exists(failure_record_file("run-001", "plan-run-001"))


@pytest.mark.parametrize("category", ["T1", "T2"])
def test_any_bridge_error_fails_the_decision_as_t3_and_does_not_propagate(store, category):
    """R18:T1 表示桥接已用尽自己的重试,与 T2 一样升级为 T3,不留 pending、不上抛。"""
    from controlplane.envelope import BigRef

    log = BigRef("file:///logs/plan-run-001-1.log", "d" * 64, 10)
    backend = ScriptedDecisionBackend([BridgeError(category, "cli timed out", attempts=3, log_ref=log)])
    make_plan_handler(backend=backend)(store, _act())  # 不抛
    d = _decision(store)
    assert d.status == "failed"
    assert d.payload["next_run_id"] is None and d.payload["decision"] is None
    assert d.payload["failure"] == {
        "category": "T3",
        "reason": f"bridge {category} after 3 bridge attempt(s): cli timed out",
        "attempts": 1,
    }
    rec = store.read_product(failure_record_file("run-001", plan_task_id("run-001"))).payload
    assert (rec["category"], rec["point"], rec["retries"]) == ("T3", "trainscheduler.plan", 0)
    assert rec["log_refs"] == [log.to_dict()]
    assert not store.exists(DIRECTIVE.format("run-002"))


def test_a_bridge_error_on_a_retry_counts_the_planner_attempts(store):
    backend = ScriptedDecisionBackend(["not json", BridgeError("T1", "down")])
    make_plan_handler(backend=backend)(store, _act())
    assert _decision(store).payload["failure"]["attempts"] == 2
    rec = store.read_product(failure_record_file("run-001", plan_task_id("run-001"))).payload
    assert rec["retries"] == 1 and rec["log_refs"] == []


def test_a_failed_decision_is_not_replanned_after_a_bridge_error(store):
    make_plan_handler(backend=ScriptedDecisionBackend([BridgeError("T1", "down")]))(store, _act())
    n = _commits(store)
    make_plan_handler(backend=ScriptedDecisionBackend([]))(store, _act())  # 再调就会 AssertionError
    assert _commits(store) == n


def test_an_os_error_from_the_backend_propagates_and_writes_nothing(store):
    backend = ScriptedDecisionBackend([OSError("broken pipe")])
    with pytest.raises(OSError, match="broken pipe"):
        make_plan_handler(backend=backend)(store, _act())
    assert not store.exists(planner_decision_file("run-001"))
    assert not store.exists(failure_record_file("run-001", "plan-run-001"))


def test_any_other_exception_propagates(store):
    backend = ScriptedDecisionBackend([RuntimeError("boom")])
    with pytest.raises(RuntimeError, match="boom"):
        make_plan_handler(backend=backend)(store, _act())
    assert not store.exists(planner_decision_file("run-001"))


def test_a_broken_scheduler_config_propagates_instead_of_failing_the_run(store):
    from trainscheduler.errors import SchedulerConfigError

    write_config(store, scheduler_config_file(), "scheduler-config", {"min_gain": "x"}, "human")
    with pytest.raises(SchedulerConfigError):
        make_plan_handler(backend=ScriptedDecisionBackend([]))(store, _act())
    assert not store.exists(planner_decision_file("run-001"))


def test_reentry_a_pending_decision_without_directive_is_resumed_without_replanning(store):
    write_decision(store, "run-001", "run-005", status="pending")
    backend = ScriptedDecisionBackend([])  # 一调就抛 AssertionError
    make_plan_handler(backend=backend)(store, _act())
    assert backend.calls == []
    assert _decision(store).status == "executed"
    assert store.read_product(DIRECTIVE.format("run-005")).payload["planner_decision_ref"] == "planner-run-001"
    assert not store.exists(DIRECTIVE.format("run-002"))  # 没有重新分配


def test_hand_named_and_unpadded_run_dirs_do_not_block_planning(store):
    """数值序(设计 §3.2):zz、test-x 不参与比较,run-9 按编号 9 参与;新 run 取 run-010。"""
    for name in ("zz", "test-x", "run-9"):
        write_directive(store, name)
    make_plan_handler(backend=ScriptedDecisionBackend([decision_json()]))(store, _act())
    assert _decision(store).status == "executed"
    assert _decision(store).payload["next_run_id"] == "run-010"
    assert store.exists(DIRECTIVE.format("run-010"))


def test_a_hand_made_dir_with_the_allocated_number_fails_the_decision(store, monkeypatch):
    """防御性分支:正常路径走不到。让编号计算"看漏"一个人手建的 run-002,分配器拒绝,决策判 T3 留痕。"""
    import trainscheduler.run_ids as rid

    write_directive(store, "run-002", status="pending")
    monkeypatch.setattr(rid, "next_run_id", lambda existing: "run-002")
    make_plan_handler(backend=ScriptedDecisionBackend([decision_json()]))(store, _act())
    d = _decision(store)
    assert d.status == "failed" and d.payload["next_run_id"] is None
    assert d.payload["decision"]["action"] == "new"  # 规划结果留痕
    assert "not numerically greater than existing run dir(s) ['run-002']" in d.payload["failure"]["reason"]
    assert d.payload["failure"]["attempts"] == 1
    assert store.exists(failure_record_file("run-001", "plan-run-001"))
    assert store.read_product(DIRECTIVE.format("run-002")).status == "pending"  # 人手建的那条原样保留


@pytest.mark.parametrize("bad", [None, "", "../escape", "run-x"])
def test_reentry_a_with_a_broken_next_run_id_fails_instead_of_writing_a_bogus_directive(store, bad):
    write_decision(store, "run-001", bad, status="pending")
    make_plan_handler(backend=ScriptedDecisionBackend([]))(store, _act())
    d = _decision(store)
    assert d.status == "failed" and "no usable next_run_id" in d.payload["failure"]["reason"]
    assert not (store.repo_root / "control-plane/runs/None").exists()
    assert not store.exists(DIRECTIVE.format("run-x"))
    assert store.exists(failure_record_file("run-001", "plan-run-001"))


@pytest.mark.parametrize(
    "mutate, fragment",
    [
        (lambda p: p.update(decision=None), "recorded decision is not a mapping"),
        (lambda p: p["decision"].update(action="teleport"), "recorded decision is malformed: action must be one of"),
        (lambda p: p["decision"].pop("mode"), "recorded decision is malformed: missing keys"),
        (lambda p: p.update(rationale=""), "recorded decision is malformed: rationale must be a non-empty string"),
    ],
)
def test_reentry_a_with_a_corrupted_decision_payload_fails_deterministically(store, mutate, fragment):
    write_decision(store, "run-001", "run-002", status="pending")
    env_ = _decision(store)
    mutate(env_.payload)
    store.write_product(planner_decision_file("run-001"), env_, "human: corrupt")
    make_plan_handler(backend=ScriptedDecisionBackend([]))(store, _act())  # 不抛
    d = _decision(store)
    assert d.status == "failed" and fragment in d.payload["failure"]["reason"]
    assert not store.exists(DIRECTIVE.format("run-002"))
    n = _commits(store)
    make_plan_handler(backend=ScriptedDecisionBackend([]))(store, _act())  # 下一个 tick 是空操作
    assert _commits(store) == n


def test_reentry_a_crash_while_writing_the_directive_resumes_with_the_same_run_id(store, monkeypatch):
    real = ph.construct_directive
    calls = []

    def flaky(*args, **kwargs):
        calls.append(kwargs["new_run_id"])
        if len(calls) == 1:
            raise OSError("disk hiccup")
        return real(*args, **kwargs)

    monkeypatch.setattr(ph, "construct_directive", flaky)
    backend = ScriptedDecisionBackend([decision_json()])
    handler = make_plan_handler(backend=backend)
    with pytest.raises(OSError):
        handler(store, _act())
    # 非桥接的瞬态异常:决策保持 pending,不写失败记录(R18 只升级 BridgeError)
    assert _decision(store).status == "pending" and _decision(store).payload["next_run_id"] == "run-002"
    assert not store.exists(failure_record_file("run-001", "plan-run-001"))
    handler(store, _act())
    assert calls == ["run-002", "run-002"]
    assert len(backend.calls) == 1
    assert _decision(store).status == "executed"


def test_reentry_b_a_pending_decision_whose_directive_exists_is_only_marked(store):
    write_decision(store, "run-001", "run-002", status="pending")
    write_directive(store, "run-002", status="awaiting_confirmation")
    before = store.read_product(DIRECTIVE.format("run-002")).to_dict()
    n = _commits(store)
    make_plan_handler(backend=ScriptedDecisionBackend([]))(store, _act())
    assert _decision(store).status == "executed"
    assert store.read_product(DIRECTIVE.format("run-002")).to_dict() == before
    assert _commits(store) == n + 1


@pytest.mark.parametrize("status", ["executed", "failed"])
def test_reentry_c_a_finished_decision_is_a_no_op(store, status):
    write_decision(store, "run-001", "run-002" if status == "executed" else None, status=status)
    n = _commits(store)
    make_plan_handler(backend=ScriptedDecisionBackend([]))(store, _act())
    assert _commits(store) == n
    assert not store.exists(DIRECTIVE.format("run-002"))


def test_running_twice_plans_once(store):
    backend = ScriptedDecisionBackend([decision_json()])
    handler = make_plan_handler(backend=backend)
    handler(store, _act())
    n = _commits(store)
    handler(store, _act())
    assert _commits(store) == n and len(backend.calls) == 1


def test_loaders_are_injectable(store):
    from trainscheduler.config import DEFAULT_SCHEDULER_CONFIG, SchedulerConfig, load_training_defaults
    from trainplanner.templates import load_template

    seen = []

    def config_loader(s):
        seen.append("config")
        return SchedulerConfig(confirm_directives=False)

    def template_loader(s, version):
        seen.append(("template", version))
        return load_template(s, version)

    def defaults_loader(s):
        seen.append("defaults")
        return load_training_defaults(s)

    make_plan_handler(backend=ScriptedDecisionBackend([decision_json()]), template_loader=template_loader,
                      config_loader=config_loader, defaults_loader=defaults_loader)(store, _act())
    assert seen == ["config", "defaults", ("template", DEFAULT_SCHEDULER_CONFIG.template_version)]
    assert store.read_product(DIRECTIVE.format("run-002")).status == "pending"


def test_decision_id_and_task_id():
    assert (decision_id("run-003"), plan_task_id("run-003")) == ("planner-run-003", "plan-run-003")


def test_a_promoted_model_from_a_later_run_is_the_base(store):
    write_round(store, "run-002", {"tom": 0.5}, model_n=2, baseline_ref="zengen-v1")
    make_plan_handler(backend=ScriptedDecisionBackend([decision_json()]))(store, _act("run-002"))
    directive = store.read_product(DIRECTIVE.format("run-003"))
    assert directive.payload["training_config"]["base_checkpoint_ref"]["uri"].endswith("cand-run-002.safetensors")
```

- [ ] **Step 3: 运行,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_ts_plan_handler.py`
Expected: 收集阶段报错 `ImportError: cannot import name 'plan_handler' from 'trainscheduler'`,`1 error`。

- [ ] **Step 4: 写实现**

新建 `src/trainscheduler/plan_handler.py`:

```python
"""规划处理器(设计 §7.2、§4.1、§3.2):plan_next_round 动作的 Handler。

流程:装配上下文 → plan(validator = validate_decision)→ 分配 run id → 写 planner_decision(pending,
带 next_run_id)→ 解析动作 → 写指令 → 决策置 executed。

重入(崩在任何一步之后再被调用):
  (a) 决策 pending、带 next_run_id、那条指令还不在:按记下的决策与 run id 续写,不重新规划、不重新分配。
  (b) 决策 pending、next_run_id 的指令已在:只把决策补成 executed,不写第二条指令。
  (c) 决策已是 executed 或 failed:空操作。

失败:
  - PlannerFailedError(规划耗尽)、装配/解析/构造的确定性错误(SchedulerError)、任何 BridgeError
    (T1 与 T2 都算,设计 R18:T1 表示桥接已用尽自己的退避重试):决策置 failed
    (payload.failure = {category: T3, reason, attempts}),record_failure 写 T3,task_id = plan-<run>,不上抛。
  - 非桥接的异常(OSError、子进程错误、配置错 SchedulerConfigError、模板错 TemplateError 等):原样上抛,
    决策保持原状(没写过就不写,已是 pending 就仍是 pending),下一轮重来。
"""
from __future__ import annotations

from collections.abc import Callable

from controlplane.envelope import CURRENT_SCHEMA_VERSION, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.ledger import record_failure
from controlplane.paths import planner_decision_file
from controlplane.reconcile import Action
from controlplane.states import NONREVIEW, assert_transition
from inference_bridge import BridgeError
from trainplanner.backends import DecisionBackend
from trainplanner.decision import DecisionParseError, PlannerDecision
from trainplanner.planner import PlannerFailedError, plan
from trainplanner.templates import PromptTemplate, load_template
from trainscheduler.assemble import assemble_context, directive_file
from trainscheduler.config import (
    SchedulerConfig,
    TrainingDefaults,
    load_scheduler_config,
    load_training_defaults,
)
from trainscheduler.directive import construct_directive
from trainscheduler.errors import ResolveError, SchedulerError
from trainscheduler.resolve import resolve_action, validate_decision
from trainscheduler.run_ids import allocate_run_id, run_number

Handler = Callable[[GitStore, Action], None]
TemplateLoader = Callable[[GitStore, str], PromptTemplate]
ConfigLoader = Callable[[GitStore], SchedulerConfig]
DefaultsLoader = Callable[[GitStore], TrainingDefaults]

NOW = "2026-10-09T00:00:00Z"
FAILURE_CATEGORY = "T3"
FAILURE_POINT = "trainscheduler.plan"


def decision_id(run_id: str) -> str:
    return f"planner-{run_id}"


def plan_task_id(run_id: str) -> str:
    return f"plan-{run_id}"


def _decision_envelope(source_run_id: str, payload: dict, status: str) -> Envelope:
    links = [source_run_id] + ([payload["next_run_id"]] if payload.get("next_run_id") else [])
    return Envelope(
        id=decision_id(source_run_id), type="planner_decision", version=1, created_at=NOW,
        created_by=OwnerRef(), status=status, schema_version=CURRENT_SCHEMA_VERSION,
        links=links, payload=payload,
    )


def _base_payload(source_run_id: str, template_version: str, backend: str) -> dict:
    return {
        "source_run_id": source_run_id,
        "next_run_id": None,
        "template_version": template_version,
        "backend": backend,
        "decision": None,
        "rationale": "",
        "failure": None,
    }


def _fail(
    store: GitStore, run_id: str, payload: dict, reason: str, attempts: int, *, log_ref=None
) -> None:
    """把决策置 failed(没写过就直接以 failed 写出),并写一份 T3 失败记录。"""
    path = planner_decision_file(run_id)
    payload = dict(payload)
    payload["failure"] = {"category": FAILURE_CATEGORY, "reason": reason, "attempts": attempts}
    if store.exists(path):
        assert_transition(NONREVIEW, store.read_product(path).status, "failed")
    store.write_product(
        path, _decision_envelope(run_id, payload, "failed"), f"trainscheduler: plan {run_id} failed"
    )
    record_failure(
        store, run_id, task_id=plan_task_id(run_id), category=FAILURE_CATEGORY,
        point=FAILURE_POINT, retries=max(attempts - 1, 0), reason=reason, log_ref=log_ref,
    )


def _mark_executed(store: GitStore, run_id: str) -> None:
    path = planner_decision_file(run_id)
    env = store.read_product(path)
    assert_transition(NONREVIEW, env.status, "executed")
    env.status = "executed"
    store.write_product(path, env, f"trainscheduler: plan {run_id} executed")


def _recorded_decision(payload: dict) -> PlannerDecision:
    """从 pending 决策的载荷里读回决策。载荷被人改坏(缺键、类型不对、过不了解析规则)抛 ResolveError,
    不让 DecisionParseError、TypeError 之类每个 tick 逃出守护循环。"""
    decision = payload.get("decision")
    if not isinstance(decision, dict):
        raise ResolveError(f"recorded decision is not a mapping: {type(decision).__name__}")
    try:
        return PlannerDecision.from_dict({**decision, "rationale": payload.get("rationale")})
    except DecisionParseError as exc:
        raise ResolveError(f"recorded decision is malformed: {exc.reason}") from exc


def _usable_next_run_id(value: object) -> bool:
    """只接受 run-<数字>:分配器只产出这种形状;空值、None、带斜杠的路径都是被改坏的载荷。"""
    return isinstance(value, str) and run_number(value) is not None


def _execute(store: GitStore, run_id: str, payload: dict, config: SchedulerConfig, defaults: TrainingDefaults) -> None:
    """决策已以 pending 写下:解析、写指令、标记 executed。确定性错误把决策置 failed。"""
    try:
        if not _usable_next_run_id(payload.get("next_run_id")):
            raise ResolveError(f"pending decision has no usable next_run_id: {payload.get('next_run_id')!r}")
        resolved = resolve_action(store, _recorded_decision(payload), config=config, defaults=defaults)
        construct_directive(
            store, resolved, new_run_id=payload["next_run_id"],
            confirm=config.confirm_directives, planner_decision_id=decision_id(run_id),
        )
    except SchedulerError as exc:
        _fail(store, run_id, payload, exc.reason, attempts=0)
        return
    _mark_executed(store, run_id)


class _CountingBackend:
    """透传给真实后端,只数调用次数:桥接失败时写进 failure.attempts 的是"规划已问了几次"。"""

    def __init__(self, inner: DecisionBackend) -> None:
        self._inner = inner
        self.name = inner.name
        self.calls = 0

    def decide(self, packed, *, run_id: str, task_id: str) -> str:
        self.calls += 1
        return self._inner.decide(packed, run_id=run_id, task_id=task_id)


def make_plan_handler(
    *,
    backend: DecisionBackend,
    template_loader: TemplateLoader = load_template,
    config_loader: ConfigLoader = load_scheduler_config,
    defaults_loader: DefaultsLoader = load_training_defaults,
) -> Handler:
    """三个 loader 每次调用处理器时各调一次,所以人改了树里的配置,下一次规划就生效。"""

    def handle(store: GitStore, action: Action) -> None:
        run_id = action.target_id
        path = planner_decision_file(run_id)
        if store.exists(path):
            env = store.read_product(path)
            if env.status != "pending":
                return  # (c)
            next_run = env.payload.get("next_run_id")
            if _usable_next_run_id(next_run) and store.exists(directive_file(next_run)):
                _mark_executed(store, run_id)  # (b)
                return
            _execute(store, run_id, env.payload, config_loader(store), defaults_loader(store))  # (a)
            return

        config = config_loader(store)
        defaults = defaults_loader(store)
        template = template_loader(store, config.template_version)
        payload = _base_payload(run_id, template.version, backend.name)
        counted = _CountingBackend(backend)
        try:
            ctx = assemble_context(store, run_id, config=config)
            result = plan(
                ctx, template.text, template.version, counted, run_id=run_id,
                max_parse_retries=config.max_plan_attempts - 1,
                validator=lambda d: validate_decision(store, d, config=config),
            )
        except PlannerFailedError as exc:
            _fail(store, run_id, payload, exc.reason, attempts=exc.attempts)
            return
        except SchedulerError as exc:
            _fail(store, run_id, payload, exc.reason, attempts=0)
            return
        except BridgeError as exc:
            # R18:T1 与 T2 一律升级为 T3。桥接已退避重试过,留作 pending 会让守护循环重启后无限空转外部推理
            _fail(
                store, run_id, payload,
                f"bridge {exc.category} after {exc.attempts} bridge attempt(s): {exc.reason}",
                attempts=counted.calls, log_ref=exc.log_ref,
            )
            return
        payload.update(result.record_fields())
        try:
            payload["next_run_id"] = allocate_run_id(store)
        except SchedulerError as exc:
            _fail(store, run_id, payload, exc.reason, attempts=result.attempts)
            return
        store.write_product(
            path, _decision_envelope(run_id, payload, "pending"),
            f"trainscheduler: plan {run_id} -> {payload['next_run_id']}",
        )
        _execute(store, run_id, payload, config, defaults)

    return handle
```

- [ ] **Step 5: 运行,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_ts_plan_handler.py`
Expected: `38 passed`,无警告。

- [ ] **Step 6: 全量回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: **B+193(1736)** passed,无警告(撰写时实测数见括号)。再跑 `git status --short`:除原本就有的 `cli/package*.json` 外,只应出现本任务 Files 里的文件。

- [ ] **Step 7: 提交**

```bash
git add src/trainscheduler/plan_handler.py
git add tests/test_ts_plan_handler.py
git commit -m "feat: trainscheduler plan handler (decision before directive, three re-entry rules, planner exhaustion and resolve errors fail as T3)"
```

---

### Task 66: 配方提名 `nomination_check`、`build_recipe_candidate`、`make_nominate_handler`

**Files:**
- Create: `src/trainscheduler/nominate.py`(226 行)
- Test: `tests/test_ts_nominate.py`(34 个用例)

**Interfaces:**
- Consumes: `controlplane.evidence.diagnostic_grade` / `evidence_acceptable`;`controlplane.paths.candidate_checkpoint_file` / `dataset_manifest_file` / `nomination_file` / `recipe_candidate_file`;`mindtrainer.data_spec.manifest_data_run`;`recipes_port.compute_gain` / `read_report_payload` / `recipe_id_for` / `regression_flags` / `validate_reproducibility`;任务 60 的 `SchedulerConfig`、`load_scheduler_config`;任务 62 的 `directive_file`。
- Produces(计划 8 注入、计划 9 组装):
  - `NominationVerdict(nominate: bool, reason: str, gain: float | None, baseline_run_id: str | None, not_yet: bool = False)`(冻结)。`not_yet` 为真表示前置条件还没到(Ruling 17),处理器不写标记。
  - `nomination_check(store, run_id, *, config, accept_mock_evidence=None) -> NominationVerdict`。
  - `build_recipe_candidate(store, run_id, *, config, repo_commit=None, accept_mock_evidence=None) -> Envelope`(不写树;不可提名抛 `ValueError`)。
  - `make_nominate_handler(*, config_loader=load_scheduler_config, accept_mock_evidence=None, repo_commit=None) -> Handler`。
  - 常量 `NOMINATED = "nominated"`、`SKIPPED = "skipped"`、`NOMINABLE_TYPES = ("batch", "interact")`、`EVAL_ANCHOR_KEYS`。

**设计要点:**
- **判定顺序**见 Ruling 17;每条不提名都有测试,原因写进 `reason`。**"还没到"不写标记**:候选 `pending_review` / `approved` 时、缺报告时,处理器零提交返回;候选晋级、报告补上之后再触发,照常提名(有测试)。候选 `rejected` 是终局,写 `skipped`(有测试)。`real` 候选对 `mock` 基线时计划 3 的 `compute_gain` 判不可比(`gain=None`),不提名(有测试);所以"`real` 证据不用开关"那条测试把基线报告也写成 `real`。`accept_mock_evidence` 的来源见 Ruling 4,有测试证明参数覆盖配置(两个方向)。
- **只提名 batch 与 interact**(设计 §15):指令的 `training_config.mode` 不是二者之一(含没有 `training_config` 的人工指令)就 skip。`agent_rsi` 配方没有训练 run,V1 不会被提名。
- **候选载荷**照设计 §4.2 与计划 3 的校验器:`recipe_type`(= 指令的 `mode`)、`dimension`、`source_run_id`、`eval_anchor`(五键)、`result{gain, baseline_ref, candidate_ref, promoted: true}`、`nondeterministic`(interact 恒真)、`training_config`(指令原样,深拷贝)、`code_version{repo_commit}`;batch 加 `stage_manifests{阶段: {id, manifest_hash}}`(哈希从清单产物读,数据所在 run 用 `manifest_data_run` 从清单 id 解出),interact 加 `scenario_ids`、`bindings`;最后 `repro = validate_reproducibility(其余载荷).to_dict()`,errors 与 warnings 原样写入(有测试逐字比对)。缺口如实成为错误(Ruling 16)。
- **候选信封**:id `recipe-<run>`、类型 `recipe_candidate`、状态 `pending_review`、`links=[run, cand-<run>]`,写到 `recipe_candidate_file(run_id)`。产物带 `repro`,所以过得了计划 3 的 `has_repro_evidence`(审核门③的证据在场规则)。
- **提名标记**:id `nomination-<run>`、类型 `nomination_note`、状态 `executed`,载荷 `{result, reason, gain, baseline_run_id, recipe_candidate_id}`,写到 `nomination_file(run_id)`;它是幂等键。
- **保护已审核的候选**(Ruling 18):测试对 `pending_review`、`approved`、`rejected`、`effective` 四种状态各跑一次:人改过的候选在删掉标记后重跑处理器,候选逐字节不变,只补一条标记。

- [ ] **Step 1: 前置检查**

运行上文"前置检查命令"。Expected: `preflight ok`。

- [ ] **Step 2: 写失败测试**

新建 `tests/test_ts_nominate.py`:

```python
"""配方提名:判定的每条不提名原因、候选载荷形状、可复现性报告原样写入、处理器幂等且不改已审核的候选。"""
import copy

import pytest

from controlplane.paths import dataset_manifest_file, nomination_file, recipe_candidate_file
from controlplane.reconcile import Action
from recipestore.admit import has_repro_evidence
from recipestore.validate import validate_reproducibility
from trainscheduler.config import SchedulerConfig
from trainscheduler.nominate import (
    NominationVerdict,
    build_recipe_candidate,
    make_nominate_handler,
    nomination_check,
)
from ts_fixtures import env, make_store, recipe_payload, write_directive, write_round

MOCK_OK = SchedulerConfig(accept_mock_evidence=True)


def _manifest(store, run_id, stage, h):
    store.write_product(dataset_manifest_file(run_id, stage),
                        env(f"dsm-{run_id}-{stage}", "dataset_manifest", "executed",
                            {"id": f"dsm-{run_id}-{stage}", "run_id": run_id, "name": stage,
                             "episode_refs": [], "manifest_hash": h}), "test: manifest")


@pytest.fixture
def store(tmp_path):
    """run-001 晋级为 zengen-v1(基线);run-002 以它为基线,tom 涨了 0.05。"""
    s = make_store(tmp_path)
    write_round(s, "run-001", {"tom": 0.40}, model_n=1)
    write_round(s, "run-002", {"tom": 0.45}, model_n=2, baseline_ref="zengen-v1")
    _manifest(s, "run-002", "sft", "b" * 64)
    _manifest(s, "run-002", "grpo_opd", "c" * 64)
    return s


def _act(run_id="run-002"):
    return Action(kind="nominate_recipe", target_id=run_id, detail={})


def test_a_gain_above_min_gain_with_acceptable_evidence_is_nominated(store):
    v = nomination_check(store, "run-002", config=MOCK_OK)
    assert v.nominate and v.gain == pytest.approx(0.05) and v.baseline_run_id == "run-001"
    assert "min_gain 0.01" in v.reason


def test_mock_evidence_is_refused_by_default(store):
    v = nomination_check(store, "run-002", config=SchedulerConfig())
    assert v == NominationVerdict(False, "diagnostic evidence for run-002 is mock, which is not acceptable", None, None)


def test_the_parameter_overrides_the_config_for_mock_evidence(store):
    assert nomination_check(store, "run-002", config=SchedulerConfig(), accept_mock_evidence=True).nominate
    assert not nomination_check(store, "run-002", config=MOCK_OK, accept_mock_evidence=False).nominate


def test_real_evidence_needs_no_switch(store):
    from ts_fixtures import write_report

    # 计划 3 的 compute_gain 不拿 real 候选减 mock 基线,所以基线 run-002 的报告也要是 real
    write_report(store, "run-002", {"tom": 0.45}, baseline_ref="zengen-v1", backends="real")
    write_round(store, "run-003", {"tom": 0.50}, baseline_ref="zengen-v2", backends="real")
    assert nomination_check(store, "run-003", config=SchedulerConfig()).nominate


def test_real_evidence_against_a_mock_baseline_is_not_comparable(store):
    write_round(store, "run-003", {"tom": 0.50}, baseline_ref="zengen-v2", backends="real")
    v = nomination_check(store, "run-003", config=SchedulerConfig())
    assert not v.nominate and not v.not_yet
    assert v.reason.startswith("no comparable gain:") and v.baseline_run_id == "run-002"


def test_stub_evidence_is_never_nominated(store):
    from ts_fixtures import write_report

    write_report(store, "run-002", stub=True)
    v = nomination_check(store, "run-002", config=MOCK_OK)
    assert not v.nominate and "is stub" in v.reason


def test_no_comparable_baseline_is_not_nominated(store):
    v = nomination_check(store, "run-001", config=MOCK_OK)  # 报告没有 baseline_ref
    assert not v.nominate and v.reason.startswith("no comparable gain:") and v.gain is None


def test_a_gain_below_min_gain_is_not_nominated(store):
    v = nomination_check(store, "run-002", config=SchedulerConfig(accept_mock_evidence=True, min_gain=0.1))
    assert not v.nominate and "below min_gain 0.1" in v.reason and v.gain == pytest.approx(0.05)


def test_a_negative_gain_is_not_nominated(store):
    write_round(store, "run-003", {"tom": 0.30}, baseline_ref="zengen-v2")
    v = nomination_check(store, "run-003", config=MOCK_OK)
    assert not v.nominate and v.gain == pytest.approx(-0.15)


def test_regression_flags_block_nomination(store):
    write_round(store, "run-003", {"tom": 0.60}, baseline_ref="zengen-v2",
                flags=[{"dimension": "empathy", "kind": "social_regression", "drop": 0.1}])
    v = nomination_check(store, "run-003", config=MOCK_OK)
    assert not v.nominate and "regression flags ['social_regression']" in v.reason


@pytest.mark.parametrize("status", ["pending_review", "approved", "rejected"])
def test_only_effective_candidates_are_nominated(store, status):
    write_round(store, "run-003", {"tom": 0.6}, baseline_ref="zengen-v2", candidate_status=status)
    v = nomination_check(store, "run-003", config=MOCK_OK)
    assert not v.nominate and f"is {status}" in v.reason


def test_a_directive_without_a_training_config_is_not_nominated(store):
    write_round(store, "run-003", {"tom": 0.6}, baseline_ref="zengen-v2")
    from controlplane.paths import run_dir

    d = store.read_product(f"{run_dir('run-003')}/directive.yaml")
    del d.payload["training_config"]
    store.write_product(f"{run_dir('run-003')}/directive.yaml", d, "test")
    v = nomination_check(store, "run-003", config=MOCK_OK)
    assert not v.nominate and "not a batch or interact training directive" in v.reason


def test_missing_pieces_are_reasons_not_exceptions(tmp_path):
    s = make_store(tmp_path)
    assert "has no candidate" in nomination_check(s, "run-009", config=MOCK_OK).reason
    write_directive(s, "run-009")
    from ts_fixtures import write_candidate

    write_candidate(s, "run-009")
    assert "has no diagnostic report" in nomination_check(s, "run-009", config=MOCK_OK).reason


def test_the_candidate_payload_has_the_recipe_shape(store):
    c = build_recipe_candidate(store, "run-002", config=MOCK_OK, repo_commit="0385004")
    assert (c.id, c.type, c.status) == ("recipe-run-002", "recipe_candidate", "pending_review")
    assert c.links == ["run-002", "cand-run-002"]
    p = c.payload
    assert set(p) == {"recipe_type", "dimension", "source_run_id", "eval_anchor", "result", "repro",
                      "nondeterministic", "training_config", "stage_manifests", "code_version"}
    assert (p["recipe_type"], p["dimension"], p["source_run_id"], p["nondeterministic"]) == (
        "batch", "tom", "run-002", False)
    assert p["eval_anchor"] == {"taxonomy_version": None, "item_pool_version": None, "benchmark_version": "bench-v1",
                                "judge_prompt_version": None, "scoring_config_version": None}
    assert p["result"] == {"gain": pytest.approx(0.05), "baseline_ref": "zengen-v1",
                           "candidate_ref": "cand-run-002", "promoted": True}
    assert p["stage_manifests"] == {"grpo_opd": {"id": "dsm-run-002-grpo_opd", "manifest_hash": "c" * 64},
                                    "sft": {"id": "dsm-run-002-sft", "manifest_hash": "b" * 64}}
    assert p["code_version"] == {"repo_commit": "0385004"}
    assert p["training_config"] == store.read_product("control-plane/runs/run-002/directive.yaml").payload[
        "training_config"]


def test_repro_is_the_validator_output_verbatim(store):
    c = build_recipe_candidate(store, "run-002", config=MOCK_OK, repo_commit="0385004")
    without = {k: v for k, v in c.payload.items() if k != "repro"}
    assert c.payload["repro"] == validate_reproducibility(without).to_dict()
    # 分类表与题库版本诊断报告里没有,如实记成错误交给审核门③
    assert c.payload["repro"]["errors"] == ["eval_anchor.taxonomy_version: missing",
                                            "eval_anchor.item_pool_version: missing"]
    assert has_repro_evidence(c.payload)


def test_an_unknown_commit_and_a_missing_manifest_are_repro_errors(store):
    from controlplane.paths import run_dir
    import os

    os.remove(store.repo_root / dataset_manifest_file("run-002", "sft"))
    c = build_recipe_candidate(store, "run-002", config=MOCK_OK)
    assert c.payload["code_version"] == {"repo_commit": None}
    assert c.payload["stage_manifests"]["sft"] == {"id": "dsm-run-002-sft", "manifest_hash": None}
    errors = c.payload["repro"]["errors"]
    assert "code_version.repo_commit: must be a 7-40 char lowercase hex commit id" in errors
    assert "stage_manifests.sft.manifest_hash: must be a 64-char lowercase hex digest" in errors


def test_an_interact_run_becomes_an_interact_candidate(store):
    cfg = recipe_payload("run-003", recipe_type="interact")["training_config"]
    write_round(store, "run-003", {"tom": 0.6}, baseline_ref="zengen-v2")
    write_directive(store, "run-003", training_config=cfg)
    p = build_recipe_candidate(store, "run-003", config=MOCK_OK, repo_commit="0385004").payload
    assert (p["recipe_type"], p["nondeterministic"]) == ("interact", True)
    assert p["scenario_ids"] == ["scen-1", "scen-2"] and p["bindings"] == cfg["bindings"]
    assert "stage_manifests" not in p
    assert any("nondeterministic" in w for w in p["repro"]["warnings"])


def test_building_a_candidate_for_a_non_nominable_run_raises(store):
    with pytest.raises(ValueError, match="not nominable"):
        build_recipe_candidate(store, "run-002", config=SchedulerConfig())


def test_the_handler_writes_the_candidate_and_a_nominated_note(store):
    make_nominate_handler(config_loader=lambda s: MOCK_OK, repo_commit="0385004")(store, _act())
    cand = store.read_product(recipe_candidate_file("run-002"))
    assert cand.status == "pending_review"
    note = store.read_product(nomination_file("run-002"))
    assert (note.id, note.type, note.status) == ("nomination-run-002", "nomination_note", "executed")
    assert note.payload["result"] == "nominated" and note.payload["recipe_candidate_id"] == "recipe-run-002"
    assert note.payload["gain"] == pytest.approx(0.05) and note.payload["baseline_run_id"] == "run-001"
    assert note.links == ["run-002", "recipe-run-002"]


def test_the_handler_writes_a_skipped_note_and_no_candidate(store):
    make_nominate_handler()(store, _act())  # 默认配置:mock 证据不可接受
    note = store.read_product(nomination_file("run-002"))
    assert note.payload["result"] == "skipped" and "mock" in note.payload["reason"]
    assert note.payload["recipe_candidate_id"] is None and note.links == ["run-002"]
    assert not store.exists(recipe_candidate_file("run-002"))


@pytest.mark.parametrize("status", ["pending_review", "approved"])
def test_a_not_yet_terminal_candidate_writes_no_note_and_is_nominated_later(store, status):
    """候选还没到 effective:不写标记(标记是幂等键),等它晋级后照常提名。"""
    write_round(store, "run-003", {"tom": 0.6}, baseline_ref="zengen-v2", candidate_status=status)
    handler = make_nominate_handler(config_loader=lambda s: MOCK_OK)
    v = nomination_check(store, "run-003", config=MOCK_OK)
    assert not v.nominate and v.not_yet
    n = store._git("rev-list", "--count", "HEAD")
    handler(store, _act("run-003"))
    assert store._git("rev-list", "--count", "HEAD") == n
    assert not store.exists(nomination_file("run-003"))
    from ts_fixtures import write_candidate

    write_candidate(store, "run-003", "effective")
    handler(store, _act("run-003"))
    assert store.read_product(nomination_file("run-003")).payload["result"] == "nominated"


def test_a_missing_report_writes_no_note_until_the_report_arrives(store):
    from ts_fixtures import write_candidate, write_report

    write_directive(store, "run-003")
    write_candidate(store, "run-003")
    handler = make_nominate_handler(config_loader=lambda s: MOCK_OK)
    handler(store, _act("run-003"))
    assert not store.exists(nomination_file("run-003"))
    write_report(store, "run-003", {"tom": 0.6}, baseline_ref="zengen-v2")
    handler(store, _act("run-003"))
    assert store.read_product(nomination_file("run-003")).payload["result"] == "nominated"


def test_a_rejected_candidate_is_a_permanent_skip(store):
    write_round(store, "run-003", {"tom": 0.6}, baseline_ref="zengen-v2", candidate_status="rejected")
    v = nomination_check(store, "run-003", config=MOCK_OK)
    assert not v.nominate and not v.not_yet
    make_nominate_handler(config_loader=lambda s: MOCK_OK)(store, _act("run-003"))
    assert store.read_product(nomination_file("run-003")).payload["result"] == "skipped"


def test_the_handler_argument_overrides_the_config(store):
    make_nominate_handler(accept_mock_evidence=True)(store, _act())
    assert store.read_product(nomination_file("run-002")).payload["result"] == "nominated"


def test_the_note_is_the_idempotency_key(store):
    handler = make_nominate_handler(config_loader=lambda s: MOCK_OK)
    handler(store, _act())
    n = store._git("rev-list", "--count", "HEAD")
    handler(store, _act())
    assert store._git("rev-list", "--count", "HEAD") == n


@pytest.mark.parametrize("status", ["pending_review", "approved", "rejected", "effective"])
def test_an_existing_candidate_is_never_rewritten(store, status):
    """崩在"候选已写、标记未写"之后,或人已经审核过:只补标记,候选原样保留。"""
    make_nominate_handler(config_loader=lambda s: MOCK_OK)(store, _act())
    cand = store.read_product(recipe_candidate_file("run-002"))
    cand.status = status
    cand.payload["review_note"] = "looked at it"
    store.write_product(recipe_candidate_file("run-002"), cand, "human: review")
    import os

    os.remove(store.repo_root / nomination_file("run-002"))
    before = store.read_product(recipe_candidate_file("run-002")).to_dict()
    make_nominate_handler(config_loader=lambda s: MOCK_OK)(store, _act())
    assert store.read_product(recipe_candidate_file("run-002")).to_dict() == before
    note = store.read_product(nomination_file("run-002"))
    assert note.payload["result"] == "nominated" and f"({status})" in note.payload["reason"]


def test_nomination_check_and_build_only_read(store):
    head = store._git("rev-parse", "HEAD")
    nomination_check(store, "run-002", config=MOCK_OK)
    build_recipe_candidate(store, "run-002", config=MOCK_OK)
    assert store._git("rev-parse", "HEAD") == head


def test_build_does_not_alias_the_directive(store):
    c = build_recipe_candidate(store, "run-002", config=MOCK_OK)
    c.payload["training_config"]["sft_steps"] = 99
    again = build_recipe_candidate(store, "run-002", config=MOCK_OK)
    assert again.payload["training_config"]["sft_steps"] == 2
    assert copy.deepcopy(again.payload) == again.payload
```

- [ ] **Step 3: 运行,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_ts_nominate.py`
Expected: 收集阶段报错 `ModuleNotFoundError: No module named 'trainscheduler.nominate'`,`1 error`。

- [ ] **Step 4: 写实现**

新建 `src/trainscheduler/nominate.py`:

```python
"""配方提名(设计 §6.3、R7、§15):nominate_recipe 动作的 Handler 与它的两个纯读函数。

只对 batch 与 interact 训练 run 提名;agent_rsi 配方 V1 不自动提名(设计 §15)。
"""
from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass

from controlplane.envelope import CURRENT_SCHEMA_VERSION, Envelope, OwnerRef
from controlplane.evidence import diagnostic_grade, evidence_acceptable
from controlplane.gitstore import GitStore
from controlplane.paths import (
    candidate_checkpoint_file,
    dataset_manifest_file,
    nomination_file,
    recipe_candidate_file,
)
from controlplane.reconcile import Action
from mindtrainer.data_spec import manifest_data_run
from trainscheduler.assemble import directive_file
from trainscheduler.config import SchedulerConfig, load_scheduler_config
from trainscheduler.recipes_port import (
    compute_gain,
    read_report_payload,
    recipe_id_for,
    regression_flags,
    validate_reproducibility,
)

Handler = Callable[[GitStore, Action], None]
ConfigLoader = Callable[[GitStore], SchedulerConfig]

NOW = "2026-10-09T00:00:00Z"
NOMINATED = "nominated"
SKIPPED = "skipped"
NOMINABLE_TYPES = ("batch", "interact")
EVAL_ANCHOR_KEYS = (
    "taxonomy_version", "item_pool_version", "benchmark_version",
    "judge_prompt_version", "scoring_config_version",
)


@dataclass(frozen=True)
class NominationVerdict:
    nominate: bool
    reason: str
    gain: float | None
    baseline_run_id: str | None
    # True:前置条件还没到(候选未到终态、缺指令或缺报告),以后会自己变好。处理器不写标记,直接返回。
    not_yet: bool = False


def _skip(reason: str, gain: float | None = None, baseline: str | None = None) -> NominationVerdict:
    return NominationVerdict(False, reason, gain, baseline)


def _not_yet(reason: str) -> NominationVerdict:
    return NominationVerdict(False, reason, None, None, not_yet=True)


def _training_mode(directive: Envelope) -> str | None:
    cfg = directive.payload.get("training_config")
    mode = cfg.get("mode") if isinstance(cfg, dict) else None
    return mode if isinstance(mode, str) else None


def nomination_check(
    store: GitStore, run_id: str, *, config: SchedulerConfig, accept_mock_evidence: bool | None = None
) -> NominationVerdict:
    """只读。accept_mock_evidence 为 None 时取 config.accept_mock_evidence。"""
    accept_mock = config.accept_mock_evidence if accept_mock_evidence is None else accept_mock_evidence
    cpath = candidate_checkpoint_file(run_id)
    if not store.exists(cpath):
        return _not_yet(f"{run_id} has no candidate checkpoint")
    status = store.read_product(cpath).status
    if status == "rejected":
        return _skip(f"candidate of {run_id} is rejected, only effective candidates are nominated")
    if status != "effective":
        return _not_yet(f"candidate of {run_id} is {status}, only effective candidates are nominated")
    dpath = directive_file(run_id)
    if not store.exists(dpath):
        return _not_yet(f"{run_id} has no directive")
    directive = store.read_product(dpath)
    mode = _training_mode(directive)
    if mode not in NOMINABLE_TYPES:
        return _skip(f"directive {run_id} is not a batch or interact training directive (mode {mode!r})")
    dimension = directive.payload.get("target_dimension")
    if not isinstance(dimension, str) or not dimension:
        return _skip(f"directive {run_id} has no target_dimension")
    report = read_report_payload(store, run_id)
    if report is None:
        return _not_yet(f"{run_id} has no diagnostic report")
    grade = diagnostic_grade(report)
    if not evidence_acceptable(grade, accept_mock):
        return _skip(f"diagnostic evidence for {run_id} is {grade}, which is not acceptable")
    gain = compute_gain(store, run_id, dimension)
    if gain.gain is None:
        return _skip(f"no comparable gain: {gain.reason}", None, gain.baseline_run_id)
    if gain.gain < config.min_gain:
        return _skip(
            f"gain {gain.gain:.4f} on {dimension} is below min_gain {config.min_gain}",
            gain.gain, gain.baseline_run_id,
        )
    flags = regression_flags(report)
    if flags:
        kinds = sorted({str(f.get("kind")) for f in flags})
        return _skip(f"diagnostic report carries regression flags {kinds}", gain.gain, gain.baseline_run_id)
    return NominationVerdict(True, f"gain {gain.gain:.4f} on {dimension} >= min_gain {config.min_gain}",
                             gain.gain, gain.baseline_run_id)


def _stage_manifests(store: GitStore, training_config: dict) -> dict:
    out: dict = {}
    refs = training_config.get("stage_manifest_refs") or {}
    for stage in sorted(refs):
        ref = refs[stage]
        data_run = manifest_data_run(ref, stage) if isinstance(ref, str) else None
        manifest_hash = None
        if data_run is not None and store.exists(dataset_manifest_file(data_run, stage)):
            manifest_hash = store.read_product(dataset_manifest_file(data_run, stage)).payload.get("manifest_hash")
        out[stage] = {"id": ref, "manifest_hash": manifest_hash}
    return out


def build_recipe_candidate(
    store: GitStore,
    run_id: str,
    *,
    config: SchedulerConfig,
    repo_commit: str | None = None,
    accept_mock_evidence: bool | None = None,
) -> Envelope:
    """按设计 §4.2 的配方载荷拼出候选信封(pending_review),不写树。run 不可提名时抛 ValueError。
    repo_commit 是产出这一轮的代码版本;不知道就是 None,可复现性校验会把它记成错误。"""
    verdict = nomination_check(store, run_id, config=config, accept_mock_evidence=accept_mock_evidence)
    if not verdict.nominate:
        raise ValueError(f"{run_id} is not nominable: {verdict.reason}")
    directive = store.read_product(directive_file(run_id))
    candidate = store.read_product(candidate_checkpoint_file(run_id))
    report = read_report_payload(store, run_id)
    training_config = copy.deepcopy(directive.payload["training_config"])
    recipe_type = training_config["mode"]
    anchor = {key: None for key in EVAL_ANCHOR_KEYS}
    anchor["benchmark_version"] = report.get("benchmark_version")
    payload: dict = {
        "recipe_type": recipe_type,
        "dimension": directive.payload["target_dimension"],
        "source_run_id": run_id,
        "eval_anchor": anchor,
        "result": {
            "gain": verdict.gain,
            "baseline_ref": report.get("baseline_ref"),
            "candidate_ref": candidate.id,
            "promoted": True,
        },
        "nondeterministic": recipe_type == "interact",
        "training_config": training_config,
        "code_version": {"repo_commit": repo_commit},
    }
    if recipe_type == "batch":
        payload["stage_manifests"] = _stage_manifests(store, training_config)
    else:
        payload["scenario_ids"] = list(training_config.get("scenario_ids", []))
        payload["bindings"] = copy.deepcopy(training_config.get("bindings", []))
    payload["repro"] = validate_reproducibility(payload).to_dict()
    return Envelope(
        id=recipe_id_for(run_id), type="recipe_candidate", version=1, created_at=NOW,
        created_by=OwnerRef(), status="pending_review", schema_version=CURRENT_SCHEMA_VERSION,
        links=[run_id, candidate.id], payload=payload,
    )


def _note(store: GitStore, run_id: str, verdict: NominationVerdict, result: str) -> None:
    env = Envelope(
        id=f"nomination-{run_id}", type="nomination_note", version=1, created_at=NOW,
        created_by=OwnerRef(), status="executed", schema_version=CURRENT_SCHEMA_VERSION,
        links=[run_id] + ([recipe_id_for(run_id)] if result == NOMINATED else []),
        payload={
            "result": result,
            "reason": verdict.reason,
            "gain": verdict.gain,
            "baseline_run_id": verdict.baseline_run_id,
            "recipe_candidate_id": recipe_id_for(run_id) if result == NOMINATED else None,
        },
    )
    store.write_product(nomination_file(run_id), env, f"trainscheduler: nomination {run_id} ({result})")


def make_nominate_handler(
    *,
    config_loader: ConfigLoader = load_scheduler_config,
    accept_mock_evidence: bool | None = None,
    repo_commit: str | None = None,
) -> Handler:
    """accept_mock_evidence 不为 None 时覆盖 scheduler.yaml 里的同名键(组合根的 --allow-mock-evidence)。"""

    def handle(store: GitStore, action: Action) -> None:
        run_id = action.target_id
        if store.exists(nomination_file(run_id)):
            return  # 标记就是幂等键
        config = config_loader(store)
        cpath = recipe_candidate_file(run_id)
        if store.exists(cpath):
            # 崩在"候选已写、标记未写"之间,或候选已被人审核过:不重写候选,只补标记
            verdict = nomination_check(store, run_id, config=config, accept_mock_evidence=accept_mock_evidence)
            existing = store.read_product(cpath)
            _note(store, run_id, NominationVerdict(
                True, f"recipe candidate {existing.id} already exists ({existing.status}); not rewritten",
                verdict.gain, verdict.baseline_run_id,
            ), NOMINATED)
            return
        verdict = nomination_check(store, run_id, config=config, accept_mock_evidence=accept_mock_evidence)
        if verdict.not_yet:
            return  # 过早的动作:不写标记,留给以后(标记是幂等键,写了就永不再提名)
        if not verdict.nominate:
            _note(store, run_id, verdict, SKIPPED)
            return
        env = build_recipe_candidate(
            store, run_id, config=config, repo_commit=repo_commit, accept_mock_evidence=accept_mock_evidence
        )
        store.write_product(cpath, env, f"trainscheduler: nominate {env.id}")
        _note(store, run_id, verdict, NOMINATED)

    return handle
```

- [ ] **Step 5: 运行,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_ts_nominate.py`
Expected: `34 passed`,无警告。

- [ ] **Step 6: 全量回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: **B+227(1770)** passed,无警告(撰写时实测数见括号)。再跑 `git status --short`:除原本就有的 `cli/package*.json` 外,只应出现本任务 Files 里的文件。

- [ ] **Step 7: 提交**

```bash
git add src/trainscheduler/nominate.py
git add tests/test_ts_nominate.py
git commit -m "feat: trainscheduler recipe nomination (evidence, gain, min_gain and regression checks; candidates carry repro; reviewed candidates are never rewritten)"
```

---
### Task 67: 调度器一侧端到端与边界测试

**Files:**
- Test: `tests/test_ts_e2e.py`(2 个用例)
- Test: `tests/test_ts_boundaries.py`(6 个用例)

**Interfaces:**
- Consumes: 任务 60–66 的全部产出;计划 2 的 `make_mindtrainer_dispatcher(..., data_producer=...)`、`TemplateDataProducer`;`mindtrainer.executor.MockExecutor`;`controlplane.daemon.reconcile_once`(只用派发缝);计划 3 的 `handle_admit_recipe`;计划 4 的 `install_default_template`。
- Produces:无新名字。

**设计要点:**
- **证明默认配置真能跑通一轮 batch**:装好两份默认配置与默认模板 → 罐头后端回答 `new batch` → 处理器写出 `awaiting_confirmation` 的 `run-002` → `reconcile_once(store)`(不注入处理器)对它不产生任何动作 → 用写状态模拟命令行 `confirm` → `reconcile_once` 带计划 2 的派发器与 `TemplateDataProducer` → 指令 `executed`,两个阶段的清单在 `run-002` 下合成出来,候选 `pending_review`,`train_curve_summary.degenerate_groups_total == 0`,候选的 `base_checkpoint_ref` 是 `run-001` 晋级模型的 checkpoint。
- **证明 clone 复用清单**:接着用写状态模拟审核门①与晋级、写一份 `mock` 诊断(tom 从 0.40 涨到 0.48)→ 提名处理器(`accept_mock_evidence=True`、`repo_commit="0385004"`)写出候选,候选的 `stage_manifests.sft.manifest_hash` 等于树里清单的哈希 → 模拟审核门③ → 计划 3 的 `handle_admit_recipe` 收录 → 第二次规划回答 `clone recipe-run-002` → `run-003` 的指令 `recipe_ref == "recipe-run-002"`、没有 `data_spec`、清单 id 是 `dsm-run-002-sft`、基座是 `run-002` 的 checkpoint → 确认 → 训练出候选,且 `run-003` 下**没有**合成任何清单。
- 人工步骤一律用写状态模拟,与既有 `tests/test_e2e_loop.py` 同一手法;规划动作直接调用处理器,不经计划 8 的 `reconcile`(那是计划 9 端到端的事)。
- **边界测试**用 `ast` 扫 `src/trainscheduler/*.py` 的全部 `import` / `from ... import` 语句,钉住 Global Constraints"依赖方向"一条的每一项:顶层包只能是允许的项目内包与六个标准库模块;`mindtrainer` 下只能是 `training_config` 与 `data_spec` 两个模块,且两个都真的在用(声明与实际一致);`diagnostic`、`simulator`、`actio`、`mindagent`、`factory` 一个都不能出现;上游五个包不反向导入;`recipestore` 只经 `recipes_port`;每个模块的 `__future__` 头。它们写完就是通过的;第 4 步做六次变异检查,证明测试真能抓住违规。

- [ ] **Step 1: 前置检查**

运行上文"前置检查命令"。Expected: `preflight ok`。

- [ ] **Step 2: 写测试**

新建 `tests/test_ts_e2e.py`:

```python
"""调度器一侧端到端:装配上下文 → 脚本化规划 → 指令 → 人确认 → MindTrainer 派发器(带默认数据生产器)
训练出候选。clone 的下一轮复用前一轮的清单,不再合成。只用 MockExecutor,不接外部服务。"""
import pytest

from controlplane.daemon import reconcile_once
from controlplane.paths import (
    candidate_checkpoint_file,
    dataset_manifest_file,
    nomination_file,
    planner_decision_file,
    recipe_candidate_file,
    run_dir,
)
from controlplane.reconcile import Action
from controlplane.states import assert_transition
from mindtrainer.data_producer import TemplateDataProducer
from mindtrainer.dispatch import make_mindtrainer_dispatcher
from mindtrainer.executor import MockExecutor
from recipestore.admit import handle_admit_recipe
from trainplanner.backends import ScriptedDecisionBackend
from trainplanner.templates import install_default_template
from trainscheduler.config import SchedulerConfig, install_default_configs
from trainscheduler.nominate import make_nominate_handler
from trainscheduler.plan_handler import make_plan_handler
from ts_fixtures import decision_json, make_store, write_model, write_report, write_round


def _confirm(store, run_id):
    """命令行 confirm 的等价操作(计划 8):awaiting_confirmation -> pending。"""
    path = f"{run_dir(run_id)}/directive.yaml"
    d = store.read_product(path)
    assert_transition("nonreview", d.status, "pending")
    d.status = "pending"
    store.write_product(path, d, f"human: confirm {run_id}")


def _train(store, run_id):
    reconcile_once(store, dispatcher=make_mindtrainer_dispatcher(
        executor_factory=MockExecutor, data_producer=TemplateDataProducer()))
    assert store.read_product(f"{run_dir(run_id)}/directive.yaml").status == "executed"
    return store.read_product(candidate_checkpoint_file(run_id))


@pytest.fixture
def store(tmp_path):
    s = make_store(tmp_path)
    install_default_configs(s)
    install_default_template(s)
    write_round(s, "run-001", {"tom": 0.40, "empathy": 0.70}, model_n=1)
    return s


def test_a_new_batch_round_is_planned_confirmed_synthesized_and_trained(store):
    make_plan_handler(backend=ScriptedDecisionBackend([decision_json()]))(
        store, Action("plan_next_round", "run-001", {}))
    assert store.read_product(planner_decision_file("run-001")).status == "executed"
    assert reconcile_once(store) == []  # awaiting_confirmation:调和循环不碰它
    _confirm(store, "run-002")
    cand = _train(store, "run-002")
    assert cand.status == "pending_review"
    for stage in ("sft", "grpo_opd"):
        assert store.exists(dataset_manifest_file("run-002", stage))
    curve = cand.payload["train_curve_summary"]
    assert curve["degenerate_groups_total"] == 0
    assert cand.payload["base_checkpoint_ref"]["uri"].endswith("cand-run-001.safetensors")


def test_promote_nominate_admit_then_clone_reuses_the_manifests(store):
    plan = make_plan_handler(backend=ScriptedDecisionBackend([
        decision_json(),
        decision_json(action="clone", source_recipe_id="recipe-run-002", perturbation={"opd_coefficient": 0.2}),
    ]))
    plan(store, Action("plan_next_round", "run-001", {}))
    _confirm(store, "run-002")
    _train(store, "run-002")

    # 审核门①与晋级、诊断用写状态模拟(与既有端到端测试同一手法)
    cand = store.read_product(candidate_checkpoint_file("run-002"))
    cand.status = "effective"
    store.write_product(candidate_checkpoint_file("run-002"), cand, "human+reconcile: promote")
    write_model(store, 2, "run-002")
    write_report(store, "run-002", {"tom": 0.48, "empathy": 0.70}, baseline_ref="zengen-v1")

    make_nominate_handler(config_loader=lambda s: SchedulerConfig(accept_mock_evidence=True),
                          repo_commit="0385004")(store, Action("nominate_recipe", "run-002", {}))
    rc = store.read_product(recipe_candidate_file("run-002"))
    assert rc.status == "pending_review"
    assert rc.payload["stage_manifests"]["sft"]["manifest_hash"] == store.read_product(
        dataset_manifest_file("run-002", "sft")).payload["manifest_hash"]
    assert store.read_product(nomination_file("run-002")).payload["result"] == "nominated"

    rc.status = "approved"  # 审核门③
    store.write_product(recipe_candidate_file("run-002"), rc, "human: approve recipe")
    handle_admit_recipe(store, Action("admit_recipe", "recipe-run-002", {}))

    plan(store, Action("plan_next_round", "run-002", {}))
    directive = store.read_product(f"{run_dir('run-003')}/directive.yaml")
    assert directive.payload["recipe_ref"] == "recipe-run-002"
    assert "data_spec" not in directive.payload
    assert directive.payload["training_config"]["stage_manifest_refs"]["sft"] == "dsm-run-002-sft"
    assert directive.payload["training_config"]["base_checkpoint_ref"]["uri"].endswith("cand-run-002.safetensors")
    _confirm(store, "run-003")
    cand3 = _train(store, "run-003")
    assert cand3.status == "pending_review"
    assert not store.exists(dataset_manifest_file("run-003", "sft"))  # 没有重新合成
```

新建 `tests/test_ts_boundaries.py`:

```python
"""依赖方向与 __future__ 头。

trainscheduler 允许导入的项目内模块(设计 §11):controlplane、recipestore(只经 recipes_port)、trainplanner、
inference_bridge(只取 BridgeError)、mindtrainer.training_config 与 mindtrainer.data_spec。
不导入 mindtrainer 的其他模块,不导入 diagnostic、simulator、actio、mindagent、factory。
反方向:controlplane、recipestore、trainplanner、mindtrainer、inference_bridge 都不导入 trainscheduler。
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src"
PROJECT_ALLOWED = {"trainscheduler", "controlplane", "recipestore", "trainplanner", "inference_bridge", "mindtrainer"}
STDLIB_ALLOWED = {"__future__", "collections", "copy", "dataclasses", "math", "re"}
# mindtrainer 只允许这两个模块:不能把编排器、执行器、派发器拉进调度器
ALLOWED_MINDTRAINER = {"mindtrainer.training_config", "mindtrainer.data_spec"}
FORBIDDEN = {"diagnostic", "simulator", "actio", "mindagent", "factory"}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


def _modules(pkg: str) -> list[Path]:
    return sorted((ROOT / pkg).glob("*.py"))


def test_trainscheduler_imports_only_the_allowed_packages():
    for path in _modules("trainscheduler"):
        for name in _imports(path):
            top = name.split(".")[0]
            assert top in PROJECT_ALLOWED | STDLIB_ALLOWED, f"{path.name} imports {name}"


def test_trainscheduler_imports_only_two_mindtrainer_modules():
    seen = set()
    for path in _modules("trainscheduler"):
        for name in _imports(path):
            if name.split(".")[0] == "mindtrainer":
                assert name in ALLOWED_MINDTRAINER, f"{path.name} imports {name}"
                seen.add(name)
    assert seen == ALLOWED_MINDTRAINER  # 声明与实际一致:两条都真的在用


def test_trainscheduler_never_imports_the_forbidden_packages():
    for path in _modules("trainscheduler"):
        hit = {n for n in _imports(path) if n.split(".")[0] in FORBIDDEN}
        assert not hit, f"{path.name} imports {sorted(hit)}"


def test_nobody_upstream_imports_trainscheduler():
    for pkg in ("controlplane", "recipestore", "trainplanner", "mindtrainer", "inference_bridge"):
        for path in _modules(pkg):
            assert not any(n.split(".")[0] == "trainscheduler" for n in _imports(path)), path


def test_every_module_starts_with_future_annotations():
    for path in _modules("trainscheduler"):
        body = ast.parse(path.read_text(encoding="utf-8")).body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]
        first = body[0]
        assert isinstance(first, ast.ImportFrom) and first.module == "__future__", path.name


def test_recipestore_is_only_imported_through_the_port():
    for path in _modules("trainscheduler"):
        if path.name == "recipes_port.py":
            continue
        assert not any(n.startswith("recipestore") for n in _imports(path)), path.name
```

- [ ] **Step 3: 运行,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_ts_e2e.py tests/test_ts_boundaries.py`
Expected: `8 passed`,无警告。

- [ ] **Step 4: 变异检查(改完立刻还原)**

1. 在 `src/trainscheduler/errors.py` 末尾临时追加一行 `import simulator  # mutation check`,跑 `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_ts_boundaries.py`。Expected: `2 failed, 4 passed`,失败信息分别是 `AssertionError: errors.py imports simulator` 与 `AssertionError: errors.py imports ['simulator']`。删掉这一行。
2. 同一位置改追加 `import mindtrainer.orchestrator  # mutation check`,再跑同一命令。Expected: `1 failed, 5 passed`,失败信息是 `AssertionError: errors.py imports mindtrainer.orchestrator`。删掉这一行。(追加 `from mindtrainer import dispatch` 同样被抓,信息是 `errors.py imports mindtrainer`。)
3. 在 `src/trainscheduler/plan_handler.py` 的 `except BridgeError as exc:` 下一行临时插入两行 `if exc.category == "T1":` / `    raise`(即回到 R18 之前的写法),跑 `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_ts_plan_handler.py`。Expected: `3 failed, 35 passed`,失败的是 `test_any_bridge_error_fails_the_decision_as_t3_and_does_not_propagate[T1]`、`test_a_bridge_error_on_a_retry_counts_the_planner_attempts`、`test_a_failed_decision_is_not_replanned_after_a_bridge_error`。删掉这两行。
4. 在 `src/trainscheduler/directive.py` 的 `directive_payload` 里临时删掉 `payload["data_spec"] = resolved.data_spec.to_dict()` 这一行,跑 `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_ts_e2e.py`。Expected: `2 failed`,两条都是 `AssertionError: assert 'failed' == 'executed'`(派发器缺清单、指令不带 `data_spec`,判 `failed`)。还原这一行。
5. 在 `src/trainscheduler/run_ids.py` 的 `blocking_run_dirs` 里临时把最后一行换成 `return sorted(name for name in existing if name >= new_id)`(即退回字符串序),跑 `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_ts_run_ids.py tests/test_ts_plan_handler.py`。Expected: `6 failed, 62 passed`,失败的是 `test_blocking_run_dirs_uses_numbers_and_ignores_other_names`、`test_allocate_succeeds_on_trees_that_used_to_look_out_of_order` 的四个参数、`test_hand_named_and_unpadded_run_dirs_do_not_block_planning`。还原这一行。
6. 在 `src/trainscheduler/assemble.py` 的 `drop_no_error_label` 里临时把最后一行换成 `return dict(distribution)`(即不丢空标签,设计 R21 之前的写法),跑 `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_ts_assemble.py tests/test_ts_plan_handler.py`。Expected: `4 failed, 68 passed`,失败的是 `test_the_no_error_label_is_dropped_without_renormalizing` 的前两个参数、`test_the_real_classifier_output_for_an_all_correct_family_is_accepted`、`test_a_candidate_that_got_every_variant_family_right_is_still_planned`(最后一条的失败是 `assert 'failed' == 'executed'`,即 R21 所说的缺陷)。还原这一行。

还原后 `git diff --stat src/` 应无输出,再跑一次第 3 步的命令,Expected: `8 passed`。

- [ ] **Step 5: 全量回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: **B+235**(撰写时 1778)passed,无警告。

- [ ] **Step 6: 提交**

```bash
git add tests/test_ts_e2e.py
git add tests/test_ts_boundaries.py
git commit -m "test: trainscheduler end to end (default config trains a synthesized batch round, clone reuses the recipe manifests) and import boundaries"
```

---

### Task 68: 全量回归与改动范围检查

**Files:** 无改动。

**Interfaces:** 无。

- [ ] **Step 1: 全量回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: **B+235**(撰写时 1778)passed,无警告。

- [ ] **Step 2: 改动范围**

`BASE` 是任务 60 第 2 步记下的起点提交。如果当前 shell 里已经没有这个变量,从执行记录里取回来(`BASE=<记下的提交号>`);执行记录也丢了,才退而用 `BASE=HEAD~8`(本计划恰好 8 个提交,任务 60–67 各一个;前提是这期间没有别的提交)。

Run: `git diff --stat "$BASE"..HEAD -- . ':(exclude)docs/superpowers'`
Expected: 恰好下列 21 个文件,别的一个都没有(撰写时在临时拷贝里的实际输出):

```
 pyproject.toml                     |   2 +-
 src/trainscheduler/__init__.py     |   6 +
 src/trainscheduler/assemble.py     | 236 ++++++++++++++++++++++
 src/trainscheduler/config.py       | 304 +++++++++++++++++++++++++++++
 src/trainscheduler/directive.py    |  99 ++++++++++
 src/trainscheduler/errors.py       |  36 ++++
 src/trainscheduler/nominate.py     | 226 +++++++++++++++++++++
 src/trainscheduler/plan_handler.py | 217 +++++++++++++++++++++
 src/trainscheduler/recipes_port.py |  22 +++
 src/trainscheduler/resolve.py      | 155 +++++++++++++++
 src/trainscheduler/run_ids.py      |  73 +++++++
 tests/test_ts_assemble.py          | 285 +++++++++++++++++++++++++++
 tests/test_ts_boundaries.py        |  76 ++++++++
 tests/test_ts_config.py            | 206 ++++++++++++++++++++
 tests/test_ts_directive.py         | 139 +++++++++++++
 tests/test_ts_e2e.py               | 105 ++++++++++
 tests/test_ts_nominate.py          | 295 ++++++++++++++++++++++++++++
 tests/test_ts_plan_handler.py      | 389 +++++++++++++++++++++++++++++++++++++
 tests/test_ts_resolve.py           | 193 ++++++++++++++++++
 tests/test_ts_run_ids.py           | 115 +++++++++++
 tests/ts_fixtures.py               | 183 +++++++++++++++++
 21 files changed, 3361 insertions(+), 1 deletion(-)
```

(`+` 图形的长度随终端宽度变化,以文件名与行数为准。)

再确认 `cli/package*.json` 没有被提交进来:

Run: `git diff --stat "$BASE"..HEAD -- . ':(exclude)docs/superpowers' | grep -c 'cli/package' || true`
Expected: `0`。

`git status --short` 只应剩原本就有的 `cli/package.json`、`cli/package-lock.json`(不要碰)。`git diff "$BASE"..HEAD -- pyproject.toml` 的差异应只是 `packages` 列表末尾多了 `"src/trainscheduler"`。

- [ ] **Step 3: 依赖方向的最后一眼**

Run: `grep -rn "trainscheduler" src/controlplane src/recipestore src/trainplanner src/mindtrainer src/inference_bridge`
Expected: 无输出(退出码 1)。

本任务不提交。

---

## 完成标准

- 九个任务的全部步骤勾完;全量命令 **B+235** 通过、无警告。绝对通过数依赖执行顺序,本计划只保证增量 +235。
- `git diff --stat "$BASE"..HEAD -- . ':(exclude)docs/superpowers'` 恰好是文件表里的 21 个文件(`BASE` 是任务 60 第 2 步记下的起点提交),其中 `cli/package` 出现 0 次;`controlplane`、`recipestore`、`trainplanner`、`mindtrainer`、`inference_bridge`、`schemas/`、`cli/` 一行未改。
- 默认配置装好后,一条罐头 `new batch` 决策能一路走到 MindTrainer 训练出候选(任务 67);`clone` 复用配方清单、不再合成。
- 规划处理器的三条重入规则与各类失败各有测试,`BridgeError` 的 T1 与 T2 都升级为 T3(设计 R18);提名处理器不改已审核的候选,有四种状态的测试。
- `assemble_context` 对未记录的成本产出 `None`,输出能被计划 4 的 `PlanningContext.from_dict` 读回。

## 已知限制

1. **`reconcile` 不在本计划里。** 两个处理器要等计划 8 的 `reconcile_once(..., handlers=...)` 注入后才会被调和循环调用;计划 9 的组合根负责组装(`make_plan_handler(backend=...)`、`make_nominate_handler(accept_mock_evidence=..., repo_commit=...)`)。
2. **`new` 只构造 batch**(Ruling 3)。规划器开不了新的 interact 训练线;interact 只能 clone / replay 人工收录的配方,且要先打开 `allow_interact`。
3. **默认基座与教师是占位引用**(Ruling 9),哈希为 64 个 0。真实部署前必须改 `training_defaults.yaml`;`MockExecutor` 不读文件,测试发现不了。
4. **V1 的配方候选必然带 `repro` 错误**:诊断报告没有分类表与题库版本,`eval_anchor` 里这两项是 `None`;组合根不传 `repo_commit` 时代码版本也缺(Ruling 16)。审核门③的人要据此判断。
5. **规划是同步的**,外部推理期间调和循环被阻塞(设计 §7.2、§14 第 6 条)。本计划的测试只用罐头与启发式后端,不涉及耗时。
6. **配置写错与非桥接的瞬态异常会让守护循环退出**(Ruling 5、6):`SchedulerConfigError`、`OSError` 等原样上抛,与派发器的瞬态异常同一已知限制,需要外部监督。
7. **外部推理短暂不可用也会让规划停在 `failed`**(Ruling 6,设计 R18):`BridgeError` 的 T1 只说明桥接自己的退避重试已用尽,处理器不区分"网络抖动"与"工具整体不可用",一律判 T3。闭环因此停住,要人写下一条指令,或删掉 `planner_decision.yaml` 与对应失败记录后让调和循环重新触发。换来的是守护循环重启后不会对同一来源 run 无限空转外部推理。
8. **`clone` 的战绩混合了配方与基座两种贡献**(Ruling 2)。
9. **分配过但没用上的 run id 会被复用**(Ruling 14):决策写下后解析失败的,`next_run_id` 留在 `failed` 的决策里,下一次规划可能分配同一个 id。
10. **run 编号按数值比较**(Ruling 22):分配器按数值分配,计划 8 按数值判最大 run(设计 §3.2),两者一致。`run-9`、`test-x`、`zz` 这类名字排在最后、不参与比较,既不挡路也不造成闭环静默停住;超过 999 轮按数值继续。唯一的失败点是人手建了编号不小于新编号的 `run-<数字>` 目录,正常路径走不到。
11. **数据与目标维度无关**(R14):`new` 指令的 `data_spec` 每轮都一样(种子 0),合成出的数据只是让流程跑通。
12. **`HeuristicBackend` 只 clone batch 配方**(计划 4 的 Ruling),开了 `allow_interact` 也不会选 interact 配方。
13. **二级维度的"答对比例"不进规划上下文**(设计 R21,Ruling 23):空标签("没有错误")在装配时被丢掉、不重新归一化,`error_type_distribution` 的和可以小于 1,全答对时是空映射;规划器只能从 `l1_scores` 看到一级维度的总体表现。
14. **`run-007` 与 `run-7` 编号相同**:两者都按编号 7 参与比较,只有手工命名才会出现;本计划不做专门处理,窗口与分配都把它们当同一编号,先后取决于名字排序。
15. **重入时外部推理成本可能少记**:规划处理器遇到非桥接的瞬态异常(例如 `OSError`)时,决策保持 `pending`,下一个 tick 从头重新规划。重跑的桥接调用沿用同一组 `task_id`(`planner-attempt-N`)与 `cost_type`,计划 1 的账本 `record_cost` 先到者为准,会把重跑的成本静默去重,成本被低估。这是有意的取舍:要修需要给 `task_id` 加重入序号,V1 不做。

## 未验证项

1. **基线 B 与绝对通过数。** 撰写时 B = 1543 是在临时拷贝里实测的:`main@0385004` 加上从 `docs/superpowers/plans/` 下计划 1(`bridge-and-foundations`)、计划 2(`data-synthesis-wiring`,含它的 `dispatch.py` 差异块,用 `patch -p1` 原样打上)、计划 3(`recipestore`)、计划 4(`trainplanner`)的计划文本原样抽出的全部代码块与测试,没有用任何撰写方的临时拷贝。各计划的执行顺序、实施时实际落地的测试数不同,B 与每个任务后的绝对通过数都会不同;**本计划只保证增量 +235,与 B 无关**。任务 60 第 2 步记下的 `BASE` 与 B 两个值写进执行记录,任务 68 的范围核对用 `BASE`,`HEAD~8` 只是 `BASE` 丢失时的退路。
2. **计划 1–4 实施时的代码与它们的计划文本是否一致。** 前置检查只验证名字可导入、几个关键签名;行为差异(例如 `compute_gain` 的原因措辞)会以本计划测试失败的形式暴露。本计划的断言只依赖计划 3 原因里的固定片段 `no comparable gain:`(本计划自己加的前缀)与 `is stub`(本计划自己的措辞)。
3. **计划 8 的 `reconcile` 与本计划处理器的联动没有跑过。** 本计划按计划 8 文本里的动作形状(`Action(kind, target_id=<run>, detail={"run_id": <run>})`)与重入契约写测试,但两边没有在同一棵树上跑过;计划 9 的端到端负责。
4. **计划 9 的组合根怎么传 `accept_mock_evidence` 与 `repo_commit`** 没有约定,本计划只给出参数(Ruling 4、16)。
5. **真实桥接后端** 没有接过:`BridgeDecisionBackend` 与 `CliBridge` 的组合只在计划 1、4 里各自测过,本计划只用罐头与启发式后端。
6. **`--color=no` 下的 `ImportError` 文本** 在不同 pytest 版本里措辞可能略有不同(撰写时的 pytest 由 `uv.lock` 锁定),"确认失败"一步以收集阶段出错、`1 error` 为准。
