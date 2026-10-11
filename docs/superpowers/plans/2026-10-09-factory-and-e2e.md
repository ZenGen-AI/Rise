# 组合根与端到端:factory 组合根、命令行开关、只读子命令与闭环端到端 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **撰写状态:完成,并已按设计 2026-10-11 的修订更新。** 十个任务都写完,并在九份计划的最终文本上做过一次完整重放:前八份按序放完后收集数为 2359(B),本计划放完后为 2507(= B+148);见「未验证项」第 1 条。

**Goal:** 新增顶层包 `factory`,作为闭环集成的组合根:读配置,按命令行开关组装派发器(带数据生产器)、六个闭环处理器、真实诊断评估器(V1 用 mock 后端)与外部推理桥接,再调用 `run_daemon`;同时提供只读子命令 `trace`、`cost`、`logs` 与单轮的 `reconcile --once`。不传 `--enable` 时,`factory run` 与现有的 `scripts/run_controlplane.py` 逐字节相同。最后用端到端测试证明闭环能转一整圈。

**Architecture:** `factory` 在依赖图的最右端,可以导入任何包,任何包都不导入它(有测试)。装配分两条路径:不传 `--enable` 时 `assemble` 只返回 `make_mindtrainer_dispatcher()`,`run` 调 `run_daemon` 时不传任何新参数,所以与 `mindtrainer.wire.run` 走同一条代码路径;传了 `--enable` 时,启动阶段一次装配完毕(缺配置、缺资产、缺凭证立即报错、退出码 2,不写树),派发器带 `TemplateDataProducer`,`request_diagnostic` 换成 `make_diagnostic_evaluator` + mock 后端,处理器由一张"处理器键 → 构造器"表逐个构造。开关到处理器键的映射是 `factory.switches.ENABLE_MAP` 这一张表。只读子命令直接调用 `controlplane.observe` 的函数,`logs` 只读 run 目录里产物的 `log_refs`。

**Tech Stack:** Python 3.11+ / uv / pytest;**无新增第三方依赖**(只用标准库 `argparse`、`hashlib`、`urllib.parse`、`inspect`)。

**Spec:**
- `docs/superpowers/specs/2026-10-09-closed-loop-integration-design.md` §10.2(本计划的范围)、§3.2–§3.4(动作、注入、确认)、§5(桥接与记账)、§11(依赖方向与名字)、§13(测试策略)、§14(未核实项)、§15(明确不做)、R1–R21(已定,本计划不推翻)。
- 计划顺序:设计 §12 第 9 份,任务号 100–109。前置:计划 1 至 8 全部落地。
- 基线:`main` 在 `0385004` 时全量 820 通过。本计划在前八份之后执行,起点 **B** 由任务 100 第 1 步实测;本计划只保证增量。

**依赖已有产出的名字**

| 来源 | 名字 | 状态 |
|---|---|---|
| `controlplane.daemon` | `Dispatcher`、`run_daemon`、`reconcile_once`(现有);`Handler`、`Evaluator`、三个入口的 `handlers` / `evaluator` / `accept_mock_evidence` 参数(**计划 8** 任务 93) | 已定稿 / 计划 8 |
| `controlplane.reconcile` | `Action`(现有);`CLOSED_LOOP_ACTIONS`(**计划 8** 任务 90) | 计划 8 |
| `controlplane.lock` | `RepoLockedError`、`acquire_repo_lock(repo_root)`(**计划 8** 任务 98) | 计划 8 |
| `controlplane.observe` | `trace`、`cost`、`sum_costs`(现有,只读;本计划不改) | 现有 |
| `controlplane.gitstore` / `envelope` | `GitStore`、`Envelope`、`OwnerRef`、`BigRef` | 现有 |
| `controlplane.paths` | `run_dir`、`backends_config_file`、`CONTROL_PLANE_ROOT`、`candidate_checkpoint_file`、`diagnostic_report_file`、`model_file`、`taxonomy_file`(现有) | 现有 |
| `controlplane.config` | `read_config`、`write_config`、`load_backend_config`、`MissingCredentialError`、`CredentialInGitTreeError`(**计划 1** 任务 22、23) | 计划 1 |
| `controlplane.ledger` | `record_cost`、`record_failure`(**计划 1** 任务 21;只在测试里造数据) | 计划 1 |
| `controlplane.evidence` | `diagnostic_grade`(**计划 1** 任务 24;只在测试里断言) | 计划 1 |
| `inference_bridge` | `CliBridge(config, *, store=None, log_dir=None, env=None, runner=..., sleep=...)`(**计划 1** 任务 26) | 计划 1 |
| `mindtrainer.dispatch` | `make_mindtrainer_dispatcher(*, ..., data_producer=None)`(**计划 2** 任务 33) | 计划 2 |
| `mindtrainer.data_producer` / `data_spec` | `TemplateDataProducer()`、`parse_data_spec`(**计划 2** 任务 30、32) | 计划 2 |
| `mindtrainer.wire` | `run`、`main`(现有,**只读**,用来做逐字节对比) | 现有 |
| `trainplanner.templates` | `install_default_template(store) -> str`、`PLANNER_V1_VERSION == "v1"`(**计划 4** 任务 53) | 计划 4 |
| `trainplanner.backends` | `BridgeDecisionBackend(bridge, *, name="bridge")`、`HeuristicBackend()`(**计划 4** 任务 54) | 计划 4 |
| `recipestore` / `trainscheduler` / `mindagent` | 见下一节「对计划 3、5、7 的依赖」 | 计划 3 / 5 / 7 |
| `simulator.realtime` / `simulator.store_adapter` | `create_server`、`EnvironmentServiceClient`、`realtime_env_pb2_grpc` / `read_persona`、`read_scenario`、`write_persona`、`write_scenario`(现有;计划 6 扩展过协议) | 现有 / 计划 6 |
| `diagnostic.evaluator` | `make_diagnostic_evaluator(*, taxonomy_version, item_pool_version, someval_runner, judge_backend, benchmark_version, tier="full", sampling_k=4, backends="mock")`(**计划 8** 任务 95) | 计划 8 |
| `diagnostic.assets` | `write_taxonomy`、`write_item_pool`、`read_taxonomy`、`read_item_pool`、`DiagnosticAssetError`(**计划 8** 任务 94) | 计划 8 |
| `diagnostic.someval_adapter` / `scorer` | `MockSoMEvalRunner(fixed)` / `MockJudgeBackend(model_id, fixed)`(现有) | 现有 |
| `diagnostic.datamodel` / `taxonomy_loader` | `DiagnosticTaxonomy`、`DiagnosticItem`、`VariantFamily` / `ItemPool`(现有) | 现有 |

## 对计划 3、5、7 的依赖(已按落盘文本核对)

九份计划都已落盘。下列名字按各计划的 File Structure 表与代码块核对过,**一律从子模块导入**:`trainscheduler`、`mindagent` 的 `__init__.py` 只有包说明、不导出名字。任务 106 的脚本 `scripts/check_factory_assumptions.py` 逐条检查它们(位置参数名元组与 keyword-only 参数名元组都要相等),任务 107、108 的第 1 步都先跑它;有任何一行不是 `OK` 就停下,把那一行报告给人,不要改本计划的代码去迁就。

| 编号 | 导入路径 | 签名(位置参数 / keyword-only) | 本计划怎么用 |
|---|---|---|---|
| A3-1 | `recipestore.library.find_recipes` | `(store, *, dimension=None, recipe_type=None, states=("effective",), order="track") -> list[RecipeView]` | 任务 108 断言配方已收录 |
| A3-2 | `recipestore.library.get_recipe` | `(store, recipe_id) -> Envelope` | 任务 108 |
| A3-3 | `recipestore.admit.handle_admit_recipe` | `(store, action) -> None` | 任务 107:`admit_recipe` 键直接用它 |
| A3-4 | `recipestore.record.handle_record_track` | `(store, action) -> None` | 任务 107:`record_track` 键直接用它 |
| A3-5 | `recipestore.gain.compute_gain` | `(store, run_id, dimension) -> GainResult` | 任务 108 断言提名用的增益 |
| A3-6 | `recipestore.gain.GainResult` | 数据类 `(gain, baseline_run_id, reason)` | 任务 108 |
| A3-7 | `recipestore.admit.has_repro_evidence` | `(payload) -> bool` | 任务 109 的共享用例表 |
| A3-8 | `recipestore.gain.baseline_run_of` | `(store, baseline_ref) -> tuple[str \| None, str]` | 任务 109 的 `links` 判法核对 |
| A5-1 | `trainscheduler.plan_handler.make_plan_handler` | `(*, backend, template_loader=load_template, config_loader=load_scheduler_config, defaults_loader=load_training_defaults) -> Handler` | 任务 107:`plan_next_round` 键,只传 `backend` 与 `config_loader` |
| A5-2 | `trainscheduler.nominate.make_nominate_handler` | `(*, config_loader=load_scheduler_config, accept_mock_evidence=None, repo_commit=None) -> Handler` | 任务 107:`nominate_recipe` 键,三个参数都传(Ruling 19、20、21) |
| A5-3 | `trainscheduler.config.install_default_configs` | `(store) -> list[str]`(已存在的文件不动) | 任务 107:`install-defaults` 写 `scheduler.yaml`、`training_defaults.yaml` |
| A5-4 | `trainscheduler.config.SchedulerConfig` | 冻结数据类,恰好九个字段(计划 5 Ruling 8):`confirm_directives`、`allow_interact`、`accept_mock_evidence`、`perturbable`、`min_gain`、`cost_gain_window`、`recent_decision_window`、`max_plan_attempts`、`template_version` | 任务 107:`--confirm-directives` 用 `dataclasses.replace` 覆盖 `confirm_directives`;脚本另有一行 `A5-4f` 核对字段集 |
| A5-5 | `trainscheduler.config.load_scheduler_config` | `(store) -> SchedulerConfig`(文件缺失取默认值) | 任务 107 的 loader 包它 |
| A5-6 | `trainscheduler.assemble.assemble_context` | `(store, run_id, *, config=None) -> PlanningContext` | 本计划不直接调用;任务 109 第 3 条核对它与计划 4 的交点 |
| A7-1 | `mindagent.rounds.make_start_round_handler` | `(*, config, client_factory, backend_factory, personas_loader, bridge=None) -> Handler` | 任务 107:`start_agent_round` 键 |
| A7-2 | `mindagent.admit.handle_admit_proposal` | `(store, action) -> None` | 任务 107:`admit_proposal` 键直接用它 |
| A7-3 | `mindagent.library.load_library` | `(store) -> LibraryState` | 任务 108 断言收录后的库版本是字符串 |
| A7-4 | `mindagent.config.AgentConfig` | 冻结数据类 `(scenario_ids, bindings, max_turns=6, episodes_per_scenario=1, max_retries_per_episode=1, min_support=3, min_ratio=0.5, min_fired_on_motivating=0.8, max_fired_elsewhere=0.2, max_parse_retries=2)` | 任务 107 由 `agent.yaml` 构造 |
| A-H | `controlplane.daemon.Handler` | `Callable[[GitStore, Action], None]`;`handlers` 的键只能是 `CLOSED_LOOP_ACTIONS` 的六个名字(计划 8 Ruling 7) | 任务 104 的表与任务 107 的构造器 |
| A8-1 | `controlplane.lock.acquire_repo_lock` | `(repo_root)`,返回上下文管理器;锁文件 `<repo_root>/.git/rismforge.lock`(**计划 8** 任务 98) | 任务 104:`reconcile_one_tick` 在装配之前取锁 |
| A8-2 | `controlplane.lock.RepoLockedError` | 异常类;`str(exc)` 是锁文件路径(**计划 8** 任务 98) | 任务 104:`factory.cli.main` 捕获并退出码 2 |

行为上的依赖,任务 108 在真实代码上验证:

- B5-1(R18):计划 5 的规划处理器遇到任何 `BridgeError`(T1 或 T2)都把决策置 `failed`、写 T3 失败记录(带桥接的 `log_ref`),不上抛。
- B5-2(R5):计划 8 的 `plan_next_round` 只对 run id 最大的 run 触发(计划 8 的契约表、实现与测试一致)。启用提名且候选 `effective` 时还要等配方入库门(B8-2)。
- B5-3:`planner_decision.template_version == "v1"`;`clone` 的指令不带 `data_spec`、复用配方锚定的清单;`new` 的指令带 `data_spec`。
- B7-1:计划 7 的轮次处理器读模型产物的 `links[1]` 作为 run,部署 episode 落在那个 run 下。
- B7-2:库版本一律是字符串。
- B8-1(CP-12):计划 8 任务 98 的 `controlplane.daemon.run_daemon` 在第一个 tick 之前取仓库排他锁,锁被占用时抛 `RepoLockedError`;`reconcile_once` 自己不取锁。任务 104 在真实代码上验证。
- B8-2(CP-11):计划 8 的 `plan_next_round` 在启用提名且最大 run 的候选 `effective` 时,要等该 run 的提名标记(`nominated` 或 `skipped`)在场,且配方候选 `recipe-<run>` 不在 `pending_review`、`approved`;候选 `rejected` 或没启用提名时不等。任务 105、108 在真实代码上验证。

## 本计划的裁决

设计 R1–R21 不动。下面是设计没规定、或与现有代码有出入的细节:

1. **Ruling: `--enable` 的五个开关到六个处理器键的映射是一张固定表 `ENABLE_MAP`:`plan → plan_next_round`、`nominate → nominate_recipe`、`track → record_track`、`agent → start_agent_round`、`admit → admit_recipe + admit_proposal`。未知开关与空项(`plan,,admit`、`""`)一律拒绝,退出码 2。** —— 原因:设计 §10.2 只给了五个开关名;两类收录都是"门已批准 → 写进正式库",没有理由只开一类;拼错的开关若被忽略,对应动作永远不触发且没有提示(与计划 8 Ruling 7 同理)。 —— 判错的代价:想只收录配方、不收录提案时做不到,要拆开关并改表与测试。
2. **Ruling: 不传 `--enable` 时,`assemble` 只构造 `make_mindtrainer_dispatcher()`,`run` 调 `run_daemon(store, interval_s, max_ticks=..., dispatcher=...)`,不传 `handlers`、`evaluator`、`accept_mock_evidence`,也不读任何配置文件;此时 `--allow-mock-evidence`、`--confirm-directives` 不产生任何效果。** —— 原因:设计 §10.2"不传 `--enable` 时与现在的 `scripts/run_controlplane.py` 行为相同";没有闭环动作时这两个开关无处可用(与计划 8 Ruling 8 同理)。用两棵逐字节相同的仓库分别跑 `mindtrainer.wire.run` 与 `factory run`,比较整棵 `control-plane/` 与全部提交说明(任务 104)。 —— 判错的代价:想"只换真实评估器、不开闭环动作"做不到;要另加开关。
3. **Ruling: 只要开了任一闭环开关,就同时注入 `TemplateDataProducer` 与 mock 后端的真实评估器;评估器不单独设开关。** —— 原因:桩诊断的证据等级恒为 `stub`,永不可接受(计划 1),闭环动作一个都不会触发;规划处理器写的 `new` 指令带 `data_spec` 而没有清单(设计 §9),不注入生产器必判 `failed`。只开开关而不注入这两样,闭环在结构上转不起来。 —— 判错的代价:闭环模式下候选的诊断报告都是 `backends: mock`,审核门显示 `[MOCK BACKENDS]`;这是 R17 要求的醒目标记,不是缺陷。
4. **Ruling: 评估器的参数放在配置信封 `control-plane/config/evaluator.yaml`,载荷恰好五个键 `taxonomy_version`、`item_pool_version`、`benchmark_version`、`tier`、`sampling_k`;`backends` 不可配置,恒为 `"mock"`;路径常量 `EVALUATOR_CONFIG` 放在 `factory.evaluation`,不进 `controlplane.paths`。** —— 原因:设计 §11"配置文件一律是信封";V1 只有 `MockSoMEvalRunner` 与 `MockJudgeBackend`,允许配置成 `real` 等于允许谎报证据等级;`controlplane.paths` 归计划 1,后续计划不得再改(设计 §11)。 **换题库必须同时换 `benchmark_version`,否则会跨题库比较**:诊断报告载荷里没有 `item_pool_version`(计划 8 的审计),计划 3 的 `compute_gain` 与计划 8 选基线时只比较 `benchmark_version`;只改 `item_pool_version` 而 `benchmark_version` 不变,新旧题库的分数会被当成可比。 —— 判错的代价:接真实 SoMEval 时要改 `factory.evaluation` 的代码(注入真实运行器本来就要改代码),而不只是改配置;TypeScript 命令行若要显示评估器配置,要自己写一遍这个路径;`read_evaluator_config` 不检查这条规则(它不知道上一份配置是什么),靠改配置的人遵守。
5. **Ruling: 默认的 mock SoMEval 对变体族的基题恒答参考答案、对变体题恒答错(`factory-mock-wrong-answer`)、对其余题恒答对,各 `sampling_k` 次;judge 的固定分数表为空(V1 只跑静态轨,计划 8 Ruling 12)。** —— 原因:每轮分数恒定,端到端的断言才是确定的(任务 103 与 108 的测试写死了这套答案下的分数与 `surface_match` 画像)。空标签冲突已由计划 5 按设计 R21 修掉(计划 5 Ruling 23),全答对的报告另有 `test_a_full_pass_report_can_be_planned` 覆盖(「跨计划核对」第 3 条)。 —— 判错的代价:mock 报告永远带一个 `surface_match` 错误画像,规划器看到的是假画像;闭环里不会出现退化标记,配方的自动 `deprecated` 不会被端到端触发(由计划 3 的单测覆盖)。
6. **Ruling: 默认诊断资产是最小夹具 `factory-tax-v1` / `factory-pool-v1`(一个一级维度、两道 rule 题),由 `factory install-defaults <repo>` 显式写入;`run` 与 `reconcile` 从不自动写默认资产或配置。** —— 原因:组合根不应静默往树里写东西;闭环模式缺资产时启动即报错、指出缺哪份文件(Ruling 7),人跑一次 `install-defaults` 即可。资产形状与计划 8 任务 95 的测试资产相同。 —— 判错的代价:第一次用闭环模式要多跑一条命令;已存在的 `evaluator.yaml` 不被覆盖,人改过的配置优先。
7. **Ruling: 闭环模式的装配全部在第一个 tick 之前完成;缺 `evaluator.yaml`、缺资产、缺 `backends.yaml` 或其中的角色、字段不合法、缺凭证,都抛 `FactoryConfigError`,命令行打印一行 `factory: error: ...` 到标准错误并以退出码 2 退出,树里不写任何东西。** —— 原因:设计要求"缺配置或缺凭证给明确错误并退出码非 0,而不是静默降级";启动即失败比"守护进程跑到第一个候选才崩"好查。计划 8 的评估器每次调用仍会重读资产,启动检查只是提前把错误暴露出来。 —— 判错的代价:资产在守护进程运行中被删时,仍按计划 8 Ruling 13 在调用处抛 `DiagnosticAssetError`。
8. **Ruling: `backends.yaml` 的载荷形状是 `{<角色>: <BackendConfig 原始字典>}`,V1 的角色只有 `planner`(规划后端)与 `agent`(技能提案生成器);`build_bridge(store, role, *, env=None, log_dir=None)` 每次新建一个 `CliBridge`(传 `store` 与 `log_dir`)。只有 `invocation_form: cli` 能构造,`api` 报"only 'cli' is implemented in V1 (R15)"。** —— 原因:计划 1 把形状留给计划 8,计划 8 声明不读任何配置文件,只剩组合根来定;计划 1 的测试已用 `{"planner": ...}` 演示过。 —— 判错的代价:要加第三种外部推理用途(例如 judge)时,在 `BRIDGE_ROLES` 里加一个名字。
9. **Ruling: 凭证相关的错误一律 `raise FactoryConfigError(...) from None`,信息只用计划 1 已脱敏的原文(变量名、键路径),绝不拼进配置值;有测试断言凭证值不出现在异常信息、`__cause__` 为空、桥接日志里也没有。** —— 原因:设计 G.3 与计划 1 的约定;断开异常链才能保证 traceback 里也没有。 —— 判错的代价:排查配置错误时看不到原始异常类型,只能看信息文字。
10. **Ruling: `logs` 只扫 run 目录顶层产物的 `payload["log_refs"]`(与 `observe.trace` 同一口径,不递归 `episodes/`);`--task <id>` 按产物载荷的 `task_id` 或桥接日志文件名 `<id>-<k>.log` 过滤;`file://` 之外的地址、文件不存在都标 `[unreachable]` 并写明"contents not shown";`sha256` 对不上照样打印内容但标 `[sha256 mismatch]`。** —— 原因:计划 1 的 `record_failure` 把 L3 引用放在 `log_refs` 列表里,`CliBridge` 的日志文件名是 `<task_id>-<k>.log`;设计要求"引用存在但文件不可达要明说"。 —— 判错的代价:成功调用的桥接日志只登记在桥接返回值里、不写进树(计划 1 的 `record_bridge_cost` 只记成本),所以 `logs` 只能看到失败记录与人工登记的引用;要看成功调用的日志,直接去 `.rismforge/logs/<run>/` 目录。
11. **Ruling: 退出码约定:0 成功;1 只读子命令发现缺失(`trace` 的 run 不存在、`logs` 的 run 不存在或有引用不可达/哈希不符);2 用法错误、装配错误或仓库被另一个进程锁住(Ruling 27)。`cost` 在没有 `run_trace` 时退出 0。** —— 原因:没有记账是正常状态(设计 §14 第 7 条),不是错误;`trace` 与 `cost` 的文本原样来自 `controlplane.observe`,不改它们的输出。 —— 判错的代价:脚本若用 `cost` 的退出码判断"run 是否存在"会判错,要用 `trace`。
12. **Ruling: `reconcile` 子命令的 `--once` 是必填开关,不带就是用法错误;输出每个动作一行 `<kind> <target_id>`,没有动作时打印 `no actions`。** —— 原因:设计 §10.2 写的就是 `reconcile --once`;把"跑一轮"与"一直跑"分成两个子命令(`reconcile --once` 与 `run`),不留无 `--once` 的歧义形态。 —— 判错的代价:多打一个开关。
13. **Ruling: `--confirm-directives/--no-confirm-directives` 是三态(不传为 `None`,表示用 `scheduler.yaml` 的 `confirm_directives`,默认真);值经 `BuildContext.confirm_directives` 交给规划与提名处理器的构造器(实现见 Ruling 21)。** —— 原因:设计 §3.4 规定确认模式由配置决定,开关只是覆盖;不传开关时不该悄悄改变配置的语义。 —— 判错的代价:无。
14. **Ruling: 增加 `--planner-backend bridge|heuristic`(默认 `bridge`)。`bridge` 用 `backends.yaml` 的 `planner` 角色构造 `BridgeDecisionBackend`,`heuristic` 用计划 4 的确定性 `HeuristicBackend`,不需要外部工具。** —— 原因:设计没规定组合根如何选规划后端;没有外部命令行工具时开发与端到端仍要能转闭环。 —— 判错的代价:多一个开关;`heuristic` 产出的决策只证明流程(设计 §14 第 5 条)。
15. **Ruling: 默认入口 `scripts/run_factory.py` 不接受 mock 证据;开发入口 `scripts/run_factory_dev.py` 对 `run`、`reconcile` 自动加 `--allow-mock-evidence`。两者都是调用 `factory.cli` 的薄壳。** —— 原因:R17"默认不接受 mock 证据,开发入口用显式开关打开"。 —— 判错的代价:用户要记两个脚本名;入口自身没有测试覆盖到"脚本被误改成默认打开",由 `dev_main` 的单测与入口薄壳的代码审查兜底。
16. **Ruling: 处理器构造器表 `factory.handlers.DEFAULT_BUILDERS` 是 `{处理器键: (BuildContext) -> Handler}`;任务 104 先放一张空表,任务 107 填满六个键;`factory.wire` 在开了闭环开关时才惰性导入它;`assemble` / `run` / `main` 都接受可选的 `builders` 覆盖整张表,开关映射出的键在表里找不到构造器时报 `no handler builder for [...]`、退出码 2。构造器可以经 `BuildContext.register_closer` 登记收尾函数,`run` 与 `reconcile_one_tick` 结束时(含异常)逆序调用。** —— 原因:不传 `--enable` 的路径不导入计划 3/5/7 的任何包,与 `mindtrainer.wire` 的导入面相同;任务 103–105 的测试用记录调用的假处理器,不受真实表影响;智能体轮次要起一个实时服务端,必须有地方停它。 —— 判错的代价:构造器与计划 3/5/7 的工厂签名之间多一层适配;某个构造器忘了登记收尾时,资源在进程退出时才释放。
17. **Ruling: `factory` 不导出任何名字(`__init__.py` 只有说明);对外只承诺 `factory.cli.main`、`factory.cli.dev_main`、`factory.wire.run`、`factory.wire.reconcile_one_tick`、`factory.wire.assemble`、`FactoryOptions`、`BuildContext`、`ENABLE_MAP`。** —— 原因:组合根是叶子,只有入口与测试调用它。 —— 判错的代价:无。
18. **Ruling: `pyproject.toml` 用追加式脚本改(任务 100 Step 4):正则读出 `packages = [...]` 那一行、按逗号拆成项;已含 `"src/factory"` 就打印 `already listed`、不改文件,否则追加在末尾再写回。** —— 原因:七份计划都改这同一行,执行顺序不同时列表内容不同,写死"改成某一整行"的步骤会覆盖别人的包;与计划 4 任务 50、计划 5 任务 60 同一写法,重复运行安全。 —— 判错的代价:有人把 `packages` 改成多行数组时正则找不到那一行,脚本以 `wheel packages line not found` 断言失败,要手工追加。
19. **Ruling: `accept_mock_evidence` 只有一个来源:命令行 `--allow-mock-evidence`。同一个布尔值既传给 `reconcile_once(..., accept_mock_evidence=...)`(计划 8 据它决定是否产生 `plan_next_round`、`nominate_recipe`、`record_track`),也传给 `make_nominate_handler(accept_mock_evidence=...)`(计划 5 据它判提名的证据);组合根从不传 `None`,所以 `scheduler.yaml` 的同名键在组合根里不起作用。规划处理器本身不判证据(计划 5 Ruling 4)。** —— 原因:两处各读各的会出现"调和循环产生了提名动作,处理器却因配置不接受 mock 而跳过"之类的分裂,而且跳过会写下幂等标记,这一轮再也不会被提名。 —— 判错的代价:想只靠改 `scheduler.yaml` 打开 mock 证据的人会发现没效果,必须用开关(或开发入口)。
20. **Ruling: `repo_commit` 取 `--repo-commit` 的值;不传时取组合根所在代码仓库根(`src/factory/wire.py` 往上两级)的 `git rev-parse HEAD`;不是 git 仓库、没有 `git`、或命令失败时为 `None`,原样交给 `make_nominate_handler`,由计划 5 在 `repro.errors` 里如实记一条错误(不是警告,设计 R20)。装配时取一次,整个进程用同一个值。V1 的配方候选因此恒有两条 `repro` 缺口(`eval_anchor.taxonomy_version`、`eval_anchor.item_pool_version`,设计 §14 第 9 条);取不到 HEAD 时才多出第三条。** —— 原因:设计 §6.1 要求配方锚定代码版本;控制面仓库(`GitStore` 指向的那个)不是代码仓库,不能用它的 HEAD;取不到时编造一个值比留空更糟。 —— 判错的代价:守护进程跨提交长跑时,记的是启动那一刻的提交,不是产出那一轮时的提交(「已知限制」第 9 条);工作区有未提交改动时也照样记 HEAD。
21. **Ruling: `--confirm-directives/--no-confirm-directives` 由 `factory.handlers.scheduler_config_loader(confirm_directives)` 实现:每次调用照常读 `scheduler.yaml`,开关不为 `None` 时只在返回的 `SchedulerConfig` 上 `dataclasses.replace(confirm_directives=...)`,不写回文件;规划与提名两个处理器共用这个 loader。`scheduler.yaml` 的其余八个键组合根不碰。** —— 原因:计划 5 的处理器每次调用都经 `config_loader` 读配置(计划 5 Ruling 20),覆盖放在 loader 里就不必改计划 5;不写回文件,人看到的配置就是实际的默认行为。 —— 判错的代价:`--no-confirm-directives` 跑出来的指令,事后看 `scheduler.yaml` 看不出它为什么没停在 `awaiting_confirmation`;要看启动命令。
22. **Ruling: 智能体轮次的配置是组合根自己的配置信封 `control-plane/config/agent.yaml`:必填 `scenario_ids`、`bindings`(`RoleBinding.to_dict()` 的列表)、`opponent_lines`;可选 `AgentConfig` 的其余八个字段,缺省用 `AgentConfig` 的默认值;未知键与类型错一律 `FactoryConfigError`。`install-defaults` 写入的默认值:三个 `deployment_simulation` 情境 `factory-deploy-s1..s3`、两个人格、`bindings = [user: frozen_reference(model_ref=zengen-v1), agent: self_play]`、对手台词 `["我真的很生气!!", "好吧,你说说看", "那就这样吧"]`、`max_turns: 4`、`min_support: 3`、`min_ratio: 0.5`。** —— 原因:计划 7 的 `AgentConfig` 明说"不落进 git 树",交给组合根决定来源;放成配置信封才能不改代码地换情境;默认值照计划 7 任务 88 的端到端(对手首句含情绪词,空库也有可分析的信号)。 —— 判错的代价:`AgentConfig` 加了字段时,`agent.yaml` 会把新字段当成未知键拒绝,要同步改 `factory.agent`(它用 `dataclasses.fields(AgentConfig)` 求可选键,所以只要新字段有默认值就自动接受);对手的 `model_ref` 只是标签。
23. **Ruling: V1 的 `backend_factory` 一律是罐头 `CannedGuidedBackend`(把 Actio 渲染的指导语拼成话语,与计划 7 任务 88 的 `Echo` 同一行为),默认入口与开发入口相同;`personas_loader` 用 `simulator.store_adapter.read_persona` 从树里读;`client_factory` 来自进程内 loopback 实时服务端 `AgentRuntime`(对手位用 `ScriptedBackend` 说 `opponent_lines`,情境经 `scenario_resolver` 从树里读),第一次要客户端时才起,`Assembly.close()` 时关通道并 `stop(0).wait()`;每个客户端的 stub 外包一层 `DeadlineStub`,每条 `Interact` 流带 15 秒 deadline(计划 7 已知限制 4,与它测试夹具里的 `_DeadlineStub` 同一写法;生产用的 `EnvironmentServiceClient` 自己没有 deadline,改它属于 simulator);`backends.yaml` 有 `agent` 项才构造桥接,没有就传 `None`(规则模板生成器)。** —— 原因:V1 没有加载 checkpoint 的推理后端(设计 §15);部署会话必须有实时服务端,组合根是唯一能起它的地方;懒启动让没有部署动作的 tick 不开端口。 —— 判错的代价:部署 episode 只证明技能路由与轨迹,不代表模型能力;服务端只绑 `127.0.0.1`,不能跨机器部署;一条部署会话超过 15 秒就以 `DEADLINE_EXCEEDED` 结束(计划 7 判为不可重跑的会话失败,轮次 `failed`),真实模型后端变慢时要调 `interact_deadline_s`(目前不在 `agent.yaml` 里)。
24. **Ruling: 端到端测试用人工登记的低分基线 `run-000`(全错的 mock 报告 + `zengen-v1`,`links = [cand-run-000, run-000]`),第一轮训练的候选晋级为 `zengen-v2`。** —— 原因:mock 诊断给每个候选同样的分(Ruling 5),没有更低的基线,`compute_gain` 恒为 0,提名永远跳过,闭环转不到配方这一步;基线经计划 8 的真实评估器写出,形状与真实报告相同。 —— 判错的代价:测试里的模型版本号比 run 号大 1,读断言时要注意;真实部署第一次闭环也需要人登记一个基线。
25. **Ruling: 整圈端到端用计划 4 的 `HeuristicBackend` 做规划(`--planner-backend heuristic`),不用罐头桥接;设计 §13 已同步为允许这样做。桥接的正向路径由任务 102 的假可执行体测试覆盖(`CliBridge` 取回文本、凭证打码),规划经桥接的正向端到端没有,只测了失败路径(R18,任务 108 的 `test_a_bridge_failure_fails_the_decision_and_stops_the_loop`)。** —— 原因:`HeuristicBackend` 是计划 4 的真实确定性后端,命令行一个开关就能选到,端到端里每一步都是真实代码;罐头桥接要另造一个吐固定 JSON 的假可执行体,它证明的只是"桥接能把字符串带回来",这一点任务 102 已经证明。 —— 判错的代价:规划提示经真实桥接往返时才出现的格式问题,例如模型输出带 Markdown 围栏、前后多出说明文字、JSON 被截断,要到接上真实后端才暴露;计划 4 的解析与重试逻辑在这条路径上只有计划 4 自己的单测覆盖。

