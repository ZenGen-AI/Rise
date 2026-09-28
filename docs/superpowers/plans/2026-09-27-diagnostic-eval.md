# DiagnosticEval 诊断评测体系 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 RismForge 的诊断评测体系(spec §2.5)——全工厂的"导航仪"。加载候选 checkpoint,跑**双轨诊断**(静态轨集成 SoMEval 适配层 + 动态轨复用 SocialSimulator 的 `run_episode`),经统一打分器(规则式 + LLM-as-judge)、错误归类器、带置信区间的统计聚合、退化判定,产出结构化 `DiagnosticReport` 写入 ControlPlane。全部逻辑核心为纯函数/可 mock,不需 GPU、不需真实 SoMEval 调用、不需 gRPC,用 fixture 测试(spec §2.5.10)。

**Architecture:** 计算层子系统,与 SocialSimulator 同属 **Python 侧**。诊断评测器不直接调用任何 TypeScript,只通过 ControlPlane 的 git 树 YAML 产物 + `schemas/` 语言中立 JSON Schema 与其余子系统通信。双轨输出在"错误归类器 + G 组探针 + 隔离校验器"层汇合成统一 `DiagnosticReport`(spec §2.5.2 架构图)。静态轨的 SoMEval 是执行层基础设施,通过薄适配层集成(子进程/库),其原始输出转成本项目 `ItemResult` 格式;适配层用 SoMEval 固定输出样本 mock 测试,不依赖真实调用(spec §2.5.10)。动态轨复用 Plan 2 已合入 main 的纯函数 `run_episode`,角色分配为"候选 checkpoint vs 标准化对手人格池"(spec §2.5.2)。

**Tech Stack:**
- Python 3.11+ / uv(包管理)/ pytest — 全部诊断评测模块
- 置信区间:**stdlib `math` 手写 Wilson score interval**,零新依赖(与项目"依赖最小化"一致;§2.5.5 要求分数带不确定性)
- YAML(产物序列化,pyyaml)/ JSON Schema(校验,jsonschema)— 复用 Plan 1 信封基础设施
- LLM-as-judge:V1 通过 `JudgeBackend` Protocol 抽象,真实调用留到接入外部推理桥接的计划;本计划用确定性 mock judge 测试(§2.5.10 "judge 部分整体 mock")

**Spec:**
- `docs/superpowers/specs/2026-09-24-rismforge-subsystem-design.md`(七子系统详细设计,本计划实现其 **§2.5 诊断评测体系**)
- `docs/superpowers/specs/2026-09-22-rismforge-architecture-design.md`(总体架构)

**依赖的既有实现(Plan 1 ControlPlane + Plan 2 SocialSimulator,均已合入 main):**
- `controlplane.envelope`:`Envelope(id, type, version, created_at, created_by, status, schema_version, links=[], payload={})`;`OwnerRef()` 默认 `owner="default"`/`visibility="shared"`;`BigRef(uri, sha256, size_bytes, fetch_hint=None)`。均带 `to_dict`/`from_dict`。
- `controlplane.gitstore.GitStore(repo_root)`:`.init()`、`.exists(rel)`、`.write_product(rel, env, message)`(幂等:逐字节相同不提交)、`.read_product(rel) -> Envelope`(经 `migration.load_product` 迁移+校验)、`.list_products(rel_dir) -> list[str]`。
- `controlplane.paths`:`CONTROL_PLANE_ROOT="control-plane"`、`SUBDIRS`、`run_dir(run_id)`、`model_file(n)`。**本计划新增** `diagnostic_report_file(run_id)` 与诊断基准子树路径助手。
- `controlplane.validate.validate_envelope(d)`;`controlplane.migration.load_product(d)`(读取唯一入口);envelope schema 位于 `schemas/envelope.schema.json`,`type`/`status` 为**开放字符串**(非闭合枚举)——新增产物类型 `diagnostic_report`/`diagnostic_taxonomy`/`diagnostic_item_pool` 零 schema 改动即可校验通过。
- `simulator.runner.run_episode(scenario, assignment, personas, backends, episode_id) -> Episode`(纯函数;**本计划扩展**其签名加可选 `stop_predicate` 支持早停,向后兼容)。`RoleSlotMismatchError`。
- `simulator.datamodel`:`Persona/RoleSlot/Scenario/RoleBinding/RoleAssignment/TurnRecord/Episode`(均带 `to_dict`/`from_dict`);`TurnRecord.activation_trace: dict|None`(§2.6.3 保留字段,裸模型恒 None)。
- `simulator.backends`:`ModelBackend` Protocol(`respond(persona, scenario, history, speaker_slot) -> tuple[str, str|None]`)、`GenerationBackend` Protocol、`ScriptedBackend(scripted: dict)`、`TemplateGenerationBackend()`。

---

## Global Constraints

每个任务的要求都隐含包含本节。以下值直接取自 spec,不可改动:

- **语言分工**:诊断评测器全部用 **Python 3.11+ / uv / pytest**,与 SocialSimulator 同侧。不直接调用任何 TypeScript,只经 git 树 YAML 产物 + `schemas/` JSON Schema 通信(spec §1 总原则)。
- **脱离真实环境可测**(spec §2.5.10,硬约束):题目执行器用固定 fixture 题目 + 罐头模型输出测试;打分器规则式部分纯逻辑单测,judge 部分整体 mock;错误归类器给定构造好的变体族通过模式验证;统计聚合器/退化判定器用合成分数分布测试;隔离校验器用构造的相似/不相似样本对验证;SoMEval 适配层用 SoMEval 固定输出样本 mock。**全部不需要 GPU、不需要真实 SoMEval、不需要 gRPC。**
- **分数必带不确定性**(spec §2.5.5,硬约束):每题采样 k 次得通过率和方差,逐层聚合到 L2/L1 时带置信区间(Wilson score interval)。**分数差异若在置信区间内,不能被当作"提升"**——这条直接约束退化判定器(不能拿噪声当信号)。
- **两轨分数不可无痕合并**(spec §2.5.9,硬约束):`DimensionScore` 必带**得分来源轨道标注**(静态/动态/融合),否则只在动态交互中才暴露的缺陷可能被静态轨的高分掩盖。
- **基准版本化**(spec §2.5.8,硬约束):基准版本 = taxonomy 版本 + 题目池版本 + SoMEval 集成版本 + judge prompt 版本 + 打分配置版本,任一变动即升版。**跨版本分数不可直接比较**——`DiagnosticReport` 必带版本号,退化判定只在同版本内做对比,跨版本明确标注"不可比"。
- **评测集隔离**(spec §2.5.7,硬约束):**隔离校验器**作为独立模块暴露给情境生成器:训练情境批量生成后、写入 ControlPlane 前,必须过一次与诊断题目池的相似度校验(语义相似度 + 关键结构匹配),命中样本剔除并记录。这是硬约束,不是可选优化。
- **judge 模型约束**(spec §2.5.2):LLM-as-judge 的模型不能是被训练模型自身或其近亲版本(自评偏差);judge 的 rubric prompt 版本化,配小型校准集定期检查 judge 与人工一致率——judge 漂移本身是需要被监控的退化。V1 用 `JudgeBackend` Protocol 抽象,mock 测试。
- **G 组探针**(spec §2.5.1):基础推理/指令遵循/格式合规,与 SoMBench 三层维度正交。**G 组不参与社会心智能力得分,唯一用途是退化监控**(OPD 是否失效的信号)。
- **凭证绝不进 git 树**(spec 补充 G.3,安全关键):git 树里的配置只允许出现凭证的**变量名引用**(如 `judge_token_env: JUDGE_API_KEY`),绝不出现凭证值。judge 后端配置进 git 树 `control-plane/config/`,凭证走环境变量;凭证解析失败必须报明确错误,禁止静默降级或发送空 token。`.gitignore` 已排除 `.env`/`*.local.yaml`/`secrets/`,本计划不得弱化。
- **退化判定只产标记不做决定**(spec §2.5.6):退化判定器只产出标记和证据,不做晋级决定——决定仍归审核门①的人工。
- **读取产物走迁移器链**(spec 补充 F.5 税④):读取任一产物必须经 `migration.load_product`(迁移→校验→反序列化),禁止直接反序列化。写入产物用 `Envelope` + `GitStore.write_product`。
- **纯函数优先**:聚合器、退化判定器、错误归类器、统计层、Wilson 区间全部设计为纯函数;有副作用的只有 SoMEval 适配层(子进程,mock)、judge 后端(网络,mock)、store 适配层(git,tmp_path)。
- **标识与命名**:维度/轨道/错误类型/状态用英文小写下划线标识符。轨道枚举 `"static"|"dynamic"|"fused"`;错误类型 `"reasoning_break"|"surface_match"|"format_fail"|"refusal"|"unstable"`(动态轨加 `"inconsistency"`)。

---

## File Structure

**Python 包 `src/diagnostic/`(诊断评测器)**
- `datamodel.py` — 数据模型(§2.5.9):`DiagnosticTaxonomy`/`DiagnosticItem`/`VariantFamily`/`ItemResult`/`ErrorProfile`/`DimensionScore`/`RegressionFlags`/`ConsistencySignals`/`DiagnosticReport`,均带 `to_dict`/`from_dict`
- `benchmark_version.py` — 基准版本聚合(§2.5.8):`BenchmarkVersion` 结构 + 可比性判定纯函数
- `stats.py` — 统计层(§2.5.5):Wilson score interval + 通过率/方差聚合,纯函数,stdlib only
- `taxonomy_loader.py` — 基准加载器(§2.5.4):从版本化配置产物加载 taxonomy + 题目池 + 分档选择(快速筛查集/完整诊断集)
- `someval_adapter.py` — 静态轨 SoMEval 薄适配层(§2.5.2):调 SoMEval(子进程/库)→ 转 `ItemResult`;`SoMEvalRunner` Protocol + mock 实现
- `scorer.py` — 打分器(§2.5.4):`RuleScorer`(规则式,客观答案题,纯逻辑)+ `JudgeBackend` Protocol + `LLMJudgeScorer`(开放题+动态轨,rubric 驱动,mock)
- `dynamic_track.py` — 动态轨(§2.5.2):复用 `run_episode`,角色分配"候选 vs 对手人格池",产出目标达成度/多轮一致性信号
- `error_classifier.py` — 错误归类器(§2.5.3):按变体族通过模式归类错误类型(静态)/ 多轮一致性分析(动态)
- `aggregator.py` — 统计聚合器(§2.5.5):L3→L2→L1 逐层聚合,带置信区间,标注轨道来源
- `regression.py` — 对比与退化判定器(§2.5.6):对照当前晋级基座,产出退化标记 + 证据 + reward hacking 嫌疑信号
- `report.py` — 报告生成器:组装 `DiagnosticReport`
- `isolation.py` — 隔离校验器(§2.5.7):相似度校验(语义 + 关键结构),独立暴露给情境生成器
- `paths.py` — 诊断产物路径助手(扩展,见下)
- `store_adapter.py` — 写/读 `DiagnosticReport`/`DiagnosticTaxonomy`/题目池到 ControlPlane
- `orchestrator.py` — 顶层编排:加载→双轨→打分→归类→聚合→退化判定→报告(一次完整诊断)

