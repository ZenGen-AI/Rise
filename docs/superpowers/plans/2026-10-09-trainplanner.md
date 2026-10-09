# TrainPlanner:规划上下文、提示渲染、严格解析、决策后端与 plan() Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增顶层包 `trainplanner`,把"一轮训练结束后下一轮做什么"变成一个可测的纯流程:TrainScheduler 装配好 `PlanningContext`,TrainPlanner 用版本化的提示模板渲染出提示,交给可替换的决策后端拿回文本,严格解析成 `PlannerDecision`,不合格就带着原因重新采样,耗尽就明确失败。包内带默认模板 `PLANNER_V1` 和把它写进树的函数。全部测试脱离外部服务。

**Architecture:** 设计 §7.1 的三段分离:上下文打包(`context`、`prompt`)、决策后端(`backends`)、输出解析(`decision`),由 `planner.plan` 串起来。`trainplanner` 只依赖 `controlplane`(信封、GitStore、路径函数)与 `inference_bridge`(桥接协议),不读树里的 run、配方、诊断;那是计划 5 的 `assemble_context` 的事。后端一律返回文本,确定性的 `HeuristicBackend` 也输出 JSON 文本,三种后端走同一条解析路径。

**Tech Stack:** Python 3.11+ / uv / pytest;**无新增第三方依赖**(只用标准库 `json`、`re`、`math`、`copy`、`dataclasses`、`typing`、`collections`)。

**Spec:**
- `docs/superpowers/specs/2026-10-09-closed-loop-integration-design.md` §7.1(本计划的范围)、§4.2(`planner_decision` 载荷)、§11(接口契约)、§12(本计划是第 4 份,任务号从 50 起)
- 前置:计划 1 `2026-10-09-bridge-and-foundations.md` 已合并(`inference_bridge` 包与 `planner_prompt_file`)
- 基线:`main` 在 `0385004` 时全量 **820 通过**、无警告。计划 1(以及若先于本计划落地的计划 2、3)会加测试,所以执行时的起点记作 **B**(任务 50 第 1 步实测)。

**依赖已有产出的名字**

| 来源 | 名字 | 状态 |
|---|---|---|
| `controlplane.envelope` | `Envelope`、`OwnerRef`、`CURRENT_SCHEMA_VERSION` | 已有 |
| `controlplane.gitstore` | `GitStore`(`init`、`exists`、`read_product`、`write_product`、`_git`) | 已有 |
| `controlplane.paths` | `planner_prompt_file(version) -> "control-plane/config/prompts/planner-<version>.yaml"` | **计划 1 产出** |
| `inference_bridge` | `InferenceBridge`(协议,`complete(prompt, *, run_id, task_id) -> BridgeResult`)、`BridgeResult(text, input_tokens, output_tokens, model, log_ref)`、`BridgeError(category, reason="", *, attempts=0, log_ref=None)`(`category` 是第一个位置参数,只能是 `"T1"` / `"T2"`)、`ScriptedBridge(script, *, model="scripted", store=None)`(脚本项可以是 `str`、`BridgeResult` 或 `BridgeError`;用完抛 `ScriptExhaustedError`,它不是 `BridgeError` 的子类) | **计划 1 产出**(任务 25) |

**本计划对计划 1 的最小假设(任务 50 第 1 步逐条检查)**

1. `from controlplane.paths import planner_prompt_file` 可导入,`planner_prompt_file("v1") == "control-plane/config/prompts/planner-v1.yaml"`。
2. `from inference_bridge import BridgeError, BridgeResult, InferenceBridge, ScriptedBridge` 可导入。
3. `BridgeError("T1", "down")` 可构造(`category` 是第一个位置参数,`reason` 第二个),`.category == "T1"`。
4. `ScriptedBridge(["hello"]).complete("p", run_id="run-1", task_id="t").text == "hello"`(脚本是字符串列表,只用第一个位置参数,不传 `model`、`store`)。

本计划的测试**只**用到这四点。需要"桥接抛错"的测试一律用测试文件里自己写的假桥接,不往 `ScriptedBridge` 的脚本里放 `BridgeError`;也没有任何测试把 `ScriptedBridge` 的脚本用完,所以不依赖 `ScriptExhaustedError`。

**本计划的取舍(要改的话改这里,任务按此排)**

1. **契约先行。** 任务 50 把 `PlanningContext` 的精确形状写死,并在包内放一份完整示例(`trainplanner.examples.example_context_dict()`),计划 5 的 `assemble_context` 测试可以直接拿它比对形状。
2. **严格,不猜。** 解析器对缺键、多键、重复键、非有限数、围栏外文字一律拒绝;不补默认值、不截取"看起来像 JSON 的那一段"。
3. **模板缺陷不是模型缺陷。** 渲染失败(`PromptRenderError`)立即上抛,不计入重试;只有解析失败与领域校验违规才带着原因重新采样。
4. **后端异常不归规划器管。** `BridgeError` 以及后端抛的任何异常原样上抛,`plan()` 不重试、不改类别,由计划 5 的处理器决定(按设计 R18:任何 `BridgeError`,无论 T1 还是 T2,都由处理器升级为 T3,决策置 `failed` 并 `record_failure`,不上抛;非桥接的瞬态异常才原样上抛)。
5. **不写树。** 除了模板的安装与读取,`trainplanner` 不写任何产物;`planner_decision` 产物由计划 5 写,本计划只提供 `PlannerResult.record_fields()` 给出其中由规划器负责的四个键。

**已核实的事实(实现者不要再猜)**

- `GitStore.write_product` 在内容逐字节相同时不提交(探测暂存区为空即返回),所以"重复安装同一模板"天然是 no-op;本计划仍显式先比对文本,以便同版本不同文本时拒绝覆盖。
- `GitStore.read_product` 走 `load_product`:迁移 → JSON Schema 校验 → 反序列化。`schemas/envelope.schema.json` 不约束 `type` 与 `payload` 的内容,所以 `type: prompt_template` 不需要改 schema。
- `GitStore.init()` 按 `SUBDIRS` 建目录,`control-plane/config/` 已在其中;`write_product` 会自己 `mkdir(parents=True)`,`config/prompts/` 不用预建。
- 诊断报告的退化标记有三种形状(`src/diagnostic/regression.py`):`social_regression` 带 `dimension`、`drop`、`level`、`track`;`g_probe_failure` 带 `probe`、`score`;`reward_hacking_suspected` 带 `diag_delta`、`reward_delta`。错误画像 `ErrorProfile` 是 `{l2_dimension, error_type_distribution, track}`。本计划的摘要结构据此归一化(见"对外契约")。
- 全仓目前没有代码写 GPU 小时(`MockExecutor` 不占 GPU),`cost_gain` 的成本大多没有记录(设计 §14 第 7 条)。`observe.sum_costs` 把缺失的键按 0 加总,那是记账汇总的口径,不能搬进规划上下文:对规划器来说,0 会被读成"免费"。所以 `CostGainEntry` 的成本字段可以是 `None`,渲染进提示时写成 `"not recorded"`。
- `tests/test_mt_packaging.py` 比对 `src/` 下带 `__init__.py` 的顶层目录与 `pyproject.toml` 的 wheel 列表,两个方向都查;新建 `src/trainplanner/__init__.py` 而不改 `pyproject.toml`,这个测试立刻失败。
- 测试里 `pythonpath = ["src"]`,`tests/` 下没有 `conftest.py`,也没有共享的辅助模块;本计划每个测试文件自带辅助函数。

## Global Constraints

每个任务的要求都隐含包含本节。

- `src/` 下每个源文件以 `from __future__ import annotations` 开头(模块 docstring 之后的第一条语句也算);任务 56 有测试强制检查 `trainplanner` 的每个模块。
- **依赖方向**:`trainplanner` 只导入 `controlplane`、`inference_bridge` 与标准库;`controlplane` 不导入 `trainplanner`(任务 56 有测试)。
- **无新增第三方依赖;凭证绝不进 git 树。** 本计划不读任何凭证;桥接的凭证由计划 1 的 `CliBridge` 处理。
- **读写产物走 `GitStore` + `Envelope`。** 模板的写入用 `GitStore.write_product`,读取用 `GitStore.read_product`,不直接碰文件系统。
- **新增顶层包加进 `pyproject.toml` 的 wheel 包列表**(`tests/test_mt_packaging.py` 强制检查),任务 50 做。
- **脱离外部服务可测**:不需要 GPU、网络、真实模型、真实命令行工具。桥接一律用 `ScriptedBridge` 或测试内的假桥接;用到仓库的测试用 `tmp_path` 隔离。
- **只改本计划文件表里的文件。** 不碰 `src/controlplane`、`src/inference_bridge`(计划 1 的领地)、`schemas/`、`cli/`。
- `git add` 逐个文件,**不要碰** `cli/package.json`、`cli/package-lock.json`;**提交信息不加 `Co-Authored-By` 行**。
- **测试命令**一律是 `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error`(下文简称"全量命令",跑单个文件时在末尾加路径),不得出现警告。
- **测试数**:起点 B(计划 1 落地后实测)。各任务后依次为 **B+48 → B+109 → B+128 → B+151 → B+165 → B+186 → B+189**(各任务增量 +48、+61、+19、+23、+14、+21、+3)。在 `0385004` 加计划 1 的临时桩(与计划 1 代码块逐字一致;计划 1 的测试只带了任务 19 对 `tests/test_paths.py` 的修改)上实测为 840 → 888 → 949 → 968 → 991 → 1005 → 1026 → 1029。

## File Structure

| 文件 | 动作 | 职责 |
|---|---|---|
| `pyproject.toml` | 修改(1 行) | wheel 包列表追加 `"src/trainplanner"` |
| `src/trainplanner/__init__.py` | 新建(5 行) | 包说明,不导出名字 |
| `src/trainplanner/context.py` | 新建(383 行) | `PlanningContext` 及五种子结构,构造时校验,`to_dict` / `from_dict` 严格互逆 |
| `src/trainplanner/examples.py` | 新建(64 行) | `example_context_dict()`:对外契约的完整示例 |
| `src/trainplanner/decision.py` | 新建(205 行) | `PlannerDecision`、`parse_decision`、`DecisionParseError` |
| `src/trainplanner/prompt.py` | 新建(131 行) | 占位符语法、`check_template`、`render_prompt`、`PackedContext` |
| `src/trainplanner/templates.py` | 新建(153 行) | `PLANNER_V1`、`install_template`、`install_default_template`、`load_template` |
| `src/trainplanner/backends.py` | 新建(111 行) | `DecisionBackend` 协议与 `BridgeDecisionBackend`、`HeuristicBackend`、`ScriptedDecisionBackend` |
| `src/trainplanner/planner.py` | 新建(94 行) | `plan`、`PlannerResult`、`PlannerFailedError`、`attempt_task_id` |
| `tests/test_tp_context.py` | 新建(48 个用例) | 契约形状 |
| `tests/test_tp_decision.py` | 新建(61 个用例) | 严格解析 |
| `tests/test_tp_prompt.py` | 新建(19 个用例) | 占位符渲染 |
| `tests/test_tp_templates.py` | 新建(23 个用例) | 默认模板与信封读写 |
| `tests/test_tp_backends.py` | 新建(14 个用例) | 三个后端 |
| `tests/test_tp_planner.py` | 新建(21 个用例) | 重试、预算、异常上抛 |
| `tests/test_tp_boundaries.py` | 新建(3 个用例) | 依赖方向与 `__future__` 头 |

(用例数按 pytest 收集的参数化展开计。)

## 对外契约:PlanningContext 的精确形状

计划 5 的 `assemble_context(store, run_id) -> PlanningContext` 必须产出下面这个形状。`PlanningContext.from_dict(d)` 对缺键、多键、类型不符一律抛 `InvalidContextError`,不补默认值;`to_dict()` 产出的字典再 `from_dict` 回来逐项相等,且是纯 JSON 可序列化的。

**完整示例**(就是 `trainplanner.examples.example_context_dict()` 的返回值):

```python
{
    "run_id": "run-003",
    "candidate": {"id": "cand-run-003", "status": "effective", "reject_reason": None},
    "diagnostic": {
        "l1_scores": {"empathy": 0.71, "pragmatics": 0.64, "tom": 0.58},
        "regression_flags": [
            {"kind": "social_regression", "subject": "pragmatics", "magnitude": 0.06}
        ],
        "error_profiles": [
            {
                "l2_dimension": "tom.false_belief",
                "error_type_distribution": {"literal_reading": 0.5, "perspective_swap": 0.5},
            }
        ],
        "benchmark_version": "bench-v1",
        "grade": "real",
    },
    "recipes": [
        {"id": "recipe-run-001", "recipe_type": "batch", "dimension": "tom",
         "status": "effective", "mean_gain": 0.04, "n_reuse": 2},
        {"id": "recipe-run-002", "recipe_type": "batch", "dimension": "empathy",
         "status": "effective", "mean_gain": None, "n_reuse": 0},
    ],
    "recent_decisions": [
        {"decision_id": "planner-run-002", "next_run_id": "run-003", "target_dimension": "tom",
         "action": "clone", "source_recipe_id": "recipe-run-001",
         "candidate_status": "effective", "gain": 0.03},
    ],
    "cost_gain": [
        {"run_id": "run-002", "target_dimension": "empathy", "gpu_hours": None, "tokens": None, "gain": None},
        {"run_id": "run-003", "target_dimension": "tom", "gpu_hours": 2.0, "tokens": 12000, "gain": 0.03},
    ],
}
```

**逐字段约束**

| 路径 | 类型 | 约束与来源 |
|---|---|---|
| `run_id` | `str` | 非空(纯空白也算空,下同:所有"非空字符串"字段都按 `strip()` 判)。候选到达终态、触发这次规划的源 run |
| `candidate.id` | `str` | 非空。候选 checkpoint 信封的 `id` |
| `candidate.status` | `str` | 只能是 `"effective"` 或 `"rejected"`(R5:只有终态候选触发规划) |
| `candidate.reject_reason` | `str \| None` | 被拒时的原因,没有原因时为 `None`;`effective` 时**必须**为 `None`(构造时强制,计划 5 的 `_candidate` 对非 `rejected` 候选一律传 `None`)。非 `None` 时不能是空串或纯空白 |
| `diagnostic.l1_scores` | `dict[str, float]` | **至少一项**。一级维度名 → 分数(诊断报告里 `level == "l1"` 的 `DimensionScore`)。分数是有限数,不能是布尔(下同:所有数值字段,`int` 一律视为有限,不转 `float` 判,超大整数不会抛 `OverflowError`) |
| `diagnostic.regression_flags` | `list[{kind, subject, magnitude}]` | 每条:`kind: str` 照抄报告;`subject: str` 对 `social_regression` 取 `dimension`、对 `g_probe_failure` 取 `probe`、对 `reward_hacking_suspected` 取 `"reward"`;`magnitude: float` 依次取 `drop`、`score`、`reward_delta − diag_delta`(计划 5 Ruling 12)。空列表表示无退化 |
| `diagnostic.error_profiles` | `list[{l2_dimension, error_type_distribution}]` | 照抄报告的 `ErrorProfile`,去掉 `track`。分布必须是映射(`from_dict` 先查类型,成对列表、整数都抛 `InvalidContextError`,不靠 `dict()` 转换);分布的值是有限数 |
| `diagnostic.benchmark_version` | `str` | 非空。照抄报告 |
| `diagnostic.grade` | `str` | 只能是 `"mock"` 或 `"real"`,即 `controlplane.evidence.diagnostic_grade` 的结果;`"stub"` 永远进不了规划 |
| `recipes[]` | `{id, recipe_type, dimension, status, mean_gain, n_reuse}` | `RecipeView` 去掉 `payload`。`recipe_type` ∈ `batch / interact / agent_rsi`;`status` 非空字符串(不限定取值,由装配方决定放哪些);`mean_gain: float \| None`(`None` 表示没有可比战绩);`n_reuse: int ≥ 0` |
| `recent_decisions[]` | `{decision_id, next_run_id, target_dimension, action, source_recipe_id, candidate_status, gain}` | `decision_id` 是 `planner_decision` 信封的 `id`;`action` ∈ `new / clone / replay`;`source_recipe_id: str \| None`;`candidate_status: str \| None`(后续 run 还没有候选时为 `None`);`gain: float \| None` |
| `cost_gain[]` | `{run_id, target_dimension, gpu_hours, tokens, gain}` | `gpu_hours: float ≥ 0 \| None`、`tokens: int ≥ 0 \| None`。**没有记录就写 `None`,不写 0,也不省略键**;不为 `None` 时照旧校验(非负有限数、非负整数,布尔拒绝)。渲染进提示时 `None` 的成本写成 `"not recorded"`。`gain: float \| None` |

列表的顺序由装配方决定,`trainplanner` 不重排(渲染进提示时保持原序;`HeuristicBackend` 自己排序)。三个列表段可以为空,`l1_scores` 不可以。

**Ruling: 子结构全部是冻结数据类,构造时校验,`from_dict` 严格拒绝缺键与多键** —— 计划 5 是独立撰写的,形状漂移要在第一次构造时就暴露,而不是在提示里静默少一段 —— 判错的代价:计划 5 想加一个上下文字段时,要同时改 `context.py` 与本节(两处,都在 `trainplanner` 里)。

**Ruling: `cost_gain` 的 `gpu_hours`、`tokens` 可空,缺失表示未记录;键始终在场,值为 `None`;渲染进提示时写成 `"not recorded"`** —— 缺失表示未记录,不能读成免费:写 0 会让规划器以为那一轮不花钱(设计 §14 第 7 条)。设计 §14 第 7 条区分两处写法:`run_trace` 的 `cost_records` 省略未记录的键,`PlanningContext.cost_gain` 键必须在场、值为 `None`(本计划取后者),因为 `from_dict` 对缺键一律拒绝(本节第一条 Ruling),省略键就要为这两个字段开例外 —— 判错的代价:计划 5 的 `assemble_context` 必须对未记录的成本显式写 `None`;若 `assemble_context` 省略键,会被 `from_dict` 以 `missing keys` 拒绝,不会静默通过。

**Ruling: 退化标记归一化为 `{kind, subject, magnitude}`** —— 诊断报告的三种标记键名各不相同,规划只需要"什么退化了、退了多少";三种的取法由计划 5 的 Ruling 12 决定(见上表) —— 判错的代价:将来出现第四种标记时,计划 5 要决定它的 `subject` / `magnitude` 取什么,规划器看不到原始键。