26. **Ruling(设计 SYS-21):本计划落地后发现的跨计划缺陷,追加编号 110 起的修补任务,允许改对应的包,修补后重跑任务 106 至 109。** —— 原因:工作包 9 的边界是"不改其他包",它是本计划能独立施工的前提;集成时发现的跨包缺陷若就地放宽这条边界,工作包 9 的回归证明就失去意义,而且缺陷本身的可追溯性也没了。 —— 判错的代价:多一轮修补任务与一次重跑;缺陷若不修,闭环在真实运行里带病。 —— 本条是约定,不是已有任务(见「已知限制」第 11 条)。
27. **Ruling(设计 CP-12):`factory` 不自己取锁。** `run` 的锁由 `controlplane.daemon.run_daemon` 在第一个 tick 之前取(计划 8 任务 98),`reconcile_one_tick` 在装配之前取同一把锁;`factory.cli.main` 把 `RepoLockedError` 翻译成一行错误与退出码 2。只读子命令 `trace`、`cost`、`logs` 不取锁,被占用时照常输出。 —— 原因:锁的粒度是"一次写入"而不是"一次进程":`run` 与 `reconcile` 是两条可能并发的写入路径,只锁其中一条等于没锁;只读路径不写树,不需要排队。 —— 判错的代价:锁被别的进程占着时 `run` 与 `reconcile` 直接退出码 2,要等人放锁。

## Global Constraints

每个任务的要求都隐含包含本节。

- **`src/` 下每个源文件以 `from __future__ import annotations` 开头**(模块 docstring 之后的第一条语句也算);任务 100 的 `tests/test_factory_boundaries.py` 对 `src/factory/` 强制检查。
- **依赖方向**:`factory` 可以导入任何包;**任何包都不导入 `factory`**(任务 100 有测试)。`controlplane` 仍不导入任何新包。
- **不引入新第三方依赖。** `pyproject.toml` 的 `dependencies` 不动。
- **新顶层包 `factory` 加进 `pyproject.toml` 的 wheel 包列表,用追加式脚本追加在列表末尾**(Ruling 18;`tests/test_mt_packaging.py` 强制检查)。前面的计划已追加过 `src/inference_bridge`、`src/actio`、`src/recipestore`、`src/trainplanner`、`src/trainscheduler`、`src/mindagent`,顺序取决于执行顺序,**以执行时的实际列表为准**,只在末尾加 `"src/factory"`,其余不动。
- **凭证绝不进 git 树**;凭证值不进标准输出、标准错误、异常信息、日志(任务 102 有测试)。
- **读写产物走 `GitStore` + `Envelope`;配置走 `read_config` / `write_config`。** 唯一的直接文件读取是 `logs` 读 `file://` 引用指向的日志文件(那不是产物)。
- **`mindtrainer.wire` 与 `scripts/run_controlplane.py` 保持原样,不删、不改**(任务 109 用 `git diff` 证明)。
- **不传 `--enable` 时与 `scripts/run_controlplane.py` 逐字节相同**(任务 104 有测试)。幂等比较只看 `control-plane/` 下的产物与提交说明;仓库排他锁的锁文件在控制面仓库的 `.git/` 下(计划 8 任务 98),不在被比较的范围里。
- **仓库排他锁**:`run` 与 `reconcile` 的写入路径取锁(计划 8 任务 98);锁被占用时退出码 2、树里不写任何东西;只读子命令 `trace`、`cost`、`logs` 不取锁(任务 104 有测试)。
- **默认关闭 `--allow-mock-evidence`;开发入口显式打开**(Ruling 15)。
- **只改本计划文件表里的文件。** 不碰 `src/controlplane`、`src/diagnostic`、`src/mindtrainer`、`src/inference_bridge`、`src/trainplanner`、`src/recipestore`、`src/trainscheduler`、`src/actio`、`src/mindagent`、`src/simulator`、`schemas/`、`cli/`。跨计划核对发现的冲突只报告,不在本计划里修(任务 109)。
- **`git add` 逐个文件**,不用 `git add .` / `-A`;**不要碰** `cli/package.json`、`cli/package-lock.json`。
- **提交信息不加 `Co-Authored-By` 行**(用户明确要求)。下文每个提交命令都照此写。
- **测试命令**一律是 `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`(下文称"全量命令";跑单个文件时在末尾加路径),不得出现警告。
- **测试数**:起点 B 由任务 100 第 1 步实测(**B = 2359**,见「未验证项」第 1 条)。各任务的增量:任务 100 **+19**、101 **+10**、102 **+14**、103 **+12**、104 **+27**、105 **+4**、106 **+0**、107 **+18**、108 **+10**、109 **+34**(共 **+148**),即 **B+19 → B+29 → B+43 → B+55 → B+82 → B+86 → B+86 → B+104 → B+114 → B+148**。绝对通过数依赖前八份计划的执行顺序与各自最终的测试数,以增量为准;其中任务 104 的锁用例 +5 与任务 108 的配方门用例 +1 来自设计 2026-10-11 的修订(CP-11、CP-12),已在九份计划按序重放的拷贝里核对过收集数(各测试文件的用例数与文件表一致,合计 +148)。

## File Structure

| 文件 | 动作 | 任务 | 职责 |
|---|---|---|---|
| `pyproject.toml` | 修改(1 行) | 100 | wheel 包列表末尾追加 `"src/factory"`(追加式脚本,Ruling 18) |
| `src/factory/__init__.py` | 新建(6 行) | 100 | 包说明 |
| `src/factory/errors.py` | 新建(6 行) | 100 | `FactoryConfigError` |
| `src/factory/switches.py` | 新建(41 行) | 100 | `ENABLE_MAP`、`SWITCHES`、`parse_enable`、`handler_keys`、`UnknownSwitchError` |
| `src/factory/readonly.py` | 新建(75 行) | 101 | `trace`、`cost`(即 `controlplane.observe` 的函数)、`collect_log_refs`、`render_logs` |
| `src/factory/bridges.py` | 新建(71 行) | 102 | `BRIDGE_ROLES`、`build_bridge` |
| `src/factory/evaluation.py` | 新建(79 行) | 103 | `EVALUATOR_CONFIG`、`EVALUATOR_KEYS`、`read_evaluator_config`、`build_mock_evaluator` |
| `src/factory/defaults.py` | 新建(49 行);任务 107 整个替换(88 行) | 103 / 107 | 默认诊断资产、评估器配置、MindAgent 部署素材、`install_defaults` |
| `src/factory/wire.py` | 新建(205 行) | 104 | `FactoryOptions`、`BuildContext`、`Assembly`、`assemble`、`run`、`reconcile_one_tick`、`resolve_repo_commit` |
| `src/factory/cli.py` | 新建(135 行) | 104 | argparse 子命令与开关、退出码、`main`、`dev_main` |
| `src/factory/handlers.py` | 新建(10 行,空表);任务 107 整个替换(92 行) | 104 / 107 | `DEFAULT_BUILDERS`、`scheduler_config_loader` |
| `src/factory/agent.py` | 新建(146 行) | 107 | `agent.yaml`、`DeadlineStub`、`AgentRuntime`、`CannedGuidedBackend`、`personas_from_tree` |
| `scripts/run_factory.py` | 新建(11 行) | 104 | 默认入口薄壳 |
| `scripts/run_factory_dev.py` | 新建(11 行) | 104 | 开发入口薄壳(自动 `--allow-mock-evidence`) |
| `scripts/check_factory_assumptions.py` | 新建(83 行) | 106 | 对计划 3、5、7 的依赖逐条检查 |
| `tests/factory_fixtures.py` | 新建(112 行,不是测试文件) | 104 | 搭仓库、写指令、合成清单、假处理器、树与提交日志快照、另起进程持锁(`held_lock`) |
| `tests/test_factory_switches.py` | 新建(17 个用例) | 100 | 开关映射表 |
| `tests/test_factory_boundaries.py` | 新建(2 个用例) | 100 | 无包导入 `factory`;`__future__` 头 |
| `tests/test_factory_readonly.py` | 新建(10 个用例) | 101 | 只读函数 |
| `tests/test_factory_bridges.py` | 新建(14 个用例) | 102 | 桥接装配与凭证不外露 |
| `tests/test_factory_evaluation.py` | 新建(12 个用例) | 103 | 评估器装配与默认资产 |
| `tests/test_factory_wire.py` | 新建(18 个用例) | 104 | 逐字节相同、闭环模式的注入、装配错误、仓库锁 |
| `tests/test_factory_cli.py` | 新建(9 个用例) | 104 | 只读子命令的输出、退出码、不提交、入口脚本、锁被占用时照常工作 |
| `tests/test_factory_e2e_frame.py` | 新建(4 个用例) | 105 | 端到端框架:空闭环一个 tick;mock 证据默认不转 |
| `tests/test_factory_handlers.py` | 新建(18 个用例) | 107 | 六个构造器、开关的单一来源、`agent.yaml`、Interact 流的 deadline |
| `tests/test_factory_e2e_loop.py` | 新建(10 个用例) | 108 | 完整闭环、R5、计划等配方门(CP-11)、R18、mock 证据关闭、MindAgent 链、R19 的轮次重入、读不出的轮次产物 |
| `tests/test_factory_cross_plan.py` | 新建(31 个用例) | 109 | 跨计划核对 |
| `tests/test_factory_run_order.py` | 新建(3 个用例) | 109 | 最大 run 的数值序(计划 8 的 `_latest_numbered_run`) |

(用例数按 pytest 收集的参数化展开计。)

---
### Task 100: 起点、包骨架与 `--enable` 映射表

**Files:**
- Modify: `pyproject.toml`(1 行)
- Create: `src/factory/__init__.py`、`src/factory/errors.py`、`src/factory/switches.py`
- Test: `tests/test_factory_switches.py`(17 个用例)、`tests/test_factory_boundaries.py`(2 个用例)

**Interfaces:**
- Consumes: `controlplane.reconcile.CLOSED_LOOP_ACTIONS`(计划 8 任务 90)。
- Produces: `ENABLE_MAP`、`SWITCHES`、`parse_enable(text) -> tuple[str, ...]`、`handler_keys(switches) -> frozenset[str]`、`UnknownSwitchError(ValueError)`;`FactoryConfigError`。

**设计要点:**
- **映射表是唯一的真相**(Ruling 1)。测试断言五个开关展开后恰好是 `CLOSED_LOOP_ACTIONS` 的六个名字、互不重叠,所以计划 8 若改了动作名,这里立刻失败。
- `parse_enable` 按 `SWITCHES` 的固定顺序去重,输出与输入顺序无关;`None`(没传 `--enable`)返回空元组。
- 边界测试在本任务就建好:`factory` 每个模块的 `__future__` 头、没有别的包导入 `factory`。

- [ ] **Step 1: 记下起点,确认前置**

```bash
BASE=$(git rev-parse HEAD); echo "$BASE"
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error 2>&1 | tail -1
```

Expected:第二行是 `B passed`(B 为前八份计划落地后的实测值,记下它;下文所有 Expected 都写成 B 加增量),无警告。撰写时在九份计划按序重放的集成拷贝里,前八份放完后的收集数是 B = 2359(见「未验证项」第 1 条)。再确认计划 8 与计划 1、2、4 的名字在场:

```bash
uv run --extra dev python -c "
from controlplane.reconcile import CLOSED_LOOP_ACTIONS
from controlplane.daemon import Handler, Evaluator, run_daemon
import inspect
assert set(CLOSED_LOOP_ACTIONS) == {'plan_next_round','nominate_recipe','record_track','start_agent_round','admit_recipe','admit_proposal'}
assert {'handlers','evaluator','accept_mock_evidence'} <= set(inspect.signature(run_daemon).parameters)
from controlplane.config import read_config, write_config, load_backend_config, MissingCredentialError, CredentialInGitTreeError
from controlplane.ledger import record_cost, record_failure
from controlplane.evidence import diagnostic_grade
from inference_bridge import CliBridge
from mindtrainer.data_producer import TemplateDataProducer
from mindtrainer.data_spec import parse_data_spec
from mindtrainer.dispatch import make_mindtrainer_dispatcher
assert 'data_producer' in inspect.signature(make_mindtrainer_dispatcher).parameters
from trainplanner.templates import install_default_template, PLANNER_V1_VERSION
assert PLANNER_V1_VERSION == 'v1'
from diagnostic.evaluator import make_diagnostic_evaluator
from diagnostic.assets import write_taxonomy, write_item_pool, read_taxonomy, read_item_pool, DiagnosticAssetError
from recipestore.admit import handle_admit_recipe
from trainscheduler.plan_handler import make_plan_handler
from mindagent.rounds import make_start_round_handler
print('prerequisites ok')
"
```

Expected:`prerequisites ok`。任何一行抛错就停下,报告是哪份计划的哪个名字缺了,不往下做。计划 3、5、7 的完整签名核对在任务 106 的脚本里,这里只确认三个包已落地。

- [ ] **Step 2: 写失败测试**

新建 `tests/test_factory_switches.py`:

```python
"""--enable 开关到六个处理器键的映射(闭环集成设计 §10.2)。"""
import pytest

from controlplane.reconcile import CLOSED_LOOP_ACTIONS
from factory.switches import ENABLE_MAP, SWITCHES, UnknownSwitchError, handler_keys, parse_enable


def test_the_switch_table_is_fixed():
    assert ENABLE_MAP == {
        "plan": ("plan_next_round",),
        "nominate": ("nominate_recipe",),
        "track": ("record_track",),
        "agent": ("start_agent_round",),
        "admit": ("admit_recipe", "admit_proposal"),
    }
    assert SWITCHES == ("plan", "nominate", "track", "agent", "admit")


def test_the_switches_cover_exactly_the_six_closed_loop_actions():
    keys = [k for v in ENABLE_MAP.values() for k in v]
    assert len(keys) == len(set(keys)) == 6
    assert set(keys) == set(CLOSED_LOOP_ACTIONS)


def test_admit_turns_on_both_admissions():
    assert handler_keys(["admit"]) == {"admit_recipe", "admit_proposal"}


@pytest.mark.parametrize("switch,keys", [
    ("plan", {"plan_next_round"}), ("nominate", {"nominate_recipe"}),
    ("track", {"record_track"}), ("agent", {"start_agent_round"}),
])
def test_each_other_switch_maps_to_one_key(switch, keys):
    assert handler_keys([switch]) == keys


def test_all_switches_give_all_six_keys():
    assert handler_keys(parse_enable("plan,nominate,track,agent,admit")) == CLOSED_LOOP_ACTIONS


def test_no_enable_means_no_switches_and_no_keys():
    assert parse_enable(None) == ()
    assert handler_keys(()) == frozenset()


def test_parse_keeps_the_fixed_order_and_drops_duplicates():
    assert parse_enable(" admit , plan,admit") == ("plan", "admit")


@pytest.mark.parametrize("text", ["", "plan,,admit", "plan,", "plann", "plan,dispatch", "plan_next_round"])
def test_empty_items_and_unknown_names_are_rejected(text):
    with pytest.raises(UnknownSwitchError):
        parse_enable(text)


def test_handler_keys_rejects_unknown_switches():
    with pytest.raises(UnknownSwitchError, match="nominatee"):
        handler_keys(["nominatee"])
```

新建 `tests/test_factory_boundaries.py`:

```python
"""factory 的边界:没有任何包导入 factory;factory 每个模块以 __future__ 开头。"""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"


def _imports(path):
    names = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.append(node.module)
    return names


def test_no_other_package_imports_factory():
    for path in sorted(SRC.rglob("*.py")):
        if path.relative_to(SRC).parts[0] == "factory":
            continue
        bad = [n for n in _imports(path) if n == "factory" or n.startswith("factory.")]
        assert not bad, f"{path.relative_to(SRC)} imports {bad}"


def test_every_factory_module_starts_with_future_annotations():
    for path in sorted((SRC / "factory").glob("*.py")):
        body = ast.parse(path.read_text(encoding="utf-8")).body
        if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
            body = body[1:]
        first = body[0]
        assert isinstance(first, ast.ImportFrom) and first.module == "__future__", path.name
```

- [ ] **Step 3: 跑测试,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_factory_switches.py tests/test_factory_boundaries.py`
Expected:收集阶段报错 `ModuleNotFoundError: No module named 'factory'`,`1 error`。

- [ ] **Step 4: 写实现**

新建 `src/factory/__init__.py`:

```python
"""组合根(闭环集成设计 §10.2):读配置、按开关装配派发器、处理器、评估器与桥接,调用 run_daemon。

依赖方向:factory 可以导入任何包;任何包都不导入 factory。mindtrainer.wire 与
scripts/run_controlplane.py 保持原样,不传 --enable 时本包的 run 与它们逐字节相同。
"""
from __future__ import annotations
```

新建 `src/factory/errors.py`:

```python
"""组合根的装配错误。命令行把它打印成一行错误并以退出码 2 退出,不静默降级。"""
from __future__ import annotations


class FactoryConfigError(Exception):
    """装配所需的配置、凭证或资产缺失或不合法。信息里只有路径、键名、变量名,绝不含凭证值。"""
```

新建 `src/factory/switches.py`:

```python
"""--enable 开关到处理器键的映射(闭环集成设计 §10.2)。

开关是给人用的五个短名;处理器键是 controlplane.reconcile.CLOSED_LOOP_ACTIONS 里的六个动作名。
admit 一个开关同时启用两类收录(配方候选与技能提案),因为两者都是"门已批准 → 写进正式库"。
"""
from __future__ import annotations

ENABLE_MAP: dict[str, tuple[str, ...]] = {
    "plan": ("plan_next_round",),
    "nominate": ("nominate_recipe",),
    "track": ("record_track",),
    "agent": ("start_agent_round",),
    "admit": ("admit_recipe", "admit_proposal"),
}
SWITCHES: tuple[str, ...] = tuple(ENABLE_MAP)


class UnknownSwitchError(ValueError):
    """--enable 里出现了五个开关之外的名字,或有空项。"""


def parse_enable(text: str | None) -> tuple[str, ...]:
    """'plan,admit' -> ('plan', 'admit')。None 表示没传 --enable,返回空元组。
    结果按 SWITCHES 的固定顺序去重;未知名字与空项(如 'plan,,admit'、'')一律拒绝。"""
    if text is None:
        return ()
    parts = [p.strip() for p in text.split(",")]
    if any(not p for p in parts):
        raise UnknownSwitchError(f"--enable has an empty item: {text!r}")
    unknown = sorted(set(parts) - set(ENABLE_MAP))
    if unknown:
        raise UnknownSwitchError(f"--enable: unknown switch(es) {unknown}; choose from {list(SWITCHES)}")
    return tuple(s for s in SWITCHES if s in parts)


def handler_keys(switches: tuple[str, ...] | list[str]) -> frozenset[str]:
    """开关 -> 要注入的处理器键。未知开关抛 UnknownSwitchError。"""
    unknown = sorted(set(switches) - set(ENABLE_MAP))
    if unknown:
        raise UnknownSwitchError(f"unknown switch(es) {unknown}")
    return frozenset(k for s in switches for k in ENABLE_MAP[s])
```

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_mt_packaging.py`
Expected:`1 failed, 1 passed`,失败信息是 `AssertionError: missing from [tool.hatch...wheel].packages: ['src/factory']`。

用下面的追加式脚本把 `"src/factory"` 登记进 `pyproject.toml` 的 wheel 包列表(Ruling 18)。它读出 `packages = [...]` 那一行、按逗号拆成项,不假设列表现在有几项、是什么顺序;已含 `"src/factory"` 就打印 `already listed`、不改文件,否则追加在末尾再写回。与计划 4 任务 50、计划 5 任务 60 同一写法:

```bash
uv run python - <<'PY'
import re
from pathlib import Path

p = Path("pyproject.toml")
s = p.read_text(encoding="utf-8")
m = re.search(r'^packages = \[(.*)\]$', s, re.M)
assert m, "wheel packages line not found"
items = [x.strip() for x in m.group(1).split(",") if x.strip()]
if '"src/factory"' in items:
    print("already listed")
else:
    items.append('"src/factory"')
    s = s[: m.start()] + "packages = [" + ", ".join(items) + "]" + s[m.end():]
    p.write_text(s, encoding="utf-8")
print(re.search(r'^packages = .*$', s, re.M).group(0))
PY
grep -c '"src/factory"' pyproject.toml
```

Expected:打印的 `packages` 行保留执行前的全部项、顺序不变,末尾是 `"src/factory"`;最后一行 `1`。九份计划都落地时(撰写时在集成拷贝里实测的顺序)为 `packages = ["src/controlplane", "src/simulator", "src/diagnostic", "src/mindtrainer", "src/inference_bridge", "src/recipestore", "src/trainplanner", "src/trainscheduler", "src/actio", "src/mindagent", "src/factory"]`;前面几项的顺序随执行顺序而变。重复运行只打印 `already listed` 与同一行,不改文件。

- [ ] **Step 5: 跑测试,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_factory_switches.py tests/test_factory_boundaries.py tests/test_mt_packaging.py`
Expected:`21 passed`。

Run: 全量命令。
Expected:`B+19 passed`,无警告。

- [ ] **Step 6: 提交**

```bash
git add pyproject.toml
git add src/factory/__init__.py
git add src/factory/errors.py
git add src/factory/switches.py
git add tests/test_factory_switches.py
git add tests/test_factory_boundaries.py
git commit -m "feat: factory package skeleton and the --enable switch table"
```

---

### Task 101: 只读函数 `trace`、`cost`、`logs`

**Files:**
- Create: `src/factory/readonly.py`
- Test: `tests/test_factory_readonly.py`(10 个用例)

**Interfaces:**
- Consumes: `controlplane.observe.trace`、`cost`(现有,原样转出);`controlplane.ledger.record_cost`、`record_failure`(计划 1,只在测试里造数据)。
- Produces: `factory.readonly.trace`、`cost`(就是 `controlplane.observe` 的那两个函数对象)、`collect_log_refs(store, run_id, *, task_id=None) -> list[tuple[str, dict]]`、`render_logs(store, run_id, *, task_id=None) -> tuple[str, int]`。

**设计要点:**
- **不重新计算。** `readonly.trace is controlplane.observe.trace`,测试用 `is` 断言,防止有人在这里另写一份。
- **成本未记录省略键**:计划 1 的 `record_cost` 对未知值省略键,`observe.sum_costs` 按 0 加总,测试用一条只有 `calls` 的外部推理记录证明汇总不抛 `TypeError`。
- **`logs` 的口径**见 Ruling 10;退出码见 Ruling 11。
- **只读**:用 `git rev-parse HEAD` 与 `git status --porcelain` 前后对比,证明三者都不提交、不改工作区。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_factory_readonly.py`:

```python
"""只读子命令:trace、cost 用 controlplane.observe 的现有函数;logs 按 log_refs 打印。三者都不提交。"""
import hashlib
import subprocess

import pytest

from controlplane.envelope import BigRef, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.ledger import record_cost, record_failure
from controlplane.observe import cost as observe_cost, trace as observe_trace
from controlplane.paths import run_dir
from factory import readonly
from factory.readonly import collect_log_refs, render_logs

RUN = "run-001"


def _head_and_status(store):
    head = subprocess.run(["git", "-C", str(store.repo_root), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout
    status = subprocess.run(["git", "-C", str(store.repo_root), "status", "--porcelain"],
                            capture_output=True, text=True, check=True).stdout
    return head, status


def _log_file(tmp_path, name, text):
    path = tmp_path / "logs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    path.write_bytes(data)
    return BigRef(uri=f"file://{path}", sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path / "cp"))
    s.init()
    env = Envelope(id=RUN, type="directive", version=1, created_at="2026-10-09T00:00:00Z",
                   created_by=OwnerRef(), status="executed", schema_version=1,
                   payload={"target_dimension": "tom", "recipe_ref": ""})
    s.write_product(f"{run_dir(RUN)}/directive.yaml", env, "seed directive")
    record_cost(s, RUN, task_id="plan-1", cost_type="external_inference", calls=1)
    record_cost(s, RUN, task_id="train", cost_type="gpu", gpu_hours=1.5)
    return s


def test_trace_and_cost_are_the_observe_functions_themselves():
    assert readonly.trace is observe_trace
    assert readonly.cost is observe_cost


def test_cost_sums_omitted_tokens_as_zero(store):
    assert "total: gpu_hours=1.5 tokens=0" in readonly.cost(store, RUN)


def test_logs_without_any_log_refs(store):
    assert render_logs(store, RUN) == (f"run {RUN}: no log_refs", 0)


def test_logs_of_a_missing_run(store):
    assert render_logs(store, "run-404") == ("run run-404: no products found", 1)


def test_logs_print_the_referenced_file(store, tmp_path):
    ref = _log_file(tmp_path, "plan-1-1.log", "--- prompt ---\nhello\n")
    record_failure(store, RUN, task_id="plan-1", category="T3", point="trainscheduler.plan",
                   retries=0, reason="bridge down", log_ref=ref)
    text, code = render_logs(store, RUN)
    assert code == 0
    assert f"log_ref [ok] {ref.uri}" in text
    assert "--- prompt ---\nhello" in text


def test_an_unreachable_reference_is_named_not_skipped(store):
    ref = BigRef(uri="file:///compute/logs/run-001/train-1.log", sha256="a" * 64, size_bytes=1)
    record_failure(store, RUN, task_id="train", category="T1", point="mindtrainer", retries=3,
                   reason="lost", log_ref=ref)
    text, code = render_logs(store, RUN)
    assert code == 1
    assert f"log_ref [unreachable] {ref.uri}" in text
    assert "contents not shown" in text


def test_a_non_file_uri_is_unreachable(store):
    ref = BigRef(uri="s3://bucket/run-001.log", sha256="a" * 64, size_bytes=1)
    record_failure(store, RUN, task_id="train", category="T1", point="x", retries=0, reason="r", log_ref=ref)
    text, code = render_logs(store, RUN)
    assert (code, "[unreachable] s3://bucket/run-001.log" in text) == (1, True)


def test_a_hash_mismatch_is_marked(store, tmp_path):
    ref = _log_file(tmp_path, "plan-1-1.log", "original\n")
    (tmp_path / "logs" / "plan-1-1.log").write_text("tampered\n", encoding="utf-8")
    record_failure(store, RUN, task_id="plan-1", category="T3", point="p", retries=0, reason="r", log_ref=ref)
    text, code = render_logs(store, RUN)
    assert code == 1
    assert "[sha256 mismatch]" in text and "tampered" in text


def test_task_filter_uses_the_record_task_id_or_the_bridge_log_name(store, tmp_path):
    a = _log_file(tmp_path, "plan-1-1.log", "A\n")
    b = _log_file(tmp_path, "agent-1-2.log", "B\n")
    record_failure(store, RUN, task_id="plan-1", category="T3", point="p", retries=0, reason="r", log_ref=a)
    trace_rel = f"{run_dir(RUN)}/run_trace.yaml"
    env = store.read_product(trace_rel)
    env.payload["log_refs"] = [b.to_dict()]
    store.write_product(trace_rel, env, "seed: bridge log ref on run_trace")
    assert [r["uri"] for _, r in collect_log_refs(store, RUN, task_id="plan-1")] == [a.uri]
    assert [r["uri"] for _, r in collect_log_refs(store, RUN, task_id="agent-1")] == [b.uri]
    assert [r["uri"] for _, r in collect_log_refs(store, RUN)] == [a.uri, b.uri]
    assert render_logs(store, RUN, task_id="nope") == (f"run {RUN}: no log_refs for task nope", 0)


def test_reading_never_commits(store, tmp_path):
    ref = BigRef(uri="file:///nowhere.log", sha256="a" * 64, size_bytes=1)
    record_failure(store, RUN, task_id="x", category="T1", point="p", retries=0, reason="r", log_ref=ref)
    before = _head_and_status(store)
    readonly.trace(store, RUN)
    readonly.cost(store, RUN)
    render_logs(store, RUN)
    render_logs(store, RUN, task_id="x")
    render_logs(store, "run-404")
    assert _head_and_status(store) == before
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_factory_readonly.py`
Expected:收集阶段报错 `ImportError: cannot import name 'readonly' from 'factory'`,`1 error`。