**扩展既有(Plan 1/2,已合入 main)**
- `src/controlplane/paths.py` — 新增 `SUBDIRS += ["diagnostics"]`;新增 `diagnostic_report_file(run_id)`、`taxonomy_file(version)`、`item_pool_file(version)`
- `src/simulator/runner.py` — `run_episode` 新增可选参数 `stop_predicate: Callable | None = None` + 早停 `terminated_reason="goal_reached"`(向后兼容:不传则行为完全不变)
- `src/simulator/stubs.py` — 新增 `diagnostic_evaluator_stub(run_id, candidate_id) -> Envelope`(可选,e2e 用)

**测试**
- `tests/test_diag_*.py`(pytest,每模块一文件,tmp_path 隔离)

---

### Task 1: 诊断评测数据模型(§2.5.9 九个实体)

**Files:** `src/diagnostic/__init__.py`(空), `src/diagnostic/datamodel.py`, `tests/test_diag_datamodel.py`

**Step 1 — 写失败测试** `tests/test_diag_datamodel.py`:

```python
from diagnostic.datamodel import (
    VariantFamily, DiagnosticItem, DiagnosticTaxonomy,
    ItemResult, ErrorProfile, DimensionScore,
    RegressionFlags, ConsistencySignals, DiagnosticReport,
)


def test_variant_family_round_trip():
    vf = VariantFamily(
        family_id="vf-001",
        base_item_id="item-001",
        variant_item_ids=["item-001-v1", "item-001-v2"],
    )
    assert VariantFamily.from_dict(vf.to_dict()) == vf


def test_diagnostic_item_round_trip_with_track_and_scoring_mode():
    item = DiagnosticItem(
        id="item-001",
        l3_task="false_belief_transfer",
        item_type="mcq",
        scoring_mode="rule",           # "rule" | "judge"
        track="static",                # "static" | "dynamic"
        reference_answer="B",
        rubric=None,
        family_id="vf-001",
    )
    assert DiagnosticItem.from_dict(item.to_dict()) == item


def test_diagnostic_item_judge_open_ended():
    item = DiagnosticItem(
        id="item-002",
        l3_task="persuasion_maintenance",
        item_type="open",
        scoring_mode="judge",
        track="dynamic",
        reference_answer=None,
        rubric={"criteria": ["goal_completion", "relationship", "social_norm"]},
        family_id=None,
    )
    assert DiagnosticItem.from_dict(item.to_dict()) == item


def test_taxonomy_round_trip_with_g_probes():
    tax = DiagnosticTaxonomy(
        version="tax-v1",
        l1_dimensions=["mental_representation", "social_interaction", "norm_internalization"],
        l2_dimensions={"mental_representation": ["belief", "intention"]},
        l3_tasks={"belief": ["false_belief_transfer"]},
        g_probes=["basic_reasoning", "instruction_following", "format_compliance"],
    )
    assert DiagnosticTaxonomy.from_dict(tax.to_dict()) == tax


def test_item_result_carries_k_samples_pass_rate_variance():
    r = ItemResult(
        item_id="item-001",
        raw_outputs=["B", "B", "A", "B"],
        per_sample_scores=[1.0, 1.0, 0.0, 1.0],
        pass_rate=0.75,
        variance=0.1875,
        track="static",
    )
    assert ItemResult.from_dict(r.to_dict()) == r
    assert r.n_samples == 4


def test_dimension_score_requires_track_provenance():
    ds = DimensionScore(
        dimension="mental_representation",
        level="l1",
        score=0.82,
        ci_low=0.75,
        ci_high=0.89,
        sample_size=120,
        track_provenance="fused",       # "static" | "dynamic" | "fused" — 硬约束
    )
    assert DimensionScore.from_dict(ds.to_dict()) == ds


def test_error_profile_round_trip():
    ep = ErrorProfile(
        l2_dimension="belief",
        error_type_distribution={
            "reasoning_break": 0.3, "surface_match": 0.5,
            "format_fail": 0.1, "refusal": 0.05, "unstable": 0.05,
        },
        track="static",
    )
    assert ErrorProfile.from_dict(ep.to_dict()) == ep


def test_regression_flags_each_flag_carries_evidence():
    rf = RegressionFlags(flags=[
        {"dimension": "empathy", "kind": "social_regression",
         "drop": 0.06, "baseline_version": "zengen-v3", "beyond_ci": True},
    ])
    assert RegressionFlags.from_dict(rf.to_dict()) == rf


def test_consistency_signals_round_trip():
    cs = ConsistencySignals(
        rule_vs_judge_divergence={"belief": 0.02},
        diagnostic_vs_reward_divergence=0.15,
    )
    assert ConsistencySignals.from_dict(cs.to_dict()) == cs


def test_diagnostic_report_full_assembly():
    report = DiagnosticReport(
        id="diag-run-001",
        candidate_ref="cand-run-001",
        baseline_ref="zengen-v3",
        benchmark_version="bench-v1",
        run_tier="full",               # "quick" | "full"
        sampling_k=4,
        dimension_scores=[],
        error_profiles=[],
        regression_flags=RegressionFlags(flags=[]),
        consistency_signals=ConsistencySignals(
            rule_vs_judge_divergence={}, diagnostic_vs_reward_divergence=0.0),
        links=["run-001"],
    )
    assert DiagnosticReport.from_dict(report.to_dict()) == report
```

**Step 2 — 确认失败**:`uv run --extra dev pytest tests/test_diag_datamodel.py -v` → `ModuleNotFoundError`。

**Step 3 — 实现** `src/diagnostic/datamodel.py`:所有 9 个 dataclass,`from __future__ import annotations`,每个带对称 `to_dict`/`from_dict`。要点:
- `DiagnosticItem`:`scoring_mode` 域 `"rule"|"judge"`;`track` 域 `"static"|"dynamic"`;`reference_answer: str|None`、`rubric: dict|None`、`family_id: str|None`(纯 str 字段,不做代码级枚举校验,与 Plan 2 datamodel 约定一致)。
- `ItemResult`:`n_samples` 用 `@property` 返回 `len(self.raw_outputs)`(不入 `to_dict`,派生量)。
- `DimensionScore.track_provenance`:**必填无默认**(硬约束,强制调用方标注轨道来源),域 `"static"|"dynamic"|"fused"`。
- `DiagnosticReport`:嵌套 `RegressionFlags`/`ConsistencySignals` 用其 `from_dict`/`to_dict` 递归;`dimension_scores`/`error_profiles` 为 list 递归;所有 list 双向 `list(...)` 防御性拷贝。
- `RegressionFlags.flags` 为 `list[dict]`(每条含 dimension/kind/drop/baseline_version/beyond_ci);`to_dict` 深拷贝(`[dict(f) for f in flags]`)。

**Step 4 — 确认通过 + 全回归**:`uv run --extra dev pytest tests/test_diag_datamodel.py -v` 全绿;`uv run --extra dev pytest -q` → 应为 93 + 本任务新增(报告实际数)。

**Step 5 — 提交**:仅 `src/diagnostic/__init__.py` + `src/diagnostic/datamodel.py` + `tests/test_diag_datamodel.py`。

Commit message:
```
feat: diagnostic eval data model (taxonomy/item/result/score/report)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

- [ ] Task 1 完成

---

### Task 2: 统计层——Wilson 置信区间与采样聚合(纯函数,stdlib only)

**Files:** `src/diagnostic/stats.py`, `tests/test_diag_stats.py`

依据 spec §2.5.5:分数必带不确定性;差异在置信区间内不算提升。零新依赖,`math` 手写 Wilson score interval。

**Step 1 — 写失败测试** `tests/test_diag_stats.py`:

```python
import math
from diagnostic.stats import (
    wilson_interval, pass_rate, variance, aggregate_pass_rates,
    difference_is_significant,
)


def test_wilson_interval_known_value():
    # 8/10 通过,95% Wilson 区间约 [0.49, 0.94]
    low, high = wilson_interval(8, 10)
    assert 0.48 < low < 0.50
    assert 0.93 < high < 0.95
    assert low < high


def test_wilson_interval_zero_samples_returns_full_range():
    assert wilson_interval(0, 0) == (0.0, 1.0)


def test_wilson_interval_all_pass_upper_bounded_below_one():
    low, high = wilson_interval(10, 10)
    assert high <= 1.0
    assert low < 1.0          # Wilson 下界严格小于 1,反映有限样本不确定性


def test_wilson_interval_is_deterministic():
    assert wilson_interval(7, 20) == wilson_interval(7, 20)


def test_pass_rate_and_variance():
    scores = [1.0, 1.0, 0.0, 1.0]
    assert pass_rate(scores) == 0.75
    assert abs(variance(scores) - 0.1875) < 1e-9


def test_pass_rate_empty_is_zero():
    assert pass_rate([]) == 0.0


def test_aggregate_pass_rates_pools_samples():
    # 逐层聚合:合并样本量而非平均比率(带样本量加权)
    agg = aggregate_pass_rates([(0.75, 4), (0.5, 2)])
    # (3 + 1) / (4 + 2) = 0.6667
    assert abs(agg[0] - 4/6) < 1e-9
    assert agg[1] == 6       # 总样本量


def test_difference_is_significant_within_ci_is_not():
    # 候选 [0.70,0.85] vs 基座 [0.72,0.88] 区间重叠 → 不显著
    assert difference_is_significant((0.70, 0.85), (0.72, 0.88)) is False


def test_difference_is_significant_disjoint_ci_is():
    # 候选 [0.86,0.94] vs 基座 [0.60,0.75] 不重叠 → 显著
    assert difference_is_significant((0.86, 0.94), (0.60, 0.75)) is True