**Ruling: `effective` 候选的 `reject_reason` 必须为 `None`,构造时强制** —— 计划 5 的 `_candidate` 只在 `status == "rejected"` 时读载荷里的 `reject_reason`,其余一律传 `None`,所以强制不会误伤正常装配;放过非 `None` 会让提示里出现"有效但带驳回原因"的矛盾输入 —— 判错的代价:若以后要给 `effective` 候选附一句备注,要换一个字段,不能复用 `reject_reason`。

**Ruling: `candidate.status` 只允许终态,`grade` 只允许 `mock` / `real`** —— 设计 R5 与 §3.2 规定只有终态候选、可接受证据才触发规划;在数据层再挡一次,装配方的 bug 不会变成一次真实推理调用 —— 判错的代价:若以后想对 `pending_review` 的候选做"预规划",要放宽这两个枚举。

---
### Task 50: 前置检查、包骨架与规划上下文

**Files:**
- Modify: `pyproject.toml`(wheel 包列表追加一项)
- Create: `src/trainplanner/__init__.py`(5 行)、`src/trainplanner/context.py`(383 行)、`src/trainplanner/examples.py`(64 行)
- Test: `tests/test_tp_context.py`(48 个用例)

**Interfaces:**
- Consumes:无(本任务只用标准库)。第 1 步检查计划 1 的产出是否在场,但本任务的代码还不导入它们。
- Produces(任务 52、54、55 与计划 5 依赖这些名字):
  - `PlanningContext(run_id, candidate, diagnostic, recipes=[], recent_decisions=[], cost_gain=[])`,`to_dict()`、`from_dict(d)`。
  - 子结构 `CandidateSummary`、`DiagnosticSummary`、`RegressionMark`、`ErrorProfileSummary`、`RecipeSummary`、`DecisionOutcome`、`CostGainEntry`,每个都有 `to_dict()` / `from_dict(d)`。
  - `InvalidContextError(ValueError)`。
  - 常量 `CANDIDATE_TERMINAL_STATES`、`PLANNABLE_GRADES`、`RECIPE_TYPES`、`DECISION_ACTIONS`。`DECISION_ACTIONS` 是决策动作集合的唯一定义,任务 51 的 `decision.py` 从这里导入。
  - `trainplanner.examples.example_context_dict() -> dict`(每次返回深拷贝)。

**设计要点:**
- 形状见上文"对外契约"。每个数据类在 `__post_init__` 里校验类型与取值,所以直接构造和 `from_dict` 同样严格。
- `from_dict` 用 `_exact_keys` 先比对键集合;错误信息只拼非空的那一半(只缺键时是 `missing keys [...]`,只多键时是 `unknown keys [...]`,两者都有时用逗号连起来),所以测试可以按具体键名断言;之后才逐字段校验。
- 数值校验对 `int`(非布尔)不调 `math.isfinite`:整数一定有限,而超大整数转 `float` 会抛 `OverflowError`,那不是 `InvalidContextError`,会漏过计划 5 的 `except InvalidContextError`。
- 数值字段拒绝布尔(`True` 是 `int` 的子类)和非有限数(`nan`、`inf`),因为它们会以 `NaN` / `Infinity` 的形式进入提示,而那不是合法 JSON。
- 各子结构把键的顺序记在类属性 `_KEYS` 里。注意它不带类型标注,所以不是数据类字段。

- [ ] **Step 1: 前置检查(计划 1 的产出必须在场)**

先记下本计划的基点,后面任务 56 用它核对改动范围:

```bash
BASE=$(git rev-parse HEAD); echo "BASE=$BASE"
```

预期:打印 `BASE=<40 位提交号>`。**把 `BASE` 写进执行记录**,任务 56 要用;换了 shell 会话就从执行记录里取回来再 `export BASE=...`。然后检查计划 1 的产出:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev python -c "
from controlplane.paths import planner_prompt_file
from inference_bridge import BridgeError, BridgeResult, InferenceBridge, ScriptedBridge
assert planner_prompt_file('v1') == 'control-plane/config/prompts/planner-v1.yaml'
err = BridgeError('T1', 'down')
assert err.category == 'T1'
assert ScriptedBridge(['hello']).complete('p', run_id='run-1', task_id='t').text == 'hello'
print('plan-1 outputs present')
"
```

预期最后一行:`plan-1 outputs present`。**出现 `ImportError`、`AssertionError` 或 `TypeError` 就停下**,不要往下做,并报告缺了哪一项:计划 1 还没落地,或者它的名字、签名与本计划"对计划 1 的最小假设"不一致。不要在本计划里补这些名字。

再跑一次全量,记下起点 B:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error
```

预期:`B passed`,无警告。B 是计划 1(以及已落地的其他计划)之后的数,不一定是 820。

> 验证说明:本计划的代码与测试是在一份临时拷贝里跑通的。那份拷贝基于 `0385004`,补了**临时桩,由计划 1 取代**,桩与计划 1 任务 25、任务 21、任务 19 的代码块逐字一致(按计划 1 修订后的文本:`ScriptedBridge` 与 ledger 都用 `_check_segment` 校验 `run_id`、`task_id`,拒绝空串、含 `/` 或 `\`、`.`、`..`):`src/inference_bridge/base.py`、`scripted.py`、`__init__.py`(`BridgeResult`、`BridgeError`、`InferenceBridge`、`ScriptedBridge`、`ScriptExhaustedError`、`record_bridge_cost`),它们依赖的 `src/controlplane/ledger.py`,以及任务 19 的 `controlplane/paths.py` 脚本;`pyproject.toml` 的 wheel 列表用计划 1 任务 25 的脚本加了 `"src/inference_bridge"`。计划 1 的测试只带了任务 19 对 `tests/test_paths.py` 的修改(否则 `SUBDIRS` 整表断言失败),所以拷贝里 B = 840。本计划所有交给桥接的 `run_id`、`task_id`(`run-003`、`r`、`planner-attempt-N`、`t`、`a`…)都是合法路径段。实施时不要建这些桩。

- [ ] **Step 2: 写失败测试**

新建 `tests/test_tp_context.py`:

```python
import json

import pytest

from trainplanner.context import (
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
from trainplanner.examples import example_context_dict


def test_the_example_round_trips_exactly():
    d = example_context_dict()
    ctx = PlanningContext.from_dict(d)
    assert ctx.to_dict() == d
    assert json.loads(json.dumps(ctx.to_dict())) == d  # 纯 JSON 可序列化


def test_from_dict_builds_the_typed_substructures():
    ctx = PlanningContext.from_dict(example_context_dict())
    assert isinstance(ctx.candidate, CandidateSummary)
    assert isinstance(ctx.diagnostic, DiagnosticSummary)
    assert isinstance(ctx.diagnostic.regression_flags[0], RegressionMark)
    assert isinstance(ctx.diagnostic.error_profiles[0], ErrorProfileSummary)
    assert isinstance(ctx.recipes[0], RecipeSummary)
    assert isinstance(ctx.recent_decisions[0], DecisionOutcome)
    assert isinstance(ctx.cost_gain[0], CostGainEntry)
    assert ctx.diagnostic.l1_scores["tom"] == 0.58


def test_example_context_dict_returns_a_fresh_copy():
    a = example_context_dict()
    a["diagnostic"]["l1_scores"]["tom"] = 0.0
    assert example_context_dict()["diagnostic"]["l1_scores"]["tom"] == 0.58


def test_the_list_sections_default_to_empty():
    d = example_context_dict()
    ctx = PlanningContext(
        run_id="run-1",
        candidate=CandidateSummary.from_dict(d["candidate"]),
        diagnostic=DiagnosticSummary.from_dict(d["diagnostic"]),
    )
    assert ctx.to_dict()["recipes"] == []
    assert ctx.to_dict()["recent_decisions"] == []
    assert ctx.to_dict()["cost_gain"] == []


@pytest.mark.parametrize(
    "path",
    [
        ("run_id",),
        ("candidate", "reject_reason"),
        ("diagnostic", "grade"),
        ("diagnostic", "benchmark_version"),
        ("recipes", 0, "n_reuse"),
        ("recent_decisions", 0, "gain"),
        ("cost_gain", 1, "tokens"),
        ("diagnostic", "regression_flags", 0, "magnitude"),
        ("diagnostic", "error_profiles", 0, "l2_dimension"),
    ],
)
def test_a_missing_key_is_rejected_not_defaulted(path):
    d = example_context_dict()
    node = d
    for p in path[:-1]:
        node = node[p]
    del node[path[-1]]
    with pytest.raises(InvalidContextError, match=rf"missing keys \['{path[-1]}'\]$") as info:
        PlanningContext.from_dict(d)
    assert "unknown keys" not in str(info.value)  # 只拼非空的那一半


@pytest.mark.parametrize(
    "path",
    [(), ("candidate",), ("diagnostic",), ("recipes", 0), ("cost_gain", 0)],
)
def test_an_unknown_key_is_rejected(path):
    d = example_context_dict()
    node = d
    for p in path:
        node = node[p]
    node["surprise"] = 1
    with pytest.raises(InvalidContextError, match=r"unknown keys \['surprise'\]$") as info:
        PlanningContext.from_dict(d)
    assert "missing keys" not in str(info.value)


def test_missing_and_unknown_keys_are_reported_together():
    d = example_context_dict()
    del d["candidate"]["status"]
    d["candidate"]["surprise"] = 1
    with pytest.raises(
        InvalidContextError, match=r"^candidate: missing keys \['status'\], unknown keys \['surprise'\]$"
    ):
        PlanningContext.from_dict(d)


@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda d: d["candidate"].update(status="pending_review"), "candidate.status"),
        (lambda d: d["diagnostic"].update(grade="stub"), "diagnostic.grade"),
        (lambda d: d["diagnostic"].update(l1_scores={}), "at least one dimension"),
        (lambda d: d["diagnostic"]["l1_scores"].update(tom="high"), "finite number"),
        (lambda d: d["diagnostic"]["l1_scores"].update(tom=True), "finite number"),
        (lambda d: d["diagnostic"]["l1_scores"].update(tom=float("nan")), "finite number"),
        (lambda d: d["recipes"][0].update(recipe_type="weird"), "recipe.recipe_type"),
        (lambda d: d["recipes"][0].update(n_reuse=-1), "non-negative integer"),
        (lambda d: d["recipes"][0].update(mean_gain="0.1"), "finite number"),
        (lambda d: d["recent_decisions"][0].update(action="retry"), "recent_decision.action"),
        (lambda d: d["cost_gain"][0].update(tokens=1.5), "non-negative integer"),
        (lambda d: d.update(run_id=""), "non-empty string"),
        (lambda d: d.update(run_id="   "), "non-empty string"),
        (lambda d: d.update(recipes={}), "expected a list"),
        (lambda d: d.update(candidate=[]), "expected a mapping"),
        (lambda d: d["candidate"].update(reject_reason="looked fine"), "must be None when status is 'effective'"),
        (
            lambda d: d["diagnostic"]["error_profiles"][0].update(error_type_distribution=[["literal_reading", 1.0]]),
            "error_profile.error_type_distribution: expected a mapping, got list",
        ),
        (
            lambda d: d["diagnostic"]["error_profiles"][0].update(error_type_distribution=5),
            "error_profile.error_type_distribution: expected a mapping, got int",
        ),
    ],
)
def test_wrong_types_and_values_are_rejected(mutate, match):
    d = example_context_dict()
    mutate(d)
    with pytest.raises(InvalidContextError, match=match):
        PlanningContext.from_dict(d)


def test_direct_construction_is_validated_too():
    with pytest.raises(InvalidContextError, match="candidate.status"):
        CandidateSummary(id="cand-run-1", status="approved", reject_reason=None)


def test_unrecorded_costs_stay_none_and_keep_their_keys():
    d = example_context_dict()
    entry = PlanningContext.from_dict(d).cost_gain[0]
    assert (entry.gpu_hours, entry.tokens) == (None, None)
    assert PlanningContext.from_dict(d).to_dict()["cost_gain"][0] == {
        "run_id": "run-002", "target_dimension": "empathy",
        "gpu_hours": None, "tokens": None, "gain": None,
    }


@pytest.mark.parametrize(
    "field, value, match",
    [
        ("gpu_hours", True, "finite number"),
        ("gpu_hours", float("inf"), "finite number"),
        ("gpu_hours", "1.5", "finite number"),
        ("gpu_hours", -0.5, "non-negative number"),
        ("tokens", -1, "non-negative integer"),
        ("tokens", True, "non-negative integer"),
    ],
)
def test_a_recorded_cost_is_still_validated(field, value, match):
    d = example_context_dict()
    d["cost_gain"][1][field] = value
    with pytest.raises(InvalidContextError, match=match):
        PlanningContext.from_dict(d)


def test_a_huge_integer_is_a_finite_number():
    d = example_context_dict()
    d["diagnostic"]["l1_scores"]["tom"] = 10**400  # 转 float 会 OverflowError;整数不该走那条路
    assert PlanningContext.from_dict(d).diagnostic.l1_scores["tom"] == 10**400


def test_a_rejected_candidate_carries_its_reason():
    d = example_context_dict()
    d["candidate"] = {"id": "cand-run-003", "status": "rejected", "reject_reason": "tom dropped"}
    assert PlanningContext.from_dict(d).candidate.reject_reason == "tom dropped"


def test_the_context_is_frozen():
    ctx = PlanningContext.from_dict(example_context_dict())
    with pytest.raises(AttributeError):
        ctx.run_id = "run-9"
```

- [ ] **Step 3: 跑测试,确认失败**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_tp_context.py
```

预期:收集阶段报错,`ModuleNotFoundError: No module named 'trainplanner'`,末行 `1 error in …`。

- [ ] **Step 4: 实现**

新建 `src/trainplanner/__init__.py`:

```python
"""TrainPlanner:上下文打包、决策后端、输出解析三者分离(设计 §7.1)。

只依赖 controlplane 与 inference_bridge;不读树里的 run、配方或诊断,那是 TrainScheduler 的事。
"""
from __future__ import annotations
```

新建 `src/trainplanner/context.py`:

