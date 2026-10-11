# 桥接与地基:路径、指令确认态、记账、配置信封、证据等级与外部推理桥接 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 一次性落下闭环集成设计里所有后续计划共用的地基:全部新增路径函数与 `SUBDIRS`、非审核类的 `awaiting_confirmation` 状态、统一的成本与失败写入函数 `controlplane.ledger`、`BackendConfig` 的命令行扩展与配置信封读写、唯一一处证据等级实现 `controlplane.evidence`,以及新顶层包 `inference_bridge`(`CliBridge` 与测试替身 `ScriptedBridge`)。调和循环的行为不变:`reconcile.py`、`daemon.py`、`observe.py` 一行不改。

**Architecture:** 地基全部放在 `controlplane` 里,只加不改语义:路径是纯字符串函数;状态机只多一行迁移表;`ledger` 与 `config` 的新函数读写都走 `GitStore` + `Envelope`。桥接是新顶层包 `inference_bridge`,依赖方向 `inference_bridge → controlplane`,`controlplane` 不导入它(有测试)。`CliBridge` 把子进程调用收进一个可注入的 `Runner`,退避的 `sleep` 也可注入,所以重试、超时、脱敏都能脱离真实外部工具测;另有几条用例用 `python -c` 做假可执行体真起子进程。日志过 `redact` 后写到 git 不追踪的目录,树里只留 `BigRef`。

**Tech Stack:** Python 3.11+ / uv / pytest;**无新增第三方依赖**(只用标准库 `subprocess`、`hashlib`)。

**Spec:**
- `docs/superpowers/specs/2026-10-09-closed-loop-integration-design.md`:§3.4 的状态机部分、§4.1 的路径、§5 全部、§11 的契约(名字逐字照用)。R1 至 R21 已定,本计划不推翻(R19 至 R21 是本计划落盘之后补的,与桥接和基础设施无交集)。
- 上游:`docs/superpowers/specs/2026-09-24-rismforge-subsystem-design.md` 补充 D.4(`CostRecord`)、E.2(T1/T2/T3)、E.5(`FailureRecord`、`RetryPolicy`)、G.3(凭证只存变量名)。
- 前置:`main` 在 `0385004`,全量基线 **820 通过**、无警告。

**依赖已有产出的名字(已逐个读源码核实)**

| 来源 | 名字 |
|---|---|
| `controlplane.paths` | `CONTROL_PLANE_ROOT`、`SUBDIRS`、`run_dir`、`backends_config_file` |
| `controlplane.envelope` | `Envelope`、`OwnerRef`、`BigRef`(`uri`、`sha256`、`size_bytes`、`fetch_hint`,`to_dict()`) |
| `controlplane.gitstore` | `GitStore`(`init`、`exists`、`write_product`、`read_product`、`list_products`、`_git`、`repo_root`) |
| `controlplane.states` | `NONREVIEW_TRANSITIONS`、`can_transition`、`next_states`、`assert_transition`、`InvalidTransitionError` |
| `controlplane.config` | `BackendConfig`、`load_backend_config`、`resolve_credential`、`redact`、`MissingCredentialError`、`CredentialInGitTreeError`、`_FORBIDDEN_SECRET_KEYS` |
| `controlplane.observe` | `sum_costs`、`cost`、`trace`(只读;本计划不改) |
| `controlplane.reconcile` / `controlplane.daemon` | `reconcile`、`TreeState`、`DirectiveView` / `reconcile_once`(只在测试里用;本计划不改) |
| `simulator.stubs` | `diagnostic_evaluator_stub`(只在证据等级的测试里用) |

**本计划产出、后续计划消费的名字(设计 §11 与补充契约)**

| 模块 | 名字 |
|---|---|
| `controlplane.paths` | `recipe_candidate_file(run_id)`、`recipe_dir(run_id)`、`recipe_file(run_id)`、`recipe_lineage_file(run_id)`、`recipe_track_file(run_id)`、`proposal_file(proposal_id)`、`skill_file(skill_id)`、`memory_file(mem_id)`、`library_state_file()`、`agent_round_file(model_id)`、`planner_decision_file(run_id)`、`nomination_file(run_id)`、`recipe_track_note_file(run_id)`、`run_trace_file(run_id)`、`failure_record_file(run_id, task_id)`、`scheduler_config_file()`、`training_defaults_file()`、`planner_prompt_file(version)`;`SUBDIRS` 末尾追加 `recipe_candidates`、`skill_library`、`memory_library`、`agent_rounds` |
| `controlplane.states` | 非审核类 `awaiting_confirmation → pending \| failed` |
| `controlplane.ledger` | `record_cost(store, run_id, *, task_id, cost_type, gpu_hours=None, tokens=None, calls=None, usd=None) -> None`、`record_failure(store, run_id, *, task_id, category, point, retries, reason, log_ref=None) -> str`、`FAILURE_CATEGORIES` |
| `controlplane.config` | `BackendConfig.command: list[str] \| None`、`BackendConfig.timeout_s: float = 120.0`、`token_env: str \| None`;`resolve_credential(...) -> str \| None`;`read_config(store, rel_path) -> dict`、`write_config(store, rel_path, config_id, payload, message) -> None`、`ConfigTypeError`、`CONFIG_TYPE = "config"` |
| `controlplane.evidence` | `diagnostic_grade(payload: dict) -> str`、`evidence_acceptable(grade: str, accept_mock: bool) -> bool`、`GRADES` |
| `inference_bridge` | `BridgeResult`、`BridgeError(category, reason="", *, attempts=0, log_ref=None)`、`InferenceBridge`、`CliBridge`、`ScriptedBridge`、`ScriptExhaustedError`、`RunOutcome`、`Runner`、`subprocess_runner`、`record_bridge_cost`、`BRIDGE_COST_TYPE = "external_inference"` |

`Handler = Callable[[GitStore, Action], None]` 与 `Evaluator` 由计划 8 在 `controlplane.daemon` 里定义,本计划不碰 `daemon.py`。planner 提示模板(`type: prompt_template` 的信封)由计划 4 经 `GitStore.write_product` 写到 `planner_prompt_file(version)`;本计划只给路径。

**本计划的裁决(设计没规定、或与现有代码冲突的细节;要改的话改这里)**