```

**Step 2 — 确认失败**。

**Step 3 — 实现** `src/diagnostic/stats.py`,`from __future__ import annotations`,纯 `math`:
- `wilson_interval(passes: int, n: int, z: float = 1.96) -> tuple[float, float]`:`n==0` 返回 `(0.0, 1.0)`;否则 Wilson 闭式,`max(0.0, center-half)`/`min(1.0, center+half)` 夹逼。公式:
  ```
  phat = passes / n
  denom = 1 + z**2 / n
  center = (phat + z**2 / (2*n)) / denom
  half = (z * math.sqrt(phat*(1-phat)/n + z**2/(4*n**2))) / denom
  ```
- `pass_rate(scores: list[float]) -> float`:空返回 `0.0`,否则 `sum/len`。
- `variance(scores) -> float`:总体方差(`mean((x-μ)^2)`),空返回 `0.0`。
- `aggregate_pass_rates(rate_and_n: list[tuple[float,int]]) -> tuple[float,int]`:按样本量把比率还原成通过数求和 `round(rate*n)`,汇总 `(总通过/总样本, 总样本)`;总样本 0 返回 `(0.0, 0)`。
- `difference_is_significant(ci_a: tuple[float,float], ci_b: tuple[float,float]) -> bool`:两置信区间**不重叠**才 True(`ci_a[0] > ci_b[1] or ci_b[0] > ci_a[1]`)。这条纯函数是 §2.5.5 "区间内差异不算提升"的落地,被退化判定器(Task 10)依赖。

**Step 4 — 通过 + 全回归。Step 5 — 提交**(仅 stats.py + 测试)。

Commit message:
```
feat: diagnostic stats layer — Wilson CI + sampling aggregation (stdlib)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

- [ ] Task 2 完成

---

### Task 3: 基准版本化与可比性判定(§2.5.8)

**Files:** `src/diagnostic/benchmark_version.py`, `tests/test_diag_benchmark_version.py`

依据 spec §2.5.8:基准版本 = taxonomy 版本 + 题目池版本 + SoMEval 集成版本 + judge prompt 版本 + 打分配置版本;任一变动即升版;跨版本不可比。

**Step 1 — 写失败测试** `tests/test_diag_benchmark_version.py`:

```python
from diagnostic.benchmark_version import BenchmarkVersion, is_comparable


def test_benchmark_version_round_trip():
    bv = BenchmarkVersion(
        taxonomy_version="tax-v1",
        item_pool_version="pool-v1",
        someval_version="someval-0.3+sombench-1.0",
        judge_prompt_version="judge-v1",
        scoring_config_version="score-v1",
    )
    assert BenchmarkVersion.from_dict(bv.to_dict()) == bv


def test_version_id_is_stable_composite():
    bv = BenchmarkVersion("tax-v1", "pool-v1", "sv-1", "jv-1", "cv-1")
    assert bv.version_id() == BenchmarkVersion("tax-v1", "pool-v1", "sv-1", "jv-1", "cv-1").version_id()


def test_comparable_only_within_identical_version():
    a = BenchmarkVersion("tax-v1", "pool-v1", "sv-1", "jv-1", "cv-1")
    b = BenchmarkVersion("tax-v1", "pool-v1", "sv-1", "jv-1", "cv-1")
    assert is_comparable(a, b) is True


def test_any_component_change_breaks_comparability():
    a = BenchmarkVersion("tax-v1", "pool-v1", "sv-1", "jv-1", "cv-1")
    # 仅 judge prompt 版本变 → 不可比
    b = BenchmarkVersion("tax-v1", "pool-v1", "sv-1", "jv-2", "cv-1")
    assert is_comparable(a, b) is False
```

**Step 2 — 确认失败。Step 3 — 实现** `src/diagnostic/benchmark_version.py`:
- `BenchmarkVersion` dataclass 五字段 + `to_dict`/`from_dict`;`version_id() -> str` 返回五段拼接(如 `f"{taxonomy_version}|{item_pool_version}|{someval_version}|{judge_prompt_version}|{scoring_config_version}"`),确定性。
- `is_comparable(a, b) -> bool`:`a.version_id() == b.version_id()`。这条被退化判定器(Task 10)与报告生成器(Task 11)依赖——跨版本对比必须拒绝或标注"不可比"。

**Step 4 — 通过 + 全回归。Step 5 — 提交。**

Commit message:
```
feat: benchmark versioning + cross-version comparability guard (§2.5.8)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

- [ ] Task 3 完成

---

### Task 4: ControlPlane 路径助手扩展 + 诊断产物子树(扩展 Plan 1)

**Files:** `src/controlplane/paths.py`(扩展), `src/diagnostic/paths.py`(新增薄封装), `tests/test_diag_paths.py`

依据 spec 行 46(诊断报告位于 `run_dir(run_id)/diagnostic_report.yaml`)+ §2.5.8(taxonomy/题目池需版本化独立存储)。

**Step 1 — 写失败测试** `tests/test_diag_paths.py`:

```python
from controlplane.paths import (
    SUBDIRS, run_dir, diagnostic_report_file, taxonomy_file, item_pool_file,
)


def test_diagnostics_subdir_registered():
    assert "diagnostics" in SUBDIRS


def test_diagnostic_report_lives_under_run_dir():
    p = diagnostic_report_file("run-001")
    assert p == f"{run_dir('run-001')}/diagnostic_report.yaml"


def test_taxonomy_file_is_versioned_under_diagnostics():
    assert taxonomy_file("tax-v1") == "control-plane/diagnostics/taxonomy_tax-v1.yaml"


def test_item_pool_file_is_versioned_under_diagnostics():
    assert item_pool_file("pool-v1") == "control-plane/diagnostics/item_pool_pool-v1.yaml"
```

**Step 2 — 确认失败。Step 3 — 实现**:在 `src/controlplane/paths.py`:
- `SUBDIRS` 追加 `"diagnostics"`(保持既有顺序,新增在末尾——不改动 Plan 1/2 既有断言)。
- `diagnostic_report_file(run_id: str) -> str`:`return f"{run_dir(run_id)}/diagnostic_report.yaml"`。
- `taxonomy_file(version: str) -> str`:`return f"{CONTROL_PLANE_ROOT}/diagnostics/taxonomy_{version}.yaml"`。
- `item_pool_file(version: str) -> str`:`return f"{CONTROL_PLANE_ROOT}/diagnostics/item_pool_{version}.yaml"`。

`src/diagnostic/paths.py` 仅 re-export(`from controlplane.paths import diagnostic_report_file, taxonomy_file, item_pool_file  # noqa: F401`),让诊断包内部引用集中。

**Step 4 — 通过 + 全回归**:注意本任务改动 Plan 1 的 `paths.py`,必须跑全量确认 Plan 1/2 既有 paths 测试仍绿。`uv run --extra dev pytest -q`。

**Step 5 — 提交**:`src/controlplane/paths.py` + `src/diagnostic/paths.py` + `tests/test_diag_paths.py`。

Commit message:
```
feat: add diagnostics path helpers + control-plane subdir

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

- [ ] Task 4 完成

---

### Task 5: 基准加载器 + 分档选择(§2.5.4 快速筛查集/完整诊断集)

**Files:** `src/diagnostic/taxonomy_loader.py`, `tests/test_diag_taxonomy_loader.py`

依据 spec §2.5.4(内部模块划分含"基准加载器")+ 报告 `run_tier` 有 `"quick"`/`"full"` 两档。加载器从内存中的 taxonomy + 题目池挑选题目子集,纯函数(不碰 git,git IO 归 store_adapter)。

**Step 1 — 写失败测试** `tests/test_diag_taxonomy_loader.py`:

```python
from diagnostic.datamodel import DiagnosticTaxonomy, DiagnosticItem, VariantFamily
from diagnostic.taxonomy_loader import (
    ItemPool, select_items, items_for_l2, variant_families_in,
)


def _pool():
    items = [
        DiagnosticItem("i1", "false_belief_transfer", "mcq", "rule", "static", "B", None, "vf1"),
        DiagnosticItem("i1v", "false_belief_transfer", "mcq", "rule", "static", "C", None, "vf1"),
        DiagnosticItem("i2", "persuasion_maintenance", "open", "judge", "dynamic", None,
                       {"criteria": ["goal"]}, None),
        DiagnosticItem("i3", "norm_conflict", "mcq", "rule", "static", "A", None, None),
    ]
    families = [VariantFamily("vf1", "i1", ["i1v"])]
    return ItemPool(version="pool-v1", items=items, variant_families=families)


def test_item_pool_round_trip():
    p = _pool()
    assert ItemPool.from_dict(p.to_dict()) == p


def test_select_full_returns_all():
    p = _pool()
    sel = select_items(p, tier="full")
    assert {i.id for i in sel} == {"i1", "i1v", "i2", "i3"}


def test_select_quick_takes_one_representative_per_l3():
    p = _pool()
    sel = select_items(p, tier="quick")
    # quick 档:每个 l3_task 取第一题,变体不重复入选
    l3s = {i.l3_task for i in sel}
    assert l3s == {"false_belief_transfer", "persuasion_maintenance", "norm_conflict"}
    assert len(sel) == 3


def test_items_for_l2_filters_by_taxonomy_mapping():
    p = _pool()
    tax = DiagnosticTaxonomy(
        "tax-v1", ["mental_representation"],
        {"mental_representation": ["belief"]},
        {"belief": ["false_belief_transfer"]},
        ["basic_reasoning"],
    )
    got = items_for_l2(p, tax, "belief")
    assert {i.id for i in got} == {"i1", "i1v"}


def test_variant_families_in_returns_families_with_all_members_present():
    p = _pool()
    fams = variant_families_in(p, [i.id for i in p.items])
    assert [f.family_id for f in fams] == ["vf1"]
```

**Step 2 — 确认失败。Step 3 — 实现** `src/diagnostic/taxonomy_loader.py`:
- `ItemPool` dataclass:`version: str`、`items: list[DiagnosticItem]`、`variant_families: list[VariantFamily]`;`to_dict`/`from_dict` 递归子实体。
- `select_items(pool, tier) -> list[DiagnosticItem]`:`tier=="full"` 返回全部(顺序稳定);`tier=="quick"` 按 `l3_task` 分组,每组取**首个出现**的题(用 dict 保序去重),跳过已选题的变体(同 `l3_task` 只留一题即天然排除)。未知 tier 抛 `ValueError`。
- `items_for_l2(pool, taxonomy, l2) -> list[DiagnosticItem]`:从 `taxonomy.l3_tasks[l2]` 取 l3 集合,过滤 `pool.items` 中 `l3_task in 集合`。
- `variant_families_in(pool, item_ids) -> list[VariantFamily]`:返回 base 与全部变体都在 `item_ids` 中的族(供错误归类器按族分析)。

纯函数,无 git IO。

**Step 4 — 通过 + 全回归。Step 5 — 提交。**

Commit message:
```
feat: benchmark loader + quick/full tier selection (§2.5.4)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

- [ ] Task 5 完成

---

### Task 6: 静态轨 SoMEval 薄适配层(§2.5.2,mock 测试)

**Files:** `src/diagnostic/someval_adapter.py`, `tests/test_diag_someval_adapter.py`

