# RecipeStore:可复现性校验、检索、增益、战绩、生命周期与两个处理器 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增顶层包 `recipestore`,实现闭环集成设计 §6 的全部内容与 §11 规定的唯一增益实现 `recipestore.gain.compute_gain`:配方载荷的可复现性校验(纯函数)、按目录扫描的检索与比较、按 `run_id` 幂等的战绩、`effective → deprecated` 的自动生命周期,以及两个可注入调和循环的处理器 `handle_admit_recipe`(收录)与 `handle_record_track`(战绩回写)。两个处理器都可以脱离调和循环直接调用,崩在任何一步之后重跑都能收敛。

**Architecture:** `recipestore` 只依赖 `controlplane`(设计 §11 依赖方向)。读写产物一律走 `GitStore` + `Envelope`。诊断报告不经 `diagnostic` 包反序列化,而是按 `DiagnosticReport.to_dict()` 的键名直接读 payload 字典,由一条测试用 `diagnostic` 自己的写法造报告来钉住字段名。处理器签名是 `Handler = Callable[[GitStore, Action], None]`;本计划不改 `reconcile`、不改守护循环;动作名与注入缝由计划 8 定义,`handlers` 的组装由计划 9 的 factory 组合根做。

**Tech Stack:** Python 3.11+ / uv / pytest;**无新增第三方依赖**。

**Spec:**
- `docs/superpowers/specs/2026-10-09-closed-loop-integration-design.md` §4.1、§4.2(配方与候选载荷、战绩条目)、§6 全部、§11(接口契约)、§12(本计划是第 3 份,任务号 40 起)
- 裁决 R6、R8、R9、R10 已定,本计划照用,不推翻
- 前置:**计划 1**(`2026-10-09-bridge-and-foundations.md`)已合并。本计划的全量数以"计划 1 落地后的全量数"为基线,记作 **B**

**依赖已有产出的名字**

| 来源 | 名字 | 现状 |
|---|---|---|
| `controlplane.envelope` | `Envelope`、`OwnerRef`、`CURRENT_SCHEMA_VERSION` | 已有 |
| `controlplane.gitstore` | `GitStore`(`exists`、`read_product`、`write_product`、`list_products`、`repo_root`、`_git`) | 已有 |
| `controlplane.paths` | `CONTROL_PLANE_ROOT`、`run_dir`、`model_file`、`diagnostic_report_file`、`candidate_checkpoint_file` | 已有 |
| `controlplane.states` | `REVIEW`、`assert_transition`、`InvalidTransitionError`(审核类 `effective → deprecated`、`approved → effective`) | 已有,计划 1 不改 REVIEW 状态机 |
| `controlplane.reconcile` | `Action(kind, target_id, detail)` | 已有 |
| `controlplane.paths` | `recipe_candidate_file`、`recipe_dir`、`recipe_file`、`recipe_lineage_file`、`recipe_track_file`、`recipe_track_note_file`、`planner_decision_file`、`failure_record_file`;`SUBDIRS` 末尾追加 `recipe_candidates`、`skill_library`、`memory_library`、`agent_rounds` | **计划 1 产出** |
| `controlplane.ledger` | `record_failure(store, run_id, *, task_id, category, point, retries, reason, log_ref=None) -> str` | **计划 1 产出** |
| `controlplane.evidence` | `diagnostic_grade(payload) -> str`(`stub`/`mock`/`real`)、`evidence_acceptable(grade, accept_mock) -> bool` | **计划 1 产出** |
| `diagnostic.datamodel` / `diagnostic.report` / `diagnostic.store_adapter` | `DimensionScore`、`RegressionFlags`、`ConsistencySignals`、`build_report`、`write_report` | 已有,**只在测试里**用 |

**已核实的事实(实现者不要再猜)**

- 诊断报告的 payload 就是 `DiagnosticReport.to_dict()`(`diagnostic/store_adapter.py` 的 `write_report`):键为 `id, candidate_ref, baseline_ref, benchmark_version, run_tier, sampling_k, dimension_scores, error_profiles, regression_flags, consistency_signals, links`。`dimension_scores` 每项是 `{dimension, level, score, ci_low, ci_high, sample_size, track_provenance}`,一级维度的 `level` 是 `"l1"`(`diagnostic/aggregator.py` 的 `aggregate_l1`)。`regression_flags` 是 `{"flags": [...]}`,每条标记是字典,`kind` 为 `social_regression`(`diagnostic/regression.py` 的 `detect_regressions`)或 `g_probe_failure`。
- 桩报告(`simulator.stubs.diagnostic_evaluator_stub`)的 payload 只有 `candidate_ref, benchmark_version, dimension_count, stub: True`,没有 `dimension_scores`、`baseline_ref`、`regression_flags`。
- 诊断报告的 `baseline_ref` 是 `DiagnosticConfig.baseline_ref` 原样写入(`diagnostic/orchestrator.py`),形如 `zengen-v3`。
- 晋级(`controlplane.daemon.apply_action` 的 `promote_candidate`)写 `models/zengen-v<N>.yaml`,`links=[候选 id, run id]`,候选 id 恒为 `cand-<run>`(`mindtrainer/checkpoint.py` 与 `simulator/stubs.py` 都这样写)。所以"产出这个模型的 run"= `links` 里满足"`cand-<它>` 也在 `links` 里"的那一项。
- `GitStore.write_product` 对逐字节相同的内容不产生新提交(先探测暂存区)。所以"重跑写同样的东西"不会堆提交,但本计划仍然用 `exists` 判断跳过,让重入的语义不依赖这条实现细节。
- `GitStore.list_products` 不递归;`recipes/recipe-<run>/` 是子目录,检索要自己扫 `recipes/` 下的目录。
- `tests/` 没有 `__init__.py`、没有 `conftest.py`;pytest 默认的 `prepend` 导入模式会把 `tests/` 放进 `sys.path`,所以测试可以 `from rs_fixtures import ...`。`rs_fixtures.py` 不以 `test_` 开头,不会被当成测试收集。

**设计 §6 的各条,本计划怎么处理**

| 条目 | 处理 |
|---|---|
| §6.1 可复现性校验 | **做**(任务 41) |
| §6.2 检索、比较、战绩、verdict、deprecate | **做**(任务 43、44) |
| §6.3 提名判定 | **不做**:归 TrainScheduler(计划 5),它调本计划的 `compute_gain` |
| §6.3 收录 `handle_admit_recipe` | **做**(任务 45) |
| §6.3 战绩回写 `handle_record_track` | **做**(任务 46) |
| §11 `compute_gain` | **做**(任务 42) |
| R8 `superseded` 自动迁移 | 不做 |
| R9 SQLite 索引 | 不做,按目录扫描 |
| `reconcile` 产生 `admit_recipe` / `record_track` 动作 | 不做:计划 8 |
| 命令行审核门③(`approve` 要求 `repro`) | 不做:计划 8 的 TypeScript 命令行。本计划给出同一规则的 Python 版 `has_repro_evidence`,收录处理器用它兜底 |

## Global Constraints

每个任务的要求都隐含包含本节。

- **`src/` 下每个源文件以 `from __future__ import annotations` 开头**(模块 docstring 之后的第一条语句也算)。任务 47 有测试强制检查 `src/recipestore/` 下的全部文件。
- **依赖方向**:`recipestore` 只导入 `controlplane` 与自己;不导入 `diagnostic`、`simulator`、`mindtrainer`、`trainscheduler`。`controlplane` 不导入 `recipestore`(由计划 9 任务 109 的跨计划核对统一检查所有新包)。任务 47 的测试强制检查的是本包往外的导入。测试文件可以导入 `diagnostic`(只为钉住报告字段名)。
- **无新增第三方依赖**;`pyproject.toml` 只改 wheel 包列表这一行。
- **凭证绝不进 git 树**:本计划不读也不写任何凭证;产物 payload 里不出现 `token`、`api_key` 之类的键。
- **读产物走 `GitStore.read_product`,写产物用 `Envelope` + `GitStore.write_product`。** 不直接用文件系统读写产物;目录扫描(`recipes/`、`runs/`)只用来列出 id,读内容仍走 `read_product`。
- **新增顶层包要加进 `pyproject.toml` 的 wheel 包列表**(`tests/test_mt_packaging.py` 强制检查),在任务 40 做。
- **不改计划 1 产出的文件**(`controlplane/paths.py`、`ledger.py`、`evidence.py`、`states.py`、`config.py`),也不改 `reconcile.py`、`daemon.py`、`schemas/`、`cli/`。
- **处理器的失败约定**(设计 §3.3):确定性失败写失败记录或 skip 标记,不抛;瞬态异常(`OSError` 等)原样上抛。处理器自己负责写终态记录。
- **`git add` 逐个文件**;**不要碰** `cli/package.json`、`cli/package-lock.json`。**提交信息不加 `Co-Authored-By` 行**(用户明确要求)。
- **测试命令**一律是 `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`,下文每一步都写全。
- **测试基线**:计划 1 落地后的全量数记作 **B**,无警告。本计划八个提交任务的增量依次为 +9、+62、+30、+20、+24、+23、+26、+3,累计 **B+9 → B+71 → B+101 → B+121 → B+145 → B+168 → B+194 → B+197**。修订时在一份干净拷贝里实测:`0385004` 加上计划 1 任务 19、21、24 的代码块(路径、`ledger`、`evidence`,本计划只用到这三块),那里 B=902,各任务后为 **911 → 973 → 1003 → 1023 → 1047 → 1070 → 1096 → 1099**。只作参考,别的计划先落地时 B 会更大,只核对增量。

## File Structure

| 文件 | 动作 | 任务 | 职责 |
|---|---|---|---|
| `pyproject.toml` | 修改(1 行) | 40 | wheel 包列表加 `src/recipestore` |
| `src/recipestore/__init__.py` | 新建(5 行) | 40 | 包说明与依赖方向 |
| `src/recipestore/ids.py` | 新建(20 行) | 40 | `recipe_id_for`、`run_id_of` |
| `src/recipestore/validate.py` | 新建(237 行) | 41 | `ReproReport`、`validate_reproducibility`、`is_relative_ref` |
| `src/recipestore/gain.py` | 新建(132 行) | 42 | `GainResult`、`compute_gain`,及读报告的小函数 |
| `src/recipestore/library.py` | 新建(154 行) | 43 | `RecipeView`、`find_recipes`、`get_recipe`、`compare`、`read_track_entries`、`track_summary` |
| `src/recipestore/track.py` | 新建(117 行) | 44 | `append_track_entry`、`verdict`、`deprecate`、`deprecated_by` |
| `src/recipestore/admit.py` | 新建(110 行) | 45 | `handle_admit_recipe`、`has_repro_evidence` |
| `src/recipestore/record.py` | 新建(156 行) | 46 | `handle_record_track`、`used_as_for` |
| `tests/rs_fixtures.py` | 新建(216 行) | 41 | 手工构造三类配方载荷、诊断报告、模型、候选、指令、规划决策、配方候选 |
| `tests/rs_recipes.py` | 新建(34 行) | 43 | 直接往 `recipes/` 放配方与战绩 |
| `tests/test_rs_ids.py` | 新建(9 个用例) | 40 | |
| `tests/test_rs_validate.py` | 新建(62 个用例) | 41 | |
| `tests/test_rs_gain.py` | 新建(30 个用例) | 42 | |
| `tests/test_rs_library.py` | 新建(20 个用例) | 43 | |
| `tests/test_rs_track.py` | 新建(24 个用例) | 44 | |
| `tests/test_rs_admit.py` | 新建(23 个用例) | 45 | |
| `tests/test_rs_record.py` | 新建(26 个用例) | 46 | |
| `tests/test_rs_flow.py` | 新建(3 个用例) | 47 | 两个处理器串联,依赖方向与 `__future__` 守卫 |

**打包说明:** `recipestore` 是新增顶层包,任务 40 把它加进 `[tool.hatch.build.targets.wheel].packages`;`tests/test_mt_packaging.py` 会在漏加时失败(任务 40 的步骤里先让它失败一次)。

**本计划的裁决(要改的话改这里,任务按此排)**

每条格式为"Ruling: 裁决 —— 原因 —— 判错的代价"。