- **Ruling: 路径函数的形参语义 —— 形参叫 `run_id`、`model_id` 的,函数自己加前缀(`recipe-<run>`、`agent-round-<model>`);形参叫 `proposal_id`、`skill_id`、`mem_id` 的,传入的就是带前缀的产物 id(`proposal-…`、`skill-…`、`mem-…`),文件名等于 id。** —— 原因:设计 §4.1 的路径是 `recipes/recipe-<run>/`、`agent_rounds/agent-round-<model>.yaml`,而 `skill-<id>`、`mem-<id>`、`proposal-<round>-<n>` 的 `<id>` 是什么设计没定,让产物 id 与文件名一一对应最不容易错。 —— 判错的代价:后续计划若以为 `skill_file("ack")` 会补 `skill-` 前缀,文件名会与产物 id 不一致;改成补前缀只动 `paths.py` 三行和对应断言。
- **Ruling: `record_cost` 同一 `(task_id, cost_type)` 先到者为准,重复调用什么都不写(不改文件、不提交);每条记录额外带 `calls`(已知时)与 `recorded_at`;`run_trace` 首次创建时 id 为 `trace-<run>`、`type: run_trace`、状态 `executed`、`links: [run_id]`,已存在时只改 `cost_records`,其余字段与状态原样保留。** —— 原因:设计 §5.2 要求幂等;"更新"会让崩溃重放写出与第一次不同的数,`run_trace` 的状态按设计 §4.1 是 `executed`,而现有测试 fixture 用的是 `effective`,不能覆盖。 —— 判错的代价:同一任务想补记更准确的 token 数时做不到,只能换 `task_id`。
- **Ruling: `record_cost` 与 `record_failure` 对明显写错的入参抛 `ValueError` 且什么都不写:`run_id`、`task_id` 都必须是单个非空路径段(不含 `/`、`\`,不是 `.`、`..`);`cost_type` 为空;数值为负、为 NaN 或无穷、为 `bool`,或 `tokens`、`calls` 不是 `int`;`category` 不在 `T1/T2/T3`;`retries` 不是非负 `int`。** —— 原因:`run_id` 拼进目录 `runs/<run>/`,为空时 `run_trace` 会写到 `control-plane/runs/` 本身,`../x` 会越出 runs 目录,任意 id 还会新建 run 目录、干扰 `_iter_run_ids` 的枚举,进而影响"最大 run"的判定(设计 §3.2,按 run 编号的数值序);`task_id` 拼进文件名 `failure-<task>.yaml`;`tokens=True` 能通过 `sum_costs` 却是错数。 —— 判错的代价:调用方传 `numpy` 整数之类会被拒,要先转 `int`。
- **Ruling: `FailureRecord` 的载荷字段为 `run_id、task_id、category、point、retries、reason、log_refs、recorded_at`;L3 引用存成 `log_refs` 列表(没有日志时为空列表),不是单个 `log_ref` 键;同一 `(run_id, task_id)` 只写一次,已存在则原样返回路径。产物 id 为 `failure-<run>-<task>`,状态 `executed`,`links: [run_id]`。** —— 原因:补充 E.5 的"失败类别、失败点、已重试次数、最终判定、原始错误 L3 引用、时刻"逐项对应;`observe.trace` 只认识 `payload["log_refs"]`,存成列表 `trace` 才会打印引用可达性(有测试)。失败记录是留痕,不该被后来的重试覆盖。 —— 判错的代价:读方若按单数 `log_ref` 取会拿不到;同一任务二次失败要换 `task_id`(如 `plan-1-retry2`)才能留第二份。
- **Ruling: `BackendConfig` 的 `token_env` 键仍必须在场(缺键照旧 `KeyError`),值可以是变量名或显式 `null`;空串或非字符串抛 `ValueError`。`command` 必须是非空、元素为非空字符串的列表;`timeout_s` 必须是正数(`bool` 不算),缺省 `120.0`,一律转成 `float`。凭证值字段仍按现有 `_FORBIDDEN_SECRET_KEYS` 检查顶层,字段名比较改为 `k.lower()`(`Token`、`API_KEY` 也被拒);`timeout_s` 拒绝 NaN 与无穷;`env_allowlist` 缺省 `None`(用模块常量 `DEFAULT_ENV_ALLOWLIST`),给了就必须是元素为非空字符串的 `list`,空 `list` 合法;`workdir` 缺省且只接受 `"empty_tmp"`。** —— 原因:设计 R15 写的是"允许显式 `null`",显式比"漏写就当自带登录态"安全;现有 `tests/test_config.py` 不改仍通过。 —— 判错的代价:老配置漏写 `token_env` 的会继续报 `KeyError`,要显式写 `token_env: null`。
- **Ruling: 配置信封:`write_config` 写 `type: config`、状态 `effective`,载荷相同且 id 相同时不写不提交,否则 `version` 加一;路径必须在 `control-plane/config/` 下且以 `.yaml` 结尾;路径先 `posixpath.normpath`,规范化前后必须一致且不含 `..` 段(`control-plane/config/../runs/x.yaml` 被拒);`config_id` 不能为空;目标位置已有非 `config` 信封时拒绝覆盖(`ConfigTypeError`);写前与读后都**递归**检查凭证值字段名(按小写比较),命中抛 `CredentialInGitTreeError`(只报键路径,不报值)。`read_config` 返回载荷的深拷贝,文件缺失抛 `FileNotFoundError`。** —— 原因:配置是"进 git 树的 L-A 层",递归检查挡住把凭证塞进嵌套配置;`planner_prompt_file` 在 `config/prompts/` 下但类型是 `prompt_template`,拒绝覆盖非配置信封,防止配置写方误盖模板。 —— 判错的代价:某份配置若真有名为 `token`、`secret` 之类的合法键,会被拒,要改键名。
- **Ruling: `diagnostic_grade` 对 `stub` 字段按真值判定;只有 `backends` 为 `real` 且 `dimension_scores` 是非空列表才判 `real`;没有 `backends` 标记、取值不认识(含 `null`)、没有分数,都判 `stub`(失败关闭,设计 DX-12);载荷不是 `dict` 抛 `TypeError`。`evidence_acceptable` 收到 `GRADES` 以外的等级抛 `ValueError`。** —— 原因:任务说明的规则原样实现;"无法判断就按最弱证据算"能保证判错时闭环停住而不是被假证据推着走。现有 `run_diagnostic` 的报告没有 `backends` 标记,判 `stub`;计划 8 让 `write_report`、`run_diagnostic`、`resolve_baseline` 的 `backends` 成为必填关键字参数(设计 DX-12),之后写出的报告都带标记。 —— 判错的代价:手写的或旧版编排器写出的无标记报告永远不可接受,有人写 `backends: "Real"` 之类也会被当成 `stub`,闭环不转,要人补上或改正标记。
- **Ruling: 桥接日志默认放 `<store.repo_root>/.rismforge/logs/<run_id>/<task_id>-<k>.log`(`k` 从 1 递增,不覆盖);首次写日志时在日志根目录放一个内容为 `*` 的 `.gitignore`,另在本仓库 `.gitignore` 加 `.rismforge/`;构造时可用 `log_dir` 改到任意位置;既没有 `store` 也没有 `log_dir` 时不写日志,`log_ref` 为 `None`。** —— 原因:设计 §5.1 的原文要"仓库外的日志目录,加进 `.gitignore`"(设计现已按本 Ruling 收窄,见设计 §5.1)。控制面仓库是 `GitStore` 指向的那个 git 仓库,不是本代码仓库,改它的 `.gitignore` 等于写一个被追踪的文件;日志根目录自带 `*` 的 `.gitignore` 不需要碰任何被追踪的文件,也不受仓库自身 `.gitignore` 是否存在影响(有测试用 `git status --porcelain --untracked-files=all` 与 `git ls-files` 证明)。 —— 判错的代价:日志实际在控制面仓库的工作区里,只是对 git 不可见;有人 `git add -f` 仍能加进去。要真正放到仓库外,构造时传 `log_dir`。
- **Ruling: `CliBridge` 的失败分类:超时、非零退出、去掉空白后为空的输出算 T1 并重试;子进程启动阶段的任何 `OSError`(不存在、无权限、`NotADirectoryError`、无 shebang 脚本的 ENOEXEC、参数过长的 E2BIG……)算 T2,立即抛,不重试;子进程输出按 `encoding="utf-8", errors="replace"` 解码,不会因非 UTF-8 输出抛 `UnicodeDecodeError`;缺凭证在**构造时**抛现有的 `MissingCredentialError`(只报变量名),不是 `BridgeError`。退避是 `backoff_base_s * 2**(attempt-1)`,只在两次尝试之间睡,`retry_policy` 缺键时取 `max_attempts=3`、`backoff_base_s=1.0`,非法值在构造时抛 `ValueError`。** —— 原因:设计 §5.1 只列了三种 T1;起不来的可执行文件重试多少次都一样,按 E.2 是 T2;若让普通 `OSError` 或 `UnicodeDecodeError` 原样上抛,按 R18 会被当瞬态,守护循环退出、重启后又撞上同一处,形成循环;缺凭证是配置问题,早失败比等到第一次调用好。 —— 判错的代价:如果外部工具在 PATH 上是被异步装上的(罕见),第一次调用会直接 T2。
- **Ruling: `CliBridge` 默认隔离子进程:环境变量只传「白名单 ∪ `{token_env}`」与 `CliBridge` 持有的环境的交集(白名单取 `BackendConfig.env_allowlist`,为 `None` 才用 `DEFAULT_ENV_ALLOWLIST`,给了空 `list` 就只传 `token_env`;`token_env` 为 `None` 时不传任何凭证变量);工作目录是每次 `complete` 调用新建的空临时目录,同一次调用的所有尝试共用它,调用结束(成功、T1 耗尽、T2)一律删除;`CliBridge` 自己持有的完整环境不变,只用于解析 `token_env` 与脱敏。`DEFAULT_COMMANDS`(`claude` 关闭全部工具、`codex` 开只读沙箱)只是放在 `inference_bridge` 里的常量,不自动写进任何配置。** —— 原因:设计 §5.1 与 BR-8 要「外部推理只产出文本」由配置强制而不是靠约定;整份环境交给子进程会把守护进程里的其他凭证一起交给外部工具,而脱敏只认 `token_env` 那一个值。 —— 判错的代价:白名单不含 `PATH` 时子进程按它自己的环境找可执行文件,找不到就是 T2;工具的登录态依赖白名单之外的变量时调用失败,要把变量名加进 `env_allowlist`;空临时目录不阻止工具按绝对路径读写 `HOME` 下的文件。
- **Ruling: `BridgeError` 的构造签名为 `BridgeError(category, reason="", *, attempts=0, log_ref=None)`,`str(exc)` 为 `"<category>: <reason>"`;`category` 只能是 `T1`、`T2`。`CliBridge` 从不解析外部工具的输出,`BridgeResult.input_tokens`、`output_tokens` 恒为 `None`,`text` 是去掉首尾空白的标准输出;记账的 `calls` 是本次成功用掉的尝试次数,`tokens` 省略。异常信息里带脱敏后的 stderr 末尾 300 个字符;启动失败时只在 `except` 块里算出脱敏后的原因,出块后再抛 `BridgeError`,所以它的 `__cause__` 与 `__context__` 都是 `None`。** —— 原因:设计 §14 第 1 条说不知道 JSON 输出里有没有用量,所以不猜;原异常(如 `PermissionError` 的 `strerror`)可能带凭证,`raise ... from None` 只断 `__cause__`,`__context__` 仍挂着原异常,在块外抛才能让异常链上一个原异常都不挂。 —— 判错的代价:成本表里外部推理的 token 一直是 0,只有调用次数;以后确认了输出格式,要在 `CliBridge` 里加一个可选的用量解析器。
- **Ruling: `ScriptedBridge(script, *, model="scripted", store=None)`:脚本项可以是字符串、`BridgeResult` 或 `BridgeError`;构造时传了 `store` 也照 `CliBridge` 一样记账;脚本用完再调用抛 `ScriptExhaustedError`,它**不是** `BridgeError` 的子类;`run_id`、`task_id` 的路径段校验与 `CliBridge` 共用 `inference_bridge.base._check_segment`,不合法时抛 `ValueError` 且不消耗脚本。** —— 原因:测试替身要能替代真桥接走完记账路径;脚本用完是测试写错,不能被调用方当 T1/T2 吞掉。 —— 判错的代价:无。
- **Ruling: `awaiting_confirmation` 只是初始状态,没有任何状态迁入它;它的出口是 `pending`(人确认)与 `failed`(人放弃)。证明 `reconcile` 不为它产生动作,用的是现有代码路径:`reconcile` 只对 `status == "pending"` 的指令产生动作,所以不需要改 `reconcile.py`。迁移表按产物种类定义,所以 `awaiting_confirmation` 对所有非审核产物都合法,目前只有指令使用它。** —— 原因:设计 §3.4。 —— 判错的代价:无。

## Global Constraints

每个任务的要求都隐含包含本节。

- **`src/` 下每个源文件以 `from __future__ import annotations` 开头**(模块 docstring 之后的第一条语句也算)。
- **不引入新第三方依赖。** `pyproject.toml` 的 `dependencies` 不动;只往 wheel 包列表里加 `src/inference_bridge`(`tests/test_mt_packaging.py` 强制检查,不加它就失败)。
- **凭证绝不进 git 树。** 配置里只存变量名;凭证值只经环境变量传给子进程,不进 argv、不进日志、不进异常信息(都有测试);日志目录对 git 不可见。
- **读写产物走 `GitStore` + `Envelope`。** `ledger`、`config` 不直接碰文件系统读写产物;桥接日志不是产物,写普通文件,树里只留 `BigRef`。
- **依赖方向:`inference_bridge → controlplane`。** `controlplane` 不导入 `inference_bridge`;`inference_bridge` 不导入 `simulator`、`diagnostic`、`mindtrainer`(有测试)。
- **不改的文件:** `src/controlplane/reconcile.py`、`daemon.py`、`observe.py`、`gitstore.py`、`envelope.py`;`tests/test_config.py`、`tests/test_states.py`、`tests/test_observe.py`;`schemas/`;`cli/` 下的一切,尤其 **不要碰** `cli/package.json`、`cli/package-lock.json`。
- **`git add` 逐个文件**,不用 `git add .` / `-A`。
- **提交信息不加 `Co-Authored-By` 行**(用户明确要求)。
- **测试命令**一律是 `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error ...`,下文简写里出现的命令都是这条的完整形式。
- **测试基线自适应(起点 B 加增量)**:任务 19 的 Step 0 记下 `BASE=$(git rev-parse HEAD)` 并实测全量通过数 B,两个值写进执行记录。各任务增量依次为 +20、+6、+33、+43、+24、+30、+17、+48、+0;各任务后的全量为 **B+20、B+26、B+59、B+102、B+126、B+156、B+173、B+221、B+221**,都不得出现警告。(以 820 为起点为 840、846、879、922、946、976、993、1041、1041,只作参考:最后一个数在一份仓库拷贝里按本计划逐步执行后实测,任务 22 起的中间数由实测增量推算,没有逐任务重跑;别的计划先落地时 B 会更大,只核对增量。)

## File Structure

| 文件 | 动作 | 职责 |
|---|---|---|
| `src/controlplane/paths.py` | 修改(61 → 149 行) | `SUBDIRS` 追加四个目录;18 个新路径函数 |
| `src/controlplane/states.py` | 修改(49 → 53 行) | 非审核类加 `awaiting_confirmation: {pending, failed}` |
| `src/controlplane/ledger.py` | 新建(137 行) | `record_cost`、`record_failure` |
| `src/controlplane/config.py` | 修改(65 → 236 行;任务 22 后 136 行) | `BackendConfig` 加 `command`、`timeout_s`、`env_allowlist`、`workdir`,`token_env` 可为 `None`;`DEFAULT_ENV_ALLOWLIST`;`read_config`、`write_config`、`ConfigTypeError` |
| `src/controlplane/evidence.py` | 新建(37 行) | `diagnostic_grade`、`evidence_acceptable` |
| `src/inference_bridge/__init__.py` | 新建(任务 25 为 21 行,任务 26 改为 27 行) | 公共导出 |
| `src/inference_bridge/base.py` | 新建(62 行) | `BridgeResult`、`BridgeError`、`InferenceBridge`、`record_bridge_cost`、两个桥接共用的 `_check_segment` |
| `src/inference_bridge/scripted.py` | 新建(48 行) | `ScriptedBridge`、`ScriptExhaustedError` |
| `src/inference_bridge/cli.py` | 新建(203 行) | `CliBridge`、`RunOutcome`、`Runner`、`subprocess_runner`、`DEFAULT_COMMANDS` |
| `pyproject.toml` | 修改(1 行) | wheel 包列表末尾追加 `src/inference_bridge`(正则定位,已有则不动) |
| `.gitignore` | 修改(+3 行) | 忽略 `.rismforge/` |
| `tests/test_paths.py` | 修改(4 → 24 个用例) | `SUBDIRS` 精确相等断言同步;新路径;`init` 建目录 |
| `tests/test_awaiting_confirmation.py` | 新建(6 个用例) | 新状态的迁移;`reconcile` 与调和循环不动它 |
| `tests/test_ledger.py` | 新建(33 个用例) | 记账与失败留痕 |
| `tests/test_config_backend_cli.py` | 新建(43 个用例) | `BackendConfig` 扩展 |
| `tests/test_config_envelope.py` | 新建(24 个用例) | 配置信封读写 |
| `tests/test_evidence.py` | 新建(30 个用例) | 证据等级 |
| `tests/test_bridge_scripted.py` | 新建(15 个用例) | 公共类型与 `ScriptedBridge` |
| `tests/test_bridge_packaging.py` | 新建(任务 25 为 2 个用例,任务 26 加到 3 个) | 依赖方向、`.gitignore` |
| `tests/test_bridge_cli.py` | 新建(47 个用例) | `CliBridge`(注入 runner 与真子进程) |
| `docs/superpowers/plans/2026-10-09-bridge-and-foundations.md` | 新建 | 本计划 |

---
### Task 19: 路径函数与 SUBDIRS

**Files:**
- Modify: `src/controlplane/paths.py`(61 → 149 行)
- Modify: `tests/test_paths.py`(4 → 24 个用例;`test_subdirs_match_spec` 的精确相等断言同步改)

**Interfaces:**
- Consumes: `CONTROL_PLANE_ROOT`、`run_dir`(现有);`GitStore.init`(按 `SUBDIRS` 建目录);`observe.cost`(只在测试里用,证明 `run_trace_file` 就是它读的位置)。
- Produces(后续全部计划依赖,名字逐字照设计 §11):`recipe_candidate_file`、`recipe_dir`、`recipe_file`、`recipe_lineage_file`、`recipe_track_file`、`proposal_file`、`skill_file`、`memory_file`、`library_state_file`、`agent_round_file`、`planner_decision_file`、`nomination_file`、`recipe_track_note_file`、`run_trace_file`、`failure_record_file`、`scheduler_config_file`、`training_defaults_file`、`planner_prompt_file`;`SUBDIRS` 末尾依次为 `recipe_candidates`、`skill_library`、`memory_library`、`agent_rounds`。

**设计要点:**
- **只追加,不重排。** `SUBDIRS` 前九项原样,四个新目录接在末尾。`test_diag_paths.py`、`test_sim_paths.py` 只断言成员,不受影响;`test_subdirs_match_spec` 断言整表相等,必须同步改。
- **`prompts` 不进 `SUBDIRS`。** `planner_prompt_file` 指向 `config/prompts/planner-<version>.yaml`,`GitStore.write_product` 会 `mkdir(parents=True)`。有测试证明 `init` 不建 `config/prompts`。
- **`run_trace_file` 与 `observe.cost` 读的是同一个文件**(`observe.py` 里写死的 `f"{run_dir(run_id)}/run_trace.yaml"`)。本计划不改 `observe.py`,用一条测试把两者钉在一起。
- **两种"战绩"文件别混:** `recipe_track_file(run_id)` 是 `recipes/recipe-<run>/track_record.yaml`(RecipeStore 维护的战绩记录);`recipe_track_note_file(run_id)` 是 `runs/<run>/recipe_track.yaml`(`record_track` 动作的幂等标记)。
- 形参语义见头部 Ruling:`run_id`、`model_id` 由函数加前缀;`proposal_id`、`skill_id`、`mem_id` 传带前缀的产物 id。

- [ ] **Step 0: 记下起点**

```bash
BASE=$(git rev-parse HEAD); echo "BASE=$BASE"
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error | tail -1
```

Expected:第一行打印起点提交;第二行是 `B passed`,无警告。把 `BASE` 与 B 写进执行记录,后续每个任务的全量预期都按 B 加增量算(在 `main` 的 `0385004` 上 B 为 820)。`BASE` 在任务 27 里还要用;换了 shell 会话就从执行记录里取回来再 `export BASE=...`。

- [ ] **Step 1: 写失败测试**

在仓库根目录运行下面的脚本改 `tests/test_paths.py`:文件头加 `import pytest`,`SUBDIRS` 断言改成新整表,文件末尾追加三个用例。

```bash
python3 - <<'PYEOF'
from pathlib import Path

t = Path("tests/test_paths.py")
s = t.read_text(encoding="utf-8")
old = '''    assert paths.SUBDIRS == ["runs", "recipes", "skill_memory_proposals", "models", "compute", "config", "scenarios", "personas", "diagnostics"]'''
new = '''    assert paths.SUBDIRS == [
        "runs", "recipes", "skill_memory_proposals", "models", "compute", "config", "scenarios",
        "personas", "diagnostics",
        "recipe_candidates", "skill_library", "memory_library", "agent_rounds",
    ]'''
assert s.count(old) == 1, s.count(old)
s = "import pytest\n\n" + s.replace(old, new)
s += '''

@pytest.mark.parametrize(
    ("fn", "args", "expected"),
    [
        ("recipe_candidate_file", ("run-003",), "control-plane/recipe_candidates/recipe-run-003.yaml"),
        ("recipe_dir", ("run-003",), "control-plane/recipes/recipe-run-003"),
        ("recipe_file", ("run-003",), "control-plane/recipes/recipe-run-003/recipe.yaml"),
        ("recipe_lineage_file", ("run-003",), "control-plane/recipes/recipe-run-003/lineage.yaml"),
        ("recipe_track_file", ("run-003",), "control-plane/recipes/recipe-run-003/track_record.yaml"),
        (
            "proposal_file", ("proposal-agent-round-zengen-v3-1",),
            "control-plane/skill_memory_proposals/proposal-agent-round-zengen-v3-1/proposal.yaml",
        ),
        ("skill_file", ("skill-ack-emotion",), "control-plane/skill_library/skill-ack-emotion.yaml"),
        ("memory_file", ("mem-0001",), "control-plane/memory_library/mem-0001.yaml"),
        ("library_state_file", (), "control-plane/library_state.yaml"),
        ("agent_round_file", ("zengen-v3",), "control-plane/agent_rounds/agent-round-zengen-v3.yaml"),
        ("planner_decision_file", ("run-003",), "control-plane/runs/run-003/planner_decision.yaml"),
        ("nomination_file", ("run-003",), "control-plane/runs/run-003/nomination.yaml"),
        ("recipe_track_note_file", ("run-003",), "control-plane/runs/run-003/recipe_track.yaml"),
        ("run_trace_file", ("run-003",), "control-plane/runs/run-003/run_trace.yaml"),
        ("failure_record_file", ("run-003", "plan-1"), "control-plane/runs/run-003/failure-plan-1.yaml"),
        ("scheduler_config_file", (), "control-plane/config/scheduler.yaml"),
        ("training_defaults_file", (), "control-plane/config/training_defaults.yaml"),
        ("planner_prompt_file", ("v1",), "control-plane/config/prompts/planner-v1.yaml"),
    ],
)
def test_closed_loop_paths(fn, args, expected):
    assert getattr(paths, fn)(*args) == expected


def test_run_trace_file_is_where_observe_cost_reads(tmp_path):
    from controlplane.envelope import Envelope, OwnerRef
    from controlplane.gitstore import GitStore
    from controlplane.observe import cost

    store = GitStore(str(tmp_path))
    store.init()
    env = Envelope(
        id="trace-run-1", type="run_trace", version=1, created_at="2026-10-09T00:00:00Z",
        created_by=OwnerRef(), status="executed", schema_version=1, links=["run-1"],
        payload={"cost_records": [{"task_id": "t1", "cost_type": "gpu", "gpu_hours": 2.0}]},
    )
    store.write_product(paths.run_trace_file("run-1"), env, "seed")
    assert "total: gpu_hours=2.0 tokens=0" in cost(store, "run-1")


def test_init_creates_the_new_subdirs_and_not_prompts(tmp_path):
    from controlplane.gitstore import GitStore

    GitStore(str(tmp_path)).init()
    root = tmp_path / paths.CONTROL_PLANE_ROOT
    for sub in ("recipe_candidates", "skill_library", "memory_library", "agent_rounds"):
        assert (root / sub).is_dir()
    assert not (root / "config" / "prompts").exists()
'''
t.write_text(s, encoding="utf-8")
PYEOF
```

- [ ] **Step 2: 运行,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_paths.py`
Expected: `21 failed, 3 passed`(18 个参数化路径用例报 `AttributeError: module 'controlplane.paths' has no attribute ...`;`SUBDIRS` 整表、`run_trace_file`、`init` 建目录三条也失败;原有三条路径用例通过)。

- [ ] **Step 3: 写实现**

在仓库根目录运行:

```bash
python3 - <<'PYEOF'
from pathlib import Path

p = Path("src/controlplane/paths.py")
s = p.read_text(encoding="utf-8")
old = 'SUBDIRS = ["runs", "recipes", "skill_memory_proposals", "models", "compute", "config", "scenarios", "personas", "diagnostics"]'
new = '''SUBDIRS = [
    "runs", "recipes", "skill_memory_proposals", "models", "compute", "config", "scenarios",
    "personas", "diagnostics",
    # 闭环集成设计 §4.1:追加在末尾,顺序固定
    "recipe_candidates", "skill_library", "memory_library", "agent_rounds",
]'''
assert s.count(old) == 1, s.count(old)
s = s.replace(old, new)
s += '''

# ---- 闭环集成设计 §4.1 / §11 的产物路径。名字是各计划之间的契约,不得改名。----
# 约定:形参叫 run_id / model_id 的函数自己加前缀(recipe-<run>、agent-round-<model>);
# 形参叫 proposal_id / skill_id / mem_id 的,传入的就是产物 id 本身(已带 proposal- / skill- / mem- 前缀),
# 文件名等于产物 id。


def recipe_candidate_file(run_id: str) -> str:
    return f"{CONTROL_PLANE_ROOT}/recipe_candidates/recipe-{run_id}.yaml"


def recipe_dir(run_id: str) -> str:
    return f"{CONTROL_PLANE_ROOT}/recipes/recipe-{run_id}"


def recipe_file(run_id: str) -> str:
    return f"{recipe_dir(run_id)}/recipe.yaml"


def recipe_lineage_file(run_id: str) -> str:
    return f"{recipe_dir(run_id)}/lineage.yaml"


def recipe_track_file(run_id: str) -> str:
    """配方的战绩记录(RecipeStore 维护)。run 目录里的战绩标记见 recipe_track_note_file。"""
    return f"{recipe_dir(run_id)}/track_record.yaml"


def proposal_file(proposal_id: str) -> str:
    return f"{CONTROL_PLANE_ROOT}/skill_memory_proposals/{proposal_id}/proposal.yaml"


def skill_file(skill_id: str) -> str:
    return f"{CONTROL_PLANE_ROOT}/skill_library/{skill_id}.yaml"


def memory_file(mem_id: str) -> str:
    return f"{CONTROL_PLANE_ROOT}/memory_library/{mem_id}.yaml"


def library_state_file() -> str:
    return f"{CONTROL_PLANE_ROOT}/library_state.yaml"


def agent_round_file(model_id: str) -> str:
    return f"{CONTROL_PLANE_ROOT}/agent_rounds/agent-round-{model_id}.yaml"


def planner_decision_file(run_id: str) -> str:
    return f"{run_dir(run_id)}/planner_decision.yaml"


def nomination_file(run_id: str) -> str:
    return f"{run_dir(run_id)}/nomination.yaml"


def recipe_track_note_file(run_id: str) -> str:
    """run 目录里的战绩回写标记(record_track 的幂等键)。"""
    return f"{run_dir(run_id)}/recipe_track.yaml"


def run_trace_file(run_id: str) -> str:
    """与 controlplane.observe.cost 读取的路径一致。"""
    return f"{run_dir(run_id)}/run_trace.yaml"


def failure_record_file(run_id: str, task_id: str) -> str:
    return f"{run_dir(run_id)}/failure-{task_id}.yaml"


def scheduler_config_file() -> str:
    return f"{CONTROL_PLANE_ROOT}/config/scheduler.yaml"


def training_defaults_file() -> str:
    return f"{CONTROL_PLANE_ROOT}/config/training_defaults.yaml"


def planner_prompt_file(version: str) -> str:
    """planner 提示模板信封(type: prompt_template)。prompts/ 不在 SUBDIRS 里:
    GitStore.write_product 会自己建父目录。"""
    return f"{CONTROL_PLANE_ROOT}/config/prompts/planner-{version}.yaml"
'''
p.write_text(s, encoding="utf-8")
PYEOF
```

- [ ] **Step 4: 运行,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_paths.py tests/test_diag_paths.py tests/test_sim_paths.py tests/test_gitstore.py`
Expected: `38 passed`。

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: `B+20 passed`(以 820 为起点为 840),无警告。

- [ ] **Step 5: 提交**

```bash
git add src/controlplane/paths.py
git add tests/test_paths.py
git commit -m "feat: add the closed-loop product paths and the four new control-plane subdirs"
```

---

### Task 20: 非审核类的 awaiting_confirmation

**Files:**
- Modify: `src/controlplane/states.py`(49 → 53 行;+4 行)
- Test: `tests/test_awaiting_confirmation.py`(新建,6 个用例)

**Interfaces:**
- Consumes: `controlplane.reconcile.reconcile`、`TreeState`、`DirectiveView`;`controlplane.daemon.reconcile_once`(都只在测试里用,不改)。
- Produces(计划 5 写 `awaiting_confirmation` 的指令,计划 8 的命令行 `confirm` 做 `awaiting_confirmation → pending`):`NONREVIEW_TRANSITIONS["awaiting_confirmation"] == {"pending", "failed"}`。

**设计要点:**
- **只加一行迁移表。** `pending`、`executed`、`consumed`、`failed` 的出口不变;没有任何状态能迁回 `awaiting_confirmation`(有测试)。迁移表按产物种类(非审核类)定义,所以新状态对所有非审核产物都合法;目前只有指令使用它(注释里写明)。
- **`reconcile.py` 不改也成立。** `reconcile` 只对 `status == "pending"` 的指令产生 `dispatch_directive`。测试从纯函数和调和循环两头证明:纯函数返回 `[]`;`reconcile_once` 连跑两轮返回 `[]`,`HEAD` 不动(没有任何提交),也没有写出候选。
- **确认之后照常派发。** 测试按命令行 `confirm` 的做法先 `assert_transition` 再写 `pending`,下一轮被派发并变 `executed`。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_awaiting_confirmation.py`:

```python
import pytest

from controlplane.daemon import reconcile_once
from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import candidate_checkpoint_file, run_dir
from controlplane.reconcile import DirectiveView, TreeState, reconcile
from controlplane.states import InvalidTransitionError, assert_transition, can_transition, next_states


def _put(store, run_id, status):
    env = Envelope(
        id=run_id, type="directive", version=1, created_at="2026-10-09T00:00:00Z",
        created_by=OwnerRef(), status=status, schema_version=1,
        payload={"target_dimension": "tom", "recipe_ref": ""},
    )
    store.write_product(f"{run_dir(run_id)}/directive.yaml", env, "scheduler: write directive")


def _head(store):
    return store._git("rev-parse", "HEAD").strip()


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    return s


def test_awaiting_confirmation_goes_to_pending_or_failed_only():
    assert next_states("nonreview", "awaiting_confirmation") == {"pending", "failed"}
    assert not can_transition("nonreview", "awaiting_confirmation", "executed")
    with pytest.raises(InvalidTransitionError):
        assert_transition("nonreview", "awaiting_confirmation", "consumed")


def test_nothing_moves_into_awaiting_confirmation():
    # 它只是初始状态:调度器直接以它写入指令,任何已有状态都迁不进来
    for frm in ("pending", "executed", "consumed", "failed"):
        assert not can_transition("nonreview", frm, "awaiting_confirmation")


def test_existing_nonreview_transitions_are_unchanged():
    assert next_states("nonreview", "pending") == {"executed", "failed"}
    assert next_states("nonreview", "executed") == {"consumed"}
    assert next_states("nonreview", "failed") == set()


def test_reconcile_produces_no_action_for_an_unconfirmed_directive():
    state = TreeState(
        directives=[
            DirectiveView(id="run-001", status="awaiting_confirmation", target_dimension="tom", recipe_ref="")
        ],
    )
    assert reconcile(state) == []


def test_the_loop_leaves_an_unconfirmed_directive_and_the_tree_alone(store):
    _put(store, "run-001", "awaiting_confirmation")
    before = _head(store)
    assert reconcile_once(store) == []
    assert reconcile_once(store) == []
    assert _head(store) == before  # 没有任何提交
    assert store.read_product(f"{run_dir('run-001')}/directive.yaml").status == "awaiting_confirmation"
    assert not store.exists(candidate_checkpoint_file("run-001"))


def test_once_confirmed_the_directive_is_dispatched(store):
    _put(store, "run-001", "awaiting_confirmation")
    rel = f"{run_dir('run-001')}/directive.yaml"
    env = store.read_product(rel)
    assert_transition("nonreview", env.status, "pending")  # 命令行 confirm 做的就是这一步
    env.status = "pending"
    store.write_product(rel, env, "human: confirm run-001")
    actions = reconcile_once(store)
    assert [a.kind for a in actions] == ["dispatch_directive"]
    assert store.read_product(rel).status == "executed"
```

- [ ] **Step 2: 运行,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_awaiting_confirmation.py`
Expected: `2 failed, 4 passed`。失败的是 `test_awaiting_confirmation_goes_to_pending_or_failed_only`(`next_states` 返回空集)与 `test_once_confirmed_the_directive_is_dispatched`(`InvalidTransitionError: nonreview: awaiting_confirmation -> pending not allowed`)。另外四条在改之前就成立:这正是"不改 `reconcile.py`"的依据。

- [ ] **Step 3: 写实现**

在仓库根目录运行:

```bash
python3 - <<'PYEOF'
from pathlib import Path

p = Path("src/controlplane/states.py")
s = p.read_text(encoding="utf-8")
old = '''# spec §1.3 非审核类：pending → executed → consumed；pending → failed
NONREVIEW_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"executed", "failed"},'''
new = '''# spec §1.3 非审核类：pending → executed → consumed；pending → failed
# 闭环集成设计 §3.4(R3):调度器构造的指令先写 awaiting_confirmation,人确认后变 pending;
# 放弃则 failed。reconcile 不为这个状态产生任何动作。
# 迁移表按产物种类(非审核类)定义,所以它对所有非审核产物都合法;目前只有指令使用它。
NONREVIEW_TRANSITIONS: dict[str, set[str]] = {
    "awaiting_confirmation": {"pending", "failed"},
    "pending": {"executed", "failed"},'''
assert s.count(old) == 1, s.count(old)
p.write_text(s.replace(old, new), encoding="utf-8")
PYEOF
git diff --stat src/controlplane/states.py
```

Expected: `src/controlplane/states.py | 4 ++++`。

- [ ] **Step 4: 运行,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_awaiting_confirmation.py tests/test_states.py tests/test_reconcile.py`
Expected: `26 passed`。

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: `B+26 passed`(以 820 为起点为 846),无警告。

- [ ] **Step 5: 提交**

```bash
git add src/controlplane/states.py
git add tests/test_awaiting_confirmation.py
git commit -m "feat: add the awaiting_confirmation directive state (pending or failed only, never dispatched)"
```

---

### Task 21: 统一的成本与失败写入 `controlplane.ledger`

**Files:**
- Create: `src/controlplane/ledger.py`(137 行)
- Test: `tests/test_ledger.py`(新建,33 个用例)

**Interfaces:**
- Consumes: `run_trace_file`、`failure_record_file`(任务 19);`Envelope`、`OwnerRef`、`BigRef`;`GitStore.exists` / `read_product` / `write_product`;`observe.cost`、`observe.sum_costs`、`observe.trace`(只在测试里用,不改)。
- Produces(桥接、TrainScheduler、RecipeStore、MindAgent 的处理器都经它写):
  - `record_cost(store, run_id, *, task_id, cost_type, gpu_hours=None, tokens=None, calls=None, usd=None) -> None`
  - `record_failure(store, run_id, *, task_id, category, point, retries, reason, log_ref=None) -> str`(返回 `failure_record_file(run_id, task_id)`)
  - `FAILURE_CATEGORIES = ("T1", "T2", "T3")`
  - **契约**:同一 `(run_id, task_id, cost_type)` 的成本只记第一次,之后的调用静默去重;所以每次外部调用(含解析重试、重新采样)必须用**不同的** `task_id`(如 `plan-1-a1`、`plan-1-a2`),否则后几次的成本丢失。`record_failure` 已存在时原样返回路径,不报告"已存在",要区分的调用方先 `store.exists(failure_record_file(run_id, task_id))`。

**设计要点:**
- **值未知就省略键。** `observe.sum_costs` 用 `r.get("tokens", 0)` 求和,写 `null` 会让 `sum(...)` 抛 `TypeError`。测试直接把写出的记录喂给 `sum_costs`。
- **`observe.cost` 能正确打印与汇总。** `cost` 用 `r.get('gpu_hours', 0.0)`、`r.get('tokens', 0)` 打印,所以省略的键打印成 `0.0` / `0`。测试逐行断言三条记录的打印格式与汇总行 `total: gpu_hours=1.5 tokens=3200`。
- **幂等。** 同一 `(task_id, cost_type)` 第二次调用直接返回,`HEAD` 不动;同一 `task_id` 换 `cost_type` 是另一条记录。已有 `run_trace`(如现有测试 fixture 里状态为 `effective`、带 `wall_clock_s`)只追加 `cost_records`,其余原样保留。
- **入参校验在写之前。** 见头部 Ruling;`run_id` 与 `task_id` 共用一个路径段校验 `_check_segment`(非空、不含 `/` 与 `\`、不是 `.` 或 `..`)。校验失败时什么都不写:参数化测试对空串、`../x`、`a/b`、`.`、`..` 断言提交数不变、`control-plane/` 下的目录树不变。数值还拒绝 NaN 与无穷。
- **`FailureRecord` 的载荷字段**:`run_id、task_id、category、point、retries、reason、log_refs、recorded_at`(上游补充 E.5)。`log_refs` 是列表,`observe.trace` 据此打印 `log_ref [unreachable] ...`(有测试)。写一次,不覆盖。
- **时间戳**用 `datetime.now(timezone.utc)`,格式 `YYYY-MM-DDTHH:MM:SSZ`,与仓库其余产物的 `created_at` 格式一致。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_ledger.py`:

```python
import pytest

from controlplane.envelope import BigRef, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.ledger import record_cost, record_failure
from controlplane.observe import cost, sum_costs, trace
from controlplane.paths import failure_record_file, run_dir, run_trace_file


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    return s


def _records(store, run_id):
    return store.read_product(run_trace_file(run_id)).payload["cost_records"]


def _head(store):
    return store._git("rev-parse", "HEAD").strip()


def _commits(store):
    # 新建的 store 还没有提交,rev-parse HEAD 会失败;用提交数比较"没有新提交"
    return store._git("rev-list", "--all", "--count").strip()


def test_the_first_cost_creates_the_run_trace(store):
    record_cost(store, "run-1", task_id="plan-1", cost_type="external_inference", tokens=1200, calls=1)
    env = store.read_product(run_trace_file("run-1"))
    assert (env.id, env.type, env.status, env.links) == ("trace-run-1", "run_trace", "executed", ["run-1"])
    [rec] = env.payload["cost_records"]
    assert {k: rec[k] for k in ("run_id", "task_id", "cost_type", "tokens", "calls")} == {
        "run_id": "run-1", "task_id": "plan-1", "cost_type": "external_inference",
        "tokens": 1200, "calls": 1,
    }
    assert isinstance(rec["recorded_at"], str) and rec["recorded_at"].endswith("Z")


def test_unknown_values_are_omitted_not_null(store):
    record_cost(store, "run-1", task_id="plan-1", cost_type="external_inference", calls=1)
    [rec] = _records(store, "run-1")
    for key in ("gpu_hours", "tokens", "usd"):
        assert key not in rec
    assert sum_costs([rec]) == {"gpu_hours": 0, "tokens": 0}  # 写 null 会在这里抛 TypeError


def test_records_append_in_order(store):
    record_cost(store, "run-1", task_id="train", cost_type="gpu", gpu_hours=1.5)
    record_cost(store, "run-1", task_id="plan-1", cost_type="external_inference", tokens=3200)
    assert [(r["task_id"], r["cost_type"]) for r in _records(store, "run-1")] == [
        ("train", "gpu"), ("plan-1", "external_inference"),
    ]


def test_same_task_and_cost_type_is_recorded_once(store):
    record_cost(store, "run-1", task_id="plan-1", cost_type="external_inference", tokens=10)
    head = _head(store)
    record_cost(store, "run-1", task_id="plan-1", cost_type="external_inference", tokens=99)
    assert [r["tokens"] for r in _records(store, "run-1")] == [10]  # 先到者为准
    assert _head(store) == head  # 没有新提交


def test_same_task_with_another_cost_type_is_a_separate_record(store):
    record_cost(store, "run-1", task_id="t", cost_type="gpu", gpu_hours=1.0)
    record_cost(store, "run-1", task_id="t", cost_type="external_inference", tokens=5)
    assert len(_records(store, "run-1")) == 2


def test_other_run_trace_fields_are_preserved(store):
    seeded = Envelope(
        id="trace-run-1", type="run_trace", version=1, created_at="2026-09-26T00:00:00Z",
        created_by=OwnerRef(), status="effective", schema_version=1, links=["run-1"],
        payload={"wall_clock_s": 120, "log_refs": [], "cost_records": [
            {"run_id": "run-1", "task_id": "task-1", "cost_type": "gpu", "gpu_hours": 1.5, "tokens": 0},
        ]},
    )
    store.write_product(run_trace_file("run-1"), seeded, "seed")
    record_cost(store, "run-1", task_id="plan-1", cost_type="external_inference", tokens=3200)
    env = store.read_product(run_trace_file("run-1"))
    assert env.status == "effective" and env.payload["wall_clock_s"] == 120
    assert [r["task_id"] for r in env.payload["cost_records"]] == ["task-1", "plan-1"]


def test_observe_cost_prints_and_sums_the_written_records(store):
    record_cost(store, "run-1", task_id="train", cost_type="gpu", gpu_hours=1.5)
    record_cost(store, "run-1", task_id="plan-1", cost_type="external_inference", tokens=3200)
    record_cost(store, "run-1", task_id="plan-2", cost_type="external_inference", calls=2)
    out = cost(store, "run-1")
    assert "train      gpu                  gpu_hours=1.5 tokens=0" in out
    assert "plan-1     external_inference   gpu_hours=0.0 tokens=3200" in out
    assert "plan-2     external_inference   gpu_hours=0.0 tokens=0" in out
    assert out.endswith("total: gpu_hours=1.5 tokens=3200")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"task_id": "", "cost_type": "gpu"},
        {"task_id": "a/b", "cost_type": "gpu"},
        {"task_id": "t", "cost_type": ""},
        {"task_id": "t", "cost_type": "gpu", "gpu_hours": -1.0},
        {"task_id": "t", "cost_type": "gpu", "tokens": True},
        {"task_id": "t", "cost_type": "gpu", "calls": 1.5},
        {"task_id": "t", "cost_type": "gpu", "gpu_hours": float("nan")},
        {"task_id": "t", "cost_type": "gpu", "usd": float("inf")},
    ],
)
def test_bad_cost_input_is_rejected_before_anything_is_written(store, kwargs):
    with pytest.raises(ValueError):
        record_cost(store, "run-1", **kwargs)
    assert not store.exists(run_trace_file("run-1"))


def test_record_failure_writes_the_product_and_returns_its_path(store):
    ref = BigRef(uri="file:///nowhere/plan-1.log", sha256="b" * 64, size_bytes=7)
    path = record_failure(
        store, "run-1", task_id="plan-1", category="T3", point="trainscheduler.plan",
        retries=2, reason="planner output unparsable after 3 attempts", log_ref=ref,
    )
    assert path == failure_record_file("run-1", "plan-1")
    env = store.read_product(path)
    assert (env.id, env.type, env.status, env.links) == (
        "failure-run-1-plan-1", "failure_record", "executed", ["run-1"],
    )
    p = dict(env.payload)
    assert isinstance(p.pop("recorded_at"), str)
    assert p == {
        "run_id": "run-1", "task_id": "plan-1", "category": "T3",
        "point": "trainscheduler.plan", "retries": 2,
        "reason": "planner output unparsable after 3 attempts",
        "log_refs": [ref.to_dict()],
    }


def test_record_failure_without_a_log_has_an_empty_log_ref_list(store):
    path = record_failure(store, "run-1", task_id="t", category="T2", point="p", retries=0, reason="r")
    assert store.read_product(path).payload["log_refs"] == []


def test_trace_shows_the_failure_and_its_log_reachability(store):
    ref = BigRef(uri="file:///nowhere/plan-1.log", sha256="b" * 64, size_bytes=7)
    record_failure(store, "run-1", task_id="plan-1", category="T3", point="p", retries=1, reason="r", log_ref=ref)
    out = trace(store, "run-1")
    assert "failure_record" in out
    assert "log_ref [unreachable] file:///nowhere/plan-1.log" in out


def test_record_failure_is_write_once(store):
    first = record_failure(store, "run-1", task_id="t", category="T2", point="p", retries=0, reason="first")
    head = _head(store)
    second = record_failure(store, "run-1", task_id="t", category="T3", point="p", retries=5, reason="second")
    assert first == second
    assert store.read_product(first).payload["reason"] == "first"
    assert _head(store) == head


@pytest.mark.parametrize(
    "kwargs",
    [
        {"category": "T4", "retries": 0, "task_id": "t"},
        {"category": "T1", "retries": -1, "task_id": "t"},
        {"category": "T1", "retries": 0, "task_id": "x/y"},
        {"category": "T1", "retries": 0, "task_id": ".."},
    ],
)
def test_bad_failure_input_is_rejected(store, kwargs):
    before = _commits(store)
    with pytest.raises(ValueError):
        record_failure(store, "run-1", point="p", reason="r", **kwargs)
    assert _commits(store) == before
    assert not (store.repo_root / run_dir("run-1")).exists()


_BAD_RUN_IDS = ["", "../x", "a/b", ".", ".."]


def _runs_tree(store):
    return sorted(str(p.relative_to(store.repo_root)) for p in (store.repo_root / "control-plane").rglob("*"))


@pytest.mark.parametrize("run_id", _BAD_RUN_IDS)
def test_record_cost_rejects_a_run_id_that_is_not_one_path_segment(store, run_id):
    commits, before = _commits(store), _runs_tree(store)
    with pytest.raises(ValueError, match="run_id"):
        record_cost(store, run_id, task_id="t", cost_type="gpu", gpu_hours=1.0)
    assert _commits(store) == commits and _runs_tree(store) == before


@pytest.mark.parametrize("run_id", _BAD_RUN_IDS)
def test_record_failure_rejects_a_run_id_that_is_not_one_path_segment(store, run_id):
    commits, before = _commits(store), _runs_tree(store)
    with pytest.raises(ValueError, match="run_id"):
        record_failure(store, run_id, task_id="t", category="T2", point="p", retries=0, reason="r")
    assert _commits(store) == commits and _runs_tree(store) == before
```

- [ ] **Step 2: 运行,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_ledger.py`
Expected: 收集阶段报错 `ModuleNotFoundError: No module named 'controlplane.ledger'`。

- [ ] **Step 3: 写实现**

新建 `src/controlplane/ledger.py`:

```python
"""统一的成本记账与失败留痕(闭环集成设计 §5.2,上游补充 D.4 / E.5)。

所有子系统经这两个函数写 run 目录里的 run_trace 与 failure_record,不各自拼产物。
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from controlplane.envelope import BigRef, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import failure_record_file, run_trace_file

FAILURE_CATEGORIES = ("T1", "T2", "T3")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_segment(name: str, value: str) -> None:
    # run_id 拼进目录名(runs/<run>/),task_id 拼进文件名(failure-<task>.yaml):
    # 必须是单个非空路径段,不含 / 与 \,也不是 . 或 ..,否则会写到 runs/ 之外或错的位置
    if (
        not isinstance(value, str)
        or not value
        or "/" in value
        or "\\" in value
        or value in (".", "..")
    ):
        raise ValueError(f"{name} must be a single non-empty path segment, got {value!r}")


def _check_number(name: str, value: object, *, integer: bool) -> None:
    if value is None:
        return
    # bool 是 int 的子类,单独排除:tokens=True 一定是调用方写错了
    if isinstance(value, bool) or not isinstance(value, int if integer else (int, float)):
        kind = "an int" if integer else "a number"
        raise ValueError(f"{name} must be {kind} or None, got {value!r}")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite number >= 0, got {value!r}")


def record_cost(
    store: GitStore,
    run_id: str,
    *,
    task_id: str,
    cost_type: str,
    gpu_hours: float | None = None,
    tokens: int | None = None,
    calls: int | None = None,
    usd: float | None = None,
) -> None:
    """向 runs/<run>/run_trace.yaml 的 cost_records 追加一条 CostRecord。

    - 值未知(None)就省略该键,不写 null:observe.sum_costs 用 r.get("tokens", 0) 求和,null 会抛 TypeError。
    - 同一 (task_id, cost_type) 只记一次,先到者为准;重复调用不改文件、不产生提交。
    - run_trace 已存在时只改 cost_records,其余字段(wall_clock_s、log_refs、状态)原样保留。
    """
    _check_segment("run_id", run_id)
    _check_segment("task_id", task_id)
    if not isinstance(cost_type, str) or not cost_type:
        raise ValueError(f"cost_type must be a non-empty string, got {cost_type!r}")
    _check_number("gpu_hours", gpu_hours, integer=False)
    _check_number("tokens", tokens, integer=True)
    _check_number("calls", calls, integer=True)
    _check_number("usd", usd, integer=False)

    rel = run_trace_file(run_id)
    now = _now()
    if store.exists(rel):
        env = store.read_product(rel)
    else:
        env = Envelope(
            id=f"trace-{run_id}", type="run_trace", version=1, created_at=now,
            created_by=OwnerRef(), status="executed", schema_version=1,
            links=[run_id], payload={},
        )
    records = list(env.payload.get("cost_records", []))
    if any(r.get("task_id") == task_id and r.get("cost_type") == cost_type for r in records):
        return
    record: dict = {"run_id": run_id, "task_id": task_id, "cost_type": cost_type}
    for key, value in (("gpu_hours", gpu_hours), ("tokens", tokens), ("calls", calls), ("usd", usd)):
        if value is not None:
            record[key] = value
    record["recorded_at"] = now
    records.append(record)
    env.payload["cost_records"] = records
    store.write_product(rel, env, f"ledger: cost {run_id}/{task_id} ({cost_type})")


def record_failure(
    store: GitStore,
    run_id: str,
    *,
    task_id: str,
    category: str,
    point: str,
    retries: int,
    reason: str,
    log_ref: BigRef | None = None,
) -> str:
    """写一份 FailureRecord 产物(上游补充 E.5)到 runs/<run>/failure-<task>.yaml,返回路径。

    载荷:run_id、task_id、category(T1/T2/T3)、point(失败点:子系统.模块)、retries(已重试次数)、
    reason(最终判定)、log_refs(原始错误的 L3 引用,沿用 observe.trace 认识的键,无日志时为空列表)、
    recorded_at(时刻)。同一 (run_id, task_id) 只写一次:已存在则原样返回路径,不覆盖,
    也不报告"已存在"——调用方要区分,先自己 store.exists(failure_record_file(run_id, task_id))。
    """
    _check_segment("run_id", run_id)
    _check_segment("task_id", task_id)
    if category not in FAILURE_CATEGORIES:
        raise ValueError(f"category must be one of {FAILURE_CATEGORIES}, got {category!r}")
    if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
        raise ValueError(f"retries must be an int >= 0, got {retries!r}")
    rel = failure_record_file(run_id, task_id)
    if store.exists(rel):
        return rel
    now = _now()
    env = Envelope(
        id=f"failure-{run_id}-{task_id}", type="failure_record", version=1, created_at=now,
        created_by=OwnerRef(), status="executed", schema_version=1, links=[run_id],
        payload={
            "run_id": run_id,
            "task_id": task_id,
            "category": category,
            "point": point,
            "retries": retries,
            "reason": reason,
            "log_refs": [log_ref.to_dict()] if log_ref is not None else [],
            "recorded_at": now,
        },
    )
    store.write_product(rel, env, f"ledger: failure {run_id}/{task_id} ({category})")
    return rel
```

- [ ] **Step 4: 运行,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_ledger.py tests/test_observe.py`
Expected: `38 passed`。

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: `B+59 passed`(以 820 为起点为 879),无警告。

- [ ] **Step 5: 提交**

```bash
git add src/controlplane/ledger.py
git add tests/test_ledger.py
git commit -m "feat: add the ledger (idempotent cost records and write-once failure records in the run directory)"
```

---

### Task 22: BackendConfig 的命令行扩展

**Files:**
- Modify: `src/controlplane/config.py`(65 → 136 行)
- Test: `tests/test_config_backend_cli.py`(新建,43 个用例);`tests/test_config.py` **一行不改**

**Interfaces:**
- Consumes: 现有 `BackendConfig`、`load_backend_config`、`resolve_credential`、`_FORBIDDEN_SECRET_KEYS`。
- Produces(任务 26 的 `CliBridge` 依赖):
  - `BackendConfig.token_env: str | None`、`BackendConfig.command: list[str] | None = None`、`BackendConfig.timeout_s: float = 120.0`
  - `BackendConfig.env_allowlist: list[str] | None = None`、`BackendConfig.workdir: str = "empty_tmp"`(任务 26 的 `CliBridge` 据此限定子进程的环境变量与工作目录)
  - `DEFAULT_ENV_ALLOWLIST`:模块级元组,`env_allowlist` 为 `None` 时的默认白名单
  - `resolve_credential(cfg, env) -> str | None`:`token_env` 为 `None` 时返回 `None`,不读环境;否则行为与现在相同(缺失或为空抛 `MissingCredentialError`,信息里只有变量名)。

**设计要点:**
- **新字段放在末尾且带默认值。** 现有用关键字构造 `BackendConfig` 的地方(仓库里只有 `load_backend_config` 一处)不受影响;`tests/test_config.py` 的 8 个用例原样通过。
- **`command` 是 argv 列表,不经 shell。** `"claude -p"` 这种字符串被拒(防止有人以为会按空格切分);载入时拷贝一份,调用方之后改原列表不影响配置。
- **凭证检查不放宽,反而收紧大小写。** 带着新字段、`token_env: null` 时,六个凭证值字段名仍然一个都不许出现;字段名按 `k.lower()` 比较,`Token`、`API_KEY` 也被拒(参数化测试)。比较逻辑收进 `_is_secret_key`,任务 23 的递归检查复用它。配置对象的字段集合被钉死,没有存放凭证值的位置。
- **子进程隔离的两个字段只存名字与模式,不存值。** `env_allowlist` 是环境变量名列表:`None` 用模块常量 `DEFAULT_ENV_ALLOWLIST`,给了就必须是元素为非空字符串的 `list`(空 `list` 合法,含义是只传 `token_env`),载入时拷贝一份;`workdir` 目前只接受 `"empty_tmp"`(每次调用一个空临时目录,由任务 26 实现),缺省即它。校验函数 `_load_env_allowlist`、`_load_workdir` 照 `_load_command`、`_load_timeout` 的写法。凭证字段检查不放宽,`BackendConfig` 仍没有存放凭证值的字段(字段集合的测试同步钉死)。
- 校验细节见头部 Ruling。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_config_backend_cli.py`:

```python
import pytest

from controlplane.config import (
    BackendConfig,
    CredentialInGitTreeError,
    MissingCredentialError,
    load_backend_config,
    resolve_credential,
)


def _raw(**over) -> dict:
    raw = {
        "backend": "claude_cli",
        "invocation_form": "cli",
        "model_name": "claude-sonnet-4",
        "prompt_template_version": "v1",
        "token_env": "ANTHROPIC_API_KEY",
        "retry_policy": {"max_attempts": 3, "backoff_base_s": 2},
    }
    raw.update(over)
    return raw


def test_old_style_config_gets_no_command_and_the_default_timeout():
    cfg = load_backend_config(_raw())
    assert cfg.command is None
    assert cfg.timeout_s == 120.0


def test_command_and_timeout_are_loaded():
    cfg = load_backend_config(_raw(command=["claude", "-p", "--model", "{model}"], timeout_s=30))
    assert cfg.command == ["claude", "-p", "--model", "{model}"]
    assert cfg.timeout_s == 30.0 and isinstance(cfg.timeout_s, float)


def test_the_loaded_command_is_a_copy_of_the_raw_list():
    raw = _raw(command=["claude", "-p"])
    cfg = load_backend_config(raw)
    raw["command"].append("--evil")
    assert cfg.command == ["claude", "-p"]


@pytest.mark.parametrize(
    "command", [[], "claude -p", ["claude", ""], ["claude", 3], {"argv": ["claude"]}],
)
def test_a_malformed_command_is_rejected(command):
    with pytest.raises(ValueError, match="command"):
        load_backend_config(_raw(command=command))


@pytest.mark.parametrize("timeout_s", [0, -1, "30", True, float("nan"), float("inf")])
def test_a_malformed_timeout_is_rejected(timeout_s):
    with pytest.raises(ValueError, match="timeout_s"):
        load_backend_config(_raw(timeout_s=timeout_s))


def test_token_env_may_be_null_for_tools_with_their_own_login():
    cfg = load_backend_config(_raw(token_env=None))
    assert cfg.token_env is None
    assert resolve_credential(cfg, {}) is None
    assert resolve_credential(cfg, {"ANTHROPIC_API_KEY": "sk-live"}) is None


def test_token_env_key_is_still_required():
    raw = _raw()
    del raw["token_env"]
    with pytest.raises(KeyError):
        load_backend_config(raw)


@pytest.mark.parametrize("token_env", ["", 7])
def test_token_env_must_be_a_name_or_null(token_env):
    with pytest.raises(ValueError, match="token_env"):
        load_backend_config(_raw(token_env=token_env))


def test_a_named_but_missing_variable_still_fails_loudly():
    with pytest.raises(MissingCredentialError, match="ANTHROPIC_API_KEY"):
        resolve_credential(load_backend_config(_raw()), {})


@pytest.mark.parametrize(
    "key", ["token", "api_key", "apikey", "password", "secret", "private_key", "Token", "API_KEY"],
)
def test_credential_value_fields_are_still_rejected_with_the_new_fields_present(key):
    with pytest.raises(CredentialInGitTreeError):
        load_backend_config(_raw(token_env=None, command=["codex", "exec"], **{key: "x"}))


def test_the_config_object_has_no_field_for_a_credential_value():
    names = set(BackendConfig.__dataclass_fields__)
    assert names == {
        "backend", "invocation_form", "model_name", "prompt_template_version",
        "token_env", "retry_policy", "command", "timeout_s", "env_allowlist", "workdir",
    }


def test_old_style_config_gets_the_default_isolation_settings():
    cfg = load_backend_config(_raw())
    assert cfg.env_allowlist is None
    assert cfg.workdir == "empty_tmp"


def test_the_default_env_allowlist_is_the_documented_set():
    from controlplane.config import DEFAULT_ENV_ALLOWLIST  # 放在用例里:它缺失时不拖垮整个文件的收集

    assert DEFAULT_ENV_ALLOWLIST == (
        "PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP",
        "LANG", "LC_ALL", "TERM", "CODEX_HOME", "CLAUDE_CONFIG_DIR",
    )


@pytest.mark.parametrize("allowlist", [None, ["PATH", "HOME"], []], ids=["none", "names", "empty"])
def test_a_valid_env_allowlist_is_loaded(allowlist):
    assert load_backend_config(_raw(env_allowlist=allowlist)).env_allowlist == allowlist


def test_the_loaded_env_allowlist_is_a_copy_of_the_raw_list():
    raw = _raw(env_allowlist=["PATH"])
    cfg = load_backend_config(raw)
    raw["env_allowlist"].append("EVIL")
    assert cfg.env_allowlist == ["PATH"]


@pytest.mark.parametrize(
    "allowlist", ["PATH", ["PATH", 3], ["PATH", ""], ("PATH", "HOME")],
    ids=["string", "non-string-item", "empty-name", "tuple"],
)
def test_a_malformed_env_allowlist_is_rejected(allowlist):
    with pytest.raises(ValueError, match="env_allowlist"):
        load_backend_config(_raw(env_allowlist=allowlist))


def test_workdir_empty_tmp_is_accepted_explicitly():
    assert load_backend_config(_raw(workdir="empty_tmp")).workdir == "empty_tmp"


@pytest.mark.parametrize("workdir", ["inherit", "/tmp", "", None])
def test_an_unknown_workdir_is_rejected(workdir):
    with pytest.raises(ValueError, match="workdir"):
        load_backend_config(_raw(workdir=workdir))
```

- [ ] **Step 2: 运行,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_config_backend_cli.py`
Expected: `35 failed, 8 passed`(新字段不存在报 `AttributeError`,非法 `command`、`timeout_s`(含 NaN、无穷)、`token_env` 不报错,`Token`、`API_KEY` 没被拒,`token_env=None` 时 `resolve_credential` 查 `env.get(None)` 抛 `MissingCredentialError`;通过的 8 条是缺 `token_env` 键、缺凭证、小写凭证字段被拒这些现有行为;新增的 `env_allowlist`、`workdir` 用例全部失败:字段不存在,非法值不报错,用例里导入 `DEFAULT_ENV_ALLOWLIST` 报 `ImportError`)。

- [ ] **Step 3: 写实现**

在仓库根目录运行:

```bash
python3 - <<'PYEOF'
from pathlib import Path

p = Path("src/controlplane/config.py")
s = p.read_text(encoding="utf-8")
old1 = '''from dataclasses import dataclass
'''
new1 = '''import math
from dataclasses import dataclass, field
'''
old2 = '''    token_env: str  # 环境变量名，如 "ANTHROPIC_API_KEY"
    retry_policy: dict


def load_backend_config(raw: dict) -> BackendConfig:
    leaked = _FORBIDDEN_SECRET_KEYS & set(raw.keys())
    if leaked:
        raise CredentialInGitTreeError(
            f"credential value fields found in git-tree config: {sorted(leaked)}; "
            "store only the env var name via 'token_env'"
        )
    return BackendConfig(
        backend=raw["backend"],
        invocation_form=raw["invocation_form"],
        model_name=raw["model_name"],
        prompt_template_version=raw["prompt_template_version"],
        token_env=raw["token_env"],
        retry_policy=raw.get("retry_policy", {}),
    )


def resolve_credential(cfg: BackendConfig, env: dict[str, str]) -> str:
    """按变量名从环境解析凭证。缺失/为空即明确失败，禁止静默降级。"""
    value = env.get(cfg.token_env)'''
new2 = '''    # 环境变量名，如 "ANTHROPIC_API_KEY"。显式 None 表示该工具用自带登录态，不解析凭证（设计 R15）。
    token_env: str | None
    retry_policy: dict
    # 命令行形态的参数模板（argv 列表，不经 shell）。None 表示没有命令行形态。
    command: list[str] | None = field(default=None)
    timeout_s: float = 120.0
    # 子进程隔离(任务 26 用):env_allowlist 为 None 时用 DEFAULT_ENV_ALLOWLIST;workdir 目前只有 "empty_tmp"。
    env_allowlist: list[str] | None = None
    workdir: str = "empty_tmp"


def _load_command(raw: dict) -> list[str] | None:
    command = raw.get("command")
    if command is None:
        return None
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(a, str) and a for a in command)
    ):
        raise ValueError(f"command must be a non-empty list of non-empty strings, got {command!r}")
    return list(command)


def _load_timeout(raw: dict) -> float:
    timeout = raw.get("timeout_s", 120.0)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError(f"timeout_s must be a finite positive number, got {timeout!r}")
    return float(timeout)


def _load_env_allowlist(raw: dict) -> list[str] | None:
    allowlist = raw.get("env_allowlist")
    if allowlist is None:
        return None  # 用 DEFAULT_ENV_ALLOWLIST
    if not isinstance(allowlist, list) or not all(isinstance(n, str) and n for n in allowlist):
        raise ValueError(f"env_allowlist must be null or a list of non-empty strings, got {allowlist!r}")
    return list(allowlist)


def _load_workdir(raw: dict) -> str:
    workdir = raw.get("workdir", "empty_tmp")
    if workdir != "empty_tmp":
        raise ValueError(f"workdir must be 'empty_tmp', got {workdir!r}")
    return workdir


def _is_secret_key(key: object) -> bool:
    """凭证值字段名按小写比较:Token、API_KEY 与 token、api_key 一样被拒。"""
    return isinstance(key, str) and key.lower() in _FORBIDDEN_SECRET_KEYS


def load_backend_config(raw: dict) -> BackendConfig:
    leaked = {k for k in raw if _is_secret_key(k)}
    if leaked:
        raise CredentialInGitTreeError(
            f"credential value fields found in git-tree config: {sorted(leaked)}; "
            "store only the env var name via 'token_env'"
        )
    token_env = raw["token_env"]  # 键必须在场；值可以是变量名或显式 null
    if token_env is not None and (not isinstance(token_env, str) or not token_env):
        raise ValueError(f"token_env must be an env var name or null, got {token_env!r}")
    return BackendConfig(
        backend=raw["backend"],
        invocation_form=raw["invocation_form"],
        model_name=raw["model_name"],
        prompt_template_version=raw["prompt_template_version"],
        token_env=token_env,
        retry_policy=raw.get("retry_policy", {}),
        command=_load_command(raw),
        timeout_s=_load_timeout(raw),
        env_allowlist=_load_env_allowlist(raw),
        workdir=_load_workdir(raw),
    )


def resolve_credential(cfg: BackendConfig, env: dict[str, str]) -> str | None:
    """按变量名从环境解析凭证。缺失/为空即明确失败，禁止静默降级。
    token_env 为 None（工具自带登录态）时返回 None，不读环境。"""
    if cfg.token_env is None:
        return None
    value = env.get(cfg.token_env)'''
old3 = '''_REDACTED = "***REDACTED***"
'''
new3 = '''_REDACTED = "***REDACTED***"

# 子进程默认能看到的环境变量名;BackendConfig.env_allowlist 为 None 时用它(设计 BR-8)。
# 凭证变量不在其中,由 token_env 单独加入。
DEFAULT_ENV_ALLOWLIST = (
    "PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP",
    "LANG", "LC_ALL", "TERM", "CODEX_HOME", "CLAUDE_CONFIG_DIR",
)
'''
for o, n in ((old1, new1), (old2, new2), (old3, new3)):
    assert s.count(o) == 1, o[:40]
    s = s.replace(o, n)
p.write_text(s, encoding="utf-8")
PYEOF
```

- [ ] **Step 4: 运行,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_config_backend_cli.py tests/test_config.py`
Expected: `51 passed`。

Run: `git diff --stat tests/test_config.py`
Expected: 无输出(现有测试一行未改)。

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: `B+102 passed`(以 820 为起点为 922),无警告。

- [ ] **Step 5: 提交**

```bash
git add src/controlplane/config.py
git add tests/test_config_backend_cli.py
git commit -m "feat: backend config gets a cli command template, a timeout, and an explicit null token_env for self-logged-in tools"
```

---

### Task 23: 配置信封读写 `read_config` / `write_config`

**Files:**
- Modify: `src/controlplane/config.py`(136 → 236 行)
- Test: `tests/test_config_envelope.py`(新建,24 个用例)

**Interfaces:**
- Consumes: `Envelope`、`OwnerRef`、`GitStore`、`CONTROL_PLANE_ROOT`;`backends_config_file`、`scheduler_config_file`(任务 19);`load_backend_config`(任务 22)。
- Produces(补充契约;计划 5 读写 `scheduler.yaml` / `training_defaults.yaml`,计划 8 读 `backends.yaml`):
  - `read_config(store, rel_path) -> dict`
  - `write_config(store, rel_path, config_id, payload, message) -> None`
  - `ConfigTypeError`、`CONFIG_TYPE = "config"`

**设计要点:**
- **配置是信封**:`type: config`、状态 `effective`、`schema_version: 1`,载荷就是配置本体。读走 `GitStore.read_product`(迁移 → 校验 → 反序列化),写走 `GitStore.write_product`。
- **`config.py` 新导入 `gitstore`、`envelope`、`paths`。** 已核实没有循环:这三个模块都不导入 `config`。
- **版本号**:载荷与 id 都没变就什么都不写(`HEAD` 不动);变了 `version` 加一。
- **深拷贝两头**:写入时拷贝,调用方之后改自己的 dict 不影响已写内容;读出时拷贝,调用方改返回值不影响下次读。
- **路径先规范化。** `posixpath.normpath` 前后必须一致、不得含 `..` 段,再要求以 `control-plane/config/` 开头、以 `.yaml` 结尾:`control-plane/config/../runs/x.yaml`、`./`、`//`、空串都被拒,什么都不写(有测试)。`config_id` 不能为空。
- **凭证检查递归**(字段名按小写比较),写前、读后各一次;异常里只有键路径(如 `payload.planner.nested[0].api_key`),没有值(有测试断言值不在信息里)。
- `backends.yaml` 的载荷形状由计划 8 定(本计划的测试用 `{"planner": <BackendConfig 原始 dict>}` 演示能 `load_backend_config` 回来)。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_config_envelope.py`:

```python
import pytest

from controlplane.config import (
    ConfigTypeError,
    CredentialInGitTreeError,
    load_backend_config,
    read_config,
    write_config,
)
from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import backends_config_file, run_dir, scheduler_config_file


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    return s


def _head(store):
    return store._git("rev-parse", "HEAD").strip()


def _commits(store):
    # 新建的 store 还没有提交,rev-parse HEAD 会失败;用提交数比较"没有新提交"
    return store._git("rev-list", "--all", "--count").strip()


def _backend():
    return {
        "backend": "claude_cli", "invocation_form": "cli", "model_name": "claude-sonnet-4",
        "prompt_template_version": "v1", "token_env": None,
        "retry_policy": {"max_attempts": 3, "backoff_base_s": 2},
        "command": ["claude", "-p", "--model", "{model}"], "timeout_s": 60,
    }


def test_write_then_read_round_trips_the_payload(store):
    payload = {"confirm_directives": True, "perturbable": ["sft_steps"], "min_gain": 0.01}
    write_config(store, scheduler_config_file(), "scheduler", payload, "human: scheduler config")
    assert read_config(store, scheduler_config_file()) == payload
    env = store.read_product(scheduler_config_file())
    assert (env.id, env.type, env.status, env.version) == ("scheduler", "config", "effective", 1)


def test_read_config_returns_a_copy(store):
    write_config(store, scheduler_config_file(), "scheduler", {"perturbable": ["a"]}, "m")
    read_config(store, scheduler_config_file())["perturbable"].append("b")
    assert read_config(store, scheduler_config_file()) == {"perturbable": ["a"]}


def test_write_config_does_not_keep_a_reference_to_the_callers_dict(store):
    payload = {"perturbable": ["a"]}
    write_config(store, scheduler_config_file(), "scheduler", payload, "m")
    payload["perturbable"].append("b")
    assert read_config(store, scheduler_config_file()) == {"perturbable": ["a"]}


def test_rewriting_bumps_the_version_and_identical_rewrite_is_a_no_op(store):
    write_config(store, scheduler_config_file(), "scheduler", {"min_gain": 0.01}, "m1")
    write_config(store, scheduler_config_file(), "scheduler", {"min_gain": 0.02}, "m2")
    assert store.read_product(scheduler_config_file()).version == 2
    head = _head(store)
    write_config(store, scheduler_config_file(), "scheduler", {"min_gain": 0.02}, "m3")
    assert _head(store) == head
    assert store.read_product(scheduler_config_file()).version == 2


def test_backends_config_read_back_loads_as_backend_config(store):
    write_config(store, backends_config_file(), "backends", {"planner": _backend()}, "m")
    cfg = load_backend_config(read_config(store, backends_config_file())["planner"])
    assert cfg.token_env is None and cfg.command == ["claude", "-p", "--model", "{model}"]


def test_reading_a_missing_config_raises_file_not_found(store):
    with pytest.raises(FileNotFoundError):
        read_config(store, scheduler_config_file())


def test_reading_a_product_that_is_not_a_config_is_refused(store):
    env = Envelope(
        id="run-1", type="directive", version=1, created_at="2026-10-09T00:00:00Z",
        created_by=OwnerRef(), status="pending", schema_version=1, payload={},
    )
    store.write_product(scheduler_config_file(), env, "oops")
    with pytest.raises(ConfigTypeError, match="directive"):
        read_config(store, scheduler_config_file())


def test_overwriting_a_non_config_product_is_refused(store):
    env = Envelope(
        id="x", type="prompt_template", version=1, created_at="2026-10-09T00:00:00Z",
        created_by=OwnerRef(), status="effective", schema_version=1, payload={},
    )
    store.write_product(scheduler_config_file(), env, "m")
    with pytest.raises(ConfigTypeError):
        write_config(store, scheduler_config_file(), "scheduler", {}, "m")


@pytest.mark.parametrize(
    "rel_path",
    [
        f"{run_dir('run-1')}/scheduler.yaml", "control-plane/config/scheduler.yml", "elsewhere/config/x.yaml",
        "control-plane/config/../runs/x.yaml", "control-plane/config/./scheduler.yaml",
        "control-plane/config//scheduler.yaml", "",
    ],
)
def test_configs_live_under_the_config_dir(store, rel_path):
    with pytest.raises(ValueError, match="control-plane/config/"):
        write_config(store, rel_path, "x", {}, "m")


@pytest.mark.parametrize("bad", [[], "text", None])
def test_payload_must_be_a_mapping(store, bad):
    with pytest.raises(ValueError, match="payload"):
        write_config(store, scheduler_config_file(), "scheduler", bad, "m")


def test_a_credential_value_at_any_depth_is_refused_before_writing(store):
    bad = {"planner": {**_backend(), "nested": [{"api_key": "sk-ant-secret"}]}}
    with pytest.raises(CredentialInGitTreeError) as exc:
        write_config(store, backends_config_file(), "backends", bad, "m")
    assert "sk-ant-secret" not in str(exc.value)
    assert not store.exists(backends_config_file())


def test_a_leaked_credential_already_in_the_tree_is_refused_on_read(store):
    env = Envelope(
        id="backends", type="config", version=1, created_at="2026-10-09T00:00:00Z",
        created_by=OwnerRef(), status="effective", schema_version=1,
        payload={"planner": {"token": "sk-ant-secret"}},
    )
    store.write_product(backends_config_file(), env, "hand-written")
    with pytest.raises(CredentialInGitTreeError):
        read_config(store, backends_config_file())


def test_a_dot_dot_path_writes_nothing(store):
    before = _commits(store)
    with pytest.raises(ValueError):
        write_config(store, "control-plane/config/../runs/x.yaml", "x", {}, "m")
    assert _commits(store) == before
    assert not (store.repo_root / "control-plane" / "runs" / "x.yaml").exists()


@pytest.mark.parametrize("config_id", ["", None])
def test_config_id_must_not_be_empty(store, config_id):
    with pytest.raises(ValueError, match="config_id"):
        write_config(store, scheduler_config_file(), config_id, {}, "m")
    assert not store.exists(scheduler_config_file())


def test_credential_field_names_are_matched_case_insensitively(store):
    with pytest.raises(CredentialInGitTreeError) as exc:
        write_config(store, backends_config_file(), "backends", {"planner": {"API_KEY": "sk-x"}}, "m")
    assert "API_KEY" in str(exc.value) and "sk-x" not in str(exc.value)

```

- [ ] **Step 2: 运行,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_config_envelope.py`
Expected: 收集阶段报错 `ImportError: cannot import name 'ConfigTypeError' from 'controlplane.config'`。

- [ ] **Step 3: 写实现**

在仓库根目录运行:

```bash
python3 - <<'PYEOF'
from pathlib import Path

p = Path("src/controlplane/config.py")
s = p.read_text(encoding="utf-8")
old = '''import math
from dataclasses import dataclass, field
'''
new = '''import copy
import math
import posixpath
from dataclasses import dataclass, field
from datetime import datetime, timezone

from controlplane.envelope import Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import CONTROL_PLANE_ROOT
'''
assert s.count(old) == 1
s = s.replace(old, new)
old = '''class CredentialInGitTreeError(Exception):
    """git 树配置里出现了凭证值本身，而非变量名引用。"""
'''
new = old + '''

class ConfigTypeError(Exception):
    """配置路径上的产物不是 type: config 的信封。"""
'''
assert s.count(old) == 1
s = s.replace(old, new)
s += '''

# ---- 配置信封（闭环集成设计 §11 补充契约）----
# backends.yaml、scheduler.yaml、training_defaults.yaml 一律是 type: config 的信封，载荷就是配置本体。

CONFIG_TYPE = "config"
_CONFIG_DIR = f"{CONTROL_PLANE_ROOT}/config/"


def _find_secret_keys(obj: object, at: str = "payload") -> list[str]:
    """递归找出凭证值字段的位置（只报键路径，不报值）。"""
    found: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            here = f"{at}.{k}"
            if _is_secret_key(k):
                found.append(here)
            found.extend(_find_secret_keys(v, here))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            found.extend(_find_secret_keys(v, f"{at}[{i}]"))
    return found


def _check_no_secrets(payload: dict, rel_path: str) -> None:
    leaked = _find_secret_keys(payload)
    if leaked:
        raise CredentialInGitTreeError(
            f"credential value fields in {rel_path}: {leaked}; store only env var names via 'token_env'"
        )


def read_config(store: GitStore, rel_path: str) -> dict:
    """读一份配置信封，返回载荷的深拷贝。文件不存在抛 FileNotFoundError；
    不是 config 信封抛 ConfigTypeError；载荷里有凭证值字段抛 CredentialInGitTreeError。"""
    env = store.read_product(rel_path)
    if env.type != CONFIG_TYPE:
        raise ConfigTypeError(f"{rel_path} is a {env.type!r} product, not {CONFIG_TYPE!r}")
    _check_no_secrets(env.payload, rel_path)
    return copy.deepcopy(env.payload)


def write_config(
    store: GitStore, rel_path: str, config_id: str, payload: dict, message: str
) -> None:
    """把配置写成 type: config 的信封（状态 effective）。

    - 路径必须在 control-plane/config/ 下且以 .yaml 结尾:先 posixpath.normpath,
      规范化前后都不得含 .. 段(control-plane/config/../runs/x.yaml 被拒)。
    - config_id 不能为空。
    - 写之前递归检查凭证值字段，命中即拒绝，树里什么都不写。
    - 已存在且载荷相同：不改文件、不产生提交；载荷不同：version 加一。
    - 已存在但不是 config 信封：拒绝覆盖（ConfigTypeError）。
    """
    norm = posixpath.normpath(rel_path) if isinstance(rel_path, str) and rel_path else ""
    if (
        norm != rel_path
        or ".." in rel_path.split("/")
        or not norm.startswith(_CONFIG_DIR)
        or not norm.endswith(".yaml")
    ):
        raise ValueError(
            f"config files live under {_CONFIG_DIR}, end with .yaml and contain no '..', got {rel_path!r}"
        )
    if not isinstance(config_id, str) or not config_id:
        raise ValueError(f"config_id must be a non-empty string, got {config_id!r}")
    if not isinstance(payload, dict):
        raise ValueError(f"config payload must be a mapping, got {type(payload).__name__}")
    _check_no_secrets(payload, rel_path)
    body = copy.deepcopy(payload)
    version = 1
    if store.exists(rel_path):
        old = store.read_product(rel_path)
        if old.type != CONFIG_TYPE:
            raise ConfigTypeError(f"refusing to overwrite a {old.type!r} product at {rel_path}")
        if old.payload == body and old.id == config_id:
            return
        version = old.version + 1
    env = Envelope(
        id=config_id,
        type=CONFIG_TYPE,
        version=version,
        created_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        created_by=OwnerRef(),
        status="effective",
        schema_version=1,
        payload=body,
    )
    store.write_product(rel_path, env, message)
'''
p.write_text(s, encoding="utf-8")
PYEOF
```

- [ ] **Step 4: 运行,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_config_envelope.py tests/test_config_backend_cli.py tests/test_config.py`
Expected: `75 passed`。

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: `B+126 passed`(以 820 为起点为 946),无警告。

- [ ] **Step 5: 提交**

```bash
git add src/controlplane/config.py
git add tests/test_config_envelope.py
git commit -m "feat: config files are envelopes (read_config / write_config with a recursive credential-field check)"
```

---

### Task 24: 证据等级 `controlplane.evidence`

**Files:**
- Create: `src/controlplane/evidence.py`(37 行)
- Test: `tests/test_evidence.py`(新建,30 个用例)

**Interfaces:**
- Consumes: `simulator.stubs.diagnostic_evaluator_stub`(只在测试里用)。`evidence.py` 本身不导入任何模块。
- Produces(补充契约;计划 3 的提名、计划 5 的规划处理器、计划 8 的 `reconcile` 扩展都调它,不得另写一份):
  - `diagnostic_grade(payload: dict) -> str`(`"stub" | "mock" | "real"`;非 `dict` 抛 `TypeError`;失败关闭:只有 `backends == "real"` 且 `dimension_scores` 是非空 `list` 才判 `real`,没有 `backends` 标记判 `stub`)
  - `evidence_acceptable(grade: str, accept_mock: bool) -> bool`
  - `GRADES = ("stub", "mock", "real")`

**设计要点:**
- **规则按顺序判,失败关闭(设计 DX-12)**:`payload.get("stub")` 为真 → `stub`;`backends == "mock"` → `mock`;`backends == "real"` 且 `dimension_scores` 是非空列表 → `real`;其余(含没有 `backends` 键)→ `stub`。
- **现有两种报告的等级**:`diagnostic_evaluator_stub` 的载荷带 `stub: True`,判 `stub`(测试直接用它);`diagnostic.orchestrator.run_diagnostic` 今天写的 `DiagnosticReport.to_dict()` 有 `dimension_scores`、没有 `backends`,判 `stub`。计划 8 让 `run_diagnostic`、`write_report`、`resolve_baseline` 的 `backends` 成为必填关键字参数之后,每份新报告都带标记;在那之前不要用 mock 后端写报告。
- **纯函数,不改入参**(有测试)。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_evidence.py`:

```python
import pytest

from controlplane.evidence import GRADES, diagnostic_grade, evidence_acceptable
from simulator.stubs import diagnostic_evaluator_stub

_SCORES = [{"dimension": "tom", "level": "l1", "score": 0.6}]


def test_the_stub_report_is_graded_stub():
    assert diagnostic_grade(diagnostic_evaluator_stub("run-1", "cand-run-1").payload) == "stub"


@pytest.mark.parametrize(
    ("payload", "grade"),
    [
        ({"stub": True, "dimension_scores": _SCORES, "backends": "real"}, "stub"),  # stub 优先
        ({"backends": "mock"}, "mock"),
        ({"backends": "mock", "dimension_scores": _SCORES}, "mock"),
        ({"backends": "mock", "dimension_scores": []}, "mock"),
        ({"stub": False, "backends": "mock", "dimension_scores": _SCORES}, "mock"),
        ({"dimension_scores": _SCORES}, "stub"),  # 现有 run_diagnostic 报告:没有 backends 标记,失败关闭
        ({"backends": "real", "dimension_scores": _SCORES}, "real"),
        ({"stub": False, "backends": "real", "dimension_scores": _SCORES}, "real"),
        ({"stub": False, "dimension_scores": _SCORES}, "stub"),
        ({}, "stub"),
        ({"dimension_scores": []}, "stub"),
        ({"backends": "real", "dimension_scores": []}, "stub"),
        ({"backends": "real"}, "stub"),
        ({"backends": "unknown", "dimension_scores": _SCORES}, "stub"),
        ({"backends": None, "dimension_scores": _SCORES}, "stub"),
        ({"dimension_scores": "lots"}, "stub"),
        ({"stub": "yes", "dimension_scores": _SCORES}, "stub"),  # stub 字段取真值判定
    ],
)
def test_grade_rules(payload, grade):
    assert diagnostic_grade(payload) == grade


def test_grade_does_not_mutate_the_payload():
    payload = {"backends": "mock", "dimension_scores": list(_SCORES)}
    snapshot = {"backends": "mock", "dimension_scores": list(_SCORES)}
    diagnostic_grade(payload)
    assert payload == snapshot


@pytest.mark.parametrize("bad", [None, [], "stub"])
def test_a_non_mapping_payload_is_a_type_error(bad):
    with pytest.raises(TypeError):
        diagnostic_grade(bad)


@pytest.mark.parametrize(
    ("grade", "accept_mock", "ok"),
    [
        ("real", False, True), ("real", True, True),
        ("mock", False, False), ("mock", True, True),
        ("stub", False, False), ("stub", True, False),
    ],
)
def test_acceptability(grade, accept_mock, ok):
    assert evidence_acceptable(grade, accept_mock) is ok


def test_an_unknown_grade_is_a_value_error():
    with pytest.raises(ValueError, match="bogus"):
        evidence_acceptable("bogus", True)


def test_grades_constant():
    assert GRADES == ("stub", "mock", "real")
```

- [ ] **Step 2: 运行,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_evidence.py`
Expected: 收集阶段报错 `ModuleNotFoundError: No module named 'controlplane.evidence'`。

- [ ] **Step 3: 写实现**

新建 `src/controlplane/evidence.py`:

```python
"""诊断证据等级(闭环集成设计 §3.1、R17)。全系统只有这一处实现:
reconcile 扩展、提名、规划处理器都调这里,不各自判断。"""
from __future__ import annotations

GRADES = ("stub", "mock", "real")


def diagnostic_grade(payload: dict) -> str:
    """给一份诊断报告的载荷定等级。

    - payload["stub"] 为真 → "stub"(占位报告,优先于其余字段)
    - payload["backends"] == "mock" → "mock"
    - backends == "real" 且 dimension_scores 是非空列表 → "real"
    - 其余(没有 backends 标记、取值不认识、没有分数、空报告)→ "stub":失败关闭,判不出来就按最弱的证据算
    """
    if not isinstance(payload, dict):
        raise TypeError(f"diagnostic payload must be a mapping, got {type(payload).__name__}")
    if payload.get("stub"):
        return "stub"
    if payload.get("backends") == "mock":
        return "mock"
    scores = payload.get("dimension_scores")
    has_scores = isinstance(scores, list) and len(scores) > 0
    if has_scores and payload.get("backends") == "real":
        return "real"
    return "stub"


def evidence_acceptable(grade: str, accept_mock: bool) -> bool:
    """real 恒可接受;mock 仅当 accept_mock 为真;stub 恒不可接受。"""
    if grade not in GRADES:
        raise ValueError(f"unknown evidence grade {grade!r}; expected one of {GRADES}")
    if grade == "real":
        return True
    if grade == "mock":
        return bool(accept_mock)
    return False
```

- [ ] **Step 4: 运行,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_evidence.py`
Expected: `30 passed`。

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: `B+156 passed`(以 820 为起点为 976),无警告。

- [ ] **Step 5: 提交**

```bash
git add src/controlplane/evidence.py
git add tests/test_evidence.py
git commit -m "feat: add the single evidence-grade implementation (stub, mock, real) and its acceptability rule"
```

---

### Task 25: 新包 `inference_bridge` 的公共类型与 ScriptedBridge

**Files:**
- Create: `src/inference_bridge/__init__.py`(21 行;任务 26 改为 27 行)
- Create: `src/inference_bridge/base.py`(62 行)
- Create: `src/inference_bridge/scripted.py`(48 行)
- Modify: `pyproject.toml`(wheel 包列表末尾追加 `src/inference_bridge`,不假设列表现有几项)
- Test: `tests/test_bridge_scripted.py`(新建,15 个用例)、`tests/test_bridge_packaging.py`(新建,本任务 2 个用例)

**Interfaces:**
- Consumes: `BigRef`、`GitStore`;`ledger.record_cost`(任务 21);`run_trace_file`(任务 19,只在测试里用)。
- Produces(计划 4 的 `BridgeDecisionBackend`、计划 7 的提案生成器与 judge、计划 9 的组合根依赖):
  - `BridgeResult(text, input_tokens, output_tokens, model, log_ref)`(frozen)
  - `BridgeError(category, reason="", *, attempts=0, log_ref=None)`:`category` 只能是 `"T1"`、`"T2"`
  - `InferenceBridge`(`runtime_checkable` 的 Protocol):`complete(prompt, *, run_id, task_id) -> BridgeResult`
  - `ScriptedBridge(script, *, model="scripted", store=None)`:属性 `calls: list[tuple[prompt, run_id, task_id]]`、`remaining`
  - `ScriptExhaustedError`(不是 `BridgeError` 的子类)
  - `_check_segment(name, value)`(包内共用的路径段校验,`ScriptedBridge` 与任务 26 的 `CliBridge` 都调用)
  - **契约**:每次 `complete` 必须用不同的 `task_id`,否则成本按 `(task_id, external_inference)` 去重,后几次调用的成本丢失(计划 4、7 的解析重试要给每次重试换 `task_id`)。
  - `record_bridge_cost(store, result, *, run_id, task_id, calls)`、`BRIDGE_COST_TYPE = "external_inference"`

**设计要点:**
- **记账只有一处**:`record_bridge_cost`。`store` 为 `None` 不记;输入、输出 token 都已知才记 `tokens`(两者之和),否则省略键;`(task_id, "external_inference")` 的幂等由 `ledger` 保证。`ScriptedBridge` 与任务 26 的 `CliBridge` 都调它,所以测试替身也能走完记账路径。
- **失败的调用不记账**(有测试:第三次调用抛 `BridgeError`,`cost_records` 只有前两条)。
- **两个桥接共用一处段校验。** `run_id`、`task_id` 会拼进日志路径与成本记录,`_check_segment` 放在 `base.py`;`ScriptedBridge` 也调用它,不合法时抛 `ValueError`,不记 `calls`、不消耗脚本(有测试)。
- **新顶层包要进 wheel 列表。** 本任务先建包,`tests/test_mt_packaging.py::test_every_top_level_src_package_is_in_the_wheel_list` 会失败,改完 `pyproject.toml` 才通过。
- **依赖方向用测试钉住**(两条用例都先断言待扫描的文件列表非空,目录为空时循环一次不跑、断言恒真的漏洞因此堵上):`src/controlplane/*.py` 不导入 `inference_bridge`;`src/inference_bridge/*.py` 不导入 `src/` 下除 `controlplane` 以外的任何一方包(用 `ast` 扫导入)。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_bridge_scripted.py`:

```python
import pytest

from controlplane.envelope import BigRef
from controlplane.gitstore import GitStore
from controlplane.paths import run_trace_file
from inference_bridge import (
    BridgeError,
    BridgeResult,
    InferenceBridge,
    ScriptedBridge,
    ScriptExhaustedError,
)


def test_bridge_error_carries_category_reason_attempts_and_log_ref():
    ref = BigRef(uri="file:///x.log", sha256="c" * 64, size_bytes=1)
    exc = BridgeError("T1", "timed out", attempts=3, log_ref=ref)
    assert (exc.category, exc.reason, exc.attempts, exc.log_ref) == ("T1", "timed out", 3, ref)
    assert str(exc) == "T1: timed out"
    assert BridgeError("T2").reason == "" and BridgeError("T2").attempts == 0


@pytest.mark.parametrize("bad", ["T3", "t1", ""])
def test_bridge_error_only_knows_t1_and_t2(bad):
    with pytest.raises(ValueError):
        BridgeError(bad)


def test_bridge_result_is_frozen():
    r = BridgeResult(text="hi", input_tokens=None, output_tokens=None, model="m", log_ref=None)
    with pytest.raises(AttributeError):
        r.text = "changed"


def test_scripted_bridge_answers_in_order_and_records_calls():
    b = ScriptedBridge(["first", "second"])
    assert isinstance(b, InferenceBridge)
    r1 = b.complete("p1", run_id="run-1", task_id="plan-1")
    r2 = b.complete("p2", run_id="run-1", task_id="plan-2")
    assert (r1.text, r2.text) == ("first", "second")
    assert r1 == BridgeResult(text="first", input_tokens=None, output_tokens=None, model="scripted", log_ref=None)
    assert b.calls == [("p1", "run-1", "plan-1"), ("p2", "run-1", "plan-2")]
    assert b.remaining == 0


def test_scripted_bridge_raises_injected_failures_in_sequence():
    b = ScriptedBridge([BridgeError("T1", "flaky"), "ok", BridgeError("T2", "bad")])
    with pytest.raises(BridgeError) as e1:
        b.complete("p", run_id="run-1", task_id="t1")
    assert e1.value.category == "T1"
    assert b.complete("p", run_id="run-1", task_id="t2").text == "ok"
    with pytest.raises(BridgeError) as e2:
        b.complete("p", run_id="run-1", task_id="t3")
    assert e2.value.category == "T2"
    assert len(b.calls) == 3  # 失败的调用也记下


def test_scripted_bridge_can_return_a_full_result():
    canned = BridgeResult(text="{}", input_tokens=10, output_tokens=5, model="claude-x", log_ref=None)
    assert ScriptedBridge([canned]).complete("p", run_id="r", task_id="t") is canned


def test_running_out_of_script_is_loud_and_not_a_bridge_error():
    b = ScriptedBridge(["only"])
    b.complete("p", run_id="r", task_id="t")
    with pytest.raises(ScriptExhaustedError):
        b.complete("p", run_id="r", task_id="t2")
    assert not issubclass(ScriptExhaustedError, BridgeError)


def test_scripted_bridge_rejects_unknown_script_items():
    with pytest.raises(TypeError):
        ScriptedBridge([42])


def test_scripted_bridge_records_cost_when_given_a_store(tmp_path):
    store = GitStore(str(tmp_path))
    store.init()
    canned = BridgeResult(text="x", input_tokens=10, output_tokens=5, model="m", log_ref=None)
    b = ScriptedBridge(["a", canned, BridgeError("T1")], store=store)
    b.complete("p", run_id="run-1", task_id="plan-1")
    b.complete("p", run_id="run-1", task_id="plan-2")
    with pytest.raises(BridgeError):
        b.complete("p", run_id="run-1", task_id="plan-3")
    recs = store.read_product(run_trace_file("run-1")).payload["cost_records"]
    assert [(r["task_id"], r.get("tokens"), r["calls"]) for r in recs] == [
        ("plan-1", None, 1), ("plan-2", 15, 1),
    ]
    assert all(r["cost_type"] == "external_inference" for r in recs)

@pytest.mark.parametrize(("run_id", "task_id"), [("", "t"), ("r", "a/b"), ("..", "t"), ("r", ".")])
def test_scripted_bridge_checks_ids_like_the_cli_bridge(run_id, task_id):
    b = ScriptedBridge(["unused"])
    with pytest.raises(ValueError):
        b.complete("p", run_id=run_id, task_id=task_id)
    assert b.calls == [] and b.remaining == 1  # 没有消耗脚本

```

新建 `tests/test_bridge_packaging.py`:

```python
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_controlplane_does_not_import_the_bridge():
    files = sorted((ROOT / "src" / "controlplane").glob("*.py"))
    assert files  # 目录为空时下面的循环一次都不跑,断言恒真
    for path in files:
        assert "inference_bridge" not in _imports(path), path.name


def test_the_bridge_only_depends_on_controlplane_and_the_stdlib():
    allowed_first_party = {"controlplane", "inference_bridge"}
    other_first_party = {p.name for p in (ROOT / "src").iterdir() if p.is_dir()} - allowed_first_party
    files = sorted((ROOT / "src" / "inference_bridge").glob("*.py"))
    assert files
    for path in files:
        assert not (_imports(path) & other_first_party), path.name
```

- [ ] **Step 2: 运行,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_bridge_scripted.py`
Expected: 收集阶段报错 `ModuleNotFoundError: No module named 'inference_bridge'`。

- [ ] **Step 3: 写实现**

新建 `src/inference_bridge/base.py`:

```python
"""桥接的公共类型:结果、错误、协议,以及每次成功调用后的统一记账。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from controlplane.envelope import BigRef
from controlplane.gitstore import GitStore
from controlplane.ledger import record_cost

BRIDGE_COST_TYPE = "external_inference"
_CATEGORIES = ("T1", "T2")


def _check_segment(name: str, value: str) -> None:
    """run_id、task_id 会拼进日志路径与成本记录:必须是单个非空路径段。两个桥接都在调用前检查。"""
    if not isinstance(value, str) or not value or "/" in value or "\\" in value or value in (".", ".."):
        raise ValueError(f"{name} must be a non-empty path segment, got {value!r}")


@dataclass(frozen=True)
class BridgeResult:
    text: str
    input_tokens: int | None
    output_tokens: int | None
    model: str
    log_ref: BigRef | None


class BridgeError(Exception):
    """桥接调用失败。category:"T1" 瞬态(重试已耗尽)| "T2" 确定性(重试无益)。
    升级为 T3 是调用方的事(设计 R18:无论 T1 还是 T2,调用方一律升级为 T3)。
    reason、str(exc) 与 __cause__ / __context__ 上都不挂带凭证的原异常:CliBridge 在 except 块外才抛它。"""

    def __init__(
        self, category: str, reason: str = "", *, attempts: int = 0, log_ref: BigRef | None = None
    ) -> None:
        if category not in _CATEGORIES:
            raise ValueError(f"BridgeError category must be one of {_CATEGORIES}, got {category!r}")
        super().__init__(f"{category}: {reason}")
        self.category = category
        self.reason = reason
        self.attempts = attempts
        self.log_ref = log_ref


@runtime_checkable
class InferenceBridge(Protocol):
    def complete(self, prompt: str, *, run_id: str, task_id: str) -> BridgeResult: ...


def record_bridge_cost(
    store: GitStore | None, result: BridgeResult, *, run_id: str, task_id: str, calls: int
) -> None:
    """成功调用后记一条 external_inference。token 数只有输入、输出都已知时才记(否则省略键);
    同一 (task_id, external_inference) 只记一次,由 ledger.record_cost 保证。store 为 None 时不记。"""
    if store is None:
        return
    tokens = None
    if result.input_tokens is not None and result.output_tokens is not None:
        tokens = result.input_tokens + result.output_tokens
    record_cost(store, run_id, task_id=task_id, cost_type=BRIDGE_COST_TYPE, tokens=tokens, calls=calls)
```

新建 `src/inference_bridge/scripted.py`:

```python
"""测试替身:按顺序吐罐头回答,脚本里放 BridgeError 即在该次调用抛出。"""
from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from controlplane.gitstore import GitStore
from inference_bridge.base import BridgeError, BridgeResult, _check_segment, record_bridge_cost


class ScriptExhaustedError(Exception):
    """脚本用完了还被调用。故意不是 BridgeError:这是测试写错了,不是桥接失败,不该被调用方当 T1/T2 吞掉。"""


class ScriptedBridge:
    def __init__(
        self,
        script: Iterable[str | BridgeResult | BridgeError],
        *,
        model: str = "scripted",
        store: GitStore | None = None,
    ) -> None:
        items = list(script)
        for item in items:
            if not isinstance(item, (str, BridgeResult, BridgeError)):
                raise TypeError(f"script items must be str, BridgeResult or BridgeError, got {item!r}")
        self._script: deque[str | BridgeResult | BridgeError] = deque(items)
        self._model = model
        self._store = store
        self.calls: list[tuple[str, str, str]] = []

    @property
    def remaining(self) -> int:
        return len(self._script)

    def complete(self, prompt: str, *, run_id: str, task_id: str) -> BridgeResult:
        _check_segment("run_id", run_id)
        _check_segment("task_id", task_id)
        self.calls.append((prompt, run_id, task_id))
        if not self._script:
            raise ScriptExhaustedError(f"ScriptedBridge ran out of answers at call {len(self.calls)}")
        item = self._script.popleft()
        if isinstance(item, BridgeError):
            raise item
        if isinstance(item, str):
            item = BridgeResult(text=item, input_tokens=None, output_tokens=None, model=self._model, log_ref=None)
        record_bridge_cost(self._store, item, run_id=run_id, task_id=task_id, calls=1)
        return item
```

新建 `src/inference_bridge/__init__.py`:

```python
"""外部推理桥接(闭环集成设计 §5.1,R15)。依赖 controlplane;controlplane 不导入本包。"""
from __future__ import annotations

from inference_bridge.base import (
    BRIDGE_COST_TYPE,
    BridgeError,
    BridgeResult,
    InferenceBridge,
    record_bridge_cost,
)
from inference_bridge.scripted import ScriptedBridge, ScriptExhaustedError

__all__ = [
    "BRIDGE_COST_TYPE",
    "BridgeError",
    "BridgeResult",
    "InferenceBridge",
    "ScriptExhaustedError",
    "ScriptedBridge",
    "record_bridge_cost",
]
```

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_mt_packaging.py`
Expected: `1 failed, 1 passed`,失败信息是 `missing from [tool.hatch...wheel].packages: ['src/inference_bridge']`。

在仓库根目录运行:

用下面的脚本在 `packages = [...]` 那一行末尾追加 `"src/inference_bridge"`。它不假设列表里现在有几项(别的计划可能已先加了自己的包);已含 `"src/inference_bridge"` 时什么都不改:

```bash
uv run python - <<'EOF'
import re
from pathlib import Path

p = Path("pyproject.toml")
s = p.read_text(encoding="utf-8")
m = re.search(r'^packages = \[(.*)\]$', s, re.M)
assert m, "wheel packages line not found"
items = [x.strip() for x in m.group(1).split(",") if x.strip()]
if '"src/inference_bridge"' in items:
    print("already listed")
else:
    items.append('"src/inference_bridge"')
    s = s[: m.start()] + "packages = [" + ", ".join(items) + "]" + s[m.end():]
    p.write_text(s, encoding="utf-8")
print(re.search(r'^packages = .*$', s, re.M).group(0))
EOF
```

预期输出(从基线四项开始时):`packages = ["src/controlplane", "src/simulator", "src/diagnostic", "src/mindtrainer", "src/inference_bridge"]`。若别的计划先落地并加了包,它们出现在 `"src/inference_bridge"` 前面;重复运行时先打印 `already listed`,再打印同一行。

- [ ] **Step 4: 运行,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_bridge_scripted.py tests/test_bridge_packaging.py tests/test_mt_packaging.py`
Expected: `19 passed`。

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: `B+173 passed`(以 820 为起点为 993),无警告。

- [ ] **Step 5: 提交**

```bash
git add src/inference_bridge/__init__.py
git add src/inference_bridge/base.py
git add src/inference_bridge/scripted.py
git add pyproject.toml
git add tests/test_bridge_scripted.py
git add tests/test_bridge_packaging.py
git commit -m "feat: add the inference_bridge package (result, error, protocol, scripted test double, single cost hook)"
```

---

### Task 26: CliBridge

**Files:**
- Create: `src/inference_bridge/cli.py`(203 行)
- Modify: `src/inference_bridge/__init__.py`(21 → 27 行,导出 `CliBridge`、`RunOutcome`、`Runner`、`subprocess_runner`、`DEFAULT_COMMANDS`)
- Modify: `.gitignore`(+3 行,忽略 `.rismforge/`)
- Test: `tests/test_bridge_cli.py`(新建,47 个用例)、`tests/test_bridge_packaging.py`(加 1 个用例,共 3 个)

**Interfaces:**
- Consumes: `BackendConfig`(含任务 22 的 `command`、`timeout_s`、可空 `token_env`、`env_allowlist`、`workdir`)、`DEFAULT_ENV_ALLOWLIST`、`resolve_credential`、`redact`;`BigRef`;`GitStore.repo_root`;`record_bridge_cost`、`BridgeError`、`BridgeResult`(任务 25)。
- Produces:
  - `CliBridge(config, *, store=None, log_dir=None, env=None, runner=subprocess_runner, sleep=time.sleep)`,满足 `InferenceBridge`
  - `RunOutcome(returncode: int | None, stdout: str, stderr: str, timed_out: bool = False)`
  - `Runner = Callable[[list[str], str, float, Mapping[str, str], str], RunOutcome]`(argv、标准输入、超时秒数、子进程环境、工作目录)
  - `subprocess_runner`:默认 runner,真起子进程,工作目录是第五个参数 `cwd`;超时由 `subprocess.run(timeout=...)` 杀掉子进程,返回 `timed_out=True`
  - `DEFAULT_COMMANDS: dict[str, list[str]]`:`claude`、`codex` 两个工具的默认 argv 模板(定义在 `cli.py`,由包的 `__init__` 再导出)
  - `LOG_SUBDIR = ".rismforge/logs"`

**设计要点:**
- **提示词经标准输入传。** argv 来自 `BackendConfig.command`,模板里的 `{model}` 换成 `model_name`,其余原样;不经 shell。
- **凭证只走环境变量,而且子进程只拿白名单里的那些。** `env` 缺省取 `os.environ` 的拷贝(`self._env`,完整环境,保留不动);凭证值在构造时经 `resolve_credential` 取出,只用于脱敏。传给子进程的是过滤后的另一份:`allowed = 白名单 ∪ {token_env}`,`child_env = {k: v for k, v in self._env.items() if k in allowed}`;`env_allowlist` 为 `None` 才用 `DEFAULT_ENV_ALLOWLIST`,空 `list` 就只传 `token_env`,`token_env` 为 `None` 时不传任何凭证变量。有测试断言凭证不在 argv 里、凭证变量到得了子进程、白名单外的变量到不了。
- **每次调用一个空临时目录。** `complete` 用 `tempfile.TemporaryDirectory()` 建目录,作为这次调用所有尝试的工作目录(runner 的第五个参数 `cwd`);`with` 保证成功、T1 耗尽、T2 都会把它删掉,同一次调用的重试共用同一个目录。
- **默认参数模板是常量。** `DEFAULT_COMMANDS`(`claude`、`codex` 两项)定义在 `cli.py`、由包的 `__init__` 再导出:claude 关闭全部工具、不落会话文件,codex 只读沙箱、不要求在 git 仓库里、不落会话文件。它只是现成的 argv 模板,不自动写进任何配置。
- **失败分类与重试**:超时、非零退出、空输出 → T1,`sleep(backoff_base_s * 2**(attempt-1))` 后重试;耗尽抛 `BridgeError("T1", ..., attempts=max_attempts)`,`reason` 带最后一次的原因和脱敏后的 stderr 末尾。子进程启动阶段的任何 `OSError` → 立即 `BridgeError("T2")`(参数化测试覆盖 `NotADirectoryError`、ENOEXEC、E2BIG;另有一条真起一个不带 shebang 的可执行文件)。退避测试断言 `sleep` 收到的序列(`[0.5, 1.0]`),最后一次失败后不睡。
- **成功后记账**:构造时传了 `store`,就 `record_bridge_cost(..., calls=本次用掉的尝试次数)`,`tokens` 省略;失败不记。
- **日志**:每次 `complete` 写一份日志(成功或失败都写),内容是后端名、模型、argv、`run_id`、`task_id`、提示词、每次尝试的 stdout/stderr,**整体过 `redact`**;路径 `<日志根>/<run_id>/<task_id>-<k>.log`;返回或异常里带 `BigRef(uri="file://<绝对路径>", sha256, size_bytes)`。日志根缺省是 `<store.repo_root>/.rismforge/logs`,首次写入时放一个内容为 `*` 的 `.gitignore`。`run_id`、`task_id` 会拼进路径,必须是不含分隔符的非空段,`.`、`..` 也拒绝。
- **凭证不外泄,用测试证明**:日志全文不含凭证;`str(exc)`、`exc.reason`、`exc.args` 不含凭证;`PermissionError` 的信息里带凭证时也被脱敏,且 `BridgeError` 在 `except` 块外抛出,`__cause__` 与 `__context__` 都是 `None`;一次真实调用之后,控制面仓库里 `git ls-files` 列出的每个文件都不含凭证;`git status --porcelain --untracked-files=all` 看不到 `.rismforge`。
- **解码不抛。** `subprocess_runner` 用 `encoding="utf-8", errors="replace"`,不再用按 locale 严格解码的 `text=True`;非法字节变成 U+FFFD(有测试)。
- **真起子进程的七条用例**:用 `sys.executable -c <脚本>` 做假可执行体,回显标准输入(大写)与 `{model}` 替换结果;非零退出且 stderr 带凭证(验证 T1 与脱敏);睡 5 秒配 `timeout_s=0.5`(验证超时是 T1);输出 `\xff\xfe`(验证替换解码);另有不存在的绝对路径与 `tmp_path` 里一个不带 shebang 的 `0o755` 脚本(都验证 T2)。第七条用一个打印自己环境变量名、工作目录与目录内容的假工具:父进程里用 `monkeypatch` 设一个不在白名单里的变量,验证子进程看不到它、看得到 `FAKE_TOKEN`,工作目录是空的、调用结束后已被删除。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_bridge_cli.py`:

```python
import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

import pytest

import inference_bridge
from controlplane.config import DEFAULT_ENV_ALLOWLIST, MissingCredentialError, load_backend_config
from controlplane.gitstore import GitStore
from controlplane.observe import cost
from controlplane.paths import run_trace_file
from inference_bridge import DEFAULT_COMMANDS, BridgeError, CliBridge, InferenceBridge, RunOutcome

SECRET = "sk-test-SECRET-0123456789"


def _cfg(**over):
    raw = {
        "backend": "fake_cli", "invocation_form": "cli", "model_name": "fake-model",
        "prompt_template_version": "v1", "token_env": "FAKE_TOKEN",
        "retry_policy": {"max_attempts": 3, "backoff_base_s": 0.5},
        "command": ["fake-tool", "-p", "--model", "{model}"], "timeout_s": 5,
    }
    raw.update(over)
    return load_backend_config(raw)


class FakeRunner:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.cwd_seen = []  # 每次调用当时工作目录的状态:(是否存在, 目录里的文件名)

    def __call__(self, argv, stdin, timeout_s, env, cwd):
        self.calls.append((argv, stdin, timeout_s, dict(env), cwd))
        path = Path(cwd)
        self.cwd_seen.append((path.is_dir(), [p.name for p in path.iterdir()] if path.is_dir() else []))
        out = self.outcomes.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out


def _ok(text="hello"):
    return RunOutcome(returncode=0, stdout=text, stderr="")


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path / "repo"))
    s.init()
    return s


def _bridge(runner, *, store=None, log_dir=None, sleeps=None, env=None, **over):
    return CliBridge(
        _cfg(**over), store=store, log_dir=log_dir,
        env={"FAKE_TOKEN": SECRET, "PATH": "/bin"} if env is None else env,
        runner=runner, sleep=(sleeps.append if sleeps is not None else (lambda s: None)),
    )


def _log_path(ref):
    return Path(urlparse(ref.uri).path)


def test_prompt_goes_through_stdin_and_the_template_is_filled():
    runner = FakeRunner([_ok("  the answer \n")])
    b = _bridge(runner)
    assert isinstance(b, InferenceBridge)
    r = b.complete("plan the next round", run_id="run-1", task_id="plan-1")
    argv, stdin, timeout_s, env, cwd = runner.calls[0]
    assert argv == ["fake-tool", "-p", "--model", "fake-model"]
    assert stdin == "plan the next round"
    assert timeout_s == 5.0
    assert env["FAKE_TOKEN"] == SECRET  # 凭证只经环境变量给子进程
    assert SECRET not in " ".join(argv)
    assert (r.text, r.model, r.input_tokens, r.output_tokens, r.log_ref) == (
        "the answer", "fake-model", None, None, None,
    )


@pytest.mark.parametrize(
    "bad",
    [
        RunOutcome(returncode=None, stdout="", stderr="", timed_out=True),
        RunOutcome(returncode=2, stdout="partial", stderr="boom"),
        RunOutcome(returncode=0, stdout="   \n", stderr=""),
    ],
    ids=["timeout", "nonzero", "empty"],
)
def test_transient_failures_are_retried_with_exponential_backoff(bad):
    sleeps = []
    runner = FakeRunner([bad, bad, _ok("finally")])
    r = _bridge(runner, sleeps=sleeps).complete("p", run_id="run-1", task_id="t")
    assert r.text == "finally"
    assert len(runner.calls) == 3
    assert sleeps == [0.5, 1.0]


def test_exhausted_retries_raise_t1_with_the_attempt_count():
    sleeps = []
    bad = RunOutcome(returncode=1, stdout="", stderr="rate limited")
    runner = FakeRunner([bad, bad, bad])
    with pytest.raises(BridgeError) as exc:
        _bridge(runner, sleeps=sleeps).complete("p", run_id="run-1", task_id="t")
    assert exc.value.category == "T1"
    assert exc.value.attempts == 3
    assert "exit code 1" in exc.value.reason and "rate limited" in exc.value.reason
    assert sleeps == [0.5, 1.0]  # 最后一次失败之后不再睡


def test_a_single_attempt_policy_does_not_sleep():
    sleeps = []
    runner = FakeRunner([RunOutcome(returncode=1, stdout="", stderr="")])
    with pytest.raises(BridgeError):
        _bridge(runner, sleeps=sleeps, retry_policy={"max_attempts": 1, "backoff_base_s": 9}).complete(
            "p", run_id="r", task_id="t"
        )
    assert sleeps == []


def test_a_missing_executable_is_t2_and_not_retried():
    runner = FakeRunner([FileNotFoundError(2, "No such file or directory")])
    with pytest.raises(BridgeError) as exc:
        _bridge(runner).complete("p", run_id="r", task_id="t")
    assert (exc.value.category, exc.value.attempts) == ("T2", 1)
    assert len(runner.calls) == 1


def test_success_records_one_external_inference_cost_with_tokens_omitted(store):
    bad = RunOutcome(returncode=1, stdout="", stderr="")
    _bridge(FakeRunner([bad, _ok()]), store=store).complete("p", run_id="run-1", task_id="plan-1")
    [rec] = store.read_product(run_trace_file("run-1")).payload["cost_records"]
    assert (rec["task_id"], rec["cost_type"], rec["calls"]) == ("plan-1", "external_inference", 2)
    assert "tokens" not in rec
    assert cost(store, "run-1").endswith("total: gpu_hours=0.0 tokens=0")


def test_failure_records_no_cost(store):
    bad = RunOutcome(returncode=1, stdout="", stderr="")
    with pytest.raises(BridgeError):
        _bridge(FakeRunner([bad] * 3), store=store).complete("p", run_id="run-1", task_id="t")
    assert not store.exists(run_trace_file("run-1"))


def test_without_store_or_log_dir_nothing_is_written(tmp_path):
    r = _bridge(FakeRunner([_ok()])).complete("p", run_id="r", task_id="t")
    assert r.log_ref is None


def test_the_log_is_redacted_hashed_and_referenced(tmp_path):
    leaky = RunOutcome(returncode=0, stdout=f"echo {SECRET} back", stderr=f"debug token={SECRET}")
    r = _bridge(FakeRunner([leaky]), log_dir=tmp_path / "logs").complete(
        f"prompt that quotes {SECRET}", run_id="run-1", task_id="plan-1",
    )
    path = _log_path(r.log_ref)
    assert path == (tmp_path / "logs" / "run-1" / "plan-1-1.log").resolve()
    data = path.read_bytes()
    assert r.log_ref.sha256 == hashlib.sha256(data).hexdigest()
    assert r.log_ref.size_bytes == len(data)
    text = data.decode()
    assert SECRET not in text
    assert "***REDACTED***" in text and "prompt that quotes" in text and "debug token=" in text
    assert SECRET not in r.text


def test_repeated_calls_for_the_same_task_get_separate_logs(tmp_path):
    b = _bridge(FakeRunner([_ok("a"), _ok("b")]), log_dir=tmp_path / "logs")
    r1 = b.complete("p", run_id="run-1", task_id="t")
    r2 = b.complete("p", run_id="run-1", task_id="t")
    assert _log_path(r1.log_ref).name == "t-1.log" and _log_path(r2.log_ref).name == "t-2.log"


def test_the_failure_log_holds_every_attempt_and_the_error_never_carries_the_secret(tmp_path):
    bad = RunOutcome(returncode=3, stdout=f"oops {SECRET}", stderr=f"auth failed for {SECRET}")
    with pytest.raises(BridgeError) as exc:
        _bridge(FakeRunner([bad] * 3), log_dir=tmp_path / "logs").complete("p", run_id="run-1", task_id="t")
    assert SECRET not in str(exc.value) and SECRET not in exc.value.reason
    assert SECRET not in repr(exc.value.args)
    text = _log_path(exc.value.log_ref).read_text()
    assert SECRET not in text
    assert [f"attempt {i}: exit code 3" in text for i in (1, 2, 3)] == [True, True, True]


def test_a_not_runnable_error_message_is_redacted(tmp_path):
    runner = FakeRunner([PermissionError(13, f"denied while holding {SECRET}")])
    with pytest.raises(BridgeError) as exc:
        _bridge(runner, log_dir=tmp_path / "logs").complete("p", run_id="r", task_id="t")
    assert SECRET not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__context__ is None
    assert SECRET not in _log_path(exc.value.log_ref).read_text()


@pytest.mark.parametrize(
    "err",
    [
        NotADirectoryError(20, "Not a directory"),
        OSError(8, "Exec format error"),
        OSError(7, "Argument list too long"),
    ],
    ids=["ENOTDIR", "ENOEXEC", "E2BIG"],
)
def test_any_os_error_at_start_is_t2_and_not_retried(err):
    runner = FakeRunner([err, _ok()])
    with pytest.raises(BridgeError) as exc:
        _bridge(runner).complete("p", run_id="r", task_id="t")
    assert (exc.value.category, exc.value.attempts) == ("T2", 1)
    assert len(runner.calls) == 1
    assert exc.value.__context__ is None


def test_logs_default_under_the_store_and_stay_untracked(store):
    r = _bridge(FakeRunner([_ok()]), store=store).complete("p", run_id="run-1", task_id="plan-1")
    path = _log_path(r.log_ref)
    assert path.is_relative_to(store.repo_root.resolve() / ".rismforge" / "logs")
    assert (store.repo_root / ".rismforge" / "logs" / ".gitignore").read_text() == "*\n"
    status = subprocess.run(
        ["git", "-C", str(store.repo_root), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert ".rismforge" not in status  # 日志目录对 git 不可见
    tracked = subprocess.run(
        ["git", "-C", str(store.repo_root), "ls-files"], capture_output=True, text=True, check=True,
    ).stdout
    assert ".rismforge" not in tracked and "run_trace.yaml" in tracked


def test_the_credential_is_never_written_into_the_git_tree(store):
    leaky = RunOutcome(returncode=0, stdout=f"x {SECRET}", stderr=SECRET)
    _bridge(FakeRunner([leaky]), store=store).complete(f"p {SECRET}", run_id="run-1", task_id="t")
    tracked = subprocess.run(
        ["git", "-C", str(store.repo_root), "ls-files"], capture_output=True, text=True, check=True,
    ).stdout.split()
    for rel in tracked:
        assert SECRET not in (store.repo_root / rel).read_text()


def test_a_missing_credential_fails_at_construction_naming_only_the_variable():
    with pytest.raises(MissingCredentialError, match="FAKE_TOKEN"):
        CliBridge(_cfg(), env={}, runner=FakeRunner([]))


def test_a_tool_with_its_own_login_needs_no_credential():
    runner = FakeRunner([_ok()])
    b = CliBridge(_cfg(token_env=None), env={"PATH": "/bin"}, runner=runner, sleep=lambda s: None)
    assert b.complete("p", run_id="r", task_id="t").text == "hello"


@pytest.mark.parametrize(
    ("over", "match"),
    [
        ({"invocation_form": "api"}, "invocation_form"),
        ({"command": None}, "command"),
        ({"retry_policy": {"max_attempts": 0}}, "max_attempts"),
        ({"retry_policy": {"backoff_base_s": -1}}, "backoff_base_s"),
    ],
)
def test_bad_configs_are_rejected_at_construction(over, match):
    with pytest.raises(ValueError, match=match):
        CliBridge(_cfg(**over), env={"FAKE_TOKEN": SECRET}, runner=FakeRunner([]))


@pytest.mark.parametrize(("run_id", "task_id"), [("", "t"), ("r", "a/b"), ("..", "t")])
def test_ids_must_be_safe_path_segments(run_id, task_id):
    with pytest.raises(ValueError):
        _bridge(FakeRunner([_ok()])).complete("p", run_id=run_id, task_id=task_id)


_FAKE_TOOL = (
    "import os, sys\n"
    "data = sys.stdin.read()\n"
    "if sys.argv[1] == 'fail':\n"
    "    sys.stderr.write('nope ' + os.environ.get('FAKE_TOKEN', ''))\n"
    "    sys.exit(4)\n"
    "if sys.argv[1] == 'sleep':\n"
    "    import time; time.sleep(5)\n"
    "print('echo:' + data.upper() + ' model=' + sys.argv[2])\n"
)


def _real(mode, store=None, **over):
    return CliBridge(
        _cfg(command=[sys.executable, "-c", _FAKE_TOOL, mode, "{model}"], **over),
        store=store, env={"FAKE_TOKEN": SECRET}, sleep=lambda s: None,
    )


def test_a_real_subprocess_round_trip(store):
    r = _real("ok", store=store).complete("hello bridge", run_id="run-1", task_id="plan-1")
    assert r.text == "echo:HELLO BRIDGE model=fake-model"
    assert _log_path(r.log_ref).is_file()
    assert store.exists(run_trace_file("run-1"))


def test_a_real_subprocess_failure_is_t1_and_redacted(store):
    with pytest.raises(BridgeError) as exc:
        _real("fail", store=store, retry_policy={"max_attempts": 2, "backoff_base_s": 0}).complete(
            "p", run_id="run-1", task_id="t"
        )
    assert exc.value.category == "T1" and exc.value.attempts == 2
    assert "exit code 4" in exc.value.reason and "nope ***REDACTED***" in exc.value.reason
    assert SECRET not in _log_path(exc.value.log_ref).read_text()


def test_a_real_subprocess_timeout_is_t1():
    with pytest.raises(BridgeError) as exc:
        _real("sleep", timeout_s=0.5, retry_policy={"max_attempts": 1}).complete("p", run_id="r", task_id="t")
    assert exc.value.category == "T1" and "timed out" in exc.value.reason


def test_a_real_missing_executable_is_t2():
    b = CliBridge(
        _cfg(command=["/nonexistent/rismforge-no-such-tool"]), env={"FAKE_TOKEN": SECRET}, sleep=lambda s: None,
    )
    with pytest.raises(BridgeError) as exc:
        b.complete("p", run_id="r", task_id="t")
    assert exc.value.category == "T2"


def test_a_real_script_without_a_shebang_is_t2(tmp_path):
    tool = tmp_path / "no-shebang-tool"
    tool.write_text("echo hello\n", encoding="utf-8")
    tool.chmod(0o755)
    b = CliBridge(_cfg(command=[str(tool)]), env={"FAKE_TOKEN": SECRET}, sleep=lambda s: None)
    with pytest.raises(BridgeError) as exc:
        b.complete("p", run_id="r", task_id="t")
    assert exc.value.category == "T2" and exc.value.attempts == 1
    assert exc.value.__context__ is None


def test_non_utf8_output_is_decoded_with_replacement():
    script = "import sys; sys.stdout.buffer.write(b'ok \\xff\\xfe end'); sys.stdout.flush()"
    b = CliBridge(
        _cfg(command=[sys.executable, "-c", script]), env={"FAKE_TOKEN": SECRET}, sleep=lambda s: None,
    )
    r = b.complete("p", run_id="r", task_id="t")
    assert r.text == "ok \ufffd\ufffd end"


def _child_env_of(runner):
    return runner.calls[0][3]


def test_the_child_env_is_the_default_allowlist_plus_token_env_and_nothing_else():
    env = {
        "FAKE_TOKEN": SECRET, "PATH": "/bin", "HOME": "/home/u", "LANG": "C",
        "AWS_SECRET_ACCESS_KEY": "other-secret", "GITHUB_TOKEN": "other-token",
    }
    runner = FakeRunner([_ok()])
    _bridge(runner, env=env).complete("p", run_id="r", task_id="t")
    assert _child_env_of(runner) == {"FAKE_TOKEN": SECRET, "PATH": "/bin", "HOME": "/home/u", "LANG": "C"}
    assert set(_child_env_of(runner)) - {"FAKE_TOKEN"} <= set(DEFAULT_ENV_ALLOWLIST)


def test_an_explicit_allowlist_replaces_the_default_and_token_env_is_added_automatically():
    env = {"FAKE_TOKEN": SECRET, "PATH": "/bin", "LANG": "C", "EXTRA": "x"}
    runner = FakeRunner([_ok()])
    _bridge(runner, env=env, env_allowlist=["LANG", "EXTRA", "ABSENT"]).complete("p", run_id="r", task_id="t")
    # PATH 不在名单里,不传;FAKE_TOKEN 不在名单里也自动加入;名单里有、环境里没有的 ABSENT 直接略过
    assert _child_env_of(runner) == {"FAKE_TOKEN": SECRET, "LANG": "C", "EXTRA": "x"}


def test_without_a_token_env_no_credential_variable_is_passed():
    env = {"FAKE_TOKEN": SECRET, "PATH": "/bin"}
    runner = FakeRunner([_ok()])
    _bridge(runner, env=env, token_env=None).complete("p", run_id="r", task_id="t")
    assert _child_env_of(runner) == {"PATH": "/bin"}


def test_an_empty_allowlist_passes_only_the_token_env():
    env = {"FAKE_TOKEN": SECRET, "PATH": "/bin", "HOME": "/home/u"}
    runner = FakeRunner([_ok()])
    _bridge(runner, env=env, env_allowlist=[]).complete("p", run_id="r", task_id="t")
    assert _child_env_of(runner) == {"FAKE_TOKEN": SECRET}


def test_every_call_runs_in_its_own_empty_directory_that_is_removed_afterwards():
    runner = FakeRunner([_ok(), _ok()])
    b = _bridge(runner)
    b.complete("p", run_id="r", task_id="t1")
    b.complete("p", run_id="r", task_id="t2")
    (_, _, _, _, cwd1), (_, _, _, _, cwd2) = runner.calls
    assert cwd1 != cwd2
    assert runner.cwd_seen == [(True, []), (True, [])]  # 调用当时:目录存在,而且是空的
    assert not Path(cwd1).exists() and not Path(cwd2).exists()  # 调用之后:已删除


def test_the_attempts_of_one_call_share_one_directory_which_is_removed_after_success():
    bad = RunOutcome(returncode=1, stdout="", stderr="")
    runner = FakeRunner([bad, _ok()])
    _bridge(runner).complete("p", run_id="r", task_id="t")
    cwds = {call[4] for call in runner.calls}
    assert len(runner.calls) == 2 and len(cwds) == 1  # 目录是每次调用一个,不是每次尝试一个
    assert runner.cwd_seen == [(True, []), (True, [])]
    assert not Path(next(iter(cwds))).exists()


def test_the_directory_is_removed_when_the_retries_are_exhausted():
    bad = RunOutcome(returncode=1, stdout="", stderr="")
    runner = FakeRunner([bad, bad, bad])
    with pytest.raises(BridgeError) as exc:
        _bridge(runner).complete("p", run_id="r", task_id="t")
    assert exc.value.category == "T1"
    cwds = {call[4] for call in runner.calls}
    assert len(runner.calls) == 3 and len(cwds) == 1
    assert not Path(next(iter(cwds))).exists()


def test_the_directory_is_removed_when_the_tool_cannot_start():
    runner = FakeRunner([FileNotFoundError(2, "No such file or directory")])
    with pytest.raises(BridgeError) as exc:
        _bridge(runner).complete("p", run_id="r", task_id="t")
    assert exc.value.category == "T2" and exc.value.__context__ is None
    assert runner.cwd_seen == [(True, [])]
    assert not Path(runner.calls[0][4]).exists()


_ENV_TOOL = (
    "import json, os\n"
    "print(json.dumps({'env': sorted(os.environ), 'cwd': os.getcwd(), 'ls': os.listdir('.')}))\n"
)


def test_a_real_subprocess_sees_only_allowlisted_variables_and_an_empty_working_directory(monkeypatch):
    monkeypatch.setenv("FAKE_TOKEN", SECRET)
    monkeypatch.setenv("RISMFORGE_UNLISTED", "must-not-leak")
    b = CliBridge(_cfg(command=[sys.executable, "-c", _ENV_TOOL]), sleep=lambda s: None)  # env 缺省取 os.environ
    seen = json.loads(b.complete("p", run_id="r", task_id="t").text)
    assert "FAKE_TOKEN" in seen["env"]
    assert "RISMFORGE_UNLISTED" not in seen["env"]
    assert seen["ls"] == []
    assert Path(seen["cwd"]).resolve() != Path.cwd().resolve()
    assert not Path(seen["cwd"]).exists()  # 调用结束后目录已删除


def test_default_commands_turn_tools_off_and_keep_sessions_out_of_the_way():
    assert DEFAULT_COMMANDS == {
        "claude": ["claude", "-p", "--model", "{model}", "--tools", "", "--no-session-persistence"],
        "codex": [
            "codex", "exec", "-m", "{model}", "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral",
        ],
    }
    claude, codex = DEFAULT_COMMANDS["claude"], DEFAULT_COMMANDS["codex"]
    assert claude[claude.index("--tools") + 1] == ""  # 关闭全部工具
    assert codex[codex.index("--sandbox") + 1] == "read-only"  # 只读沙箱


def test_default_commands_are_exported_from_the_package():
    assert "DEFAULT_COMMANDS" in inference_bridge.__all__


@pytest.mark.parametrize("tool", ["claude", "codex"])
def test_a_default_command_template_is_filled_and_passed_as_argv(tool):
    cfg = replace(_cfg(), command=DEFAULT_COMMANDS[tool])  # 直接构造配置,不经 load_backend_config
    runner = FakeRunner([_ok()])
    CliBridge(cfg, env={"FAKE_TOKEN": SECRET}, runner=runner, sleep=lambda s: None).complete(
        "p", run_id="r", task_id="t"
    )
    argv = runner.calls[0][0]
    assert argv[0] == tool and "fake-model" in argv and "{model}" not in argv
    assert len(argv) == len(DEFAULT_COMMANDS[tool])

```

在 `tests/test_bridge_packaging.py` 末尾追加:

```python


def test_the_repo_gitignore_covers_the_default_log_dir():
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".rismforge/" in lines
```

- [ ] **Step 2: 运行,确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_bridge_cli.py tests/test_bridge_packaging.py`
Expected: 收集阶段报错 `ImportError: cannot import name 'DEFAULT_COMMANDS' from 'inference_bridge'`。(单跑 `tests/test_bridge_packaging.py` 时是 `1 failed, 2 passed`,失败的是 `.gitignore` 那条。)

- [ ] **Step 3: 写实现**

新建 `src/inference_bridge/cli.py`:

```python
"""命令行形态的桥接(设计 §5.1、R15):子进程调外部工具,提示词走标准输入。

- 参数模板来自 BackendConfig.command(argv 列表,不经 shell);模板里的 "{model}" 换成 model_name,
  其余文本原样传。凭证只经环境变量传给子进程,绝不出现在 argv 里。
- 子进程默认隔离(设计 BR-8):环境变量只有「白名单 ∪ {token_env}」与传入环境的交集,
  工作目录是每次 complete 调用新建、结束即删的空临时目录。DEFAULT_COMMANDS 是两个工具的默认参数模板。
- 超时、非零退出、空输出算 T1,按 retry_policy 指数退避重试;耗尽抛 BridgeError("T1")。
  启动阶段的任何 OSError(不存在、无权限、不是目录、ENOEXEC、E2BIG……)都是确定性的,
  立即抛 BridgeError("T2"),不重试。
- 子进程输出按 UTF-8 解码,非法字节替换成 U+FFFD,不会因外部工具的编码抛 UnicodeDecodeError。
- 请求与响应过 controlplane.config.redact 之后写日志文件;git 树里只留 BigRef 形式的 log_ref。
  日志根目录里放一个内容为 "*" 的 .gitignore,日志不会被任何 git 仓库追踪。
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from controlplane.config import DEFAULT_ENV_ALLOWLIST, BackendConfig, redact, resolve_credential
from controlplane.envelope import BigRef
from controlplane.gitstore import GitStore
from inference_bridge.base import BridgeError, BridgeResult, _check_segment, record_bridge_cost

LOG_SUBDIR = ".rismforge/logs"  # 相对 store.repo_root 的默认日志根目录
_TAIL = 300  # 异常信息里最多带多少字符的 stderr 尾巴(已脱敏)

# 两个工具的默认参数模板(设计 §5.1):claude 关闭全部工具、不落会话文件;codex 只读沙箱、
# 不要求工作目录是 git 仓库、不落会话文件。只是常量:不自动写进任何配置,组合根按需取用。
DEFAULT_COMMANDS: dict[str, list[str]] = {
    "claude": ["claude", "-p", "--model", "{model}", "--tools", "", "--no-session-persistence"],
    "codex": ["codex", "exec", "-m", "{model}", "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral"],
}


@dataclass(frozen=True)
class RunOutcome:
    returncode: int | None  # 超时为 None
    stdout: str
    stderr: str
    timed_out: bool = False


# (argv, 标准输入文本, 超时秒数, 子进程环境, 工作目录) -> RunOutcome。
# 子进程起不来时抛 OSError(FileNotFoundError、PermissionError、ENOEXEC 等)。
Runner = Callable[[list[str], str, float, Mapping[str, str], str], RunOutcome]


def _text(data: str | bytes | None) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data


def subprocess_runner(
    argv: list[str], stdin: str, timeout_s: float, env: Mapping[str, str], cwd: str
) -> RunOutcome:
    """默认 runner:真起子进程,工作目录是 cwd。超时由 subprocess.run 杀掉子进程。"""
    try:
        proc = subprocess.run(
            argv, input=stdin, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout_s,
            env=dict(env), cwd=cwd, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return RunOutcome(returncode=None, stdout=_text(exc.stdout), stderr=_text(exc.stderr), timed_out=True)
    return RunOutcome(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


def _retry_policy(policy: dict) -> tuple[int, float]:
    max_attempts = policy.get("max_attempts", 3)
    backoff = policy.get("backoff_base_s", 1.0)
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
        raise ValueError(f"retry_policy.max_attempts must be an int >= 1, got {max_attempts!r}")
    if isinstance(backoff, bool) or not isinstance(backoff, (int, float)) or backoff < 0:
        raise ValueError(f"retry_policy.backoff_base_s must be a number >= 0, got {backoff!r}")
    return max_attempts, float(backoff)


class CliBridge:
    def __init__(
        self,
        config: BackendConfig,
        *,
        store: GitStore | None = None,
        log_dir: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        runner: Runner = subprocess_runner,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if config.invocation_form != "cli":
            raise ValueError(f"CliBridge needs invocation_form 'cli', got {config.invocation_form!r}")
        if not config.command:
            raise ValueError(f"backend {config.backend!r} has no 'command' template")
        self._config = config
        self._max_attempts, self._backoff = _retry_policy(config.retry_policy)
        self._env = dict(os.environ if env is None else env)
        # 缺凭证在构造时就明确失败(MissingCredentialError,只报变量名);token_env 为 None 时 secret 为 None
        self._secret = resolve_credential(config, self._env)
        self._argv = [a.replace("{model}", config.model_name) for a in config.command]
        # 子进程只拿「白名单 ∪ {token_env}」与完整环境的交集。self._env 仍是完整环境(解析凭证、脱敏要用);
        # env_allowlist 为 None 才用默认白名单,给了空 list 就是"只传 token_env"。
        allowed = set(DEFAULT_ENV_ALLOWLIST if config.env_allowlist is None else config.env_allowlist)
        if config.token_env:
            allowed.add(config.token_env)
        self._child_env = {k: v for k, v in self._env.items() if k in allowed}
        self._store = store
        if log_dir is not None:
            self._log_root: Path | None = Path(log_dir)
        elif store is not None:
            self._log_root = store.repo_root / LOG_SUBDIR
        else:
            self._log_root = None
        self._runner = runner
        self._sleep = sleep

    def _redact(self, text: str) -> str:
        return redact(text, [self._secret])

    def _write_log(self, run_id: str, task_id: str, prompt: str, attempts: list[str]) -> BigRef | None:
        if self._log_root is None:
            return None
        self._log_root.mkdir(parents=True, exist_ok=True)
        ignore = self._log_root / ".gitignore"
        if not ignore.exists():
            ignore.write_text("*\n", encoding="utf-8")
        run_logs = self._log_root / run_id
        run_logs.mkdir(parents=True, exist_ok=True)
        k = 1
        while (run_logs / f"{task_id}-{k}.log").exists():
            k += 1
        path = (run_logs / f"{task_id}-{k}.log").resolve()
        body = "\n".join(
            [
                f"backend: {self._config.backend}",
                f"model: {self._config.model_name}",
                f"argv: {self._redact(repr(self._argv))}",
                f"run_id: {run_id}",
                f"task_id: {task_id}",
                "--- prompt ---",
                self._redact(prompt),
                *attempts,
                "",
            ]
        )
        data = body.encode("utf-8")
        path.write_bytes(data)
        return BigRef(uri=f"file://{path}", sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))

    def complete(self, prompt: str, *, run_id: str, task_id: str) -> BridgeResult:
        _check_segment("run_id", run_id)
        _check_segment("task_id", task_id)
        # 每次调用(不是每次尝试)一个空临时目录:成功、T1 耗尽、T2 都由 with 删掉,一次调用的所有尝试共用它。
        with tempfile.TemporaryDirectory(prefix="rismforge-bridge-") as cwd:
            return self._complete_in(cwd, prompt, run_id, task_id)

    def _complete_in(self, cwd: str, prompt: str, run_id: str, task_id: str) -> BridgeResult:
        attempts_log: list[str] = []
        last = ""
        for attempt in range(1, self._max_attempts + 1):
            not_runnable: str | None = None
            try:
                out = self._runner(list(self._argv), prompt, self._config.timeout_s, self._child_env, cwd)
            except OSError as exc:
                # 只在块内算出脱敏后的原因;出块后再抛,BridgeError 的 __context__ 不会挂着带凭证的原异常
                not_runnable = self._redact(f"cannot run {self._argv[0]!r}: {type(exc).__name__}: {exc}")
            if not_runnable is not None:
                attempts_log.append(f"--- attempt {attempt}: not runnable ---\n{not_runnable}")
                ref = self._write_log(run_id, task_id, prompt, attempts_log)
                raise BridgeError("T2", not_runnable, attempts=attempt, log_ref=ref)
            stdout, stderr = self._redact(out.stdout), self._redact(out.stderr)
            if out.timed_out:
                last = f"timed out after {self._config.timeout_s}s"
            elif out.returncode != 0:
                last = f"exit code {out.returncode}"
            elif not stdout.strip():
                last = "empty output"
            else:
                attempts_log.append(f"--- attempt {attempt}: ok ---\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}")
                ref = self._write_log(run_id, task_id, prompt, attempts_log)
                result = BridgeResult(
                    text=stdout.strip(), input_tokens=None, output_tokens=None,
                    model=self._config.model_name, log_ref=ref,
                )
                record_bridge_cost(self._store, result, run_id=run_id, task_id=task_id, calls=attempt)
                return result
            tail = stderr.strip()[-_TAIL:]
            if tail:
                last = f"{last}; stderr tail: {tail}"
            attempts_log.append(f"--- attempt {attempt}: {last} ---\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}")
            if attempt < self._max_attempts:
                self._sleep(self._backoff * (2 ** (attempt - 1)))
        ref = self._write_log(run_id, task_id, prompt, attempts_log)
        raise BridgeError(
            "T1", f"{self._config.backend}: {self._max_attempts} attempt(s) failed; last: {last}",
            attempts=self._max_attempts, log_ref=ref,
        )
```

把 `src/inference_bridge/__init__.py` 整个替换为:

```python
"""外部推理桥接(闭环集成设计 §5.1,R15)。依赖 controlplane;controlplane 不导入本包。"""
from __future__ import annotations

from inference_bridge.base import (
    BRIDGE_COST_TYPE,
    BridgeError,
    BridgeResult,
    InferenceBridge,
    record_bridge_cost,
)
from inference_bridge.cli import DEFAULT_COMMANDS, CliBridge, RunOutcome, Runner, subprocess_runner
from inference_bridge.scripted import ScriptedBridge, ScriptExhaustedError

__all__ = [
    "BRIDGE_COST_TYPE",
    "DEFAULT_COMMANDS",
    "BridgeError",
    "BridgeResult",
    "CliBridge",
    "InferenceBridge",
    "RunOutcome",
    "Runner",
    "ScriptExhaustedError",
    "ScriptedBridge",
    "record_bridge_cost",
    "subprocess_runner",
]
```

在仓库根目录运行(改 `.gitignore`):

```bash
python3 - <<'PYEOF'
from pathlib import Path

g = Path(".gitignore")
s = g.read_text(encoding="utf-8")
old = "secrets/\n"
assert s.count(old) == 1, s.count(old)
g.write_text(
    s.replace(old, "secrets/\n\n# 外部推理桥接的 L3 日志(已脱敏,仍不进 git 树;设计 §5.1)\n.rismforge/\n"),
    encoding="utf-8",
)
PYEOF
```

- [ ] **Step 4: 运行,确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_bridge_cli.py tests/test_bridge_packaging.py tests/test_bridge_scripted.py`
Expected: `65 passed`(实测约 8 秒,其中超时用例占 0.5 秒)。

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: `B+221 passed`(以 820 为起点为 1041),无警告。

- [ ] **Step 5: 提交**

```bash
git add src/inference_bridge/cli.py
git add src/inference_bridge/__init__.py
git add .gitignore
git add tests/test_bridge_cli.py
git add tests/test_bridge_packaging.py
git commit -m "feat: add CliBridge (stdin prompt, T1 retry with backoff, redacted untracked logs, cost record on success)"
```

---

### Task 27: 全量回归与改动范围检查

**Files:** 无改动。

- [ ] **Step 1: 全量回归**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`
Expected: `B+221 passed`(以 820 为起点为 1041),无警告(实测约 90 至 110 秒)。

- [ ] **Step 2: 改动范围只在文件表内**

Run:

```bash
git diff --stat "$BASE"..HEAD -- . ':(exclude)docs/superpowers'
```

`BASE` 是任务 19 Step 0 记下的起点提交。会话里丢了它又找不回执行记录时,退路是 `HEAD~8`(本计划共八次提交,任务 27 不提交),只在确认这八次提交之间没有夹进别的提交时用。

Expected(顺序为 git 的字母序):

```
 .gitignore                          |   3 +
 pyproject.toml                      |   2 +-
 src/controlplane/config.py          | 183 +++++++++++++-
 src/controlplane/evidence.py        |  37 +++
 src/controlplane/ledger.py          | 137 +++++++++++
 src/controlplane/paths.py           |  90 ++++++-
 src/controlplane/states.py          |   4 +
 src/inference_bridge/__init__.py    |  27 +++
 src/inference_bridge/base.py        |  62 +++++
 src/inference_bridge/cli.py         | 203 ++++++++++++++++
 src/inference_bridge/scripted.py    |  48 ++++
 tests/test_awaiting_confirmation.py |  78 ++++++
 tests/test_bridge_cli.py            | 467 ++++++++++++++++++++++++++++++++++++
 tests/test_bridge_packaging.py      |  36 +++
 tests/test_bridge_scripted.py       |  97 ++++++++
 tests/test_config_backend_cli.py    | 142 +++++++++++
 tests/test_config_envelope.py       | 159 ++++++++++++
 tests/test_evidence.py              |  70 ++++++
 tests/test_ledger.py                | 197 +++++++++++++++
 tests/test_paths.py                 |  64 ++++-
 20 files changed, 2097 insertions(+), 9 deletions(-)
```

`docs/superpowers/` 下只应有设计文档与各计划文档本身。再确认三件事:

```bash
git diff --name-only "$BASE"..HEAD -- cli schemas src/controlplane/reconcile.py src/controlplane/daemon.py src/controlplane/observe.py src/controlplane/gitstore.py src/controlplane/envelope.py tests/test_config.py tests/test_states.py tests/test_observe.py
grep -rn "inference_bridge" src/controlplane || echo "controlplane clean"
git status --short cli
```

Expected:第一条无输出;第二条输出 `controlplane clean`;第三条只列出开工前就在的 ` M cli/package-lock.json`、` M cli/package.json`(本计划没碰它们,也没提交它们)。

- [ ] **Step 3: 新源文件都以 `from __future__ import annotations` 开头**

Run:

```bash
python3 - <<'PYEOF'
import ast
import pathlib

files = [
    "src/controlplane/paths.py", "src/controlplane/states.py", "src/controlplane/ledger.py",
    "src/controlplane/config.py", "src/controlplane/evidence.py",
    "src/inference_bridge/__init__.py", "src/inference_bridge/base.py",
    "src/inference_bridge/scripted.py", "src/inference_bridge/cli.py",
]
bad = []
for f in files:
    body = ast.parse(pathlib.Path(f).read_text(encoding="utf-8")).body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]
    first = body[0] if body else None
    if not (isinstance(first, ast.ImportFrom) and first.module == "__future__"):
        bad.append(f)
print("missing __future__:", bad)
PYEOF
```

Expected: `missing __future__: []`。

本任务不提交(没有改动)。

---

## 完成标准

九个任务做完(八次提交)后:

- `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error` 为 **B+221 通过**、无警告(以 820 为起点为 1041;本计划新增 221 个用例:`test_paths` +20、`test_awaiting_confirmation` 6、`test_ledger` 33、`test_config_backend_cli` 43、`test_config_envelope` 24、`test_evidence` 30、`test_bridge_scripted` 15、`test_bridge_packaging` 3、`test_bridge_cli` 47)。
- 设计 §11 的 18 个路径函数全部存在、名字逐字一致;`SUBDIRS` 末尾是 `recipe_candidates`、`skill_library`、`memory_library`、`agent_rounds`。
- `reconcile.py`、`daemon.py`、`observe.py`、`gitstore.py`、`envelope.py`、`tests/test_config.py` 未改;`awaiting_confirmation` 的指令不产生任何动作、不产生任何提交。
- `record_cost` 写出的记录能被 `observe.cost` 正确打印与汇总;未知值省略键;同一 `(task_id, cost_type)` 只记一次。
- `CliBridge` 的凭证值不出现在 argv、日志、异常信息、git 树里;日志对 git 不可见;git 树里只有 `BigRef`;子进程只拿白名单环境变量与 `token_env`,在每次调用新建、结束即删的空临时目录里运行。
- `controlplane` 不导入 `inference_bridge`;`pyproject.toml` 的 wheel 列表含 `src/inference_bridge`;没有新增第三方依赖;`cli/` 未改。

## 已知限制

1. **外部推理没有 token 数。** `CliBridge` 不解析外部工具输出,`BridgeResult` 的 token 字段恒为 `None`,成本表里 `external_inference` 只有 `calls`,`tokens` 汇总为 0。要有用量,得先确认工具的 JSON 输出格式(设计 §14 第 1 条),再给 `CliBridge` 加一个可选的用量解析器。
2. **日志不在控制面仓库的工作区之外。** 缺省位置是 `<repo_root>/.rismforge/logs`,只是对 git 不可见(自带 `*` 的 `.gitignore`)。这与设计 §5.1 原文"仓库外的日志目录"不同,设计 §5.1 已同步改为与本计划一致。`git add -f` 仍能加进去;`git clean -fdx` 会把日志删掉。要放到仓库外,组合根构造 `CliBridge` 时传 `log_dir`。
3. **日志无轮转、无清理。** 每次 `complete` 一份文件,长期运行会累积。
4. **`log_ref` 的 `file://` 地址是本机绝对路径。** 换机器后 `observe.trace` 会把它标成 `unreachable`,与现有 L3 引用的行为一致。
5. **同步阻塞。** `CliBridge.complete` 在超时和退避期间阻塞调用线程;最坏情况约 `max_attempts × timeout_s + backoff_base_s × (2^(max_attempts-1) - 1)` 秒。调和循环在这段时间不转(设计 §14 第 6 条)。
6. **`record_cost` 先到者为准,同一 `task_id` 的成本静默去重。** 每次 `complete` 必须用不同的 `task_id`,否则成本去重,后几次调用的成本丢失(计划 7 的解析重试若复用同一个 `task_id` 就会少记)。同一任务想用更准确的数字覆盖也做不到。
6a. **人工重置后再失败,失败记录静默不写。** `record_failure` 对已存在的 `(run_id, task_id)` 原样返回路径,不返回"已存在"标志;人把产物状态改回 `pending` 重跑后若再失败,第二份失败记录不会写出,调用方也察觉不到。要留第二份,调用方得换 `task_id`(如 `plan-1-retry2`),或先 `store.exists(failure_record_file(...))` 自己判断。
7. **`record_cost` 与 `write_config` 是读改写,不加锁。** 两个进程同时写同一 `run_trace` 会丢一条记录。当前只有单个调和循环进程写树,与 `GitStore` 现有假设一致。
8. **凭证检查按字段名,且只查键不查值。** 字段名按小写精确比较:`Token`、`API_KEY` 能挡住,但 `access_token`、`anthropic_api_key` 这类带前后缀的键挡不住;列表值与字符串值完全不检查,`command: [..., "--api-key=sk-..."]` 会原样进树。日志脱敏只替换 `token_env` 指向的那一个值;子进程只拿白名单环境变量与 `token_env`,其余凭证进不了子进程,但白名单里的变量(如 `HOME` 的路径、`CODEX_HOME`)若含敏感内容,工具自己打印出来时不会被脱敏。
9. **`subprocess_runner` 超时只杀直接子进程。** 外部工具若自己再派生子进程,孙进程可能残留。
10. **`ScriptedBridge` 不写日志**,`log_ref` 恒为 `None`(除非脚本里给的 `BridgeResult` 自带)。
11. **没有 `backends` 标记的报告一律判 `stub`(失败关闭,设计 DX-12)。** 手写的或旧版编排器写的无标记报告要补上 `backends` 才能驱动闭环(规划与提名);今天的 `run_diagnostic` 报告没有标记,判 `stub`,不可接受。

## 未验证项

1. **真实外部工具没有调用过。** `claude -p`、`codex exec` 的参数模板、是否接受标准输入、退出码语义、输出里是否混有进度信息,都没有真实验证;测试只用了 `python -c` 的假可执行体和注入的 runner。
2. **Windows 未验证。** `file://` 地址的拼法(`file://` + 绝对路径)在 POSIX 下正确;Windows 盘符路径会得到 `file://C:\...`,`observe._reachable` 能否识别没有验证。
3. **超时后的子进程清理、无 shebang 脚本的 ENOEXEC 只在 macOS 上跑过**(`test_a_real_subprocess_timeout_is_t1`、`test_a_real_script_without_a_shebang_is_t2`);Linux 下的行为按 `subprocess.run` 的文档推断应一致,没有实际运行。
4. **累计测试数是在一份仓库拷贝里跑出来的**(按本计划的顺序逐任务执行,以 820 为起点,每个任务后跑全量:840、846、879、922、946、976、993、1041;最后一个数实测,中间数由增量推算)。**绝对通过数依赖执行顺序,本计划只保证增量**(+20、+6、+33、+43、+24、+30、+17、+48、+0)。拷贝不含 `.git` 与 `cli/`,所以第 27 任务里 `git diff "$BASE"..HEAD` 是在拷贝里以基线提交为 `BASE` 模拟的,文件清单与行数来自那次运行;在真仓库里 `docs/superpowers/` 下会多出设计与计划文档(已用 pathspec 排除)。
5. **任务 19、22 的"预期失败数"(21 失败 3 通过、35 失败 8 通过)与任务 20 的 2 失败 4 通过**来自同一份拷贝的实际运行;如果执行者改了测试的参数化列表,数字会变。
6. **默认参数模板与白名单在真实工具上没有验证。** `claude -p --tools ""`、`codex exec --sandbox read-only` 是否真的让工具写不了文件,默认白名单(含 `HOME`、`CODEX_HOME`、`CLAUDE_CONFIG_DIR`)是否足以让两个工具保持登录态,只有真实调用才能确认;测试只用了 `python -c` 的假工具与注入的 runner。