依据 spec §2.5.2(静态轨集成 SoMEval,薄适配层)+ §2.5.10(适配层用 SoMEval 固定输出样本 mock 测试,不依赖真实调用)。适配层职责:调 SoMEval(真实为子进程/库,V1 为 `SoMEvalRunner` Protocol)→ 原始输出转 `ItemResult`。

**Step 1 — 写失败测试** `tests/test_diag_someval_adapter.py`:

```python
from diagnostic.datamodel import DiagnosticItem, ItemResult
from diagnostic.someval_adapter import (
    SoMEvalRunner, MockSoMEvalRunner, run_static_items,
)


def _items():
    return [
        DiagnosticItem("i1", "false_belief_transfer", "mcq", "rule", "static", "B", None, None),
        DiagnosticItem("i2", "norm_conflict", "mcq", "rule", "static", "A", None, None),
    ]


def test_mock_runner_returns_fixed_raw_outputs():
    # SoMEval 固定输出样本:每题 k 个原始回答
    runner = MockSoMEvalRunner(fixed={
        "i1": ["B", "B", "A", "B"],
        "i2": ["A", "A", "A", "A"],
    })
    out = runner.evaluate([_items()[0], _items()[1]], candidate_ref="cand-1", k=4)
    assert out["i1"] == ["B", "B", "A", "B"]
    assert out["i2"] == ["A", "A", "A", "A"]


def test_run_static_items_converts_to_item_result_with_track_static():
    runner = MockSoMEvalRunner(fixed={
        "i1": ["B", "B", "A", "B"],   # 3/4 命中参考答案 B
        "i2": ["A", "A", "A", "A"],   # 4/4 命中参考答案 A
    })
    results = run_static_items(_items(), runner, candidate_ref="cand-1", k=4)
    by_id = {r.item_id: r for r in results}
    assert by_id["i1"].track == "static"
    assert by_id["i1"].pass_rate == 0.75
    assert by_id["i1"].per_sample_scores == [1.0, 1.0, 0.0, 1.0]
    assert by_id["i2"].pass_rate == 1.0
    assert all(isinstance(r, ItemResult) for r in results)


def test_run_static_items_is_deterministic():
    runner = MockSoMEvalRunner(fixed={"i1": ["B", "A"], "i2": ["A", "B"]})
    a = run_static_items(_items(), runner, candidate_ref="cand-1", k=2)
    b = run_static_items(_items(), runner, candidate_ref="cand-1", k=2)
    assert [r.to_dict() for r in a] == [r.to_dict() for r in b]


def test_mock_runner_missing_item_raises_loud():
    import pytest
    runner = MockSoMEvalRunner(fixed={"i1": ["B"]})
    with pytest.raises(KeyError):
        runner.evaluate(_items(), candidate_ref="cand-1", k=1)  # i2 缺样本 → 报错不静默
```

**Step 2 — 确认失败。Step 3 — 实现** `src/diagnostic/someval_adapter.py`:
- `SoMEvalRunner` Protocol:`evaluate(items: list[DiagnosticItem], candidate_ref: str, k: int) -> dict[str, list[str]]`(item_id → k 个原始回答)。真实实现将 spawn SoMEval 子进程,不在本计划范围。
- `MockSoMEvalRunner(fixed: dict[str, list[str]])`:`evaluate` 返回 `{item.id: fixed[item.id] ...}`;缺 item 的 `fixed` 键 → 让 `fixed[item.id]` 自然 `KeyError`(§凭证/依赖失败要报明确错误的同源原则:mock 也不静默补默认)。
- `run_static_items(items, runner, candidate_ref, k) -> list[ItemResult]`:调 `runner.evaluate` 拿原始输出;对 `scoring_mode=="rule"` 的题,用 `RuleScorer`(Task 7 提供;本任务为解耦先内联一个 `_rule_hit(raw, item.reference_answer) -> float` 精确匹配,Task 7 落地正式 `RuleScorer` 后由 orchestrator 组装,不改本函数签名)每样本打 0/1,`per_sample_scores` + `pass_rate`(用 `stats.pass_rate`)+ `variance`(用 `stats.variance`),`track="static"`。

> 说明:静态轨里 SoMEval 已给出的题多为客观题(规则式),judge 题主要在动态轨与开放题。本任务只负责"原始输出 → ItemResult"的静态轨转换与规则命中,judge 打分在 Task 7。

**Step 4 — 通过 + 全回归。Step 5 — 提交。**

Commit message:
```
feat: SoMEval thin adapter (static track) with mock runner (§2.5.2/2.5.10)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

- [ ] Task 6 完成

---

### Task 7: 打分器——规则式 + LLM-as-judge(§2.5.4,judge 整体 mock)

**Files:** `src/diagnostic/scorer.py`, `tests/test_diag_scorer.py`

依据 spec §2.5.4(打分器:规则式 + LLM-as-judge)+ §2.5.2(judge 不能是被训练模型或近亲;rubric prompt 版本化)+ §2.5.10(judge 部分整体 mock)。

**Step 1 — 写失败测试** `tests/test_diag_scorer.py`:

```python
import pytest
from diagnostic.datamodel import DiagnosticItem, ItemResult
from diagnostic.scorer import (
    RuleScorer, JudgeBackend, MockJudgeBackend, LLMJudgeScorer,
    SelfEvalBiasError,
)


def _mcq():
    return DiagnosticItem("i1", "false_belief_transfer", "mcq", "rule", "static", "B", None, None)


def _open():
    return DiagnosticItem("i2", "persuasion_maintenance", "open", "judge", "dynamic",
                          None, {"criteria": ["goal_completion"], "prompt_version": "judge-v1"}, None)


# --- 规则式:纯逻辑 ---
def test_rule_scorer_exact_match():
    s = RuleScorer()
    assert s.score(_mcq(), "B") == 1.0
    assert s.score(_mcq(), "A") == 0.0


def test_rule_scorer_normalizes_whitespace_and_case():
    s = RuleScorer()
    assert s.score(_mcq(), "  b ") == 1.0


def test_rule_scorer_score_samples_returns_item_result():
    s = RuleScorer()
    r = s.score_samples(_mcq(), ["B", "B", "A", "B"])
    assert isinstance(r, ItemResult)
    assert r.pass_rate == 0.75
    assert r.track == "static"


# --- judge:整体 mock ---
def test_mock_judge_returns_fixed_scores():
    judge = MockJudgeBackend(model_id="judge-model-x",
                             fixed={("i2", 0): 0.8, ("i2", 1): 0.6})
    scorer = LLMJudgeScorer(judge, candidate_model_id="cand-model")
    r = scorer.score_samples(_open(), ["resp0", "resp1"])
    assert r.per_sample_scores == [0.8, 0.6]
    assert r.pass_rate == 0.7           # 均值作为开放题"通过率"
    assert r.track == "dynamic"


def test_judge_must_not_be_candidate_or_near_relative():
    # judge 与被评模型同族 → 自评偏差,构造时报错(§2.5.2)
    judge = MockJudgeBackend(model_id="cand-model-sft", fixed={})
    with pytest.raises(SelfEvalBiasError):
        LLMJudgeScorer(judge, candidate_model_id="cand-model")


def test_judge_backend_is_protocol_swappable():
    # 任意实现 judge(item, response) -> float 的对象可用
    class _Stub:
        model_id = "external-judge"
        def judge(self, item, response, sample_index): return 0.5
    scorer = LLMJudgeScorer(_Stub(), candidate_model_id="cand-model")
    r = scorer.score_samples(_open(), ["x", "y"])
    assert r.per_sample_scores == [0.5, 0.5]
```

**Step 2 — 确认失败。Step 3 — 实现** `src/diagnostic/scorer.py`:
- `RuleScorer`:`score(item, raw) -> float` 归一化(`str.strip().lower()`)后与 `item.reference_answer` 精确比对返回 1.0/0.0;`score_samples(item, raws) -> ItemResult` 逐样本打分,组装 `ItemResult`(track 取 `item.track`,用 `stats.pass_rate`/`variance`)。
- `JudgeBackend` Protocol:`model_id: str`;`judge(item, response, sample_index) -> float`(0..1)。
- `MockJudgeBackend(model_id, fixed: dict[tuple[str,int], float])`:`judge` 查 `fixed[(item.id, sample_index)]`,缺键 `KeyError`(不静默)。
- `SelfEvalBiasError(Exception)`。
- `LLMJudgeScorer(judge, candidate_model_id)`:构造时校验 `_is_near_relative(judge.model_id, candidate_model_id)`——若 judge 的 model_id 以 candidate_model_id 为前缀(同族微调:`cand-model-sft` vs `cand-model`)或相等,抛 `SelfEvalBiasError`。`score_samples(item, responses) -> ItemResult`:逐样本 `judge.judge(...)`,`pass_rate` 取均值(开放题连续分),`track=item.track`。
- rubric 的 `prompt_version` 透传给 judge(真实实现用,mock 忽略),不在打分数值逻辑内。

> 安全:judge 后端凭证走环境变量,不进本模块的任何持久化;`model_id` 是标识符不是凭证。近亲判定是保守前缀匹配,V1 足够;真实接入可换更严格的模型血缘表。

**Step 4 — 通过 + 全回归。Step 5 — 提交。**

Commit message:
```
feat: scorer — rule-based + LLM-judge (mock) with self-eval bias guard

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

- [ ] Task 7 完成

---

### Task 8: 扩展 run_episode 支持早停 + 动态轨(§2.5.2,修改 Plan 2 冻结核心)

**Files:** `src/simulator/runner.py`(扩展), `tests/test_runner_stop_predicate.py`(新增), `src/diagnostic/dynamic_track.py`, `tests/test_diag_dynamic_track.py`

> ⚠️ 本任务修改 **Plan 2 已合入 main 的纯函数核心** `run_episode`(批量 + 未来实时共享)。硬性要求:**向后兼容**(不传 `stop_predicate` 时行为逐字节等价于当前),且 **Plan 2 现有 6 个 runner 测试全部保持绿**(回归保护)。用户已批准此扩展方案。

**Step 1a — 先加回归保护测试** `tests/test_runner_stop_predicate.py`(先确认既有行为不变,再加新行为):