- [ ] **Step 3: 写实现**

新建 `src/factory/readonly.py`:

```python
"""只读子命令:trace、cost 直接用 controlplane.observe,logs 按 run 目录里的 log_refs 打印日志。

三者都只读树与本机文件,不写产物、不提交。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from urllib.parse import urlparse

from controlplane.gitstore import GitStore
from controlplane.observe import cost, trace
from controlplane.paths import run_dir

__all__ = ["cost", "trace", "collect_log_refs", "render_logs"]


def _local_path(uri: str) -> Path | None:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    return Path(parsed.path)


def _matches_task(payload: dict, uri: str, task_id: str) -> bool:
    # 失败记录的载荷带 task_id;CliBridge 的日志文件名是 <task_id>-<k>.log
    if payload.get("task_id") == task_id:
        return True
    path = _local_path(uri)
    name = path.name if path is not None else uri.rsplit("/", 1)[-1]
    return re.fullmatch(rf"{re.escape(task_id)}-\d+\.log", name) is not None


def collect_log_refs(store: GitStore, run_id: str, *, task_id: str | None = None) -> list[tuple[str, dict]]:
    """run 目录下(不递归)每个产物的 payload["log_refs"],按产物路径、再按出现顺序。
    返回 [(产物相对路径, 引用字典)]。task_id 非空时只留属于该任务的引用。"""
    out: list[tuple[str, dict]] = []
    for rel in store.list_products(run_dir(run_id)):
        payload = store.read_product(rel).payload
        for ref in payload.get("log_refs", []) or []:
            if task_id is None or _matches_task(payload, ref["uri"], task_id):
                out.append((rel, ref))
    return out


def render_logs(store: GitStore, run_id: str, *, task_id: str | None = None) -> tuple[str, int]:
    """返回 (要打印的文本, 退出码)。

    退出码:0 = 每个引用都打印出来了(或没有引用);1 = run 不存在,或至少一个引用不可达、哈希对不上。
    不可达的引用明说 unreachable,不静默省略;哈希对不上时照样打印内容,但标 sha256 mismatch。"""
    if not store.list_products(run_dir(run_id)):
        return f"run {run_id}: no products found", 1
    refs = collect_log_refs(store, run_id, task_id=task_id)
    scope = f" for task {task_id}" if task_id is not None else ""
    if not refs:
        return f"run {run_id}: no log_refs{scope}", 0
    lines = [f"run {run_id} logs{scope}"]
    code = 0
    for rel, ref in refs:
        uri = ref["uri"]
        path = _local_path(uri)
        if path is None or not path.is_file():
            lines.append(f"== {rel} log_ref [unreachable] {uri}")
            lines.append("   (not reachable from this machine; contents not shown)")
            code = 1
            continue
        data = path.read_bytes()
        mark = "ok"
        if ref.get("sha256") and hashlib.sha256(data).hexdigest() != ref["sha256"]:
            mark = "sha256 mismatch"
            code = 1
        lines.append(f"== {rel} log_ref [{mark}] {uri}")
        lines.append(data.decode("utf-8", errors="replace").rstrip("\n"))
    return "\n".join(lines), code
```

- [ ] **Step 4: 跑测试,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_factory_readonly.py`
Expected:`10 passed`。

Run: 全量命令。
Expected:`B+29 passed`,无警告。

- [ ] **Step 5: 提交**

```bash
git add src/factory/readonly.py
git add tests/test_factory_readonly.py
git commit -m "feat: factory read-only helpers (trace and cost from observe, logs from log_refs)"
```

---

### Task 102: 桥接装配 `build_bridge`

**Files:**
- Create: `src/factory/bridges.py`
- Test: `tests/test_factory_bridges.py`(14 个用例)

**Interfaces:**
- Consumes: `controlplane.config.read_config`、`load_backend_config`、`MissingCredentialError`、`CredentialInGitTreeError`(计划 1 任务 22、23);`controlplane.paths.backends_config_file`;`inference_bridge.CliBridge`(计划 1 任务 26)。
- Produces: `BRIDGE_ROLES = ("planner", "agent")`、`build_bridge(store, role, *, env=None, log_dir=None) -> CliBridge`。

**设计要点:**
- `backends.yaml` 的形状与角色见 Ruling 8;错误处理与凭证不外露见 Ruling 7、9。
- **真起子进程**:正向用例用 `sys.executable -c` 做假可执行体,它把凭证打印到标准输出;断言 `BridgeResult.text` 里是 `***REDACTED***`,日志文件里也没有凭证值。这证明组合根把 `store` 与 `log_dir` 都传进了 `CliBridge`,且没有绕开计划 1 的脱敏。
- **凭证值写进树里**:手写一份带 `token` 键的配置信封(绕过 `write_config` 的写前检查),`read_config` 的读后检查拒绝它;断言异常信息有键名、没有值。
- `env` 可注入,测试从不读真实环境变量。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_factory_bridges.py`:

```python
"""桥接装配:backends.yaml(配置信封)→ BackendConfig → CliBridge。缺什么明说,凭证值绝不外露。"""
import sys

import pytest

from controlplane.config import write_config
from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import backends_config_file
from factory.bridges import BRIDGE_ROLES, build_bridge
from factory.errors import FactoryConfigError
from inference_bridge import CliBridge

SECRET = "sekrit-value-0042"
VAR = "RF_FACTORY_TEST_TOKEN"


def _backend(**kw):
    raw = {
        "backend": "fake_cli", "invocation_form": "cli", "model_name": "m1",
        "prompt_template_version": "v1", "token_env": VAR,
        "retry_policy": {"max_attempts": 1, "backoff_base_s": 0},
        "command": [sys.executable, "-c", f"import os; print('reply', os.environ['{VAR}'])"],
        "timeout_s": 30,
    }
    raw.update(kw)
    return raw


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path / "cp"))
    s.init()
    return s


def _write(store, payload):
    write_config(store, backends_config_file(), "backends", payload, "test: backends")


def test_roles():
    assert BRIDGE_ROLES == ("planner", "agent")


def test_a_configured_role_builds_a_cli_bridge_that_redacts_its_secret(store, tmp_path):
    _write(store, {"planner": _backend()})
    bridge = build_bridge(store, "planner", env={VAR: SECRET}, log_dir=tmp_path / "logs")
    assert isinstance(bridge, CliBridge)
    result = bridge.complete("hi", run_id="run-001", task_id="plan-1")
    assert result.text == "reply ***REDACTED***"
    log = (tmp_path / "logs" / "run-001" / "plan-1-1.log").read_text(encoding="utf-8")
    assert SECRET not in log


def test_token_env_null_needs_no_credential(store, tmp_path):
    _write(store, {"agent": _backend(token_env=None, command=[sys.executable, "-c", "print('ok')"])})
    bridge = build_bridge(store, "agent", env={}, log_dir=tmp_path / "logs")
    assert bridge.complete("x", run_id="run-001", task_id="t").text == "ok"


def test_missing_backends_file(store):
    with pytest.raises(FactoryConfigError, match="backends.yaml is missing"):
        build_bridge(store, "planner", env={})


def test_missing_role(store):
    _write(store, {"agent": _backend()})
    with pytest.raises(FactoryConfigError, match=r"no 'planner' entry \(has: \['agent'\]\)"):
        build_bridge(store, "planner", env={VAR: SECRET})


def test_unknown_role(store):
    with pytest.raises(FactoryConfigError, match="unknown bridge role"):
        build_bridge(store, "judge", env={})


def test_missing_key(store):
    raw = _backend()
    del raw["model_name"]
    _write(store, {"planner": raw})
    with pytest.raises(FactoryConfigError, match="missing key 'model_name'"):
        build_bridge(store, "planner", env={VAR: SECRET})


@pytest.mark.parametrize("bad", [{"command": []}, {"timeout_s": 0}, {"token_env": ""},
                                 {"retry_policy": {"max_attempts": 0}}])
def test_invalid_fields(store, bad):
    _write(store, {"planner": _backend(**bad)})
    with pytest.raises(FactoryConfigError, match="'planner'"):
        build_bridge(store, "planner", env={VAR: SECRET})


def test_api_form_is_not_implemented(store):
    _write(store, {"planner": _backend(invocation_form="api")})
    with pytest.raises(FactoryConfigError, match="only 'cli'"):
        build_bridge(store, "planner", env={VAR: SECRET})


def test_missing_credential_names_the_variable_only(store):
    _write(store, {"planner": _backend()})
    with pytest.raises(FactoryConfigError) as exc:
        build_bridge(store, "planner", env={VAR: ""})
    assert VAR in str(exc.value)
    assert exc.value.__cause__ is None


def test_a_credential_value_written_into_the_tree_is_refused_without_echoing_it(store):
    raw = _backend()
    raw["token"] = SECRET
    env = Envelope(id="backends", type="config", version=1, created_at="t", created_by=OwnerRef(),
                   status="effective", schema_version=1, payload={"planner": raw})
    store.write_product(backends_config_file(), env, "hand-written leak")
    with pytest.raises(FactoryConfigError) as exc:
        build_bridge(store, "planner", env={VAR: SECRET})
    assert "token" in str(exc.value)
    assert SECRET not in str(exc.value)
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_factory_bridges.py`
Expected:收集阶段报错 `ModuleNotFoundError: No module named 'factory.bridges'`,`1 error`。

- [ ] **Step 3: 写实现**

新建 `src/factory/bridges.py`:

```python
"""桥接装配:从 control-plane/config/backends.yaml(配置信封)按角色取 BackendConfig,构造 CliBridge。

backends.yaml 的载荷形状(本计划裁决):{<角色>: <BackendConfig 原始字典>},角色 V1 有
planner(规划后端)与 agent(技能提案生成器)。缺文件、缺角色、字段不合法、缺凭证都抛
FactoryConfigError,信息里只有路径、角色、键名与变量名,不含凭证值。
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from controlplane.config import (
    CredentialInGitTreeError,
    MissingCredentialError,
    load_backend_config,
    read_config,
)
from controlplane.gitstore import GitStore
from controlplane.paths import backends_config_file
from factory.errors import FactoryConfigError
from inference_bridge import CliBridge

BRIDGE_ROLES: tuple[str, ...] = ("planner", "agent")


def build_bridge(
    store: GitStore,
    role: str,
    *,
    env: Mapping[str, str] | None = None,
    log_dir: str | Path | None = None,
) -> CliBridge:
    """按角色构造命令行桥接。V1 只有命令行形态(R15):invocation_form 不是 cli 直接报错。"""
    if role not in BRIDGE_ROLES:
        raise FactoryConfigError(f"unknown bridge role {role!r}; choose from {list(BRIDGE_ROLES)}")
    rel = backends_config_file()
    try:
        payload = read_config(store, rel)
    except FileNotFoundError:
        raise FactoryConfigError(
            f"{rel} is missing; the {role!r} bridge needs a backend entry there"
        ) from None
    except CredentialInGitTreeError as exc:
        # 原信息只含键路径,不含值(计划 1 的 _check_no_secrets)
        raise FactoryConfigError(str(exc)) from None
    if role not in payload:
        raise FactoryConfigError(f"{rel} has no {role!r} entry (has: {sorted(payload)})")
    raw = payload[role]
    if not isinstance(raw, dict):
        raise FactoryConfigError(f"{rel}: {role!r} must be a mapping, got {type(raw).__name__}")
    try:
        cfg = load_backend_config(raw)
    except KeyError as exc:
        raise FactoryConfigError(f"{rel}: {role!r} is missing key {exc.args[0]!r}") from None
    except (ValueError, CredentialInGitTreeError) as exc:
        raise FactoryConfigError(f"{rel}: {role!r}: {exc}") from None
    if cfg.invocation_form != "cli":
        raise FactoryConfigError(
            f"{rel}: {role!r} uses invocation_form {cfg.invocation_form!r}; "
            "only 'cli' is implemented in V1 (R15)"
        )
    try:
        return CliBridge(
            cfg, store=store, log_dir=log_dir, env=dict(os.environ if env is None else env),
        )
    except MissingCredentialError as exc:
        # 信息只报变量名(controlplane.config.resolve_credential)
        raise FactoryConfigError(str(exc)) from None
    except ValueError as exc:
        raise FactoryConfigError(f"{rel}: {role!r}: {exc}") from None
```

- [ ] **Step 4: 跑测试,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_factory_bridges.py`
Expected:`14 passed`。

Run: 全量命令。
Expected:`B+43 passed`,无警告。

- [ ] **Step 5: 提交**

```bash
git add src/factory/bridges.py
git add tests/test_factory_bridges.py
git commit -m "feat: factory builds CLI bridges from backends.yaml and fails loudly without leaking credentials"
```

---

### Task 103: 评估器装配与默认资产

**Files:**
- Create: `src/factory/evaluation.py`、`src/factory/defaults.py`
- Test: `tests/test_factory_evaluation.py`(12 个用例)

**Interfaces:**
- Consumes: `diagnostic.evaluator.make_diagnostic_evaluator`、`diagnostic.assets.*`(计划 8 任务 94、95);`MockSoMEvalRunner`、`MockJudgeBackend`(现有);`controlplane.config.read_config`、`write_config`(计划 1);`trainplanner.templates.install_default_template`(计划 4 任务 53)。
- Produces: `EVALUATOR_CONFIG = "control-plane/config/evaluator.yaml"`、`EVALUATOR_KEYS`、`read_evaluator_config(store) -> dict`、`build_mock_evaluator(store) -> Evaluator`;`DEFAULT_TAXONOMY`、`DEFAULT_ITEM_POOL`、`DEFAULT_EVALUATOR_CONFIG`、`install_defaults(store) -> list[str]`。

**设计要点:**
- 配置形状与 `backends` 恒为 `mock` 见 Ruling 4;mock 运行器的答案(基题对、变体错)见 Ruling 5,测试断言一级分数严格在 0 与 1 之间、错误画像是 `{"surface_match": 1.0}`;默认资产与 `install-defaults` 见 Ruling 6。默认的 `factory-tax-v1`、`factory-pool-v1`、`factory-bench-v1` 三个版本是成套的:换掉分类表或题库时,`benchmark_version` 也要一起换(Ruling 4)。
- **启动时检查资产**(Ruling 7):`build_mock_evaluator` 先读一次分类表与题库,缺了抛 `FactoryConfigError`;返回的评估器每次调用仍由计划 8 重读。
- `install_defaults` 第二次运行不改任何文件(逐字节比较),已存在的 `evaluator.yaml` 不被覆盖。它也安装规划提示模板:模板版本 `v1`(文件 `control-plane/config/prompts/planner-v1.yaml`,计划 4);任务 107 再加上计划 5 的 `install_default_configs` 与 MindAgent 的部署素材。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_factory_evaluation.py`:

```python
"""评估器装配:mock 后端的真实诊断评估器,报告标 backends: mock(R17)。"""
import pytest

from controlplane.config import write_config
from controlplane.evidence import diagnostic_grade
from controlplane.gitstore import GitStore
from controlplane.paths import diagnostic_report_file, taxonomy_file
from factory.defaults import DEFAULT_EVALUATOR_CONFIG, install_defaults
from factory.errors import FactoryConfigError
from factory.evaluation import EVALUATOR_CONFIG, EVALUATOR_KEYS, build_mock_evaluator, read_evaluator_config


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path / "cp"))
    s.init()
    return s


def test_install_defaults_writes_assets_config_and_template_and_is_idempotent(store):
    paths = install_defaults(store)
    assert EVALUATOR_CONFIG in paths
    assert "control-plane/config/prompts/planner-v1.yaml" in paths
    assert all(store.exists(p) for p in paths)
    before = {p: (store.repo_root / p).read_bytes() for p in paths}
    assert install_defaults(store) == paths
    assert {p: (store.repo_root / p).read_bytes() for p in paths} == before


def test_install_defaults_keeps_a_hand_edited_evaluator_config(store):
    edited = dict(DEFAULT_EVALUATOR_CONFIG, sampling_k=2)
    write_config(store, EVALUATOR_CONFIG, "evaluator", edited, "human: k=2")
    install_defaults(store)
    assert read_evaluator_config(store)["sampling_k"] == 2


def test_the_default_evaluator_writes_a_mock_graded_report(store):
    install_defaults(store)
    build_mock_evaluator(store)(store, "run-001", "cand-run-001")
    payload = store.read_product(diagnostic_report_file("run-001")).payload
    assert payload["backends"] == "mock"
    assert "stub" not in payload
    assert diagnostic_grade(payload) == "mock"
    assert payload["benchmark_version"] == DEFAULT_EVALUATOR_CONFIG["benchmark_version"]
    (l1,) = [d for d in payload["dimension_scores"] if d["level"] == "l1"]
    assert 0.0 < l1["score"] < 1.0  # 基题对、变体错
    assert [p["error_type_distribution"] for p in payload["error_profiles"]] == [{"surface_match": 1.0}]


def test_missing_config_is_a_clear_error(store):
    with pytest.raises(FactoryConfigError, match="evaluator.yaml is missing"):
        build_mock_evaluator(store)


@pytest.mark.parametrize("change", [
    {"drop": "tier"}, {"extra": "backends"}, {"set": ("tier", "slow")},
    {"set": ("sampling_k", 0)}, {"set": ("sampling_k", True)}, {"set": ("benchmark_version", "")},
])
def test_invalid_config_is_rejected(store, change):
    payload = dict(DEFAULT_EVALUATOR_CONFIG)
    if "drop" in change:
        del payload[change["drop"]]
    elif "extra" in change:
        payload[change["extra"]] = "real"
    else:
        payload[change["set"][0]] = change["set"][1]
    write_config(store, EVALUATOR_CONFIG, "evaluator", payload, "bad")
    with pytest.raises(FactoryConfigError, match="evaluator.yaml"):
        build_mock_evaluator(store)


def test_the_config_keys_are_fixed():
    assert EVALUATOR_KEYS == ("taxonomy_version", "item_pool_version", "benchmark_version", "tier", "sampling_k")
    assert set(DEFAULT_EVALUATOR_CONFIG) == set(EVALUATOR_KEYS)


def test_missing_assets_fail_at_assembly_not_at_the_first_candidate(store):
    install_defaults(store)
    (store.repo_root / taxonomy_file(DEFAULT_EVALUATOR_CONFIG["taxonomy_version"])).unlink()
    with pytest.raises(FactoryConfigError, match="diagnostic assets"):
        build_mock_evaluator(store)
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_factory_evaluation.py`
Expected:收集阶段报错 `ModuleNotFoundError: No module named 'factory.defaults'`,`1 error`。

- [ ] **Step 3: 写实现**

新建 `src/factory/evaluation.py`:

```python
"""评估器装配(闭环集成设计 §10.2、R17):V1 注入 make_diagnostic_evaluator + mock 后端。

评估器配置是信封 control-plane/config/evaluator.yaml,载荷恰好五个键:
taxonomy_version、item_pool_version、benchmark_version、tier、sampling_k。
backends 标记不进配置,恒为 "mock":V1 只有 MockSoMEvalRunner 与 MockJudgeBackend,
标成 real 就是谎报证据等级。
"""
from __future__ import annotations

from collections.abc import Callable

from controlplane.config import read_config
from controlplane.gitstore import GitStore
from controlplane.paths import CONTROL_PLANE_ROOT
from diagnostic.assets import DiagnosticAssetError, read_item_pool, read_taxonomy
from diagnostic.evaluator import make_diagnostic_evaluator
from diagnostic.scorer import MockJudgeBackend
from diagnostic.someval_adapter import MockSoMEvalRunner
from factory.errors import FactoryConfigError

EVALUATOR_CONFIG = f"{CONTROL_PLANE_ROOT}/config/evaluator.yaml"
EVALUATOR_KEYS = ("taxonomy_version", "item_pool_version", "benchmark_version", "tier", "sampling_k")
MOCK_JUDGE_ID = "factory-mock-judge"
MOCK_WRONG = "factory-mock-wrong-answer"

Evaluator = Callable[[GitStore, str, str], None]  # 形状同 controlplane.daemon.Evaluator


def read_evaluator_config(store: GitStore) -> dict:
    try:
        payload = read_config(store, EVALUATOR_CONFIG)
    except FileNotFoundError:
        raise FactoryConfigError(
            f"{EVALUATOR_CONFIG} is missing; closed-loop mode needs it to build the diagnostic evaluator"
        ) from None
    keys = set(payload)
    if keys != set(EVALUATOR_KEYS):
        raise FactoryConfigError(
            f"{EVALUATOR_CONFIG} must have exactly the keys {list(EVALUATOR_KEYS)}; "
            f"missing {sorted(set(EVALUATOR_KEYS) - keys)}, unknown {sorted(keys - set(EVALUATOR_KEYS))}"
        )
    for key in ("taxonomy_version", "item_pool_version", "benchmark_version"):
        if not isinstance(payload[key], str) or not payload[key]:
            raise FactoryConfigError(f"{EVALUATOR_CONFIG}: {key} must be a non-empty string")
    if payload["tier"] not in ("full", "quick"):
        raise FactoryConfigError(f"{EVALUATOR_CONFIG}: tier must be 'full' or 'quick', got {payload['tier']!r}")
    k = payload["sampling_k"]
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise FactoryConfigError(f"{EVALUATOR_CONFIG}: sampling_k must be an int >= 1, got {k!r}")
    return payload


def build_mock_evaluator(store: GitStore) -> Evaluator:
    """启动时读配置与资产(缺了立刻 FactoryConfigError,不等到第一个候选);返回的评估器每次调用
    仍按计划 8 的约定从树里重读资产。

    mock SoMEval 的答案(Ruling 5):变体族的基题恒答参考答案,变体题恒答错,其余题恒答对,
    各 sampling_k 次。所以每个变体族都被归为 surface_match,错误画像里没有空标签。"""
    cfg = read_evaluator_config(store)
    try:
        read_taxonomy(store, cfg["taxonomy_version"])
        pool = read_item_pool(store, cfg["item_pool_version"])
    except DiagnosticAssetError as exc:
        raise FactoryConfigError(f"diagnostic assets: {exc}") from None
    variants = {v for fam in pool.variant_families for v in fam.variant_item_ids}
    fixed = {
        item.id: [MOCK_WRONG if item.id in variants else (item.reference_answer or "")] * cfg["sampling_k"]
        for item in pool.items
    }
    return make_diagnostic_evaluator(
        taxonomy_version=cfg["taxonomy_version"],
        item_pool_version=cfg["item_pool_version"],
        someval_runner=MockSoMEvalRunner(fixed=fixed),
        judge_backend=MockJudgeBackend(model_id=MOCK_JUDGE_ID, fixed={}),
        benchmark_version=cfg["benchmark_version"],
        tier=cfg["tier"],
        sampling_k=cfg["sampling_k"],
        backends="mock",
    )
```

新建 `src/factory/defaults.py`:

```python
"""闭环模式的默认资产与配置:诊断分类表、题库、评估器配置、规划提示模板。

全部经 GitStore / write_config 写入,重复安装是无操作(内容相同不提交)。
分类表与题库是最小的 V1 夹具,与计划 8 任务 95 的测试资产同形;真实题库替换它们即可。
"""
from __future__ import annotations

from controlplane.config import write_config
from controlplane.gitstore import GitStore
from diagnostic.assets import write_item_pool, write_taxonomy
from diagnostic.datamodel import DiagnosticItem, DiagnosticTaxonomy, VariantFamily
from diagnostic.taxonomy_loader import ItemPool
from factory.evaluation import EVALUATOR_CONFIG
from trainplanner.templates import install_default_template

DEFAULT_TAXONOMY = DiagnosticTaxonomy(
    "factory-tax-v1",
    ["mental_representation"],
    {"mental_representation": ["belief"]},
    {"belief": ["false_belief_transfer"]},
    ["format_compliance"],
)
DEFAULT_ITEM_POOL = ItemPool(
    "factory-pool-v1",
    [
        DiagnosticItem("fi1", "false_belief_transfer", "mcq", "rule", "static", "B", None, "fvf1"),
        DiagnosticItem("fi1v", "false_belief_transfer", "mcq", "rule", "static", "C", None, "fvf1"),
    ],
    [VariantFamily("fvf1", "fi1", ["fi1v"])],
)
DEFAULT_EVALUATOR_CONFIG = {
    "taxonomy_version": DEFAULT_TAXONOMY.version,
    "item_pool_version": DEFAULT_ITEM_POOL.version,
    "benchmark_version": "factory-bench-v1",
    "tier": "full",
    "sampling_k": 4,
}


def install_defaults(store: GitStore) -> list[str]:
    """写入默认资产与配置,返回写到的相对路径(已存在且相同的也列出)。
    已存在的 evaluator.yaml 不覆盖:人改过的配置优先。"""
    paths = [write_taxonomy(store, DEFAULT_TAXONOMY), write_item_pool(store, DEFAULT_ITEM_POOL)]
    if not store.exists(EVALUATOR_CONFIG):
        write_config(store, EVALUATOR_CONFIG, "evaluator", dict(DEFAULT_EVALUATOR_CONFIG),
                     "factory: install default evaluator config")
    paths.append(EVALUATOR_CONFIG)
    paths.append(install_default_template(store))
    return paths
```

- [ ] **Step 4: 跑测试,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_factory_evaluation.py`
Expected:`12 passed`。

Run: 全量命令。
Expected:`B+55 passed`,无警告。

- [ ] **Step 5: 提交**

```bash
git add src/factory/evaluation.py
git add src/factory/defaults.py
git add tests/test_factory_evaluation.py
git commit -m "feat: factory assembles the mock-backed diagnostic evaluator and installs default assets"
```

---

### Task 104: 组合根 `factory.wire` 与命令行 `factory.cli`

**Files:**
- Create: `src/factory/wire.py`、`src/factory/cli.py`、`src/factory/handlers.py`、`scripts/run_factory.py`、`scripts/run_factory_dev.py`、`tests/factory_fixtures.py`
- Test: `tests/test_factory_wire.py`(18 个用例)、`tests/test_factory_cli.py`(9 个用例)

**Interfaces:**
- Consumes: 任务 100–103 的全部名字;`controlplane.daemon.run_daemon`、`reconcile_once`、`Dispatcher`、`Handler`、`Evaluator`(计划 8 任务 93);`controlplane.lock.acquire_repo_lock`、`RepoLockedError`(**计划 8** 任务 98);`mindtrainer.dispatch.make_mindtrainer_dispatcher(data_producer=...)`(计划 2);`TemplateDataProducer`(计划 2);`mindtrainer.wire`(现有,只在测试里做对比)。
- Produces:
  - `FactoryOptions(switches=(), allow_mock_evidence=False, confirm_directives=None, planner_backend="bridge", repo_commit=None)`;`CODE_ROOT`、`resolve_repo_commit(override, *, code_root=CODE_ROOT) -> str | None`(Ruling 20)。
  - `BuildContext(store, accept_mock_evidence, confirm_directives, planner_backend, env, log_dir, repo_commit=None, closers=[])`,方法 `bridge(role) -> CliBridge`、`register_closer(close)`。
  - `HandlerBuilder = Callable[[BuildContext], Handler]`;`Assembly(dispatcher, handlers, evaluator, accept_mock_evidence, closers=())`,方法 `close()`;`run` 与 `reconcile_one_tick` 结束时(含异常)调 `close()`,构造器中途失败时已登记的收尾函数立刻调用。
  - `assemble(store, options, *, env=None, log_dir=None, builders=None) -> Assembly`;`run(repo_root, options=FactoryOptions(), *, interval_s=2.0, max_ticks=None, env=None, log_dir=None, builders=None)`;`reconcile_one_tick(repo_root, options=FactoryOptions(), *, env=None, log_dir=None, builders=None) -> list[Action]`。
  - `factory.cli.build_parser()`、`main(argv=None, *, env=None, builders=None) -> int`、`dev_main(argv=None, **kw) -> int`;`main` 把 `RepoLockedError` 翻成一行 `factory: error: repository is locked by another process: <锁文件路径>` 与退出码 2。
  - `factory.handlers.DEFAULT_BUILDERS`(本任务为空表,任务 107 填满)。

**设计要点:**
- **两条路径**见 Ruling 2、3;**装配失败**见 Ruling 7;**构造器表**见 Ruling 16;**仓库锁**见 Ruling 27。
- **仓库锁(设计 CP-12、Ruling 27)**:`run` 的锁由 `controlplane.daemon.run_daemon` 在第一个 tick 之前取(计划 8 任务 98),`factory` 不再重复取;`reconcile_one_tick` 在装配之前用 `acquire_repo_lock(repo_root)` 取同一把锁——一次性 tick 也是一次写入,同样要挡住第二个写者。`factory.cli.main` 捕获 `RepoLockedError` 打一行错误并返回 2;只读子命令 `trace`、`cost`、`logs` 不取锁,被占用时照常输出。锁必须**不可重入**:同进程第二次取同一把锁也要失败,否则测试里的"持锁再调 main"证明不了任何事(若计划 8 的实现按进程判重入,以它的测试为准并改这两条)。
- **锁文件在 `.git/` 下**(计划 8 任务 98),所以第 1 条"逐字节相同"的比较(`control-plane/` 下的产物与提交说明)不受影响,`git status` 也不会看到它。
- **逐字节相同的证明**:两棵从同一份拷贝复制出来的仓库(含 `.git`,有一条 pending 指令和它的两份清单),一棵跑 `mindtrainer.wire.run`(或 `main`),一棵跑 `factory.cli.main(["run", ...])`,比较整棵 `control-plane/` 下的所有文件内容与 `git log --format=%s` 的全部提交说明。三个 tick 走到候选、诊断、待审;再人工批准、各跑一轮,走到晋级。`--allow-mock-evidence`、`--no-confirm-directives` 单独出现时也逐字节相同。
- **不读配置**:默认路径下树里没有 `evaluator.yaml` 也照跑,诊断仍是桩(`stub: True`);带 `data_spec` 而缺清单的指令照旧判 `failed` 并点名 `data_producer`(与计划 2 的已知限制第 8 条一致)。
- **闭环模式注入**:用一条带 `data_spec`、没有清单的指令,直接调 `assemble(...).dispatcher`,证明数据生产器被注入、训练出候选;`BuildContext` 把三个开关原样交给构造器。
- **命令行**:三个只读子命令输出与 `controlplane.observe` 逐字相同,`scripts/run_factory.py` 真起子进程跑一次 `trace`。
- `tests/factory_fixtures.py` 不以 `test_` 开头,pytest 不收集它。测试文件用 `from factory_fixtures import ...` 导入:pytest 默认的 `prepend` 导入模式会把测试文件所在目录(没有 `__init__.py` 的 `tests/`)插进 `sys.path`,不需要改 `pyproject.toml` 的 `pythonpath`(已在拷贝里实测)。

- [ ] **Step 1: 写测试辅助模块与失败测试**

新建 `tests/factory_fixtures.py`:

```python
"""factory 测试共用的仓库搭建与假处理器(不是测试文件,pytest 不收集)。"""
from __future__ import annotations