```python
"""规划上下文:TrainScheduler 的 assemble_context 产出、TrainPlanner 消费的纯数据。

字段名与类型是计划 5 的契约。所有结构都是冻结数据类,构造时校验类型;to_dict / from_dict
互逆,from_dict 对缺键与多键一律拒绝,不做任何补默认值。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

CANDIDATE_TERMINAL_STATES = ("effective", "rejected")
PLANNABLE_GRADES = ("mock", "real")
# 与 recipestore 的配方类型集合(计划 3 的 RECIPE_TYPES,validate_reproducibility 按它分派)必须一致,
# 改一处要同步另一处。两个包互不导入,所以各留一份字面量。
RECIPE_TYPES = ("batch", "interact", "agent_rsi")
# 决策动作集合的唯一定义。decision.parse_decision 从这里导入,不另写字面量。
DECISION_ACTIONS = ("new", "clone", "replay")


class InvalidContextError(ValueError):
    """规划上下文的形状不对:缺键、多键、类型不符或取值越界。"""


def _exact_keys(d: object, keys: tuple[str, ...], where: str) -> dict:
    if not isinstance(d, dict):
        raise InvalidContextError(f"{where}: expected a mapping, got {type(d).__name__}")
    missing = sorted(set(keys) - set(d))
    unknown = sorted(str(k) for k in set(d) - set(keys))
    parts = []
    if missing:
        parts.append(f"missing keys {missing}")
    if unknown:
        parts.append(f"unknown keys {unknown}")
    if parts:
        raise InvalidContextError(f"{where}: " + ", ".join(parts))
    return d


def _check_str(v: object, where: str) -> None:
    if not isinstance(v, str) or not v.strip():
        raise InvalidContextError(f"{where}: expected a non-empty string, got {v!r}")


def _check_opt_str(v: object, where: str) -> None:
    if v is not None:
        _check_str(v, where)


def _check_num(v: object, where: str) -> None:
    # 整数一定有限,不进 isfinite:超大整数(如 10**400)转 float 会抛 OverflowError
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise InvalidContextError(f"{where}: expected a finite number, got {v!r}")
    if isinstance(v, float) and not math.isfinite(v):
        raise InvalidContextError(f"{where}: expected a finite number, got {v!r}")


def _check_opt_num(v: object, where: str) -> None:
    if v is not None:
        _check_num(v, where)


def _check_opt_nonneg_num(v: object, where: str) -> None:
    if v is not None:
        _check_num(v, where)
        if v < 0:
            raise InvalidContextError(f"{where}: expected a non-negative number, got {v!r}")


def _check_count(v: object, where: str) -> None:
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise InvalidContextError(f"{where}: expected a non-negative integer, got {v!r}")


def _check_opt_count(v: object, where: str) -> None:
    if v is not None:
        _check_count(v, where)


def _check_choice(v: object, choices: tuple[str, ...], where: str) -> None:
    if v not in choices:
        raise InvalidContextError(f"{where}: expected one of {list(choices)}, got {v!r}")


def _check_list_of(v: object, cls: type, where: str) -> None:
    if not isinstance(v, list) or not all(isinstance(x, cls) for x in v):
        raise InvalidContextError(f"{where}: expected a list of {cls.__name__}")


def _check_score_map(v: object, where: str) -> None:
    if not isinstance(v, dict):
        raise InvalidContextError(f"{where}: expected a mapping, got {type(v).__name__}")
    for k, s in v.items():
        _check_str(k, f"{where} key")
        _check_num(s, f"{where}[{k}]")


def _list_field(d: dict, key: str, where: str) -> list:
    v = d[key]
    if not isinstance(v, list):
        raise InvalidContextError(f"{where}.{key}: expected a list, got {type(v).__name__}")
    return v


@dataclass(frozen=True)
class CandidateSummary:
    """源 run 的候选 checkpoint。只有到达终态的候选才会触发规划(R5)。"""

    id: str
    status: str  # "effective" | "rejected"
    reject_reason: str | None  # effective 时必须为 None

    _KEYS = ("id", "status", "reject_reason")

    def __post_init__(self) -> None:
        _check_str(self.id, "candidate.id")
        _check_choice(self.status, CANDIDATE_TERMINAL_STATES, "candidate.status")
        _check_opt_str(self.reject_reason, "candidate.reject_reason")
        if self.status == "effective" and self.reject_reason is not None:
            raise InvalidContextError("candidate.reject_reason: must be None when status is 'effective'")

    def to_dict(self) -> dict:
        return {"id": self.id, "status": self.status, "reject_reason": self.reject_reason}

    @classmethod
    def from_dict(cls, d: dict) -> CandidateSummary:
        _exact_keys(d, cls._KEYS, "candidate")
        return cls(id=d["id"], status=d["status"], reject_reason=d["reject_reason"])


@dataclass(frozen=True)
class RegressionMark:
    """一条退化标记的摘要。kind 照抄诊断报告;subject 是维度(social_regression)、探针名
    (g_probe_failure)或 "reward"(reward_hacking_suspected);magnitude 是下降量、探针分数或 reward_delta - diag_delta。"""

    kind: str
    subject: str
    magnitude: float

    _KEYS = ("kind", "subject", "magnitude")

    def __post_init__(self) -> None:
        _check_str(self.kind, "regression_flag.kind")
        _check_str(self.subject, "regression_flag.subject")
        _check_num(self.magnitude, "regression_flag.magnitude")

    def to_dict(self) -> dict:
        return {"kind": self.kind, "subject": self.subject, "magnitude": self.magnitude}

    @classmethod
    def from_dict(cls, d: dict) -> RegressionMark:
        _exact_keys(d, cls._KEYS, "regression_flag")
        return cls(kind=d["kind"], subject=d["subject"], magnitude=d["magnitude"])


@dataclass(frozen=True)
class ErrorProfileSummary:
    """一个二级维度的错误类型分布,照抄诊断报告的 ErrorProfile(去掉 track)。"""

    l2_dimension: str
    error_type_distribution: dict[str, float]

    _KEYS = ("l2_dimension", "error_type_distribution")

    def __post_init__(self) -> None:
        _check_str(self.l2_dimension, "error_profile.l2_dimension")
        _check_score_map(self.error_type_distribution, "error_profile.error_type_distribution")

    def to_dict(self) -> dict:
        return {
            "l2_dimension": self.l2_dimension,
            "error_type_distribution": dict(self.error_type_distribution),
        }

    @classmethod
    def from_dict(cls, d: dict) -> ErrorProfileSummary:
        _exact_keys(d, cls._KEYS, "error_profile")
        dist = d["error_type_distribution"]
        if not isinstance(dist, dict):
            raise InvalidContextError(
                f"error_profile.error_type_distribution: expected a mapping, got {type(dist).__name__}"
            )
        return cls(
            l2_dimension=d["l2_dimension"],
            error_type_distribution=dict(d["error_type_distribution"]),
        )


@dataclass(frozen=True)
class DiagnosticSummary:
    """源 run 诊断报告里规划用得上的部分。l1_scores 至少一项:没有一级分数就无从规划。"""

    l1_scores: dict[str, float]
    regression_flags: list[RegressionMark]
    error_profiles: list[ErrorProfileSummary]
    benchmark_version: str
    grade: str  # "mock" | "real";"stub" 永远不是可接受证据,进不了规划

    _KEYS = ("l1_scores", "regression_flags", "error_profiles", "benchmark_version", "grade")

    def __post_init__(self) -> None:
        _check_score_map(self.l1_scores, "diagnostic.l1_scores")
        if not self.l1_scores:
            raise InvalidContextError("diagnostic.l1_scores: must name at least one dimension")
        _check_list_of(self.regression_flags, RegressionMark, "diagnostic.regression_flags")
        _check_list_of(self.error_profiles, ErrorProfileSummary, "diagnostic.error_profiles")
        _check_str(self.benchmark_version, "diagnostic.benchmark_version")
        _check_choice(self.grade, PLANNABLE_GRADES, "diagnostic.grade")

    def to_dict(self) -> dict:
        return {
            "l1_scores": dict(self.l1_scores),
            "regression_flags": [f.to_dict() for f in self.regression_flags],
            "error_profiles": [p.to_dict() for p in self.error_profiles],
            "benchmark_version": self.benchmark_version,
            "grade": self.grade,
        }

    @classmethod
    def from_dict(cls, d: dict) -> DiagnosticSummary:
        _exact_keys(d, cls._KEYS, "diagnostic")
        if not isinstance(d["l1_scores"], dict):
            raise InvalidContextError("diagnostic.l1_scores: expected a mapping")
        return cls(
            l1_scores=dict(d["l1_scores"]),
            regression_flags=[
                RegressionMark.from_dict(x) for x in _list_field(d, "regression_flags", "diagnostic")
            ],
            error_profiles=[
                ErrorProfileSummary.from_dict(x)
                for x in _list_field(d, "error_profiles", "diagnostic")
            ],
            benchmark_version=d["benchmark_version"],
            grade=d["grade"],
        )


@dataclass(frozen=True)
class RecipeSummary:
    """RecipeView 去掉 payload 的摘要。mean_gain 为 None 表示还没有可比的战绩。"""

    id: str
    recipe_type: str  # "batch" | "interact" | "agent_rsi"
    dimension: str
    status: str
    mean_gain: float | None
    n_reuse: int

    _KEYS = ("id", "recipe_type", "dimension", "status", "mean_gain", "n_reuse")

    def __post_init__(self) -> None:
        _check_str(self.id, "recipe.id")
        _check_choice(self.recipe_type, RECIPE_TYPES, "recipe.recipe_type")
        _check_str(self.dimension, "recipe.dimension")
        _check_str(self.status, "recipe.status")
        _check_opt_num(self.mean_gain, "recipe.mean_gain")
        _check_count(self.n_reuse, "recipe.n_reuse")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "recipe_type": self.recipe_type,
            "dimension": self.dimension,
            "status": self.status,
            "mean_gain": self.mean_gain,
            "n_reuse": self.n_reuse,
        }

    @classmethod
    def from_dict(cls, d: dict) -> RecipeSummary:
        _exact_keys(d, cls._KEYS, "recipe")
        return cls(**{k: d[k] for k in cls._KEYS})


@dataclass(frozen=True)
class DecisionOutcome:
    """一条历史决策与它产生的那一轮的结果。candidate_status 为 None 表示那一轮还没有候选。"""

    decision_id: str
    next_run_id: str
    target_dimension: str
    action: str  # "new" | "clone" | "replay"
    source_recipe_id: str | None
    candidate_status: str | None
    gain: float | None

    _KEYS = (
        "decision_id", "next_run_id", "target_dimension", "action",
        "source_recipe_id", "candidate_status", "gain",
    )

    def __post_init__(self) -> None:
        _check_str(self.decision_id, "recent_decision.decision_id")
        _check_str(self.next_run_id, "recent_decision.next_run_id")
        _check_str(self.target_dimension, "recent_decision.target_dimension")
        _check_choice(self.action, DECISION_ACTIONS, "recent_decision.action")
        _check_opt_str(self.source_recipe_id, "recent_decision.source_recipe_id")
        _check_opt_str(self.candidate_status, "recent_decision.candidate_status")
        _check_opt_num(self.gain, "recent_decision.gain")

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self._KEYS}

    @classmethod
    def from_dict(cls, d: dict) -> DecisionOutcome:
        _exact_keys(d, cls._KEYS, "recent_decision")
        return cls(**{k: d[k] for k in cls._KEYS})


@dataclass(frozen=True)
class CostGainEntry:
    """近 K 轮里一轮的成本与增益。成本为 None 表示没有记录,不是 0:没记录不能读成免费。
    键始终在场,值可以是 None;渲染进提示时写成 "not recorded"。"""

    run_id: str
    target_dimension: str
    gpu_hours: float | None
    tokens: int | None
    gain: float | None

    _KEYS = ("run_id", "target_dimension", "gpu_hours", "tokens", "gain")

    def __post_init__(self) -> None:
        _check_str(self.run_id, "cost_gain.run_id")
        _check_str(self.target_dimension, "cost_gain.target_dimension")
        _check_opt_nonneg_num(self.gpu_hours, "cost_gain.gpu_hours")
        _check_opt_count(self.tokens, "cost_gain.tokens")
        _check_opt_num(self.gain, "cost_gain.gain")

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self._KEYS}

    @classmethod
    def from_dict(cls, d: dict) -> CostGainEntry:
        _exact_keys(d, cls._KEYS, "cost_gain")
        return cls(**{k: d[k] for k in cls._KEYS})


@dataclass(frozen=True)
class PlanningContext:
    """一次规划的全部输入。run_id 是候选到达终态、触发这次规划的源 run。"""

    run_id: str
    candidate: CandidateSummary
    diagnostic: DiagnosticSummary
    recipes: list[RecipeSummary] = field(default_factory=list)
    recent_decisions: list[DecisionOutcome] = field(default_factory=list)
    cost_gain: list[CostGainEntry] = field(default_factory=list)

    _KEYS = ("run_id", "candidate", "diagnostic", "recipes", "recent_decisions", "cost_gain")

    def __post_init__(self) -> None:
        _check_str(self.run_id, "run_id")
        if not isinstance(self.candidate, CandidateSummary):
            raise InvalidContextError("candidate: expected a CandidateSummary")
        if not isinstance(self.diagnostic, DiagnosticSummary):
            raise InvalidContextError("diagnostic: expected a DiagnosticSummary")
        _check_list_of(self.recipes, RecipeSummary, "recipes")
        _check_list_of(self.recent_decisions, DecisionOutcome, "recent_decisions")
        _check_list_of(self.cost_gain, CostGainEntry, "cost_gain")

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "candidate": self.candidate.to_dict(),
            "diagnostic": self.diagnostic.to_dict(),
            "recipes": [r.to_dict() for r in self.recipes],
            "recent_decisions": [r.to_dict() for r in self.recent_decisions],
            "cost_gain": [c.to_dict() for c in self.cost_gain],
        }

    @classmethod
    def from_dict(cls, d: dict) -> PlanningContext:
        _exact_keys(d, cls._KEYS, "context")
        return cls(
            run_id=d["run_id"],
            candidate=CandidateSummary.from_dict(d["candidate"]),
            diagnostic=DiagnosticSummary.from_dict(d["diagnostic"]),
            recipes=[RecipeSummary.from_dict(x) for x in _list_field(d, "recipes", "context")],
            recent_decisions=[
                DecisionOutcome.from_dict(x) for x in _list_field(d, "recent_decisions", "context")
            ],
            cost_gain=[CostGainEntry.from_dict(x) for x in _list_field(d, "cost_gain", "context")],
        )
```

新建 `src/trainplanner/examples.py`:

```python
"""对外契约的样例:一份完整、合法的 PlanningContext 字典。

测试与计划 5(TrainScheduler)共用它:assemble_context 的产出必须能被
PlanningContext.from_dict 读回,形状与这里逐键一致。每次调用返回新的深拷贝。
"""
from __future__ import annotations

import copy

_EXAMPLE_CONTEXT = {
    "run_id": "run-003",
    "candidate": {"id": "cand-run-003", "status": "effective", "reject_reason": None},
    "diagnostic": {
        "l1_scores": {"empathy": 0.71, "pragmatics": 0.64, "tom": 0.58},
        "regression_flags": [
            {"kind": "social_regression", "subject": "pragmatics", "magnitude": 0.06}
        ],
        "error_profiles": [
            {
                "l2_dimension": "tom.false_belief",
                "error_type_distribution": {"literal_reading": 0.5, "perspective_swap": 0.5},
            }
        ],
        "benchmark_version": "bench-v1",
        "grade": "real",
    },
    "recipes": [
        {
            "id": "recipe-run-001",
            "recipe_type": "batch",
            "dimension": "tom",
            "status": "effective",
            "mean_gain": 0.04,
            "n_reuse": 2,
        },
        {
            "id": "recipe-run-002",
            "recipe_type": "batch",
            "dimension": "empathy",
            "status": "effective",
            "mean_gain": None,
            "n_reuse": 0,
        },
    ],
    "recent_decisions": [
        {
            "decision_id": "planner-run-002",
            "next_run_id": "run-003",
            "target_dimension": "tom",
            "action": "clone",
            "source_recipe_id": "recipe-run-001",
            "candidate_status": "effective",
            "gain": 0.03,
        }
    ],
    "cost_gain": [
        {"run_id": "run-002", "target_dimension": "empathy", "gpu_hours": None, "tokens": None, "gain": None},
        {"run_id": "run-003", "target_dimension": "tom", "gpu_hours": 2.0, "tokens": 12000, "gain": 0.03},
    ],
}


def example_context_dict() -> dict:
    return copy.deepcopy(_EXAMPLE_CONTEXT)
```

- [ ] **Step 5: 跑测试,确认打包检查先失败**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_tp_context.py tests/test_mt_packaging.py
```

预期:`1 failed, 49 passed`,失败的是 `test_every_top_level_src_package_is_in_the_wheel_list`,信息为 `AssertionError: missing from [tool.hatch...wheel].packages: ['src/trainplanner']`。

- [ ] **Step 6: 把包登记进 wheel 列表**

用下面的脚本在 `packages = [...]` 那一行末尾追加 `"src/trainplanner"`(不假设列表里现在有几项,计划 1 会加 `"src/inference_bridge"`)。脚本是幂等的:列表里已有 `"src/trainplanner"`(重复运行,或别的计划先追加过)时什么都不改,打印 `already listed`:

```bash
uv run python - <<'EOF'
import re
from pathlib import Path

p = Path("pyproject.toml")
s = p.read_text(encoding="utf-8")
m = re.search(r'^packages = \[(.*)\]$', s, re.M)
assert m, "wheel packages line not found"
items = [x.strip() for x in m.group(1).split(",") if x.strip()]
if '"src/trainplanner"' in items:
    print("already listed")
else:
    items.append('"src/trainplanner"')
    s = s[: m.start()] + "packages = [" + ", ".join(items) + "]" + s[m.end():]
    p.write_text(s, encoding="utf-8")
print(re.search(r'^packages = .*$', s, re.M).group(0))
EOF
```

预期输出:最后一行是当前的 `packages = [...]` 行,以 `"src/trainplanner"]` 结尾(前面有哪些包取决于已落地的计划,不在此列出)。重复运行时先打印 `already listed`,再打印同一行,正常退出。

- [ ] **Step 7: 跑测试,确认通过**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_tp_context.py tests/test_mt_packaging.py
```