```python
from simulator.datamodel import (
    Scenario, RoleSlot, Persona, RoleBinding, RoleAssignment,
)
from simulator.backends import ScriptedBackend
from simulator.runner import run_episode


def _fixture():
    scenario = Scenario(
        id="s1", background="bg",
        role_slots=[RoleSlot("a", "role a"), RoleSlot("b", "role b")],
        conflict="c", norms=["n"], purpose="belief",
    )
    personas = {"pa": Persona("pa", ["t"], "m", "s", ["v"]),
                "pb": Persona("pb", ["t"], "m", "s", ["v"])}
    assignment = RoleAssignment(
        scenario_id="s1",
        bindings=[RoleBinding("a", "pa", "self_play"),
                  RoleBinding("b", "pb", "frozen_reference")],
        max_turns=4,
    )
    backends = {"a": ScriptedBackend({"a": ["a1", "a2"]}),
                "b": ScriptedBackend({"b": ["b1", "b2"]})}
    return scenario, assignment, personas, backends


def test_backward_compatible_no_predicate_runs_full_max_turns():
    scenario, assignment, personas, backends = _fixture()
    ep = run_episode(scenario, assignment, personas, backends, "e1")
    assert len(ep.turns) == 4
    assert ep.terminated_reason == "max_turns"


def test_stop_predicate_triggers_goal_reached_early():
    scenario, assignment, personas, backends = _fixture()
    # 第 2 轮(index 1)后判定目标达成
    def stop(turns): return len(turns) >= 2
    ep = run_episode(scenario, assignment, personas, backends, "e1", stop_predicate=stop)
    assert len(ep.turns) == 2
    assert ep.terminated_reason == "goal_reached"


def test_stop_predicate_never_true_falls_back_to_max_turns():
    scenario, assignment, personas, backends = _fixture()
    ep = run_episode(scenario, assignment, personas, backends, "e1",
                     stop_predicate=lambda turns: False)
    assert len(ep.turns) == 4
    assert ep.terminated_reason == "max_turns"


def test_stop_predicate_receives_turns_so_far():
    scenario, assignment, personas, backends = _fixture()
    seen = []
    def stop(turns):
        seen.append(len(turns))
        return False
    run_episode(scenario, assignment, personas, backends, "e1", stop_predicate=stop)
    # 每轮结束后调用一次,看到 1,2,3,4
    assert seen == [1, 2, 3, 4]
```

**Step 1b — 动态轨测试** `tests/test_diag_dynamic_track.py`:

```python
from diagnostic.datamodel import DiagnosticItem
from diagnostic.dynamic_track import run_dynamic_item, DynamicOutcome
from simulator.datamodel import Scenario, RoleSlot, Persona, RoleAssignment, RoleBinding
from simulator.backends import ScriptedBackend


def _dyn_fixture():
    scenario = Scenario("s1", "bg",
        [RoleSlot("candidate", "被测"), RoleSlot("opponent", "对手")],
        "c", ["n"], "persuasion")
    personas = {"pc": Persona("pc", ["t"], "m", "s", ["v"]),
                "po": Persona("po", ["t"], "m", "s", ["v"])}
    assignment = RoleAssignment("s1",
        [RoleBinding("candidate", "pc", "self_play"),
         RoleBinding("opponent", "po", "frozen_reference")], max_turns=4)
    backends = {"candidate": ScriptedBackend({"candidate": ["ok", "agreed", "ok"]}),
                "opponent": ScriptedBackend({"opponent": ["no", "no", "no"]})}
    item = DiagnosticItem("i2", "persuasion_maintenance", "open", "judge", "dynamic",
                          None, {"goal_keyword": "agreed"}, None)
    return item, scenario, assignment, personas, backends


def test_run_dynamic_item_produces_episode_and_outcome():
    item, scenario, assignment, personas, backends = _dyn_fixture()
    outcome = run_dynamic_item(item, scenario, assignment, personas, backends,
                               episode_id="dyn-e1")
    assert isinstance(outcome, DynamicOutcome)
    assert outcome.episode.id == "dyn-e1"
    assert outcome.candidate_slot == "candidate"
    # 候选在第 2 轮说出目标关键词 → 目标达成,早停
    assert outcome.goal_reached is True
    assert outcome.episode.terminated_reason == "goal_reached"


def test_run_dynamic_item_no_goal_runs_full_and_marks_unreached():
    item, scenario, assignment, personas, backends = _dyn_fixture()
    item.rubric = {"goal_keyword": "NEVER_SAID"}
    outcome = run_dynamic_item(item, scenario, assignment, personas, backends,
                               episode_id="dyn-e2")
    assert outcome.goal_reached is False
    assert outcome.episode.terminated_reason == "max_turns"


def test_candidate_utterances_extracted_for_scoring():
    item, scenario, assignment, personas, backends = _dyn_fixture()
    outcome = run_dynamic_item(item, scenario, assignment, personas, backends,
                               episode_id="dyn-e3")
    # 供动态轨 judge 打分:仅候选 slot 的发言
    assert all("candidate" == t.speaker_slot for t in
               [outcome.episode.turns[i] for i in outcome.candidate_turn_indices])
```

**Step 2 — 确认失败。Step 3 — 实现**:

`src/simulator/runner.py` 扩展 `run_episode` 签名(新增末位可选参数,`from __future__ import annotations` + `from typing import Callable`):
```python
def run_episode(scenario, assignment, personas, backends, episode_id,
                stop_predicate: "Callable[[list], bool] | None" = None):
    ...  # 既有校验(slot 子集、bindings 非空、persona 存在)完全不变
    turns = []
    for turn_index in range(assignment.max_turns):
        ...  # 既有 round-robin + respond 逻辑不变,append turn
        if stop_predicate is not None and stop_predicate(turns):
            return Episode(episode_id, scenario.id, assignment, turns,
                           terminated_reason="goal_reached")
    return Episode(episode_id, scenario.id, assignment, turns,
                   terminated_reason="max_turns")
```
关键:`stop_predicate` 在**每轮 append 之后**判定;默认 `None` 时循环体与返回值与 Plan 2 完全一致(回归测试保证)。

`src/diagnostic/dynamic_track.py`:
- `DynamicOutcome` dataclass:`episode: Episode`、`candidate_slot: str`、`goal_reached: bool`、`candidate_turn_indices: list[int]`;`to_dict`/`from_dict`(episode 递归)。
- `run_dynamic_item(item, scenario, assignment, personas, backends, episode_id) -> DynamicOutcome`:
  - 候选 slot = bindings 中 `model_backend=="self_play"` 的首个 slot(对手为 `frozen_reference`/`external_api`)。
  - 构造 `stop_predicate`:若 `item.rubric` 含 `goal_keyword`,当最新一轮候选发言包含该关键词即停(目标达成)。
  - 调 `run_episode(..., stop_predicate=pred)`;`goal_reached = (episode.terminated_reason == "goal_reached")`。
  - `candidate_turn_indices` = 候选 slot 发言的轮次下标(供 Task 7 judge 打分与 Task 9 一致性分析)。

**Step 4 — 通过 + 全回归**:**必须**确认 Plan 2 原有 `tests/test_runner*.py` / `test_sim_integration.py` 全绿(回归保护),再确认新测试绿。`uv run --extra dev pytest -q`。

**Step 5 — 提交**:`src/simulator/runner.py` + `tests/test_runner_stop_predicate.py` + `src/diagnostic/dynamic_track.py` + `tests/test_diag_dynamic_track.py`。

Commit message:
```
feat: run_episode early-stop (goal_reached) + dynamic diagnostic track

Extends Plan 2 frozen run_episode with optional stop_predicate
(backward compatible: default None preserves exact prior behavior).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

- [ ] Task 8 完成

---

### Task 9: 错误归类器(§2.5.3 变体族通过模式 + 动态一致性)

**Files:** `src/diagnostic/error_classifier.py`, `tests/test_diag_error_classifier.py`

依据 spec §2.5.3:静态轨按**变体族通过模式**归类(base 过但变体挂 → 表层匹配/`surface_match`;base 就挂 → 推理断裂/`reasoning_break`;抖动大 → `unstable`;格式错 → `format_fail`;拒答 → `refusal`);动态轨看**多轮一致性**(前后立场矛盾 → `inconsistency`)。

**Step 1 — 写失败测试** `tests/test_diag_error_classifier.py`:

```python
from diagnostic.datamodel import ItemResult, VariantFamily
from diagnostic.error_classifier import (
    classify_static_family, classify_dynamic_consistency,
    aggregate_error_profile,
)


def _r(item_id, pass_rate, variance=0.0, track="static", outputs=None):
    n = 4
    return ItemResult(item_id, outputs or ["x"] * n, [pass_rate] * n,
                      pass_rate, variance, track)


def test_surface_match_base_passes_variants_fail():
    fam = VariantFamily("vf1", "base", ["v1", "v2"])
    results = {"base": _r("base", 1.0), "v1": _r("v1", 0.0), "v2": _r("v2", 0.0)}
    assert classify_static_family(fam, results) == "surface_match"


def test_reasoning_break_base_fails():
    fam = VariantFamily("vf1", "base", ["v1"])
    results = {"base": _r("base", 0.0), "v1": _r("v1", 0.0)}
    assert classify_static_family(fam, results) == "reasoning_break"


def test_unstable_high_variance():
    fam = VariantFamily("vf1", "base", ["v1"])
    results = {"base": _r("base", 0.5, variance=0.25), "v1": _r("v1", 0.5, variance=0.25)}
    assert classify_static_family(fam, results) == "unstable"


def test_format_fail_detected_from_outputs():
    fam = VariantFamily("vf1", "base", [])
    results = {"base": _r("base", 0.0, outputs=["<malformed", "<malformed", "<malformed", "<malformed"])}
    assert classify_static_family(fam, results, format_validator=lambda o: o.startswith("<")) == "format_fail"


def test_refusal_detected():
    fam = VariantFamily("vf1", "base", [])
    results = {"base": _r("base", 0.0, outputs=["I can't help", "I refuse", "no", "no"])}
    assert classify_static_family(fam, results,
        refusal_detector=lambda o: "can't" in o or "refuse" in o) == "refusal"


def test_dynamic_consistency_flip_flops():
    # 候选发言前后立场矛盾
    utterances = ["I agree completely", "actually I disagree", "yes agree", "no way"]
    assert classify_dynamic_consistency(
        utterances, stance_fn=lambda u: "agree" in u and "disagree" not in u) == "inconsistency"


def test_dynamic_consistency_stable_returns_none():
    utterances = ["I agree", "still agree", "agree"]
    assert classify_dynamic_consistency(
        utterances, stance_fn=lambda u: "agree" in u) is None


def test_aggregate_error_profile_normalizes_distribution():
    labels = ["surface_match", "surface_match", "reasoning_break", "unstable"]
    ep = aggregate_error_profile("belief", labels, track="static")
    assert ep.l2_dimension == "belief"
    assert abs(ep.error_type_distribution["surface_match"] - 0.5) < 1e-9
    assert abs(sum(ep.error_type_distribution.values()) - 1.0) < 1e-9