import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

from controlplane.envelope import BigRef, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import candidate_checkpoint_file, run_dir
from factory.defaults import install_defaults
from mindtrainer.data_producer import TemplateDataProducer
from mindtrainer.data_spec import parse_data_spec
from mindtrainer.training_config import RewardConfig, load_config_from_directive

BASE = BigRef("file:///ckpt/base", "a" * 64, 1)
TEACHER = BigRef("file:///ckpt/teacher", "b" * 64, 1)
REWARD = RewardConfig(["goal"], "outcome_split", "sum", {"goal": ["agreed"]})
DATA_SPEC = {"scenario_count": 2, "seed": 0, "purpose": "training",
             "stages": {"sft": {"group_size": 1}, "grpo_opd": {"group_size": 2}}}


def training_config(run_id: str) -> dict:
    return {
        "mode": "batch", "trained_slot": "a",
        "base_checkpoint_ref": BASE.to_dict(), "teacher_model_ref": TEACHER.to_dict(),
        "reward": REWARD.to_dict(), "sft_steps": 1, "grpo_steps": 1, "sft_batch_size": 1,
        "groups_per_step": 1, "group_size": 2, "grpo_config": {}, "opd_coefficient": 0.1,
        "checkpoint_every_steps": 1, "allow_resume": True,
        "stage_manifest_refs": {"sft": f"dsm-{run_id}-sft", "grpo_opd": f"dsm-{run_id}-grpo_opd"},
    }


def write_directive(store: GitStore, run_id: str, *, data_spec: dict | None = None,
                    status: str = "pending", dimension: str = "tom") -> Envelope:
    payload = {"target_dimension": dimension, "recipe_ref": "", "training_config": training_config(run_id)}
    if data_spec is not None:
        payload["data_spec"] = data_spec
    env = Envelope(id=run_id, type="directive", version=1, created_at="2026-10-09T00:00:00Z",
                   created_by=OwnerRef(), status=status, schema_version=1, payload=payload)
    store.write_product(f"{run_dir(run_id)}/directive.yaml", env, "human: write directive")
    return env


def produce_datasets(store: GitStore, directive: Envelope) -> None:
    """用默认生产器把指令点名的两份清单先写进树(“带数据集清单的仓库”)。"""
    TemplateDataProducer().produce(
        store, directive.id, load_config_from_directive(directive), parse_data_spec(DATA_SPEC)
    )


def closed_loop_repo(root: str, *, run_id: str | None = "run-001", with_datasets: bool = True) -> GitStore:
    """一棵装好默认资产与配置的仓库;run_id 非空时再放一条 pending 指令(及其数据集清单)。"""
    store = GitStore(root)
    store.init()
    install_defaults(store)
    if run_id is not None:
        env = write_directive(store, run_id)
        if with_datasets:
            produce_datasets(store, env)
    return store


def approve(store: GitStore, run_id: str) -> None:
    rel = candidate_checkpoint_file(run_id)
    cand = store.read_product(rel)
    cand.status = "approved"
    store.write_product(rel, cand, f"human: approve {cand.id}")


def git_head(store: GitStore) -> str:
    return subprocess.run(["git", "-C", str(store.repo_root), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()


def git_log_messages(store: GitStore) -> list[str]:
    out = subprocess.run(["git", "-C", str(store.repo_root), "log", "--format=%s"],
                         capture_output=True, text=True, check=True).stdout
    return out.splitlines()


def tree_files(store: GitStore) -> dict[str, bytes]:
    root = store.repo_root / "control-plane"
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


@contextmanager
def held_lock(repo_root):
    """另起一个进程持有仓库排他锁(跨进程才真的互斥,与锁用 flock 还是 lockf 无关);退出时放锁。"""
    src = Path(__file__).resolve().parents[1] / "src"
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(src)!r})\n"
        "from controlplane.lock import acquire_repo_lock\n"
        f"with acquire_repo_lock({str(repo_root)!r}):\n"
        "    print('locked', flush=True)\n"
        "    sys.stdin.read()\n"
    )
    # with Popen:退出时关闭并回收两条管道;裸 Popen 会留下未关闭的文件对象,在 -W error 下变成 ResourceWarning
    with subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as proc:
        try:
            assert proc.stdout.readline().strip() == "locked"
            yield
        finally:
            proc.stdin.close()
            proc.wait(timeout=10)


class Recorder:
    """假处理器:记下 (动作名, target_id),不写任何东西。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def handler(self, store, action) -> None:
        self.calls.append((action.kind, action.target_id))


def recording_builders(recorder: Recorder, keys=None, seen_contexts: list | None = None) -> dict:
    from controlplane.reconcile import CLOSED_LOOP_ACTIONS

    def build(ctx):
        if seen_contexts is not None:
            seen_contexts.append(ctx)
        return recorder.handler

    return {k: build for k in (CLOSED_LOOP_ACTIONS if keys is None else keys)}
```

新建 `tests/test_factory_wire.py`:

```python
"""组合根:不传 --enable 时与 mindtrainer.wire 逐字节相同;传了才注入评估器、数据生产器与处理器。"""
import shutil

import pytest

from controlplane.daemon import reconcile_once
from controlplane.gitstore import GitStore
from controlplane.paths import candidate_checkpoint_file, diagnostic_report_file, run_dir
from controlplane.reconcile import CLOSED_LOOP_ACTIONS
from factory.cli import build_parser, dev_main, main
from factory.evaluation import EVALUATOR_CONFIG
from factory.wire import FactoryOptions, assemble
from factory_fixtures import (
    DATA_SPEC, Recorder, closed_loop_repo, git_head, git_log_messages, held_lock, produce_datasets,
    recording_builders, tree_files, write_directive,
)
from mindtrainer import wire as mt_wire
from mindtrainer.dispatch import make_mindtrainer_dispatcher

RUN = "run-001"


@pytest.fixture
def twin(tmp_path):
    """两棵逐字节相同的仓库(含 .git):一条 pending 指令,数据集清单已在树里,没有任何默认配置。"""
    a = GitStore(str(tmp_path / "a"))
    a.init()
    produce_datasets(a, write_directive(a, RUN))
    shutil.copytree(tmp_path / "a", tmp_path / "b")
    return a, GitStore(str(tmp_path / "b"))


def _same(a, b):
    assert tree_files(a) == tree_files(b)
    assert git_log_messages(a) == git_log_messages(b)


def test_without_enable_run_is_byte_identical_to_mindtrainer_wire(twin):
    a, b = twin
    mt_wire.run(str(a.repo_root), interval_s=0, max_ticks=3)
    assert main(["run", str(b.repo_root), "--interval-s", "0", "--max-ticks", "3"]) == 0
    assert a.exists(diagnostic_report_file(RUN))
    _same(a, b)


def test_without_enable_the_entry_scripts_match_through_promotion(twin, capsys):
    a, b = twin
    assert mt_wire.main([str(a.repo_root), "--interval-s", "0", "--max-ticks", "3"]) == 0
    assert main(["run", str(b.repo_root), "--interval-s", "0", "--max-ticks", "3"]) == 0
    for store in (a, b):
        cand = store.read_product(candidate_checkpoint_file(RUN))
        cand.status = "approved"
        store.write_product(candidate_checkpoint_file(RUN), cand, "human: approve")
    reconcile_once(a, dispatcher=make_mindtrainer_dispatcher())
    assert main(["reconcile", str(b.repo_root), "--once"]) == 0
    assert capsys.readouterr().out == f"promote_candidate cand-{RUN}\n"
    _same(a, b)


def test_without_enable_stub_diagnostics_stay_stub_and_no_config_is_read(twin):
    _, b = twin
    assert not b.exists(EVALUATOR_CONFIG)
    assert main(["run", str(b.repo_root), "--interval-s", "0", "--max-ticks", "2"]) == 0
    assert b.read_product(diagnostic_report_file(RUN)).payload["stub"] is True


def test_the_default_assembly_injects_nothing_but_the_dispatcher(twin):
    a, _ = twin
    asm = assemble(a, FactoryOptions())
    assert (asm.handlers, asm.evaluator, asm.accept_mock_evidence, asm.closed_loop) == ({}, None, False, False)


def test_allow_mock_and_confirm_flags_alone_change_nothing(twin):
    a, b = twin
    mt_wire.run(str(a.repo_root), interval_s=0, max_ticks=2)
    assert main(["run", str(b.repo_root), "--interval-s", "0", "--max-ticks", "2",
                 "--allow-mock-evidence", "--no-confirm-directives"]) == 0
    _same(a, b)


def test_without_a_data_producer_a_data_spec_directive_fails_in_default_mode(tmp_path):
    store = GitStore(str(tmp_path / "cp"))
    store.init()
    write_directive(store, RUN, data_spec=DATA_SPEC)
    assert main(["reconcile", str(store.repo_root), "--once"]) == 0
    d = store.read_product(f"{run_dir(RUN)}/directive.yaml")
    assert d.status == "failed" and "data_producer" in d.payload["failure"]["reason"]


# ---- 闭环模式的装配 ----

def _closed(tmp_path, **kw):
    return closed_loop_repo(str(tmp_path / "cp"), **kw)


def test_enabled_assembly_injects_evaluator_handlers_and_data_producer(tmp_path):
    store = _closed(tmp_path, run_id=None)
    write_directive(store, RUN, data_spec=DATA_SPEC)  # 没有清单:要靠注入的数据生产器
    rec, seen = Recorder(), []
    asm = assemble(store, FactoryOptions(switches=("admit",), allow_mock_evidence=True,
                                         confirm_directives=False, planner_backend="heuristic"),
                   env={}, builders=recording_builders(rec, seen_contexts=seen))
    assert set(asm.handlers) == {"admit_recipe", "admit_proposal"}
    assert asm.evaluator is not None and asm.accept_mock_evidence is True
    assert [(c.accept_mock_evidence, c.confirm_directives, c.planner_backend) for c in seen] == \
        [(True, False, "heuristic")] * 2
    asm.dispatcher(store, store.read_product(f"{run_dir(RUN)}/directive.yaml"))
    assert store.read_product(candidate_checkpoint_file(RUN)).status == "pending_review"


def test_only_the_mapped_keys_are_built(tmp_path):
    store = _closed(tmp_path, run_id=None)
    rec = Recorder()
    for switches, keys in ((("plan",), {"plan_next_round"}), (("track", "nominate"), {"record_track", "nominate_recipe"}),
                           (("plan", "nominate", "track", "agent", "admit"), set(CLOSED_LOOP_ACTIONS))):
        asm = assemble(store, FactoryOptions(switches=switches), env={}, builders=recording_builders(rec))
        assert set(asm.handlers) == keys


def test_a_missing_builder_is_an_assembly_error(tmp_path, capsys):
    store = _closed(tmp_path, run_id=None)
    rec = Recorder()
    code = main(["reconcile", str(store.repo_root), "--once", "--enable", "plan,agent"],
                env={}, builders=recording_builders(rec, keys=["plan_next_round"]))
    assert code == 2
    assert "no handler builder for ['start_agent_round']" in capsys.readouterr().err


def test_closed_loop_mode_without_evaluator_config_exits_2_and_commits_nothing(tmp_path, capsys):
    store = GitStore(str(tmp_path / "cp"))
    store.init()
    write_directive(store, RUN)
    head = git_head(store)
    code = main(["run", str(store.repo_root), "--enable", "plan", "--max-ticks", "1"],
                env={}, builders=recording_builders(Recorder()))
    assert code == 2
    assert "evaluator.yaml is missing" in capsys.readouterr().err
    assert git_head(store) == head


def test_unknown_switch_exits_2(tmp_path, capsys):
    store = _closed(tmp_path, run_id=None)
    assert main(["reconcile", str(store.repo_root), "--once", "--enable", "plan,nominatee"]) == 2
    assert "nominatee" in capsys.readouterr().err


def test_reconcile_requires_once(tmp_path):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["reconcile", str(tmp_path)])


def test_flag_defaults():
    args = build_parser().parse_args(["run", "/r"])
    assert (args.enable, args.allow_mock_evidence, args.confirm_directives, args.planner_backend,
            args.interval_s, args.max_ticks) == (None, False, None, "bridge", 2.0, None)
    assert build_parser().parse_args(["run", "/r", "--confirm-directives"]).confirm_directives is True
    assert build_parser().parse_args(["run", "/r", "--no-confirm-directives"]).confirm_directives is False


def test_dev_entry_turns_mock_evidence_on(tmp_path):
    store = _closed(tmp_path, run_id=None)
    seen = []
    assert dev_main(["reconcile", str(store.repo_root), "--once", "--enable", "plan"],
                    env={}, builders=recording_builders(Recorder(), seen_contexts=seen)) == 0
    assert seen[0].accept_mock_evidence is True
    seen.clear()
    assert main(["reconcile", str(store.repo_root), "--once", "--enable", "plan"],
                env={}, builders=recording_builders(Recorder(), seen_contexts=seen)) == 0
    assert seen[0].accept_mock_evidence is False


# ---- 仓库锁(设计 CP-12):run 的锁由 run_daemon 取,reconcile 的锁由 reconcile_one_tick 在装配之前取 ----

def _locked_message(capsys):
    err = capsys.readouterr().err
    assert err.startswith("factory: error: repository is locked by another process: ")
    assert "rismforge.lock" in err


def test_run_exits_2_when_the_repository_is_locked_and_writes_nothing(twin, capsys):
    a, _ = twin
    head, files = git_head(a), tree_files(a)
    with held_lock(a.repo_root):
        assert main(["run", str(a.repo_root), "--interval-s", "0", "--max-ticks", "2"]) == 2
    _locked_message(capsys)
    assert (git_head(a), tree_files(a)) == (head, files)


def test_reconcile_exits_2_when_the_repository_is_locked_and_writes_nothing(twin, capsys):
    a, _ = twin
    head, files = git_head(a), tree_files(a)
    with held_lock(a.repo_root):
        assert main(["reconcile", str(a.repo_root), "--once"]) == 2
    _locked_message(capsys)
    assert (git_head(a), tree_files(a)) == (head, files)


def test_closed_loop_reconcile_takes_the_lock_before_any_builder_runs(tmp_path, capsys):
    store = closed_loop_repo(str(tmp_path / "cp"))
    head, files, seen = git_head(store), tree_files(store), []
    with held_lock(store.repo_root):
        assert main(["reconcile", str(store.repo_root), "--once", "--enable", "plan"], env={},
                    builders=recording_builders(Recorder(), seen_contexts=seen)) == 2
    _locked_message(capsys)
    assert seen == []  # 锁在装配之前取:被占用时一个处理器构造器都没跑
    assert (git_head(store), tree_files(store)) == (head, files)


def test_the_lock_is_released_when_run_and_reconcile_finish(twin):
    a, _ = twin
    assert main(["run", str(a.repo_root), "--interval-s", "0", "--max-ticks", "1"]) == 0
    assert main(["reconcile", str(a.repo_root), "--once"]) == 0
    with held_lock(a.repo_root):  # 取得到,说明前两条没把锁留在身后
        pass
```

新建 `tests/test_factory_cli.py`:

```python
"""命令行的只读子命令:输出与 controlplane.observe 相同,退出码表达缺失,且从不提交。"""
import hashlib
import subprocess

import pytest

from controlplane.envelope import BigRef, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.ledger import record_cost, record_failure
from controlplane.observe import cost as observe_cost, trace as observe_trace
from controlplane.paths import run_dir
from factory.cli import main
from factory_fixtures import held_lock

RUN = "run-001"


def _snapshot(store):
    def git(*a):
        return subprocess.run(["git", "-C", str(store.repo_root), *a],
                              capture_output=True, text=True, check=True).stdout
    return git("rev-parse", "HEAD"), git("status", "--porcelain", "--untracked-files=all")


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path / "cp"))
    s.init()
    env = Envelope(id=RUN, type="directive", version=1, created_at="2026-10-09T00:00:00Z",
                   created_by=OwnerRef(), status="executed", schema_version=1,
                   payload={"target_dimension": "tom", "recipe_ref": ""})
    s.write_product(f"{run_dir(RUN)}/directive.yaml", env, "seed directive")
    record_cost(s, RUN, task_id="train", cost_type="gpu", gpu_hours=1.5)
    return s


def test_trace_prints_what_observe_prints(store, capsys):
    assert main(["trace", str(store.repo_root), RUN]) == 0
    assert capsys.readouterr().out == observe_trace(store, RUN) + "\n"


def test_cost_prints_what_observe_prints(store, capsys):
    assert main(["cost", str(store.repo_root), RUN]) == 0
    assert capsys.readouterr().out == observe_cost(store, RUN) + "\n"


def test_trace_of_a_missing_run_exits_1(store, capsys):
    assert main(["trace", str(store.repo_root), "run-404"]) == 1
    assert capsys.readouterr().out == "run run-404: no products found\n"


def test_cost_without_a_run_trace_exits_0(store, capsys):
    assert main(["cost", str(store.repo_root), "run-404"]) == 0
    assert capsys.readouterr().out == "run run-404: no run_trace (nothing recorded yet)\n"


def test_logs_with_task_filter(store, tmp_path, capsys):
    path = tmp_path / "plan-1-1.log"
    path.write_bytes(b"A-body\n")
    ref = BigRef(uri=f"file://{path}", sha256=hashlib.sha256(b"A-body\n").hexdigest(), size_bytes=7)
    record_failure(store, RUN, task_id="plan-1", category="T3", point="p", retries=0, reason="r", log_ref=ref)
    assert main(["logs", str(store.repo_root), RUN, "--task", "plan-1"]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"run {RUN} logs for task plan-1\n") and "A-body" in out


def test_logs_exit_1_when_a_reference_is_unreachable(store, capsys):
    ref = BigRef(uri="file:///compute/logs/x-1.log", sha256="a" * 64, size_bytes=1)
    record_failure(store, RUN, task_id="x", category="T1", point="p", retries=0, reason="r", log_ref=ref)
    assert main(["logs", str(store.repo_root), RUN]) == 1
    assert "[unreachable] file:///compute/logs/x-1.log" in capsys.readouterr().out


def test_the_read_only_commands_never_commit(store, capsys):
    ref = BigRef(uri="file:///nowhere.log", sha256="a" * 64, size_bytes=1)
    record_failure(store, RUN, task_id="x", category="T1", point="p", retries=0, reason="r", log_ref=ref)
    before = _snapshot(store)
    for argv in (["trace", RUN], ["cost", RUN], ["logs", RUN], ["logs", RUN, "--task", "x"],
                 ["trace", "run-404"], ["cost", "run-404"], ["logs", "run-404"]):
        main([argv[0], str(store.repo_root), *argv[1:]])
    capsys.readouterr()
    assert _snapshot(store) == before


def test_entry_script_runs_trace(store):
    import sys
    from pathlib import Path
    script = Path(__file__).resolve().parents[1] / "scripts" / "run_factory.py"
    out = subprocess.run([sys.executable, str(script), "trace", str(store.repo_root), RUN],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout == observe_trace(store, RUN) + "\n"


def test_the_read_only_commands_work_while_the_repository_is_locked(store, capsys):
    """只读子命令不取锁(设计 CP-12):另一个进程持锁时照常输出,不退出码 2,也不提交。"""
    before = _snapshot(store)
    with held_lock(store.repo_root):
        assert main(["trace", str(store.repo_root), RUN]) == 0
        assert capsys.readouterr().out == observe_trace(store, RUN) + "\n"
        assert main(["cost", str(store.repo_root), RUN]) == 0
        assert capsys.readouterr().out == observe_cost(store, RUN) + "\n"
        assert main(["logs", str(store.repo_root), RUN]) != 2
    assert "locked" not in capsys.readouterr().err
    assert _snapshot(store) == before
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_factory_wire.py tests/test_factory_cli.py`
Expected:收集阶段报错 `ModuleNotFoundError: No module named 'factory.cli'`,`2 errors`。

- [ ] **Step 3: 写实现**

新建 `src/factory/handlers.py`:

```python
"""六个处理器键的真实构造器表:{处理器键: (BuildContext) -> Handler}。

任务 104 先放一张空表,闭环开关在没有显式 builders 时一律报
"no handler builder for [...]" 并以退出码 2 退出;任务 107 按计划 3、5、7 的处理器填满它。
"""
from __future__ import annotations

from collections.abc import Callable, Mapping

DEFAULT_BUILDERS: Mapping[str, Callable] = {}
```

新建 `src/factory/wire.py`:

```python
"""组合根(闭环集成设计 §10.2)。

两条路径:
- 不传 --enable(switches 为空):与 mindtrainer.wire.run 完全相同,只注入
  make_mindtrainer_dispatcher(),不读任何配置,不注入评估器与数据生产器。
- 传了 --enable:启动时一次装配完(缺配置、缺资产、缺凭证立刻 FactoryConfigError),
  派发器带默认数据生产器,request_diagnostic 换成 mock 后端的真实评估器(报告标 mock),
  按开关映射出的处理器键逐个调用处理器构造器。
"""
from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from controlplane.daemon import Dispatcher, Evaluator, Handler, reconcile_once, run_daemon
from controlplane.gitstore import GitStore
from controlplane.lock import acquire_repo_lock
from controlplane.reconcile import Action
from factory.bridges import build_bridge
from factory.errors import FactoryConfigError
from factory.evaluation import build_mock_evaluator
from factory.switches import handler_keys
from inference_bridge import CliBridge
from mindtrainer.data_producer import TemplateDataProducer
from mindtrainer.dispatch import make_mindtrainer_dispatcher

PLANNER_BACKENDS = ("bridge", "heuristic")


@dataclass(frozen=True)
class FactoryOptions:
    switches: tuple[str, ...] = ()
    allow_mock_evidence: bool = False
    confirm_directives: bool | None = None  # None:用 scheduler.yaml 的 confirm_directives
    planner_backend: str = "bridge"
    repo_commit: str | None = None  # None:取代码仓库的 git rev-parse HEAD(取不到仍是 None)


CODE_ROOT = Path(__file__).resolve().parents[2]  # src/factory/wire.py -> 代码仓库根


def resolve_repo_commit(override: str | None, *, code_root: Path = CODE_ROOT) -> str | None:
    """--repo-commit 优先;否则 git -C <代码仓库根> rev-parse HEAD;不是 git 仓库或没有 git 时返回 None。"""
    if override is not None:
        return override
    try:
        out = subprocess.run(["git", "-C", str(code_root), "rev-parse", "HEAD"],
                             capture_output=True, text=True, check=False)
    except OSError:
        return None
    commit = out.stdout.strip()
    return commit if out.returncode == 0 and commit else None


@dataclass(frozen=True)
class BuildContext:
    """交给处理器构造器的装配上下文。bridge(role) 每次调用都新建一个 CliBridge。"""

    store: GitStore
    accept_mock_evidence: bool
    confirm_directives: bool | None
    planner_backend: str
    env: Mapping[str, str]
    log_dir: Path | None
    repo_commit: str | None = None
    closers: list = field(default_factory=list)

    def bridge(self, role: str) -> CliBridge:
        return build_bridge(self.store, role, env=self.env, log_dir=self.log_dir)

    def register_closer(self, close: Callable[[], None]) -> None:
        """构造器起了要收尾的资源(如 loopback 实时服务端)时登记;run 结束时逆序调用。"""
        self.closers.append(close)


HandlerBuilder = Callable[[BuildContext], Handler]


@dataclass(frozen=True)
class Assembly:
    dispatcher: Dispatcher
    handlers: dict[str, Handler]
    evaluator: Evaluator | None
    accept_mock_evidence: bool
    closers: tuple = ()

    def close(self) -> None:
        for close in reversed(self.closers):
            close()

    @property
    def closed_loop(self) -> bool:
        return bool(self.handlers)


def default_builders() -> Mapping[str, HandlerBuilder]:
    """六个处理器键的真实构造器,在 factory.handlers 里(依赖计划 3、5、7)。"""
    from factory.handlers import DEFAULT_BUILDERS

    return DEFAULT_BUILDERS


def assemble(
    store: GitStore,
    options: FactoryOptions,
    *,
    env: Mapping[str, str] | None = None,
    log_dir: str | Path | None = None,
    builders: Mapping[str, HandlerBuilder] | None = None,
) -> Assembly:
    if options.planner_backend not in PLANNER_BACKENDS:
        raise FactoryConfigError(
            f"planner backend must be one of {list(PLANNER_BACKENDS)}, got {options.planner_backend!r}"
        )
    keys = handler_keys(options.switches)
    if not keys:
        # 与 mindtrainer.wire.run 逐字节相同的路径:不读配置、不注入别的
        return Assembly(make_mindtrainer_dispatcher(), {}, None, False)
    table = default_builders() if builders is None else builders
    missing = sorted(keys - set(table))
    if missing:
        raise FactoryConfigError(f"no handler builder for {missing}")
    evaluator = build_mock_evaluator(store)
    ctx = BuildContext(
        store=store,
        accept_mock_evidence=options.allow_mock_evidence,
        confirm_directives=options.confirm_directives,
        planner_backend=options.planner_backend,
        env=dict(os.environ if env is None else env),
        log_dir=None if log_dir is None else Path(log_dir),
        repo_commit=resolve_repo_commit(options.repo_commit),
    )
    try:
        handlers = {key: table[key](ctx) for key in sorted(keys)}
    except BaseException:
        for close in reversed(ctx.closers):
            close()
        raise
    return Assembly(
        dispatcher=make_mindtrainer_dispatcher(data_producer=TemplateDataProducer()),
        handlers=handlers,
        evaluator=evaluator,
        accept_mock_evidence=options.allow_mock_evidence,
        closers=tuple(ctx.closers),
    )


def run(
    repo_root: str,
    options: FactoryOptions = FactoryOptions(),
    *,
    interval_s: float = 2.0,
    max_ticks: int | None = None,
    env: Mapping[str, str] | None = None,
    log_dir: str | Path | None = None,
    builders: Mapping[str, HandlerBuilder] | None = None,
) -> None:
    store = GitStore(repo_root)
    a = assemble(store, options, env=env, log_dir=log_dir, builders=builders)
    if not a.closed_loop:
        run_daemon(store, interval_s, max_ticks=max_ticks, dispatcher=a.dispatcher)
        return
    try:
        run_daemon(
            store, interval_s, max_ticks=max_ticks, dispatcher=a.dispatcher,
            handlers=a.handlers, evaluator=a.evaluator, accept_mock_evidence=a.accept_mock_evidence,
        )
    finally:
        a.close()


def reconcile_one_tick(
    repo_root: str,
    options: FactoryOptions = FactoryOptions(),
    *,
    env: Mapping[str, str] | None = None,
    log_dir: str | Path | None = None,
    builders: Mapping[str, HandlerBuilder] | None = None,
) -> list[Action]:
    store = GitStore(repo_root)
    # 设计 CP-12:一次性的 reconcile 也是一次写入,与 run_daemon 争同一把仓库排他锁(计划 8 任务 98)。
    # 锁在装配之前取:装配失败(退出码 2)时树里同样不写任何东西,with 会释放锁。
    with acquire_repo_lock(repo_root):
        a = assemble(store, options, env=env, log_dir=log_dir, builders=builders)
        if not a.closed_loop:
            return reconcile_once(store, dispatcher=a.dispatcher)
        try:
            return reconcile_once(
                store, dispatcher=a.dispatcher, handlers=a.handlers, evaluator=a.evaluator,
                accept_mock_evidence=a.accept_mock_evidence,
            )
        finally:
            a.close()
```

新建 `src/factory/cli.py`:

```python
"""Python 入口(闭环集成设计 §10.2、R16):调和与只读可观测。门控写命令在 TypeScript 命令行。

    factory run <repo> [--enable ...] [--allow-mock-evidence] [--confirm-directives | --no-confirm-directives]
                       [--planner-backend bridge|heuristic] [--repo-commit SHA]
                       [--interval-s S] [--max-ticks N]
    factory reconcile <repo> --once [同上的装配开关]
    factory install-defaults <repo>
    factory trace <repo> <run>
    factory cost <repo> <run>
    factory logs <repo> <run> [--task <id>]