预期:`50 passed`。然后跑全量:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error
```

预期:`B+48 passed`,无警告(临时拷贝里实测 `888 passed`)。

- [ ] **Step 8: 提交**

```bash
git add pyproject.toml
git add src/trainplanner/__init__.py
git add src/trainplanner/context.py
git add src/trainplanner/examples.py
git add tests/test_tp_context.py
git commit -m "feat: add the trainplanner package and the PlanningContext contract"
```

---

### Task 51: 规划决策与严格解析

**Files:**
- Create: `src/trainplanner/decision.py`(205 行)
- Test: `tests/test_tp_decision.py`(61 个用例)

**Interfaces:**
- Consumes:标准库 `json`、`re`、`math`;任务 50 的 `trainplanner.context.DECISION_ACTIONS`(决策动作集合的唯一定义)。
- Produces(任务 54、55 与计划 5 依赖):
  - `PlannerDecision(target_dimension, action, mode, source_recipe_id, perturbation, data_synthesis_hint, rationale)`(冻结,七个字段都没有默认值),`to_dict()`(七键,顺序同 `DECISION_KEYS`)、`decision_payload()`(去掉 `rationale` 的六键,即设计 §4.2 `payload.decision` 的形状)、`from_dict(d)`(与 `parse_decision` 同一套规则)。
  - `parse_decision(text: str) -> PlannerDecision`。
  - `DecisionParseError(ValueError)`,属性 `reason`,`str(err) == err.reason`。
  - 常量 `MODES = ("batch", "interact")`、`DECISION_KEYS`。动作集合不在本模块重复定义,用 `trainplanner.context.DECISION_ACTIONS`。

**设计要点:**
- **输入必须是单个 JSON 对象。** 去掉首尾空白后,要么整段就是 JSON,要么整段恰好是一个代码围栏。`json.loads` 本身拒绝前后多余的文字和两个对象相连,所以"截取第一个对象"这种宽容做法不存在。
- **重复键与非有限数拒绝。** `object_pairs_hook` 检查重复键(否则 `json.loads` 静默保留后一个);`parse_constant` 拒绝 `NaN`、`Infinity`。
- **`json.loads` 的一切失败都变成 `DecisionParseError`。** 钩子抛的 `DecisionParseError` 原样上抛;`JSONDecodeError` 照旧;超过 4300 位的整数让 `json.loads` 抛普通 `ValueError`,深层嵌套 `[[[…` 在 3.11–3.13 抛 `RecursionError`(3.14 改成 `JSONDecodeError`),这两种也转成 `DecisionParseError`。否则它们会漏出只捕 `DecisionParseError` 的 `plan()`,计划 5 的决策卡在 `pending`,每个 tick 重新调用外部推理。
- **扰动值的标量判断不对 `int` 调 `math.isfinite`**(超大整数会 `OverflowError`);只有 `float` 要查有限。
- **回显截断。** 错误信息里回显模型给的值(`action`、`mode`、扰动的键与值)都经 `_clip(value, n=80)`:`repr` 超过 80 个字符就截断并加 `…`。这些原因会拼进重试提示、写进失败记录,不设上限就可能把一大段模型输出原样带过去。
- **规则的检查顺序**:先查是否是对象 → 缺键 → 未知键 → 逐字段类型 → 跨字段规则。每一步给一句可读原因,`plan()` 原样拼进重试提示。
- `PlannerDecision` 直接构造不校验,这是刻意的:从模型来的数据只走 `parse_decision` / `from_dict` 两个入口。

**Ruling: 允许恰好一个 markdown 代码围栏包住整段输出,开头只能是 ```` ``` ```` 或 ```` ```json ````(其后可有空格),围栏外只能是空白;别的语言标签、围栏前后的文字、两个围栏都拒绝** —— 命令行模型常常习惯性加围栏,拒掉它只会白白耗一次重试;但围栏外的说明文字说明模型没按指令"只输出一个对象",接受它就是宽容猜测 —— 判错的代价:某个模型稳定输出 ```` ```JSON ```` 或带前言时,每轮都要多耗一次重试,或者直到改模板之前一直失败。

**Ruling: 病态 JSON(超长整数、深层嵌套)算解析失败,进重试,不当成程序错误上抛** —— 它们来自模型输出,和"不是 JSON"同一性质;上抛会让计划 5 把它当非桥接异常、决策留在 `pending`,下个 tick 再问一次外部推理,无限循环 —— 判错的代价:如果某个 Python 版本对别的病态输入抛出第三种异常(既不是 `ValueError` 也不是 `RecursionError`),它仍会漏出 `plan()`;目前已知的只有这两种。

**Ruling: 七个键全部必填,`mode` 与 `source_recipe_id` 要显式写 `null`,`perturbation` 要显式写 `{}`** —— 设计说"字段与 `PlannerDecision` 同名"且"不做宽容猜测";缺键补默认值就是猜 —— 判错的代价:模型漏写一个 `null` 键也要重试一次。

**Ruling: 在设计的四条规则之外再加三条:`new` 不能带 `source_recipe_id` 与 `perturbation`;`clone` 与 `replay` 的 `mode` 必须为 `null`;`perturbation` 的值只能是字符串、有限数或布尔** —— 这些组合在计划 5 的 `resolve_action` 里要么没有意义(`new` 用默认配置,不看配方),要么会与配方自带的模式冲突;扰动是对训练配置标量键的覆盖,嵌套结构没法套用 —— 判错的代价:如果以后要让 `new` 以某个配方为起点、或扰动嵌套键,要放宽 `_decision_from_obj`,并同步改 `PLANNER_V1` 的说明。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_tp_decision.py`:

```python
import json

import pytest

from trainplanner.decision import DECISION_KEYS, DecisionParseError, PlannerDecision, parse_decision


def _new(**over):
    d = {
        "target_dimension": "tom",
        "action": "new",
        "mode": "batch",
        "source_recipe_id": None,
        "perturbation": {},
        "data_synthesis_hint": "false-belief dialogues",
        "rationale": "tom is the weakest dimension",
    }
    d.update(over)
    return d


def _clone(**over):
    d = _new(action="clone", mode=None, source_recipe_id="recipe-run-001",
             perturbation={"grpo_config.learning_rate": 5e-6})
    d.update(over)
    return d


def _replay(**over):
    d = _new(action="replay", mode=None, source_recipe_id="recipe-run-001")
    d.update(over)
    return d


def _text(d):
    return json.dumps(d)


def test_a_valid_new_decision_parses():
    dec = parse_decision(_text(_new()))
    assert dec == PlannerDecision(
        target_dimension="tom", action="new", mode="batch", source_recipe_id=None,
        perturbation={}, data_synthesis_hint="false-belief dialogues",
        rationale="tom is the weakest dimension",
    )


def test_valid_clone_and_replay_decisions_parse():
    assert parse_decision(_text(_clone())).perturbation == {"grpo_config.learning_rate": 5e-6}
    assert parse_decision(_text(_replay())).source_recipe_id == "recipe-run-001"


def test_to_dict_has_the_seven_keys_and_round_trips():
    dec = parse_decision(_text(_clone()))
    assert tuple(dec.to_dict()) == DECISION_KEYS
    assert PlannerDecision.from_dict(dec.to_dict()) == dec
    assert parse_decision(json.dumps(dec.to_dict())) == dec


def test_decision_payload_drops_only_the_rationale():
    payload = parse_decision(_text(_new())).decision_payload()
    assert set(payload) == set(DECISION_KEYS) - {"rationale"}


def test_surrounding_whitespace_is_allowed():
    assert parse_decision("\n  " + _text(_new()) + "  \n").action == "new"


@pytest.mark.parametrize("opening", ["```json\n", "```\n", "```json  \n"])
def test_one_markdown_fence_around_the_object_is_allowed(opening):
    assert parse_decision(f"  {opening}{_text(_new())}\n```\n").action == "new"


@pytest.mark.parametrize(
    "text, match",
    [
        ("Here you go:\n```json\n" + json.dumps(_new()) + "\n```", "not a single JSON value"),
        ("```json\n" + json.dumps(_new()) + "\n```\nHope this helps", "exactly one fenced block"),
        ("```json\n" + json.dumps(_new()) + "\n```\n```json\n{}\n```", "not a single JSON value"),
        ("```python\n" + json.dumps(_new()) + "\n```", "exactly one fenced block"),
        ("```json\n" + json.dumps(_new()), "exactly one fenced block"),
        ("```json\n```", "output is empty"),
        ("", "output is empty"),
        ("   ", "output is empty"),
        ("Sure! " + json.dumps(_new()), "not a single JSON value"),
        (json.dumps(_new()) + " thanks", "not a single JSON value"),
        (json.dumps(_new()) + json.dumps(_new()), "not a single JSON value"),
        ("[" + json.dumps(_new()) + "]", "must be a JSON object"),
        ('"tom"', "must be a JSON object"),
        ("{'action': 'new'}", "not a single JSON value"),
    ],
)
def test_anything_but_one_json_object_is_rejected(text, match):
    with pytest.raises(DecisionParseError, match=match):
        parse_decision(text)


def test_duplicate_keys_are_rejected():
    text = '{"action": "new", ' + _text(_new())[1:]
    with pytest.raises(DecisionParseError, match="duplicate key 'action'"):
        parse_decision(text)


def test_non_finite_numbers_are_rejected():
    text = _text(_clone()).replace("5e-06", "NaN")
    with pytest.raises(DecisionParseError, match="non-finite number NaN"):
        parse_decision(text)


@pytest.mark.parametrize("key", DECISION_KEYS)
def test_every_key_is_required(key):
    d = _new()
    del d[key]
    with pytest.raises(DecisionParseError, match=f"missing keys: \\['{key}'\\]"):
        parse_decision(_text(d))


def test_unknown_keys_are_rejected():
    with pytest.raises(DecisionParseError, match=r"unknown keys: \['confidence'\]"):
        parse_decision(_text(_new(confidence=0.9)))


@pytest.mark.parametrize(
    "decision, match",
    [
        (_new(mode=None), "action 'new' requires mode"),
        (_new(mode="agent_rsi"), "mode must be one of"),
        (_new(source_recipe_id="recipe-run-001"), "action 'new' must not name a source_recipe_id"),
        (_new(perturbation={"sft_steps": 1}), "action 'new' must not carry a perturbation"),
        (_clone(source_recipe_id=None), "action 'clone' requires source_recipe_id"),
        (_clone(mode="batch"), "action 'clone' must leave mode null"),
        (_replay(source_recipe_id=None), "action 'replay' requires source_recipe_id"),
        (_replay(perturbation={"sft_steps": 1}), "action 'replay' must not carry a perturbation"),
        (_replay(mode="interact"), "action 'replay' must leave mode null"),
        (_new(action="retrain"), "action must be one of"),
        (_new(rationale=""), "rationale must be a non-empty string"),
        (_new(rationale="   "), "rationale must be a non-empty string"),
        (_new(rationale=None), "rationale must be a non-empty string"),
        (_new(target_dimension=""), "target_dimension must be a non-empty string"),
        (_new(target_dimension=3), "target_dimension must be a non-empty string"),
        (_clone(source_recipe_id=""), "source_recipe_id must be a non-empty string or null"),
        (_new(perturbation=None), "perturbation must be a JSON object"),
        (_clone(perturbation={"sft_steps": [1, 2]}), "must be a string, number or boolean"),
        (_clone(perturbation={"sft_steps": None}), "must be a string, number or boolean"),
        (_clone(perturbation={"": 1}), "perturbation keys must be non-empty"),
        (_new(data_synthesis_hint=None), "data_synthesis_hint must be a string"),
    ],
)
def test_the_rules_are_enforced(decision, match):
    with pytest.raises(DecisionParseError, match=match):
        parse_decision(_text(decision))


def test_an_empty_hint_is_allowed():
    assert parse_decision(_text(_new(data_synthesis_hint=""))).data_synthesis_hint == ""


def test_the_error_carries_a_readable_reason():
    with pytest.raises(DecisionParseError) as info:
        parse_decision(_text(_clone(source_recipe_id=None)))
    assert info.value.reason == "action 'clone' requires source_recipe_id"
    assert str(info.value) == info.value.reason


def test_non_text_output_is_rejected():
    with pytest.raises(DecisionParseError, match="output must be text"):
        parse_decision(None)


def test_a_huge_integer_perturbation_value_is_a_scalar():
    text = _text(_clone()).replace("5e-06", "1" + "0" * 399)  # 400 位整数,转 float 会 OverflowError
    assert parse_decision(text).perturbation == {"grpo_config.learning_rate": 10**399}


@pytest.mark.parametrize(
    "text",
    [
        "1" * 5000,  # 超过 4300 位:json.loads 抛普通 ValueError,不是 JSONDecodeError
        "[" * 100000,  # 3.11–3.13 抛 RecursionError,3.14 抛 JSONDecodeError
    ],
    ids=["5000-digit-integer", "deep-open-brackets"],
)
def test_pathological_json_becomes_a_parse_error(text):
    with pytest.raises(DecisionParseError, match="not a single JSON value"):
        parse_decision(text)


def test_from_dict_rejects_a_non_string_perturbation_key():
    with pytest.raises(DecisionParseError, match="perturbation keys must be non-empty strings"):
        PlannerDecision.from_dict(_clone(perturbation={1: 2}))


def test_echoed_values_are_clipped():
    with pytest.raises(DecisionParseError) as info:
        parse_decision(_text(_new(action="x" * 500)))
    reason = info.value.reason
    assert reason.endswith("…") and len(reason) < 200
    assert "'" + "x" * 79 + "…" in reason
    with pytest.raises(DecisionParseError) as info:
        parse_decision(_text(_clone(perturbation={"k" * 300: ["v" * 300]})))
    assert len(info.value.reason) < 250 and info.value.reason.count("…") == 2
```

- [ ] **Step 2: 跑测试,确认失败**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_tp_decision.py
```

预期:收集阶段报错,`ModuleNotFoundError: No module named 'trainplanner.decision'`,末行 `1 error in …`。

- [ ] **Step 3: 实现**

新建 `src/trainplanner/decision.py`:

```python
"""规划决策与严格解析。

模型输出必须是单个 JSON 对象;允许整段被一个 markdown 代码围栏包住(``` 或 ```json),
围栏外只能是空白。七个键全部必填,未知键拒绝,任何不合规则抛 DecisionParseError,
不做宽容猜测。
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass

from trainplanner.context import DECISION_ACTIONS

MODES = ("batch", "interact")
DECISION_KEYS = (
    "target_dimension",
    "action",
    "mode",
    "source_recipe_id",
    "perturbation",
    "data_synthesis_hint",
    "rationale",
)

_FENCE = re.compile(r"\A```(?:json)?[ \t]*\n(?P<body>.*?)\n?```\Z", re.DOTALL)


class DecisionParseError(ValueError):
    """模型输出不是合规的规划决策。reason 是给人和给重试提示看的一句话。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class PlannerDecision:
    """直接构造不做校验;从模型输出或字典得到决策一律走 parse_decision / from_dict。"""

    target_dimension: str
    action: str  # "new" | "clone" | "replay"
    mode: str | None  # "batch" | "interact",仅 new 需要
    source_recipe_id: str | None  # clone / replay 必填
    perturbation: dict  # 仅 clone 可非空
    data_synthesis_hint: str
    rationale: str

    def to_dict(self) -> dict:
        """七个键全量,与模型输出的形状相同。from_dict(to_dict(d)) == d。"""
        return {
            "target_dimension": self.target_dimension,
            "action": self.action,
            "mode": self.mode,
            "source_recipe_id": self.source_recipe_id,
            "perturbation": dict(self.perturbation),
            "data_synthesis_hint": self.data_synthesis_hint,
            "rationale": self.rationale,
        }

    def decision_payload(self) -> dict:
        """planner_decision 产物 payload.decision 的形状(设计 §4.2):去掉 rationale 的六个键。"""
        d = self.to_dict()
        del d["rationale"]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> PlannerDecision:
        """与 parse_decision 走同一套规则,只是输入已经是字典。"""
        return _decision_from_obj(d)


def _strip_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        m = _FENCE.match(stripped)
        if m is None:
            raise DecisionParseError(
                "output starts a markdown code fence but is not exactly one fenced block"
            )
        return m.group("body")
    return stripped


def _reject_constant(name: str) -> object:
    raise DecisionParseError(f"non-finite number {name} is not allowed")


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    out: dict = {}
    for k, v in pairs:
        if k in out:
            raise DecisionParseError(f"duplicate key {k!r}")
        out[k] = v
    return out


def _is_scalar(v: object) -> bool:
    if isinstance(v, (bool, str, int)):
        # 整数一定有限,不进 isfinite:超大整数(如 10**400)转 float 会抛 OverflowError
        return True
    if isinstance(v, float):
        return math.isfinite(v)
    return False


def _clip(value: object, n: int = 80) -> str:
    """模型给的值回显进错误信息(进而进重试提示与失败记录)前先截断,repr 超过 n 个字符就截掉并加 …。"""
    try:
        text = repr(value)
    except (ValueError, RecursionError):  # 超过 4300 位的整数、深层嵌套的列表
        return f"<unprintable {type(value).__name__}>"
    return text if len(text) <= n else text[:n] + "…"


def _decision_from_obj(obj: object) -> PlannerDecision:
    if not isinstance(obj, dict):
        raise DecisionParseError(f"output must be a JSON object, got {type(obj).__name__}")
    missing = [k for k in DECISION_KEYS if k not in obj]
    if missing:
        raise DecisionParseError(f"missing keys: {missing}")
    unknown = sorted(str(k) for k in obj if k not in DECISION_KEYS)
    if unknown:
        raise DecisionParseError(f"unknown keys: {unknown}")

    target = obj["target_dimension"]
    if not isinstance(target, str) or not target.strip():
        raise DecisionParseError("target_dimension must be a non-empty string")
    action = obj["action"]
    if action not in DECISION_ACTIONS:
        raise DecisionParseError(f"action must be one of {list(DECISION_ACTIONS)}, got {_clip(action)}")
    mode = obj["mode"]
    if mode is not None and mode not in MODES:
        raise DecisionParseError(f"mode must be one of {list(MODES)} or null, got {_clip(mode)}")
    source = obj["source_recipe_id"]
    if source is not None and (not isinstance(source, str) or not source.strip()):
        raise DecisionParseError("source_recipe_id must be a non-empty string or null")
    perturbation = obj["perturbation"]
    if not isinstance(perturbation, dict):
        raise DecisionParseError("perturbation must be a JSON object")
    for k, v in perturbation.items():
        if not isinstance(k, str) or not k.strip():
            raise DecisionParseError("perturbation keys must be non-empty strings")
        if not _is_scalar(v):
            raise DecisionParseError(
                f"perturbation[{_clip(k)}] must be a string, number or boolean, got {_clip(v)}"
            )
    hint = obj["data_synthesis_hint"]
    if not isinstance(hint, str):
        raise DecisionParseError("data_synthesis_hint must be a string")
    rationale = obj["rationale"]
    if not isinstance(rationale, str) or not rationale.strip():
        raise DecisionParseError("rationale must be a non-empty string")

    if action == "new":
        if mode is None:
            raise DecisionParseError("action 'new' requires mode ('batch' or 'interact')")
        if source is not None:
            raise DecisionParseError("action 'new' must not name a source_recipe_id")
        if perturbation:
            raise DecisionParseError("action 'new' must not carry a perturbation")
    else:
        if source is None:
            raise DecisionParseError(f"action {action!r} requires source_recipe_id")
        if mode is not None:
            raise DecisionParseError(
                f"action {action!r} must leave mode null (the recipe fixes it)"
            )
        if action == "replay" and perturbation:
            raise DecisionParseError("action 'replay' must not carry a perturbation")

    return PlannerDecision(
        target_dimension=target,
        action=action,
        mode=mode,
        source_recipe_id=source,
        perturbation=dict(perturbation),
        data_synthesis_hint=hint,
        rationale=rationale,
    )


def parse_decision(text: str) -> PlannerDecision:
    """把模型输出解析成 PlannerDecision。任何不合规都抛 DecisionParseError。"""
    if not isinstance(text, str):
        raise DecisionParseError(f"output must be text, got {type(text).__name__}")
    body = _strip_fence(text)
    if not body.strip():
        raise DecisionParseError("output is empty")
    try:
        obj = json.loads(
            body, object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant
        )
    except DecisionParseError:
        raise  # 重复键、NaN:钩子自己给了原因
    except json.JSONDecodeError as exc:
        raise DecisionParseError(f"output is not a single JSON value: {exc.msg} at char {exc.pos}") from exc
    except (ValueError, RecursionError) as exc:
        # 超过 4300 位的整数让 json.loads 抛普通 ValueError;深层嵌套抛 RecursionError(3.11–3.13)。
        # 都是模型输出的问题,转成解析失败进重试,不让它漏出 plan()
        raise DecisionParseError(
            f"output is not a single JSON value: {type(exc).__name__}: {_clip(str(exc))}"
        ) from exc
    return _decision_from_obj(obj)
```

- [ ] **Step 4: 跑测试,确认通过**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_tp_decision.py
```

预期:`61 passed`,无警告(耗时因机器而异)。

全量:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error
```

预期:`B+109 passed`,无警告(临时拷贝里实测 `949 passed`)。

- [ ] **Step 5: 提交**

```bash
git add src/trainplanner/decision.py
git add tests/test_tp_decision.py
git commit -m "feat: strict parsing of planner decisions"
```

---

### Task 52: 提示渲染

**Files:**
- Create: `src/trainplanner/prompt.py`(131 行)
- Test: `tests/test_tp_prompt.py`(19 个用例)

**Interfaces:**
- Consumes:任务 50 的 `PlanningContext`、`example_context_dict`(测试里)。
- Produces(任务 53、54、55 依赖):
  - `render_prompt(ctx, template_text, template_version) -> PackedContext`。
  - `PackedContext(text, template_version, context, placeholders, feedback=())`(冻结):`prompt` 属性(真正发给后端的文本)、`with_feedback(reasons) -> PackedContext`。
  - `check_template(template_text) -> tuple[str, ...]`(只查模板,返回用到的名字,排序去重)。
  - `placeholder_values(ctx) -> dict[str, str]`、`PLACEHOLDERS`、`NOT_RECORDED = "not recorded"`、`PromptRenderError(ValueError)`。

**设计要点:**
- **语法**:`{{name}}`,名字匹配 `[A-Za-z_][A-Za-z0-9_]*`,两侧允许空白。可用的名字固定七个:`run_id`(纯文本)、`candidate`、`diagnostic`、`recipes`、`recent_decisions`、`cost_gain`(各自那一段的 JSON)、`context`(整个上下文的 JSON)。
- **报错而不是留空**:未知名字、不构成占位符的 `{{`(包括 `{{ }}`、`{{run-id}}`、`{{{run_id}}}`、没闭合的 `{{ run_id`)、整份模板没有占位符,都抛 `PromptRenderError`,错误里带行号或名字。单独的 `}}` 是普通文本。
- **只扫一遍**:`re.sub` 一次替换完,替换进去的值(比如 `reject_reason` 里恰好有 `{{run_id}}`)不会再被解释。
- **稳定**:JSON 一律 `indent=2, sort_keys=True, ensure_ascii=False`,同一上下文渲染两次逐字节相同。
- **未记录的成本**:`cost_gain` 段(以及 `context` 里的 `cost_gain`)中值为 `None` 的 `gpu_hours`、`tokens` 渲染成字符串 `"not recorded"`(常量 `NOT_RECORDED`),不写 `null` 也不写 0;`gain` 的 `null` 照旧。只改渲染出的文本,不改上下文对象(有测试)。
- **重试反馈与模板分开**:`text` 永远是纯模板渲染结果;`feedback` 是被拒原因,`prompt` 在 `text` 末尾追加一节 `## Rejected attempts`。这样确定性后端可以忽略反馈,桥接后端发完整提示。
- `PackedContext.context` 原样带着上下文对象,`HeuristicBackend` 直接读它,不去反解析提示文本。

**Ruling: 占位符语法用 `{{name}}`,名字白名单固定,未知名字与游离的 `{{` 一律报错** —— Python 的 `str.format` 会把模板里 JSON 示例的 `{` 当成占位符,而 `string.Template` 的 `$name` 在提示文本里太容易误伤;双花括号在 `PLANNER_V1` 里不会自然出现 —— 判错的代价:想在模板里写字面的 `{{` 时无法转义,只能改写措辞。

**Ruling: 模板不必用到全部占位符,但至少用到一个** —— 设计没规定"必须全用";强制全用会让试验性的精简模板写不出来,而一个占位符都没有的模板必然是写错了 —— 判错的代价:一份漏了 `{{diagnostic}}` 的模板能装进树,规划时模型看不到诊断;只能靠审模板的人发现。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_tp_prompt.py`:

```python
import json

import pytest

from trainplanner.context import PlanningContext
from trainplanner.examples import example_context_dict
from trainplanner.prompt import PLACEHOLDERS, PackedContext, PromptRenderError, check_template, render_prompt


@pytest.fixture
def ctx():
    return PlanningContext.from_dict(example_context_dict())


def test_placeholders_are_replaced_and_the_version_is_recorded(ctx):
    packed = render_prompt(ctx, "run={{run_id}}\n{{ diagnostic }}", "v7")
    assert isinstance(packed, PackedContext)
    assert packed.template_version == "v7"
    assert packed.context is ctx
    assert packed.placeholders == ("diagnostic", "run_id")
    head, body = packed.text.split("\n", 1)
    assert head == "run=run-003"
    assert json.loads(body) == example_context_dict()["diagnostic"]


def test_every_placeholder_renders(ctx):
    template = "\n---\n".join("{{" + name + "}}" for name in PLACEHOLDERS)
    parts = render_prompt(ctx, template, "v1").text.split("\n---\n")
    d = example_context_dict()
    for entry in d["cost_gain"]:  # 未记录的成本渲染成 "not recorded"(见下一条测试)
        for k in ("gpu_hours", "tokens"):
            if entry[k] is None:
                entry[k] = "not recorded"
    assert parts[0] == "run-003"
    for name, part in zip(PLACEHOLDERS[1:-1], parts[1:-1]):
        assert json.loads(part) == d[name]
    assert json.loads(parts[-1]) == d


def test_structured_values_render_as_stable_sorted_json(ctx):
    text = render_prompt(ctx, "{{recipes}}", "v1").text
    assert text == json.dumps(example_context_dict()["recipes"], indent=2, sort_keys=True, ensure_ascii=False)
    assert render_prompt(ctx, "{{recipes}}", "v1").text == text


def test_unrecorded_costs_render_as_not_recorded_never_as_zero_or_null(ctx):
    for template in ("{{cost_gain}}", "{{context}}"):
        rendered = json.loads(render_prompt(ctx, template, "v1").text)
        entries = rendered if template == "{{cost_gain}}" else rendered["cost_gain"]
        assert entries[0]["gpu_hours"] == "not recorded"
        assert entries[0]["tokens"] == "not recorded"
        assert entries[1]["gpu_hours"] == 2.0 and entries[1]["tokens"] == 12000
        assert entries[0]["gain"] is None  # 只有成本改写,增益的 null 照旧
    text = render_prompt(ctx, "{{cost_gain}}", "v1").text
    assert '"gpu_hours": 0' not in text and '"tokens": 0' not in text
    assert '"gpu_hours": null' not in text and '"tokens": null' not in text
    assert ctx.cost_gain[0].gpu_hours is None  # 渲染不改上下文


def test_a_repeated_placeholder_is_replaced_everywhere(ctx):
    assert render_prompt(ctx, "{{run_id}} and {{run_id}}", "v1").text == "run-003 and run-003"


@pytest.mark.parametrize(
    "template, match",
    [
        ("{{run_id}} {{budget}}", r"unknown placeholders \['budget'\]"),
        ("{{Run_Id}}", "unknown placeholders"),
        ("{{run_id}} {{ }}", "line 1: '\\{\\{' does not open a valid placeholder"),
        ("{{run_id}}\n{{run-id}}", "line 2: '\\{\\{' does not open a valid placeholder"),
        ("{{run_id}} {{ run_id", "does not open a valid placeholder"),
        ("{{{run_id}}}", "does not open a valid placeholder"),
        ("no placeholders at all", "references no placeholder"),
        ("{run_id}", "references no placeholder"),
    ],
)
def test_bad_templates_raise_instead_of_leaving_blanks(ctx, template, match):
    with pytest.raises(PromptRenderError, match=match):
        render_prompt(ctx, template, "v1")


def test_a_lone_closing_brace_pair_is_plain_text(ctx):
    assert render_prompt(ctx, "{{run_id}} }} end", "v1").text == "run-003 }} end"


def test_substituted_values_are_not_rescanned():
    # 两个占位符:按名字依次 str.replace 的错误实现会把 reject_reason 里的 {{cost_gain}} 再替换一次
    d = example_context_dict()
    d["candidate"]["reject_reason"] = "contains {{cost_gain}} literally"
    d["candidate"]["status"] = "rejected"
    ctx = PlanningContext.from_dict(d)
    text = render_prompt(ctx, "{{candidate}}\n{{cost_gain}}", "v1").text
    assert "contains {{cost_gain}} literally" in text
    cost_json = json.dumps(
        json.loads(render_prompt(ctx, "{{cost_gain}}", "v1").text), indent=2, sort_keys=True, ensure_ascii=False
    )
    assert text.count(cost_json) == 1


def test_the_version_must_be_non_empty(ctx):
    with pytest.raises(PromptRenderError, match="template_version"):
        render_prompt(ctx, "{{run_id}}", " ")


def test_the_context_must_be_a_planning_context():
    with pytest.raises(TypeError, match="PlanningContext"):
        render_prompt(example_context_dict(), "{{run_id}}", "v1")


def test_check_template_returns_the_names_used():
    assert check_template("{{cost_gain}} {{ run_id }} {{run_id}}") == ("cost_gain", "run_id")


def test_feedback_is_appended_to_the_prompt_only(ctx):
    packed = render_prompt(ctx, "{{run_id}}", "v1")
    assert packed.prompt == packed.text == "run-003"
    retry = packed.with_feedback(("attempt 1: parse error: output is empty",))
    assert retry.text == "run-003"
    assert retry.prompt == (
        "run-003\n\n## Rejected attempts\n"
        "Your previous output was rejected for the reasons below. "
        "Fix them and output only one JSON object.\n"
        "- attempt 1: parse error: output is empty\n"
    )
    assert packed.feedback == ()
```

- [ ] **Step 2: 跑测试,确认失败**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_tp_prompt.py
```

预期:收集阶段报错,`ModuleNotFoundError: No module named 'trainplanner.prompt'`,末行 `1 error in …`。

- [ ] **Step 3: 实现**

新建 `src/trainplanner/prompt.py`:

```python
"""提示渲染。

占位符语法:`{{name}}`,名字两侧允许空白(`{{ name }}`)。可用名字固定为 PLACEHOLDERS。
- 名字不在 PLACEHOLDERS 里 → PromptRenderError(不留空、不原样保留)。
- 模板里出现不构成占位符的 `{{` → PromptRenderError。单独的 `}}` 是普通文本。
- 模板一个占位符都没有 → PromptRenderError(提示里不会带任何上下文)。
- 替换只扫描一遍模板,替换进去的值里即使含 `{{` 也不会再被解释。
结构化的值渲染成 JSON(键排序、缩进 2、不转义非 ASCII),同一上下文渲染结果逐字节稳定。
cost_gain 里为 None 的成本(gpu_hours、tokens)渲染成字符串 "not recorded",不写 null 也不写 0:
没记录的成本不能被模型读成免费。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace

from trainplanner.context import PlanningContext

PLACEHOLDERS = (
    "run_id",
    "candidate",
    "diagnostic",
    "recipes",
    "recent_decisions",
    "cost_gain",
    "context",
)

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_OPEN = re.compile(r"\{\{")

NOT_RECORDED = "not recorded"
_COST_KEYS = ("gpu_hours", "tokens")


class PromptRenderError(ValueError):
    """模板与上下文渲染不出一份完整的提示。这是模板的缺陷,不是模型的,不进重试。"""


def _json(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)


def _render_costs(entries: list[dict]) -> list[dict]:
    return [
        {k: (NOT_RECORDED if k in _COST_KEYS and v is None else v) for k, v in e.items()}
        for e in entries
    ]


def placeholder_values(ctx: PlanningContext) -> dict[str, str]:
    d = ctx.to_dict()
    d["cost_gain"] = _render_costs(d["cost_gain"])
    return {
        "run_id": ctx.run_id,
        "candidate": _json(d["candidate"]),
        "diagnostic": _json(d["diagnostic"]),
        "recipes": _json(d["recipes"]),
        "recent_decisions": _json(d["recent_decisions"]),
        "cost_gain": _json(d["cost_gain"]),
        "context": _json(d),
    }


@dataclass(frozen=True)
class PackedContext:
    """一次规划要发给后端的全部东西。

    text 是模板渲染结果;feedback 是前几次被拒的原因,prompt 把两者拼成真正发出的提示。
    context 原样带着,确定性后端(HeuristicBackend)直接读它,不去解析提示文本。
    """

    text: str
    template_version: str
    context: PlanningContext
    placeholders: tuple[str, ...]
    feedback: tuple[str, ...] = field(default_factory=tuple)

    @property
    def prompt(self) -> str:
        if not self.feedback:
            return self.text
        lines = [
            self.text.rstrip("\n"),
            "",
            "## Rejected attempts",
            "Your previous output was rejected for the reasons below. "
            "Fix them and output only one JSON object.",
        ]
        lines += [f"- {reason}" for reason in self.feedback]
        return "\n".join(lines) + "\n"

    def with_feedback(self, feedback: tuple[str, ...]) -> PackedContext:
        return replace(self, feedback=tuple(feedback))


def check_template(template_text: str) -> tuple[str, ...]:
    """只查模板本身:语法、名字是否已知、至少一个占位符。返回用到的名字(排序去重)。"""
    if not isinstance(template_text, str):
        raise PromptRenderError("template_text must be a string")
    matches = list(_PLACEHOLDER.finditer(template_text))
    starts = {m.start() for m in matches}
    for m in _OPEN.finditer(template_text):
        if m.start() not in starts:
            line = template_text.count("\n", 0, m.start()) + 1
            raise PromptRenderError(f"line {line}: '{{{{' does not open a valid placeholder")
    if not matches:
        raise PromptRenderError(
            "template references no placeholder; the prompt would carry no context"
        )
    used = sorted({m.group(1) for m in matches})
    unknown = [name for name in used if name not in PLACEHOLDERS]
    if unknown:
        raise PromptRenderError(
            f"template uses unknown placeholders {unknown}; known: {list(PLACEHOLDERS)}"
        )
    return tuple(used)


def render_prompt(ctx: PlanningContext, template_text: str, template_version: str) -> PackedContext:
    if not isinstance(ctx, PlanningContext):
        raise TypeError(f"ctx must be a PlanningContext, got {type(ctx).__name__}")
    if not isinstance(template_version, str) or not template_version.strip():
        raise PromptRenderError("template_version must be a non-empty string")
    used = check_template(template_text)
    values = placeholder_values(ctx)
    text = _PLACEHOLDER.sub(lambda m: values[m.group(1)], template_text)
    return PackedContext(
        text=text, template_version=template_version, context=ctx, placeholders=used
    )
```

- [ ] **Step 4: 跑测试,确认通过**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_tp_prompt.py
```

预期:`19 passed`,无警告(耗时因机器而异)。

全量:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error
```

预期:`B+128 passed`,无警告(临时拷贝里实测 `968 passed`)。

- [ ] **Step 5: 提交**

```bash
git add src/trainplanner/prompt.py
git add tests/test_tp_prompt.py
git commit -m "feat: render planner prompts from a PlanningContext with strict placeholders"
```

---

### Task 53: 默认提示模板与模板信封

**Files:**
- Create: `src/trainplanner/templates.py`(153 行)
- Test: `tests/test_tp_templates.py`(23 个用例)

**Interfaces:**
- Consumes:`controlplane.paths.planner_prompt_file`(**计划 1 产出**);`controlplane.envelope.Envelope`、`OwnerRef`、`CURRENT_SCHEMA_VERSION`;`controlplane.gitstore.GitStore`;任务 52 的 `check_template`、`PromptRenderError`。
- Produces(计划 5、计划 9 依赖):
  - `PLANNER_V1: str`、`PLANNER_V1_VERSION = "v1"`。
  - `install_template(store, version, text) -> str`(返回相对路径)、`install_default_template(store) -> str`。
  - `load_template(store, version) -> PromptTemplate`(`PromptTemplate(version, text)`,冻结)。
  - `TemplateError(ValueError)`、`TEMPLATE_TYPE = "prompt_template"`、`TEMPLATE_STATUS = "executed"`。

**设计要点:**
- **信封形状**:`id = "prompt-planner-<version>"`,`type = "prompt_template"`,`status = "executed"`,`version = 1`,`payload` 恰为 `{"version", "text"}`,路径 `planner_prompt_file(version)`。
- **写入前先校验**:模板过不了 `check_template` 就不写,树里不会出现渲染不了的模板。
- **版本不可变**:同版本同文本 → 不动(连提交都不产生);同版本不同文本 → `TemplateError`,要求升版本。这样已经写进 `planner_decision` 的 `template_version` 永远能对回当时的文本。
- **版本号格式**:`install_template` 与 `load_template` 都先查版本号匹配 `^[A-Za-z0-9._-]+$` 且不是 `.`、`..`,否则 `TemplateError`,什么都不写。版本号会拼进 `planner-<version>.yaml`,含 `/` 会建出子目录,含空格会让路径难以引用。
- **读回时同样严格**:类型不对、载荷键不对、载荷里的版本与路径不一致、文本过不了 `check_template`,都抛 `TemplateError`。读不到文件也抛 `TemplateError`(带路径),不让 `FileNotFoundError` 漏出去被当成瞬态错误。
- `PLANNER_V1` 用英文写:要求模型"Output exactly one JSON object and nothing else",逐个说明七个键的含义与取值,包括 `null` 与 `{}` 的写法,和上文 Ruling 的三条附加规则一致。它用到除 `context` 以外的六个占位符,并在成本一节写明 `"not recorded"` 表示未知,不是 0,也不是免费。
- 它还写明四条会被下游拒掉的约束,免得模型白耗重试:(a) `null` 不是标量,扰动值不能是 `null`;(b) 扰动键是训练配置里的点路径(示例 `"grpo_config.learning_rate"`、`"sft_steps"`,两者都在计划 5 默认 `perturbable` 白名单 `[opd_coefficient, sft_steps, grpo_steps, grpo_config.learning_rate]` 里),不在调度器白名单里的键会被拒绝并耗掉一次尝试;(c) interact 默认关闭,用 `"batch"`;(d) 退化标记 `magnitude` 的方向因 kind 而异:`social_regression` 是下降量(越大越差),`g_probe_failure` 是探针分数(越小越差),`reward_hacking_suspected` 是 `reward_delta − diag_delta`(奖励涨幅减诊断涨幅,越大越差;与 `src/diagnostic/regression.py` 的 `detect_reward_hacking` 判据及计划 5 Ruling 12 的取值一致)。本计划先于计划 5 执行,模板不导入白名单,只描述格式与规则,不枚举全部键。

**Ruling: `PLANNER_V1` 的版本号是 `"v1"`,文件是 `control-plane/config/prompts/planner-v1.yaml`,决策产物里记的 `template_version` 也是 `"v1"`** —— 补充契约规定路径是 `planner-<version>.yaml`,版本号若写成 `planner-v1`,文件名就成了 `planner-planner-v1.yaml`;设计 §4.2 写的是 `template_version: v1`,与本计划一致;`planner-v1` 只出现在计划 3 的测试夹具里(另行修正) —— 判错的代价:任何照抄 `planner-v1` 作版本号的地方都会对不上,应当以 `PLANNER_V1_VERSION` 为准。

**Ruling: 模板版本号必须匹配 `^[A-Za-z0-9._-]+$` 且不是 `.`、`..`,安装与读取都校验** —— 版本号拼进文件名,`a/b` 会写出 `planner-a/b.yaml` 这个子目录,`..` 虽然拼出的是 `planner-...yaml` 但没有意义 —— 判错的代价:想用带空格或斜杠的版本名(如 `v1 beta`)时要改写成 `v1-beta`。

**Ruling: 模板信封的状态是 `executed`(非审核类终态)** —— 设计没规定;模板随代码发布,不走审核门,`executed` 与 `run_trace`、`library_state` 等"写了就算数"的产物一致 —— 判错的代价:若以后要求模板改动过人审,要改成审核类并经 `pending_review → approved → effective`,已装入的模板要迁移。

**Ruling: 模板文本用英文** —— 目标是外部命令行模型,英文指令对字段名与 JSON 的约束最直接;字段名本身就是英文 —— 判错的代价:中文审阅者读模板稍费力;想换中文模板就装一个新版本,不影响解析。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_tp_templates.py`:

```python
import pytest

from controlplane.gitstore import GitStore
from controlplane.paths import planner_prompt_file
from trainplanner.context import PlanningContext
from trainplanner.decision import DECISION_KEYS
from trainplanner.examples import example_context_dict
from trainplanner.prompt import PLACEHOLDERS, check_template, render_prompt
from trainplanner.templates import (
    PLANNER_V1,
    PLANNER_V1_VERSION,
    TemplateError,
    install_default_template,
    install_template,
    load_template,
)


@pytest.fixture
def store(tmp_path):
    s = GitStore(str(tmp_path))
    s.init()
    return s


def _commits(store):
    # 空仓库安全:刚 init、还没有提交时 rev-list HEAD 会抛 CalledProcessError;--all 返回 0
    return store._git("rev-list", "--all", "--count").strip()


def test_planner_v1_uses_every_section_of_the_context():
    assert set(check_template(PLANNER_V1)) == set(PLACEHOLDERS) - {"context"}


def test_planner_v1_names_every_decision_key_and_demands_one_json_object():
    for key in DECISION_KEYS:
        assert f'"{key}"' in PLANNER_V1
    assert "exactly one JSON object" in PLANNER_V1
    assert '"not recorded"' in PLANNER_V1 and "not zero and not free" in PLANNER_V1
    for value in ('"new"', '"clone"', '"replay"', '"batch"', '"interact"'):
        assert value in PLANNER_V1


def test_planner_v1_states_the_rules_the_scheduler_enforces():
    assert "null is not a scalar value" in PLANNER_V1
    assert "dotted path" in PLANNER_V1 and "perturbable whitelist" in PLANNER_V1
    # 示例键必须真在计划 5 的默认 perturbable 白名单里
    # (opd_coefficient、sft_steps、grpo_steps、grpo_config.learning_rate)
    assert '"grpo_config.learning_rate"' in PLANNER_V1 and '"sft_steps"' in PLANNER_V1
    assert "interact is off by default" in PLANNER_V1
    assert "larger is worse" in PLANNER_V1 and "smaller is worse" in PLANNER_V1
    assert "reward_hacking_suspected" in PLANNER_V1


def test_planner_v1_renders_against_the_example_context():
    ctx = PlanningContext.from_dict(example_context_dict())
    text = render_prompt(ctx, PLANNER_V1, PLANNER_V1_VERSION).text
    assert "{{" not in text
    assert "recipe-run-001" in text


def test_install_default_writes_a_prompt_template_envelope(store):
    rel = install_default_template(store)
    assert rel == planner_prompt_file(PLANNER_V1_VERSION)
    assert rel == "control-plane/config/prompts/planner-v1.yaml"
    env = store.read_product(rel)
    assert env.type == "prompt_template"
    assert env.id == "prompt-planner-v1"
    assert env.status == "executed"
    assert env.payload == {"version": "v1", "text": PLANNER_V1}


def test_load_template_reads_it_back(store):
    install_default_template(store)
    tpl = load_template(store, PLANNER_V1_VERSION)
    assert (tpl.version, tpl.text) == ("v1", PLANNER_V1)


def test_installing_twice_is_a_no_op(store):
    install_default_template(store)
    before = _commits(store)
    install_default_template(store)
    assert _commits(store) == before


def test_a_different_text_for_the_same_version_is_refused(store):
    install_template(store, "v2", "first {{run_id}}")
    with pytest.raises(TemplateError, match="bump the version"):
        install_template(store, "v2", "second {{run_id}}")
    assert load_template(store, "v2").text == "first {{run_id}}"


def test_an_invalid_template_is_never_written(store):
    with pytest.raises(TemplateError, match="unknown placeholders"):
        install_template(store, "v3", "{{budget}}")
    assert not store.exists(planner_prompt_file("v3"))


def test_loading_a_missing_version_fails_clearly(store):
    with pytest.raises(TemplateError, match="planner prompt template v9 not found"):
        load_template(store, "v9")


@pytest.mark.parametrize("version", ["", "a/b", "..", "v 1"], ids=["empty", "slash", "dotdot", "space"])
@pytest.mark.parametrize("op", ["install", "load"])
def test_a_malformed_version_is_refused(store, op, version):
    with pytest.raises(TemplateError, match="template version must match"):
        if op == "install":
            install_template(store, version, "{{run_id}}")
        else:
            load_template(store, version)
    # 什么都没写:prompts/ 目录都没被建出来,仓库里也没有任何提交
    assert not (store.repo_root / "control-plane/config/prompts").exists()
    assert store._git("rev-list", "--all").strip() == ""


def _put(store, version, *, type_="prompt_template", payload):
    from controlplane.envelope import Envelope, OwnerRef

    env = Envelope(
        id=f"prompt-planner-{version}", type=type_, version=1, created_at="2026-10-09T00:00:00Z",
        created_by=OwnerRef(), status="executed", schema_version=1, payload=payload,
    )
    store.write_product(planner_prompt_file(version), env, "test: hand-written template")


@pytest.mark.parametrize(
    "type_, payload, match",
    [
        ("config", {"version": "v4", "text": "{{run_id}}"}, "expected type prompt_template"),
        ("prompt_template", {"version": "v4"}, "exactly the keys version and text"),
        ("prompt_template", {"version": "v4", "text": "{{run_id}}", "x": 1}, "exactly the keys"),
        ("prompt_template", {"version": "v5", "text": "{{run_id}}"}, "payload version"),
        ("prompt_template", {"version": "v4", "text": "{{oops}}"}, "unknown placeholders"),
    ],
)
def test_a_malformed_template_in_the_tree_is_refused(store, type_, payload, match):
    _put(store, "v4", type_=type_, payload=payload)
    with pytest.raises(TemplateError, match=match):
        load_template(store, "v4")
```

- [ ] **Step 2: 跑测试,确认失败**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_tp_templates.py
```

预期:收集阶段报错,`ModuleNotFoundError: No module named 'trainplanner.templates'`,末行 `1 error in …`。

- [ ] **Step 3: 实现**

新建 `src/trainplanner/templates.py`:

```python
"""规划提示模板:包内默认模板 PLANNER_V1,以及它在树里的信封形态。

模板产物:type 为 prompt_template,status 为 executed,载荷恰为 {version, text},
路径 planner_prompt_file(version)。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from controlplane.envelope import CURRENT_SCHEMA_VERSION, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import planner_prompt_file
from trainplanner.prompt import PromptRenderError, check_template

_NOW = "2026-10-09T00:00:00Z"
TEMPLATE_TYPE = "prompt_template"
TEMPLATE_STATUS = "executed"

PLANNER_V1_VERSION = "v1"
# 版本号拼进文件名 planner-<version>.yaml:只允许单个路径段里安全的字符,"/" 会建出子目录
_VERSION = re.compile(r"^[A-Za-z0-9._-]+$")

PLANNER_V1 = """You are the training planner of a model factory. A training round has just
finished. Read the evidence below and choose what the next round should do.

## Source run
{{run_id}}

## Candidate checkpoint of the source run
{{candidate}}

## Diagnostic summary of the source run
l1_scores maps each first-level dimension to its score (higher is better).
regression_flags lists statistically significant problems; error_profiles shows
where the errors concentrate. The meaning of a flag's magnitude depends on its kind:
for "social_regression" it is the drop in the subject dimension (larger is worse);
for "g_probe_failure" it is the probe's score (smaller is worse);
for "reward_hacking_suspected" it is reward gain minus diagnostic gain
(larger is worse).
{{diagnostic}}

## Recipes on record
Only recipes whose status is "effective" may be cloned or replayed.
mean_gain is the average gain of past reuses (null means no comparable result yet).
{{recipes}}

## Recent planner decisions and how their rounds turned out
{{recent_decisions}}

## Cost and gain of recent rounds
gpu_hours and tokens are "not recorded" when nothing was logged for that round;
that means unknown, not zero and not free.
{{cost_gain}}

## Your answer
Output exactly one JSON object and nothing else: no prose before or after it.
The object must have exactly these seven keys:

- "target_dimension": string. The first-level dimension the next round should improve,
  normally one of the keys of l1_scores.
- "action": one of "new", "clone", "replay".
  "new" trains from the default training config; "clone" reuses a recipe's training
  config with a small perturbation; "replay" reruns a recipe exactly as recorded.
- "mode": "batch" or "interact" when action is "new"; null for "clone" and "replay"
  (the recipe already fixes the mode). Use "batch": interact is off by default and a
  decision that uses it is normally rejected.
- "source_recipe_id": the id of an effective recipe when action is "clone" or "replay";
  null when action is "new".
- "perturbation": object mapping training-config keys to new scalar values. Allowed only
  for "clone"; must be {} for "new" and "replay". Each key is a dotted path into the
  recipe's training config, for example "grpo_config.learning_rate" or "sft_steps".
  Only keys on the scheduler's perturbable whitelist are accepted; any other key is
  rejected and costs you one of your limited attempts. Each value must be a string,
  a number or true/false; null is not a scalar value and is rejected.
- "data_synthesis_hint": string, may be empty. What the training data should emphasise.
- "rationale": non-empty string. Why this choice, citing the evidence above.
"""


class TemplateError(ValueError):
    """树里的模板缺失、形状不对,或与要写入的模板冲突。确定性错误,重试无益。"""


@dataclass(frozen=True)
class PromptTemplate:
    version: str
    text: str


def _check_version(version: object) -> None:
    if not isinstance(version, str) or not _VERSION.match(version) or version in (".", ".."):
        raise TemplateError(
            f"template version must match [A-Za-z0-9._-]+ and not be '.' or '..', got {version!r}"
        )


def _envelope(version: str, text: str) -> Envelope:
    return Envelope(
        id=f"prompt-planner-{version}",
        type=TEMPLATE_TYPE,
        version=1,
        created_at=_NOW,
        created_by=OwnerRef(),
        status=TEMPLATE_STATUS,
        schema_version=CURRENT_SCHEMA_VERSION,
        payload={"version": version, "text": text},
    )


def install_template(store: GitStore, version: str, text: str) -> str:
    """把一份模板写进树。已有同版本同文本则不动;同版本不同文本拒绝覆盖。返回相对路径。"""
    _check_version(version)
    try:
        check_template(text)
    except PromptRenderError as exc:
        raise TemplateError(f"template {version}: {exc}") from exc
    rel = planner_prompt_file(version)
    if store.exists(rel):
        existing = load_template(store, version)
        if existing.text != text:
            raise TemplateError(
                f"{rel} already holds a different template for version {version}; "
                "bump the version instead of overwriting"
            )
        return rel
    store.write_product(rel, _envelope(version, text), f"trainplanner: install planner prompt {version}")
    return rel


def install_default_template(store: GitStore) -> str:
    return install_template(store, PLANNER_V1_VERSION, PLANNER_V1)


def load_template(store: GitStore, version: str) -> PromptTemplate:
    _check_version(version)
    rel = planner_prompt_file(version)
    if not store.exists(rel):
        raise TemplateError(f"planner prompt template {version} not found at {rel}")
    env = store.read_product(rel)
    if env.type != TEMPLATE_TYPE:
        raise TemplateError(f"{rel}: expected type {TEMPLATE_TYPE}, got {env.type}")
    payload = env.payload
    if set(payload) != {"version", "text"}:
        raise TemplateError(f"{rel}: payload must have exactly the keys version and text")
    if payload["version"] != version:
        raise TemplateError(f"{rel}: payload version {payload['version']!r} != {version!r}")
    text = payload["text"]
    try:
        check_template(text)
    except PromptRenderError as exc:
        raise TemplateError(f"{rel}: {exc}") from exc
    return PromptTemplate(version=version, text=text)
```

- [ ] **Step 4: 跑测试,确认通过**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_tp_templates.py
```

预期:`23 passed`,无警告(耗时因机器而异)。

全量:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error
```

预期:`B+151 passed`,无警告(临时拷贝里实测 `991 passed`)。

- [ ] **Step 5: 提交**

```bash
git add src/trainplanner/templates.py
git add tests/test_tp_templates.py
git commit -m "feat: ship the PLANNER_V1 prompt template as a prompt_template envelope"
```

---

### Task 54: 三个决策后端

**Files:**
- Create: `src/trainplanner/backends.py`(111 行)
- Test: `tests/test_tp_backends.py`(14 个用例)

**Interfaces:**
- Consumes:`inference_bridge.InferenceBridge`(**计划 1 产出**;测试里还用 `BridgeError`、`ScriptedBridge`);任务 50 的 `RecipeSummary`;任务 52 的 `PackedContext`、`render_prompt`;任务 51 的 `parse_decision`(测试里)。
- Produces(任务 55、计划 5、计划 9 依赖):
  - `DecisionBackend`(协议):属性 `name: str`,方法 `decide(packed, *, run_id, task_id) -> str`。
  - `BridgeDecisionBackend(bridge, *, name="bridge")`。
  - `HeuristicBackend()`,`name == "heuristic"`。
  - `ScriptedDecisionBackend(responses, *, name="scripted")`,属性 `calls: list[tuple[prompt, run_id, task_id]]`。
  - 常量 `CLONABLE_RECIPE_TYPES = ("batch",)`。

**设计要点:**
- **后端只返回文本。** 解析不在后端里做,三个后端的输出走同一条 `parse_decision` 路径。
- **`BridgeDecisionBackend`** 发 `packed.prompt`(含重试反馈),返回 `BridgeResult.text`。`BridgeError` 与其他异常原样上抛:不重试、不改类别。重试归桥接自己(T1 退避)和 `plan()`(T2 重新采样)。
- **`HeuristicBackend`** 不看提示文本,只读 `packed.context`:
  1. 维度 = `l1_scores` 里分数最低的;同分按维度名升序。
  2. 候选配方 = 该维度、`status == "effective"`、`recipe_type == "batch"` 的配方。
  3. 有候选就 `clone` 排第一的一条,不扰动;排序与 recipestore 的 `order="track"` 相同:`mean_gain` 降序(`None` 排最后),再 `n_reuse` 降序,再 id 升序。
  4. 没有候选就 `new`,`mode = "batch"`。
  5. `rationale` 写明维度、分数和选择理由。输出用 `json.dumps`,保证是合法 JSON。
- **`ScriptedDecisionBackend`** 元素是字符串就返回,是异常实例就抛出;用完再调用抛 `AssertionError`(测试写错了,不是被测代码的错)。
- **后端名**进 `PlannerResult.backend`,再进 `planner_decision.payload.backend`(设计 §4.2 示例 `backend: claude_cli`)。所以 `BridgeDecisionBackend` 的名字由调用方传入,计划 9 的组合根用配置里的 `backend` 字段。

**Ruling: `HeuristicBackend` 只 clone `batch` 配方,`interact` 与 `agent_rsi` 配方都忽略** —— 设计说"有该维度的有效配方就 clone",没说类型。`agent_rsi` 配方构造不成训练指令(§7.2"不构造 AgentRSI 指令");`interact` 默认被计划 5 的领域校验拒绝(`allow_interact` 默认关)。确定性后端每次重试给的答案都一样,一旦被拒就必然耗尽重试 —— 判错的代价:开了 `allow_interact` 之后,启发式也不会复用 interact 配方,只会对那个维度新开 batch 轮。

**Ruling: `HeuristicBackend` 不看 `candidate.status` 与退化标记** —— 设计把它定义成"取最低维度"的纯确定性兜底,加规则就成了第二个规划器 —— 判错的代价:候选被拒后启发式可能再次 clone 同一条配方,要靠战绩回写把配方 `deprecated`(R8)才会换路。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_tp_backends.py`:

```python
import json

import pytest

from inference_bridge import BridgeError, ScriptedBridge
from trainplanner.backends import BridgeDecisionBackend, HeuristicBackend, ScriptedDecisionBackend
from trainplanner.context import PlanningContext
from trainplanner.decision import parse_decision
from trainplanner.examples import example_context_dict
from trainplanner.prompt import render_prompt


def _packed(mutate=None):
    d = example_context_dict()
    if mutate is not None:
        mutate(d)
    return render_prompt(PlanningContext.from_dict(d), "{{context}}", "v1")


def _recipe(rid, dimension, *, mean_gain, n_reuse=0, status="effective", recipe_type="batch"):
    return {"id": rid, "recipe_type": recipe_type, "dimension": dimension, "status": status,
            "mean_gain": mean_gain, "n_reuse": n_reuse}


def _heuristic(mutate=None):
    text = HeuristicBackend().decide(_packed(mutate), run_id="run-003", task_id="t")
    json.loads(text)  # 输出本身就是合法 JSON
    return parse_decision(text)


def test_heuristic_clones_the_recipe_of_the_lowest_dimension():
    dec = _heuristic()
    assert (dec.target_dimension, dec.action, dec.source_recipe_id) == ("tom", "clone", "recipe-run-001")
    assert dec.mode is None and dec.perturbation == {}
    assert "tom" in dec.rationale and "0.58" in dec.rationale


def test_heuristic_starts_a_new_batch_when_the_dimension_has_no_recipe():
    dec = _heuristic(lambda d: d["diagnostic"]["l1_scores"].update(empathy=0.1))
    assert dec.target_dimension == "empathy"
    assert dec.action == "clone" and dec.source_recipe_id == "recipe-run-002"

    dec = _heuristic(lambda d: d["diagnostic"]["l1_scores"].update(pragmatics=0.1))
    assert (dec.target_dimension, dec.action, dec.mode, dec.source_recipe_id) == (
        "pragmatics", "new", "batch", None)


def test_heuristic_breaks_score_ties_by_dimension_name():
    dec = _heuristic(lambda d: d["diagnostic"].update(l1_scores={"zeta": 0.2, "alpha": 0.2}))
    assert dec.target_dimension == "alpha"


def test_heuristic_ranks_recipes_by_track_then_reuse_then_id():
    def recipes(d):
        d["recipes"] = [
            _recipe("recipe-c", "tom", mean_gain=None, n_reuse=9),
            _recipe("recipe-b", "tom", mean_gain=0.02, n_reuse=1),
            _recipe("recipe-a", "tom", mean_gain=0.02, n_reuse=1),
            _recipe("recipe-d", "tom", mean_gain=0.02, n_reuse=3),
            _recipe("recipe-e", "tom", mean_gain=-0.01, n_reuse=5),
        ]

    assert _heuristic(recipes).source_recipe_id == "recipe-d"


@pytest.mark.parametrize(
    "recipe",
    [
        _recipe("recipe-x", "tom", mean_gain=0.5, status="deprecated"),
        _recipe("recipe-x", "tom", mean_gain=0.5, recipe_type="agent_rsi"),
        _recipe("recipe-x", "tom", mean_gain=0.5, recipe_type="interact"),
        _recipe("recipe-x", "empathy", mean_gain=0.5),
    ],
)
def test_heuristic_ignores_recipes_it_cannot_clone(recipe):
    dec = _heuristic(lambda d: d.update(recipes=[recipe]))
    assert (dec.action, dec.mode) == ("new", "batch")


def test_heuristic_ignores_the_prompt_text():
    packed = _packed()
    a = HeuristicBackend().decide(packed, run_id="r", task_id="t")
    b = HeuristicBackend().decide(packed.with_feedback(("attempt 1: whatever",)), run_id="r", task_id="t")
    assert a == b
    assert HeuristicBackend().name == "heuristic"


class _RecordingBridge:
    def __init__(self, inner):
        self.inner = inner
        self.calls = []

    def complete(self, prompt, *, run_id, task_id):
        self.calls.append((prompt, run_id, task_id))
        return self.inner.complete(prompt, run_id=run_id, task_id=task_id)


def test_bridge_backend_sends_the_full_prompt_and_returns_the_text():
    bridge = _RecordingBridge(ScriptedBridge(["{\"a\": 1}"]))
    backend = BridgeDecisionBackend(bridge, name="claude_cli")
    packed = _packed().with_feedback(("attempt 1: parse error: output is empty",))
    assert backend.decide(packed, run_id="run-003", task_id="planner-attempt-2") == '{"a": 1}'
    assert bridge.calls == [(packed.prompt, "run-003", "planner-attempt-2")]
    assert backend.name == "claude_cli"


class _FailingBridge:
    def __init__(self, err):
        self.err = err

    def complete(self, prompt, *, run_id, task_id):
        raise self.err


@pytest.mark.parametrize("category", ["T1", "T2"])
def test_bridge_backend_lets_bridge_errors_through_unchanged(category):
    err = BridgeError(category, "cli timed out")
    backend = BridgeDecisionBackend(_FailingBridge(err))
    with pytest.raises(BridgeError) as info:
        backend.decide(_packed(), run_id="run-003", task_id="t")
    assert info.value is err
    assert info.value.category == category
    assert backend.name == "bridge"


def test_bridge_backend_needs_a_name():
    with pytest.raises(ValueError, match="name"):
        BridgeDecisionBackend(ScriptedBridge([]), name="")


def test_scripted_backend_replays_answers_and_records_calls():
    boom = RuntimeError("boom")
    backend = ScriptedDecisionBackend(["one", boom, "two"])
    packed = _packed()
    assert backend.decide(packed, run_id="r", task_id="a") == "one"
    with pytest.raises(RuntimeError) as info:
        backend.decide(packed, run_id="r", task_id="b")
    assert info.value is boom
    assert backend.decide(packed, run_id="r", task_id="c") == "two"
    assert [c[2] for c in backend.calls] == ["a", "b", "c"]
    with pytest.raises(AssertionError, match="exhausted"):
        backend.decide(packed, run_id="r", task_id="d")
```

- [ ] **Step 2: 跑测试,确认失败**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_tp_backends.py
```

预期:收集阶段报错,`ModuleNotFoundError: No module named 'trainplanner.backends'`,末行 `1 error in …`。

- [ ] **Step 3: 实现**

新建 `src/trainplanner/backends.py`:

```python
"""决策后端:把一份 PackedContext 变成模型输出的文本。三个实现都返回文本,走同一条解析路径。"""
from __future__ import annotations

import json
from typing import Protocol

from inference_bridge import InferenceBridge
from trainplanner.context import RecipeSummary
from trainplanner.prompt import PackedContext

# HeuristicBackend 只 clone batch 配方:AgentRSI 配方构造不成训练指令,interact 默认被领域校验
# 拒绝(allow_interact 默认关),而确定性后端重试也只会给出同一个答案。
CLONABLE_RECIPE_TYPES = ("batch",)


class DecisionBackend(Protocol):
    name: str

    def decide(self, packed: PackedContext, *, run_id: str, task_id: str) -> str: ...


class BridgeDecisionBackend:
    """经外部推理桥接取模型输出。发出去的是 packed.prompt(含前几次被拒的原因)。
    桥接抛出的 BridgeError 原样上抛,不在这里重试或改类别。"""

    def __init__(self, bridge: InferenceBridge, *, name: str = "bridge") -> None:
        if not name:
            raise ValueError("backend name must be non-empty")
        self._bridge = bridge
        self.name = name

    def decide(self, packed: PackedContext, *, run_id: str, task_id: str) -> str:
        return self._bridge.complete(packed.prompt, run_id=run_id, task_id=task_id).text


def _recipe_rank(r: RecipeSummary) -> tuple:
    # 与 recipestore 的 order="track" 一致:mean_gain 降序(None 排最后),n_reuse 降序,id 升序
    return (r.mean_gain is None, -(r.mean_gain or 0.0), -r.n_reuse, r.id)


class HeuristicBackend:
    """纯确定性:不看提示文本,只看 packed.context。

    取一级分数最低的维度(同分按维度名升序);该维度有 effective 的 batch 配方,就 clone
    战绩最好的一条(不扰动);否则 new 一条 batch。
    """

    name = "heuristic"

    def decide(self, packed: PackedContext, *, run_id: str, task_id: str) -> str:
        ctx = packed.context
        scores = ctx.diagnostic.l1_scores
        dimension = min(scores, key=lambda d: (scores[d], d))
        usable = sorted(
            (
                r
                for r in ctx.recipes
                if r.dimension == dimension
                and r.status == "effective"
                and r.recipe_type in CLONABLE_RECIPE_TYPES
            ),
            key=_recipe_rank,
        )
        score = scores[dimension]
        if usable:
            best = usable[0]
            decision = {
                "target_dimension": dimension,
                "action": "clone",
                "mode": None,
                "source_recipe_id": best.id,
                "perturbation": {},
                "data_synthesis_hint": "",
                "rationale": (
                    f"heuristic: {dimension} has the lowest l1 score ({score}); "
                    f"clone {best.id}, the best-tracked effective recipe for it"
                ),
            }
        else:
            decision = {
                "target_dimension": dimension,
                "action": "new",
                "mode": "batch",
                "source_recipe_id": None,
                "perturbation": {},
                "data_synthesis_hint": "",
                "rationale": (
                    f"heuristic: {dimension} has the lowest l1 score ({score}); "
                    "no effective recipe targets it, so start a new batch round"
                ),
            }
        return json.dumps(decision, ensure_ascii=False)


class ScriptedDecisionBackend:
    """测试用:按顺序吐罐头回答。元素是字符串则返回它,是异常实例则抛出它。
    calls 记下每次收到的 (prompt, run_id, task_id),便于断言重试提示的内容。"""

    def __init__(self, responses: list, *, name: str = "scripted") -> None:
        self._responses = list(responses)
        self.name = name
        self.calls: list[tuple[str, str, str]] = []

    def decide(self, packed: PackedContext, *, run_id: str, task_id: str) -> str:
        self.calls.append((packed.prompt, run_id, task_id))
        if not self._responses:
            raise AssertionError("ScriptedDecisionBackend: script exhausted")
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item
```

- [ ] **Step 4: 跑测试,确认通过**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_tp_backends.py
```

预期:`14 passed`,无警告(耗时因机器而异)。

全量:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error
```

预期:`B+165 passed`,无警告(临时拷贝里实测 `1005 passed`)。

- [ ] **Step 5: 提交**

```bash
git add src/trainplanner/backends.py
git add tests/test_tp_backends.py
git commit -m "feat: bridge, heuristic and scripted decision backends for the planner"
```

---

### Task 55: plan():解析、校验、带原因重采样

**Files:**
- Create: `src/trainplanner/planner.py`(94 行)
- Test: `tests/test_tp_planner.py`(21 个用例)

**Interfaces:**
- Consumes:任务 50 的 `PlanningContext`;任务 51 的 `parse_decision`、`DecisionParseError`、`PlannerDecision`;任务 52 的 `render_prompt`;任务 54 的 `DecisionBackend`(测试里还有三个后端与 `PLANNER_V1`);计划 1 的 `BridgeError`、`ScriptedBridge`(测试里)。
- Produces(计划 5 的 `make_plan_handler` 依赖):
  - `plan(ctx, template_text, template_version, backend, *, run_id, max_parse_retries=2, validator=None) -> PlannerResult`。
  - `PlannerResult(decision, rationale, template_version, attempts, backend, rejected=())`(冻结),`record_fields() -> dict`:`{template_version, backend, decision, rationale}`,就是 `planner_decision.payload` 里由规划器负责的四个键。
  - `PlannerFailedError(reason, attempts, *, reasons=())`:属性 `reason`(最后一次被拒的原因)、`attempts`(总尝试次数)、`reasons`(每次的原因,按顺序)。
  - `Validator = Callable[[PlannerDecision], str | None]`。
  - `attempt_task_id(attempt) -> "planner-attempt-<n>"`。

**设计要点:**
- **一次渲染,多次采样。** 模板只渲染一次;第 n 次尝试的提示是同一份 `text` 加上前 n−1 次的被拒原因。渲染错误(`PromptRenderError`)在第一次调用后端之前抛出,不计入重试。
- **共用一个预算。** 总尝试次数 = `1 + max_parse_retries`。解析失败记作 `attempt N: parse error: <原因>`,校验违规记作 `attempt N: invalid decision: <原因>`,两种都消耗预算。
- **`validator` 只在解析成功后调用**,收到 `PlannerDecision`。返回 `None` 表示通过,返回非空字符串表示违规。返回空串、纯空白或非字符串是调用方的 bug,抛 `TypeError`,不当成"通过"。
- **耗尽** 抛 `PlannerFailedError`,`reason` 是最后一次的原因,`reasons` 是全部,便于计划 5 写 `failure: {category: "T3", reason, attempts}`。
- **后端异常原样上抛。** `plan()` 只捕获 `DecisionParseError`。`BridgeError`(不管 T1 还是 T2)和其他异常都直接逃出,后面的尝试不再进行。
- **`task_id`** 每次尝试各不相同(`planner-attempt-1`、`-2`……),桥接按 `(task_id, cost_type)` 记账时(设计 §5.2)每次调用各记一条,不会被去重合并。
- 参数检查:`max_parse_retries` 必须是非负整数(布尔拒绝),`run_id` 非空,否则 `ValueError`。

**Ruling: `max_parse_retries` 计的是"重试"次数,不是总次数;`PlannerFailedError.attempts` 计的是总次数** —— 参数名里的 retries 就是重试;默认 2 即最多问三次 —— 判错的代价:若计划 5 把 `attempts` 当重试次数写进失败记录,会差一。

**Ruling: 后端抛 `BridgeError` 时,不管类别是 T1 还是 T2,`plan()` 都原样上抛,不把 T2 当成解析失败去重试** —— 设计 §5.1 规定"输出格式不合法是调用方的 T2",桥接自己的 T2 指的是桥接层的确定性故障(比如配置错),换个提示也修不好 —— 判错的代价:桥接对某次请求偶发的 T2 会让这次规划直接失败,而不是重采样;由计划 5 决定如何记录。

**Ruling: `validator` 返回空串或非字符串时抛 `TypeError`** —— 空串在布尔上下文里是假,容易被误写成"通过";把它当违规又会给模型一个空原因 —— 判错的代价:计划 5 的 `validate_decision` 必须严格返回 `None` 或非空字符串。

**Ruling: `PlannerResult` 额外带 `rejected`(被拒原因元组),`PlannerFailedError` 额外带 `reasons`** —— 设计没要求,但重试过的规划要能事后看出模型第一次为什么错 —— 判错的代价:无,两个都是附加字段;计划 5 可以不用。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_tp_planner.py`:

```python
import json

import pytest

from inference_bridge import BridgeError, ScriptedBridge
from trainplanner.backends import BridgeDecisionBackend, HeuristicBackend, ScriptedDecisionBackend
from trainplanner.context import PlanningContext
from trainplanner.examples import example_context_dict
from trainplanner.planner import PlannerFailedError, PlannerResult, attempt_task_id, plan
from trainplanner.prompt import PromptRenderError
from trainplanner.templates import PLANNER_V1, PLANNER_V1_VERSION

GOOD = json.dumps({
    "target_dimension": "tom", "action": "new", "mode": "batch", "source_recipe_id": None,
    "perturbation": {}, "data_synthesis_hint": "", "rationale": "tom is lowest",
})
CLONE = json.dumps({
    "target_dimension": "tom", "action": "clone", "mode": None, "source_recipe_id": "recipe-run-001",
    "perturbation": {"grpo_config.learning_rate": 1e-5}, "data_synthesis_hint": "", "rationale": "reuse",
})


@pytest.fixture
def ctx():
    return PlanningContext.from_dict(example_context_dict())


def _plan(ctx, backend, **kw):
    return plan(ctx, PLANNER_V1, PLANNER_V1_VERSION, backend, run_id="run-003", **kw)


def test_a_good_first_answer_is_returned(ctx):
    backend = ScriptedDecisionBackend([GOOD], name="claude_cli")
    result = _plan(ctx, backend)
    assert isinstance(result, PlannerResult)
    assert (result.attempts, result.backend, result.template_version) == (1, "claude_cli", "v1")
    assert result.decision.action == "new"
    assert result.rationale == "tom is lowest"
    assert result.rejected == ()
    prompt, run_id, task_id = backend.calls[0]
    assert (run_id, task_id) == ("run-003", "planner-attempt-1")
    assert "## Rejected attempts" not in prompt


def test_record_fields_match_the_planner_decision_payload(ctx):
    fields = _plan(ctx, ScriptedDecisionBackend([CLONE])).record_fields()
    assert fields == {
        "template_version": "v1",
        "backend": "scripted",
        "decision": {
            "target_dimension": "tom", "action": "clone", "mode": None,
            "source_recipe_id": "recipe-run-001", "perturbation": {"grpo_config.learning_rate": 1e-5},
            "data_synthesis_hint": "",
        },
        "rationale": "reuse",
    }


def test_a_parse_failure_is_fed_back_and_resampled(ctx):
    backend = ScriptedDecisionBackend(["not json", GOOD])
    result = _plan(ctx, backend)
    assert result.attempts == 2
    assert result.rejected == (
        "attempt 1: parse error: output is not a single JSON value: Expecting value at char 0",
    )
    second_prompt = backend.calls[1][0]
    assert second_prompt.endswith(
        "## Rejected attempts\n"
        "Your previous output was rejected for the reasons below. "
        "Fix them and output only one JSON object.\n"
        "- attempt 1: parse error: output is not a single JSON value: Expecting value at char 0\n"
    )
    assert [c[2] for c in backend.calls] == [attempt_task_id(1), attempt_task_id(2)]


def test_a_validator_violation_is_fed_back_and_resampled(ctx):
    seen = []

    def validator(decision):
        seen.append(decision.action)
        return "recipe recipe-run-001 is not effective" if decision.action == "clone" else None

    backend = ScriptedDecisionBackend([CLONE, GOOD])
    result = _plan(ctx, backend, validator=validator)
    assert seen == ["clone", "new"]
    assert result.attempts == 2 and result.decision.action == "new"
    assert "- attempt 1: invalid decision: recipe recipe-run-001 is not effective\n" in backend.calls[1][0]


def test_parse_failures_and_violations_share_one_budget(ctx):
    backend = ScriptedDecisionBackend(["oops", CLONE, "{}", GOOD])
    with pytest.raises(PlannerFailedError) as info:
        _plan(ctx, backend, validator=lambda d: "no clones today")
    err = info.value
    assert err.attempts == 3
    assert err.reason == "attempt 3: parse error: missing keys: " + str([
        "target_dimension", "action", "mode", "source_recipe_id",
        "perturbation", "data_synthesis_hint", "rationale",
    ])
    assert [r.split(":")[1].strip() for r in err.reasons] == ["parse error", "invalid decision", "parse error"]
    assert len(backend.calls) == 3  # 第四个罐头回答没被取走
    third_prompt = backend.calls[2][0]
    assert "- attempt 1: parse error:" in third_prompt
    assert "- attempt 2: invalid decision: no clones today" in third_prompt


def test_zero_retries_means_one_attempt(ctx):
    with pytest.raises(PlannerFailedError) as info:
        _plan(ctx, ScriptedDecisionBackend(["nope", GOOD]), max_parse_retries=0)
    assert info.value.attempts == 1
    assert str(info.value).startswith("planner gave up after 1 attempts: attempt 1: parse error")


def test_more_retries_allow_more_attempts(ctx):
    result = _plan(ctx, ScriptedDecisionBackend(["a", "b", "c", "d", GOOD]), max_parse_retries=4)
    assert result.attempts == 5


@pytest.mark.parametrize("bad", [-1, 1.5, True, None])
def test_the_retry_limit_must_be_a_non_negative_integer(ctx, bad):
    with pytest.raises(ValueError, match="max_parse_retries"):
        _plan(ctx, ScriptedDecisionBackend([GOOD]), max_parse_retries=bad)


def test_run_id_is_required(ctx):
    with pytest.raises(ValueError, match="run_id"):
        plan(ctx, PLANNER_V1, "v1", ScriptedDecisionBackend([GOOD]), run_id="")


class _GarbageThenFailBridge:
    """第一次返回不合规文本,第二次抛 BridgeError,之后若还被调用就说明 plan 吞了异常。"""

    def __init__(self, err):
        self.err = err
        self.calls = 0

    def complete(self, prompt, *, run_id, task_id):
        self.calls += 1
        if self.calls == 1:
            return ScriptedBridge(["garbage"]).complete(prompt, run_id=run_id, task_id=task_id)
        if self.calls == 2:
            raise self.err
        raise AssertionError("plan kept sampling after a BridgeError")


@pytest.mark.parametrize("category", ["T1", "T2"])
def test_a_bridge_error_propagates_unchanged_and_ends_the_attempts(ctx, category):
    # T2 也原样上抛,不当成解析失败去重采样(见本任务第二条 Ruling)
    err = BridgeError(category, "cli exited 1")
    bridge = _GarbageThenFailBridge(err)
    with pytest.raises(BridgeError) as info:
        _plan(ctx, BridgeDecisionBackend(bridge))
    assert info.value is err
    assert info.value.category == category
    assert bridge.calls == 2


def test_a_template_error_is_not_retried(ctx):
    backend = ScriptedDecisionBackend([GOOD])
    with pytest.raises(PromptRenderError):
        plan(ctx, "{{budget}}", "v1", backend, run_id="run-003")
    assert backend.calls == []


@pytest.mark.parametrize("bad", ["", "   ", 3])
def test_a_validator_must_return_none_or_a_reason(ctx, bad):
    with pytest.raises(TypeError, match="validator"):
        _plan(ctx, ScriptedDecisionBackend([GOOD]), validator=lambda d: bad)


def test_the_heuristic_backend_plans_through_the_same_path(ctx):
    result = _plan(ctx, HeuristicBackend())
    assert (result.backend, result.attempts) == ("heuristic", 1)
    assert (result.decision.action, result.decision.source_recipe_id) == ("clone", "recipe-run-001")


def test_the_bridge_backend_plans_end_to_end_with_a_fenced_answer(ctx):
    bridge = ScriptedBridge(["```json\n" + GOOD + "\n```"])
    result = _plan(ctx, BridgeDecisionBackend(bridge, name="claude_cli"))
    assert (result.backend, result.decision.mode) == ("claude_cli", "batch")


def test_the_planner_does_not_mutate_the_context(ctx):
    before = ctx.to_dict()
    _plan(ctx, ScriptedDecisionBackend(["x", GOOD]))
    assert ctx.to_dict() == before
```

- [ ] **Step 2: 跑测试,确认失败**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_tp_planner.py
```

预期:收集阶段报错,`ModuleNotFoundError: No module named 'trainplanner.planner'`,末行 `1 error in …`。

- [ ] **Step 3: 实现**

新建 `src/trainplanner/planner.py`:

```python
"""plan():渲染一次提示,向后端要输出,严格解析,可选做领域校验;不合格就带着原因重新采样。

解析失败与校验违规共用一个重试上限:总尝试次数 = 1 + max_parse_retries。耗尽抛
PlannerFailedError。后端抛出的任何异常(包括 BridgeError)原样上抛,由调用方决定。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from trainplanner.backends import DecisionBackend
from trainplanner.context import PlanningContext
from trainplanner.decision import DecisionParseError, PlannerDecision, parse_decision
from trainplanner.prompt import render_prompt

Validator = Callable[[PlannerDecision], str | None]


class PlannerFailedError(Exception):
    """重试耗尽仍没有合格决策。reason 是最后一次被拒的原因,attempts 是总尝试次数,
    reasons 按顺序记下每一次被拒的原因。调用方按 T3 处理(设计 §7.2)。"""

    def __init__(self, reason: str, attempts: int, *, reasons: tuple[str, ...] = ()) -> None:
        super().__init__(f"planner gave up after {attempts} attempts: {reason}")
        self.reason = reason
        self.attempts = attempts
        self.reasons = tuple(reasons) if reasons else (reason,)


@dataclass(frozen=True)
class PlannerResult:
    decision: PlannerDecision
    rationale: str
    template_version: str
    attempts: int
    backend: str
    rejected: tuple[str, ...] = ()

    def record_fields(self) -> dict:
        """planner_decision 产物 payload 里由规划器负责的四个键(设计 §4.2)。"""
        return {
            "template_version": self.template_version,
            "backend": self.backend,
            "decision": self.decision.decision_payload(),
            "rationale": self.rationale,
        }


def attempt_task_id(attempt: int) -> str:
    """第 attempt 次(从 1 起)后端调用的 task_id。每次尝试各一个,记账时不会被去重合并。"""
    return f"planner-attempt-{attempt}"


def plan(
    ctx: PlanningContext,
    template_text: str,
    template_version: str,
    backend: DecisionBackend,
    *,
    run_id: str,
    max_parse_retries: int = 2,
    validator: Validator | None = None,
) -> PlannerResult:
    if isinstance(max_parse_retries, bool) or not isinstance(max_parse_retries, int) or max_parse_retries < 0:
        raise ValueError(f"max_parse_retries must be a non-negative integer, got {max_parse_retries!r}")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run_id must be a non-empty string")
    base = render_prompt(ctx, template_text, template_version)
    rejected: list[str] = []
    total = 1 + max_parse_retries
    for attempt in range(1, total + 1):
        packed = base.with_feedback(tuple(rejected))
        text = backend.decide(packed, run_id=run_id, task_id=attempt_task_id(attempt))
        try:
            decision = parse_decision(text)
        except DecisionParseError as exc:
            rejected.append(f"attempt {attempt}: parse error: {exc.reason}")
            continue
        if validator is not None:
            violation = validator(decision)
            if violation is not None:
                if not isinstance(violation, str) or not violation.strip():
                    raise TypeError("validator must return None or a non-empty violation string")
                rejected.append(f"attempt {attempt}: invalid decision: {violation}")
                continue
        return PlannerResult(
            decision=decision,
            rationale=decision.rationale,
            template_version=template_version,
            attempts=attempt,
            backend=backend.name,
            rejected=tuple(rejected),
        )
    raise PlannerFailedError(rejected[-1], total, reasons=tuple(rejected))
```

- [ ] **Step 4: 跑测试,确认通过**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_tp_planner.py
```

预期:`21 passed`,无警告(耗时因机器而异)。

全量:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error
```

预期:`B+186 passed`,无警告(临时拷贝里实测 `1026 passed`)。

- [ ] **Step 5: 提交**

```bash
git add src/trainplanner/planner.py
git add tests/test_tp_planner.py
git commit -m "feat: plan() with strict parsing, validation feedback and a shared retry budget"
```

---

### Task 56: 边界测试与全量回归

**Files:**
- Test: `tests/test_tp_boundaries.py`(3 个用例)

**Interfaces:**
- Consumes:`src/trainplanner/*.py`、`src/controlplane/*.py` 的源码(用 `ast` 读,不导入)。
- Produces:无新名字。

**设计要点:**
- 三条机器可查的约束:`trainplanner` 只导入 `trainplanner`、`controlplane`、`inference_bridge` 与七个标准库模块;`controlplane` 不导入 `trainplanner`;`trainplanner` 每个模块的第一条语句(跳过 docstring)是 `from __future__ import annotations`。
- 标准库白名单是显式列出的,将来新增一个标准库导入也要改这个测试。这是刻意的:要防的是"顺手导入 `simulator`、`recipestore`"这类越界。
- 这些测试写完就是通过的(它们钉住的是前面任务已经满足的性质)。为了证明它们真能抓住越界,第 2 步做一次变异检查。

- [ ] **Step 1: 写测试**

新建 `tests/test_tp_boundaries.py`:

```python
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
ALLOWED = {"trainplanner", "controlplane", "inference_bridge"}
STDLIB_OK = {"__future__", "collections", "copy", "dataclasses", "json", "math", "re", "typing"}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            out.add(node.module.split(".")[0])
    return out


def test_trainplanner_imports_only_controlplane_inference_bridge_and_stdlib():
    for path in sorted((SRC / "trainplanner").glob("*.py")):
        extra = _imports(path) - ALLOWED - STDLIB_OK
        assert not extra, f"{path.name} imports {sorted(extra)}"


def test_controlplane_does_not_import_trainplanner():
    for path in sorted((SRC / "controlplane").glob("*.py")):
        assert "trainplanner" not in _imports(path), path.name


def test_every_trainplanner_module_starts_with_future_annotations():
    for path in sorted((SRC / "trainplanner").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        body = tree.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]
        first = body[0]
        assert isinstance(first, ast.ImportFrom) and first.module == "__future__", path.name
```

- [ ] **Step 2: 变异检查(证明测试会失败)**

临时往 `examples.py` 末尾加一条越界导入,跑测试,再撤掉。追加与撤销都用 Python,追加的文本自带前导换行 `"\nimport simulator.datamodel\n"`,不依赖文件原本是否以换行结尾;撤销时去掉同一段文本,文件恢复到逐字节原样:

```bash
uv run python -c "from pathlib import Path; p = Path('src/trainplanner/examples.py'); p.write_text(p.read_text(encoding='utf-8') + '\nimport simulator.datamodel\n', encoding='utf-8')"
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_tp_boundaries.py
uv run python -c "from pathlib import Path; p = Path('src/trainplanner/examples.py'); s = p.read_text(encoding='utf-8'); assert s.endswith('\nimport simulator.datamodel\n'); p.write_text(s.removesuffix('\nimport simulator.datamodel\n'), encoding='utf-8')"
git diff --stat src/trainplanner/examples.py
```

预期:第二条命令输出 `AssertionError: examples.py imports ['simulator']` 和 `1 failed, 2 passed`;最后一条无输出(变异已撤掉)。

- [ ] **Step 3: 跑测试,确认通过**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_tp_boundaries.py
```

预期:`3 passed`,无警告(耗时因机器而异)。

- [ ] **Step 4: 全量回归**

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error
```

预期:`B+189 passed`,无警告(临时拷贝里实测 `1029 passed`)。再单独跑本计划的七个测试文件:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest -q -p no:cacheprovider --color=no -W error tests/test_tp_context.py tests/test_tp_decision.py tests/test_tp_prompt.py tests/test_tp_templates.py tests/test_tp_backends.py tests/test_tp_planner.py tests/test_tp_boundaries.py
```

预期:`189 passed`。

- [ ] **Step 5: 核对改动范围**

`BASE` 是任务 50 第 1 步记下的提交。先提交本任务,再看从 `BASE` 起的改动:

```bash
git add tests/test_tp_boundaries.py
git commit -m "test: pin the trainplanner import boundaries and module headers"
```

```bash
git diff --stat "$BASE"..HEAD -- . ':(exclude)docs/superpowers'
git diff --stat "$BASE"..HEAD -- . ':(exclude)docs/superpowers' | grep -c 'cli/package' || true
git status --short
```

(`BASE` 取自执行记录。)比较对象是两个提交,不是工作区,所以本计划开始前就存在的未提交改动不会混进来。预期第一条命令恰好列出下面 16 个文件,`pyproject.toml` 是 `1 insertion(+), 1 deletion(-)`,其余都是新文件:

```
 pyproject.toml                |   2 +-
 src/trainplanner/__init__.py  |   5 +
 src/trainplanner/backends.py  | 111 +++++++++++++
 src/trainplanner/context.py   | 383 ++++++++++++++++++++++++++++++++++++++++++
 src/trainplanner/decision.py  | 205 ++++++++++++++++++++++
 src/trainplanner/examples.py  |  64 ++++++++
 src/trainplanner/planner.py   |  94 +++++++++++
 src/trainplanner/prompt.py    | 131 ++++++++++++++++
 src/trainplanner/templates.py | 153 +++++++++++++++++
 tests/test_tp_backends.py     | 142 ++++++++++++++++
 tests/test_tp_boundaries.py   |  38 +++++
 tests/test_tp_context.py      | 190 +++++++++++++++++++++
 tests/test_tp_decision.py     | 201 ++++++++++++++++++++++
 tests/test_tp_planner.py      | 186 ++++++++++++++++++++
 tests/test_tp_prompt.py       | 125 ++++++++++++++
 tests/test_tp_templates.py    | 140 +++++++++++++++
 16 files changed, 2169 insertions(+), 1 deletion(-)
```

第二条命令预期输出 `0`:提交里不得有 `cli/package.json`、`cli/package-lock.json`。不是 `0` 就说明误提交了这两个文件,停下报告。

若 `BASE` 之后还有别的计划的提交(并行执行时),列表里会多出那些文件;只核对上面 16 个文件都在、且没有别的 `src/trainplanner` 之外由本计划引入的改动。`git status --short` 里允许出现的只有本计划开始前就存在的未提交改动(`cli/package*.json` 等),不得有本计划留下的未提交文件。

---

## 完成标准

七个任务都提交后:

- 全量命令为 **B+189 通过**、无警告(B 是任务 50 第 1 步实测的起点;在 `0385004` 加计划 1 临时桩的拷贝里实测为 1029)。
- `git diff --stat "$BASE"..HEAD -- . ':(exclude)docs/superpowers'` 只涉及 File Structure 表里的 16 个文件,不含 `src/controlplane`、`src/inference_bridge`、`schemas/`;对它的输出 `grep -c 'cli/package'` 为 `0`。
- `src/controlplane` 下没有任何 `trainplanner` 的导入;`src/trainplanner` 只导入 `controlplane`、`inference_bridge` 与标准库(`tests/test_tp_boundaries.py` 钉住)。
- 下面这段在仓库根目录跑得通,输出 `clone recipe-run-001 1 heuristic v1`:

```bash
PYTHONDONTWRITEBYTECODE=1 uv run --extra dev python -c "
from trainplanner.backends import HeuristicBackend
from trainplanner.context import PlanningContext
from trainplanner.examples import example_context_dict
from trainplanner.planner import plan
from trainplanner.templates import PLANNER_V1, PLANNER_V1_VERSION
r = plan(PlanningContext.from_dict(example_context_dict()), PLANNER_V1, PLANNER_V1_VERSION, HeuristicBackend(), run_id='run-003')
print(r.decision.action, r.decision.source_recipe_id, r.attempts, r.backend, r.template_version)
"
```

## 已知限制

1. **规划质量没有证据。** 罐头后端只能证明流程、解析与重试正确;`PLANNER_V1` 的措辞没有用真实模型试过,外部模型给出的决策好不好不知道(设计 §14 第 3、5 条)。
2. **启发式很粗。** `HeuristicBackend` 只看一级分数与配方战绩,不看退化标记、错误画像、候选是否被拒、成本(成本大多是 `"not recorded"`,反正也比不了);被拒后可能反复 clone 同一条配方,直到战绩回写把它 `deprecated`。
3. **只 clone batch 配方。** 开了 `allow_interact` 之后,启发式也不会复用 interact 配方。
4. **提示可能很长。** 上下文整段以 JSON 渲染进提示,没有截断;配方与历史决策的条数由计划 5 的装配方控制(近 K 轮)。超过命令行工具的上下文上限时,表现为桥接的 T1 或 T2,本计划不处理。
5. **代码围栏的容忍度是固定的。** 只认 ```` ``` ```` 与 ```` ```json ````;其他写法(```` ```JSON ````、前言文字)一律算解析失败,耗一次重试。
6. **同步阻塞。** `plan()` 是同步的,外部推理期间调和循环被阻塞(设计 §7.2、§14 第 6 条),本计划不改变这一点。
7. **模板版本号。** 实际记录的 `template_version` 是 `"v1"`。设计 §4.2 写的是 `v1`;`planner-v1` 只出现在计划 3 的测试夹具里(另行修正),不要照抄它作版本号(见任务 53 的 Ruling)。
8. **不写 `planner_decision`。** 决策产物、`next_run_id` 分配、失败记录、成本记账都是计划 5 的事;本计划只给出 `PlannerResult.record_fields()` 与 `PlannerFailedError` 的字段。
9. **配方类型集合有两份字面量。** `trainplanner.context.RECIPE_TYPES` 与计划 3 `recipestore` 的 `RECIPE_TYPES` 都是 `("batch", "interact", "agent_rsi")`。`trainplanner` 不导入 `recipestore`,所以没法共用一份;改一处必须同步另一处,目前没有测试钉住两者一致。本计划里配方类型字面量出现在:`context.py` 的 `RECIPE_TYPES` 定义与 `RecipeSummary.recipe_type` 的注释;`backends.py` 的 `CLONABLE_RECIPE_TYPES = ("batch",)`(它的子集);`tests/test_tp_backends.py` 的 `_recipe(..., recipe_type="agent_rsi")`、`recipe_type="interact"` 两个参数化用例;`tests/test_tp_context.py` 的 `recipe_type="weird"` 用例(只用非法值)。`decision.py` 的 `MODES = ("batch", "interact")` 是指令模式,不是配方类型,不在此列。两份字面量的一致性测试放在计划 9。

10. **扰动白名单的权威清单在计划 5。** `PLANNER_V1` 只写扰动键的格式(训练配置里的点路径)与"不在白名单就被拒"的规则,举了两个默认白名单里确有的键作例子,不枚举全部键;`scheduler.yaml` 的 `perturbable` 被人改了之后,模板不会跟着变,模型只能靠被拒原因(计划 5 的违规信息会列出允许的键)纠正。

## 未验证项

1. **计划 1 的真实接口:已按计划 1 的真实签名核对。** 对照 `2026-10-09-bridge-and-foundations.md` 任务 25(以及它依赖的任务 21 `ledger`、任务 19 路径函数)的代码块:`BridgeError(category, reason="", *, attempts=0, log_ref=None)`、`ScriptedBridge(script, *, model="scripted", store=None)`、`ScriptExhaustedError`、`planner_prompt_file(version)`。临时拷贝里的桩就是这些代码块逐字拷贝,本计划七个测试文件在它上面全部通过。改动 5 处 `BridgeError` 构造(前置检查与"对计划 1 的最小假设"第 3 条、依赖表、两个测试文件各一处),全部改成 `category` 在前的位置参数形式;`ScriptedBridge` 只按 `ScriptedBridge([str, ...])` 使用,不依赖单参数之外的构造方式,也不依赖 `ScriptExhaustedError`。尚未核对的只剩"计划 1 实施时的代码是否与它的计划文本一致",任务 50 第 1 步的前置检查会抓到不一致;不一致时先停下报告,不要改本计划去迁就。
2. **累计测试数的起点。** 只在 `0385004` 加桩的拷贝里实测过(840 起,1029 止)。计划 1(以及先落地的计划 2、3)会改变起点 B,本计划只保证增量为 +48、+61、+19、+23、+14、+21、+3。
3. **`BridgeError` 的构造方式:已按计划 1 的真实签名核对。** 测试里一律写 `BridgeError("T1", "…")`(`category` 第一个位置参数、`reason` 第二个),前置检查写 `BridgeError('T1', 'down')`。在与计划 1 任务 25 代码块逐字一致的桩上跑通。
4. **`PLANNER_V1` 对真实命令行模型的效果**:没有调用过 `claude -p` 或 `codex exec`,不知道它们是否稳定只输出一个对象、是否加围栏。
5. **`git diff --stat "$BASE"..HEAD -- . ':(exclude)docs/superpowers'` 的行数。** 在拷贝里用一次性 git 仓库模拟两次提交得到;实际仓库里如果 `BASE` 之后混入别的计划的提交,列表会更长。`docs/superpowers/` 下的设计与计划文档由 pathspec 排除。
6. **任务 50 第 6 步的输出。** 预期输出只约定末尾是 `"src/trainplanner"]`,不约定前面各包的顺序;脚本幂等,重复运行或别的计划先追加过都打印 `already listed` 并正常退出。
7. **`cost_gain` 的未记录写法。** 设计 §14 第 7 条已区分两处:`run_trace` 的 `cost_records` 省略未记录的键,`PlanningContext.cost_gain` 键必须在场、值为 `None`,本计划与之一致(见对外契约一节的 Ruling)。计划 5 的 `assemble_context` 必须显式写 `None`,省略键会被 `from_dict` 以缺键拒绝;计划 5 是否照此产出,本计划无法验证。