```

**Step 2 — 确认失败。Step 3 — 实现** `src/diagnostic/error_classifier.py`,纯函数:
- `classify_static_family(family, results, *, pass_threshold=0.5, var_threshold=0.2, format_validator=None, refusal_detector=None) -> str`:优先级——先 refusal(任一 output 命中 `refusal_detector`)→ format_fail(任一 output 未过 `format_validator`)→ unstable(base 方差 > var_threshold)→ surface_match(base pass_rate≥threshold 且任一变体 < threshold)→ reasoning_break(base < threshold)→ 否则返回空/`None`(全过,无错误)。判定顺序在实现里固定并注释。
- `classify_dynamic_consistency(utterances, stance_fn) -> str|None`:对候选发言序列取 `stance_fn` 布尔立场,若出现翻转(相邻或整体出现过 True 又 False 的交替)返回 `"inconsistency"`,稳定返回 `None`。
- `aggregate_error_profile(l2_dimension, error_labels, track) -> ErrorProfile`:统计各错误类型频次并归一化成分布,组装 `ErrorProfile`。

**Step 4 — 通过 + 全回归。Step 5 — 提交。**

Commit message:
```
feat: error classifier — variant-family patterns + dynamic consistency (§2.5.3)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

- [ ] Task 9 完成

---

### Task 10: 统计聚合器 L3→L2→L1(§2.5.5,带置信区间 + 轨道来源)

**Files:** `src/diagnostic/aggregator.py`, `tests/test_diag_aggregator.py`

依据 spec §2.5.5(逐层聚合带置信区间)+ §2.5.9(DimensionScore 必带轨道来源标注)。用 Task 2 的 `stats`。

**Step 1 — 写失败测试** `tests/test_diag_aggregator.py`:

```python
from diagnostic.datamodel import ItemResult, DimensionScore, DiagnosticTaxonomy
from diagnostic.aggregator import (
    aggregate_l3, aggregate_l2, aggregate_l1, fuse_tracks,
)


def _r(item_id, pass_rate, track="static", n=4):
    return ItemResult(item_id, ["x"] * n, [pass_rate] * n, pass_rate, 0.0, track)


def _tax():
    return DiagnosticTaxonomy(
        "tax-v1",
        ["mental_representation"],
        {"mental_representation": ["belief", "intention"]},
        {"belief": ["false_belief_transfer"], "intention": ["intent_infer"]},
        ["basic_reasoning"],
    )


def test_aggregate_l3_pools_item_samples_with_ci():
    results = [_r("i1", 0.75), _r("i2", 0.75)]  # 各 3/4,合并 6/8
    ds = aggregate_l3("false_belief_transfer", results, track="static")
    assert ds.level == "l3"
    assert ds.dimension == "false_belief_transfer"
    assert ds.sample_size == 8
    assert abs(ds.score - 0.75) < 1e-9
    assert ds.ci_low < ds.score < ds.ci_high
    assert ds.track_provenance == "static"


def test_aggregate_l2_rolls_up_l3():
    l3s = [
        DimensionScore("false_belief_transfer", "l3", 0.75, 0.5, 0.9, 8, "static"),
        DimensionScore("intent_infer", "l3", 0.5, 0.3, 0.7, 8, "static"),
    ]
    # l2 belief 只含 false_belief_transfer
    ds = aggregate_l2("belief", l3s, _tax(), track="static")
    assert ds.level == "l2"
    assert ds.dimension == "belief"
    assert abs(ds.score - 0.75) < 1e-9
    assert ds.track_provenance == "static"


def test_aggregate_l1_rolls_up_l2():
    l2s = [
        DimensionScore("belief", "l2", 0.75, 0.5, 0.9, 8, "static"),
        DimensionScore("intention", "l2", 0.5, 0.3, 0.7, 8, "static"),
    ]
    ds = aggregate_l1("mental_representation", l2s, _tax(), track="static")
    assert ds.level == "l1"
    assert ds.sample_size == 16


def test_fuse_tracks_marks_provenance_fused():
    static_ds = DimensionScore("belief", "l2", 0.9, 0.8, 0.95, 40, "static")
    dynamic_ds = DimensionScore("belief", "l2", 0.5, 0.35, 0.65, 20, "dynamic")
    fused = fuse_tracks(static_ds, dynamic_ds)
    assert fused.track_provenance == "fused"
    # 融合是样本量加权,动态低分不被静态高分掩盖(分数在两者之间且低于纯静态)
    assert dynamic_ds.score < fused.score < static_ds.score
    assert fused.sample_size == 60
```

**Step 2 — 确认失败。Step 3 — 实现** `src/diagnostic/aggregator.py`,纯函数(用 `stats.aggregate_pass_rates`/`wilson_interval`):
- `aggregate_l3(l3_task, item_results, track) -> DimensionScore`:合并各 ItemResult 的 `(pass_rate, n_samples)`,`stats.aggregate_pass_rates` 得 `(score, N)`,`stats.wilson_interval(round(score*N), N)` 得区间,`level="l3"`,`track_provenance=track`。
- `aggregate_l2(l2, l3_scores, taxonomy, track) -> DimensionScore`:从 `taxonomy.l3_tasks[l2]` 选出属于该 l2 的 l3 分数,按各自 `sample_size` 加权合并(还原通过数),Wilson 区间,`level="l2"`。
- `aggregate_l1(l1, l2_scores, taxonomy, track) -> DimensionScore`:从 `taxonomy.l2_dimensions[l1]` 选 l2,同法上卷,`level="l1"`。
- `fuse_tracks(static_score, dynamic_score) -> DimensionScore`:同维度两轨按样本量加权合并分数与样本量,Wilson 区间基于合并样本,`track_provenance="fused"`,`level` 取两者相同的 level。融合保证动态轨低分对结果有实质拉低作用(硬约束:缺陷不被掩盖)。

**Step 4 — 通过 + 全回归。Step 5 — 提交。**

Commit message:
```
feat: statistical aggregator L3->L2->L1 with CIs + track provenance (§2.5.5)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

- [ ] Task 10 完成

---

### Task 11: 对比与退化判定器(§2.5.6,只产标记 + 证据)

**Files:** `src/diagnostic/regression.py`, `tests/test_diag_regression.py`

依据 spec §2.5.6:对照当前晋级基座,任一维度显著下降 → 标记退化;区间内差异不算退化(用 Task 2 的 `difference_is_significant`);G 组失效单独标记(OPD 失效信号);诊断分与训练 reward 背离 → reward hacking 嫌疑。**只产标记 + 证据,不做晋级决定**。跨基准版本不可比(用 Task 3 的 `is_comparable`)则拒绝对比并标注。

**Step 1 — 写失败测试** `tests/test_diag_regression.py`:

```python
import pytest
from diagnostic.datamodel import DimensionScore, RegressionFlags
from diagnostic.benchmark_version import BenchmarkVersion
from diagnostic.regression import (
    detect_regressions, detect_g_probe_failure, detect_reward_hacking,
    CrossVersionError,
)


def _ds(dim, score, lo, hi, level="l2", track="fused"):
    return DimensionScore(dim, level, score, lo, hi, 40, track)


def test_significant_drop_is_flagged_with_evidence():
    cand = [_ds("empathy", 0.60, 0.52, 0.68)]
    base = [_ds("empathy", 0.85, 0.80, 0.90)]
    rf = detect_regressions(cand, base, baseline_version="zengen-v3")
    assert len(rf.flags) == 1
    f = rf.flags[0]
    assert f["dimension"] == "empathy"
    assert f["beyond_ci"] is True
    assert f["baseline_version"] == "zengen-v3"
    assert f["drop"] > 0


def test_drop_within_ci_not_flagged():
    cand = [_ds("empathy", 0.82, 0.75, 0.89)]
    base = [_ds("empathy", 0.85, 0.80, 0.90)]   # 区间重叠
    rf = detect_regressions(cand, base, baseline_version="zengen-v3")
    assert rf.flags == []


def test_improvement_not_flagged():
    cand = [_ds("empathy", 0.95, 0.90, 0.98)]
    base = [_ds("empathy", 0.70, 0.62, 0.78)]
    rf = detect_regressions(cand, base, baseline_version="zengen-v3")
    assert rf.flags == []


def test_g_probe_failure_flagged_separately():
    # G 组不参与能力分,但失效是 OPD 崩坏信号
    flags = detect_g_probe_failure({"format_compliance": 0.4, "basic_reasoning": 0.95},
                                   threshold=0.7)
    assert any(f["probe"] == "format_compliance" and f["kind"] == "g_probe_failure"
               for f in flags)
    assert all(f["probe"] != "basic_reasoning" for f in flags)


def test_reward_hacking_suspected_when_diag_and_reward_diverge():
    # 训练 reward 高涨但诊断分下降 → 嫌疑
    f = detect_reward_hacking(diag_delta=-0.05, reward_delta=+0.20, threshold=0.15)
    assert f is not None
    assert f["kind"] == "reward_hacking_suspected"


def test_reward_hacking_not_flagged_when_aligned():
    assert detect_reward_hacking(diag_delta=+0.10, reward_delta=+0.12, threshold=0.15) is None


def test_cross_version_comparison_refused():
    cand = [_ds("empathy", 0.6, 0.5, 0.7)]
    base = [_ds("empathy", 0.85, 0.8, 0.9)]
    v_a = BenchmarkVersion("tax-v1", "pool-v1", "sv-1", "jv-1", "cv-1")
    v_b = BenchmarkVersion("tax-v2", "pool-v1", "sv-1", "jv-1", "cv-1")  # taxonomy 变
    with pytest.raises(CrossVersionError):
        detect_regressions(cand, base, baseline_version="zengen-v3",
                           candidate_bench=v_a, baseline_bench=v_b)
```

**Step 2 — 确认失败。Step 3 — 实现** `src/diagnostic/regression.py`,纯函数:
- `CrossVersionError(Exception)`。
- `detect_regressions(candidate_scores, baseline_scores, *, baseline_version, candidate_bench=None, baseline_bench=None) -> RegressionFlags`:若两 bench 都给且 `not is_comparable(...)` → 抛 `CrossVersionError`(跨版本不可比,硬约束)。按维度配对,仅当候选分 < 基座分 **且** `stats.difference_is_significant(cand_ci, base_ci)`(区间不重叠)才记 flag,证据含 `dimension/kind="social_regression"/drop=base-cand/baseline_version/beyond_ci=True`。区间内差异或提升不记(§2.5.5)。
- `detect_g_probe_failure(g_scores: dict, threshold=0.7) -> list[dict]`:低于阈值的 G 探针各记一条 `kind="g_probe_failure"` + `probe` + `score`。G 组只监控不计分。
- `detect_reward_hacking(diag_delta, reward_delta, threshold=0.15) -> dict|None`:`reward_delta - diag_delta > threshold`(reward 涨而诊断跌/背离)→ 记 `kind="reward_hacking_suspected"` + 两 delta;否则 None。
- 全部只产标记 + 证据,无晋级决定(§2.5.6)。

**Step 4 — 通过 + 全回归。Step 5 — 提交。**

Commit message:
```
feat: regression detector — CI-gated drops, G-probe & reward-hacking flags (§2.5.6)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