退出码:0 成功;1 只读子命令发现缺失(run 不存在、日志不可达);2 用法错误、装配错误或仓库被另一个进程锁住(只读子命令不取锁)。
"""
from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping

from controlplane.gitstore import GitStore
from controlplane.lock import RepoLockedError
from controlplane.paths import run_dir
from factory import readonly
from factory.defaults import install_defaults
from factory.errors import FactoryConfigError
from factory.switches import SWITCHES, UnknownSwitchError, parse_enable
from factory.wire import PLANNER_BACKENDS, FactoryOptions, HandlerBuilder, reconcile_one_tick, run


def _assembly_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("repo_root", help="ControlPlane 所在的 git 仓库根目录")
    p.add_argument("--enable", default=None,
                   help=f"逗号分隔的闭环开关,取自 {','.join(SWITCHES)};不传则与 run_controlplane.py 相同")
    p.add_argument("--allow-mock-evidence", action="store_true",
                   help="接受 backends: mock 的诊断报告驱动规划、提名与战绩(默认不接受,R17)")
    p.add_argument("--confirm-directives", dest="confirm_directives",
                   action=argparse.BooleanOptionalAction, default=None,
                   help="调度器写的指令是否先停在 awaiting_confirmation;不传则用 scheduler.yaml")
    p.add_argument("--planner-backend", choices=PLANNER_BACKENDS, default="bridge",
                   help="规划后端:bridge 走 backends.yaml 的 planner 桥接,heuristic 是确定性后端")
    p.add_argument("--repo-commit", default=None,
                   help="写进配方候选 code_version.repo_commit 的提交;不传则取代码仓库的 git rev-parse HEAD")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="factory", description="RismForge 组合根:调和循环与只读可观测")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="跑调和循环")
    _assembly_flags(p_run)
    p_run.add_argument("--interval-s", type=float, default=2.0, help="两次扫描之间的间隔秒数")
    p_run.add_argument("--max-ticks", type=int, default=None, help="跑几轮后退出;不填则一直跑")

    p_rec = sub.add_parser("reconcile", help="只跑一轮调和并打印动作")
    _assembly_flags(p_rec)
    p_rec.add_argument("--once", action="store_true", required=True, help="只跑一轮(必填)")

    p_def = sub.add_parser("install-defaults", help="写入闭环模式的默认诊断资产、评估器配置与提示模板")
    p_def.add_argument("repo_root")

    for name, text in (("trace", "打印一轮的全链路产物"), ("cost", "打印一轮的成本明细与汇总")):
        p = sub.add_parser(name, help=text)
        p.add_argument("repo_root")
        p.add_argument("run_id")

    p_logs = sub.add_parser("logs", help="按 run 目录里的 log_refs 打印日志")
    p_logs.add_argument("repo_root")
    p_logs.add_argument("run_id")
    p_logs.add_argument("--task", default=None, help="只看该任务的日志")
    return parser


def _options(args: argparse.Namespace) -> FactoryOptions:
    return FactoryOptions(
        switches=parse_enable(args.enable),
        allow_mock_evidence=args.allow_mock_evidence,
        confirm_directives=args.confirm_directives,
        planner_backend=args.planner_backend,
        repo_commit=args.repo_commit,
    )


def main(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    builders: Mapping[str, HandlerBuilder] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            run(args.repo_root, _options(args), interval_s=args.interval_s, max_ticks=args.max_ticks,
                env=env, builders=builders)
            return 0
        if args.command == "reconcile":
            actions = reconcile_one_tick(args.repo_root, _options(args), env=env, builders=builders)
            for a in actions:
                print(f"{a.kind} {a.target_id}")
            if not actions:
                print("no actions")
            return 0
        store = GitStore(args.repo_root)
        if args.command == "install-defaults":
            for rel in install_defaults(store):
                print(rel)
            return 0
        if args.command == "trace":
            print(readonly.trace(store, args.run_id))
            return 0 if store.list_products(run_dir(args.run_id)) else 1
        if args.command == "cost":
            print(readonly.cost(store, args.run_id))
            return 0
        text, code = readonly.render_logs(store, args.run_id, task_id=args.task)
        print(text)
        return code
    except RepoLockedError as exc:
        # 设计 CP-12:另一个写者(守护进程或另一次 reconcile)正持锁;树里不写任何东西
        print(f"factory: error: repository is locked by another process: {exc}", file=sys.stderr)
        return 2
    except (FactoryConfigError, UnknownSwitchError) as exc:
        print(f"factory: error: {exc}", file=sys.stderr)
        return 2


def dev_main(argv: list[str] | None = None, **kw) -> int:
    """开发入口:run 与 reconcile 一律加上 --allow-mock-evidence(R17 的显式开关)。"""
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("run", "reconcile") and "--allow-mock-evidence" not in args:
        args.insert(1, "--allow-mock-evidence")
    return main(args, **kw)


if __name__ == "__main__":
    sys.exit(main())
```

新建 `scripts/run_factory.py`:

```python
#!/usr/bin/env python3
"""组合根入口的薄壳,逻辑在 factory.cli(见那里的说明)。默认不接受 mock 证据。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from factory.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
```

新建 `scripts/run_factory_dev.py`:

```python
#!/usr/bin/env python3
"""开发入口:与 run_factory.py 相同,但 run / reconcile 自动加 --allow-mock-evidence(R17)。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from factory.cli import dev_main  # noqa: E402

if __name__ == "__main__":
    sys.exit(dev_main())
```

```bash
chmod +x scripts/run_factory.py scripts/run_factory_dev.py
```

- [ ] **Step 4: 跑测试,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_factory_wire.py tests/test_factory_cli.py`
Expected:`27 passed`。

Run: 全量命令。
Expected:`B+82 passed`,无警告。

再手工跑一次入口,确认用法与退出码:

```bash
uv run --extra dev python scripts/run_factory.py --help | head -3
uv run --extra dev python scripts/run_factory.py reconcile /nonexistent; echo "exit=$?"
```

Expected:第一条打印 `usage: factory [-h] {run,reconcile,install-defaults,trace,cost,logs} ...`;第二条在标准错误打印 `factory reconcile: error: the following arguments are required: --once`,`exit=2`。

- [ ] **Step 5: 提交**

```bash
git add src/factory/wire.py
git add src/factory/cli.py
git add src/factory/handlers.py
git add scripts/run_factory.py
git add scripts/run_factory_dev.py
git add tests/factory_fixtures.py
git add tests/test_factory_wire.py
git add tests/test_factory_cli.py
git commit -m "feat: factory composition root and CLI (byte-identical to run_controlplane.py without --enable)"
```

---

### Task 105: 端到端框架:闭环模式的一个 tick 与证据开关

**Files:**
- Test: `tests/test_factory_e2e_frame.py`(4 个用例)

**Interfaces:**
- Consumes: 任务 104 的 `run`、`main`、`tests/factory_fixtures.py`(`closed_loop_repo`、`approve`、`Recorder`、`recording_builders`)。
- Produces: 端到端的仓库搭建方式(`closed_loop_repo(root, *, run_id="run-001", with_datasets=True)`:`init` → `install_defaults` → 指令 → 用默认生产器写好两份清单),任务 108 沿用。

**设计要点:**
- **空闭环**:五个开关全开、没有任何 run,跑一个 tick 不出错,假处理器一次都没被调。
- **闭环模式的晋级链**:经 `factory reconcile --once` 四次(派发 → mock 诊断 → 待审 → 人工批准后晋级),打印的动作序列逐行断言;诊断报告的等级是 `mock`。
- **证据开关(R17)**:晋级后的同一棵树,不带 `--allow-mock-evidence` 时一轮 `reconcile --once` 打印 `no actions`、假处理器没被调;带上后同一状态只产生 `nominate_recipe`:`plan_next_round` 还要等来源 run 的提名标记(CP-11),而假处理器不写标记(规划等配方入库门用真处理器在任务 108 的 `test_planning_waits_for_the_recipe_gate` 里验证)。`record_track` 不出现,因为这条人工指令的 `recipe_ref` 为空。
- 本任务的处理器全是假的(只记调用),所以测试证明的是"组合根把开关、证据参数、评估器正确地交给了计划 8 的调和循环",不是处理器的行为;后者在任务 108。
- **变异检查(撰写时做过)**:把 `reconcile_one_tick` 传给 `reconcile_once` 的 `accept_mock_evidence` 改成恒 `True`,`test_mock_evidence_does_not_turn_the_loop_unless_allowed` 失败,其余 3 个通过。

- [ ] **Step 1: 写测试**

新建 `tests/test_factory_e2e_frame.py`:

```python
"""端到端框架(任务 105):带清单与默认配置的仓库,闭环模式跑通;mock 证据默认不推动闭环。"""
from controlplane.evidence import diagnostic_grade
from controlplane.paths import candidate_checkpoint_file, diagnostic_report_file, model_file, run_dir
from factory.cli import main
from factory.wire import FactoryOptions, run
from factory_fixtures import Recorder, approve, closed_loop_repo, recording_builders

RUN = "run-001"


def test_an_empty_closed_loop_runs_a_tick_without_error(tmp_path):
    store = closed_loop_repo(str(tmp_path / "cp"), run_id=None)
    rec = Recorder()
    run(str(store.repo_root), FactoryOptions(switches=("plan", "nominate", "track", "agent", "admit")),
        interval_s=0, max_ticks=1, env={}, builders=recording_builders(rec))
    assert rec.calls == []


def _to_promotion(store, argv_tail, rec):
    """指令 → 训练 → mock 诊断 → 待审 → 人工批准 → 晋级;每步都走 factory 的 reconcile --once。"""
    root = str(store.repo_root)
    for _ in range(3):
        assert main(["reconcile", root, "--once", *argv_tail], env={}, builders=recording_builders(rec)) == 0
    approve(store, RUN)
    assert main(["reconcile", root, "--once", *argv_tail], env={}, builders=recording_builders(rec)) == 0


def test_closed_loop_mode_trains_with_a_mock_graded_diagnostic(tmp_path, capsys):
    store = closed_loop_repo(str(tmp_path / "cp"))
    _to_promotion(store, ["--enable", "plan"], Recorder())
    assert store.read_product(candidate_checkpoint_file(RUN)).status == "effective"
    assert store.exists(model_file(1))
    payload = store.read_product(diagnostic_report_file(RUN)).payload
    assert diagnostic_grade(payload) == "mock"
    out = capsys.readouterr().out.splitlines()
    assert out == [f"dispatch_directive {RUN}", f"request_diagnostic cand-{RUN}",
                   f"await_review cand-{RUN}", f"promote_candidate cand-{RUN}"]


def test_mock_evidence_does_not_turn_the_loop_unless_allowed(tmp_path, capsys):
    store = closed_loop_repo(str(tmp_path / "cp"))
    rec = Recorder()
    _to_promotion(store, ["--enable", "plan,nominate,track"], rec)
    capsys.readouterr()
    root = str(store.repo_root)
    assert main(["reconcile", root, "--once", "--enable", "plan,nominate,track"],
                env={}, builders=recording_builders(rec)) == 0
    assert capsys.readouterr().out == "no actions\n"
    assert rec.calls == []
    assert main(["reconcile", root, "--once", "--enable", "plan,nominate,track", "--allow-mock-evidence"],
                env={}, builders=recording_builders(rec)) == 0
    # 假处理器不写提名标记,所以规划还在等它(CP-11):只出现 nominate_recipe
    assert rec.calls == [("nominate_recipe", RUN)]
    assert capsys.readouterr().out == f"nominate_recipe {RUN}\n"


def test_the_recorder_handlers_write_nothing_so_the_tree_only_holds_the_round(tmp_path):
    store = closed_loop_repo(str(tmp_path / "cp"))
    _to_promotion(store, ["--enable", "plan", "--allow-mock-evidence"], Recorder())
    names = sorted(p.split("/")[-1] for p in store.list_products(run_dir(RUN)))
    assert "planner_decision.yaml" not in names and "nomination.yaml" not in names
```

- [ ] **Step 2: 跑测试**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_factory_e2e_frame.py`
Expected:`4 passed`。本任务没有新的实现代码,测试一写就该通过;若失败,说明任务 104 的装配或计划 8 的调和循环与本计划的理解不一致,停下报告失败的断言,不要改测试迁就。

Run: 全量命令。
Expected:`B+86 passed`,无警告。

- [ ] **Step 3: 提交**

```bash
git add tests/test_factory_e2e_frame.py
git commit -m "test: factory end-to-end frame (one closed-loop tick, mock evidence gated by the switch)"
```

---
### Task 106: 对计划 3、5、7 的假设核对脚本

**Files:**
- Create: `scripts/check_factory_assumptions.py`

**Interfaces:**
- Consumes: 「对计划 3、5、7 的假设」一节的 A3-1 至 A7-4 与 A8-1、A8-2(只查名字与参数,不调用)。
- Produces: `scripts/check_factory_assumptions.py`(退出码 0 = 全部成立;1 = 至少一条不成立)。任务 107、108 的第 1 步都跑它。

**设计要点:**
- 一律从**子模块**导入:`trainscheduler`、`mindagent` 的 `__init__.py` 只有包说明、不导出名字(两份计划的 File Structure 表)。
- 只用 `importlib` 与 `inspect.signature`,对每条假设比较"位置参数名的元组"与"keyword-only 参数名的元组";`None` 表示只查存在(数据类)。另有一行 `A5-4f` 比对 `SchedulerConfig` 的九个字段(计划 5 Ruling 8),防止计划 5 改了 `scheduler.yaml` 的键而本计划的覆盖方式(Ruling 21)没跟上。
- 脚本自己把 `src/` 插进 `sys.path`,与 `scripts/run_controlplane.py` 的薄壳同一手法。
- **撰写时的验证**:在只有计划 1、2、4、8 桩的拷贝里跑,每行都是 `MISSING (ModuleNotFoundError: ...)`,`exit=1`;在九份计划按序重放出来的拷贝里(前八份提交之后)跑,21 行 `OK`(含 A8-1、A8-2),最后一行 `all assumptions hold`,实测。

- [ ] **Step 1: 写脚本**

新建 `scripts/check_factory_assumptions.py`:

```python
#!/usr/bin/env python3
"""计划 9 对计划 3、5、7、8 的假设核对(任务 107、108、109 第 1 步都跑一次)。

一律从子模块导入(trainscheduler、mindagent 的 __init__.py 不导出名字)。每条假设一行:OK / MISSING(导入失败)/ MISMATCH(签名不符)。全部 OK 退出 0,否则退出 1。
只检查名字、参数名与参数种类,不调用任何处理器。
"""
from __future__ import annotations

import importlib
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

KW = inspect.Parameter.KEYWORD_ONLY
POS = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)

# (编号, 模块, 名字, 期望的位置参数名, 期望的 keyword-only 参数名;None 表示只查存在)
ASSUMPTIONS: list[tuple[str, str, str, tuple[str, ...] | None, tuple[str, ...] | None]] = [
    ("A3-1", "recipestore.library", "find_recipes", ("store",), ("dimension", "recipe_type", "states", "order")),
    ("A3-2", "recipestore.library", "get_recipe", ("store", "recipe_id"), ()),
    ("A3-3", "recipestore.admit", "handle_admit_recipe", ("store", "action"), ()),
    ("A3-4", "recipestore.record", "handle_record_track", ("store", "action"), ()),
    ("A3-5", "recipestore.gain", "compute_gain", ("store", "run_id", "dimension"), ()),
    ("A3-6", "recipestore.gain", "GainResult", None, None),
    ("A3-7", "recipestore.admit", "has_repro_evidence", ("payload",), ()),
    ("A3-8", "recipestore.gain", "baseline_run_of", ("store", "baseline_ref"), ()),
    ("A5-1", "trainscheduler.plan_handler", "make_plan_handler", (),
     ("backend", "template_loader", "config_loader", "defaults_loader")),
    ("A5-2", "trainscheduler.nominate", "make_nominate_handler", (),
     ("config_loader", "accept_mock_evidence", "repo_commit")),
    ("A5-3", "trainscheduler.config", "install_default_configs", ("store",), ()),
    ("A5-4", "trainscheduler.config", "SchedulerConfig", None, None),
    ("A5-5", "trainscheduler.config", "load_scheduler_config", ("store",), ()),
    ("A5-6", "trainscheduler.assemble", "assemble_context", ("store", "run_id"), ("config",)),
    ("A7-1", "mindagent.rounds", "make_start_round_handler", (),
     ("config", "client_factory", "backend_factory", "personas_loader", "bridge")),
    ("A7-2", "mindagent.admit", "handle_admit_proposal", ("store", "action"), ()),
    ("A7-3", "mindagent.library", "load_library", ("store",), ()),
    ("A7-4", "mindagent.config", "AgentConfig", None, None),
    ("A8-1", "controlplane.lock", "acquire_repo_lock", ("repo_root",), ()),
    ("A8-2", "controlplane.lock", "RepoLockedError", None, None),
]
# A5-4 的字段集:scheduler.yaml 恰好九个键(计划 5 Ruling 8)
SCHEDULER_KEYS = ("confirm_directives", "allow_interact", "accept_mock_evidence", "perturbable", "min_gain",
                  "cost_gain_window", "recent_decision_window", "max_plan_attempts", "template_version")


def check(module: str, name: str, pos, kw) -> str:
    try:
        obj = getattr(importlib.import_module(module), name)
    except (ImportError, AttributeError) as exc:
        return f"MISSING ({type(exc).__name__}: {exc})"
    if pos is None:
        return "OK"
    params = list(inspect.signature(obj).parameters.values())
    got_pos = tuple(p.name for p in params if p.kind in POS)
    got_kw = tuple(p.name for p in params if p.kind == KW)
    if got_pos != pos or got_kw != kw:
        return f"MISMATCH (positional {got_pos}, keyword-only {got_kw}; expected {pos}, {kw})"
    return "OK"


def main() -> int:
    bad = 0
    for code, module, name, pos, kw in ASSUMPTIONS:
        verdict = check(module, name, pos, kw)
        bad += verdict != "OK"
        print(f"{code} {module}.{name}: {verdict}")
    try:
        from dataclasses import fields
        from trainscheduler.config import SchedulerConfig
        got = tuple(f.name for f in fields(SchedulerConfig))
        verdict = "OK" if got == SCHEDULER_KEYS else f"MISMATCH (fields {got}; expected {SCHEDULER_KEYS})"
    except ImportError as exc:
        verdict = f"MISSING ({type(exc).__name__}: {exc})"
    bad += verdict != "OK"
    print(f"A5-4f trainscheduler.config.SchedulerConfig fields: {verdict}")
    print("all assumptions hold" if not bad else f"{bad} assumption(s) do not hold; stop and report")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: 运行**

```bash
chmod +x scripts/check_factory_assumptions.py
uv run --extra dev python scripts/check_factory_assumptions.py; echo "exit=$?"
```

Expected:21 行 `OK`(`A3-1` 至 `A8-2` 共 20 行,加 `A5-4f`),最后一行 `all assumptions hold`,`exit=0`。若有 `MISSING` 或 `MISMATCH`,本任务仍然提交脚本(它就是用来报告这件事的),但**不要开始任务 107**,把输出报告给人。

Run: 全量命令。
Expected:`B+86 passed`(脚本不是测试,增量为 0),无警告。

- [ ] **Step 3: 提交**

```bash
git add scripts/check_factory_assumptions.py
git commit -m "chore: a check script for what the factory assumes about recipestore, trainscheduler and mindagent"
```

---

### Task 107: 六个处理器的构造器 `DEFAULT_BUILDERS`

**Files:**
- Create: `src/factory/agent.py`
- Modify: `src/factory/handlers.py`(整个替换,10 → 92 行)、`src/factory/defaults.py`(整个替换,49 → 88 行)
- Test: `tests/test_factory_handlers.py`(18 个用例)

**Interfaces:**
- Consumes: A3-3、A3-4、A5-1 至 A5-5、A7-1 至 A7-4;任务 102 的 `build_bridge`;任务 104 的 `BuildContext`(含 `repo_commit`、`register_closer`);计划 4 的 `BridgeDecisionBackend`、`HeuristicBackend`;`simulator.realtime` 的 `create_server`、`EnvironmentServiceClient`;`simulator.store_adapter` 的 `read_persona`、`read_scenario`、`write_persona`、`write_scenario`。
- Produces:
  - `factory.handlers.DEFAULT_BUILDERS`:六个键;`scheduler_config_loader(confirm_directives) -> Callable[[GitStore], SchedulerConfig]`。
  - `factory.agent`:`AGENT_CONFIG = "control-plane/config/agent.yaml"`、`read_agent_settings(store) -> (AgentConfig, tuple[str, ...])`、`INTERACT_DEADLINE_S = 15.0`、`DeadlineStub(stub, timeout_s=INTERACT_DEADLINE_S)`(只包 `Interact`,每条流带 `timeout`)、`AgentRuntime(store, opponent_lines, *, turn_timeout_s=5.0, interact_deadline_s=INTERACT_DEADLINE_S, stub_factory=EnvironmentServiceStub)`(`client_factory()`、`started`、`close()`)、`CannedGuidedBackend`、`canned_backend_factory(model_id, checkpoint_ref)`、`personas_from_tree(store, persona_ids)`。
  - `factory.defaults`:`DEFAULT_AGENT_SCENARIOS`、`DEFAULT_AGENT_PERSONAS`、`DEFAULT_AGENT_CONFIG`;`install_defaults` 另写 `scheduler.yaml`、`training_defaults.yaml`(计划 5 的 `install_default_configs`)、三个部署情境、两个人格与 `agent.yaml`。

**设计要点:**
- **三个键就是处理器本身**:`admit_recipe → recipestore.admit.handle_admit_recipe`、`record_track → recipestore.record.handle_record_track`、`admit_proposal → mindagent.admit.handle_admit_proposal`;测试用 `==` 断言装配出的就是这三个函数对象。
- **`accept_mock_evidence` 只有一个来源**(Ruling 19):`--allow-mock-evidence` → `FactoryOptions.allow_mock_evidence` → `Assembly.accept_mock_evidence`(传给 `reconcile_once`)与 `BuildContext.accept_mock_evidence`(传给 `make_nominate_handler`)。`scheduler.yaml` 的同名键在组合根里不起作用,因为参数永远不是 `None`。测试用替身记下 `make_nominate_handler` 收到的参数;变异检查(把它改成 `None`)让 2 个用例失败。
- **`repo_commit`**(Ruling 20):`--repo-commit` 优先,否则代码仓库根的 `git rev-parse HEAD`,取不到为 `None`,由计划 5 写进 `repro.errors`。
- **`--confirm-directives`**(Ruling 13、21):`scheduler_config_loader` 只在返回的 `SchedulerConfig` 上 `dataclasses.replace`,不写回 `scheduler.yaml`;规划与提名两个处理器共用这个 loader。
- **规划后端**(Ruling 14):`bridge` 时在装配阶段构造 `BridgeDecisionBackend(ctx.bridge("planner"))`,`backends.yaml` 缺失就退出码 2;`heuristic` 时用 `HeuristicBackend()`。
- **智能体轮次**(Ruling 22、23):`AgentConfig` 与对手台词来自 `agent.yaml`;实时服务端是进程内 loopback(`AgentRuntime`,第一次要客户端时才起,`Assembly.close()` 时停);模型后端是罐头 `CannedGuidedBackend`(把指导语拼成话语,与计划 7 任务 88 的 `Echo` 同一行为);`personas_loader` 从树里读;`backends.yaml` 有 `agent` 项才传桥接,没有就传 `None`(计划 7 的规则模板生成器)。
- `agent.yaml` 写错(缺键、多键、类型、`AgentConfig` 自己的校验)一律 `FactoryConfigError`,退出码 2。
- **Interact 流的 deadline**:`EnvironmentServiceClient` 只调 `stub.Interact`,`DeadlineStub` 只包这一个方法、补上 `timeout=15.0`;服务端失联时 `next()` 以 `DEADLINE_EXCEEDED` 结束,不挂住守护进程或测试套件。`test_every_interact_stream_carries_a_deadline` 注入一个记录调用参数的假 stub,断言 `Interact` 带了 `timeout`(默认 15.0,可经 `interact_deadline_s` 改),不依赖真实超时;变异检查(撰写时做过):去掉 `timeout=` 这条失败。`close()` 先关通道,再 `stop(0).wait()` 等服务端真正停下。

- [ ] **Step 1: 前置检查**

```bash
uv run --extra dev python scripts/check_factory_assumptions.py; echo "exit=$?"
```

Expected:21 行都以 `: OK` 结尾,最后一行 `all assumptions hold`,`exit=0`。有任何一行是 `MISSING` 或 `MISMATCH`:停下,把那几行原样报告,不往下做。

- [ ] **Step 2: 写失败测试**

新建 `tests/test_factory_handlers.py`:

```python
"""六个处理器构造器(任务 107):每个键都接到计划 3、5、7 的真实处理器;开关值只有一个来源。"""
import subprocess
import sys

import pytest

from controlplane.config import write_config
from controlplane.paths import backends_config_file, scheduler_config_file
from controlplane.reconcile import CLOSED_LOOP_ACTIONS
from factory import handlers as fh
from factory.agent import AGENT_CONFIG, AgentRuntime, CannedGuidedBackend, read_agent_settings
from factory.defaults import DEFAULT_AGENT_CONFIG, install_defaults
from factory.errors import FactoryConfigError
from factory.wire import CODE_ROOT, FactoryOptions, assemble, resolve_repo_commit
from factory_fixtures import closed_loop_repo
from mindagent.admit import handle_admit_proposal
from mindagent.config import AgentConfig
from recipestore.admit import handle_admit_recipe
from recipestore.record import handle_record_track
from trainscheduler.config import load_scheduler_config

ALL = ("plan", "nominate", "track", "agent", "admit")


@pytest.fixture
def store(tmp_path):
    return closed_loop_repo(str(tmp_path / "cp"), run_id=None)


def _planner_backend(store):
    write_config(store, backends_config_file(), "backends", {"planner": {
        "backend": "fake", "invocation_form": "cli", "model_name": "m", "prompt_template_version": "v1",
        "token_env": None, "command": [sys.executable, "-c", "print('{}')"],
    }}, "test: backends")


def test_the_table_covers_exactly_the_six_keys():
    assert set(fh.DEFAULT_BUILDERS) == set(CLOSED_LOOP_ACTIONS)


def test_three_keys_are_the_plan_3_and_plan_7_handlers_themselves(store):
    asm = assemble(store, FactoryOptions(switches=("track", "admit")), env={})
    assert asm.handlers == {"record_track": handle_record_track, "admit_recipe": handle_admit_recipe,
                            "admit_proposal": handle_admit_proposal}


def test_every_switch_builds_a_callable_handler_with_the_heuristic_backend(store):
    asm = assemble(store, FactoryOptions(switches=ALL, planner_backend="heuristic"), env={})
    try:
        assert set(asm.handlers) == set(CLOSED_LOOP_ACTIONS)
        assert all(callable(h) for h in asm.handlers.values())
    finally:
        asm.close()


def test_the_bridge_planner_needs_a_planner_backend_entry(store, capsys):
    from factory.cli import main
    assert main(["reconcile", str(store.repo_root), "--once", "--enable", "plan"], env={}) == 2
    assert "backends.yaml is missing" in capsys.readouterr().err
    _planner_backend(store)
    assert main(["reconcile", str(store.repo_root), "--once", "--enable", "plan"], env={}) == 0


def test_the_mock_evidence_switch_reaches_the_nominate_handler(store, monkeypatch):
    seen = []
    monkeypatch.setattr(fh, "make_nominate_handler", lambda **kw: seen.append(kw) or (lambda s, a: None))
    for flag in (False, True):
        assemble(store, FactoryOptions(switches=("nominate",), allow_mock_evidence=flag, repo_commit="abc"), env={})
    assert [(kw["accept_mock_evidence"], kw["repo_commit"]) for kw in seen] == [(False, "abc"), (True, "abc")]


def test_confirm_override_only_changes_the_loaded_value(store):
    write_config(store, scheduler_config_file(), "scheduler",
                 dict(load_scheduler_config(store).to_dict(), confirm_directives=True), "human: confirm on")
    assert fh.scheduler_config_loader(None)(store).confirm_directives is True
    assert fh.scheduler_config_loader(False)(store).confirm_directives is False
    assert load_scheduler_config(store).confirm_directives is True  # 配置文件没被改


def test_repo_commit_prefers_the_override_then_git_then_none(tmp_path):
    assert resolve_repo_commit("deadbeef") == "deadbeef"
    head = subprocess.run(["git", "-C", str(CODE_ROOT), "rev-parse", "HEAD"], capture_output=True, text=True)
    expected = head.stdout.strip() if head.returncode == 0 else None
    assert resolve_repo_commit(None) == expected
    assert resolve_repo_commit(None, code_root=tmp_path) is None


def test_the_cli_flag_sets_repo_commit(store, monkeypatch):
    from factory.cli import main
    seen = []
    monkeypatch.setattr(fh, "make_nominate_handler", lambda **kw: seen.append(kw) or (lambda s, a: None))
    assert main(["reconcile", str(store.repo_root), "--once", "--enable", "nominate", "--repo-commit", "c0ffee"],
                env={}) == 0
    assert seen[0]["repo_commit"] == "c0ffee"


def test_agent_settings_come_from_agent_yaml(store):
    config, lines = read_agent_settings(store)
    assert isinstance(config, AgentConfig)
    assert config.scenario_ids == tuple(DEFAULT_AGENT_CONFIG["scenario_ids"])
    assert (config.agent_slot, config.max_turns, config.min_support) == ("agent", 4, 3)
    assert lines == tuple(DEFAULT_AGENT_CONFIG["opponent_lines"])


@pytest.mark.parametrize("change,match", [
    ({"drop": "opponent_lines"}, "missing"), ({"set": ("colour", "red")}, "unknown"),
    ({"set": ("scenario_ids", [])}, "scenario_ids"), ({"set": ("min_ratio", 2.0)}, "min_ratio"),
    ({"set": ("bindings", [{"slot_id": "a"}])}, "KeyError"),
])
def test_bad_agent_yaml_is_an_assembly_error(store, change, match):
    payload = dict(DEFAULT_AGENT_CONFIG)
    if "drop" in change:
        del payload[change["drop"]]
    else:
        payload[change["set"][0]] = change["set"][1]
    write_config(store, AGENT_CONFIG, "agent", payload, "human: break agent.yaml")
    with pytest.raises(FactoryConfigError, match=match):
        assemble(store, FactoryOptions(switches=("agent",)), env={})


def test_the_agent_runtime_starts_lazily_and_closes(store):
    runtime = AgentRuntime(store, ("hi",))
    assert not runtime.started
    runtime.client_factory()
    assert runtime.started
    runtime.close()
    assert not runtime.started


def test_every_interact_stream_carries_a_deadline(store):
    """计划 7 已知限制 4:生产用的 EnvironmentServiceClient 没有 deadline,组合根在构造 stub 时包一层。
    用假 stub 记下调用参数,不依赖真实超时。"""
    from factory.agent import INTERACT_DEADLINE_S, DeadlineStub

    calls = []

    class FakeStub:
        def __init__(self, channel):
            pass

        def Interact(self, request_iterator, **kw):
            calls.append(kw)
            return iter(())

    default = AgentRuntime(store, ("hi",), stub_factory=FakeStub)
    short = AgentRuntime(store, ("hi",), stub_factory=FakeStub, interact_deadline_s=2.5)
    try:
        client = default.client_factory()
        assert isinstance(client._stub, DeadlineStub)
        assert calls == [{"timeout": INTERACT_DEADLINE_S}] and INTERACT_DEADLINE_S == 15.0
        short.client_factory()
        assert calls[-1] == {"timeout": 2.5}
    finally:
        default.close()
        short.close()
    assert not default.started and not short.started


def test_the_canned_backend_echoes_guidance():
    assert CannedGuidedBackend().respond_guided(None, None, [], "agent", ["[skill:x] a", "b"]) == \
        ("[skill:x] a|b", "saw 0")
    assert CannedGuidedBackend().respond_guided(None, None, [1, 2], "agent", []) == ("plain", "saw 2")


def test_install_defaults_writes_scheduler_and_agent_material_and_is_idempotent(tmp_path):
    from factory_fixtures import git_head
    from controlplane.gitstore import GitStore
    s = GitStore(str(tmp_path / "x"))
    s.init()
    paths = install_defaults(s)
    assert {scheduler_config_file(), AGENT_CONFIG} <= set(paths)
    assert all(s.exists(p) for p in paths)
    head = git_head(s)
    assert install_defaults(s) == paths
    assert git_head(s) == head
```

- [ ] **Step 3: 跑测试,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_factory_handlers.py`
Expected:收集阶段报错 `ModuleNotFoundError: No module named 'factory.agent'`,`1 error`。

- [ ] **Step 4: 写实现**

新建 `src/factory/agent.py`:

```python
"""MindAgent 轮次处理器的装配材料:agent.yaml、进程内 loopback 实时服务端、V1 罐头模型后端。