1. Ruling: 配方候选与收录后的配方共用 id `recipe-<来源 run>`,`recipestore.ids` 负责互转,形状不对抛 `ValueError` —— 设计 §4.1 的路径 `recipe_candidates/recipe-<run>.yaml` 与 `recipes/recipe-<run>/` 都以 run id 为键,路径函数也只收 run id —— 一个 run 只能产出一条配方;要一 run 多配方得改路径函数与 id 规则。
2. Ruling: 产物类型名照设计 §4.1 表:`recipe_candidate`、`recipe`、`recipe_lineage`、`recipe_track_record`、`recipe_track_note`;战绩记录的载荷是 `{recipe_id, entries: [...]}`,lineage 的载荷是 `{recipe_id, source_run_id, admitted_from, events: [{event, run_id, reason?}]}` —— 设计只给了类型名与条目形状,没给外层载荷 —— 计划 5、8 若要读 lineage 或战绩,须按这两个形状读。
3. Ruling: 相对引用扫描覆盖整个载荷,两处例外:`repro`(本函数自己的输出)与 `training_config.reward.outcome_rules`(整个子树);词表为 `latest/current/current_best/best/newest/head/previous/prev`(不分大小写)以及 `:latest/-latest/@latest/_latest` 结尾 —— 设计只说"形如 latest、current_best 的相对引用算错误",没限定位置;`outcome_rules` 是奖励的结局匹配关键词(`mindtrainer.training_config.RewardConfig.outcome_rules`,拿去匹配对话文本),是自然语言词表而不是引用,用户把 `best`、`current`、`previous` 当关键词是正常用法 —— 任何其他字符串字段恰好取这些值都会报错,即使它不是引用(例如 `grpo_config.scheduler: "best"`);这是错误而非拒绝,由审核人判断。`outcome_rules` 里不论写什么都不报。
4. Ruling: batch 的"`training_config` 完整"只核对 `TrainingConfig` 无默认值的 14 个键在场、两个 `BigRef` 带 64 位十六进制 `sha256`、`mode` 与 `recipe_type` 一致,以及 `stage_manifest_refs` 点名的每个阶段都在 `stage_manifests` 里锚定且 id 相同;不做类型校验 —— `recipestore` 不得导入 `mindtrainer`(设计 §11 依赖方向),类型与取值由派发器在派发时校验;interact 的 `training_config` 完整性检查与此相同(同一套 14 个键与 `mode` 一致检查) —— 类型错的配置能通过本校验,到复用时才在派发器里被判 `failed`。
5. Ruling: batch 的 `eval_anchor` 只强制 `taxonomy_version` 与 `item_pool_version`,其余三项允许 `null` —— 设计 §6.1 表只列这两项,§4.2 注明"暂无的填 null" —— 没有 `benchmark_version` 的配方也能无错误通过。
6. Ruling: `code_version.repo_commit` 必须是 7 到 40 位小写十六进制;`HEAD`、`main` 之类算错误 —— 分支名是会漂移的软引用 —— 用大写十六进制写提交号会被误报;使用 SHA-256 对象格式(`git init --object-format=sha256`)的 git 仓库提交号是 64 位,也会被误报,要支持时把上限放宽到 64。
7. Ruling: interact 配方必须声明 `nondeterministic: true`,否则是错误;同时恒给一条警告 —— 设计 §4.2 写"interact 恒为 true",§6.1 写"`nondeterministic` 为真"是警告 —— 手写 interact 候选漏写该键(或写成 `false`)会多一条错误;计划 5 的 `build_recipe_candidate` 对 interact 恒写 `true`,不受影响。
8. Ruling: interact 的"对手 `model_ref` 都是具体 id"解释为:`bindings` 必须是非空列表,且 `model_backend` 不是 `self_play` 的每个 binding 都要有非空、非相对的 `model_ref` —— 与 `simulator.datamodel.RoleBinding` 的约定一致(`self_play` 必须为 None,另两种必填) —— 无。
9. Ruling: agent_rsi 的"使用桥接时"由载荷里的布尔键 `uses_bridge`(缺省 `false`)表达,为真时 `prompt_template_version` 必填 —— 撰写时设计 §4.2 的 agent_rsi 键表里没有能判定"是否用桥接"的字段(设计现已回写 `uses_bridge`,见 §4.2) —— 这是对 §4.2 载荷的一处新增键,写 agent_rsi 配方候选的一方(计划 7 或后续计划)须写它;不写则按不用桥接处理,漏报缺失的模板版本。
10. Ruling: `compute_gain` 用 `controlplane.evidence.diagnostic_grade` 给候选与基线两份报告定级,本包不另写等级判断:任一份是 `stub` 返回 `gain=None`;基线是 `mock` 而候选是 `real` 时也返回 `gain=None`,`reason` 写明两边的等级;其余组合(两边同级,或候选 `mock`、基线 `real`)照常相减。证据是否可接受仍由调用方用 `evidence_acceptable` 判 —— 设计 §11"证据等级只有一处实现";R17 默认不让 mock 证据驱动提名,而提名只按候选报告的等级判可接受,real 候选减 mock 基线得到的增益一半来自 mock 分数,照常返回就会在默认配置下驱动提名;候选是 mock 时调用方本来就会按 R17 拒绝或显式接受,不需要在这里再挡。stub 的判法与计划 8 Ruling 11 选基线时相同(同一个 `diagnostic_grade(payload) == "stub"`) —— 后端刚从 mock 切到 real 的那几轮(基线还是 mock 报告)没有可比增益,提名与战绩的 `gain` 都是 `None`,要等基线也换成 real 报告;直接调用 `handle_record_track` 而不经 `reconcile` 时,mock 候选的增益仍会被记进战绩。
11. Ruling: 基线 run 从基线模型 `links` 里找"`cand-<它>` 也在 `links` 里"的那一项,恰好一项才算数 —— `promote_candidate` 写 `links=[cand-<run>, <run>]`,不靠位置猜 —— 晋级写法改了(例如不再带候选 id)就找不到基线,`gain` 为 `None` 并写明原因。
12. Ruling: `compute_gain` 另外四种情况也返回 `gain=None`:候选与基线报告 `benchmark_version` 不同、同一维度有多于一条 l1 分数、l1 分数不是有限数(NaN、正负无穷)、基线模型就是本 run 自己产出的 —— 跨版本分数不可比(`diagnostic.benchmark_version` §2.5.8),重复与自比较都不能给出有意义的差值;NaN 与任何数比较都为假,计划 5 的 `gain < min_gain` 会把 NaN 增益当成达标,还会打乱 `_track_key` 的排序 —— 换了题库或分类表之后的第一轮没有可比增益,提名会因此跳过。
13. Ruling: `regressed` 取"诊断报告里有任意一条退化标记",包括 `g_probe_failure` —— 设计只说"读 diagnostic 的 regression_flags",两种标记都是 `detect_*` 写进来的退化信号 —— G 探针失败也会让配方被 deprecate;若只想看社交退化,改成按 `kind == "social_regression"` 过滤。
14. Ruling: 战绩条目的 `used_as` 先按本 run 指令的 `planner_decision_ref`(形如 `planner-<源 run>`)到 `planner_decision_file(源 run)` 取 `decision.action`;指令没有这个键、键的形状不对或那份决策不在时,退路是扫 `runs/` 下状态为 `executed`、`next_run_id` 等于本 run 的决策(正常树上至多一份);都没有(人工指令)或 action 不是 `clone`/`replay` 时记 `clone` —— 决策放在触发它的源 run 下(设计 §4.1),本 run 只能反查;计划 5 Ruling 14:失败决策分配过的 run id 会被下一次规划再次分配,两份决策可能有相同的 `next_run_id`,读历史要以指令的 `planner_decision_ref` 为准;退路只认 `executed`,因为只有它真写过指令 —— 人工写的 `recipe_ref` 指令一律记成 `clone`;没有 `planner_decision_ref` 的指令每次回写都扫一遍 `runs/`;退路看不到崩在"指令已写、决策未置 executed"之间的 `pending` 决策,指令若恰好又没有 `planner_decision_ref`(计划 5 的指令恒有),会记成 `clone`。
15. Ruling: `handle_record_track` 在"没有指令、没有候选、候选未到终态、没有诊断报告"时什么都不写;在"`recipe_ref` 为空、配方不存在、报告是 stub"时写 `result: skipped` 的标记并写明原因 —— 候选与报告之后会变,写了标记就永远不会再记;后三种不会再变,写标记让 `reconcile` 不再触发;没有指令时 `runs/<run>/` 可能根本不存在,写标记会新建一个只有 `recipe_track.yaml` 的 run 目录,它会被 `_iter_run_ids` 扫进去、参与"最大 run"判定与 run id 分配(计划 5 Ruling 22),一个多余的最大 run 会让 `plan_next_round` 永远不再触发 —— 前一类若被反复触发会每次空转一次(`reconcile` 的触发条件要求 `recipe_ref` 非空、候选终态、证据可接受,已排除它们);直接调用处理器处理一个没有指令的 run,树里不留任何痕迹。
16. Ruling: 两个处理器都把 `reconcile` 的幂等键放在最后一步写:收录最后把候选置 `effective`,战绩回写最后写标记;之前每一步都"不在才写"或按 run_id 幂等 —— 崩在任一步之后重跑都能收敛,有逐步故障注入测试 —— 无。
17. Ruling: `deprecate` 先写 lineage 事件(同一配方只记一次 `deprecated`),再改配方状态;标记里的 `deprecated` 字段以 lineage 记录的触发 run 为准 —— 崩在两步之间重跑时,状态已是 `deprecated`,只看"这次有没有改状态"会把触发者记错 —— 无。
18. Ruling: 收录处理器遇到 `approved` 但没有合法 `repro` 的候选,不收录、不抛,写一次失败记录(`task_id="admit_recipe"`,类别 T2,`point="recipestore.admit"`,计划 1 约定的"子系统.模块"格式);同一候选不重复写;`runs/<run>/` 不存在时不写(理由同 Ruling 15) —— 设计 §4.2 的"证据在场"规则在命令行(计划 8)执行,处理器兜底,与 `reconcile` 对"approved 但无诊断报告不晋级"的态度一致 —— `reconcile` 每轮仍会对它产生 `admit_recipe` 动作(处理器是空操作),直到人把它改掉。
19. Ruling: 收录写配方时直接写成 `effective`,不对配方本身调 `assert_transition`(新产物没有"迁移");对候选 `approved → effective` 调 `assert_transition` —— 设计 §4.1 规定配方由 RecipeStore 写成 `effective` —— 无。
20. Ruling: `handle_admit_recipe` 不因 `target_id` 抛异常,确定性的坏动作一律是空操作:`target_id` 不是 `recipe-<run>` 形状、或解析出的 run id 不是单个路径段(含 `/`、`\`,或是 `.`、`..`)时什么都不做;形状对但候选不在时写一次失败记录(`task_id="admit_recipe"`,类别 T2,`point="recipestore.admit"`),前提是 `runs/<run>/` 已存在,否则也什么都不做;同一 run 只写一次(先查 `exists`,`record_failure` 本身也按 `(run_id, task_id)` 幂等),每 tick 重来不堆提交。瞬态异常(`OSError` 等)仍原样上抛 —— 计划 8 的 `GateView` 用信封 `id` 而不是文件名(计划 8 Ruling 1),手写候选的 id 与文件名不一致、或 id 不以 `recipe-` 开头时,`reconcile` 每 tick 都会产生这个动作;计划 8 调处理器时不接异常,抛错会让守护进程退出、重启后同一动作再来,永久卡死,与 09aeeff("不可读的清单让指令失败而不是卡死守护进程")同一原则;失败记录不新建 run 目录,理由同 Ruling 15 —— 这类坏候选每 tick 仍会产生一次空操作的 `admit_recipe` 动作,直到人改好文件名或 id;形状不对、或 run 目录不存在时树里不留痕迹,只能靠 `status` 里一直挂着的 approved 候选发现。
21. Ruling: 排序 `order="track"` 时 `mean_gain` 为 `None` 的配方排在所有有数据的配方之后(含负增益);另提供 `order="id"`,其他值抛 `ValueError` —— 设计只规定"按 mean_gain 降序",没说空值 —— 没有战绩的新配方永远排在末尾,规划器的"选战绩最好的一条"不会选它。`n_reuse` 是战绩条目总数,含 `gain` 为空的条目(设计 §6.2 已写明)。
22. Ruling: `compare` 只比配置,排除 `result`、`repro`、`source_run_id` 三个键;差异按点分路径展开,列表整体比较,一侧缺失记 `"<missing>"` —— 这三个键是结果、校验输出与来源,不是配置,算进来会淹没真正的配置差异;`stage_manifests` 与 `training_config.stage_manifest_refs` 是训练数据的锚,属于配置:`new` 出来的配方各不相同,`clone`/`replay` 出来的配方与来源配方相同(计划 5 复用来源配方的清单),所以照常比较 —— 两条 `new` 配方的比较结果里恒有清单 id 与哈希的差异。
23. Ruling: `append_track_entry` 严格校验条目恰好是五个键、类型正确;同一 `run_id` 已存在时先写者为准、不覆盖;返回 `None` —— 设计 §11 把它定为 `-> None`;幂等不覆盖是为了让重入不改历史 —— 若某次记错,只能人工改 `track_record.yaml`。
24. Ruling: 测试用的构造函数放 `tests/rs_fixtures.py` 与 `tests/rs_recipes.py`,不建 `conftest.py` —— 仓库现在没有 `conftest.py`,新建一个会影响全部测试的收集 —— 无。
25. Ruling: 全量测试数写成"B + 增量",B 是计划 1 落地后的全量数 —— 计划 1 尚未落地,它会加多少测试本计划无法得知 —— 实施者要先数一次 B。
26. Ruling: 任务 40 改 `pyproject.toml` 的 wheel 列表用追加式脚本:读出 `packages = [...]` 那一行,已含 `"src/recipestore"` 就打印 `already listed` 不改,否则追加到末尾;不精确匹配整行 —— 计划 1、3、4、6 都改同一个列表,只有追加式才与执行顺序无关 —— 无。

---

### Task 40: 前置检查、包骨架与配方 id

**Files:**
- Modify: `pyproject.toml`(wheel 包列表 1 行)
- Create: `src/recipestore/__init__.py`、`src/recipestore/ids.py`
- Test: `tests/test_rs_ids.py`

**Interfaces:**
- Consumes(**计划 1 产出**,本任务第一步检查它们在场):`controlplane.paths` 的 `recipe_candidate_file`、`recipe_dir`、`recipe_file`、`recipe_lineage_file`、`recipe_track_file`、`recipe_track_note_file`、`planner_decision_file`、`failure_record_file`、`SUBDIRS` 末尾四个新目录;`controlplane.ledger.record_failure`;`controlplane.evidence.diagnostic_grade`、`evidence_acceptable`;`controlplane.states` 的 REVIEW 状态机不变。
- Produces(后续任务依赖):
  - `RECIPE_PREFIX = "recipe-"`
  - `recipe_id_for(run_id: str) -> str`:`run-003 → recipe-run-003`,空串抛 `ValueError`。
  - `run_id_of(recipe_id: str) -> str`:反向;不是 `recipe-<非空>` 的形状(含非字符串)抛 `ValueError`。

**设计要点:**
- **前置检查先于一切。** 计划 1 的名字有任何一个缺失或形状不对,停下并报告,不要在本计划里补。检查里断言了路径的具体形状,因为本计划所有路径都从这些函数来,形状错了测试会全部按错的路径通过。
- **id 规则只有一处。** 候选与配方共用 `recipe-<run>`(Ruling 1),处理器拿到 `target_id` 后经 `run_id_of` 取 run id,再交给路径函数。

- [ ] **Step 1: 前置检查(计划 1 的产出在场)**

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src uv run --extra dev python -c "
import inspect
from controlplane.paths import SUBDIRS, recipe_candidate_file, recipe_dir, recipe_file, recipe_lineage_file, recipe_track_file, recipe_track_note_file, planner_decision_file, failure_record_file
from controlplane.ledger import record_failure
from controlplane.evidence import diagnostic_grade, evidence_acceptable
from controlplane.states import REVIEW_TRANSITIONS
assert SUBDIRS[-4:] == ['recipe_candidates', 'skill_library', 'memory_library', 'agent_rounds'], SUBDIRS
assert recipe_candidate_file('run-1') == 'control-plane/recipe_candidates/recipe-run-1.yaml'
assert recipe_dir('run-1') == 'control-plane/recipes/recipe-run-1'
assert recipe_file('run-1') == 'control-plane/recipes/recipe-run-1/recipe.yaml'
assert recipe_lineage_file('run-1') == 'control-plane/recipes/recipe-run-1/lineage.yaml'
assert recipe_track_file('run-1') == 'control-plane/recipes/recipe-run-1/track_record.yaml'
assert recipe_track_note_file('run-1') == 'control-plane/runs/run-1/recipe_track.yaml'
assert planner_decision_file('run-1') == 'control-plane/runs/run-1/planner_decision.yaml'
assert failure_record_file('run-1', 't').startswith('control-plane/runs/run-1/')
assert set(inspect.signature(record_failure).parameters) >= {'store', 'run_id', 'task_id', 'category', 'point', 'retries', 'reason', 'log_ref'}
assert diagnostic_grade({'stub': True}) == 'stub'
assert diagnostic_grade({'backends': 'mock', 'dimension_scores': []}) == 'mock'
assert evidence_acceptable('real', False) and not evidence_acceptable('mock', False) and evidence_acceptable('mock', True)
assert REVIEW_TRANSITIONS['approved'] == {'effective'} and 'deprecated' in REVIEW_TRANSITIONS['effective']
print('preflight ok')
"
```

Expected: 打印 `preflight ok`。**出现 `ImportError`、`AssertionError` 或任何其他输出,停下,把输出原样报告给计划 1 的负责人,不要继续本计划。**

记下起点提交号,再数一次基线:

```bash
BASE=$(git rev-parse HEAD); echo "BASE=$BASE"
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error
```

Expected: 打印 `BASE=<40 位提交号>`;测试全部通过、无警告。把通过数记为 **B**(计划 1 落地后的全量数;修订时的拷贝只搭了计划 1 的任务 19、21、24,那里 B=902)。**把 `BASE` 与 `B` 两个值写进执行记录**,任务 48 要用;shell 会话可能在两个任务之间换掉,不要只留在环境变量里。

- [ ] **Step 2: 写失败测试**

新建 `tests/test_rs_ids.py`:

```python
import pytest

from recipestore.ids import RECIPE_PREFIX, recipe_id_for, run_id_of


def test_a_recipe_id_is_the_prefix_plus_the_source_run():
    assert RECIPE_PREFIX == "recipe-"
    assert recipe_id_for("run-003") == "recipe-run-003"


def test_run_id_of_inverts_recipe_id_for():
    assert run_id_of(recipe_id_for("run-017")) == "run-017"


@pytest.mark.parametrize("bad", ["", "recipe-", "run-003", "cand-run-003", None, 3])
def test_run_id_of_refuses_anything_that_is_not_a_recipe_id(bad):
    with pytest.raises(ValueError):
        run_id_of(bad)


def test_recipe_id_for_refuses_an_empty_run_id():
    with pytest.raises(ValueError):
        recipe_id_for("")
```

- [ ] **Step 3: 运行,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_rs_ids.py`
Expected: 收集阶段报错 `E   ModuleNotFoundError: No module named 'recipestore'`。

- [ ] **Step 4: 写实现**

新建 `src/recipestore/__init__.py`:

```python
"""RecipeStore:配方的可复现性校验、检索、战绩、生命周期与收录(闭环集成设计 §6)。

依赖方向:本包只导入 controlplane,不导入 diagnostic、mindtrainer、trainscheduler。
"""
from __future__ import annotations
```

新建 `src/recipestore/ids.py`:

```python
"""配方 id 与 run id 的互转。配方候选与收录后的配方共用同一个 id:recipe-<来源 run>。"""
from __future__ import annotations

RECIPE_PREFIX = "recipe-"


def recipe_id_for(run_id: str) -> str:
    if not run_id:
        raise ValueError("run_id must be a non-empty string")
    return f"{RECIPE_PREFIX}{run_id}"


def run_id_of(recipe_id: str) -> str:
    """recipe-run-003 -> run-003。形状不对抛 ValueError,不猜。"""
    if not isinstance(recipe_id, str) or not recipe_id.startswith(RECIPE_PREFIX):
        raise ValueError(f"not a recipe id: {recipe_id!r}")
    run_id = recipe_id[len(RECIPE_PREFIX):]
    if not run_id:
        raise ValueError(f"not a recipe id: {recipe_id!r}")
    return run_id
```

- [ ] **Step 5: 确认打包防回归测试抓住了漏登记**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_mt_packaging.py`
Expected: `1 failed, 1 passed`,失败信息为 `AssertionError: missing from [tool.hatch...wheel].packages: ['src/recipestore']`。

- [ ] **Step 6: 把包登记进 wheel 列表**

用下面的追加式脚本在 `packages = [...]` 那一行末尾加 `"src/recipestore"`,不假设列表里现在有几项(Ruling 26):

```bash
uv run python - <<'PYEOF'
import re
from pathlib import Path

p = Path("pyproject.toml")
s = p.read_text(encoding="utf-8")
m = re.search(r'^packages = \[(.*)\]$', s, re.M)
assert m, "wheel packages line not found"
items = [x.strip() for x in m.group(1).split(",") if x.strip()]
if '"src/recipestore"' in items:
    print("already listed")
else:
    items.append('"src/recipestore"')
    s = s[: m.start()] + "packages = [" + ", ".join(items) + "]" + s[m.end():]
    p.write_text(s, encoding="utf-8")
print(re.search(r'^packages = .*$', s, re.M).group(0))
PYEOF
git diff --stat pyproject.toml
```

Expected(两种起点都在撰写时用 `main` 的 `pyproject.toml` 实测过):
- 从基线四项开始时:打印 `packages = ["src/controlplane", "src/simulator", "src/diagnostic", "src/mindtrainer", "src/recipestore"]`。
- 列表已含 `inference_bridge` 时(计划 1 落地后,按设计 §12 是本计划的实际起点):打印 `packages = ["src/controlplane", "src/simulator", "src/diagnostic", "src/mindtrainer", "src/inference_bridge", "src/recipestore"]`。若计划 2 也先落地并加了别的包,它们会出现在 `"src/recipestore"` 前面。
- 两种情况下 `git diff --stat` 都是 `pyproject.toml | 2 +-`。
- 重复运行不会重复追加:第二次运行先打印 `already listed`,再打印与上面相同的那一行,文件不变。

- [ ] **Step 7: 运行,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_rs_ids.py tests/test_mt_packaging.py`
Expected: `11 passed`。

- [ ] **Step 8: 全量回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: **B+9** 通过、无警告(修订时实测 911,B=902)。

- [ ] **Step 9: 提交**

```bash
git add pyproject.toml
git add src/recipestore/__init__.py
git add src/recipestore/ids.py
git add tests/test_rs_ids.py
git commit -m "feat: recipestore package skeleton and recipe ids (recipe-<run> shared by candidate and recipe)"
```

---

### Task 41: 可复现性校验

**Files:**
- Create: `src/recipestore/validate.py`
- Test: `tests/rs_fixtures.py`(共用构造函数,后续任务都用)、`tests/test_rs_validate.py`

**Interfaces:**
- Consumes: 无(纯函数,不读 git 树)。
- Produces(计划 5 的 `build_recipe_candidate` 调它,把结果写进候选的 `repro`):
  - `ReproReport(errors: tuple[str, ...], warnings: tuple[str, ...])`,冻结;属性 `ok`(没有错误);`to_dict() -> {"errors": [...], "warnings": [...]}`。
  - `validate_reproducibility(recipe_payload: dict) -> ReproReport`。
  - `is_relative_ref(value) -> bool`、`RECIPE_TYPES`、`TRAINING_CONFIG_REQUIRED`、`INTERACT_WARNING`。

**设计要点:**
- **三类型表(设计 §6.1)逐项落成检查:**

| 类型 | 错误 | 警告 |
|---|---|---|
| batch | `training_config` 缺失或缺无默认值的键(Ruling 4);`mode` 与类型不一致;两个 checkpoint 引用不带合法 `sha256`;`stage_manifests` 为空、某阶段缺 `id` 或 `manifest_hash` 不是 64 位十六进制;`training_config.stage_manifest_refs` 点名的阶段未锚定或 id 不一致;`code_version.repo_commit` 不是提交号(Ruling 6);`eval_anchor` 缺 `taxonomy_version` 或 `item_pool_version`(Ruling 5) | 无 |
| interact | `training_config` 同上;`scenario_ids` 为空;`bindings` 为空;非 `self_play` 的 binding 没有具体 `model_ref`(Ruling 8);`code_version` 同上;`nondeterministic` 不是 `true`(Ruling 7) | 恒有一条 `INTERACT_WARNING` |
| agent_rsi | 缺 `bound_model_id`、`scenario_ids`、`persona_ids`、`analyzer_config`、`verifier_config`、`generator_version`;`uses_bridge` 为真时缺 `prompt_template_version`(Ruling 9);`uses_bridge` 不是布尔 | 无 |

- **相对引用扫描全载荷**,两处例外:`repro`(本函数自己的输出)与 `training_config.reward.outcome_rules`(奖励关键词,不是引用);每个命中给出点分路径(Ruling 3)。
- **键的排序用 `key=str`**:`stage_manifests` 或 `stage_manifest_refs` 里混了非字符串键也不抛 `TypeError`(有测试)。
- **纯函数**:不读树、不改入参(有测试);未知 `recipe_type` 或载荷不是字典也只返回错误,不抛。
- **校验不拒绝。** 有错误的候选照样写进 `recipe_candidates/`,审核人看 `repro` 决定。

- [ ] **Step 1: 写测试用的共用构造函数**

新建 `tests/rs_fixtures.py`(不以 `test_` 开头,pytest 不收集;后续任务的测试都从它导入):

```python
"""RecipeStore 测试共用的构造函数。不是测试文件(不以 test_ 开头),pytest 不收集它;
测试文件靠 pytest 默认的 prepend 导入模式把 tests/ 放进 sys.path 后直接导入。"""
from __future__ import annotations

import copy

from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import (
    candidate_checkpoint_file,
    diagnostic_report_file,
    model_file,
    planner_decision_file,
    recipe_candidate_file,
    run_dir,
)

NOW = "2026-10-09T00:00:00Z"
SHA = "a" * 64


def make_store(tmp_path) -> GitStore:
    store = GitStore(str(tmp_path))
    store.init()
    return store


def _ref(name: str) -> dict:
    return {"uri": f"file:///compute/{name}.safetensors", "sha256": SHA, "size_bytes": 1, "fetch_hint": None}


def training_config(mode: str = "batch") -> dict:
    cfg = {
        "mode": mode,
        "trained_slot": "a",
        "base_checkpoint_ref": _ref("base"),
        "teacher_model_ref": _ref("teacher"),
        "reward": {"components": ["outcome"], "attribution": "outcome_split", "combination": "sum"},
        "sft_steps": 1,
        "grpo_steps": 1,
        "sft_batch_size": 1,
        "groups_per_step": 1,
        "group_size": 2,
        "grpo_config": {"lr": 1e-5},
        "opd_coefficient": 0.1,
        "checkpoint_every_steps": 1,
        "allow_resume": True,
    }
    if mode == "batch":
        cfg["stage_manifest_refs"] = {"sft": "dsm-run-003-sft", "grpo_opd": "dsm-run-003-grpo_opd"}
    return cfg


def eval_anchor() -> dict:
    return {
        "taxonomy_version": "tax-v1",
        "item_pool_version": "pool-v1",
        "benchmark_version": "bench-v1",
        "judge_prompt_version": None,
        "scoring_config_version": None,
    }


def _common(recipe_type: str) -> dict:
    return {
        "recipe_type": recipe_type,
        "dimension": "tom",
        "source_run_id": "run-003",
        "eval_anchor": eval_anchor(),
        "result": {"gain": 0.05, "baseline_ref": "zengen-v1", "candidate_ref": "cand-run-003", "promoted": True},
        "repro": {"errors": [], "warnings": []},
        "nondeterministic": False,
    }


def batch_payload() -> dict:
    p = _common("batch")
    p["training_config"] = training_config("batch")
    p["stage_manifests"] = {
        "sft": {"id": "dsm-run-003-sft", "manifest_hash": "b" * 64},
        "grpo_opd": {"id": "dsm-run-003-grpo_opd", "manifest_hash": "c" * 64},
    }
    p["code_version"] = {"repo_commit": "0385004"}
    return p


def interact_payload() -> dict:
    p = _common("interact")
    p["nondeterministic"] = True
    p["training_config"] = training_config("interact")
    p["scenario_ids"] = ["scen-1"]
    p["bindings"] = [
        {"slot_id": "a", "persona_id": "p-1", "model_backend": "self_play"},
        {"slot_id": "b", "persona_id": "p-2", "model_backend": "frozen_reference", "model_ref": "zengen-v1"},
    ]
    p["code_version"] = {"repo_commit": "0385004"}
    return p


def agent_rsi_payload() -> dict:
    p = _common("agent_rsi")
    p["bound_model_id"] = "zengen-v2"
    p["scenario_ids"] = ["scen-1"]
    p["persona_ids"] = ["p-1"]
    p["analyzer_config"] = {"min_support": 3, "min_ratio": 0.5}
    p["verifier_config"] = {"method": "replay_behavior"}
    p["generator_version"] = "rule-v1"
    p["uses_bridge"] = False
    return p


def envelope(id_: str, type_: str, status: str, payload: dict, links: list[str] | None = None) -> Envelope:
    return Envelope(
        id=id_, type=type_, version=1, created_at=NOW, created_by=OwnerRef(),
        status=status, schema_version=1, links=list(links or []), payload=copy.deepcopy(payload),
    )


def l1(dimension: str, score: float, *, level: str = "l1") -> dict:
    return {
        "dimension": dimension, "level": level, "score": score, "ci_low": max(0.0, score - 0.05),
        "ci_high": min(1.0, score + 0.05), "sample_size": 40, "track_provenance": "static",
    }


def write_report(
    store: GitStore,
    run_id: str,
    *,
    scores: list[dict] | None = None,
    baseline_ref: str = "",
    flags: list[dict] | None = None,
    benchmark_version: str = "bench-v1",
    stub: bool = False,
    backends: str | None = "mock",
) -> None:
    """按 diagnostic.datamodel.DiagnosticReport.to_dict() 的形状手工写一份诊断报告;
    stub=True 时写成 simulator.stubs.diagnostic_evaluator_stub 的形状。"""
    if stub:
        payload = {"candidate_ref": f"cand-{run_id}", "benchmark_version": benchmark_version,
                   "dimension_count": 0, "stub": True}
    else:
        payload = {
            "id": run_id, "candidate_ref": f"cand-{run_id}", "baseline_ref": baseline_ref,
            "benchmark_version": benchmark_version, "run_tier": "quick", "sampling_k": 1,
            "dimension_scores": list(scores or []), "error_profiles": [],
            "regression_flags": {"flags": list(flags or [])},
            "consistency_signals": {"rule_vs_judge_divergence": {}, "diagnostic_vs_reward_divergence": 0.0},
            "links": [run_id],
        }
        if backends is not None:
            payload["backends"] = backends
    store.write_product(
        diagnostic_report_file(run_id),
        envelope(f"diag-{run_id}", "diagnostic_report", "executed", payload, [run_id]),
        f"test: report {run_id}",
    )


def write_model(store: GitStore, n: int, run_id: str, *, status: str = "effective") -> None:
    """与 controlplane.daemon.apply_action 的 promote_candidate 写法一致:links=[候选 id, run id]。"""
    store.write_product(
        model_file(n),
        envelope(f"zengen-v{n}", "zengen_model", status, {"promoted_from": f"cand-{run_id}"},
                 [f"cand-{run_id}", run_id]),
        f"test: model v{n}",
    )


def write_candidate_checkpoint(store: GitStore, run_id: str, status: str) -> None:
    store.write_product(
        candidate_checkpoint_file(run_id),
        envelope(f"cand-{run_id}", "candidate_checkpoint", status, {"checkpoint_ref": _ref(run_id)}, [run_id]),
        f"test: candidate {run_id}",
    )


def write_directive(store: GitStore, run_id: str, *, recipe_ref: str = "", status: str = "executed",
                    planner_decision_ref: str | None = None) -> None:
    """planner_decision_ref 为 None 时不写这个键(人工指令);计划 5 的指令写 planner-<源 run>。"""
    payload = {"target_dimension": "tom", "recipe_ref": recipe_ref}
    if planner_decision_ref is not None:
        payload["planner_decision_ref"] = planner_decision_ref
    store.write_product(
        f"{run_dir(run_id)}/directive.yaml",
        envelope(run_id, "directive", status, payload),
        f"test: directive {run_id}",
    )


def write_planner_decision(store: GitStore, source_run_id: str, next_run_id: str, action: str,
                           *, status: str = "executed") -> None:
    """规划决策放在触发规划的那个 run 下(设计 §4.1),next_run_id 指向它产出的指令。"""
    store.write_product(
        planner_decision_file(source_run_id),
        envelope(f"planner-{source_run_id}", "planner_decision", status, {
            "source_run_id": source_run_id, "next_run_id": next_run_id, "template_version": "v1",
            "backend": "heuristic",
            "decision": {"target_dimension": "tom", "action": action, "mode": None,
                         "source_recipe_id": "recipe-run-003", "perturbation": {},
                         "data_synthesis_hint": ""},
            "rationale": "test", "failure": None,
        }),
        f"test: decision {source_run_id}",
    )


def write_recipe_candidate(store: GitStore, run_id: str, status: str, payload: dict | None = None) -> None:
    body = batch_payload() if payload is None else payload
    body = copy.deepcopy(body)
    body["source_run_id"] = run_id
    store.write_product(
        recipe_candidate_file(run_id),
        envelope(f"recipe-{run_id}", "recipe_candidate", status, body, [run_id, f"cand-{run_id}"]),
        f"test: recipe candidate {run_id}",
    )
```

- [ ] **Step 2: 写失败测试**

新建 `tests/test_rs_validate.py`:

```python
import copy

import pytest

from recipestore.validate import (
    INTERACT_WARNING,
    ReproReport,
    is_relative_ref,
    validate_reproducibility,
)
from rs_fixtures import agent_rsi_payload, batch_payload, interact_payload


def _without(payload, *path):
    p = copy.deepcopy(payload)
    node = p
    for key in path[:-1]:
        node = node[key]
    del node[path[-1]]
    return p


def _with(payload, value, *path):
    p = copy.deepcopy(payload)
    node = p
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return p


# ---- batch ----

def test_a_complete_batch_recipe_has_no_errors_and_no_warnings():
    assert validate_reproducibility(batch_payload()) == ReproReport(errors=(), warnings=())


@pytest.mark.parametrize("path, needle", [
    (("training_config",), "training_config: missing"),
    (("training_config", "sft_steps"), "training_config.sft_steps: missing"),
    (("training_config", "reward"), "training_config.reward: missing"),
    (("stage_manifests",), "stage_manifests: must be a non-empty mapping"),
    (("stage_manifests", "sft", "manifest_hash"), "stage_manifests.sft.manifest_hash"),
    (("stage_manifests", "sft", "id"), "stage_manifests.sft.id: missing"),
    (("code_version",), "code_version: missing"),
    (("code_version", "repo_commit"), "code_version.repo_commit"),
    (("eval_anchor", "taxonomy_version"), "eval_anchor.taxonomy_version: missing"),
    (("eval_anchor", "item_pool_version"), "eval_anchor.item_pool_version: missing"),
])
def test_a_batch_recipe_missing_an_anchor_is_an_error(path, needle):
    report = validate_reproducibility(_without(batch_payload(), *path))
    assert not report.ok
    assert any(needle in e for e in report.errors), report.errors


def test_a_batch_stage_the_config_names_but_the_recipe_does_not_anchor_is_an_error():
    p = _without(batch_payload(), "stage_manifests", "grpo_opd")
    errors = validate_reproducibility(p).errors
    assert "stage_manifests.grpo_opd: training_config names it but it is not anchored" in errors


def test_a_batch_stage_whose_anchored_id_differs_from_the_config_is_an_error():
    p = _with(batch_payload(), "dsm-run-001-sft", "stage_manifests", "sft", "id")
    assert any("does not match training_config.stage_manifest_refs.sft" in e
               for e in validate_reproducibility(p).errors)


def test_a_batch_recipe_whose_mode_disagrees_with_its_type_is_an_error():
    p = _with(batch_payload(), "interact", "training_config", "mode")
    assert any("does not match recipe_type 'batch'" in e for e in validate_reproducibility(p).errors)


def test_a_checkpoint_ref_without_a_real_digest_is_an_error():
    p = _with(batch_payload(), "latest", "training_config", "base_checkpoint_ref", "sha256")
    errors = validate_reproducibility(p).errors
    assert "training_config.base_checkpoint_ref.sha256: must be a 64-char lowercase hex digest" in errors


@pytest.mark.parametrize("commit", ["HEAD", "main", "abc", "", None])
def test_a_branch_name_or_short_junk_is_not_a_commit(commit):
    p = _with(batch_payload(), commit, "code_version", "repo_commit")
    assert any(e.startswith("code_version.repo_commit") for e in validate_reproducibility(p).errors)


# ---- interact ----

def test_a_complete_interact_recipe_has_no_errors_and_one_warning():
    report = validate_reproducibility(interact_payload())
    assert report.errors == ()
    assert report.warnings == (INTERACT_WARNING,)


@pytest.mark.parametrize("path, needle", [
    (("training_config",), "training_config: missing"),
    (("scenario_ids",), "scenario_ids: missing"),
    (("code_version",), "code_version: missing"),
    (("bindings",), "bindings: must be a non-empty list"),
])
def test_an_interact_recipe_missing_an_anchor_is_an_error(path, needle):
    errors = validate_reproducibility(_without(interact_payload(), *path)).errors
    assert any(needle in e for e in errors), errors


def test_an_opponent_without_a_concrete_model_ref_is_an_error():
    p = _without(interact_payload(), "bindings", 1, "model_ref")
    assert "bindings[1].model_ref: opponent must name a concrete model id" in validate_reproducibility(p).errors


def test_an_opponent_pinned_to_latest_is_an_error():
    p = _with(interact_payload(), "latest", "bindings", 1, "model_ref")
    errors = validate_reproducibility(p).errors
    assert "bindings[1].model_ref: opponent must name a concrete model id" in errors
    assert "bindings[1].model_ref: relative reference 'latest' is not anchored" in errors


def test_an_interact_recipe_that_claims_to_be_deterministic_is_an_error():
    p = _with(interact_payload(), False, "nondeterministic")
    assert any(e.startswith("nondeterministic:") for e in validate_reproducibility(p).errors)


# ---- agent_rsi ----

def test_a_complete_agent_rsi_recipe_without_a_bridge_has_no_errors():
    assert validate_reproducibility(agent_rsi_payload()) == ReproReport(errors=(), warnings=())


@pytest.mark.parametrize("key", [
    "bound_model_id", "scenario_ids", "persona_ids", "analyzer_config", "verifier_config", "generator_version",
])
def test_an_agent_rsi_recipe_missing_an_anchor_is_an_error(key):
    errors = validate_reproducibility(_without(agent_rsi_payload(), key)).errors
    assert f"{key}: missing" in errors


def test_using_the_bridge_without_a_prompt_template_version_is_an_error():
    p = _with(agent_rsi_payload(), True, "uses_bridge")
    assert "prompt_template_version: missing" in validate_reproducibility(p).errors
    p["prompt_template_version"] = "proposer-v1"
    assert validate_reproducibility(p).errors == ()


def test_uses_bridge_must_be_a_boolean():
    p = _with(agent_rsi_payload(), "yes", "uses_bridge")
    assert "uses_bridge: must be a boolean" in validate_reproducibility(p).errors


def test_a_bound_model_of_current_best_is_an_error():
    p = _with(agent_rsi_payload(), "current_best", "bound_model_id")
    assert "bound_model_id: relative reference 'current_best' is not anchored" in validate_reproducibility(p).errors


# ---- 通用 ----

@pytest.mark.parametrize("value", ["latest", "LATEST", "current_best", "head", "zengen:latest", "model-latest"])
def test_relative_refs_are_recognised(value):
    assert is_relative_ref(value)


@pytest.mark.parametrize("value", ["zengen-v3", "dsm-run-003-sft", "latestness", "", 3, None])
def test_concrete_values_are_not_relative(value):
    assert not is_relative_ref(value)


def test_a_relative_ref_anywhere_in_the_payload_is_an_error():
    p = _with(batch_payload(), "latest", "training_config", "grpo_config", "scheduler")
    assert "training_config.grpo_config.scheduler: relative reference 'latest' is not anchored" in (
        validate_reproducibility(p).errors
    )


def test_the_repro_field_itself_is_not_scanned():
    p = _with(batch_payload(), {"errors": ["x: relative reference 'latest'"], "warnings": []}, "repro")
    assert validate_reproducibility(p).errors == ()


def test_outcome_rule_keywords_are_not_scanned():
    # reward.outcome_rules 是结局匹配关键词,不是引用(Ruling 3 的例外)
    p = _with(batch_payload(), {"outcome": ["best", "current", "previous"]}, "training_config", "reward", "outcome_rules")
    assert validate_reproducibility(p).errors == ()


def test_the_rest_of_the_reward_is_still_scanned():
    p = _with(batch_payload(), {"outcome": ["best"]}, "training_config", "reward", "outcome_rules")
    p["training_config"]["reward"]["combination"] = "latest"
    assert validate_reproducibility(p).errors == (
        "training_config.reward.combination: relative reference 'latest' is not anchored",
    )


def test_stage_keys_of_mixed_types_do_not_raise():
    p = batch_payload()
    p["stage_manifests"][3] = {"id": "dsm-x", "manifest_hash": "d" * 64}
    p["training_config"]["stage_manifest_refs"][7] = "dsm-y"
    errors = validate_reproducibility(p).errors
    assert "stage_manifests.7: training_config names it but it is not anchored" in errors


@pytest.mark.parametrize("bad", [None, "", "rl", 3])
def test_an_unknown_recipe_type_is_an_error(bad):
    p = _with(batch_payload(), bad, "recipe_type")
    assert validate_reproducibility(p).errors[0].startswith("recipe_type: must be one of")


def test_a_payload_that_is_not_a_mapping_is_an_error():
    assert validate_reproducibility(["x"]).errors == ("payload: must be a mapping",)


def test_validation_does_not_mutate_its_input():
    p = batch_payload()
    snapshot = copy.deepcopy(p)
    validate_reproducibility(p)
    assert p == snapshot


def test_the_report_serialises_as_lists():
    assert ReproReport(errors=("a",), warnings=("b",)).to_dict() == {"errors": ["a"], "warnings": ["b"]}
```

- [ ] **Step 3: 运行,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_rs_validate.py`
Expected: 收集阶段报错 `E   ModuleNotFoundError: No module named 'recipestore.validate'`。

- [ ] **Step 4: 写实现**

新建 `src/recipestore/validate.py`:

```python
"""可复现性校验(设计 §6.1)。纯函数:不读 git 树、不改入参。

错误 = 未锚定的软引用或缺失的必填项;警告 = 已知的复现限制。结果写进候选的 repro,
不硬拒绝,由审核门③的人参考。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

RECIPE_TYPES = ("batch", "interact", "agent_rsi")
# 与 trainplanner.context.RECIPE_TYPES(计划 4)是同两份字面量,必须同步;计划 9 任务 109
# 有一条跨计划一致性测试钉住两边相等(本包不导入 trainplanner,所以只能放在计划 9)。

# TrainingConfig(mindtrainer.training_config)里没有默认值的字段。本包不导入 mindtrainer,
# 所以这里只核对"键在场",类型与取值由 MindTrainer 在派发时校验。
TRAINING_CONFIG_REQUIRED = (
    "mode", "trained_slot", "base_checkpoint_ref", "teacher_model_ref", "reward",
    "sft_steps", "grpo_steps", "sft_batch_size", "groups_per_step", "group_size",
    "grpo_config", "opd_coefficient", "checkpoint_every_steps", "allow_resume",
)

# 相对引用:取值随时间漂移,锚不住。比较时忽略大小写。
RELATIVE_TOKENS = frozenset({
    "latest", "current", "current_best", "best", "newest", "head", "previous", "prev",
})
_RELATIVE_SUFFIXES = (":latest", "-latest", "@latest", "_latest")

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{7,40}$")

INTERACT_WARNING = "interact recipes are nondeterministic: a rerun can only be highly similar, not identical"


@dataclass(frozen=True)
class ReproReport:
    errors: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {"errors": list(self.errors), "warnings": list(self.warnings)}


def is_relative_ref(value: object) -> bool:
    if not isinstance(value, str):
        return False
    low = value.strip().lower()
    return low in RELATIVE_TOKENS or low.endswith(_RELATIVE_SUFFIXES)


def _walk_strings(obj: object, path: str):
    if isinstance(obj, dict):
        for k in sorted(obj, key=str):
            yield from _walk_strings(obj[k], f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk_strings(v, f"{path}[{i}]")
    elif isinstance(obj, str):
        yield path, obj


def _concrete_str(value: object) -> bool:
    return isinstance(value, str) and value.strip() != "" and not is_relative_ref(value)


def _need_str(p: dict, key: str, errors: list[str], label: str | None = None) -> None:
    label = label or key
    value = p.get(key)
    if value is None:
        errors.append(f"{label}: missing")
    elif not isinstance(value, str) or not value.strip():
        errors.append(f"{label}: must be a non-empty string")


def _need_str_list(p: dict, key: str, errors: list[str]) -> None:
    value = p.get(key)
    if value is None:
        errors.append(f"{key}: missing")
    elif not isinstance(value, list) or not value:
        errors.append(f"{key}: must be a non-empty list")
    else:
        for i, v in enumerate(value):
            if not isinstance(v, str) or not v.strip():
                errors.append(f"{key}[{i}]: must be a non-empty string")


def _need_dict(p: dict, key: str, errors: list[str]) -> None:
    value = p.get(key)
    if value is None:
        errors.append(f"{key}: missing")
    elif not isinstance(value, dict) or not value:
        errors.append(f"{key}: must be a non-empty mapping")


def _check_bigref(cfg: dict, key: str, errors: list[str]) -> None:
    ref = cfg.get(key)
    if not isinstance(ref, dict):
        errors.append(f"training_config.{key}: must be a BigRef mapping")
        return
    if not isinstance(ref.get("sha256"), str) or not _SHA256.match(ref["sha256"]):
        errors.append(f"training_config.{key}.sha256: must be a 64-char lowercase hex digest")
    if not isinstance(ref.get("uri"), str) or not ref["uri"].strip():
        errors.append(f"training_config.{key}.uri: must be a non-empty string")


def _check_training_config(p: dict, recipe_type: str, errors: list[str]) -> dict:
    cfg = p.get("training_config")
    if cfg is None:
        errors.append("training_config: missing")
        return {}
    if not isinstance(cfg, dict):
        errors.append("training_config: must be a mapping")
        return {}
    for key in TRAINING_CONFIG_REQUIRED:
        if key not in cfg:
            errors.append(f"training_config.{key}: missing")
    if "mode" in cfg and cfg["mode"] != recipe_type:
        errors.append(f"training_config.mode: {cfg['mode']!r} does not match recipe_type {recipe_type!r}")
    for key in ("base_checkpoint_ref", "teacher_model_ref"):
        if key in cfg:
            _check_bigref(cfg, key, errors)
    return cfg


def _check_code_version(p: dict, errors: list[str]) -> None:
    cv = p.get("code_version")
    if not isinstance(cv, dict):
        errors.append("code_version: missing")
        return
    commit = cv.get("repo_commit")
    if not isinstance(commit, str) or not _COMMIT.match(commit):
        errors.append("code_version.repo_commit: must be a 7-40 char lowercase hex commit id")


def _check_batch(p: dict, errors: list[str], warnings: list[str]) -> None:
    cfg = _check_training_config(p, "batch", errors)
    stages = p.get("stage_manifests")
    if not isinstance(stages, dict) or not stages:
        errors.append("stage_manifests: must be a non-empty mapping")
        stages = {}
    for stage in sorted(stages, key=str):  # key=str:非字符串键也不让 sorted 抛 TypeError
        entry = stages[stage]
        if not isinstance(entry, dict):
            errors.append(f"stage_manifests.{stage}: must be a mapping")
            continue
        if not _concrete_str(entry.get("id")):
            errors.append(f"stage_manifests.{stage}.id: missing")
        h = entry.get("manifest_hash")
        if not isinstance(h, str) or not _SHA256.match(h):
            errors.append(f"stage_manifests.{stage}.manifest_hash: must be a 64-char lowercase hex digest")
    refs = cfg.get("stage_manifest_refs", {}) if isinstance(cfg, dict) else {}
    if isinstance(refs, dict):
        for stage in sorted(refs, key=str):
            entry = stages.get(stage)
            if not isinstance(entry, dict):
                errors.append(f"stage_manifests.{stage}: training_config names it but it is not anchored")
            elif entry.get("id") != refs[stage]:
                errors.append(
                    f"stage_manifests.{stage}.id: {entry.get('id')!r} does not match "
                    f"training_config.stage_manifest_refs.{stage} {refs[stage]!r}"
                )
    _check_code_version(p, errors)
    anchor = p.get("eval_anchor")
    if not isinstance(anchor, dict):
        errors.append("eval_anchor: missing")
    else:
        _need_str(anchor, "taxonomy_version", errors, "eval_anchor.taxonomy_version")
        _need_str(anchor, "item_pool_version", errors, "eval_anchor.item_pool_version")


def _check_interact(p: dict, errors: list[str], warnings: list[str]) -> None:
    _check_training_config(p, "interact", errors)
    _need_str_list(p, "scenario_ids", errors)
    bindings = p.get("bindings")
    if not isinstance(bindings, list) or not bindings:
        errors.append("bindings: must be a non-empty list")
        bindings = []
    for i, b in enumerate(bindings):
        if not isinstance(b, dict):
            errors.append(f"bindings[{i}]: must be a mapping")
            continue
        if b.get("model_backend") != "self_play" and not _concrete_str(b.get("model_ref")):
            errors.append(f"bindings[{i}].model_ref: opponent must name a concrete model id")
    _check_code_version(p, errors)
    if p.get("nondeterministic") is not True:
        errors.append("nondeterministic: interact recipes must declare nondeterministic: true")
    warnings.append(INTERACT_WARNING)


def _check_agent_rsi(p: dict, errors: list[str], warnings: list[str]) -> None:
    _need_str(p, "bound_model_id", errors)
    _need_str_list(p, "scenario_ids", errors)
    _need_str_list(p, "persona_ids", errors)
    _need_dict(p, "analyzer_config", errors)
    _need_dict(p, "verifier_config", errors)
    _need_str(p, "generator_version", errors)
    uses_bridge = p.get("uses_bridge", False)
    if not isinstance(uses_bridge, bool):
        errors.append("uses_bridge: must be a boolean")
    elif uses_bridge:
        _need_str(p, "prompt_template_version", errors)


_CHECKS = {"batch": _check_batch, "interact": _check_interact, "agent_rsi": _check_agent_rsi}


def _scan_view(payload: dict) -> dict:
    """参与相对引用扫描的那部分载荷(浅拷贝,不改入参)。两处例外:repro 是本函数自己的输出;
    training_config.reward.outcome_rules 是奖励的结局匹配关键词(自然语言词表,不是引用),
    用户把 best、current、previous 当关键词是正常用法。"""
    scanned = {k: v for k, v in payload.items() if k != "repro"}
    cfg = scanned.get("training_config")
    if isinstance(cfg, dict) and isinstance(cfg.get("reward"), dict) and "outcome_rules" in cfg["reward"]:
        reward = {k: v for k, v in cfg["reward"].items() if k != "outcome_rules"}
        scanned["training_config"] = dict(cfg, reward=reward)
    return scanned


def validate_reproducibility(recipe_payload: dict) -> ReproReport:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(recipe_payload, dict):
        return ReproReport(errors=("payload: must be a mapping",), warnings=())
    recipe_type = recipe_payload.get("recipe_type")
    check = _CHECKS.get(recipe_type) if isinstance(recipe_type, str) else None
    if check is None:
        errors.append(f"recipe_type: must be one of {list(RECIPE_TYPES)}, got {recipe_type!r}")
    else:
        check(recipe_payload, errors, warnings)
    for path, value in _walk_strings(_scan_view(recipe_payload), ""):
        if is_relative_ref(value):
            errors.append(f"{path}: relative reference {value!r} is not anchored")
    return ReproReport(errors=tuple(errors), warnings=tuple(warnings))
```

- [ ] **Step 5: 运行,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_rs_validate.py`
Expected: `62 passed`。

- [ ] **Step 6: 全量回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: **B+71** 通过、无警告(修订时实测 973)。

- [ ] **Step 7: 提交**

```bash
git add src/recipestore/validate.py
git add tests/rs_fixtures.py
git add tests/test_rs_validate.py
git commit -m "feat: recipestore reproducibility validator (per-type anchors, relative refs are errors, never rejects)"
```

---

### Task 42: 增益 `compute_gain`

**Files:**
- Create: `src/recipestore/gain.py`
- Test: `tests/test_rs_gain.py`

**Interfaces:**
- Consumes: `GitStore.exists` / `read_product`;`controlplane.paths.diagnostic_report_file`、`model_file`;`controlplane.evidence.diagnostic_grade`(计划 1)。
- Produces(设计 §11 契约;计划 5 的提名与本计划任务 46 都调它):
  - `GainResult(gain: float | None, baseline_run_id: str | None, reason: str)`,冻结。成功时 `reason == OK == "ok"`。
  - `compute_gain(store, run_id, dimension) -> GainResult`。
  - 辅助(任务 46 与计划 5 可复用):`read_report_payload(store, run_id) -> dict | None`、`l1_score(payload, dimension) -> (float | None, str)`、`regression_flags(payload) -> list[dict]`、`baseline_run_of(store, baseline_ref) -> (str | None, str)`。

**设计要点:**
- **链路**:候选报告 → l1 分数 → 报告的 `baseline_ref`(`zengen-vN`)→ `models/zengen-vN.yaml` 的 `links` → 基线 run(Ruling 11)→ 基线报告 → 同维度 l1 分数 → 相减。
- **任何一环缺失返回 `gain=None`,`reason` 写明缺什么**,一共 14 种:候选无报告、候选报告是 stub、候选缺该维度 l1、无 `baseline_ref`、`baseline_ref` 不是模型 id、基线模型不在、模型 `links` 认不出 run、基线是自己、基线 run 无报告、基线报告是 stub、real 候选对非 real 基线、版本不可比、基线缺该维度、分数不是数字、不是有限数或重复(Ruling 12)。基线 run 已经找到的情况下 `baseline_run_id` 照填,便于审核人追查。
- **等级只走 `diagnostic_grade`**(Ruling 10):两份报告都定级;任一份 `stub` 无增益;候选 `real` 而基线不是 `real` 无增益,`reason` 写明基线等级;是否可接受仍归调用方。
- **不导入 `diagnostic`。** 最后一条测试用 `diagnostic.report.build_report` + `diagnostic.store_adapter.write_report` 真实写报告,钉住字段名;`diagnostic` 改了字段名这条测试会先失败。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_rs_gain.py`:

```python
import pytest

from controlplane.paths import model_file
from recipestore.gain import (
    OK,
    GainResult,
    baseline_run_of,
    compute_gain,
    l1_score,
    read_report_payload,
    regression_flags,
)
from rs_fixtures import envelope, l1, make_store, write_model, write_report


@pytest.fixture
def store(tmp_path):
    s = make_store(tmp_path)
    # 基线:run-001 产出 zengen-v1,它的报告 tom=0.50
    write_model(s, 1, "run-001")
    write_report(s, "run-001", scores=[l1("tom", 0.50), l1("tom", 0.9, level="l2")])
    return s


def test_gain_is_the_candidate_l1_minus_the_baseline_l1(store):
    write_report(store, "run-002", scores=[l1("tom", 0.62)], baseline_ref="zengen-v1")
    result = compute_gain(store, "run-002", "tom")
    assert result.gain == pytest.approx(0.12)
    assert (result.baseline_run_id, result.reason) == ("run-001", OK)


def test_a_negative_gain_is_reported_as_is(store):
    write_report(store, "run-002", scores=[l1("tom", 0.40)], baseline_ref="zengen-v1")
    assert compute_gain(store, "run-002", "tom").gain == pytest.approx(-0.10)


def test_only_l1_scores_count(store):
    write_report(store, "run-002", scores=[l1("tom", 0.99, level="l2")], baseline_ref="zengen-v1")
    assert compute_gain(store, "run-002", "tom") == GainResult(
        None, None, "run-002: report has no l1 score for dimension 'tom'"
    )


def test_no_candidate_report(store):
    assert compute_gain(store, "run-002", "tom") == GainResult(None, None, "no diagnostic report for run-002")


def test_a_stub_candidate_report(store):
    write_report(store, "run-002", stub=True)
    assert compute_gain(store, "run-002", "tom").reason == "diagnostic report for run-002 is a stub"


def test_no_baseline_ref(store):
    write_report(store, "run-002", scores=[l1("tom", 0.6)], baseline_ref="")
    assert compute_gain(store, "run-002", "tom").reason == "diagnostic report for run-002 has no baseline_ref"


def test_a_baseline_ref_that_is_not_a_model_id(store):
    write_report(store, "run-002", scores=[l1("tom", 0.6)], baseline_ref="latest")
    assert compute_gain(store, "run-002", "tom") == GainResult(
        None, None, "baseline_ref 'latest' is not a zengen-vN model id"
    )


def test_no_baseline_model(store):
    write_report(store, "run-002", scores=[l1("tom", 0.6)], baseline_ref="zengen-v7")
    assert compute_gain(store, "run-002", "tom").reason == "baseline model zengen-v7 not found"


def test_a_baseline_model_whose_links_do_not_name_a_run(store):
    store.write_product(model_file(2), envelope("zengen-v2", "zengen_model", "effective", {}, ["x"]), "t")
    write_report(store, "run-002", scores=[l1("tom", 0.6)], baseline_ref="zengen-v2")
    assert compute_gain(store, "run-002", "tom").reason == (
        "baseline model zengen-v2 links do not name exactly one producing run"
    )


def test_the_baseline_run_has_no_report(tmp_path):
    s = make_store(tmp_path)
    write_model(s, 1, "run-001")
    write_report(s, "run-002", scores=[l1("tom", 0.6)], baseline_ref="zengen-v1")
    assert compute_gain(s, "run-002", "tom") == GainResult(
        None, "run-001", "no diagnostic report for baseline run run-001"
    )


def test_the_baseline_run_has_a_stub_report(tmp_path):
    s = make_store(tmp_path)
    write_model(s, 1, "run-001")
    write_report(s, "run-001", stub=True)
    write_report(s, "run-002", scores=[l1("tom", 0.6)], baseline_ref="zengen-v1")
    assert compute_gain(s, "run-002", "tom").reason == "diagnostic report for baseline run run-001 is a stub"


def test_the_baseline_report_lacks_the_dimension(store):
    write_report(store, "run-002", scores=[l1("tom", 0.6), l1("emotion", 0.7)], baseline_ref="zengen-v1")
    assert compute_gain(store, "run-002", "emotion") == GainResult(
        None, "run-001", "baseline run run-001: report has no l1 score for dimension 'emotion'"
    )


def test_different_benchmark_versions_are_not_comparable(store):
    write_report(store, "run-002", scores=[l1("tom", 0.6)], baseline_ref="zengen-v1", benchmark_version="bench-v2")
    result = compute_gain(store, "run-002", "tom")
    assert result.gain is None and "not comparable" in result.reason


def test_a_run_cannot_be_its_own_baseline(tmp_path):
    s = make_store(tmp_path)
    write_model(s, 1, "run-002")
    write_report(s, "run-002", scores=[l1("tom", 0.6)], baseline_ref="zengen-v1")
    assert compute_gain(s, "run-002", "tom") == GainResult(
        None, "run-002", "baseline zengen-v1 was produced by run-002 itself"
    )


def test_two_l1_scores_for_one_dimension_are_refused():
    payload = {"dimension_scores": [l1("tom", 0.1), l1("tom", 0.2)]}
    assert l1_score(payload, "tom") == (None, "report has 2 l1 scores for dimension 'tom'; refusing to pick one")


@pytest.mark.parametrize("score", ["0.5", None, True])
def test_a_non_numeric_score_is_refused(score):
    payload = {"dimension_scores": [dict(l1("tom", 0.1), score=score)]}
    assert l1_score(payload, "tom") == (None, "l1 score for dimension 'tom' is not a number")


@pytest.mark.parametrize("score", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_score_is_refused(score):
    payload = {"dimension_scores": [dict(l1("tom", 0.1), score=score)]}
    assert l1_score(payload, "tom") == (None, "l1 score for dimension 'tom' is not a finite number")


def test_a_real_candidate_against_a_mock_baseline_has_no_gain(store):
    # 夹具的基线报告是 mock;backends=None 的报告按 controlplane.evidence 判 real
    write_report(store, "run-002", scores=[l1("tom", 0.62)], baseline_ref="zengen-v1", backends=None)
    assert compute_gain(store, "run-002", "tom") == GainResult(
        None, "run-001", "baseline run run-001 evidence is mock while run-002 is real; scores are not comparable"
    )


def test_a_real_baseline_is_comparable_to_a_real_or_a_mock_candidate(tmp_path):
    s = make_store(tmp_path)
    write_model(s, 1, "run-001")
    write_report(s, "run-001", scores=[l1("tom", 0.50)], backends=None)
    write_report(s, "run-002", scores=[l1("tom", 0.60)], baseline_ref="zengen-v1", backends=None)
    write_report(s, "run-003", scores=[l1("tom", 0.70)], baseline_ref="zengen-v1", backends="mock")
    assert compute_gain(s, "run-002", "tom").gain == pytest.approx(0.10)
    assert compute_gain(s, "run-003", "tom").gain == pytest.approx(0.20)


def test_the_grade_comes_from_controlplane_evidence(store):
    # 没有 stub 键,但 backends 取值不认识:diagnostic_grade 判 stub,这里不另写一份判断
    write_report(store, "run-002", scores=[l1("tom", 0.6)], baseline_ref="zengen-v1", backends="Real")
    assert compute_gain(store, "run-002", "tom").reason == "diagnostic report for run-002 is a stub"


def test_a_report_without_a_score_list_is_refused():
    assert l1_score({}, "tom") == (None, "report has no dimension_scores list")


def test_baseline_run_of_reads_the_links_the_daemon_writes(store):
    assert baseline_run_of(store, "zengen-v1") == ("run-001", OK)


def test_regression_flags_reads_the_flag_list(store):
    flag = {"dimension": "tom", "kind": "social_regression", "drop": 0.2}
    write_report(store, "run-002", scores=[l1("tom", 0.3)], baseline_ref="zengen-v1", flags=[flag])
    assert regression_flags(read_report_payload(store, "run-002")) == [flag]
    assert regression_flags(read_report_payload(store, "run-001")) == []


def test_a_stub_report_has_no_regression_flags(store):
    write_report(store, "run-002", stub=True)
    assert regression_flags(read_report_payload(store, "run-002")) == []


def test_read_report_payload_is_none_when_the_report_is_absent(store):
    assert read_report_payload(store, "run-404") is None


def test_a_report_written_by_the_diagnostic_package_is_read_correctly(tmp_path):
    # 本包不导入 diagnostic;这条测试用 diagnostic 自己的写法造报告,钉住字段名没有漂移。
    from diagnostic.datamodel import ConsistencySignals, DimensionScore, RegressionFlags
    from diagnostic.report import build_report
    from diagnostic.store_adapter import write_report as diag_write_report

    s = make_store(tmp_path)
    write_model(s, 1, "run-001")

    def _report(run_id, score, baseline_ref, flags):
        return build_report(
            report_id=run_id, candidate_ref=f"cand-{run_id}", baseline_ref=baseline_ref,
            benchmark_version="bench-v1", run_tier="quick", sampling_k=1,
            dimension_scores=[DimensionScore("tom", "l1", score, score - 0.1, score + 0.1, 30, "static")],
            error_profiles=[], regression_flags=RegressionFlags(flags=flags),
            consistency_signals=ConsistencySignals({}, 0.0), run_id=run_id,
        )

    diag_write_report(s, "run-001", _report("run-001", 0.5, "", []))
    diag_write_report(s, "run-002", _report("run-002", 0.75, "zengen-v1", [{"kind": "g_probe_failure"}]))
    assert compute_gain(s, "run-002", "tom") == GainResult(0.25, "run-001", OK)
    assert regression_flags(read_report_payload(s, "run-002")) == [{"kind": "g_probe_failure"}]
```

- [ ] **Step 2: 运行,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_rs_gain.py`
Expected: 收集阶段报错 `E   ModuleNotFoundError: No module named 'recipestore.gain'`。

- [ ] **Step 3: 写实现**

新建 `src/recipestore/gain.py`:

```python
"""增益的唯一实现(闭环集成设计 §11):候选 run 的一级维度分数减去基线 run 的同维度分数。

提名(TrainScheduler)与战绩回写(本包)都调 compute_gain,不得各写一份。
本包不导入 diagnostic:报告按 DiagnosticReport.to_dict() 的键名直接读 payload 字典。
任何一环缺失都返回 gain=None 并在 reason 写明缺什么,绝不编造数字。
证据等级一律用 controlplane.evidence.diagnostic_grade(设计 §11 唯一实现),本包不另判。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from controlplane.evidence import diagnostic_grade
from controlplane.gitstore import GitStore
from controlplane.paths import diagnostic_report_file, model_file

_MODEL_ID = re.compile(r"^zengen-v(\d+)$")

OK = "ok"


@dataclass(frozen=True)
class GainResult:
    gain: float | None
    baseline_run_id: str | None
    reason: str


def read_report_payload(store: GitStore, run_id: str) -> dict | None:
    """该 run 的诊断报告 payload;文件不在返回 None。"""
    path = diagnostic_report_file(run_id)
    if not store.exists(path):
        return None
    return store.read_product(path).payload


def l1_score(payload: dict, dimension: str) -> tuple[float | None, str]:
    """取 level 为 l1 且 dimension 匹配的 DimensionScore.score。返回 (分数, 原因)。"""
    scores = payload.get("dimension_scores")
    if not isinstance(scores, list):
        return None, "report has no dimension_scores list"
    hits = [
        s for s in scores
        if isinstance(s, dict) and s.get("level") == "l1" and s.get("dimension") == dimension
    ]
    if not hits:
        return None, f"report has no l1 score for dimension {dimension!r}"
    if len(hits) > 1:
        return None, f"report has {len(hits)} l1 scores for dimension {dimension!r}; refusing to pick one"
    score = hits[0].get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return None, f"l1 score for dimension {dimension!r} is not a number"
    if not math.isfinite(score):
        # NaN 与任何数比较都为假:gain < min_gain 会把它当成达标
        return None, f"l1 score for dimension {dimension!r} is not a finite number"
    return float(score), OK


def regression_flags(payload: dict) -> list[dict]:
    """报告的退化标记列表(diagnostic.regression 写的 social_regression 与 g_probe_failure)。
    没有该字段(stub 报告)返回空列表。"""
    raw = payload.get("regression_flags")
    if not isinstance(raw, dict):
        return []
    flags = raw.get("flags")
    if not isinstance(flags, list):
        return []
    return [f for f in flags if isinstance(f, dict)]


def baseline_run_of(store: GitStore, baseline_ref: str) -> tuple[str | None, str]:
    """zengen-vN → 产出它的 run id。

    controlplane.daemon 晋级时写 links=[候选 id, run id],候选 id 恒为 cand-<run>;
    所以取 links 里满足"cand-<它> 也在 links 里"的那一项,不靠位置猜。
    注意:计划 8 的 read_tree_state 与诊断评估器对同一问题取 links[1]
    (docs/superpowers/plans/2026-10-09-reconcile-extension.md 第 43、1258、2313 行)。
    两种判法在 promote_candidate 的产物上结果相同;改晋级写法时两处要一起改。"""
    match = _MODEL_ID.match(baseline_ref or "")
    if match is None:
        return None, f"baseline_ref {baseline_ref!r} is not a zengen-vN model id"
    path = model_file(int(match.group(1)))
    if not store.exists(path):
        return None, f"baseline model {baseline_ref} not found"
    links = store.read_product(path).links
    runs = [link for link in links if f"cand-{link}" in links]
    if len(runs) != 1:
        return None, f"baseline model {baseline_ref} links do not name exactly one producing run"
    return runs[0], OK


def compute_gain(store: GitStore, run_id: str, dimension: str) -> GainResult:
    cand = read_report_payload(store, run_id)
    if cand is None:
        return GainResult(None, None, f"no diagnostic report for {run_id}")
    cand_grade = diagnostic_grade(cand)
    if cand_grade == "stub":
        return GainResult(None, None, f"diagnostic report for {run_id} is a stub")
    cand_score, why = l1_score(cand, dimension)
    if cand_score is None:
        return GainResult(None, None, f"{run_id}: {why}")
    baseline_ref = cand.get("baseline_ref")
    if not isinstance(baseline_ref, str) or not baseline_ref:
        return GainResult(None, None, f"diagnostic report for {run_id} has no baseline_ref")
    base_run, why = baseline_run_of(store, baseline_ref)
    if base_run is None:
        return GainResult(None, None, why)
    if base_run == run_id:
        return GainResult(None, base_run, f"baseline {baseline_ref} was produced by {run_id} itself")
    base = read_report_payload(store, base_run)
    if base is None:
        return GainResult(None, base_run, f"no diagnostic report for baseline run {base_run}")
    base_grade = diagnostic_grade(base)
    if base_grade == "stub":
        return GainResult(None, base_run, f"diagnostic report for baseline run {base_run} is a stub")
    if cand_grade == "real" and base_grade != "real":
        # real 候选减 mock 基线:增益一半来自 mock 分数,不能让它按 real 证据驱动提名(R17)
        return GainResult(
            None, base_run,
            f"baseline run {base_run} evidence is {base_grade} while {run_id} is real; scores are not comparable",
        )
    if cand.get("benchmark_version") != base.get("benchmark_version"):
        return GainResult(
            None, base_run,
            f"benchmark versions differ ({cand.get('benchmark_version')!r} vs "
            f"{base.get('benchmark_version')!r}); scores are not comparable",
        )
    base_score, why = l1_score(base, dimension)
    if base_score is None:
        return GainResult(None, base_run, f"baseline run {base_run}: {why}")
    return GainResult(cand_score - base_score, base_run, OK)
```

- [ ] **Step 4: 运行,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_rs_gain.py`
Expected: `30 passed`。

- [ ] **Step 5: 全量回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: **B+101** 通过、无警告(修订时实测 1003)。

- [ ] **Step 6: 提交**

```bash
git add src/recipestore/gain.py
git add tests/test_rs_gain.py
git commit -m "feat: recipestore compute_gain (l1 score minus the baseline run's, None with a reason when any link is missing)"
```

---

### Task 43: 检索与比较

**Files:**
- Create: `src/recipestore/library.py`
- Test: `tests/rs_recipes.py`(直接放配方与战绩)、`tests/test_rs_library.py`

**Interfaces:**
- Consumes: 任务 40 的 `run_id_of`、`RECIPE_PREFIX`;`controlplane.paths.recipe_file`、`recipe_track_file`、`CONTROL_PLANE_ROOT`。
- Produces(计划 4、5 的规划上下文与领域校验调它):
  - `RecipeView(id, recipe_type, dimension, status, payload, mean_gain, n_reuse)`,冻结。
  - `find_recipes(store, *, dimension=None, recipe_type=None, states=("effective",), order="track") -> list[RecipeView]`;`order` 取 `"track"` 或 `"id"`。
  - `get_recipe(store, recipe_id) -> Envelope`;不在或 id 形状不对抛 `RecipeNotFoundError`(`LookupError` 子类)。
  - `compare(store, a, b) -> dict`:`{"a", "b", "config_diff": {路径: {"a", "b"}}, "track": {id: {"status", "mean_gain", "n_reuse"}}}`。
  - `read_track_entries(store, recipe_id) -> list[dict]`、`track_summary(entries) -> (mean_gain, n_reuse)`、`view_of(store, env) -> RecipeView`。

**设计要点:**
- **`mean_gain` 只统计 `gain` 非空的条目**;没有一条就是 `None`。`n_reuse` 是条目总数,含 `gain` 为空的条目。布尔值不算数字。
- **排序**:`mean_gain` 降序(`None` 排最后,Ruling 21)→ `n_reuse` 降序 → id 升序。
- **不建索引(R9)**:扫 `recipes/` 下名字以 `recipe-` 开头、且 `recipe_file(run_id_of(名字))` 存在的子目录;杂项目录忽略。
- **`compare` 只比配置**(Ruling 22)。

- [ ] **Step 1: 写测试用的放置函数**

新建 `tests/rs_recipes.py`:

```python
"""直接往 recipes/ 里放配方与战绩,供检索、战绩与战绩处理器的测试用(绕开收录处理器)。"""
from __future__ import annotations

from controlplane.gitstore import GitStore
from controlplane.paths import recipe_file, recipe_track_file
from rs_fixtures import batch_payload, envelope


def put_recipe(
    store: GitStore,
    run_id: str,
    *,
    status: str = "effective",
    dimension: str = "tom",
    payload: dict | None = None,
    entries: list[dict] | None = None,
) -> str:
    body = dict(batch_payload() if payload is None else payload)
    body["dimension"] = dimension
    body["source_run_id"] = run_id
    recipe_id = f"recipe-{run_id}"
    store.write_product(recipe_file(run_id), envelope(recipe_id, "recipe", status, body, [recipe_id, run_id]), "t")
    if entries is not None:
        store.write_product(
            recipe_track_file(run_id),
            envelope(f"track-{recipe_id}", "recipe_track_record", "executed",
                     {"recipe_id": recipe_id, "entries": entries}, [recipe_id]),
            "t",
        )
    return recipe_id


def entry(run_id: str, gain, *, regressed: bool = False, promoted: bool = True, used_as: str = "clone") -> dict:
    return {"run_id": run_id, "gain": gain, "promoted": promoted, "regressed": regressed, "used_as": used_as}
```

- [ ] **Step 2: 写失败测试**

新建 `tests/test_rs_library.py`:

```python
import pytest

from recipestore.library import (
    RecipeNotFoundError,
    RecipeView,
    compare,
    find_recipes,
    get_recipe,
    read_track_entries,
    track_summary,
)
from rs_fixtures import agent_rsi_payload, interact_payload, make_store
from rs_recipes import entry, put_recipe


@pytest.fixture
def store(tmp_path):
    return make_store(tmp_path)


def test_get_recipe_reads_the_envelope(store):
    put_recipe(store, "run-003")
    env = get_recipe(store, "recipe-run-003")
    assert (env.id, env.type, env.status) == ("recipe-run-003", "recipe", "effective")


@pytest.mark.parametrize("bad", ["recipe-run-404", "run-003", ""])
def test_get_recipe_raises_for_a_missing_or_malformed_id(store, bad):
    put_recipe(store, "run-003")
    with pytest.raises(RecipeNotFoundError):
        get_recipe(store, bad)


def test_mean_gain_counts_only_entries_with_a_gain():
    entries = [entry("r1", 0.2), entry("r2", None), entry("r3", 0.4)]
    assert track_summary(entries) == (pytest.approx(0.3), 3)


def test_mean_gain_is_none_without_any_gain():
    assert track_summary([entry("r1", None)]) == (None, 1)
    assert track_summary([]) == (None, 0)


def test_a_boolean_gain_is_not_a_number():
    assert track_summary([entry("r1", True)]) == (None, 1)


def test_find_recipes_returns_views_with_the_track_summary(store):
    put_recipe(store, "run-003", entries=[entry("run-004", 0.1), entry("run-005", None)])
    [view] = find_recipes(store)
    assert isinstance(view, RecipeView)
    assert (view.id, view.recipe_type, view.dimension, view.status) == ("recipe-run-003", "batch", "tom", "effective")
    assert (view.mean_gain, view.n_reuse) == (pytest.approx(0.1), 2)
    assert view.payload["source_run_id"] == "run-003"


def test_a_recipe_without_a_track_file_has_no_reuse(store):
    put_recipe(store, "run-003")
    [view] = find_recipes(store)
    assert (view.mean_gain, view.n_reuse) == (None, 0)
    assert read_track_entries(store, "recipe-run-003") == []


def test_track_order_is_mean_gain_then_reuse_then_id(store):
    put_recipe(store, "run-001", entries=[entry("x1", 0.1)])
    put_recipe(store, "run-002", entries=[entry("x2", 0.3)])
    put_recipe(store, "run-003", entries=[entry("x3", 0.1), entry("x4", None)])  # 同 0.1,复用更多
    put_recipe(store, "run-004", entries=[])  # 没有增益数据,排最后
    put_recipe(store, "run-000", entries=[entry("x5", 0.1)])  # 同 0.1 同次数,按 id
    assert [v.id for v in find_recipes(store)] == [
        "recipe-run-002", "recipe-run-003", "recipe-run-000", "recipe-run-001", "recipe-run-004",
    ]


def test_a_negative_mean_gain_still_ranks_above_no_data(store):
    put_recipe(store, "run-001", entries=[entry("x1", -0.2)])
    put_recipe(store, "run-002", entries=[entry("x2", None)])
    assert [v.id for v in find_recipes(store)] == ["recipe-run-001", "recipe-run-002"]


def test_id_order(store):
    put_recipe(store, "run-002", entries=[entry("x", 0.9)])
    put_recipe(store, "run-001")
    assert [v.id for v in find_recipes(store, order="id")] == ["recipe-run-001", "recipe-run-002"]


def test_an_unknown_order_is_refused(store):
    with pytest.raises(ValueError, match="order must be one of"):
        find_recipes(store, order="gain")


def test_filters(store):
    put_recipe(store, "run-001", dimension="tom")
    put_recipe(store, "run-002", dimension="emotion")
    put_recipe(store, "run-003", dimension="tom", payload=interact_payload())
    put_recipe(store, "run-004", dimension="tom", status="deprecated")
    assert [v.id for v in find_recipes(store, dimension="tom", order="id")] == ["recipe-run-001", "recipe-run-003"]
    assert [v.id for v in find_recipes(store, recipe_type="interact")] == ["recipe-run-003"]
    assert [v.id for v in find_recipes(store, states=("deprecated",))] == ["recipe-run-004"]
    assert len(find_recipes(store, states=("effective", "deprecated"))) == 4


def test_an_empty_store_has_no_recipes(store):
    assert find_recipes(store) == []


def test_stray_directories_are_ignored(store):
    put_recipe(store, "run-001")
    (store.repo_root / "control-plane" / "recipes" / "notes").mkdir()
    (store.repo_root / "control-plane" / "recipes" / "recipe-run-009").mkdir()  # 没有 recipe.yaml
    assert [v.id for v in find_recipes(store)] == ["recipe-run-001"]


def test_compare_lists_config_differences_and_the_track(store):
    put_recipe(store, "run-001", entries=[entry("x1", 0.2)])
    other = interact_payload()
    put_recipe(store, "run-002", payload=other, entries=[entry("x2", None), entry("x3", None)])
    out = compare(store, "recipe-run-001", "recipe-run-002")
    assert (out["a"], out["b"]) == ("recipe-run-001", "recipe-run-002")
    diff = out["config_diff"]
    assert diff["recipe_type"] == {"a": "batch", "b": "interact"}
    assert diff["training_config.mode"] == {"a": "batch", "b": "interact"}
    assert diff["scenario_ids"] == {"a": "<missing>", "b": ["scen-1"]}
    assert diff["stage_manifests.sft.id"] == {"a": "dsm-run-003-sft", "b": "<missing>"}
    assert "source_run_id" not in diff and not any(k.startswith(("result", "repro")) for k in diff)
    assert out["track"] == {
        "recipe-run-001": {"status": "effective", "mean_gain": pytest.approx(0.2), "n_reuse": 1},
        "recipe-run-002": {"status": "effective", "mean_gain": None, "n_reuse": 2},
    }


def test_two_identical_configs_have_no_difference(store):
    put_recipe(store, "run-001")
    put_recipe(store, "run-002")
    assert compare(store, "recipe-run-001", "recipe-run-002")["config_diff"] == {}


def test_compare_raises_for_a_missing_recipe(store):
    put_recipe(store, "run-001")
    with pytest.raises(RecipeNotFoundError):
        compare(store, "recipe-run-001", "recipe-run-404")


def test_an_agent_rsi_recipe_is_found_and_ranked_like_any_other(store):
    # 已知限制 12:agent_rsi 配方不自动提名,但检索照常工作
    put_recipe(store, "run-001", entries=[entry("x1", 0.1)])
    put_recipe(store, "run-002", payload=agent_rsi_payload(), entries=[entry("x2", 0.3), entry("x3", None)])
    [view] = find_recipes(store, recipe_type="agent_rsi")
    assert (view.id, view.recipe_type, view.mean_gain, view.n_reuse) == (
        "recipe-run-002", "agent_rsi", pytest.approx(0.3), 2)
    assert view.payload["bound_model_id"] == "zengen-v2"
    assert [v.id for v in find_recipes(store)] == ["recipe-run-002", "recipe-run-001"]
```

- [ ] **Step 3: 运行,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_rs_library.py`
Expected: 收集阶段报错 `E   ModuleNotFoundError: No module named 'recipestore.library'`。

- [ ] **Step 4: 写实现**

新建 `src/recipestore/library.py`:

```python
"""配方检索(设计 §6.2)。不建索引(R9):每次调用直接扫描 recipes/ 目录。"""
from __future__ import annotations

from dataclasses import dataclass

from controlplane.envelope import Envelope
from controlplane.gitstore import GitStore
from controlplane.paths import CONTROL_PLANE_ROOT, recipe_file, recipe_track_file
from recipestore.ids import RECIPE_PREFIX, run_id_of

ORDERS = ("track", "id")

# compare() 只比"配置":这三个键是结果、校验输出与来源,不是配置,不算配置差异(Ruling 22)。
# stage_manifests / stage_manifest_refs 是训练数据的锚,属于配置,照常比较。
NON_CONFIG_KEYS = frozenset({"result", "repro", "source_run_id"})


class RecipeNotFoundError(LookupError):
    """点名的配方不在 recipes/ 里。"""


@dataclass(frozen=True)
class RecipeView:
    id: str
    recipe_type: str
    dimension: str
    status: str
    payload: dict
    mean_gain: float | None
    n_reuse: int


def get_recipe(store: GitStore, recipe_id: str) -> Envelope:
    try:
        run_id = run_id_of(recipe_id)
    except ValueError as exc:
        raise RecipeNotFoundError(str(exc)) from exc
    path = recipe_file(run_id)
    if not store.exists(path):
        raise RecipeNotFoundError(f"recipe {recipe_id} not found")
    return store.read_product(path)


def read_track_entries(store: GitStore, recipe_id: str) -> list[dict]:
    """配方的战绩条目,按追加顺序。战绩文件不在(收录崩在中途)视为空。"""
    path = recipe_track_file(run_id_of(recipe_id))
    if not store.exists(path):
        return []
    return [dict(e) for e in store.read_product(path).payload.get("entries", [])]


def track_summary(entries: list[dict]) -> tuple[float | None, int]:
    """(mean_gain, n_reuse)。mean_gain 只统计 gain 非空的条目;一条都没有就是 None。
    n_reuse 是条目总数,含 gain 为空的条目。"""
    gains = [
        float(e["gain"]) for e in entries
        if isinstance(e.get("gain"), (int, float)) and not isinstance(e.get("gain"), bool)
    ]
    mean = sum(gains) / len(gains) if gains else None
    return mean, len(entries)


def _recipe_ids(store: GitStore) -> list[str]:
    root = store.repo_root / CONTROL_PLANE_ROOT / "recipes"
    if not root.is_dir():
        return []
    ids = []
    for p in root.iterdir():
        if not p.is_dir() or not p.name.startswith(RECIPE_PREFIX) or p.name == RECIPE_PREFIX:
            continue
        # 是否是配方以路径函数为准,不手拼文件名
        if store.exists(recipe_file(run_id_of(p.name))):
            ids.append(p.name)
    return sorted(ids)


def view_of(store: GitStore, env: Envelope) -> RecipeView:
    mean, n = track_summary(read_track_entries(store, env.id))
    return RecipeView(
        id=env.id,
        recipe_type=env.payload.get("recipe_type", ""),
        dimension=env.payload.get("dimension", ""),
        status=env.status,
        payload=env.payload,
        mean_gain=mean,
        n_reuse=n,
    )


def _track_key(v: RecipeView):
    # mean_gain 降序,没有增益数据的排最后;再 n_reuse 降序;再 id 升序
    return (v.mean_gain is None, -(v.mean_gain or 0.0), -v.n_reuse, v.id)


def find_recipes(
    store: GitStore,
    *,
    dimension: str | None = None,
    recipe_type: str | None = None,
    states: tuple[str, ...] = ("effective",),
    order: str = "track",
) -> list[RecipeView]:
    if order not in ORDERS:
        raise ValueError(f"order must be one of {list(ORDERS)}, got {order!r}")
    views: list[RecipeView] = []
    for recipe_id in _recipe_ids(store):
        env = get_recipe(store, recipe_id)
        if env.status not in states:
            continue
        if dimension is not None and env.payload.get("dimension") != dimension:
            continue
        if recipe_type is not None and env.payload.get("recipe_type") != recipe_type:
            continue
        views.append(view_of(store, env))
    if order == "track":
        views.sort(key=_track_key)
    else:
        views.sort(key=lambda v: v.id)
    return views


def _flatten(obj: object, prefix: str, out: dict) -> None:
    if isinstance(obj, dict) and obj:
        for k in obj:
            _flatten(obj[k], f"{prefix}.{k}" if prefix else str(k), out)
    else:
        out[prefix] = obj


_MISSING = "<missing>"


def compare(store: GitStore, a: str, b: str) -> dict:
    """配置差异 + 战绩对比。差异按点分路径列出,列表整体比较;一侧没有该键记为 "<missing>"。"""
    ea, eb = get_recipe(store, a), get_recipe(store, b)
    fa: dict = {}
    fb: dict = {}
    _flatten({k: v for k, v in ea.payload.items() if k not in NON_CONFIG_KEYS}, "", fa)
    _flatten({k: v for k, v in eb.payload.items() if k not in NON_CONFIG_KEYS}, "", fb)
    diff = {
        path: {"a": fa.get(path, _MISSING), "b": fb.get(path, _MISSING)}
        for path in sorted(set(fa) | set(fb))
        if fa.get(path, _MISSING) != fb.get(path, _MISSING)
    }
    va, vb = view_of(store, ea), view_of(store, eb)
    return {
        "a": a,
        "b": b,
        "config_diff": diff,
        "track": {
            a: {"status": va.status, "mean_gain": va.mean_gain, "n_reuse": va.n_reuse},
            b: {"status": vb.status, "mean_gain": vb.mean_gain, "n_reuse": vb.n_reuse},
        },
    }
```

- [ ] **Step 5: 运行,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_rs_library.py`
Expected: `20 passed`。

- [ ] **Step 6: 全量回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: **B+121** 通过、无警告(修订时实测 1023)。

- [ ] **Step 7: 提交**

```bash
git add src/recipestore/library.py
git add tests/rs_recipes.py
git add tests/test_rs_library.py
git commit -m "feat: recipestore lookup (directory scan, track ordering by mean gain over non-null entries, config compare)"
```

---

### Task 44: 战绩与生命周期

**Files:**
- Create: `src/recipestore/track.py`
- Test: `tests/test_rs_track.py`

**Interfaces:**
- Consumes: 任务 43 的 `get_recipe`、`RecipeNotFoundError`;`controlplane.states.REVIEW`、`assert_transition`;`controlplane.paths.recipe_file`、`recipe_lineage_file`、`recipe_track_file`。
- Produces:
  - `append_track_entry(store, recipe_id, entry) -> None`:条目恰好是 `{run_id, gain, promoted, regressed, used_as}`,`used_as ∈ USED_AS = ("clone", "replay")`;按 `run_id` 幂等,先写者为准(Ruling 23);格式不对抛 `ValueError`,配方不在抛 `RecipeNotFoundError`。
  - `verdict(entries) -> str`:`KEEP = "keep"` 或 `DEPRECATE = "deprecate"`。
  - `deprecate(store, recipe_id, *, reason, run_id) -> bool`:做了返回 `True`,已是 `deprecated` 返回 `False`;非 `effective` 的其他状态抛 `InvalidTransitionError`。
  - `deprecated_by(store, recipe_id) -> str | None`:lineage 里记录的触发 run。
  - `track_record_envelope(recipe_id, entries)`、`lineage_envelope(recipe_id, payload)`、`NOW`(任务 45、46 用)。

**设计要点:**
- **verdict(R8)**:只看最近一条,`regressed` 为真就 `deprecate`;早先的退化之后又有一条干净的,保留。
- **deprecate 两步、先 lineage 后状态**(Ruling 17),同一配方只记一条 `deprecated` 事件;已有 lineage(收录时写的)就在其 `events` 后面追加,其他键原样保留。
- **`superseded` 不自动产生**,本模块没有任何代码写它。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_rs_track.py`:

```python
import pytest

from controlplane.paths import recipe_lineage_file, recipe_track_file
from controlplane.states import InvalidTransitionError
from recipestore.library import RecipeNotFoundError, find_recipes, get_recipe, read_track_entries
from recipestore.track import (
    DEPRECATE,
    KEEP,
    append_track_entry,
    deprecate,
    deprecated_by,
    lineage_envelope,
    verdict,
)
from rs_fixtures import agent_rsi_payload, make_store
from rs_recipes import entry, put_recipe

R = "recipe-run-003"


@pytest.fixture
def store(tmp_path):
    s = make_store(tmp_path)
    put_recipe(s, "run-003", entries=[])
    return s


def _commits(store):
    return int(store._git("rev-list", "--count", "HEAD").strip())


def test_append_adds_an_entry(store):
    assert append_track_entry(store, R, entry("run-004", 0.1)) is None
    assert read_track_entries(store, R) == [entry("run-004", 0.1)]


def test_append_is_idempotent_by_run_id_and_the_first_write_wins(store):
    append_track_entry(store, R, entry("run-004", 0.1))
    before = _commits(store)
    append_track_entry(store, R, entry("run-004", 0.9, regressed=True))
    assert read_track_entries(store, R) == [entry("run-004", 0.1)]
    assert _commits(store) == before


def test_append_keeps_order(store):
    for i, g in enumerate([0.1, None, -0.2]):
        append_track_entry(store, R, entry(f"run-01{i}", g))
    assert [e["run_id"] for e in read_track_entries(store, R)] == ["run-010", "run-011", "run-012"]


def test_append_creates_the_track_file_when_it_is_missing(tmp_path):
    s = make_store(tmp_path)
    put_recipe(s, "run-003")  # 没有 track_record
    append_track_entry(s, R, entry("run-004", None))
    assert s.exists(recipe_track_file("run-003"))
    assert read_track_entries(s, R) == [entry("run-004", None)]


def test_append_to_a_missing_recipe_raises(store):
    with pytest.raises(RecipeNotFoundError):
        append_track_entry(store, "recipe-run-404", entry("run-004", 0.1))


@pytest.mark.parametrize("bad", [
    {"run_id": "run-004"},
    dict(entry("run-004", 0.1), extra=1),
    entry("", 0.1),
    entry("run-004", "0.1"),
    entry("run-004", True),
    dict(entry("run-004", 0.1), promoted="yes"),
    dict(entry("run-004", 0.1), regressed=None),
    entry("run-004", 0.1, used_as="new"),
    "run-004",
])
def test_a_malformed_entry_is_refused(store, bad):
    with pytest.raises(ValueError):
        append_track_entry(store, R, bad)
    assert read_track_entries(store, R) == []


def test_verdict_keeps_when_the_latest_entry_did_not_regress():
    assert verdict([]) == KEEP
    assert verdict([entry("a", 0.1)]) == KEEP
    assert verdict([entry("a", 0.1, regressed=True), entry("b", 0.0)]) == KEEP


def test_verdict_deprecates_when_the_latest_entry_regressed():
    assert verdict([entry("a", 0.1), entry("b", -0.1, regressed=True)]) == DEPRECATE
    assert verdict([entry("a", None, regressed=True)]) == DEPRECATE


def test_deprecate_moves_the_recipe_and_records_why_in_the_lineage(store):
    assert deprecate(store, R, reason="regressed on run-004", run_id="run-004") is True
    assert get_recipe(store, R).status == "deprecated"
    lineage = store.read_product(recipe_lineage_file("run-003"))
    assert (lineage.type, lineage.status) == ("recipe_lineage", "executed")
    assert lineage.payload["events"] == [{"event": "deprecated", "run_id": "run-004", "reason": "regressed on run-004"}]
    assert find_recipes(store) == []
    assert [v.id for v in find_recipes(store, states=("deprecated",))] == [R]


def test_deprecate_appends_to_an_existing_lineage(store):
    admitted = {"recipe_id": R, "source_run_id": "run-003", "admitted_from": R,
                "events": [{"event": "admitted", "run_id": "run-003"}]}
    store.write_product(recipe_lineage_file("run-003"), lineage_envelope(R, admitted), "t")
    deprecate(store, R, reason="r", run_id="run-004")
    payload = store.read_product(recipe_lineage_file("run-003")).payload
    assert payload["admitted_from"] == R
    assert [e["event"] for e in payload["events"]] == ["admitted", "deprecated"]


def test_deprecating_twice_changes_nothing(store):
    deprecate(store, R, reason="r", run_id="run-004")
    before = _commits(store)
    assert deprecate(store, R, reason="other", run_id="run-005") is False
    assert _commits(store) == before
    events = store.read_product(recipe_lineage_file("run-003")).payload["events"]
    assert events == [{"event": "deprecated", "run_id": "run-004", "reason": "r"}]


def test_a_crash_between_lineage_and_status_converges_on_rerun(store, monkeypatch):
    real = store.write_product
    calls = []

    def flaky(rel, env, msg):
        calls.append(rel)
        if len(calls) == 2:
            raise OSError("disk full")
        real(rel, env, msg)

    monkeypatch.setattr(store, "write_product", flaky)
    with pytest.raises(OSError):
        deprecate(store, R, reason="r", run_id="run-004")
    monkeypatch.setattr(store, "write_product", real)
    assert get_recipe(store, R).status == "effective"  # 只写了 lineage
    assert deprecate(store, R, reason="r", run_id="run-004") is True
    assert get_recipe(store, R).status == "deprecated"
    assert len(store.read_product(recipe_lineage_file("run-003")).payload["events"]) == 1


def test_only_an_effective_recipe_can_be_deprecated(tmp_path):
    s = make_store(tmp_path)
    put_recipe(s, "run-003", status="superseded")
    with pytest.raises(InvalidTransitionError):
        deprecate(s, R, reason="r", run_id="run-004")


def test_deprecate_needs_a_reason(store):
    with pytest.raises(ValueError):
        deprecate(store, R, reason="", run_id="run-004")


def test_deprecated_by_reads_the_triggering_run_from_the_lineage(store):
    assert deprecated_by(store, R) is None
    deprecate(store, R, reason="r", run_id="run-004")
    assert deprecated_by(store, R) == "run-004"


def test_an_agent_rsi_recipe_takes_entries_and_a_verdict_like_any_other(tmp_path):
    # 已知限制 12:append_track_entry、verdict、deprecate 不看 recipe_type
    s = make_store(tmp_path)
    rid = put_recipe(s, "run-007", payload=agent_rsi_payload(), entries=[])
    append_track_entry(s, rid, entry("run-008", 0.2))
    append_track_entry(s, rid, entry("run-009", None, regressed=True))
    entries = read_track_entries(s, rid)
    assert [e["run_id"] for e in entries] == ["run-008", "run-009"]
    assert verdict(entries) == DEPRECATE
    assert deprecate(s, rid, reason="regressed", run_id="run-009") is True
    assert get_recipe(s, rid).payload["recipe_type"] == "agent_rsi"
    assert [v.id for v in find_recipes(s, states=("deprecated",), recipe_type="agent_rsi")] == [rid]
```

- [ ] **Step 2: 运行,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_rs_track.py`
Expected: 收集阶段报错 `E   ModuleNotFoundError: No module named 'recipestore.track'`。

- [ ] **Step 3: 写实现**

新建 `src/recipestore/track.py`:

```python
"""战绩与生命周期(设计 §6.2,R8)。

- 战绩条目 {run_id, gain, promoted, regressed, used_as},按 run_id 幂等追加。
- verdict:最近一条战绩 regressed 为真 → "deprecate",否则 "keep"。
- deprecate:effective → deprecated(走 controlplane.states.assert_transition),
  先在 lineage 里记原因与触发的 run,再改配方状态,所以崩在两步之间重跑能收敛。
- superseded 不自动产生(R8)。
"""
from __future__ import annotations

from controlplane.envelope import CURRENT_SCHEMA_VERSION, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import recipe_file, recipe_lineage_file, recipe_track_file
from controlplane.states import REVIEW, assert_transition
from recipestore.ids import run_id_of
from recipestore.library import RecipeNotFoundError, get_recipe

NOW = "2026-10-09T00:00:00Z"

USED_AS = ("clone", "replay")
KEEP = "keep"
DEPRECATE = "deprecate"


def _env(id_: str, type_: str, status: str, links: list[str], payload: dict) -> Envelope:
    return Envelope(
        id=id_, type=type_, version=1, created_at=NOW, created_by=OwnerRef(),
        status=status, schema_version=CURRENT_SCHEMA_VERSION, links=links, payload=payload,
    )


def track_record_envelope(recipe_id: str, entries: list[dict]) -> Envelope:
    return _env(f"track-{recipe_id}", "recipe_track_record", "executed", [recipe_id],
                {"recipe_id": recipe_id, "entries": [dict(e) for e in entries]})


def lineage_envelope(recipe_id: str, payload: dict) -> Envelope:
    return _env(f"lineage-{recipe_id}", "recipe_lineage", "executed", [recipe_id], payload)


def validate_entry(entry: dict) -> None:
    if not isinstance(entry, dict):
        raise ValueError("track entry must be a mapping")
    expected = {"run_id", "gain", "promoted", "regressed", "used_as"}
    if set(entry) != expected:
        raise ValueError(f"track entry keys must be exactly {sorted(expected)}, got {sorted(entry)}")
    if not isinstance(entry["run_id"], str) or not entry["run_id"]:
        raise ValueError("track entry run_id must be a non-empty string")
    gain = entry["gain"]
    if gain is not None and (isinstance(gain, bool) or not isinstance(gain, (int, float))):
        raise ValueError("track entry gain must be a number or None")
    for key in ("promoted", "regressed"):
        if not isinstance(entry[key], bool):
            raise ValueError(f"track entry {key} must be a boolean")
    if entry["used_as"] not in USED_AS:
        raise ValueError(f"track entry used_as must be one of {list(USED_AS)}")


def append_track_entry(store: GitStore, recipe_id: str, entry: dict) -> None:
    """追加一条战绩。同一 run_id 已有条目时什么都不写(先写者为准)。"""
    validate_entry(entry)
    get_recipe(store, recipe_id)  # 配方不在抛 RecipeNotFoundError
    path = recipe_track_file(run_id_of(recipe_id))
    entries = list(store.read_product(path).payload.get("entries", [])) if store.exists(path) else []
    if any(e.get("run_id") == entry["run_id"] for e in entries):
        return
    entries.append(dict(entry))
    store.write_product(path, track_record_envelope(recipe_id, entries),
                        f"recipestore: track {recipe_id} <- {entry['run_id']}")


def verdict(entries: list[dict]) -> str:
    if entries and entries[-1].get("regressed") is True:
        return DEPRECATE
    return KEEP


def deprecate(store: GitStore, recipe_id: str, *, reason: str, run_id: str) -> bool:
    """effective → deprecated。已经 deprecated 返回 False 且不写任何东西;做了返回 True。"""
    if not reason:
        raise ValueError("deprecate needs a reason")
    recipe = get_recipe(store, recipe_id)
    if recipe.status == "deprecated":
        return False
    assert_transition(REVIEW, recipe.status, "deprecated")
    source_run = run_id_of(recipe_id)
    lpath = recipe_lineage_file(source_run)
    if store.exists(lpath):
        lineage = store.read_product(lpath).payload
    else:
        lineage = {"recipe_id": recipe_id, "source_run_id": source_run, "events": []}
    events = list(lineage.get("events", []))
    if not any(e.get("event") == "deprecated" for e in events):
        events.append({"event": "deprecated", "run_id": run_id, "reason": reason})
        lineage = dict(lineage, events=events)
        store.write_product(lpath, lineage_envelope(recipe_id, lineage),
                            f"recipestore: lineage {recipe_id} deprecated by {run_id}")
    recipe.status = "deprecated"
    store.write_product(recipe_file(source_run), recipe, f"recipestore: {recipe_id} deprecated")
    return True


def deprecated_by(store: GitStore, recipe_id: str) -> str | None:
    """lineage 里记下的、触发 deprecate 的 run id;没有这条事件返回 None。"""
    lpath = recipe_lineage_file(run_id_of(recipe_id))
    if not store.exists(lpath):
        return None
    for event in store.read_product(lpath).payload.get("events", []):
        if event.get("event") == "deprecated":
            return event.get("run_id")
    return None


__all__ = [
    "DEPRECATE", "KEEP", "USED_AS", "RecipeNotFoundError", "append_track_entry", "deprecate", "deprecated_by",
    "lineage_envelope", "track_record_envelope", "validate_entry", "verdict",
]
```

- [ ] **Step 4: 运行,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_rs_track.py`
Expected: `24 passed`。

- [ ] **Step 5: 全量回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: **B+145** 通过、无警告(修订时实测 1047)。

- [ ] **Step 6: 提交**

```bash
git add src/recipestore/track.py
git add tests/test_rs_track.py
git commit -m "feat: recipestore track record and lifecycle (idempotent by run id, latest regression deprecates, lineage records why)"
```

---

### Task 45: 收录处理器 `handle_admit_recipe`

**Files:**
- Create: `src/recipestore/admit.py`
- Test: `tests/test_rs_admit.py`

**Interfaces:**
- Consumes: 任务 40 的 `run_id_of`;任务 44 的 `NOW`、`lineage_envelope`、`track_record_envelope`;`controlplane.ledger.record_failure`(计划 1);`controlplane.paths.recipe_candidate_file`、`recipe_file`、`recipe_lineage_file`、`recipe_track_file`、`failure_record_file`、`run_dir`;`controlplane.reconcile.Action`;`controlplane.states.REVIEW`、`assert_transition`。
- Produces(计划 9 的组合根注册到 `handlers['admit_recipe']`,键名由计划 8 定义):
  - `handle_admit_recipe(store, action) -> None`,签名即 `Handler`;`action.target_id` 是配方候选 id。
  - `has_repro_evidence(payload) -> bool`:审核门"证据在场"规则的 Python 版,计划 8 的命令行规则须与它一致。
  - `TASK_ID = "admit_recipe"`、`FAILURE_POINT = "recipestore.admit"`、`recipe_envelope(candidate) -> Envelope`。处理器对确定性的坏动作不抛异常(Ruling 20)。

**设计要点:**
- **四步,每步重入安全(Ruling 16):**

| 步 | 写什么 | 跳过条件 |
|---|---|---|
| 1 | `recipes/recipe-<run>/recipe.yaml`,类型 `recipe`,状态 `effective`,载荷与 `links` 照抄候选 | 文件已在 |
| 2 | `lineage.yaml`,`events: [{event: admitted, run_id}]` | 文件已在 |
| 3 | `track_record.yaml`,`entries: []` | 文件已在 |
| 4 | 候选 `approved → effective`(`assert_transition`) | 候选已是 `effective`(处理器入口直接返回) |

- **候选不是 `approved` 就什么都不做**(`pending_review`、`rejected`、`draft` 都是过期的动作)。
- **坏动作是空操作(Ruling 20)**:`target_id` 形状不对或 run id 不是单个路径段,什么都不做;候选不在,写一次 T2 失败记录。处理器不抛 `RecipeAdmissionError` 之类的异常:计划 8 调处理器时不接异常,抛了守护进程就会在同一动作上反复退出。
- **证据在场兜底(Ruling 18)**:`approved` 但 `repro` 不合法,不收录,写一次失败记录。
- **失败记录不新建 run 目录**:`runs/<run>/` 不在时两种失败记录都不写(理由见 Ruling 15)。
- **已存在的配方不覆盖**:有测试先 deprecate 已收录的配方、把候选退回 `approved`,再跑一次,配方仍是 `deprecated`。
- **故障注入**:`monkeypatch` 替换 `store.write_product`,在第 1、2、3、4 次写时抛 `OSError`;异常原样上抛(瞬态),重跑后的四个文件与一次跑通的树逐项相等。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_rs_admit.py`:

```python
import pytest

from controlplane.paths import (
    CONTROL_PLANE_ROOT,
    failure_record_file,
    recipe_candidate_file,
    recipe_file,
    recipe_lineage_file,
    recipe_track_file,
    run_dir,
)
from controlplane.reconcile import Action
from recipestore.admit import handle_admit_recipe, has_repro_evidence
from recipestore.library import find_recipes, get_recipe, read_track_entries
from rs_fixtures import batch_payload, make_store, write_directive, write_recipe_candidate

R = "recipe-run-003"
ACTION = Action(kind="admit_recipe", target_id=R, detail={})
WRITTEN = [recipe_file("run-003"), recipe_lineage_file("run-003"), recipe_track_file("run-003"),
           recipe_candidate_file("run-003")]


def _tree(store):
    return {p: store.read_product(p).to_dict() for p in WRITTEN if store.exists(p)}


def _commits(store):
    return int(store._git("rev-list", "--count", "HEAD").strip())


@pytest.fixture
def store(tmp_path):
    s = make_store(tmp_path)
    write_recipe_candidate(s, "run-003", "approved")
    return s


def test_admitting_writes_the_recipe_lineage_and_an_empty_track_record(store):
    handle_admit_recipe(store, ACTION)
    recipe = get_recipe(store, R)
    cand = store.read_product(recipe_candidate_file("run-003"))
    assert (recipe.type, recipe.status) == ("recipe", "effective")
    assert recipe.payload == cand.payload  # 候选与配方共用同一形状
    assert recipe.links == cand.links
    lineage = store.read_product(recipe_lineage_file("run-003"))
    assert lineage.payload == {
        "recipe_id": R, "source_run_id": "run-003", "admitted_from": R,
        "events": [{"event": "admitted", "run_id": "run-003"}],
    }
    assert read_track_entries(store, R) == []
    assert cand.status == "effective"
    assert [v.id for v in find_recipes(store)] == [R]


def test_admitting_twice_changes_nothing(store):
    handle_admit_recipe(store, ACTION)
    before, commits = _tree(store), _commits(store)
    handle_admit_recipe(store, ACTION)
    assert _tree(store) == before and _commits(store) == commits


@pytest.mark.parametrize("crash_at", [1, 2, 3, 4])
def test_a_crash_at_any_step_converges_on_rerun(tmp_path, monkeypatch, crash_at):
    clean = make_store(tmp_path / "clean")
    write_recipe_candidate(clean, "run-003", "approved")
    handle_admit_recipe(clean, ACTION)

    s = make_store(tmp_path / "crashy")
    write_recipe_candidate(s, "run-003", "approved")
    real = s.write_product
    calls = []

    def flaky(rel, env, msg):
        calls.append(rel)
        if len(calls) == crash_at:
            raise OSError("simulated crash")
        real(rel, env, msg)

    monkeypatch.setattr(s, "write_product", flaky)
    with pytest.raises(OSError):
        handle_admit_recipe(s, ACTION)
    monkeypatch.setattr(s, "write_product", real)
    assert calls[-1] == WRITTEN[crash_at - 1]
    if crash_at < 4:
        assert s.read_product(recipe_candidate_file("run-003")).status == "approved"
    handle_admit_recipe(s, ACTION)
    assert _tree(s) == _tree(clean)


@pytest.mark.parametrize("status", ["pending_review", "rejected", "draft"])
def test_a_candidate_that_is_not_approved_is_left_alone(tmp_path, status):
    s = make_store(tmp_path)
    write_recipe_candidate(s, "run-003", status)
    handle_admit_recipe(s, ACTION)
    assert s.read_product(recipe_candidate_file("run-003")).status == status
    assert not s.exists(recipe_file("run-003"))


def test_an_existing_recipe_is_not_overwritten(store):
    handle_admit_recipe(store, ACTION)
    # 人为退回 approved,模拟"配方已写、候选还没置 effective"之后配方又被改过
    from recipestore.track import deprecate
    deprecate(store, R, reason="r", run_id="run-009")
    cand = store.read_product(recipe_candidate_file("run-003"))
    cand.status = "approved"
    store.write_product(recipe_candidate_file("run-003"), cand, "t")
    handle_admit_recipe(store, ACTION)
    assert get_recipe(store, R).status == "deprecated"
    assert store.read_product(recipe_candidate_file("run-003")).status == "effective"


def test_an_approved_candidate_without_repro_is_not_admitted_and_the_failure_is_recorded(tmp_path):
    s = make_store(tmp_path)
    payload = batch_payload()
    del payload["repro"]
    write_recipe_candidate(s, "run-003", "approved", payload)
    write_directive(s, "run-003")  # 来源 run 的目录在场,失败记录才写得进去
    handle_admit_recipe(s, ACTION)
    assert not s.exists(recipe_file("run-003"))
    assert s.read_product(recipe_candidate_file("run-003")).status == "approved"
    failure = s.read_product(failure_record_file("run-003", "admit_recipe"))
    assert (failure.payload["category"], failure.payload["point"]) == ("T2", "recipestore.admit")
    assert "carries no repro evidence" in failure.payload["reason"]
    commits = _commits(s)
    handle_admit_recipe(s, ACTION)  # 每轮重来也不堆提交
    assert _commits(s) == commits


@pytest.mark.parametrize("repro", [None, {}, {"errors": []}, {"errors": "x", "warnings": []}, "ok"])
def test_has_repro_evidence_needs_both_lists(repro):
    assert not has_repro_evidence({"repro": repro})


def test_has_repro_evidence_accepts_errors_too():
    # 有错误也算"证据在场":校验结果给人看,不硬拒绝
    assert has_repro_evidence({"repro": {"errors": ["x"], "warnings": []}})


def _runs(store):
    return sorted(p.name for p in (store.repo_root / CONTROL_PLANE_ROOT / "runs").iterdir())


@pytest.mark.parametrize("target", ["run-003", "recipe-", "recipe-a/b", "recipe-.."])
def test_a_malformed_target_is_a_no_op(store, target):
    # 计划 8 的 GateView 用信封 id;手写候选的 id 可能不是 recipe-<run>。抛错会卡死守护进程(Ruling 20)
    commits, runs = _commits(store), _runs(store)
    handle_admit_recipe(store, Action(kind="admit_recipe", target_id=target, detail={}))
    assert (_commits(store), _runs(store)) == (commits, runs)
    assert store.read_product(recipe_candidate_file("run-003")).status == "approved"


def test_a_missing_candidate_records_one_failure_and_does_not_raise(store):
    write_directive(store, "run-404")
    action = Action(kind="admit_recipe", target_id="recipe-run-404", detail={})
    handle_admit_recipe(store, action)
    failure = store.read_product(failure_record_file("run-404", "admit_recipe"))
    assert (failure.payload["category"], failure.payload["point"]) == ("T2", "recipestore.admit")
    assert failure.payload["reason"] == "recipe candidate recipe-run-404 not found"
    commits = _commits(store)
    handle_admit_recipe(store, action)  # 每 tick 重来也不堆提交
    assert _commits(store) == commits


def test_a_missing_candidate_without_a_run_dir_leaves_no_trace(store):
    commits = _commits(store)
    handle_admit_recipe(store, Action(kind="admit_recipe", target_id="recipe-zz", detail={}))
    assert _commits(store) == commits
    assert not (store.repo_root / run_dir("zz")).exists()  # 多出的 runs/zz 会挡住 run id 分配
```

- [ ] **Step 2: 运行,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_rs_admit.py`
Expected: 收集阶段报错 `E   ModuleNotFoundError: No module named 'recipestore.admit'`。

- [ ] **Step 3: 写实现**

新建 `src/recipestore/admit.py`:

```python
"""收录处理器(设计 §6.3):把 approved 的配方候选写进 recipes/。

顺序与重入:
  1. recipe.yaml(effective)   —— 不在才写
  2. lineage.yaml             —— 不在才写
  3. track_record.yaml(空)   —— 不在才写
  4. 候选 approved → effective —— 最后一步,它是 reconcile 的幂等键
崩在任一步之后重跑,已写的步骤跳过,未写的补上,最终树相同。
"""
from __future__ import annotations

import copy

from controlplane.envelope import CURRENT_SCHEMA_VERSION, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.ledger import record_failure
from controlplane.paths import (
    failure_record_file,
    recipe_candidate_file,
    recipe_file,
    recipe_lineage_file,
    recipe_track_file,
    run_dir,
)
from controlplane.reconcile import Action
from controlplane.states import REVIEW, assert_transition
from recipestore.ids import run_id_of
from recipestore.track import NOW, lineage_envelope, track_record_envelope

TASK_ID = "admit_recipe"
FAILURE_POINT = "recipestore.admit"


def _single_segment(run_id: str) -> bool:
    return "/" not in run_id and "\\" not in run_id and run_id not in (".", "..")


def _fail_once(store: GitStore, run_id: str, reason: str) -> None:
    """写一次 T2 失败记录。已写过、或 runs/<run>/ 不在(不新建 run 目录,Ruling 15)时什么都不做。"""
    if store.exists(failure_record_file(run_id, TASK_ID)):
        return  # 已记过;每轮重来不重复写
    if not (store.repo_root / run_dir(run_id)).is_dir():
        return
    record_failure(store, run_id, task_id=TASK_ID, category="T2", point=FAILURE_POINT, retries=0, reason=reason)


def has_repro_evidence(payload: dict) -> bool:
    """审核门的"证据在场"规则:候选必须带 repro,且 errors、warnings 都是列表。"""
    repro = payload.get("repro")
    return (
        isinstance(repro, dict)
        and isinstance(repro.get("errors"), list)
        and isinstance(repro.get("warnings"), list)
    )


def recipe_envelope(candidate: Envelope) -> Envelope:
    return Envelope(
        id=candidate.id, type="recipe", version=1, created_at=NOW, created_by=OwnerRef(),
        status="effective", schema_version=CURRENT_SCHEMA_VERSION,
        links=list(candidate.links), payload=copy.deepcopy(candidate.payload),
    )


def handle_admit_recipe(store: GitStore, action: Action) -> None:
    # 确定性的坏动作一律是空操作,不抛(Ruling 20):计划 8 调处理器时不接异常,
    # 抛了守护进程会退出,重启后同一动作再来,永久卡死。瞬态异常(OSError 等)照常上抛。
    recipe_id = action.target_id
    try:
        run_id = run_id_of(recipe_id)
    except ValueError:
        return  # 不是 recipe-<run> 形状:解析不出 run id,无处留痕
    if not _single_segment(run_id):
        return  # 拼进路径会越出 recipe_candidates/ 与 runs/
    cpath = recipe_candidate_file(run_id)
    if not store.exists(cpath):
        _fail_once(store, run_id, f"recipe candidate {recipe_id} not found")
        return
    candidate = store.read_product(cpath)
    if candidate.status == "effective":
        return  # 已收录
    if candidate.status != "approved":
        return  # 只收录 approved;其余状态是过期的动作,什么都不做
    if not has_repro_evidence(candidate.payload):
        _fail_once(store, run_id,
                   f"recipe candidate {recipe_id} is approved but carries no repro evidence; not admitted")
        return

    rpath = recipe_file(run_id)
    if not store.exists(rpath):
        store.write_product(rpath, recipe_envelope(candidate), f"recipestore: admit {recipe_id}")

    lpath = recipe_lineage_file(run_id)
    if not store.exists(lpath):
        lineage = {
            "recipe_id": recipe_id,
            "source_run_id": run_id,
            "admitted_from": candidate.id,
            "events": [{"event": "admitted", "run_id": run_id}],
        }
        store.write_product(lpath, lineage_envelope(recipe_id, lineage), f"recipestore: lineage {recipe_id}")

    tpath = recipe_track_file(run_id)
    if not store.exists(tpath):
        store.write_product(tpath, track_record_envelope(recipe_id, []), f"recipestore: track record {recipe_id}")

    candidate = store.read_product(cpath)
    assert_transition(REVIEW, candidate.status, "effective")
    candidate.status = "effective"
    store.write_product(cpath, candidate, f"recipestore: candidate {recipe_id} effective")
```

- [ ] **Step 4: 运行,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_rs_admit.py`
Expected: `23 passed`。

- [ ] **Step 5: 全量回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: **B+168** 通过、无警告(修订时实测 1070)。

- [ ] **Step 6: 提交**

```bash
git add src/recipestore/admit.py
git add tests/test_rs_admit.py
git commit -m "feat: recipestore admit handler (approved candidate to effective recipe, lineage, empty track; each step re-entrant)"
```

---

### Task 46: 战绩处理器 `handle_record_track`

**Files:**
- Create: `src/recipestore/record.py`
- Test: `tests/test_rs_record.py`

**Interfaces:**
- Consumes: 任务 42 的 `compute_gain`、`read_report_payload`、`regression_flags`;任务 43 的 `get_recipe`、`read_track_entries`、`RecipeNotFoundError`;任务 44 的 `append_track_entry`、`verdict`、`deprecate`、`deprecated_by`、`DEPRECATE`、`USED_AS`、`NOW`;`controlplane.evidence.diagnostic_grade`(计划 1);`controlplane.paths.candidate_checkpoint_file`、`planner_decision_file`、`recipe_track_note_file`、`run_dir`、`CONTROL_PLANE_ROOT`;`controlplane.reconcile.Action`。
- Produces(计划 9 的组合根注册到 `handlers['record_track']`,键名由计划 8 定义):
  - `handle_record_track(store, action) -> None`,签名即 `Handler`;`action.target_id` 是 run id。
  - `used_as_for(store, run_id) -> str`。
  - 标记产物 `runs/<run>/recipe_track.yaml`:类型 `recipe_track_note`,状态 `executed`,载荷 `{recipe_id, result: "recorded" | "skipped", reason, entry, verdict, deprecated}`;`links = [run_id]`,有配方 id 时追加它。

**设计要点:**
- **流程**:标记已在 → 返回;读指令(不在就返回,不写标记)的 `recipe_ref` → 读配方 → 读候选(须是 `effective` 或 `rejected`)→ 读报告(stub 跳过)→ `compute_gain(store, run_id, 配方的 dimension)` → `regressed = bool(regression_flags(report))`(Ruling 13)→ `used_as`(Ruling 14)→ `append_track_entry` → `verdict` 为 `deprecate` 且配方仍 `effective` 就 `deprecate` → 最后写标记。
- **增益缺失不跳过**:基线找不到时条目照记,`gain` 为 `None`,标记的 `reason` 是 `compute_gain` 给的原因。`mean_gain` 本来就只统计非空条目。
- **两类"不能记"(Ruling 15)**:没有指令,以及会变的(没候选、候选未终态、没报告)什么都不写;不会变的(`recipe_ref` 为空、配方不存在、stub 报告)写 `skipped` 标记。都不抛异常。没有指令时不写标记,是因为 `runs/<run>/` 可能根本不存在,写了会新建一个只有标记的 run 目录。
- **`used_as` 以指令的 `planner_decision_ref` 为准**(Ruling 14):先按它读 `planner_decision_file(源 run)`;没有这个键或决策不在时,才扫 `runs/` 下 `executed` 的决策。
- **故障注入**:带退化的场景写四次(战绩 → lineage → 配方状态 → 标记),在每一次抛 `OSError`,重跑后只有一条战绩、配方 `deprecated`、lineage 只有一条事件、标记 `deprecated: true`(Ruling 17)。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_rs_record.py`:

```python
import pytest

from controlplane.paths import recipe_file, recipe_lineage_file, recipe_track_file, recipe_track_note_file, run_dir
from controlplane.reconcile import Action
from recipestore.library import get_recipe, read_track_entries
from recipestore.record import handle_record_track, used_as_for
from rs_fixtures import (
    l1,
    make_store,
    write_candidate_checkpoint,
    write_directive,
    write_model,
    write_planner_decision,
    write_report,
)
from rs_recipes import entry, put_recipe

R = "recipe-run-003"
FLAG = {"dimension": "tom", "kind": "social_regression", "drop": 0.2}
WRITE_ORDER = [
    recipe_track_file("run-003"),
    recipe_lineage_file("run-003"),
    recipe_file("run-003"),
    recipe_track_note_file("run-004"),
]


def _act(run_id="run-004"):
    return Action(kind="record_track", target_id=run_id, detail={})


def _note(store, run_id="run-004"):
    return store.read_product(recipe_track_note_file(run_id))


def _commits(store):
    return int(store._git("rev-list", "--count", "HEAD").strip())


@pytest.fixture
def store(tmp_path):
    """run-001 产出基线 zengen-v1(tom=0.5);配方 recipe-run-003 已收录;
    run-004 用该配方 clone,候选已晋级,报告 tom=0.6 对比 zengen-v1。"""
    s = make_store(tmp_path)
    write_model(s, 1, "run-001")
    write_report(s, "run-001", scores=[l1("tom", 0.5)])
    put_recipe(s, "run-003", entries=[])
    write_planner_decision(s, "run-003", "run-004", "clone")
    write_directive(s, "run-004", recipe_ref=R)
    write_candidate_checkpoint(s, "run-004", "effective")
    write_report(s, "run-004", scores=[l1("tom", 0.6)], baseline_ref="zengen-v1")
    return s


def test_a_promoted_run_is_recorded_with_its_gain(store):
    handle_record_track(store, _act())
    [e] = read_track_entries(store, R)
    assert e == {"run_id": "run-004", "gain": pytest.approx(0.1), "promoted": True,
                 "regressed": False, "used_as": "clone"}
    note = _note(store)
    assert (note.type, note.status) == ("recipe_track_note", "executed")
    assert note.payload["result"] == "recorded"
    assert (note.payload["verdict"], note.payload["deprecated"], note.payload["reason"]) == ("keep", False, "ok")
    assert get_recipe(store, R).status == "effective"


def test_a_rejected_run_is_recorded_as_not_promoted(store):
    write_candidate_checkpoint(store, "run-004", "rejected")
    handle_record_track(store, _act())
    assert read_track_entries(store, R)[0]["promoted"] is False


def test_a_regression_deprecates_the_recipe_and_says_why(store):
    write_report(store, "run-004", scores=[l1("tom", 0.3)], baseline_ref="zengen-v1", flags=[FLAG])
    handle_record_track(store, _act())
    [e] = read_track_entries(store, R)
    assert e["regressed"] is True and e["gain"] == pytest.approx(-0.2)
    assert get_recipe(store, R).status == "deprecated"
    assert _note(store).payload["verdict"] == "deprecate" and _note(store).payload["deprecated"] is True
    events = store.read_product(recipe_lineage_file("run-003")).payload["events"]
    assert events == [{"event": "deprecated", "run_id": "run-004", "reason": "latest track entry (run-004) regressed"}]


def test_an_earlier_regression_followed_by_a_clean_run_keeps_the_recipe(tmp_path):
    s = make_store(tmp_path)
    write_model(s, 1, "run-001")
    write_report(s, "run-001", scores=[l1("tom", 0.5)])
    put_recipe(s, "run-003", entries=[entry("run-002", -0.1, regressed=True)])
    write_directive(s, "run-004", recipe_ref=R)
    write_candidate_checkpoint(s, "run-004", "effective")
    write_report(s, "run-004", scores=[l1("tom", 0.6)], baseline_ref="zengen-v1")
    handle_record_track(s, _act())
    assert get_recipe(s, R).status == "effective"


def test_a_missing_baseline_still_records_the_entry_with_no_gain(store):
    write_report(store, "run-004", scores=[l1("tom", 0.6)], baseline_ref="zengen-v9")
    handle_record_track(store, _act())
    assert read_track_entries(store, R)[0]["gain"] is None
    assert _note(store).payload["reason"] == "baseline model zengen-v9 not found"


def test_a_directive_without_a_recipe_ref_writes_a_skip_note(store):
    write_directive(store, "run-004", recipe_ref="")
    handle_record_track(store, _act())
    assert _note(store).payload == {"recipe_id": "", "result": "skipped",
                                    "reason": "directive run-004 has no recipe_ref",
                                    "entry": None, "verdict": None, "deprecated": False}
    assert read_track_entries(store, R) == []


def test_a_missing_recipe_writes_a_skip_note(store):
    write_directive(store, "run-004", recipe_ref="recipe-run-404")
    handle_record_track(store, _act())
    assert _note(store).payload["reason"] == "recipe recipe-run-404 not found"
    assert _note(store).links == ["run-004", "recipe-run-404"]


def test_a_malformed_recipe_ref_writes_a_skip_note(store):
    write_directive(store, "run-004", recipe_ref="latest")
    handle_record_track(store, _act())
    assert _note(store).payload["reason"] == "recipe latest not found"


def test_a_missing_directive_writes_nothing_and_creates_no_run_dir(tmp_path):
    # 写标记会新建只有 recipe_track.yaml 的 runs/run-004/,它会参与"最大 run"判定(Ruling 15)
    s = make_store(tmp_path)
    put_recipe(s, "run-003", entries=[])
    commits = _commits(s)
    handle_record_track(s, _act())
    assert _commits(s) == commits
    assert not (s.repo_root / run_dir("run-004")).exists()


def test_a_stub_report_writes_a_skip_note(store):
    write_report(store, "run-004", stub=True)
    handle_record_track(store, _act())
    assert _note(store).payload["reason"] == "diagnostic report for run-004 is a stub"
    assert read_track_entries(store, R) == []


@pytest.mark.parametrize("setup", ["no_candidate", "pending_review", "approved", "no_report"])
def test_a_premature_action_writes_nothing(tmp_path, setup):
    s = make_store(tmp_path)
    put_recipe(s, "run-003", entries=[])
    write_directive(s, "run-004", recipe_ref=R)
    if setup in ("pending_review", "approved"):
        write_candidate_checkpoint(s, "run-004", setup)
        write_report(s, "run-004", scores=[l1("tom", 0.6)], baseline_ref="zengen-v1")
    elif setup == "no_report":
        write_candidate_checkpoint(s, "run-004", "effective")
    commits = _commits(s)
    handle_record_track(s, _act())
    assert _commits(s) == commits
    assert not s.exists(recipe_track_note_file("run-004"))


def test_running_twice_changes_nothing(store):
    handle_record_track(store, _act())
    commits = _commits(store)
    handle_record_track(store, _act())
    assert _commits(store) == commits
    assert len(read_track_entries(store, R)) == 1


@pytest.mark.parametrize("crash_at", [1, 2, 3, 4])
def test_a_crash_at_any_step_converges_on_rerun(store, monkeypatch, crash_at):
    # 带退化:写入顺序是 track_record → lineage → recipe(deprecated) → note,共 4 次
    write_report(store, "run-004", scores=[l1("tom", 0.3)], baseline_ref="zengen-v1", flags=[FLAG])
    real = store.write_product
    calls = []

    def flaky(rel, env, msg):
        calls.append(rel)
        if len(calls) == crash_at:
            raise OSError("simulated crash")
        real(rel, env, msg)

    monkeypatch.setattr(store, "write_product", flaky)
    with pytest.raises(OSError):
        handle_record_track(store, _act())
    monkeypatch.setattr(store, "write_product", real)
    assert not store.exists(recipe_track_note_file("run-004"))
    handle_record_track(store, _act())
    assert len(read_track_entries(store, R)) == 1
    assert get_recipe(store, R).status == "deprecated"
    assert len(store.read_product(recipe_lineage_file("run-003")).payload["events"]) == 1
    assert calls == WRITE_ORDER[:crash_at]
    assert _note(store).payload["verdict"] == "deprecate"
    assert _note(store).payload["deprecated"] is True


def test_a_crash_before_the_note_converges_on_rerun(store, monkeypatch):
    real = store.write_product

    def flaky(rel, env, msg):
        if rel == recipe_track_note_file("run-004"):
            raise OSError("simulated crash")
        real(rel, env, msg)

    monkeypatch.setattr(store, "write_product", flaky)
    with pytest.raises(OSError):
        handle_record_track(store, _act())
    monkeypatch.setattr(store, "write_product", real)
    handle_record_track(store, _act())
    assert len(read_track_entries(store, R)) == 1
    assert _note(store).payload["result"] == "recorded"


def test_a_recipe_already_deprecated_still_gets_the_entry(store):
    from recipestore.track import deprecate
    deprecate(store, R, reason="manual", run_id="run-002")
    write_report(store, "run-004", scores=[l1("tom", 0.3)], baseline_ref="zengen-v1", flags=[FLAG])
    handle_record_track(store, _act())
    assert len(read_track_entries(store, R)) == 1
    assert _note(store).payload["deprecated"] is False


def test_used_as_comes_from_the_planner_decision(store):
    assert used_as_for(store, "run-004") == "clone"
    write_planner_decision(store, "run-003", "run-004", "replay")
    assert used_as_for(store, "run-004") == "replay"


def test_used_as_defaults_to_clone_without_a_decision(tmp_path):
    s = make_store(tmp_path)
    assert used_as_for(s, "run-004") == "clone"
    write_planner_decision(s, "run-003", "run-004", "new")
    assert used_as_for(s, "run-004") == "clone"


def test_used_as_follows_the_directive_when_two_decisions_name_the_same_run(tmp_path):
    # 计划 5 Ruling 14:失败决策分配过的 run id 会被再分配,两份决策的 next_run_id 相同
    s = make_store(tmp_path)
    write_planner_decision(s, "run-002", "run-004", "clone", status="failed")
    write_planner_decision(s, "run-003", "run-004", "replay")
    write_directive(s, "run-004", recipe_ref=R, planner_decision_ref="planner-run-003")
    assert used_as_for(s, "run-004") == "replay"
    write_directive(s, "run-004", recipe_ref=R, planner_decision_ref="planner-run-002")
    assert used_as_for(s, "run-004") == "clone"  # 指令点名哪份就读哪份,不看状态
    write_planner_decision(s, "run-002", "run-004", "replay", status="failed")
    assert used_as_for(s, "run-004") == "replay"


def test_without_a_decision_ref_only_an_executed_decision_counts(tmp_path):
    s = make_store(tmp_path)
    write_planner_decision(s, "run-002", "run-004", "replay", status="failed")
    write_directive(s, "run-004", recipe_ref=R)  # 人工指令,没有 planner_decision_ref
    assert used_as_for(s, "run-004") == "clone"
    write_planner_decision(s, "run-003", "run-004", "replay")
    assert used_as_for(s, "run-004") == "replay"


def test_a_decision_ref_that_names_no_decision_falls_back_to_the_scan(tmp_path):
    s = make_store(tmp_path)
    write_planner_decision(s, "run-003", "run-004", "replay")
    write_directive(s, "run-004", recipe_ref=R, planner_decision_ref="decision-run-003")  # 形状不对
    assert used_as_for(s, "run-004") == "replay"
```

- [ ] **Step 2: 运行,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_rs_record.py`
Expected: 收集阶段报错 `E   ModuleNotFoundError: No module named 'recipestore.record'`。

- [ ] **Step 3: 写实现**

新建 `src/recipestore/record.py`:

```python
"""战绩处理器(设计 §6.3):一个用过配方的 run 到达终态后,把它的结果记进配方战绩。

流程:读指令的 recipe_ref → 读配方 → 读候选与诊断 → compute_gain 与退化标记 →
追加战绩(按 run_id 幂等)→ 按 verdict 决定是否 deprecate → 最后写 recipe_track 标记。
标记是 reconcile 的幂等键,所以放在最后:崩在中途重跑,前面各步都是幂等的,能收敛。
"""
from __future__ import annotations

from controlplane.envelope import CURRENT_SCHEMA_VERSION, Envelope, OwnerRef
from controlplane.evidence import diagnostic_grade
from controlplane.gitstore import GitStore
from controlplane.paths import (
    CONTROL_PLANE_ROOT,
    candidate_checkpoint_file,
    planner_decision_file,
    recipe_track_note_file,
    run_dir,
)
from controlplane.reconcile import Action
from recipestore.gain import compute_gain, read_report_payload, regression_flags
from recipestore.library import RecipeNotFoundError, get_recipe, read_track_entries
from recipestore.track import (
    DEPRECATE,
    NOW,
    USED_AS,
    append_track_entry,
    deprecate,
    deprecated_by,
    verdict,
)

TERMINAL_CANDIDATE_STATES = ("effective", "rejected")
RECORDED = "recorded"
SKIPPED = "skipped"
DEFAULT_USED_AS = "clone"
DECISION_PREFIX = "planner-"  # 计划 5 的决策 id:planner-<源 run>


def _note(store: GitStore, run_id: str, payload: dict) -> None:
    env = Envelope(
        id=f"recipe-track-{run_id}", type="recipe_track_note", version=1, created_at=NOW,
        created_by=OwnerRef(), status="executed", schema_version=CURRENT_SCHEMA_VERSION,
        links=[run_id] + ([payload["recipe_id"]] if payload.get("recipe_id") else []),
        payload=payload,
    )
    store.write_product(recipe_track_note_file(run_id), env, f"recipestore: track note {run_id} ({payload['result']})")


def _skip(store: GitStore, run_id: str, recipe_id: str, reason: str) -> None:
    _note(store, run_id, {"recipe_id": recipe_id, "result": SKIPPED, "reason": reason,
                          "entry": None, "verdict": None, "deprecated": False})


def _action_of(payload: dict) -> str:
    action = (payload.get("decision") or {}).get("action")
    return action if action in USED_AS else DEFAULT_USED_AS


def _decision_named_by_directive(store: GitStore, run_id: str) -> dict | None:
    """本 run 指令的 planner_decision_ref(planner-<源 run>)点名的那份决策的载荷;读不到返回 None。"""
    dpath = f"{run_dir(run_id)}/directive.yaml"
    if not store.exists(dpath):
        return None
    ref = store.read_product(dpath).payload.get("planner_decision_ref")
    if not isinstance(ref, str) or not ref.startswith(DECISION_PREFIX):
        return None
    source = ref[len(DECISION_PREFIX):]
    if not source or "/" in source or "\\" in source or source in (".", ".."):
        return None
    path = planner_decision_file(source)
    if not store.exists(path):
        return None
    return store.read_product(path).payload


def used_as_for(store: GitStore, run_id: str) -> str:
    """产出本 run 指令的那份规划决策的 action;没有决策(人工指令)或 action 不是 clone/replay 时记 clone。

    以指令的 planner_decision_ref 为准(计划 5 Ruling 14:失败决策分配过的 run id 会被再分配,
    两份决策可能有相同的 next_run_id)。指令没有这个键或点名的决策不在时,退路是扫 runs/,
    只认 executed 且 next_run_id 是本 run 的决策(只有它真写过指令)。"""
    named = _decision_named_by_directive(store, run_id)
    if named is not None:
        return _action_of(named)
    runs_root = store.repo_root / CONTROL_PLANE_ROOT / "runs"
    if runs_root.is_dir():
        for source in sorted(p.name for p in runs_root.iterdir() if p.is_dir()):
            path = planner_decision_file(source)
            if not store.exists(path):
                continue
            env = store.read_product(path)
            if env.status != "executed" or env.payload.get("next_run_id") != run_id:
                continue
            return _action_of(env.payload)
    return DEFAULT_USED_AS


def handle_record_track(store: GitStore, action: Action) -> None:
    run_id = action.target_id
    if store.exists(recipe_track_note_file(run_id)):
        return  # 已处理

    dpath = f"{run_dir(run_id)}/directive.yaml"
    if not store.exists(dpath):
        # 不写标记:runs/<run>/ 可能根本不存在,写了会新建一个只有标记的 run 目录,
        # 参与"最大 run"判定与 run id 分配(Ruling 15)
        return
    recipe_id = store.read_product(dpath).payload.get("recipe_ref") or ""
    if not isinstance(recipe_id, str) or not recipe_id:
        _skip(store, run_id, "", f"directive {run_id} has no recipe_ref")
        return
    try:
        recipe = get_recipe(store, recipe_id)
    except RecipeNotFoundError:
        _skip(store, run_id, recipe_id, f"recipe {recipe_id} not found")
        return

    cpath = candidate_checkpoint_file(run_id)
    if not store.exists(cpath):
        return  # 还没有候选:过早的动作,什么都不写,留给以后
    candidate = store.read_product(cpath)
    if candidate.status not in TERMINAL_CANDIDATE_STATES:
        return  # 候选还没到终态,同上

    report = read_report_payload(store, run_id)
    if report is None:
        return  # 还没有诊断,同上
    if diagnostic_grade(report) == "stub":
        _skip(store, run_id, recipe_id, f"diagnostic report for {run_id} is a stub")
        return

    dimension = recipe.payload.get("dimension", "")
    gain = compute_gain(store, run_id, dimension)
    entry = {
        "run_id": run_id,
        "gain": gain.gain,
        "promoted": candidate.status == "effective",
        "regressed": bool(regression_flags(report)),
        "used_as": used_as_for(store, run_id),
    }
    append_track_entry(store, recipe_id, entry)

    decided = verdict(read_track_entries(store, recipe_id))
    if decided == DEPRECATE and get_recipe(store, recipe_id).status == "effective":
        deprecate(store, recipe_id, reason=f"latest track entry ({run_id}) regressed", run_id=run_id)
    # 以 lineage 为准:崩在"配方已置 deprecated、标记未写"之后重跑,仍记为本 run 触发
    deprecated = deprecated_by(store, recipe_id) == run_id

    _note(store, run_id, {
        "recipe_id": recipe_id,
        "result": RECORDED,
        "reason": gain.reason,
        "entry": entry,
        "verdict": decided,
        "deprecated": deprecated,
    })
```

- [ ] **Step 4: 运行,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_rs_record.py`
Expected: `26 passed`。

- [ ] **Step 5: 全量回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: **B+194** 通过、无警告(修订时实测 1096)。

- [ ] **Step 6: 提交**

```bash
git add src/recipestore/record.py
git add tests/test_rs_record.py
git commit -m "feat: recipestore track handler (gain and regression into the track record, deprecate on regression, note last)"
```

---

### Task 47: 处理器串联与边界守卫

**Files:**
- Test: `tests/test_rs_flow.py`

**Interfaces:**
- Consumes: 任务 45、46 的两个处理器;任务 43 的 `find_recipes`、`get_recipe`。
- Produces: 无新名字。

**设计要点:**
- **一条完整的配方生命**:候选 `approved` → 收录 → 两次复用(一次晋级、一次被拒,增益 0.1、0.2)→ 第三次复用退化 → 自动 `deprecated`,lineage 事件依次是 `admitted`、`deprecated`,触发者是第三次复用的 run。
- **依赖方向守卫**:解析 `src/recipestore/*.py` 的导入,项目内的包只允许 `controlplane` 与 `recipestore`。
- **`__future__` 守卫**:`src/recipestore/*.py` 每个文件 docstring 之后的第一条语句必须是 `from __future__ import annotations`。
- 两条守卫已在撰写时做过变异检查:往 `ids.py` 末尾加 `import diagnostic`,第一条失败(`AssertionError: ids.py imports ['diagnostic']`);把 `ids.py` 的 `__future__` 行换成 `import re as _re`,第二条失败(`AssertionError: ids.py`)。

- [ ] **Step 1: 写测试**

新建 `tests/test_rs_flow.py`:

```python
"""RecipeStore 的两个处理器串起来:收录 → 复用两轮 → 第二轮退化 → 自动 deprecated。
另钉住依赖方向:recipestore 只导入 controlplane。"""
import ast
from pathlib import Path

import pytest

from controlplane.paths import recipe_candidate_file, recipe_lineage_file
from controlplane.reconcile import Action
from recipestore.admit import handle_admit_recipe
from recipestore.library import find_recipes, get_recipe
from recipestore.record import handle_record_track
from rs_fixtures import (
    l1,
    make_store,
    write_candidate_checkpoint,
    write_directive,
    write_model,
    write_recipe_candidate,
    write_report,
)

SRC = Path(__file__).resolve().parents[1] / "src" / "recipestore"
R = "recipe-run-002"


def _use(store, run_id, score, *, flags=None, status="effective"):
    write_directive(store, run_id, recipe_ref=R)
    write_candidate_checkpoint(store, run_id, status)
    write_report(store, run_id, scores=[l1("tom", score)], baseline_ref="zengen-v1", flags=flags)
    handle_record_track(store, Action(kind="record_track", target_id=run_id, detail={}))


def test_admit_then_reuse_then_deprecate(tmp_path):
    s = make_store(tmp_path)
    write_model(s, 1, "run-001")
    write_report(s, "run-001", scores=[l1("tom", 0.5)])
    write_recipe_candidate(s, "run-002", "approved")
    handle_admit_recipe(s, Action(kind="admit_recipe", target_id=R, detail={}))
    assert s.read_product(recipe_candidate_file("run-002")).status == "effective"

    _use(s, "run-003", 0.6)
    _use(s, "run-004", 0.7, status="rejected")
    [view] = find_recipes(s)
    assert (view.id, view.n_reuse, view.mean_gain) == (R, 2, pytest.approx(0.15))

    _use(s, "run-005", 0.2, flags=[{"kind": "social_regression", "dimension": "tom"}])
    assert get_recipe(s, R).status == "deprecated"
    assert find_recipes(s) == []
    [view] = find_recipes(s, states=("deprecated",))
    assert view.n_reuse == 3 and view.mean_gain == pytest.approx((0.1 + 0.2 - 0.3) / 3)
    events = s.read_product(recipe_lineage_file("run-002")).payload["events"]
    assert [e["event"] for e in events] == ["admitted", "deprecated"]
    assert events[1]["run_id"] == "run-005"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.add(node.module.split(".")[0])
    return out


def test_recipestore_only_imports_controlplane_among_project_packages():
    project = {p.name for p in SRC.parent.iterdir() if (p / "__init__.py").exists()}
    for path in sorted(SRC.glob("*.py")):
        bad = (_imports(path) & project) - {"controlplane", "recipestore"}
        assert not bad, f"{path.name} imports {sorted(bad)}"


def test_every_recipestore_module_starts_with_the_future_import():
    for path in sorted(SRC.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        body = tree.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]
        first = body[0]
        assert isinstance(first, ast.ImportFrom) and first.module == "__future__", path.name
```

- [ ] **Step 2: 运行,确认通过**

本任务只加测试,前面的任务已经实现了被测行为,所以直接通过。

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_rs_flow.py`
Expected: `3 passed`。

- [ ] **Step 3: 确认守卫会抓人(变异检查,不提交)**

```bash
cp src/recipestore/ids.py ids.py.orig
echo "import diagnostic  # noqa" >> src/recipestore/ids.py
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_rs_flow.py 2>&1 | grep -m1 "AssertionError"
mv ids.py.orig src/recipestore/ids.py
git diff --stat src/recipestore/ids.py
```

Expected: 打印 `E           AssertionError: ids.py imports ['diagnostic']`;还原之后 `git diff --stat` 无输出。

- [ ] **Step 4: 全量回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: **B+197** 通过、无警告(修订时实测 1099)。

- [ ] **Step 5: 提交**

```bash
git add tests/test_rs_flow.py
git commit -m "test: recipestore end to end (admit, reuse, regression deprecates) plus dependency and future-import guards"
```

---

### Task 48: 全量回归与改动范围检查

**Files:** 无改动。

**Interfaces:** 无。

**设计要点:**
- 只做核对,不提交。范围检查以"本计划第一个提交的父提交"为基点,所以不会把计划文件本身或计划 1 的改动算进来。

- [ ] **Step 1: 全量回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: **B+197** 通过、无警告(修订时实测 `1099 passed`)。

- [ ] **Step 2: 改动范围只在本计划文件表里**

`BASE` 取任务 40 第 1 步写进执行记录的提交号(新会话里先 `BASE=<记录里的提交号>`)。不要用 `HEAD~N` 当主方案:真实仓库里本计划的提交之后可能紧跟别的计划的提交。

```bash
echo "BASE=$BASE"
git diff --stat "$BASE"..HEAD
git diff --name-only "$BASE"..HEAD | sort
```

退路(执行记录丢了 `BASE`,且确认任务 40 至 47 的 8 个提交是 `HEAD` 往前连续的 8 个、中间没有别的计划的提交):用 `BASE=$(git rev-parse HEAD~8)` 代替,先 `git log --oneline -9` 核对第 9 行是本计划开始前的提交。

Expected: **硬性检查只有文件名**:`--name-only` 恰好列出以下 19 个文件,不多不少。`git diff --stat` 的最后一行**仅供参考**,插入行数对不上不要停:修订时在拷贝里实测为 `19 files changed, 2454 insertions(+), 1 deletion(-)`(那一处删除是 `pyproject.toml` 被替换的 `packages` 行),代码块任何一处改动都会让插入数变。

```
pyproject.toml
src/recipestore/__init__.py
src/recipestore/admit.py
src/recipestore/gain.py
src/recipestore/ids.py
src/recipestore/library.py
src/recipestore/record.py
src/recipestore/track.py
src/recipestore/validate.py
tests/rs_fixtures.py
tests/rs_recipes.py
tests/test_rs_admit.py
tests/test_rs_flow.py
tests/test_rs_gain.py
tests/test_rs_ids.py
tests/test_rs_library.py
tests/test_rs_record.py
tests/test_rs_track.py
tests/test_rs_validate.py
```

出现任何其他文件(尤其是 `src/controlplane/*`、`cli/package*.json`、`schemas/*`),停下排查,不要提交修复以外的东西。

- [ ] **Step 3: 依赖方向的反向检查**

```bash
grep -rn "recipestore" src/controlplane src/diagnostic src/simulator src/mindtrainer --include='*.py'; echo "exit=$?"
git status --short
```

Expected: `grep` 无输出,`exit=1`;`git status --short` 不列出任何 `src/`、`tests/`、`pyproject.toml` 下的文件,只剩本计划开始前就有的未提交改动(`cli/package.json`、`cli/package-lock.json` 等,不要碰)。

---

## 完成标准

任务 40 至 47 都提交、任务 48 核对通过后:

- `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error` 为 **B+197** 通过、无警告(B 是计划 1 落地后的全量数;修订时在 B=902 的拷贝里实测 1099)。
- 改动只涉及 File Structure 表里的 19 个文件;不含 `cli/package*.json`、`src/controlplane/*`、`schemas/*`。
- `src/recipestore/` 只导入 `controlplane` 与自己,每个文件以 `from __future__ import annotations` 开头(`tests/test_rs_flow.py` 强制检查);`src/controlplane`、`src/diagnostic`、`src/simulator`、`src/mindtrainer` 里没有 `recipestore` 的导入。
- 设计 §11 的契约名字全部存在且签名照抄:`recipestore.gain.compute_gain(store, run_id, dimension) -> GainResult(gain, baseline_run_id, reason)`;`find_recipes`、`get_recipe`、`compare`、`append_track_entry`、`verdict`、`RecipeView`;`handle_admit_recipe`、`handle_record_track` 都是 `Handler = Callable[[GitStore, Action], None]`。
- 两个处理器的逐步故障注入测试通过:崩在任意一次写之后重跑,树与一次跑通的结果相同。

## 已知限制

1. **`reconcile` 还不会产生 `admit_recipe` 与 `record_track` 动作。** 两个处理器只能直接调用;接进调和循环是计划 8 的事。
2. **直接调用 `handle_record_track` 不检查证据是否可接受。** mock 候选对 mock 基线的增益会被记进战绩(Ruling 10)。经 `reconcile` 触发时,触发条件已经要求证据可接受。real 候选对 mock 基线不给增益,条目照记、`gain` 为 `None`。
3. **命令行审核门③还不认配方候选。** `cli` 的 `approve` 仍只认候选 checkpoint;本计划只提供 `has_repro_evidence` 这条规则的 Python 版,收录处理器兜底。人直接改 `status: approved` 时,没有 `repro` 的候选不会被收录,但 `reconcile`(计划 8 之后)每轮会对它产生一个空操作的 `admit_recipe` 动作,直到人处理。
4. **配方候选还没有写方。** 提名器 `build_recipe_candidate` 在计划 5;本计划的测试手工构造候选。
5. **没有 `planner_decision_ref` 的指令,`used_as_for` 要扫描 `runs/` 下全部 `planner_decision`**(Ruling 14)。计划 5 写的指令都带这个键,只读一份决策;人工指令才走扫描。run 到数百个以上才会慢,与 R9 同一取舍。
6. **`superseded` 不自动产生**(R8):被更优配方取代的旧配方仍显示 `effective`。
7. **校验器不核对 `training_config` 的类型**(Ruling 4),也不核对 `stage_manifests` 的哈希是否与树里的清单一致;后者要读树,不再是纯函数。复用时派发器会因清单不符判 `failed`。
8. **agent_rsi 的 `uses_bridge` 是本计划新增的载荷键**(Ruling 9)。计划 7 或后续写 agent_rsi 配方候选时须写它。
9. **`regressed` 把 `g_probe_failure` 也算作退化**(Ruling 13)。
10. **处理器里的瞬态异常会让守护循环退出**,与派发器的已知限制相同(设计 §3.3)。
11. **"模型 → 产出 run"有两种判法。** 本计划的 `baseline_run_of` 取"`cand-<x>` 也在 `links` 中"的 x(Ruling 11),计划 8 的 `read_tree_state` 与评估器取 `links[1]`(reconcile-extension 计划第 43、1258、2313 行)。两种判法目前一致,计划 9 的一致性核对会加测试钉住在 promote_candidate 产物上结果相同。
12. **agent_rsi 类型的配方在 V1 不自动提名**(设计 §15)。`nominate_recipe` 只由训练 run 的候选触发,MindAgent 的轮次结果没有对应的提名触发;`recipe_type: agent_rsi` 只在本计划的校验器、检索与战绩里存在。需要时由人工写配方候选,载荷须含 `uses_bridge`(Ruling 9)。校验器、检索、`verdict`、`append_track_entry` 对 agent_rsi 配方照常工作(`tests/test_rs_library.py`、`tests/test_rs_track.py` 各有一条用 agent_rsi 载荷的用例)。
13. **形状不对的 `admit_recipe` 动作不留痕迹**(Ruling 20)。`target_id` 不是 `recipe-<run>`、或来源 run 目录不在时,处理器什么都不写;`reconcile` 每 tick 仍会产生这个空操作,直到人改好候选的文件名或 id。

## 未验证项

1. **计划 1 的真实产出。** 计划 1 尚未合并。修订时的拷贝不再用临时桩:在 `0385004` 上原样执行计划 1 任务 19 的两段脚本(路径与 `SUBDIRS`),原样落下任务 21 的 `ledger.py`、`test_ledger.py` 与任务 24 的 `evidence.py`、`test_evidence.py`,再逐任务重放本计划;任务 40 的前置检查打印 `preflight ok`。计划 1 的其余任务(20、22、23、25–27)本计划不用,没有搭。计划 1 合并前若再改这三块的代码块,任务 40 的前置检查或 `tests/test_rs_admit.py` 里读 `payload["category"]`、`payload["point"]`、`payload["reason"]` 的用例会先失败。
2. **B 的具体值。** 计划 1 会加多少测试未知;各任务写成 B+N,N 是在拷贝里实测的增量。绝对通过数依赖各计划的执行顺序(在本计划之前落地的计划都会改变 B),本计划只保证增量。
3. **`pyproject.toml` 那一行。** 任务 40 Step 6 的追加式脚本在两种起点(基线四项、已含 `inference_bridge`)上都实测过,重复运行打印 `already listed`;计划 2、4、6 若在本计划之前加了别的包,列表顺序会不同,脚本本身不受影响。
4. **真实诊断报告的 `backends` 字段与 `baseline_ref` 的取值。** 计划 8 的 `make_diagnostic_evaluator` 还没写;本计划假定它沿用 `DiagnosticConfig.baseline_ref` 写成 `zengen-vN`。若写成别的形状,`compute_gain` 会返回 `gain=None` 并写明 `baseline_ref ... is not a zengen-vN model id`,不会算错。
5. **提交步骤只在拷贝里跑过。** 拷贝里每个任务提交一次(提交信息用了简写,`git add` 用的是整体暂存而不是逐个文件),任务 48 的 `git diff --stat` 在拷贝里得到 `19 files changed, 2454 insertions(+), 1 deletion(-)` 与 19 个文件名。插入行数只作参考(任务 48 Step 2)。真实仓库里 `git status --short` 还会列出 `cli/package*.json` 与本计划文件之外的既有未提交改动,以实施时为准。