- [ ] Task 11 完成

---

### Task 12: 报告生成器 + ControlPlane store 适配层

**Files:** `src/diagnostic/report.py`, `src/diagnostic/store_adapter.py`, `tests/test_diag_report.py`, `tests/test_diag_store_adapter.py`

依据 spec §2.5.9(DiagnosticReport)+ 行 46(报告落 `run_dir/diagnostic_report.yaml`)+ 补充 F.5(读走 `migration.load_product`,写用 Envelope+GitStore)。

**Step 1 — 写失败测试** `tests/test_diag_report.py`:

```python
from diagnostic.datamodel import (
    DimensionScore, ErrorProfile, RegressionFlags, ConsistencySignals,
)
from diagnostic.report import build_report


def test_build_report_assembles_all_sections():
    dims = [DimensionScore("empathy", "l1", 0.8, 0.72, 0.87, 100, "fused")]
    errs = [ErrorProfile("belief", {"surface_match": 1.0}, "static")]
    rf = RegressionFlags(flags=[])
    cs = ConsistencySignals({"belief": 0.02}, 0.1)
    report = build_report(
        report_id="diag-1", candidate_ref="cand-1", baseline_ref="zengen-v3",
        benchmark_version="bench-v1", run_tier="full", sampling_k=4,
        dimension_scores=dims, error_profiles=errs,
        regression_flags=rf, consistency_signals=cs, run_id="run-1",
    )
    assert report.id == "diag-1"
    assert report.benchmark_version == "bench-v1"
    assert report.dimension_scores == dims
    assert "run-1" in report.links
```

`tests/test_diag_store_adapter.py`:

```python
from controlplane.gitstore import GitStore
from controlplane.paths import diagnostic_report_file
from diagnostic.datamodel import DiagnosticReport, RegressionFlags, ConsistencySignals
from diagnostic.store_adapter import write_report, read_report


def _report():
    return DiagnosticReport(
        "diag-1", "cand-1", "zengen-v3", "bench-v1", "full", 4,
        [], [], RegressionFlags([]), ConsistencySignals({}, 0.0), ["run-1"])


def test_write_then_read_report_round_trips(tmp_path):
    store = GitStore(str(tmp_path)); store.init()
    write_report(store, "run-1", _report())
    got = read_report(store, "run-1")
    assert got == _report()


def test_report_written_at_spec_path(tmp_path):
    store = GitStore(str(tmp_path)); store.init()
    write_report(store, "run-1", _report())
    assert store.exists(diagnostic_report_file("run-1"))


def test_write_is_idempotent(tmp_path):
    store = GitStore(str(tmp_path)); store.init()
    write_report(store, "run-1", _report())
    write_report(store, "run-1", _report())   # 逐字节相同,不应报错/重复提交
    assert read_report(store, "run-1") == _report()


def test_read_goes_through_migration(tmp_path):
    # 写入的 envelope 必须能被 migration.load_product 正常读回(校验通过)
    store = GitStore(str(tmp_path)); store.init()
    write_report(store, "run-1", _report())
    env = store.read_product(diagnostic_report_file("run-1"))
    assert env.type == "diagnostic_report"
    assert env.payload["candidate_ref"] == "cand-1"
```

**Step 2 — 确认失败。Step 3 — 实现**:
- `src/diagnostic/report.py`:`build_report(...) -> DiagnosticReport`——纯组装,`links` 至少含 `run_id`。
- `src/diagnostic/store_adapter.py`:
  - `write_report(store, run_id, report)`:构造 `Envelope(id=report.id, type="diagnostic_report", version=1, created_at=_NOW, created_by=OwnerRef(), status="executed", schema_version=CURRENT_SCHEMA_VERSION, links=report.links, payload=report.to_dict())`,调 `store.write_product(diagnostic_report_file(run_id), env, message=...)`。`_NOW="2026-09-27T00:00:00Z"`(与 Plan 2 store_adapter 惯例一致的固定时间戳)。
  - `read_report(store, run_id) -> DiagnosticReport`:`env = store.read_product(diagnostic_report_file(run_id))`(内部走 migration),`return DiagnosticReport.from_dict(env.payload)`。
  - `type="diagnostic_report"` 为开放字符串,schema 无需改。

**Step 4 — 通过 + 全回归。Step 5 — 提交。**

Commit message:
```
feat: DiagnosticReport builder + ControlPlane store adapter

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

- [ ] Task 12 完成

---

### Task 13: 评测集隔离校验器(§2.5.7,独立暴露给情境生成器)

**Files:** `src/diagnostic/isolation.py`, `tests/test_diag_isolation.py`

依据 spec §2.5.7(硬约束):训练情境写入前必须过与诊断题目池的相似度校验(语义相似度 + 关键结构匹配),命中样本剔除并记录。独立模块暴露给情境生成器。V1 用 stdlib 词元 Jaccard + 关键结构键匹配(不引入 embedding 依赖;真实语义相似度可后续替换后端)。

**Step 1 — 写失败测试** `tests/test_diag_isolation.py`:

```python
from diagnostic.isolation import (
    token_similarity, structural_overlap, is_contaminated, filter_contaminated,
)


def test_token_similarity_identical_is_one():
    assert token_similarity("the cat sat", "the cat sat") == 1.0


def test_token_similarity_disjoint_is_zero():
    assert token_similarity("alpha beta", "gamma delta") == 0.0


def test_structural_overlap_matches_shared_keys():
    a = {"conflict": "resource", "norms": ["honesty"]}
    b = {"conflict": "resource", "norms": ["loyalty"]}
    assert structural_overlap(a, b) > 0     # conflict 键值相同


def test_is_contaminated_above_threshold():
    cand = {"text": "Anne moves the marble while Sally is away", "structure": {"conflict": "false_belief"}}
    pool_item = {"text": "Sally leaves and Anne moves the marble", "structure": {"conflict": "false_belief"}}
    assert is_contaminated(cand, [pool_item], text_threshold=0.3, struct_threshold=0.5) is True


def test_is_contaminated_below_threshold_is_clean():
    cand = {"text": "two traders negotiate a fair price", "structure": {"conflict": "resource"}}
    pool_item = {"text": "Sally leaves and Anne moves the marble", "structure": {"conflict": "false_belief"}}
    assert is_contaminated(cand, [pool_item], text_threshold=0.3, struct_threshold=0.5) is False


def test_filter_contaminated_removes_and_records():
    pool = [{"text": "Sally moves the marble", "structure": {"conflict": "false_belief"}}]
    candidates = [
        {"id": "c1", "text": "Sally moves the marble", "structure": {"conflict": "false_belief"}},
        {"id": "c2", "text": "traders negotiate price", "structure": {"conflict": "resource"}},
    ]
    kept, rejected = filter_contaminated(candidates, pool,
                                         text_threshold=0.3, struct_threshold=0.5)
    assert [c["id"] for c in kept] == ["c2"]
    assert len(rejected) == 1
    assert rejected[0]["id"] == "c1"
    assert "reason" in rejected[0]      # 记录剔除原因(硬约束:命中要记录)
```

**Step 2 — 确认失败。Step 3 — 实现** `src/diagnostic/isolation.py`,纯函数,stdlib:
- `token_similarity(a: str, b: str) -> float`:小写分词(`split()`)Jaccard(交/并),空并集返回 0.0。
- `structural_overlap(a: dict, b: dict) -> float`:共享键中值相等的比例(如 `conflict` 键值相同计入);无共享键返回 0.0。
- `is_contaminated(candidate: dict, pool: list[dict], *, text_threshold, struct_threshold) -> bool`:对任一 pool item,若 `token_similarity(candidate["text"], item["text"]) >= text_threshold` **且** `structural_overlap(candidate["structure"], item["structure"]) >= struct_threshold` → True。
- `filter_contaminated(candidates, pool, *, text_threshold, struct_threshold) -> tuple[list, list]`:返回 `(kept, rejected)`;rejected 每条附 `reason`(命中的 pool item id/相似度),满足"剔除并记录"。

> 说明:V1 相似度是词元级近似,接口设计允许后续把 `token_similarity` 换成 embedding 后端而不改调用方(隔离校验器契约稳定)。

**Step 4 — 通过 + 全回归。Step 5 — 提交。**

Commit message:
```
feat: eval-set isolation validator — text+structural contamination filter (§2.5.7)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

- [ ] Task 13 完成

---

### Task 14: 顶层编排器 + 诊断评测 stub + 端到端(合流点)

**Files:** `src/diagnostic/orchestrator.py`, `src/simulator/stubs.py`(扩展 `diagnostic_evaluator_stub`), `tests/test_diag_orchestrator.py`

编排一次完整诊断:加载题目池 → 静态轨(SoMEval 适配 + 规则打分)+ 动态轨(run_episode + judge 打分)→ 错误归类 → L3→L2→L1 聚合(标注轨道 + 融合)→ 退化判定 → 组装报告 → 写 ControlPlane。全部用 mock 后端,无 GPU/gRPC/真实 SoMEval。

**Step 1 — 写失败测试** `tests/test_diag_orchestrator.py`(端到端,tmp_path):