agent.yaml 是组合根自己的配置信封(control-plane/config/agent.yaml),载荷键:
- 必填 scenario_ids(非空字符串列表)、bindings(RoleBinding.to_dict() 的列表)、opponent_lines(非空字符串列表);
- 可选 AgentConfig 的其余字段(max_turns、episodes_per_scenario、max_retries_per_episode、min_support、
  min_ratio、min_fired_on_motivating、max_fired_elsewhere、max_parse_retries),缺省用 AgentConfig 的默认值。
未知键、类型错都抛 FactoryConfigError;AgentConfig 自己的校验错误也转成 FactoryConfigError。
"""
from __future__ import annotations

import threading
from dataclasses import fields

import grpc

from controlplane.config import read_config
from controlplane.gitstore import GitStore
from controlplane.paths import CONTROL_PLANE_ROOT
from factory.errors import FactoryConfigError
from mindagent.config import AgentConfig
from simulator.backends import ScriptedBackend
from simulator.datamodel import RoleBinding
from simulator.realtime.client import EnvironmentServiceClient
from simulator.realtime.proto import realtime_env_pb2_grpc as pb2_grpc
from simulator.realtime.server import create_server
from simulator.store_adapter import read_persona, read_scenario

AGENT_CONFIG = f"{CONTROL_PLANE_ROOT}/config/agent.yaml"
AGENT_BACKENDS = ("canned",)
_REQUIRED = ("scenario_ids", "bindings", "opponent_lines")
_OPTIONAL = tuple(f.name for f in fields(AgentConfig) if f.name not in ("scenario_ids", "bindings"))


class CannedGuidedBackend:
    """V1 唯一的智能体模型后端:不加载 checkpoint,把 Actio 渲染的指导语拼成话语。
    技能是否生效在落盘的 episode 里直接看得见;它不代表任何模型能力。"""

    def respond_guided(self, persona, scenario, history, speaker_slot, guidance):
        return ("|".join(guidance) or "plain", f"saw {len(history)}")


def canned_backend_factory(model_id: str, checkpoint_ref) -> CannedGuidedBackend:
    return CannedGuidedBackend()


def personas_from_tree(store: GitStore, persona_ids) -> dict:
    """personas_loader:从树里读人格(simulator.store_adapter)。缺了由轮次处理器的输入检查判 failed。"""
    return {pid: read_persona(store, pid) for pid in persona_ids}


def _str_list(payload: dict, key: str) -> list[str]:
    value = payload[key]
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and v for v in value):
        raise FactoryConfigError(f"{AGENT_CONFIG}: {key} must be a non-empty list of non-empty strings")
    return list(value)


def read_agent_settings(store: GitStore) -> tuple[AgentConfig, tuple[str, ...]]:
    """返回 (AgentConfig, 对手罐头台词)。"""
    try:
        payload = read_config(store, AGENT_CONFIG)
    except FileNotFoundError:
        raise FactoryConfigError(
            f"{AGENT_CONFIG} is missing; the agent switch needs it (run install-defaults)"
        ) from None
    keys = set(payload)
    missing = sorted(set(_REQUIRED) - keys)
    unknown = sorted(keys - set(_REQUIRED) - set(_OPTIONAL))
    if missing or unknown:
        raise FactoryConfigError(f"{AGENT_CONFIG}: missing {missing}, unknown {unknown}")
    scenario_ids = _str_list(payload, "scenario_ids")
    lines = _str_list(payload, "opponent_lines")
    raw_bindings = payload["bindings"]
    if not isinstance(raw_bindings, list) or not all(isinstance(b, dict) for b in raw_bindings):
        raise FactoryConfigError(f"{AGENT_CONFIG}: bindings must be a list of mappings")
    try:
        bindings = tuple(RoleBinding.from_dict(b) for b in raw_bindings)
        config = AgentConfig(
            scenario_ids=tuple(scenario_ids), bindings=bindings,
            **{k: payload[k] for k in _OPTIONAL if k in payload},
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise FactoryConfigError(f"{AGENT_CONFIG}: {type(exc).__name__}: {exc}") from None
    return config, tuple(lines)


INTERACT_DEADLINE_S = 15.0


class DeadlineStub:
    """给每条 Interact 流加 gRPC deadline(计划 7 已知限制 4 与它测试夹具里的 _DeadlineStub 同一写法):
    服务端失联时客户端的 next() 以 DEADLINE_EXCEEDED 结束,而不是让守护进程或测试套件挂住。
    EnvironmentServiceClient 只调 stub.Interact,所以只包这一个方法。"""

    def __init__(self, stub, timeout_s: float = INTERACT_DEADLINE_S) -> None:
        self._stub = stub
        self.timeout_s = timeout_s

    def Interact(self, request_iterator):
        return self._stub.Interact(request_iterator, timeout=self.timeout_s)


class AgentRuntime:
    """进程内的 loopback 实时服务端,第一次 client_factory() 时才启动;close() 关通道、停服务端并等它停完。
    对手位一律用 ScriptedBackend 说 opponent_lines;情境从树里读(scenario_resolver)。
    每条 Interact 流带 interact_deadline_s 的 deadline(DeadlineStub);stub_factory 只为测试注入。"""

    def __init__(self, store: GitStore, opponent_lines: tuple[str, ...], *, turn_timeout_s: float = 5.0,
                 interact_deadline_s: float = INTERACT_DEADLINE_S,
                 stub_factory=pb2_grpc.EnvironmentServiceStub) -> None:
        self._store = store
        self._lines = list(opponent_lines)
        self._timeout = turn_timeout_s
        self._deadline = interact_deadline_s
        self._stub_factory = stub_factory
        self._server = None
        self._channels: list = []
        self._lock = threading.Lock()

    def _backends(self, scenario, assignment, personas, client_slot):
        return {s: ScriptedBackend({s: list(self._lines)}) for s in assignment.slot_ids() - {client_slot}}

    def client_factory(self) -> EnvironmentServiceClient:
        with self._lock:
            if self._server is None:
                self._server = create_server(
                    self._backends, port=0, turn_timeout_s=self._timeout,
                    scenario_resolver=lambda sid: read_scenario(self._store, sid),
                )
                self._server.start()
            channel = grpc.insecure_channel(f"127.0.0.1:{self._server.bound_port}")
            self._channels.append(channel)
        return EnvironmentServiceClient(DeadlineStub(self._stub_factory(channel), self._deadline))

    @property
    def started(self) -> bool:
        return self._server is not None

    def close(self) -> None:
        with self._lock:
            for channel in self._channels:
                channel.close()
            self._channels.clear()
            if self._server is not None:
                self._server.stop(0).wait()
                self._server = None
```

把 `src/factory/handlers.py` 整个替换为:

```python
"""六个处理器键的真实构造器表:{处理器键: (BuildContext) -> Handler}。

- admit_recipe / record_track:计划 3 的处理器本身(recipestore.admit / recipestore.record)。
- admit_proposal:计划 7 的处理器本身(mindagent.admit)。
- plan_next_round:计划 5 的 make_plan_handler;后端按 --planner-backend 选;--confirm-directives
  覆盖 scheduler.yaml 的 confirm_directives(只在内存里,不写回配置)。
- nominate_recipe:计划 5 的 make_nominate_handler;accept_mock_evidence 与传给 reconcile_once 的是
  同一个值(--allow-mock-evidence);repo_commit 来自 BuildContext。
- start_agent_round:计划 7 的 make_start_round_handler;AgentConfig 与对手台词来自 agent.yaml,
  实时服务端是进程内 loopback(factory.agent.AgentRuntime),模型后端是罐头后端。
"""
from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping

from controlplane.config import read_config
from controlplane.daemon import Handler
from controlplane.gitstore import GitStore
from controlplane.paths import backends_config_file
from factory.agent import (
    AgentRuntime,
    canned_backend_factory,
    personas_from_tree,
    read_agent_settings,
)
from mindagent.admit import handle_admit_proposal
from mindagent.rounds import make_start_round_handler
from recipestore.admit import handle_admit_recipe
from recipestore.record import handle_record_track
from trainplanner.backends import BridgeDecisionBackend, HeuristicBackend
from trainscheduler.config import SchedulerConfig, load_scheduler_config
from trainscheduler.nominate import make_nominate_handler
from trainscheduler.plan_handler import make_plan_handler


def scheduler_config_loader(confirm_directives: bool | None) -> Callable[[GitStore], SchedulerConfig]:
    """读 scheduler.yaml;confirm_directives 不为 None 时只在返回值里覆盖同名键。"""

    def load(store: GitStore) -> SchedulerConfig:
        config = load_scheduler_config(store)
        if confirm_directives is None:
            return config
        return dataclasses.replace(config, confirm_directives=confirm_directives)

    return load


def _plan(ctx) -> Handler:
    if ctx.planner_backend == "heuristic":
        backend = HeuristicBackend()
    else:
        backend = BridgeDecisionBackend(ctx.bridge("planner"), name="bridge")
    return make_plan_handler(backend=backend, config_loader=scheduler_config_loader(ctx.confirm_directives))


def _nominate(ctx) -> Handler:
    return make_nominate_handler(
        config_loader=scheduler_config_loader(ctx.confirm_directives),
        accept_mock_evidence=ctx.accept_mock_evidence,
        repo_commit=ctx.repo_commit,
    )


def _has_bridge_role(store: GitStore, role: str) -> bool:
    if not store.exists(backends_config_file()):
        return False
    return role in read_config(store, backends_config_file())


def _agent(ctx) -> Handler:
    config, lines = read_agent_settings(ctx.store)
    runtime = AgentRuntime(ctx.store, lines)
    ctx.register_closer(runtime.close)
    bridge = ctx.bridge("agent") if _has_bridge_role(ctx.store, "agent") else None
    return make_start_round_handler(
        config=config,
        client_factory=runtime.client_factory,
        backend_factory=canned_backend_factory,
        personas_loader=personas_from_tree,
        bridge=bridge,
    )


DEFAULT_BUILDERS: Mapping[str, Callable] = {
    "admit_recipe": lambda ctx: handle_admit_recipe,
    "admit_proposal": lambda ctx: handle_admit_proposal,
    "record_track": lambda ctx: handle_record_track,
    "nominate_recipe": _nominate,
    "plan_next_round": _plan,
    "start_agent_round": _agent,
}
```

把 `src/factory/defaults.py` 整个替换为:

```python
"""闭环模式的默认资产与配置:诊断分类表、题库、评估器配置、规划提示模板。

全部经 GitStore / write_config 写入,重复安装是无操作(内容相同不提交)。
分类表与题库是最小的 V1 夹具,与计划 8 任务 95 的测试资产同形;真实题库替换它们即可。
"""
from __future__ import annotations

from controlplane.config import write_config
from controlplane.gitstore import GitStore
from controlplane.paths import scheduler_config_file, training_defaults_file
from diagnostic.assets import write_item_pool, write_taxonomy
from diagnostic.datamodel import DiagnosticItem, DiagnosticTaxonomy, VariantFamily
from diagnostic.taxonomy_loader import ItemPool
from factory.agent import AGENT_CONFIG
from factory.evaluation import EVALUATOR_CONFIG
from simulator.datamodel import Persona, RoleBinding, RoleSlot, Scenario
from simulator.store_adapter import write_persona, write_scenario
from trainplanner.templates import install_default_template
from trainscheduler.config import install_default_configs

DEFAULT_TAXONOMY = DiagnosticTaxonomy(
    "factory-tax-v1",
    ["mental_representation"],
    {"mental_representation": ["belief"]},
    {"belief": ["false_belief_transfer"]},
    ["format_compliance"],
)
DEFAULT_ITEM_POOL = ItemPool(
    "factory-pool-v1",
    [
        DiagnosticItem("fi1", "false_belief_transfer", "mcq", "rule", "static", "B", None, "fvf1"),
        DiagnosticItem("fi1v", "false_belief_transfer", "mcq", "rule", "static", "C", None, "fvf1"),
    ],
    [VariantFamily("fvf1", "fi1", ["fi1v"])],
)
DEFAULT_EVALUATOR_CONFIG = {
    "taxonomy_version": DEFAULT_TAXONOMY.version,
    "item_pool_version": DEFAULT_ITEM_POOL.version,
    "benchmark_version": "factory-bench-v1",
    "tier": "full",
    "sampling_k": 4,
}

# MindAgent 部署模拟的默认素材:三个 deployment_simulation 情境、两个人格、对手罐头台词。
# 对手第一句含 emotion_high 的触发词,让经验分析器在空库上也有可分析的信号(计划 7 任务 88 同一手法)。
DEFAULT_AGENT_SCENARIOS = tuple(
    Scenario(sid, "a customer escalates a complaint", [RoleSlot("agent", "support"), RoleSlot("user", "customer")],
             "the order arrived broken", ["stay polite"], "deployment_simulation")
    for sid in ("factory-deploy-s1", "factory-deploy-s2", "factory-deploy-s3")
)
DEFAULT_AGENT_PERSONAS = (
    Persona("factory-agent-persona", ["calm"], "resolve the issue", "plain", ["honesty"]),
    Persona("factory-user-persona", ["impatient"], "get a refund", "blunt", ["fairness"]),
)
DEFAULT_AGENT_CONFIG = {
    "scenario_ids": [s.id for s in DEFAULT_AGENT_SCENARIOS],
    "bindings": [
        RoleBinding("user", "factory-user-persona", "frozen_reference", model_ref="zengen-v1").to_dict(),
        RoleBinding("agent", "factory-agent-persona", "self_play").to_dict(),
    ],
    "opponent_lines": ["我真的很生气!!", "好吧,你说说看", "那就这样吧"],
    "max_turns": 4,
    "min_support": 3,
    "min_ratio": 0.5,
}


def _write_config_once(store: GitStore, rel: str, config_id: str, payload: dict) -> None:
    if not store.exists(rel):
        write_config(store, rel, config_id, payload, f"factory: install default {rel.rsplit('/', 1)[-1]}")


def install_defaults(store: GitStore) -> list[str]:
    """写入默认资产与配置,返回涉及的相对路径(已存在的也列出)。
    已存在的配置文件一律不覆盖:人改过的配置优先。"""
    paths = [write_taxonomy(store, DEFAULT_TAXONOMY), write_item_pool(store, DEFAULT_ITEM_POOL)]
    _write_config_once(store, EVALUATOR_CONFIG, "evaluator", dict(DEFAULT_EVALUATOR_CONFIG))
    paths.append(EVALUATOR_CONFIG)
    paths.append(install_default_template(store))
    install_default_configs(store)
    paths += [scheduler_config_file(), training_defaults_file()]
    for scenario in DEFAULT_AGENT_SCENARIOS:
        write_scenario(store, scenario)
    for persona in DEFAULT_AGENT_PERSONAS:
        write_persona(store, persona)
    _write_config_once(store, AGENT_CONFIG, "agent", DEFAULT_AGENT_CONFIG)
    paths.append(AGENT_CONFIG)
    return paths
```

- [ ] **Step 5: 跑测试,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_factory_handlers.py tests/test_factory_wire.py tests/test_factory_cli.py tests/test_factory_e2e_frame.py tests/test_factory_evaluation.py`
Expected:`61 passed`(本任务 18 个,任务 103–105 的 43 个照样通过:它们用 `builders=` 注入假处理器,不受真实表影响)。

Run: 全量命令。
Expected:`B+104 passed`,无警告。

- [ ] **Step 6: 提交**

```bash
git add src/factory/agent.py
git add src/factory/handlers.py
git add src/factory/defaults.py
git add tests/test_factory_handlers.py
git commit -m "feat: factory builds the six closed-loop handlers from recipestore, trainscheduler and mindagent"
```

---

### Task 108: 完整闭环与 MindAgent 端到端

**Files:**
- Test: `tests/test_factory_e2e_loop.py`(10 个用例)

**Interfaces:**
- Consumes: 任务 105 的 `closed_loop_repo`、`produce_datasets`、`write_directive(..., dimension=...)`;任务 107 的 `DEFAULT_BUILDERS`、`factory.handlers.canned_backend_factory`(测试里 monkeypatch 它);A3-1、A3-2、A3-5、A7-3;计划 7 的 `mindagent.rounds.package`(测试里 monkeypatch 它);计划 8 的 `make_diagnostic_evaluator`(造基线)与 R19 的 `start_agent_round` 触发条件 `agent_round_status in (None, "pending")`。
- Produces: 无新名字。

**设计要点:**
- **全部走 `factory reconcile --once`**:每个 tick 的动作行逐行断言;人工动作(批准、确认、驳回)用写状态模拟,等价于 TypeScript 的 `approve`、`confirm`、`reject`(与 `tests/test_mt_wire.py` 同一手法)。没有任何处理器是假的。
- **基线**:mock 诊断对每个候选打同样的分,所以要有一个更低分的基线,提名才有正增益。`run-000` 是人工登记的基线:用"全错"的 mock 运行器经计划 8 的评估器写一份报告,再登记 `zengen-v1`(`links = [cand-run-000, run-000]`,带 `checkpoint_ref`)。run id 一律补零(`run-000`、`run-001`、`run-002`,设计 §4.1)。
- **目标维度**:指令的 `target_dimension` 用默认分类表唯一的一级维度 `mental_representation`;不然 `compute_gain` 找不到一级分数,提名永远跳过。
- **一整圈**(`test_the_whole_loop_turns_twice`):`run-001` 训练 → mock 诊断 → 批准 → 晋级(`zengen-v2`)→ 提名(增益 > `min_gain`)→ 配方候选(`repro` 在场,满足 `has_repro_evidence`)→ 批准 → 收录 → 规划(启发式后端 clone `recipe-run-001`,决策 `template_version == "v1"`、`next_run_id == "run-002"`)→ `awaiting_confirmation` 的新指令(闭环不动)→ 确认 → 第二轮训练出候选并晋级(`zengen-v3`;clone 复用 `run-001` 的清单,不为 `run-002` 合成)→ 一个 tick 里 `record_track run-002`、`nominate_recipe run-002`(增益 0,写 `skipped` 标记),下一个 tick 才出现 `plan_next_round run-002`(规划要等提名标记,CP-11;只对最大的 run)。最后 `factory cost run-001` 的输出里没有 `None`。
- **不确认**(`test_a_new_round_without_confirmation_synthesizes_its_data`):`--no-confirm-directives`。启用集是 `plan,track,admit`,没开提名,规划不等配方入库门(CP-11),没有配方可 clone,启发式后端走 `new batch`,指令直接 `pending` 且带 `data_spec`;下一个 tick 由注入的数据生产器合成 `run-002` 的两份清单并训练出候选。
- **只对最大的 run 规划**(R5,`test_only_the_largest_run_is_planned`):`run-001` 晋级、`run-002` 被驳回(写 `reject_reason`),两者都满足条件,只有 `run-002` 触发。
- **规划等配方入库门**(CP-11,`test_planning_waits_for_the_recipe_gate`):启用集 `plan,nominate,admit`。晋级后的 tick 只提名(`nominate_recipe run-001`),这个 tick 的快照里还没有提名标记,规划不出现;配方候选停在 `pending_review` 时多个 tick 都是 `no actions`,没有决策;人工批准后的 tick 只出现 `admit_recipe recipe-run-001`(快照里候选是 `approved`,规划仍被挡着),候选收录为 `effective` 之后的下一个 tick 才出现 `plan_next_round run-001`,决策 `clone recipe-run-001`。**先失败**(按计划 8 的触发条件推演,未重放):把触发条件改回不等配方门,这一条与 `test_the_whole_loop_turns_twice` 失败(提名与规划挤进同一个 tick),本任务其余 8 条照样通过。
- **R18**(`test_a_bridge_failure_fails_the_decision_and_stops_the_loop`):`--planner-backend bridge`,`backends.yaml` 的 `planner` 是一个恒以退出码 3 结束、往标准错误写 `tool down` 的 `sys.executable -c` 脚本,`max_attempts=1`。`CliBridge` 抛 `BridgeError("T1", ...)`,计划 5 的规划处理器把决策置 `failed`(类别 T3,原因以 `bridge T1` 开头)、写 T3 失败记录(带桥接日志的 `log_refs`),不写新指令;下一个 tick 没有动作;`factory logs run-001 --task plan-run-001` 打印出含 `tool down` 的日志、标 `[ok]`。
- **mock 证据关闭**(`test_without_mock_evidence_the_loop_does_not_turn`):不带 `--allow-mock-evidence`,晋级后一轮 `no actions`,没有提名标记、决策、配方候选。
- **MindAgent 链**(`test_the_mindagent_line_runs_inside_the_reconcile_loop`):复用计划 7 任务 88 的搭法(罐头带指导语后端、对手首句含情绪词、空库零提案、手工放一条不处理情绪的基线技能),但每一步都由 factory 装配的 `handlers` 经计划 8 的 `reconcile_once` 触发:晋级 `zengen-v2` → `start_agent_round zengen-v2`(零提案,库版本 `{"skill": "0", "memory": "0"}`)→ 同一模型不再开轮次 → 放基线技能 → 第二轮晋级 `zengen-v3` → `start_agent_round zengen-v3` 产出一个 `pending_review` 提案(`verification.method == "replay_behavior"`)→ 批准 → `admit_proposal` → 库版本 `{"skill": "2", "memory": "0"}`,两个值都是字符串。实时服务端是 `AgentRuntime` 起的进程内 loopback,情境与人格来自 `install-defaults`。
- **R19:轮次对 pending 的重入**(计划 7 的完成标准把"调和循环对 pending 轮次的重新触发"留给计划 8、9 验证;计划 8 Ruling 20 规定 `start_agent_round` 在 `agent_round_status` 为 `None` 或 `pending` 时触发,只有 `executed`、`failed` 挡住)。两条用例都先晋级 `zengen-v2` 并放一条不处理情绪的基线技能,让这一轮一定产出提案;只开 `--enable agent`,每一步都经 `factory reconcile --once`:
  - `test_a_transient_failure_leaves_the_round_pending_and_the_next_tick_resumes`:把 `factory.handlers.canned_backend_factory` 换成一个"第一次构造的后端在第 5 句发言时抛 `OSError`"的工厂(前两个情境各 2 句已落盘,第三个情境第一句抛)。计划 7 的约定是实例自己的异常不在任何 `except` 里、原样上抛(`run_deployment_episodes` 的 docstring),所以 ① 第一个 tick 抛 `OSError`,轮次产物是 `pending`,`runs/run-001/` 下没有 `failure-agent-*` 记录,两条部署 episode 已落盘;② 第二个 tick(同一棵树,后端恢复)再次打印 `start_agent_round zengen-v2`,已落盘的两条 episode 逐字节不变(计划 7 的 `_reusable` 复用),第三条补上,轮次 `executed`,提案恰好一个、id 是确定性的 `proposal-agent-round-zengen-v2-1`,没有失败记录;③ 第三个 tick 没有动作(`executed` 挡住)。
  - `test_a_crash_between_episodes_and_proposals_converges_on_the_next_tick`:把 `mindagent.rounds.package` 换成"第一次抛 `RuntimeError`、之后调真的"的替身,模拟进程在写完三条部署 episode 之后、写提案之前被杀。第一个 tick 抛出,轮次 `pending`、三条 episode 在、没有本轮的提案;第二个 tick 再次触发,三条 episode 逐字节不变,`package` 被调第二次,轮次 `executed`,磁盘上本轮的提案与轮次载荷的 `proposals` 一致且非空,没有失败记录;第三个 tick 没有动作。
  - 只数本轮(`proposal-agent-round-zengen-v2-*`)的提案:`train_and_promote` 期间基线 `zengen-v1` 的轮次也会被触发并写出它自己的提案,不计在内。
  - **先失败**:把计划 8 的触发条件改回 R19 之前的"只在没有轮次产物时触发"(`top.agent_round_status is None`),这两条失败、本任务其余 8 条照样通过;改回 R19 后 10 条全过。
- **读不出的轮次产物**(`test_an_unreadable_round_product_is_failed_by_the_handler_and_the_loop_moves_on`):计划 8 的视图把读不出的轮次产物当成"没有可用轮次",R19 让 `start_agent_round` 再次触发;计划 7 的轮次处理器在读不出时自己把它整份重写为 `failed`,而不是抛出异常让守护进程每个 tick 崩溃。测试先正常跑完 `zengen-v2` 的一轮,再把 `agent_rounds/agent-round-zengen-v2.yaml` 换成带合并冲突标记的内容并提交。之后第一个 tick 打印 `start_agent_round zengen-v2`,没有异常逃出;轮次产物被整份重写(文件里不再有 `<<<<<<<`),状态 `failed`,失败类别 `T3`、原因 `unreadable round product`,`run_id` 取自模型产物(`run-001`);`runs/run-001/` 下的 `failure-agent-*` 记录恰好多一份。第二个 tick 没有动作(`failed` 挡住)。**变异检查(撰写时做过)**:把计划 7 处理器开头的 `read_or_none` + `_fail_unreadable` 换成直接 `store.read_product`(不接读取异常),这条失败,本任务其余 9 条照样通过。
- **变异检查(撰写时做过)**:把提名的 `accept_mock_evidence` 改成 `None`,`test_the_whole_loop_turns_twice` 失败;把 `scheduler_config_loader` 的覆盖去掉,`test_a_new_round_without_confirmation_synthesizes_its_data` 失败;把 mock 运行器改回"全对",只有任务 103 的 `test_the_default_evaluator_writes_a_mock_graded_report`(它断言分数严格在 0 与 1 之间、画像是 `surface_match`)失败,本任务 10 个用例照样通过——计划 5 按 R21 丢掉空标签之后,全答对的报告不再让规划失败。

- [ ] **Step 1: 前置检查**

```bash
uv run --extra dev python scripts/check_factory_assumptions.py; echo "exit=$?"
```

Expected:21 行都以 `: OK` 结尾,最后一行 `all assumptions hold`,`exit=0`。有任何一行是 `MISSING` 或 `MISMATCH`:停下,把那几行原样报告,不往下做。

- [ ] **Step 2: 写测试**

新建 `tests/test_factory_e2e_loop.py`:

```python
"""完整闭环与 MindAgent 端到端(任务 108):全部经 factory 的命令行与计划 3、5、7、8 的真实代码。

人工动作(批准、确认、驳回)用写状态模拟,与 tests/test_mt_wire.py 同一手法(TypeScript 命令行的等价操作)。
诊断一律是 mock 后端;mock SoMEval 恒满分,所以第一轮要有一个低分的基线,提名才有正增益:
run-000 是人工登记的基线(全错的 mock 报告 + 已晋级的 zengen-v1)。
"""
import sys

import pytest

from controlplane.config import write_config
from controlplane.envelope import BigRef, Envelope, OwnerRef
from controlplane.evidence import diagnostic_grade
from controlplane.paths import (
    agent_round_file, backends_config_file, candidate_checkpoint_file, dataset_manifest_file,
    diagnostic_report_file, episodes_dir, failure_record_file, library_state_file, model_file, nomination_file,
    planner_decision_file, proposal_file, recipe_candidate_file, recipe_track_note_file, run_dir,
    skill_file,
)
from diagnostic.evaluator import make_diagnostic_evaluator
from diagnostic.scorer import MockJudgeBackend
from diagnostic.someval_adapter import MockSoMEvalRunner
from factory.cli import main
from factory.defaults import DEFAULT_EVALUATOR_CONFIG, DEFAULT_ITEM_POOL
from factory_fixtures import closed_loop_repo, produce_datasets, write_directive
from mindagent.library import load_library
from recipestore.gain import compute_gain
from recipestore.library import find_recipes, get_recipe

DIM = "mental_representation"  # 默认分类表唯一的一级维度
LOOP = ["--allow-mock-evidence", "--planner-backend", "heuristic"]
GOAL = {"id": "skill-goal", "version": 1, "level": "macro", "priority": 0, "trigger": {"turn_at_least": 0},
        "policy": {"instructions": ["推进目标"]}, "description": "baseline: ignores emotion"}


def _env(id_, type_, status, payload, links=()):
    return Envelope(id=id_, type=type_, version=1, created_at="2026-10-09T00:00:00Z", created_by=OwnerRef(),
                    status=status, schema_version=1, links=list(links), payload=payload)


def _seed_baseline(store):
    """run-000:全错的 mock 报告 + 已晋级的 zengen-v1(带 checkpoint_ref,规划 new 时要用)。"""
    cfg = DEFAULT_EVALUATOR_CONFIG
    wrong = MockSoMEvalRunner(fixed={i.id: ["X"] * cfg["sampling_k"] for i in DEFAULT_ITEM_POOL.items})
    make_diagnostic_evaluator(
        taxonomy_version=cfg["taxonomy_version"], item_pool_version=cfg["item_pool_version"],
        someval_runner=wrong, judge_backend=MockJudgeBackend(model_id="j", fixed={}),
        benchmark_version=cfg["benchmark_version"], tier=cfg["tier"], sampling_k=cfg["sampling_k"],
    )(store, "run-000", "cand-run-000")
    ckpt = BigRef("file:///ckpt/v1", "1" * 64, 1)
    store.write_product(model_file(1), _env("zengen-v1", "zengen_model", "effective",
                        {"promoted_from": "cand-run-000", "checkpoint_ref": ckpt.to_dict()},
                        ["cand-run-000", "run-000"]), "human: register baseline zengen-v1")


@pytest.fixture
def store(tmp_path):
    s = closed_loop_repo(str(tmp_path / "cp"), run_id=None)
    _seed_baseline(s)
    produce_datasets(s, write_directive(s, "run-001", dimension=DIM))
    return s


def tick(store, enable, *extra, env=None):
    """一轮 factory reconcile --once,返回打印出的动作行。"""
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(["reconcile", str(store.repo_root), "--once", "--enable", enable, *extra],
                    env={} if env is None else env)
    assert code == 0
    out = buf.getvalue().splitlines()
    return [] if out == ["no actions"] else out


def set_status(store, rel, status, **payload):
    env = store.read_product(rel)
    env.status = status
    env.payload.update(payload)
    store.write_product(rel, env, f"human: {env.id} -> {status}")


def directive(store, run_id):
    return store.read_product(f"{run_dir(run_id)}/directive.yaml")


def train_and_promote(store, run_id, enable, *extra):
    """派发 → mock 诊断 → 待审 → 人工批准 → 晋级。返回每个 tick 的动作行。"""
    seen = [tick(store, enable, *extra) for _ in range(3)]
    set_status(store, candidate_checkpoint_file(run_id), "approved")
    seen.append(tick(store, enable, *extra))
    assert store.read_product(candidate_checkpoint_file(run_id)).status == "effective"
    return seen


def test_the_whole_loop_turns_twice(store, capsys):
    enable = "nominate,track,admit"
    assert train_and_promote(store, "run-001", enable, *LOOP) == [
        ["dispatch_directive run-001"], ["request_diagnostic cand-run-001"],
        ["await_review cand-run-001"], ["promote_candidate cand-run-001"],
    ]
    assert diagnostic_grade(store.read_product(diagnostic_report_file("run-001")).payload) == "mock"
    assert compute_gain(store, "run-001", DIM).gain > 0.01

    # 提名 → 配方候选(repro 在场) → 门③ → 收录
    assert tick(store, enable, *LOOP) == ["nominate_recipe run-001"]
    assert store.read_product(nomination_file("run-001")).payload["result"] == "nominated"
    cand = store.read_product(recipe_candidate_file("run-001"))
    assert cand.status == "pending_review"
    assert set(cand.payload["repro"]) == {"errors", "warnings"}
    assert cand.payload["code_version"]["repo_commit"] is not None or \
        any("repo_commit" in e for e in cand.payload["repro"]["errors"])
    set_status(store, recipe_candidate_file("run-001"), "approved")
    assert tick(store, enable, *LOOP) == ["admit_recipe recipe-run-001"]
    assert [v.id for v in find_recipes(store, dimension=DIM)] == ["recipe-run-001"]
    assert get_recipe(store, "recipe-run-001").status == "effective"

    # 打开规划:启发式后端 clone 刚收录的配方;指令先停在 awaiting_confirmation
    enable = "plan,nominate,track,admit"
    assert tick(store, enable, *LOOP) == ["plan_next_round run-001"]
    decision = store.read_product(planner_decision_file("run-001"))
    assert decision.status == "executed", decision.payload["failure"]
    assert (decision.payload["template_version"], decision.payload["next_run_id"]) == ("v1", "run-002")
    assert decision.payload["decision"]["action"] == "clone"
    assert decision.payload["decision"]["source_recipe_id"] == "recipe-run-001"
    d2 = directive(store, "run-002")
    assert (d2.status, d2.payload["recipe_ref"]) == ("awaiting_confirmation", "recipe-run-001")
    assert "data_spec" not in d2.payload  # clone 复用配方锚定的清单
    assert tick(store, enable, *LOOP) == []  # 等人确认,闭环不动

    # 模拟 rismforge confirm run-002 → 第二轮
    set_status(store, f"{run_dir('run-002')}/directive.yaml", "pending")
    train_and_promote(store, "run-002", enable, *LOOP)
    assert store.exists(model_file(3))
    # 第二轮用的是 run-001 的清单,没有为 run-002 合成
    assert not store.exists(dataset_manifest_file("run-002", "sft"))

    # 战绩回写到 recipe-run-001;提名因增益为 0 写 skipped 标记。规划要等提名标记(CP-11),这个 tick 还不规划
    assert tick(store, enable, *LOOP) == ["record_track run-002", "nominate_recipe run-002"]
    assert store.exists(recipe_track_note_file("run-002"))
    assert store.read_product(nomination_file("run-002")).payload["result"] == "skipped"
    assert not store.exists(planner_decision_file("run-002"))
    # 下一个 tick:提名标记在场(skipped 也放行,且没有配方候选),规划落在 run-002(最大的 run)
    assert tick(store, enable, *LOOP) == ["plan_next_round run-002"]
    assert directive(store, "run-003").status == "awaiting_confirmation"

    capsys.readouterr()
    assert main(["cost", str(store.repo_root), "run-001"]) == 0
    assert "None" not in capsys.readouterr().out


def test_a_new_round_without_confirmation_synthesizes_its_data(store):
    enable = "plan,track,admit"
    train_and_promote(store, "run-001", enable, *LOOP, "--no-confirm-directives")
    # 没开提名,规划不等配方入库门(CP-11);没有配方可 clone,启发式后端走 new batch
    assert tick(store, enable, *LOOP, "--no-confirm-directives") == ["plan_next_round run-001"]
    d2 = directive(store, "run-002")
    assert d2.status == "pending" and d2.payload["data_spec"]["purpose"] == "training"
    assert not store.exists(dataset_manifest_file("run-002", "sft"))
    assert tick(store, enable, *LOOP, "--no-confirm-directives") == ["dispatch_directive run-002"]
    assert store.exists(dataset_manifest_file("run-002", "sft"))
    assert store.exists(dataset_manifest_file("run-002", "grpo_opd"))
    assert store.read_product(candidate_checkpoint_file("run-002")).status == "pending_review"


def test_only_the_largest_run_is_planned(store):
    """R5:run-001 已晋级、run-002 被驳回,两者都满足规划条件;只有 run-002 触发。"""
    train_and_promote(store, "run-001", "track", *LOOP)
    produce_datasets(store, write_directive(store, "run-002", dimension=DIM))
    for _ in range(3):
        tick(store, "track", *LOOP)
    set_status(store, candidate_checkpoint_file("run-002"), "rejected", reject_reason="not convincing")
    assert tick(store, "plan", *LOOP) == ["plan_next_round run-002"]
    assert not store.exists(planner_decision_file("run-001"))
    assert store.read_product(planner_decision_file("run-002")).status == "executed"


def test_planning_waits_for_the_recipe_gate(store):
    """CP-11:启用提名时,规划要等来源 run 的提名标记,且配方候选离开 pending_review 与 approved。"""
    enable = "plan,nominate,admit"
    train_and_promote(store, "run-001", enable, *LOOP)
    # 晋级后的 tick:只提名;这个 tick 的快照里还没有提名标记,规划不出现
    assert tick(store, enable, *LOOP) == ["nominate_recipe run-001"]
    assert store.read_product(nomination_file("run-001")).payload["result"] == "nominated"
    assert store.read_product(recipe_candidate_file("run-001")).status == "pending_review"
    # 配方候选停在 pending_review:多个 tick 都不规划
    for _ in range(2):
        assert tick(store, enable, *LOOP) == []
    assert not store.exists(planner_decision_file("run-001"))
    # 人工批准后的 tick:只收录;快照里候选是 approved,规划仍被挡着
    set_status(store, recipe_candidate_file("run-001"), "approved")
    assert tick(store, enable, *LOOP) == ["admit_recipe recipe-run-001"]
    assert not store.exists(planner_decision_file("run-001"))
    # 候选已是 effective:下一个 tick 才规划,并且 clone 刚收录的配方
    assert tick(store, enable, *LOOP) == ["plan_next_round run-001"]
    d = store.read_product(planner_decision_file("run-001"))
    assert d.status == "executed", d.payload.get("failure")
    assert (d.payload["decision"]["action"], d.payload["decision"]["source_recipe_id"]) == ("clone", "recipe-run-001")


def test_a_bridge_failure_fails_the_decision_and_stops_the_loop(store, capsys):
    """R18:规划桥接用尽重试 → 决策 failed、T3 失败记录(带桥接日志),此后不再触发规划。"""
    write_config(store, backends_config_file(), "backends", {"planner": {
        "backend": "broken_cli", "invocation_form": "cli", "model_name": "m", "prompt_template_version": "v1",
        "token_env": None, "retry_policy": {"max_attempts": 1, "backoff_base_s": 0},
        "command": [sys.executable, "-c", "import sys; sys.stderr.write('tool down'); sys.exit(3)"],
    }}, "human: a planner backend that always fails")
    train_and_promote(store, "run-001", "plan", "--allow-mock-evidence")
    assert tick(store, "plan", "--allow-mock-evidence") == ["plan_next_round run-001"]
    d = store.read_product(planner_decision_file("run-001"))
    assert d.status == "failed" and d.payload["failure"]["category"] == "T3"
    assert d.payload["failure"]["reason"].startswith("bridge T1")
    rec = store.read_product(failure_record_file("run-001", "plan-run-001")).payload
    assert (rec["category"], rec["point"]) == ("T3", "trainscheduler.plan")
    assert len(rec["log_refs"]) == 1
    assert not store.exists(f"{run_dir('run-002')}/directive.yaml")
    assert tick(store, "plan", "--allow-mock-evidence") == []  # 循环停住,等人
    capsys.readouterr()
    assert main(["logs", str(store.repo_root), "run-001", "--task", "plan-run-001"]) == 0
    out = capsys.readouterr().out
    assert "log_ref [ok]" in out and "tool down" in out


def test_without_mock_evidence_the_loop_does_not_turn(store):
    enable = "plan,nominate,track,admit"
    train_and_promote(store, "run-001", enable, "--planner-backend", "heuristic")
    assert tick(store, enable, "--planner-backend", "heuristic") == []
    for rel in (nomination_file("run-001"), planner_decision_file("run-001"), recipe_candidate_file("run-001")):
        assert not store.exists(rel)


def test_the_mindagent_line_runs_inside_the_reconcile_loop(store):
    """计划 7 任务 88 的环路,但每一步都由 factory 装配的 handlers 经计划 8 的 reconcile_once 触发:
    空库轮次零提案 → 放基线技能 → 下一个模型的轮次产出提案 → 门② → 收录 → 库版本是字符串。"""
    enable = "agent,admit"
    train_and_promote(store, "run-001", enable)
    assert tick(store, enable) == ["start_agent_round zengen-v2"]
    r = store.read_product(agent_round_file("zengen-v2"))
    assert r.status == "executed" and r.payload["proposals"] == [] and r.payload["run_id"] == "run-001"
    assert r.payload["library_version"] == {"skill": "0", "memory": "0"}
    assert tick(store, enable) == []  # 同一模型不再开第二轮

    store.write_product(skill_file("skill-goal"), _env("skill-goal", "skill", "effective", GOAL), "human: skill")
    store.write_product(library_state_file(), _env("library-state", "library_state", "executed",
                        {"skill_version": "1", "memory_version": "0", "admitted": []}), "human: library")
    produce_datasets(store, write_directive(store, "run-002", dimension=DIM))
    train_and_promote(store, "run-002", enable)
    assert tick(store, enable) == ["start_agent_round zengen-v3"]
    r3 = store.read_product(agent_round_file("zengen-v3"))
    assert r3.status == "executed" and r3.payload["library_version"] == {"skill": "1", "memory": "0"}
    (pid,) = r3.payload["proposals"]
    proposal = store.read_product(proposal_file(pid))
    assert proposal.status == "pending_review" and proposal.payload["verification"]["method"] == "replay_behavior"

    set_status(store, proposal_file(pid), "approved")
    assert tick(store, enable) == [f"admit_proposal {pid}"]
    assert store.read_product(proposal_file(pid)).status == "effective"
    lib = load_library(store)
    assert lib.version == {"skill": "2", "memory": "0"}
    assert all(isinstance(v, str) for v in lib.version.values())
    assert tick(store, enable) == []


# ---- R19:start_agent_round 对 pending 轮次的重入(计划 7 留给本计划验证) ----

class _Flaky:
    """第一次构造的后端在第 fail_after 次发言时抛 OSError(瞬态,原样上抛);之后构造的后端正常。"""

    def __init__(self, fail_after):
        self.fail_after = fail_after
        self.armed = True
        self.calls = 0

    def factory(self, model_id, ckpt):
        from factory.agent import CannedGuidedBackend
        inner = CannedGuidedBackend()
        if not self.armed:
            return inner
        self.armed = False
        outer = self

        class Backend:
            def respond_guided(self, *a):
                outer.calls += 1
                if outer.calls >= outer.fail_after:
                    raise OSError("model host went away")
                return inner.respond_guided(*a)

        return Backend()


def _agent_ready(store):
    """晋级 zengen-v2,并放一条不处理情绪的基线技能,让这一轮一定产出提案。"""
    store.write_product(skill_file("skill-goal"), _env("skill-goal", "skill", "effective", GOAL), "human: skill")
    store.write_product(library_state_file(), _env("library-state", "library_state", "executed",
                        {"skill_version": "1", "memory_version": "0", "admitted": []}), "human: library")
    train_and_promote(store, "run-001", "agent")


def _episode_files(store, run_id):
    root = store.repo_root / episodes_dir(run_id)
    return sorted(p.name for p in root.glob("deploy-*.yaml")) if root.exists() else []


def _proposals_on_disk(store, model_id="zengen-v2"):
    """该模型轮次写出的提案 id(skill_memory_proposals/<id>/proposal.yaml)。
    基线 zengen-v1 的轮次在 train_and_promote 期间也会被触发,它的提案不计在内。"""
    root = (store.repo_root / proposal_file("x")).parent.parent
    prefix = f"proposal-agent-round-{model_id}-"
    return sorted(p.parent.name for p in root.glob(f"{prefix}*/proposal.yaml")) if root.exists() else []


def _agent_failures(store, run_id):
    root = store.repo_root / run_dir(run_id)
    return sorted(p.name for p in root.glob("failure-agent-*.yaml"))


def test_a_transient_failure_leaves_the_round_pending_and_the_next_tick_resumes(store, monkeypatch):
    from factory import handlers as fh
    _agent_ready(store)
    flaky = _Flaky(fail_after=5)  # 第一个情境 2 句、第二个情境 2 句,第三个情境的第一句抛
    monkeypatch.setattr(fh, "canned_backend_factory", flaky.factory)

    # 第 1 次:瞬态异常原样上抛,轮次 pending,没有失败记录,前两条 episode 已落盘
    with pytest.raises(OSError, match="model host went away"):
        tick(store, "agent")
    r = store.read_product(agent_round_file("zengen-v2"))
    assert r.status == "pending" and r.payload["proposals"] == []
    assert _agent_failures(store, "run-001") == []
    done = _episode_files(store, "run-001")
    assert len(done) == 2
    before = {n: (store.repo_root / episodes_dir("run-001") / n).read_bytes() for n in done}

    # 第 2 次:同一棵树、后端恢复;R19 再次触发,复用已落盘的 episode 续跑,轮次 executed
    assert tick(store, "agent") == ["start_agent_round zengen-v2"]
    r = store.read_product(agent_round_file("zengen-v2"))
    assert r.status == "executed"
    assert len(r.payload["episode_ids"]) == 3 and len(_episode_files(store, "run-001")) == 3
    assert {n: (store.repo_root / episodes_dir("run-001") / n).read_bytes() for n in done} == before
    (pid,) = r.payload["proposals"]
    assert pid == "proposal-agent-round-zengen-v2-1"
    assert _proposals_on_disk(store) == [pid]
    assert _agent_failures(store, "run-001") == []

    # 第 3 次:executed 挡住
    assert tick(store, "agent") == []


def test_a_crash_between_episodes_and_proposals_converges_on_the_next_tick(store, monkeypatch):
    """模拟进程在写完部署 episode 之后、写提案之前被杀:package 抛一次 KeyboardInterrupt 以外的异常。"""
    import mindagent.rounds as mr
    _agent_ready(store)
    real_package = mr.package
    calls = []

    def dying_package(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("killed before packaging")
        return real_package(*a, **kw)

    monkeypatch.setattr(mr, "package", dying_package)
    with pytest.raises(RuntimeError, match="killed before packaging"):
        tick(store, "agent")
    assert store.read_product(agent_round_file("zengen-v2")).status == "pending"
    episodes = _episode_files(store, "run-001")
    assert len(episodes) == 3 and _proposals_on_disk(store) == []
    before = {n: (store.repo_root / episodes_dir("run-001") / n).read_bytes() for n in episodes}

    assert tick(store, "agent") == ["start_agent_round zengen-v2"]
    r = store.read_product(agent_round_file("zengen-v2"))
    assert r.status == "executed" and len(calls) == 2
    assert {n: (store.repo_root / episodes_dir("run-001") / n).read_bytes() for n in episodes} == before
    assert _proposals_on_disk(store) == r.payload["proposals"] != []
    assert _agent_failures(store, "run-001") == []
    assert tick(store, "agent") == []


def test_an_unreadable_round_product_is_failed_by_the_handler_and_the_loop_moves_on(store):
    """计划 7 修订 + 计划 8 Ruling 20:轮次产物读不出时,视图按"没有轮次"再次触发 start_agent_round,
    由轮次处理器自己把它整份重写为 failed(T3),而不是每个 tick 抛异常让守护进程崩溃循环。"""
    train_and_promote(store, "run-001", "agent")
    rel = agent_round_file("zengen-v2")
    tick(store, "agent")  # 正常跑完一轮,产物 executed
    path = store.repo_root / rel
    path.write_text("<<<<<<< HEAD\nstatus: executed\n=======\nstatus: pending\n>>>>>>> other\n", encoding="utf-8")
    store._git("add", rel)
    store._git("commit", "-q", "-m", "human: botched merge of the round product")
    before = _agent_failures(store, "run-001")

    assert tick(store, "agent") == ["start_agent_round zengen-v2"]  # 没有异常逃出 tick()
    r = store.read_product(rel)
    assert r.status == "failed"
    assert (r.payload["failure"]["category"], r.payload["failure"]["reason"]) == ("T3", "unreadable round product")
    assert r.payload["model_id"] == "zengen-v2" and r.payload["run_id"] == "run-001"
    assert "<<<<<<<" not in path.read_text(encoding="utf-8")  # 整份重写,不是补丁
    assert _agent_failures(store, "run-001") == sorted(set(before) | {"failure-agent-zengen-v2.yaml"})
    assert len(_agent_failures(store, "run-001")) - len(before) == 1

    assert tick(store, "agent") == []  # failed 挡住
```

- [ ] **Step 3: 跑测试**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_factory_e2e_loop.py`
Expected:`10 passed`。本任务只加测试,实现都在前面的任务与计划 1–8 里;测试一写就该通过。失败若指向前八份计划的实现,停下报告失败的断言,不要在 `factory` 里绕过、也不要改测试迁就。

Run: 全量命令。
Expected:`B+114 passed`,无警告。

- [ ] **Step 4: 提交**

```bash
git add tests/test_factory_e2e_loop.py
git commit -m "test: closed-loop and MindAgent end to end through the factory"
```

---

### Task 109: 跨计划核对、全量回归与改动范围

**Files:**
- Test: `tests/test_factory_cross_plan.py`(31 个用例)、`tests/test_factory_run_order.py`(3 个用例)

**Interfaces:**
- Consumes: `src/controlplane` 的源文件(只用 `ast` 读);计划 5 的 `trainscheduler.run_ids.run_number`;计划 8 的 `controlplane.reconcile._RUN_ID`(私有名字,只在这条核对里读);计划 3 的 `has_repro_evidence`、`baseline_run_of`、`recipestore.validate.RECIPE_TYPES`;计划 4 的 `trainplanner.context.RECIPE_TYPES`;计划 8 的 `apply_action`(`promote_candidate`)、`read_tree_state`、`resolve_baseline`;计划 7 的 `mindagent.rounds._read_model`(私有函数,只在这条核对里读);计划 5 的 `make_plan_handler`;计划 4 的 `HeuristicBackend`。
- Produces: 无新名字。

**跨计划核对**(每条在测试里各有一条或一组用例;编号供上文引用):

1. **"产出模型的 run"的两种判法。** 计划 3 的 `baseline_run_of` 取 `links` 里满足"`cand-<x>` 也在 `links` 里"的那一项;计划 8 的 `read_tree_state`(`ModelView.run_id`)与计划 7 的 `_read_model` 取 `links[1]`。在 `apply_action(promote_candidate)` 写出的真实模型产物上,对 `run-001`、`run-010`、`run-xyz` 三种 run id,三处结果相同;计划 8 的 `resolve_baseline` 选出的模型,`baseline_run_of` 解回同一个 run。改晋级的写法时这四处要一起改。
2. **审核门③"证据在场"。** 计划 3 的 `recipestore.admit.has_repro_evidence` 与计划 8 的 TypeScript `hasReproEvidence`(`cli/src/commands.ts`)共用下面这张用例表。Python 一侧在 `tests/test_factory_cross_plan.py` 的 `REPRO_CASES` 引用它;TypeScript 一侧是计划 8 任务 96 的 `cli/test/gates.test.ts`:前三行对应 `approves a recipe candidate that carries repro`(夹具 `RECIPE_PAYLOAD` 的 `repro`)、`... whose repro has empty errors and an array of warnings`、`... even when repro.errors is non-empty`,其余十行就是 `refuses a recipe candidate whose repro is ...` 循环里的十个值,顺序相同。任何一侧增删用例,另一侧照改。

   | # | `payload.repro` | 放行 |
   |---|---|---|
   | 1 | `{"errors": [], "warnings": ["interact runs are only highly similar"]}` | 是 |
   | 2 | `{"errors": [], "warnings": []}` | 是 |
   | 3 | `{"errors": ["unanchored ref"], "warnings": []}` | 是 |
   | 4 | 缺 `repro` 键 | 否 |
   | 5 | `null` | 否 |
   | 6 | `{}` | 否 |
   | 7 | `[]` | 否 |
   | 8 | `"ok"` | 否 |
   | 9 | `{"x": 1}` | 否 |
   | 10 | `{"errors": []}` | 否 |
   | 11 | `{"errors": {}, "warnings": []}` | 否 |
   | 12 | `{"errors": [], "warnings": null}` | 否 |
   | 13 | `{"errors": [], "warnings": "w"}` | 否 |

   (测试表里第 4、5 行的顺序是 `None`、`"<missing>"`,与 TypeScript 循环的 `undefined, null` 对应。)
3. **全对的变体族:已修复,见计划 5 Ruling 23 与设计 R21。** 现有 `diagnostic.error_classifier.classify_static_family` 对"基题与变体都答对"的变体族返回空标签 `""`,计划 8 的评估器因此写出 `error_type_distribution == {"": 1.0}`;计划 4 的 `PlanningContext` 拒绝空键。计划 5 的 `assemble_context` 现在经 `drop_no_error_label` 丢掉空标签(不重新归一化,全是空标签时得到空映射)再交给计划 4。`test_a_full_pass_report_can_be_planned` 是正向回归:用全答对的 mock 运行器经计划 8 的评估器写出真实形状的报告(断言里确实出现 `{"": 1.0}`),再经计划 5 的真实规划处理器与计划 4 的 `HeuristicBackend`(包一层记录器)规划,断言决策 `executed`、计划 4 收到的分布是 `[{}]`。撰写时在 R21 修订前的计划 5 上实测过这条会失败(决策 `failed`,原因 `error_type_distribution key: expected a non-empty string, got ''`),修订后通过。
4. **R5 的"最大 run"。** 计划 8 与计划 5 对"最大"的定义一致,都是 `run-<数字>` 的数值序。计划 8 的 `_latest_numbered_run` 只让匹配 `^run-([0-9]+)$` 的 run 参与,按整数取最大,一个都没有时不触发规划;`scratch` 之类的名字不参与"最大",但仍算在途轮。计划 5 的 `allocate_run_id` 用数值语义的 `blocking_run_dirs`:只有编号不小于新 id 的 `run-<数字>` 目录才算挡路,`run-9`、`test-x`、`zz` 都不挡路,`run-9` 与 `run-010` 同在时照常分配 `run-011`;`RunIdOrderError` 只剩防御性断言。本计划一侧由任务 108 的 `test_only_the_largest_run_is_planned`(真实处理器上,两个都满足条件的 run 只规划编号最大的那个)与 `tests/test_factory_run_order.py` 的三条(第 8 条)共同覆盖。
5. **`scheduler.yaml` 的九个键**与本计划的覆盖方式:`--allow-mock-evidence` 只经 `make_nominate_handler(accept_mock_evidence=...)` 进入计划 5(参数覆盖配置,计划 5 Ruling 4);`--confirm-directives` 经 loader 覆盖 `confirm_directives`;其余七个键组合根不碰。`SchedulerConfig` 的字段集由任务 106 的 `A5-4f` 核对。
6. **`backends.yaml` 的形状**(Ruling 8):计划 5、7 都不读 `backends.yaml`,桥接由组合根构造后注入(`BridgeDecisionBackend(bridge)`、`make_start_round_handler(bridge=...)`),所以形状只有本计划一处。
7. **R18 的落点**:计划 5 与计划 7 都把任何 `BridgeError` 升级为 T3 并写失败记录;任务 108 在真实子进程桥接上验证了计划 5 一侧(计划 7 一侧由它自己的任务 87 覆盖,本计划的 MindAgent 端到端不走桥接)。
8. **最大 run 的数值序。** 设计 §3.2 把"最大 run"定为 `run-<数字>` 的数值序,计划 8 用 `_RUN_ID = re.compile(r"^run-([0-9]+)$")` 与 `_latest_numbered_run` 实现,它自己的测试也按数值序写。`tests/test_factory_run_order.py` 在本计划一侧直接对计划 8 的纯函数 `reconcile(TreeState)` 再断言三件事:树里同时有 `run-9` 与 `run-011` 时 `plan_next_round` 选 `run-011`(两种列表顺序都试);`scratch` 目录不挡住 `run-002`;没有任何 `run-<数字>` 时不触发。按字符串序取最大的旧实现下三条都会失败(撰写时在计划 8 修订前的重放上实测过:分别选了 `run-9`、什么都不选、选了 `scratch`),所以它们能区分两种口径。计划 5 一侧的口径见第 4 条。
9. **配方类型集合。** 计划 3 的 `recipestore.validate.RECIPE_TYPES` 与计划 4 的 `trainplanner.context.RECIPE_TYPES` 是两份字面量,都是 `("batch", "interact", "agent_rsi")`;两个包互不导入(计划 4 的已知限制第 9 条),本计划是唯一同时导入两边的地方。`test_recipe_types_agree_between_recipestore_and_trainplanner` 断言两份集合相等且各自无重复,失败信息写明"两边都要改";计划 3 的字面量旁有反向注释指向这条测试。变异检查(撰写时做过):把计划 4 一侧删掉 `agent_rsi`,这条失败,信息里列出两边的集合。
10. **controlplane 不导入任何新包。** 设计 §11 的依赖方向要求 `controlplane` 不导入 `inference_bridge`、`recipestore`、`trainplanner`、`trainscheduler`、`actio`、`mindagent`、`factory`;各计划的边界测试只检查自己包往外的导入(计划 3 与 `actio` 一侧都没有反向检查)。`test_controlplane_imports_none_of_the_new_packages` 用 `ast` 解析 `src/controlplane` 下全部 `.py`,收集绝对导入的顶层包名,失败信息写出"哪个文件第几行导入了哪个包"。变异检查(撰写时做过):往 `controlplane/ledger.py` 末尾加一行 `import recipestore`,这条失败,信息为 `controlplane/ledger.py:138 imports recipestore`。
11. **run 编号的两份解析。** 计划 5 的 `trainscheduler.run_ids.run_number` 与计划 8 的 `controlplane.reconcile._RUN_ID` 各写了一份 run 编号正则;早先一边用 `\d`、一边用 `[0-9]`,遇到 Unicode 数字会分叉(计划 5 分配出来的 id 计划 8 不认,或反过来),现在两边与设计 §3.2 统一成 `^run-([0-9]+)$`。`test_run_number_parsers_agree` 对同一组名字比较两边:`run-1`、`run-001`、`run-999`、`run-1000`、`run-٥٠`(阿拉伯-印度数字 U+0665 U+0660)、`run-1b`、`run-`、`RUN-001`、`run-+1`、`scratch`;断言计划 5 解析出编号当且仅当计划 8 的正则匹配,且两边编号相等,失败信息写明"两边都要改"。其中 `run-٥٠` 正好区分 `[0-9]` 与 `\d`(`\d` 会匹配它),任何一边退回 `\d` 这条就失败。变异检查(撰写时做过):把计划 8 的 `_RUN_ID` 临时改回 `^run-(\d+)$`,只有 `test_run_number_parsers_agree[run-٥٠]` 失败,其余 9 个参数通过;改回后 10 个全过。
12. **分层规则。** 设计 §11 规定 `controlplane` 不导入 `diagnostic` 与 `mindtrainer`、`mindtrainer` 不导入 `diagnostic`、`diagnostic` 不导入 `mindtrainer`;原先只有计划 2、计划 8 里的一次性 grep(计划 8 另有一条 `diagnostic` 不导入 `mindtrainer` 的测试,与这里重复无妨)。`test_layer_rules_hold` 用 `ast` 扫 `src/` 下这三个包的真实源码(只看绝对导入),失败信息写出"文件:行号 imports 包"。撰写时在按计划 1–8 重放出的集成拷贝上确认现状成立;变异检查(撰写时做过):分别往 `controlplane/ledger.py`、`controlplane/daemon.py`、`mindtrainer/dispatch.py`、`diagnostic/evaluator.py` 末尾加一行越界导入,四次都失败,信息依次为 `controlplane/ledger.py:138 imports diagnostic`、`controlplane/daemon.py:360 imports mindtrainer`、`mindtrainer/dispatch.py:245 imports diagnostic`、`diagnostic/evaluator.py:112 imports mindtrainer`。
13. **仓库排他锁。** 计划 8 任务 98 的 `controlplane.lock` 只用标准库,锁文件在控制面仓库的 `.git/rismforge.lock`:它不在 `control-plane/` 下,也不被 git 追踪,所以任务 104 的"逐字节相同"比较不受影响;第 10 条(`controlplane` 不导入新包)与第 12 条(分层规则)的 `ast` 检查对它同样成立。`factory` 自己不取 `run` 的锁(Ruling 27):`run` 的锁由 `run_daemon` 取,`reconcile_one_tick` 在装配之前取。两条用例在任务 104,不在 `tests/test_factory_cross_plan.py`,所以任务 109 的用例数不变。

- [ ] **Step 1: 前置检查**

```bash
uv run --extra dev python scripts/check_factory_assumptions.py; echo "exit=$?"
```

Expected:21 行都以 `: OK` 结尾,最后一行 `all assumptions hold`,`exit=0`。有任何一行是 `MISSING` 或 `MISMATCH`:停下,把那几行原样报告,不往下做。

- [ ] **Step 2: 写测试**

新建 `tests/test_factory_cross_plan.py`:

```python
"""跨计划一致性核对(任务 109 Step 4):同一事实在两个计划里各有一处实现时,在真实产物上比对。"""
import pytest

from controlplane.daemon import apply_action, read_tree_state
from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import candidate_checkpoint_file, model_file, planner_decision_file
from controlplane.reconcile import Action
from diagnostic.evaluator import resolve_baseline
from factory.evaluation import build_mock_evaluator
from factory_fixtures import closed_loop_repo
from mindagent.rounds import _read_model
from recipestore.admit import has_repro_evidence
from recipestore.gain import baseline_run_of
from trainplanner.backends import HeuristicBackend
from trainscheduler.plan_handler import make_plan_handler

# 审核门③"证据在场"的共享用例表。TypeScript 一侧的同一张表在计划 8 任务 96 的
# cli/test/gates.test.ts("approves a recipe candidate ..." 三条与 "refuses a recipe candidate whose repro is ..."
# 的循环);两侧任何一侧改了,另一侧照改(本计划「跨计划核对」一节列出了这张表)。
REPRO_CASES = [
    ({"errors": [], "warnings": ["interact runs are only highly similar"]}, True),
    ({"errors": [], "warnings": []}, True),
    ({"errors": ["unanchored ref"], "warnings": []}, True),
    (None, False), ("<missing>", False), ({}, False), ([], False), ("ok", False), ({"x": 1}, False),
    ({"errors": []}, False), ({"errors": {}, "warnings": []}, False),
    ({"errors": [], "warnings": None}, False), ({"errors": [], "warnings": "w"}, False),
]


@pytest.mark.parametrize("repro,accepted", REPRO_CASES)
def test_has_repro_evidence_matches_the_shared_table(repro, accepted):
    payload = {"recipe_type": "batch"} if repro == "<missing>" else {"recipe_type": "batch", "repro": repro}
    assert has_repro_evidence(payload) is accepted


def _promoted(tmp_path, run_id, n):
    store = GitStore(str(tmp_path / "cp"))
    store.init()
    store.write_product(candidate_checkpoint_file(run_id), Envelope(
        id=f"cand-{run_id}", type="candidate_checkpoint", version=1, created_at="t", created_by=OwnerRef(),
        status="approved", schema_version=1, payload={}), "seed approved candidate")
    apply_action(store, Action("promote_candidate", f"cand-{run_id}", {"run_id": run_id, "model_version": n}))
    return store


@pytest.mark.parametrize("run_id,n", [("run-001", 1), ("run-010", 2), ("run-xyz", 7)])
def test_plan_3_and_plan_8_agree_on_which_run_produced_a_model(tmp_path, run_id, n):
    """计划 3 的 baseline_run_of 取"cand-<x> 也在 links 里"的那一项;计划 8 的 read_tree_state
    (与计划 7 的 _read_model)取 links[1]。在 promote_candidate 写出的真实模型产物上两者相同。"""
    store = _promoted(tmp_path, run_id, n)
    links = store.read_product(model_file(n)).links
    assert baseline_run_of(store, f"zengen-v{n}") == (run_id, "ok")
    (view,) = read_tree_state(store, enabled=frozenset({"start_agent_round"})).models
    assert view.run_id == links[1] == run_id
    assert _read_model(store, f"zengen-v{n}")[1] == run_id


def test_plan_8_evaluator_baseline_names_the_same_run(tmp_path):
    store = closed_loop_repo(str(tmp_path / "cp"), run_id=None)
    build_mock_evaluator(store)(store, "run-001", "cand-run-001")
    store.write_product(candidate_checkpoint_file("run-001"), Envelope(
        id="cand-run-001", type="candidate_checkpoint", version=1, created_at="t", created_by=OwnerRef(),
        status="approved", schema_version=1, payload={}), "seed approved candidate")
    apply_action(store, Action("promote_candidate", "cand-run-001", {"run_id": "run-001", "model_version": 1}))
    ref, scores = resolve_baseline(store, "run-002", "factory-bench-v1", backends="mock")
    assert ref == "zengen-v1" and scores
    assert baseline_run_of(store, ref)[0] == "run-001"


def test_a_full_pass_report_can_be_planned(tmp_path):
    """设计 R21 / 计划 5 Ruling 23 的回归:现有 diagnostic.error_classifier 对"基题与变体都答对"的变体族给
    空标签,真实报告里出现 error_type_distribution == {"": 1.0};计划 5 的 assemble_context 丢掉空标签再交给
    计划 4 的 PlanningContext,所以全答对的报告也能被规划,计划 4 收到的分布是空映射。"""
    from diagnostic.evaluator import make_diagnostic_evaluator
    from diagnostic.scorer import MockJudgeBackend
    from diagnostic.someval_adapter import MockSoMEvalRunner
    from factory.defaults import DEFAULT_EVALUATOR_CONFIG as C, DEFAULT_ITEM_POOL
    store = closed_loop_repo(str(tmp_path / "cp"), run_id=None)
    right = MockSoMEvalRunner(fixed={i.id: [i.reference_answer] * C["sampling_k"] for i in DEFAULT_ITEM_POOL.items})
    make_diagnostic_evaluator(taxonomy_version=C["taxonomy_version"], item_pool_version=C["item_pool_version"],
                              someval_runner=right, judge_backend=MockJudgeBackend(model_id="j", fixed={}),
                              benchmark_version=C["benchmark_version"])(store, "run-001", "cand-run-001")
    report = store.read_product("control-plane/runs/run-001/diagnostic_report.yaml").payload
    assert [p["error_type_distribution"] for p in report["error_profiles"]] == [{"": 1.0}]  # 真实形状
    store.write_product(candidate_checkpoint_file("run-001"), Envelope(
        id="cand-run-001", type="candidate_checkpoint", version=1, created_at="t", created_by=OwnerRef(),
        status="effective", schema_version=1, payload={}), "seed effective candidate")

    seen = []

    class Recording(HeuristicBackend):
        def decide(self, packed, *, run_id, task_id):
            seen.append([p.error_type_distribution for p in packed.context.diagnostic.error_profiles])
            return super().decide(packed, run_id=run_id, task_id=task_id)

    make_plan_handler(backend=Recording())(store, Action("plan_next_round", "run-001", {}))
    d = store.read_product(planner_decision_file("run-001"))
    assert d.status == "executed", d.payload.get("failure")
    assert seen and seen[0] == [{}]


def test_recipe_types_agree_between_recipestore_and_trainplanner():
    """计划 3 的 recipestore.validate.RECIPE_TYPES 与计划 4 的 trainplanner.context.RECIPE_TYPES 是两份字面量
    (两个包互不导入,计划 4 已知限制第 9 条)。这里是唯一同时导入两边的地方。"""
    from recipestore.validate import RECIPE_TYPES as recipestore_types
    from trainplanner.context import RECIPE_TYPES as trainplanner_types
    assert set(recipestore_types) == set(trainplanner_types), (
        f"recipestore.validate.RECIPE_TYPES {sorted(recipestore_types)} != "
        f"trainplanner.context.RECIPE_TYPES {sorted(trainplanner_types)}: 两边都要改,保持同一组配方类型"
    )
    assert len(set(recipestore_types)) == len(recipestore_types)
    assert len(set(trainplanner_types)) == len(trainplanner_types)


NEW_PACKAGES = ("inference_bridge", "recipestore", "trainplanner", "trainscheduler", "actio", "mindagent", "factory")


def test_controlplane_imports_none_of_the_new_packages():
    """设计 §11 的依赖方向:controlplane 不导入闭环集成新增的任何包。各计划的边界测试只查自己包往外的导入,
    本计划是唯一同时看得见所有新包的地方。只看绝对导入(相对导入留在 controlplane 内部)。"""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "controlplane"
    offenders = []
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if top in NEW_PACKAGES:
                    offenders.append(f"{path.relative_to(root.parent)}:{node.lineno} imports {top}")
    assert sorted(root.rglob("*.py")), "src/controlplane not found"
    assert not offenders, "controlplane must not import the new packages: " + "; ".join(offenders)


RUN_NAMES = ["run-1", "run-001", "run-999", "run-1000", "run-٥٠", "run-1b", "run-", "RUN-001", "run-+1",
             "scratch"]


@pytest.mark.parametrize("name", RUN_NAMES)
def test_run_number_parsers_agree(name):
    """计划 5 的 trainscheduler.run_ids.run_number 与计划 8 的 controlplane.reconcile._RUN_ID 各写了一份 run 编号
    正则(设计 §3.2:^run-([0-9]+)$)。两边对同一个名字必须同时认或同时不认,认出的编号相等;
    "run-٥٠"(阿拉伯-印度数字)用来钉住 [0-9] 与 \\d 的分叉。"""
    from controlplane.reconcile import _RUN_ID
    from trainscheduler.run_ids import run_number

    number = run_number(name)
    match = _RUN_ID.match(name)
    assert (number is not None) == (match is not None), (
        f"{name!r}: trainscheduler.run_ids.run_number -> {number!r}, controlplane.reconcile._RUN_ID matches: "
        f"{match is not None}; 两边都要改,保持同一个 run 编号口径(设计 §3.2)"
    )
    if match is not None:
        assert number == int(match.group(1)), f"{name!r}: {number} != {int(match.group(1))}; 两边都要改"


# 设计 §11 的分层规则:{源包: 不得导入的包}
LAYER_RULES = {
    "controlplane": ("diagnostic", "mindtrainer"),
    "mindtrainer": ("diagnostic",),
    "diagnostic": ("mindtrainer",),
}


def _top_level_imports(path):
    """一个 .py 文件里全部绝对导入的 (行号, 顶层包名)。相对导入留在包内,不看。"""
    import ast
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.lineno, node.module.split(".")[0]


def test_layer_rules_hold():
    """设计 §11 的三条分层规则,在真实源码上常驻检查:controlplane 不导入 diagnostic 与 mindtrainer,
    mindtrainer 不导入 diagnostic,diagnostic 不导入 mindtrainer。各计划里原先只有一次性 grep。"""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src"
    offenders = []
    for package, forbidden in LAYER_RULES.items():
        files = sorted((src / package).rglob("*.py"))
        assert files, f"src/{package} not found"
        for path in files:
            for lineno, top in _top_level_imports(path):
                if top in forbidden:
                    offenders.append(f"{path.relative_to(src)}:{lineno} imports {top}")
    assert not offenders, "layer rules (design §11) violated: " + "; ".join(offenders)
```

新建 `tests/test_factory_run_order.py`:

```python
"""最大 run 的数值序(设计 §3.2;计划 8 的 _latest_numbered_run):plan_next_round 只看 run-<数字> 里数值最大的那个。