```python
from controlplane.gitstore import GitStore
from diagnostic.orchestrator import run_diagnostic, DiagnosticConfig
from diagnostic.taxonomy_loader import ItemPool
from diagnostic.datamodel import DiagnosticTaxonomy, DiagnosticItem, VariantFamily
from diagnostic.someval_adapter import MockSoMEvalRunner
from diagnostic.scorer import MockJudgeBackend
from diagnostic.store_adapter import read_report


def _setup():
    tax = DiagnosticTaxonomy(
        "tax-v1", ["mental_representation"],
        {"mental_representation": ["belief"]},
        {"belief": ["false_belief_transfer"]},
        ["format_compliance"],
    )
    items = [
        DiagnosticItem("i1", "false_belief_transfer", "mcq", "rule", "static", "B", None, "vf1"),
        DiagnosticItem("i1v", "false_belief_transfer", "mcq", "rule", "static", "C", None, "vf1"),
    ]
    pool = ItemPool("pool-v1", items, [VariantFamily("vf1", "i1", ["i1v"])])
    someval = MockSoMEvalRunner(fixed={
        "i1": ["B", "B", "B", "B"], "i1v": ["C", "C", "C", "C"]})
    judge = MockJudgeBackend(model_id="external-judge", fixed={})
    return tax, pool, someval, judge


def test_run_diagnostic_end_to_end_writes_report(tmp_path):
    store = GitStore(str(tmp_path)); store.init()
    tax, pool, someval, judge = _setup()
    cfg = DiagnosticConfig(
        run_id="run-1", candidate_ref="cand-1", candidate_model_id="cand-model",
        baseline_ref="zengen-v3", tier="full", sampling_k=4,
        benchmark_version="bench-v1",
    )
    report = run_diagnostic(cfg, tax, pool, someval, judge, store,
                            baseline_scores=[], dynamic_specs=[])
    # 报告已落盘,可读回
    got = read_report(store, "run-1")
    assert got.id == report.id
    assert got.benchmark_version == "bench-v1"
    # 至少产出 L1 维度分,且带轨道来源标注
    assert any(d.level == "l1" for d in got.dimension_scores)
    assert all(d.track_provenance in ("static", "dynamic", "fused")
               for d in got.dimension_scores)


def test_run_diagnostic_static_only_track_provenance(tmp_path):
    store = GitStore(str(tmp_path)); store.init()
    tax, pool, someval, judge = _setup()
    cfg = DiagnosticConfig("run-2", "cand-1", "cand-model", "zengen-v3", "quick", 4, "bench-v1")
    report = run_diagnostic(cfg, tax, pool, someval, judge, store,
                            baseline_scores=[], dynamic_specs=[])
    # 无动态 specs → 纯静态轨,轨道来源都是 static
    assert all(d.track_provenance == "static" for d in report.dimension_scores)
```

`diagnostic_evaluator_stub` 测试可并入 `test_diag_orchestrator.py` 或复用 Plan 2 stubs 测试风格:

```python
def test_diagnostic_evaluator_stub_shape():
    from simulator.stubs import diagnostic_evaluator_stub
    env = diagnostic_evaluator_stub("run-1", "cand-1")
    assert env.type == "diagnostic_report"
    assert env.status == "executed"
    assert "run-1" in env.links
    assert env.payload["candidate_ref"] == "cand-1"
```

**Step 2 — 确认失败。Step 3 — 实现**:
- `src/diagnostic/orchestrator.py`:
  - `DiagnosticConfig` dataclass:`run_id, candidate_ref, candidate_model_id, baseline_ref, tier, sampling_k, benchmark_version`。
  - `run_diagnostic(cfg, taxonomy, pool, someval_runner, judge_backend, store, *, baseline_scores, dynamic_specs) -> DiagnosticReport`:
    1. `select_items(pool, cfg.tier)` 选题。
    2. 静态轨:`run_static_items(static_items, someval_runner, cfg.candidate_ref, cfg.sampling_k)` → ItemResult(规则题用 `RuleScorer.score_samples` 重算确保一致)。
    3. 动态轨:对每个 `dynamic_specs`(含 item + scenario/assignment/personas/backends)`run_dynamic_item(...)` + `LLMJudgeScorer(judge_backend, cfg.candidate_model_id).score_samples(...)`(候选发言)。无 dynamic_specs → 跳过,纯静态。
    4. 错误归类:`classify_static_family` 按 `variant_families_in` 遍历 → `aggregate_error_profile`;动态轨 `classify_dynamic_consistency`。
    5. 聚合:`aggregate_l3`→`aggregate_l2`→`aggregate_l1`,静态与动态分别聚合,同维度两轨都有则 `fuse_tracks`。轨道来源如实标注。
    6. 退化:`detect_regressions(l_scores, baseline_scores, baseline_version=cfg.baseline_ref)`(baseline 空 → 无 flag);G 探针/reward hacking 若有数据则附。
    7. 一致性信号:rule vs judge 背离(若同维度两法都有)、诊断 vs reward 背离(有则填,无则 0)。
    8. `build_report(...)` 组装 → `write_report(store, cfg.run_id, report)` → 返回 report。
  - 编排器本身不含统计/判定逻辑,只调下层纯函数(薄编排,易测)。
- `src/simulator/stubs.py`:新增 `diagnostic_evaluator_stub(run_id, candidate_id) -> Envelope`(type=`"diagnostic_report"`,status=`"executed"`,links=`[run_id]`,payload 含 `candidate_ref`/`benchmark_version`/`dimension_count`),沿用现有 `_NOW`/`OwnerRef()`/`schema_version=1` 惯例。

**Step 4 — 通过 + 全回归**:`uv run --extra dev pytest -q` 全绿(93 + 全部新增)。**Step 5 — 提交。**

Commit message:
```
feat: diagnostic orchestrator (dual-track e2e) + evaluator stub

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```

- [ ] Task 14 完成

---

## Self-Review

### Spec 覆盖表(§2.5)

| Spec 条目 | 需求 | 落地 Task | 落地方式 |
|---|---|---|---|
| §2.5.1 | L1/L2/L3 能力坐标系 + G 组正交探针 | 1, 10, 11 | `DiagnosticTaxonomy(l1/l2/l3 + g_probes)`;聚合按 L3→L2→L1;G 组只监控不计分(`detect_g_probe_failure`) |
| §2.5.2 | 双轨(静态 SoMEval 薄适配 + 动态复用 run_episode);judge 非近亲 | 6, 8, 7, 14 | `someval_adapter`(mock)/`dynamic_track`(run_episode + 早停);`LLMJudgeScorer` 自评偏差守卫 |
| §2.5.3 | VariantFamily + 通过模式归类错误类型 | 1, 9 | `VariantFamily`;`classify_static_family`(surface_match/reasoning_break/unstable/format_fail/refusal)+ `classify_dynamic_consistency` |
| §2.5.4 | 内部模块划分(加载器/执行器/打分器/归类/聚合/退化/报告) | 5–14 | 每模块一 Task,契约分离 |
| §2.5.5 | 分数必带不确定性;区间内差异不算提升 | 2, 10, 11 | `wilson_interval`;逐层聚合带 CI;`difference_is_significant` 门控退化判定 |
| §2.5.6 | 退化只产标记+证据,不做决定;reward hacking 嫌疑 | 11 | `detect_regressions`/`detect_reward_hacking`,均只出 flag+evidence |
| §2.5.7 | 评测集隔离校验器(语义+结构),独立暴露 | 13 | `isolation.py`(`token_similarity`+`structural_overlap`+`filter_contaminated` 剔除并记录) |
| §2.5.8 | 基准版本化(5 要素);跨版本不可比 | 3, 11 | `BenchmarkVersion.version_id()`;`is_comparable` 门控;`detect_regressions` 跨版本抛 `CrossVersionError` |
| §2.5.9 | 数据模型;DimensionScore 必带轨道来源;两轨不无痕合并 | 1, 10 | 9 个 dataclass;`track_provenance` 必填无默认;`fuse_tracks` 标 `"fused"` 且低分不被掩盖 |
| §2.5.10 | 脱离 GPU/SoMEval/gRPC 可测 | 全部 | MockSoMEvalRunner / MockJudgeBackend / ScriptedBackend / tmp_path GitStore;纯函数单测 |

### 安全 / 硬约束核对

- 凭证不进 git 树:judge 后端只持 `model_id`(标识非凭证);配置层凭证走 `*_env` 变量名(真实接入实现),本计划不落任何凭证值。缺失凭证报错不静默 —— mock runner/judge 缺样本 `KeyError`(同源"失败要响"原则)。
- judge ≠ 被训模型或近亲:`LLMJudgeScorer` 构造期 `SelfEvalBiasError` 守卫(前缀匹配同族)。
- 修改 Plan 2 冻结核心 `run_episode`:向后兼容(默认 `stop_predicate=None` 行为等价)+ 专门回归测试保护既有 6 个 runner 测试。
- 读产物走 `migration.load_product`(`store.read_product` 内部即走);写用 Envelope+GitStore(幂等)。
- 无新第三方依赖(Wilson/相似度全 stdlib);`pyproject.toml` 仅需把 `src/diagnostic` 加入 packages。

### 占位符扫描

计划内无 `TODO`/`FIXME`/`pass  # stub`/`raise NotImplementedError` 作为交付物。真实 SoMEval 子进程调用、真实 judge 网络调用、embedding 语义相似度均为**明确越界项**(V1 用 Protocol + mock/近似,契约稳定以便后续替换),已在对应 Task 说明中标注,非隐藏占位符。

### 类型一致性

- `ItemResult.n_samples` 为 `@property`(派生),不入 `to_dict`。
- `DimensionScore.track_provenance` 必填(无默认值),编译期即强制标注。
- 轨道枚举 `"static"|"dynamic"|"fused"`、错误类型枚举全程字符串常量,跨模块一致(datamodel 注释 + 各 classifier/aggregator 产出对齐)。
- 所有 dataclass `from_dict(to_dict()) == self` 往返测试覆盖(Task 1/3/5/8/12)。
- `run_episode` 新签名末位可选参数,不破坏既有位置调用。

### 依赖顺序(实现顺序即任务号)

Task 1(datamodel)→ 2(stats)→ 3(version)为无依赖基石,可并行起步。4(paths)独立。5(loader)依赖 1。6(someval)依赖 1+2。7(scorer)依赖 1+2。8(dynamic+runner)依赖 1+ Plan 2 runner。9(classifier)依赖 1。10(aggregator)依赖 1+2。11(regression)依赖 1+2+3。12(report+store)依赖 1+4。13(isolation)依赖无(仅 stdlib)。14(orchestrator)依赖全部。建议线性 1→14(pipeline 中每题实现器可与上一题评审并行)。

---

## Execution Handoff

**执行方式:** 用 superpowers **subagent-driven-development**(推荐)逐任务执行:每任务 fresh implementer(haiku,机械 TDD)→ 独立验证该 commit(git show --stat 核范围 + 安全 grep + 全回归)→ review package → task reviewer(sonnet)→ 评审通过才记 ledger → 下一任务。全部任务后跑 whole-branch final review(opus tier),按需 fix-wave,再走 finishing-a-development-branch。可 pipeline(评审当前任务时并行派发下一实现器,每 commit 持久;ledger 记录仍受评审门控)。

**分支:** 从合入了 Plan 2 的 `main`(5d822d6)新建 `plan3-diagnostic-eval` worktree。

**依赖调用:** `uv run --extra dev pytest -q`。基线 93 passed,每任务后应递增。

**验证基线:** 全部 14 任务完成后,`uv run --extra dev pytest -q` 全绿;Plan 2 既有 6 个 runner 测试 + 93 全量在 Task 8 后仍绿(回归保护是 Task 8 的验收硬条件)。

**收尾:** whole-branch review → fix-wave(如需)→ finishing-a-development-branch(合入 main;因 session git-fenced,ff-only merge 在 main checkout 终端执行)。

**Ledger:** `.superpowers/sdd/2026-09-27-diagnostic-eval/progress.md`。