直接对计划 8 的纯函数 reconcile(TreeState) 断言,不经处理器:字符串序下这三条都会失败
("run-9" > "run-011","scratch" > "run-002",没有编号 run 时 scratch 也会被规划)。"""
from controlplane.reconcile import RunView, TreeState, reconcile


def _run(run_id, *, candidate="effective", grade="real", decision=None):
    return RunView(run_id=run_id, directive_status="executed", recipe_ref="", candidate_status=candidate,
                   has_diagnostic=True, diagnostic_grade=grade, planner_decision_status=decision,
                   has_nomination_note=True, has_track_note=True)


def _plan_targets(runs):
    state = TreeState(runs=list(runs), enabled=frozenset({"plan_next_round"}))
    return [a.target_id for a in reconcile(state) if a.kind == "plan_next_round"]


def test_run_011_beats_run_9():
    assert _plan_targets([_run("run-9"), _run("run-011")]) == ["run-011"]
    assert _plan_targets([_run("run-011"), _run("run-9")]) == ["run-011"]


def test_a_scratch_directory_does_not_block_planning():
    # "scratch" 在字符串序上大于 "run-002";数值序只看 run-<数字>,它不挡路
    assert _plan_targets([_run("run-001"), _run("run-002"), _run("scratch", candidate=None, grade=None)]) == ["run-002"]


def test_nothing_is_planned_without_any_numbered_run():
    assert _plan_targets([_run("scratch"), _run("exp-a")]) == []
```

- [ ] **Step 3: 跑测试**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_factory_cross_plan.py tests/test_factory_run_order.py`
Expected:`34 passed`(共享用例表 13 个、`links` 判法 3 个、评估器基线 1 个、全答对可规划 1 个、配方类型 1 个、controlplane 反向依赖 1 个、run 编号解析 10 个、分层规则 1 个、数值序 3 个)。

- [ ] **Step 4: 全量回归**

Run: 全量命令。
Expected:`B+148 passed`,无警告。

- [ ] **Step 5: 提交**

```bash
git add tests/test_factory_cross_plan.py
git add tests/test_factory_run_order.py
git commit -m "test: cross-plan checks (producing run from model links, shared repro evidence table, a known context conflict)"
```

- [ ] **Step 6: 改动范围**

```bash
git diff --stat "$BASE"..HEAD
```

`BASE` 是任务 100 第 1 步记下的提交;若 shell 已换,用 `git log --oneline` 找到任务 100 提交的父提交代替(`HEAD~10` 只作退路,本计划共 10 个提交)。

Expected:只列出下面 28 个文件(九份计划按序重放、前八份提交之后再放本计划得到的输出;`+` 号条是 `git diff --stat` 自己画的):

```
 pyproject.toml                       |   2 +-
 scripts/check_factory_assumptions.py |  85 ++++++++
 scripts/run_factory.py               |  11 +
 scripts/run_factory_dev.py           |  11 +
 src/factory/__init__.py              |   6 +
 src/factory/agent.py                 | 146 +++++++++++++
 src/factory/bridges.py               |  71 +++++++
 src/factory/cli.py                   | 134 ++++++++++++
 src/factory/defaults.py              |  88 ++++++++
 src/factory/errors.py                |   6 +
 src/factory/evaluation.py            |  79 +++++++
 src/factory/handlers.py              |  92 ++++++++
 src/factory/readonly.py              |  75 +++++++
 src/factory/switches.py              |  41 ++++
 src/factory/wire.py                  | 196 +++++++++++++++++
 tests/factory_fixtures.py            | 128 +++++++++++
 tests/test_factory_boundaries.py     |  32 +++
 tests/test_factory_bridges.py        | 116 ++++++++++
 tests/test_factory_cli.py            | 105 +++++++++
 tests/test_factory_cross_plan.py     | 198 +++++++++++++++++
 tests/test_factory_e2e_frame.py      |  61 ++++++
 tests/test_factory_e2e_loop.py       | 401 +++++++++++++++++++++++++++++++++++
 tests/test_factory_evaluation.py     |  81 +++++++
 tests/test_factory_handlers.py       | 175 +++++++++++++++
 tests/test_factory_readonly.py       | 123 +++++++++++
 tests/test_factory_run_order.py      |  30 +++
 tests/test_factory_switches.py       |  58 +++++
 tests/test_factory_wire.py           | 214 +++++++++++++++++++
 28 files changed, 2764 insertions(+), 1 deletion(-)
```

`pyproject.toml` 的 `2 +-` 是 wheel 列表那一行。

- [ ] **Step 7: 守住不该动的文件**

```bash
git diff --stat "$BASE"..HEAD -- src/mindtrainer/wire.py scripts/run_controlplane.py src/controlplane src/diagnostic src/mindtrainer src/inference_bridge src/trainplanner src/recipestore src/trainscheduler src/actio src/mindagent src/simulator schemas cli
git diff "$BASE"..HEAD -- pyproject.toml | grep '^[-+]packages'
grep -rn "^from factory\|^import factory" src --include=*.py | grep -v "^src/factory/"
```

Expected:第一条无输出;第二条恰好两行(`-packages = [...]` 与以 `, "src/factory"]` 结尾的 `+packages = [...]`);第三条无输出。`git status --short` 只剩原本就有的 ` M cli/package-lock.json`、` M cli/package.json`(不要碰)。

本 Step 不提交。

---

## 完成标准

- 全量命令的通过数为 **B+148**、无警告;B 是任务 100 第 1 步实测的起点。
- `scripts/check_factory_assumptions.py` 21 行 `OK`。
- `factory run <repo>` 不带 `--enable` 时,与 `scripts/run_controlplane.py <repo>` 在同一仓库上写出的树与提交说明逐字节相同。
- `--enable` 的五个开关恰好覆盖 `CLOSED_LOOP_ACTIONS` 的六个处理器键,`admit` 同时启用两类收录;每个键都接到计划 3、5、7 的真实处理器。
- 经 `factory reconcile --once` 与模拟的人工动作,闭环从一条人工指令转到第二轮训练与第三条指令;MindAgent 链从空库转到提案收录、库版本是字符串;轮次遇瞬态异常或在写提案前被杀时停在 `pending`,下一个 tick 复用已落盘的 episode 收敛到 `executed`,再下一个 tick 不再触发(R19);轮次产物读不出时由处理器写成 `failed`,循环继续。
- 不带 `--allow-mock-evidence` 时闭环动作不触发;规划桥接失败时决策 `failed`、失败记录 T3、循环停住。
- `trace`、`cost` 的输出与 `controlplane.observe` 逐字相同;`logs` 对不可达的引用明说;三者都不改 `HEAD` 与工作区。
- 闭环模式缺配置、资产、桥接角色或凭证时,退出码 2、一行错误、树里不写东西;凭证值不出现在任何输出里。
- `mindtrainer/wire.py`、`scripts/run_controlplane.py` 一行未改;`controlplane` 不导入任何新包(「跨计划核对」第 10 条);设计 §11 的三条分层规则成立(第 12 条)。
- 全答对的诊断报告可以被规划(`test_a_full_pass_report_can_be_planned`,设计 R21)。
- 启用提名时规划等配方入库门:晋级后的 tick 只提名,配方候选停在 `pending_review` 期间不规划,收录之后才规划(`test_planning_waits_for_the_recipe_gate`,设计 CP-11)。
- 仓库被另一个进程锁住时,`run`、`reconcile` 退出码 2、`HEAD` 与产物不变、闭环模式下没有处理器构造器被调用;只读子命令照常输出;`run` 与 `reconcile` 结束后锁已放(任务 104,设计 CP-12)。

## 已知限制

1. **闭环模式的诊断一律是 mock 证据。** V1 没有真实 SoMEval 与真实 judge(设计 §15),所以默认入口下闭环永远不会自己转;开发入口打开 `--allow-mock-evidence` 才转(R17)。审核门会醒目标出 `[MOCK BACKENDS]`。
2. **mock 诊断对每个候选打同样的分**(Ruling 5)。第一次闭环要有人登记一个更低分的基线,提名才有正增益;之后各轮增益恒为 0,不再提名新配方,只有收录过的配方被 clone。闭环里不会出现退化标记,配方的自动 `deprecated` 不会被端到端触发。
3. **`logs` 看不到成功调用的桥接日志**(Ruling 10):树里只有失败记录登记的 `log_refs`;成功调用的日志在 `<repo>/.rismforge/logs/<run>/`(计划 1)。
4. **装配是一次性的。** 守护进程运行期间改了 `backends.yaml`、`evaluator.yaml` 或 `agent.yaml`,要重启才生效。`scheduler.yaml`、`training_defaults.yaml` 与规划模板由计划 5 的 loader 每次调用时重读,改了下一次规划就生效。
5. **规划、外部推理与部署会话都是同步的**(设计 §14 第 6 条):调用期间调和循环不转,审批被饿住。
6. **瞬态异常让守护进程退出**,与派发器、处理器的既有约定相同,需要外部监督者重启。`factory run` 自己不重试。
7. **默认诊断资产是最小夹具**(Ruling 6),一个一级维度、两道题,只证明流程;规划器看到的一级分数只有一个维度。
8. **MindAgent 的模型后端是罐头**(Ruling 23):不加载 checkpoint,话语是指导语拼成的;对手位是罐头台词。部署 episode 只证明技能的路由与轨迹,不代表任何模型能力。
9. **`repo_commit` 是组合根所在代码仓库的 HEAD**(Ruling 20),不是产出那一轮时的提交:守护进程跨提交长跑时,配方候选记的是提名那一刻的代码版本。
10. **重入时外部推理成本可能少记**(设计 §14 第 11 条)。R19 重入后计划 7 会重跑 `propose`,计划 5 遇到非桥接的瞬态异常后会从头规划;重跑的桥接调用沿用同一组 `(task_id, cost_type)`,计划 1 的账本以先到者为准,会把重跑的成本静默去重。所以 `factory cost` 打出的外部推理成本是下限,不是实际花费。
11. **集成缺陷的修补任务是约定,不是已有任务**(设计 SYS-21、Ruling 26)。本计划落地后发现的跨计划缺陷追加编号 110 起的修补任务,允许改对应的包,并重跑任务 106 至 109;目前没有任何 110 以后的任务,施工计划里也没有为它们预留文件或用例数。
12. **规划等配方入库门让每一轮多停一次等人**(设计 CP-11)。启用提名时,规划要等配方候选过门③;人手放进树的候选没有提名标记,规划永不触发,要人手写下一条指令或关掉提名开关。端到端里 `nominate` 与 `plan` 同开时,`plan_next_round` 最早出现在配方收录之后的那个 tick。
13. **仓库锁只挡写入路径**(设计 CP-12、Ruling 27)。`run` 与 `reconcile` 取锁,只读子命令不取;TypeScript 命令行不取锁,与守护进程同时提交时 git 自己的索引锁冲突仍会让一方失败,尚未复现。进程被杀后锁是否由操作系统放掉,取决于计划 8 的实现用文件锁而不是"文件存在即占用"的标记文件,见「未验证项」第 8 条。

## 未验证项

1. **在九份计划按序重放的拷贝上验证(最终文本)。** 做法:把 `main@0385004` 拷贝一份(全量 820 通过),用脚本按设计 §12 的顺序重放计划 1–9,包括写出「新建 / 整个替换 / 末尾追加」的代码块,`git apply` 每个 `diff` 块与 `git apply <<'PATCH'` 块(计划 6 里作为期望输出展示的 `.pyi` 差异除外),运行改文件的 heredoc 脚本,并按计划 6 的步骤重新生成协议代码。各计划放完后的收集数为 820 → 1041 → 1186 → 1383 → 1572 → 1807 → 1992 → 2144 → 2359 → 2507,增量依次 +221、+145、+197、+189、+235、+185、+152、+215、+148,与各计划声明的增量逐一相等;本计划的 +148 与任务 100 至 109 声明的增量之和相等。前八份提交之后再单放本计划:`git diff --stat` 是 Step 6 列出的 28 个文件,依赖核对脚本 21 行全部 `OK`。九份放完的全量实跑:**2507 passed**(= B+148,`-W error` 下无警告)。逐任务的数(B+19、B+29 等)由增量推算,没有逐任务重放。
2. **重放不等于落地。** 集成拷贝是计划文本的重放,不是各计划实施者真实提交的代码;实施者若偏离了自己计划里的代码块,结果以实际运行为准。
3. **TypeScript 一侧只在拷贝里跑过。** 在一份只含 `cli/` 的临时拷贝里(`package.json` 与 `package-lock.json` 取 `HEAD` 已提交的版本,`vitest` 声明 `^1.6.0`,装到的是 1.6.1)按文档顺序应用了计划 8 的命令行改动(新建 `cli/test/gates.test.ts`,再 `git apply` 三份补丁),`npm ci --ignore-scripts` 装依赖,`npx tsc --noEmit` 无输出,`npx vitest run` 为 `Tests  64 passed (64)`,与计划 8 的 Expected 相同;「跨计划核对」第 2 条的 13 行共享用例在 vitest 的输出里逐条对得上(3 条 approve、10 条 refuse)。真实仓库的 `cli/` 没有动过。
4. **变异检查只做了十二处**(任务 105 的证据开关、任务 107/108 的提名证据参数、确认覆盖、mock 答案、Interact 的 deadline、计划 8 的 R19 触发条件、计划 7 的读取异常处理、任务 109 的配方类型集合、controlplane 反向导入、分层规则、run 编号正则退回 `\d`、全答对报告在 R21 修订前后的计划 5),都被测试抓住;其中 Interact 的 deadline、计划 7 的读取异常处理、分层规则与 run 编号正则四处在最终重放的文本上又做了一次。没有做系统性的变异测试。
5. **`CliBridge` 只用 `sys.executable -c` 的假可执行体测过**,没有接真实的 `claude` 或 `codex` 命令行(设计 §14 第 1 条);规划经桥接的正向端到端没有,只有失败路径,见 Ruling 25。
6. **`factory run` 的长循环**只以 `--max-ticks` 跑过少数几轮;`AgentRuntime` 在长跑中的端口与通道占用、真实 `run_daemon` 的耗时都没有测;Interact 的 15 秒 deadline 只用假 stub 验证了参数,没有真的等它超时。
7. **逐任务的红绿输出没有重放。** 本计划的代码块与测试是在九份计划按序重放的拷贝里整体放过一次,合计收集数与全量结果如第 1 条;每个任务内部「先失败、后通过」的中间输出,以及任务 104、105、106、108 里设计修订之后改动的 Expected 行,没有按步骤重放核对。
8. **对 `controlplane.lock` 的两点假设。** (a) `RepoLockedError` 的 `str` 是锁文件路径(`factory.cli.main` 把它拼进 `factory: error: repository is locked by another process: <路径>`);(b) 锁在进程之间互斥,且进程被杀后由操作系统放掉(`held_lock` 另起子进程持锁,不依赖 flock 与 lockf 的同进程语义)。任务 106 的 A8-1、A8-2 只核对名字与参数,不核对这两点;不符时报告,不在本计划里改。
