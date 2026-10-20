# 策略经验（Strategy Experience）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让规划器在每一轮看到窗口之外的长期策略证据：把终态 run 压成带策略签名的记录，重算成条目，注入 `PlanningContext` 的第七段。

**Architecture:** 纯函数层（`trainscheduler.experience`，签名/归因/重算/路径安全）与 I/O 层（`trainscheduler.experience_port`，读树、写信封、选条目）分离。落账挂在已有的 `plan_next_round` 处理器里、`assemble_context` 之前，不新增调和循环动作。条目状态写在载荷里，不走产物状态机；人工结论经 `overrides` 覆盖表表达。

**Tech Stack:** Python 3.11+、pytest、PyYAML、git 子进程（`GitStore`）。无新增第三方依赖。

**Spec:** `docs/design/55-strategy-experience.md`

## Global Constraints

- 每个源文件首条语句是 `from __future__ import annotations`（有测试强制）。
- 依赖方向：`trainscheduler` 不得导入 `actio`、`mindagent`、`diagnostic`、`simulator`、`factory`；对 RecipeStore 的导入只经 `recipes_port`。
- 证据等级只经 `controlplane.evidence.diagnostic_grade` 判定，不另写一份；增益只经 `compute_gain`，不另写一份。
- 产物一律是标准信封（`id`、`type`、`version`、`created_at`、`created_by`、`status`、`schema_version`、`links`、`payload`），写入只经 `GitStore.write_product`。
- 新增产物目录追加在 `SUBDIRS` 末尾，顺序固定。
- 开关是 `scheduler.yaml` 的布尔键 `strategy_experience`，默认 `False`；关闭时 `PlanningContext` 与现状渲染结果逐字节相同。
- 测试命令一律 `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest <path> -q`。
- 不跑全量测试；每个任务只跑自己相关的测试文件。

---

### Task 1: 产物目录与路径函数

**Files:**
- Modify: `src/controlplane/paths.py`
- Test: `tests/test_paths.py`, `tests/test_se_paths.py`（新建）

**Interfaces:**
- Produces:
  - `paths.SUBDIRS` 末尾追加 `"experience"`
  - `paths.strategy_experience_note_file(run_id: str) -> str` → `control-plane/runs/<run>/strategy_experience.yaml`
  - `paths.strategy_entry_file(entry_id: str) -> str` → `control-plane/experience/<entry_id>.yaml`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_se_paths.py
"""策略经验的产物路径与目录登记(设计 4.1)。"""
from controlplane import paths


def test_experience_dir_is_registered_last():
    assert paths.SUBDIRS[-1] == "experience"
    assert paths.SUBDIRS.count("experience") == 1


def test_note_file_lives_in_the_run_directory():
    assert paths.strategy_experience_note_file("run-003") == (
        "control-plane/runs/run-003/strategy_experience.yaml"
    )


def test_entry_file_lives_in_the_experience_directory():
    assert paths.strategy_entry_file("exp-tom-3f9c1a2b") == (
        "control-plane/experience/exp-tom-3f9c1a2b.yaml"
    )


def test_init_creates_the_experience_directory(tmp_path):
    from controlplane.gitstore import GitStore

    GitStore(str(tmp_path)).init()
    assert (tmp_path / paths.CONTROL_PLANE_ROOT / "experience").is_dir()
```

- [ ] **Step 2: 运行确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_se_paths.py -q`
Expected: FAIL，`AttributeError: module 'controlplane.paths' has no attribute 'strategy_experience_note_file'`

- [ ] **Step 3: 实现**

在 `src/controlplane/paths.py` 的 `SUBDIRS` 末尾加 `"experience"`：

```python
SUBDIRS = [
    "runs", "recipes", "skill_memory_proposals", "models", "compute", "config", "scenarios",
    "personas", "diagnostics",
    # 闭环集成设计 §4.1:追加在末尾,顺序固定
    "recipe_candidates", "skill_library", "memory_library", "agent_rounds",
    # 策略经验:记录放 run 目录,条目放全局目录
    "experience",
]
```

在文件末尾追加两个路径函数：

```python
def strategy_experience_note_file(run_id: str) -> str:
    """一轮的策略经验记录(落账的幂等键)。放 run 顶层,list_products 的非递归扫描能扫到。"""
    return f"{run_dir(run_id)}/strategy_experience.yaml"


def strategy_entry_file(entry_id: str) -> str:
    """跨 run 聚合出的策略条目。entry_id 已由调用方做过路径安全校验。"""
    return f"{CONTROL_PLANE_ROOT}/experience/{entry_id}.yaml"
```

- [ ] **Step 4: 更新既有断言**

`tests/test_paths.py` 的 `test_subdirs_match_spec` 精确列表末尾追加 `"experience"`：

```python
def test_subdirs_match_spec():
    assert paths.SUBDIRS == [
        "runs", "recipes", "skill_memory_proposals", "models", "compute", "config", "scenarios",
        "personas", "diagnostics",
        "recipe_candidates", "skill_library", "memory_library", "agent_rounds",
        "experience",
    ]
```

- [ ] **Step 5: 运行确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_se_paths.py tests/test_paths.py -q`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add src/controlplane/paths.py tests/test_paths.py tests/test_se_paths.py
git commit -m "feat(paths): 注册 experience 产物目录与两个路径函数"
```

---

### Task 2: 配置开关

**Files:**
- Modify: `src/trainscheduler/config.py`
- Test: `tests/test_se_config.py`（新建）

**Interfaces:**
- Consumes: 无
- Produces: `SchedulerConfig.strategy_experience: bool = False`；`to_dict()` 含该键；`parse_scheduler_config` 接受它、仍拒绝未知键。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_se_config.py
"""scheduler.yaml 的 strategy_experience 开关(设计 2.3、SE-9)。"""
import pytest

from trainscheduler.config import SchedulerConfig, parse_scheduler_config
from trainscheduler.errors import SchedulerConfigError


def test_default_is_off():
    assert SchedulerConfig().strategy_experience is False


def test_bool_round_trips_through_to_dict():
    cfg = SchedulerConfig(strategy_experience=True)
    assert cfg.to_dict()["strategy_experience"] is True
    assert parse_scheduler_config(cfg.to_dict()).strategy_experience is True


def test_absent_key_keeps_the_default():
    assert parse_scheduler_config({}).strategy_experience is False


def test_non_bool_is_rejected():
    with pytest.raises(SchedulerConfigError, match="strategy_experience"):
        parse_scheduler_config({"strategy_experience": "yes"})


def test_unknown_keys_are_still_rejected():
    with pytest.raises(SchedulerConfigError, match="unknown key"):
        parse_scheduler_config({"strategy_exp": True})
```

- [ ] **Step 2: 运行确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_se_config.py -q`
Expected: FAIL，`TypeError: __init__() got an unexpected keyword argument 'strategy_experience'`

- [ ] **Step 3: 实现**

`SchedulerConfig` 加一个字段（放在 `template_version` 之后，保持既有字段顺序不变）：

```python
    max_plan_attempts: int = 3
    template_version: str = PLANNER_V1_VERSION
    strategy_experience: bool = False
```

`to_dict()` 的返回字典同步加一行：

```python
            "template_version": self.template_version,
            "strategy_experience": self.strategy_experience,
```

`parse_scheduler_config` 的构造调用加一行（`_SCHEDULER_KEYS` 由 `fields(SchedulerConfig)` 导出，会自动接受新键；`_bool` 负责类型校验并已把键名写进报错）：

```python
        template_version=template_version,
        strategy_experience=_bool(values, "strategy_experience"),
```

- [ ] **Step 4: 运行确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_se_config.py tests/test_ts_config.py -q`
Expected: PASS（`test_ts_config.py` 若断言了 `to_dict()` 的完整键集合，把 `strategy_experience` 加进那个期望集合）

- [ ] **Step 5: 提交**

```bash
git add src/trainscheduler/config.py tests/test_se_config.py
git commit -m "feat(trainscheduler): scheduler.yaml 增加 strategy_experience 开关,默认关闭"
```

---

### Task 3: 纯函数层

**Files:**
- Create: `src/trainscheduler/experience.py`
- Test: `tests/test_se_experience.py`（新建）

**Interfaces:**
- Produces:
  - `strategy_key(*, dimension, action, mode, perturbation, source_recipe_id) -> dict`：含扰动**值**的精确签名，键排序。
  - `bucket_key(key: dict) -> tuple[str, str, str, tuple[str, ...], str]`：只含扰动**键名**的分桶键。
  - `safe_dimension(dimension: object) -> str | None`
  - `entry_id(dimension: str | None, bucket: tuple) -> str`
  - `attribution_for(action: str, perturbation: dict) -> str`：`high`／`medium`／`low`
  - `recompute_entries(records, existing, *, benchmark_version) -> list[EntryState]`
  - `EntryState`（冻结数据类）：`entry_id`、`bucket`、`support`、`wins`、`losses`、`mean_gain`、`contradictions`、`value_examples`、`status`、`evidence_scope`、`baseline_model_id`、`benchmark_version`、`overrides`，以及 `to_payload()`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_se_experience.py
"""纯函数层:签名、归因、路径安全、重算(设计 4、6.1、6.3)。"""
import math

import pytest

from trainscheduler.experience import (
    EntryState,
    attribution_for,
    bucket_key,
    entry_id,
    recompute_entries,
    safe_dimension,
    strategy_key,
)

NOW = "2026-10-20T00:00:00Z"
BASE = "zengen-v2"


def rec(run_id, *, dimension="tom", gain=0.05, grade="real", regressions=(),
        attribution="medium", perturbation=None, benchmark="bench-v1", base=BASE):
    return {
        "run_id": run_id,
        "strategy_key": strategy_key(
            dimension=dimension, action="clone", mode="batch",
            perturbation={"opd_coefficient": 0.2} if perturbation is None else perturbation,
            source_recipe_id="recipe-run-002",
        ),
        "outcome": {"gain": gain, "promoted": True, "regressions": list(regressions),
                    "cost": {"calls": 6}},
        "evidence": {"grade": grade, "baseline_ref": base, "benchmark_version": benchmark,
                     "baseline_model_id": base},
        "attribution": attribution,
        "ignored": [],
    }


def test_signature_records_values_not_only_key_names():
    a = strategy_key(dimension="tom", action="clone", mode="batch",
                     perturbation={"opd_coefficient": 0.2}, source_recipe_id="recipe-run-002")
    b = strategy_key(dimension="tom", action="clone", mode="batch",
                     perturbation={"opd_coefficient": 0.3}, source_recipe_id="recipe-run-002")
    assert a != b
    assert bucket_key(a, BASE) == bucket_key(b, BASE)


def test_signature_ignores_key_order():
    a = strategy_key(dimension="tom", action="clone", mode="batch",
                     perturbation={"sft_steps": 4, "opd_coefficient": 0.2},
                     source_recipe_id="r")
    b = strategy_key(dimension="tom", action="clone", mode="batch",
                     perturbation={"opd_coefficient": 0.2, "sft_steps": 4},
                     source_recipe_id="r")
    assert a == b


def test_different_baselines_never_share_an_entry_id():
    key = strategy_key(dimension="tom", action="clone", mode="batch",
                       perturbation={"opd_coefficient": 0.2}, source_recipe_id="r")
    assert entry_id("tom", bucket_key(key, "zengen-v1")) != entry_id("tom", bucket_key(key, "zengen-v2"))


def test_int_and_float_of_the_same_number_share_a_signature():
    a = strategy_key(dimension="tom", action="clone", mode="batch",
                     perturbation={"sft_steps": 4}, source_recipe_id="r")
    b = strategy_key(dimension="tom", action="clone", mode="batch",
                     perturbation={"sft_steps": 4.0}, source_recipe_id="r")
    assert a == b


def test_non_scalar_perturbation_is_rejected():
    with pytest.raises(ValueError, match="perturbation"):
        strategy_key(dimension="tom", action="clone", mode="batch",
                     perturbation={"sft_steps": None}, source_recipe_id="r")


@pytest.mark.parametrize("dimension,ok", [
    ("tom", True), ("1", True), ("a" * 64, True), ("", False), ("a" * 65, False),
    ("../escape", False), ("a/b", False), (".", False), ("..", False), (None, False),
    ("tom\n", False),
])
def test_safe_dimension(dimension, ok):
    assert (safe_dimension(dimension) is not None) is ok


def test_entry_id_falls_back_to_the_digest_for_bad_dimensions():
    good = entry_id("tom", ("tom", "clone", "batch", ("opd_coefficient",), "recipe-run-002", BASE))
    bad = entry_id(None, ("..", "clone", "batch", (), "", BASE))
    assert good.startswith("exp-tom-")
    assert bad.startswith("exp-") and "/" not in bad and ".." not in bad


@pytest.mark.parametrize("action,perturbation,expected", [
    ("clone", {"opd_coefficient": 0.2}, "medium"),
    ("clone", {"opd_coefficient": 0.2, "sft_steps": 4}, "low"),
    ("clone", {}, "low"),
    ("replay", {}, "low"),
    ("new", {}, "low"),
])
def test_attribution(action, perturbation, expected):
    assert attribution_for(action, perturbation) == expected


def test_single_witness_is_insufficient():
    [entry] = recompute_entries([rec("run-003")], [], benchmark_version="bench-v1")
    assert entry.status == "insufficient"
    assert entry.support == 1


def test_two_witnesses_become_effective_with_a_mean():
    entries = recompute_entries([rec("run-003"), rec("run-005", gain=0.03)], [],
                                benchmark_version="bench-v1")
    [entry] = entries
    assert entry.status == "effective"
    assert entry.support == 2 and entry.wins == 2
    assert entry.mean_gain == pytest.approx(0.04)


def test_low_attribution_never_aggregates():
    entries = recompute_entries([rec("run-003", attribution="low"),
                                 rec("run-005", attribution="low")], [],
                                benchmark_version="bench-v1")
    assert entries == []


def test_null_gain_and_mock_evidence_never_aggregate():
    assert recompute_entries([rec("run-003", gain=None), rec("run-005", gain=None)], [],
                             benchmark_version="bench-v1") == []
    assert recompute_entries([rec("run-003", grade="mock"), rec("run-005", grade="mock")], [],
                             benchmark_version="bench-v1") == []


def test_records_under_different_baselines_form_separate_entries():
    """对照基线进桶:两组各两条,得到两个互不覆盖的条目,而不是互相污染或只剩一个。"""
    v1 = [rec("run-003", base="zengen-v1"), rec("run-005", base="zengen-v1")]
    v2 = [rec("run-007", base="zengen-v2"), rec("run-009", base="zengen-v2")]
    entries = recompute_entries(v1 + v2, [], benchmark_version="bench-v1")
    assert len(entries) == 2
    assert {e.baseline_model_id for e in entries} == {"zengen-v1", "zengen-v2"}
    assert entries[0].entry_id != entries[1].entry_id
    assert all(e.status == "effective" and e.support == 2 for e in entries)


def test_other_benchmark_never_aggregates():
    assert recompute_entries([rec("run-003", benchmark="bench-v0"),
                              rec("run-005", benchmark="bench-v0")], [],
                             benchmark_version="bench-v1") == []


def test_half_the_support_deprecates():
    records = [rec("run-003"), rec("run-005", gain=0.03), rec("run-008", gain=-0.06,
                                                              regressions=["social_regression"])]
    [entry] = recompute_entries(records, [], benchmark_version="bench-v1")
    assert entry.support == 3 and entry.losses == 1
    assert entry.status == "effective"      # 一条反例未达半数
    records.append(rec("run-011", gain=-0.02, regressions=["social_regression"]))
    [entry] = recompute_entries(records, [], benchmark_version="bench-v1")
    assert entry.status == "deprecated"


def test_contradictions_keep_the_five_latest_by_run_number():
    records = [rec(f"run-{n:03d}", gain=-0.01, regressions=["social_regression"])
               for n in range(3, 13)]
    [entry] = recompute_entries(records, [], benchmark_version="bench-v1")
    assert [c["run_id"] for c in entry.contradictions] == [
        "run-008", "run-009", "run-010", "run-011", "run-012"
    ]


def test_summing_is_independent_of_input_order():
    records = [rec("run-003", gain=0.1), rec("run-005", gain=0.2), rec("run-007", gain=0.3)]
    a = recompute_entries(records, [], benchmark_version="bench-v1")
    b = recompute_entries(list(reversed(records)), [],
                          benchmark_version="bench-v1")
    assert a[0].to_payload() == b[0].to_payload()


def test_overrides_survive_a_recompute():
    [first] = recompute_entries([rec("run-003"), rec("run-005")], [],
                                benchmark_version="bench-v1")
    existing = [first.with_overrides({"status": "deprecated"})]
    [again] = recompute_entries([rec("run-003"), rec("run-005")], existing,
                                benchmark_version="bench-v1")
    assert again.status == "deprecated"
    assert again.support == 2


def test_payload_round_trips_through_entry_state():
    """条目写盘再读回要等值:这条路径曾经因为 bucket_key 少一个参数而必然抛 TypeError。"""
    [entry] = recompute_entries([rec("run-003"), rec("run-005", gain=0.03)], [],
                                benchmark_version="bench-v1")
    again = EntryState.from_payload(entry.entry_id, entry.to_payload())
    assert again.to_payload() == entry.to_payload()
    assert again.entry_id == entry.entry_id
    assert again.bucket == entry.bucket


def test_payload_keeps_perturbation_values_out_of_the_bucket():
    """bucket 只存键名;值与次数在 value_examples 里。"""
    [entry] = recompute_entries([rec("run-003"), rec("run-005", gain=0.03)], [],
                                benchmark_version="bench-v1")
    payload = entry.to_payload()
    assert payload["strategy_key"]["perturbation_keys"] == ["opd_coefficient"]
    assert "perturbation" not in payload["strategy_key"]
    assert payload["value_examples"] == {"opd_coefficient": {"0.2": 2}}


def test_deprecation_counts_every_contradiction_not_just_the_stored_five():
    """反例超过五条时,阈值必须用全部反例计数;否则高支持数条目会被洗成 effective。"""
    records = [rec(f"run-{n:03d}", gain=-0.01, regressions=["social_regression"])
               for n in range(3, 14)]
    [entry] = recompute_entries(records, [], benchmark_version="bench-v1")
    assert entry.support == 11
    assert len(entry.contradictions) == 5          # 载荷只存最近五条
    assert entry.status == "deprecated"            # 但十一次反例足以废弃


def test_a_non_positive_mean_never_becomes_effective():
    records = [rec("run-003", gain=0.1), rec("run-005", gain=0.1),
               rec("run-007", gain=0.1), rec("run-009", gain=-0.9)]
    [entry] = recompute_entries(records, [], benchmark_version="bench-v1")
    assert entry.mean_gain < 0
    assert entry.status != "effective"


def test_a_zero_mean_never_becomes_effective():
    records = [rec("run-003", gain=0.0), rec("run-005", gain=0.0)]
    [entry] = recompute_entries(records, [], benchmark_version="bench-v1")
    assert entry.status != "effective"


def test_stale_does_not_overwrite_a_human_status():
    [old] = recompute_entries([rec("run-003"), rec("run-005", benchmark="bench-v0")], [],
                              benchmark_version="bench-v0")
    pinned = old.with_overrides({"status": "deprecated"})
    [now] = recompute_entries([], [pinned], benchmark_version="bench-v1")
    assert now.status == "deprecated"
    assert now.overrides == {"status": "deprecated"}


def test_overrides_apply_to_other_fields_too():
    [entry] = recompute_entries([rec("run-003"), rec("run-005")], [],
                                benchmark_version="bench-v1")
    pinned = entry.with_overrides({"mean_gain": 9.0})
    [again] = recompute_entries([rec("run-003"), rec("run-005")], [pinned],
                                benchmark_version="bench-v1")
    assert again.mean_gain == 9.0
    assert again.support == 2          # 没被覆盖的字段仍然刷新


def test_an_unknown_override_key_is_kept_but_not_applied():
    [entry] = recompute_entries([rec("run-003"), rec("run-005")], [],
                                benchmark_version="bench-v1")
    pinned = entry.with_overrides({"nonsense": 1})
    assert pinned.status == entry.status
    assert pinned.overrides == {"nonsense": 1}
    unknown_status = entry.with_overrides({"status": "maybe"})
    assert unknown_status.status == entry.status


def test_contradictions_are_kept_in_numeric_run_order_under_shuffled_input():
    records = [rec(f"run-{n:03d}", gain=-0.01, regressions=["social_regression"])
               for n in (12, 3, 9, 5, 7)]
    [entry] = recompute_entries(records, [], benchmark_version="bench-v1")
    assert [c["run_id"] for c in entry.contradictions] == [
        "run-003", "run-005", "run-007", "run-009", "run-012"
    ]


def test_unpadded_run_ids_order_numerically():
    records = [rec(f"run-{n}", gain=-0.01, regressions=["social_regression"])
               for n in (10, 9)]
    [entry] = recompute_entries(records, [], benchmark_version="bench-v1")
    assert [c["run_id"] for c in entry.contradictions] == ["run-9", "run-10"]


def test_a_record_without_a_baseline_is_skipped_not_fatal():
    bad = rec("run-003")
    bad["evidence"]["baseline_model_id"] = None
    good = [rec("run-005"), rec("run-007")]
    entries = recompute_entries([bad] + good, [], benchmark_version="bench-v1")
    assert len(entries) == 1 and entries[0].support == 2


def test_integer_perturbations_render_without_a_decimal_point():
    key = strategy_key(dimension="tom", action="clone", mode="batch",
                       perturbation={"sft_steps": 4}, source_recipe_id="r")
    assert key["perturbation"]["sft_steps"] == {"num": "4"}


def test_untouched_entries_become_stale():
    [old] = recompute_entries([rec("run-003"), rec("run-005", benchmark="bench-v0")], [],
                              benchmark_version="bench-v0")
    [now] = recompute_entries([], [old], benchmark_version="bench-v1")
    assert now.status == "stale"
```

- [ ] **Step 2: 运行确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_se_experience.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'trainscheduler.experience'`

- [ ] **Step 3: 实现**

```python
# src/trainscheduler/experience.py
"""策略经验的纯函数层(设计 4、6.1、6.3)。

不读写产物树,不导入业务包。签名含扰动值;分桶只含扰动键名,值收进 value_examples。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field, fields, replace
from typing import Any

ATTR_HIGH = "high"
ATTR_MEDIUM = "medium"
ATTR_LOW = "low"
ATTRIBUTIONS = (ATTR_HIGH, ATTR_MEDIUM, ATTR_LOW)

STATUS_INSUFFICIENT = "insufficient"
STATUS_EFFECTIVE = "effective"
STATUS_DEPRECATED = "deprecated"
STATUS_STALE = "stale"
STATUSES = (STATUS_INSUFFICIENT, STATUS_EFFECTIVE, STATUS_DEPRECATED, STATUS_STALE)

MIN_SUPPORT = 2
MAX_CONTRADICTIONS = 5

# 维度取自诊断载荷,要拼进文件名:只允许单段、无控制字符、不以点开头
_DIMENSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

# dimension, action, mode, 扰动键名, 源配方, 对照基线
Bucket = tuple[str, str, str, tuple[str, ...], str, str]


def safe_dimension(dimension: object) -> str | None:
    """能安全拼进文件名的维度;不合法返回 None。"""
    if not isinstance(dimension, str) or not _DIMENSION.fullmatch(dimension):
        return None
    if ".." in dimension:
        return None
    return dimension


def _canonical_value(value: object) -> dict[str, Any]:
    """把扰动值规范化成可比较、可序列化的形状。int 与 float 同值等价。"""
    if isinstance(value, bool):
        return {"bool": value}
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"perturbation value {value!r} is not a finite number")
        text = repr(number)
        if text.endswith(".0"):
            text = text[:-2]   # 整数值显示成 4,不是 4.0
        return {"num": text}
    if isinstance(value, str):
        return {"str": value}
    raise ValueError(
        f"perturbation value {value!r} must be a string, a number or a bool"
    )


def strategy_key(
    *, dimension: str, action: str, mode: str, perturbation: dict, source_recipe_id: str
) -> dict:
    """精确签名:扰动带值。键按名字排序,值规范化,所以同输入同签名。"""
    if not isinstance(perturbation, dict):
        raise ValueError(f"perturbation must be a mapping, got {type(perturbation).__name__}")
    values = {k: _canonical_value(v) for k, v in sorted(perturbation.items())}
    return {
        "dimension": dimension,
        "action": action,
        "mode": mode,
        "perturbation": values,
        "source_recipe_id": source_recipe_id,
    }


def bucket_key(key: dict, baseline_model_id: str) -> Bucket:
    """分桶键:扰动只看键名不看值;对照基线进桶,否则不同基座的同类策略会撞同一个 id。"""
    return (
        key["dimension"],
        key["action"],
        key["mode"],
        tuple(sorted(key["perturbation"])),
        key["source_recipe_id"],
        baseline_model_id,
    )


def digest(bucket: Bucket) -> str:
    blob = json.dumps(bucket, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:8]


def entry_id(dimension: str | None, bucket: Bucket) -> str:
    name = safe_dimension(dimension)
    suffix = digest(bucket)
    return f"exp-{name}-{suffix}" if name is not None else f"exp-{suffix}"


def attribution_for(action: str, perturbation: dict) -> str:
    """只有单键扰动的 clone 能把结果归到一个变量上。"""
    if action == "clone" and isinstance(perturbation, dict) and len(perturbation) == 1:
        return ATTR_MEDIUM
    return ATTR_LOW


def _value_examples(records: list[dict]) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for record in records:
        for name, encoded in record["strategy_key"]["perturbation"].items():
            shown = (
                str(encoded["bool"]).lower() if "bool" in encoded
                else encoded["num"] if "num" in encoded
                else encoded["str"]
            )
            per_key = counts.setdefault(name, {})
            per_key[shown] = per_key.get(shown, 0) + 1
    return {name: dict(sorted(values.items())) for name, values in sorted(counts.items())}


@dataclass(frozen=True)
class EntryState:
    entry_id: str
    bucket: Bucket
    support: int
    wins: int
    losses: int
    mean_gain: float
    contradictions: tuple[dict, ...]
    value_examples: dict
    status: str
    evidence_scope: str
    baseline_model_id: str
    benchmark_version: str
    overrides: dict = field(default_factory=dict)

    def with_overrides(self, overrides: dict) -> EntryState:
        """人写过的字段原样保留。status 只认四个已知取值;不认识的键不应用,仍留在覆盖表里让人看见。"""
        known = {f.name for f in fields(self)} - {"overrides"}
        changes: dict = {}
        for name, value in overrides.items():
            if name not in known:
                continue
            if name == "status" and value not in STATUSES:
                continue
            changes[name] = value
        return replace(self, overrides=dict(overrides), **changes)

    def to_payload(self) -> dict:
        return {
            "strategy_key": {
                "dimension": self.bucket[0], "action": self.bucket[1], "mode": self.bucket[2],
                # 条目按扰动键名分桶;具体的值在 value_examples 里
                "perturbation_keys": list(self.bucket[3]),
                "source_recipe_id": self.bucket[4],
            },
            "support": self.support,
            "wins": self.wins,
            "losses": self.losses,
            "mean_gain": self.mean_gain,
            "contradictions": [dict(c) for c in self.contradictions],
            "value_examples": dict(self.value_examples),
            "status": self.status,
            "evidence_scope": self.evidence_scope,
            "baseline_model_id": self.baseline_model_id,
            "benchmark_version": self.benchmark_version,
            "overrides": dict(self.overrides),
        }

    @classmethod
    def from_payload(cls, entry_id_: str, payload: dict) -> EntryState:
        key = payload["strategy_key"]
        bucket = bucket_key(
            {
                "dimension": key["dimension"], "action": key["action"], "mode": key["mode"],
                "perturbation": {name: None for name in key["perturbation_keys"]},
                "source_recipe_id": key["source_recipe_id"],
            },
            payload["baseline_model_id"],
        )
        return cls(
            entry_id=entry_id_,
            bucket=bucket,
            support=payload["support"],
            wins=payload["wins"],
            losses=payload["losses"],
            mean_gain=payload["mean_gain"],
            contradictions=tuple(payload["contradictions"]),
            value_examples=payload["value_examples"],
            status=payload["status"],
            evidence_scope=payload["evidence_scope"],
            baseline_model_id=payload["baseline_model_id"],
            benchmark_version=payload["benchmark_version"],
            overrides=payload.get("overrides", {}),
        )


def _eligible(record: dict, *, benchmark_version: str) -> bool:
    """能不能进聚合:设计 6.3 的资格表。缺对照基线的记录跳过,不让一条坏记录终止整次重算。"""
    if record.get("attribution") != ATTR_MEDIUM:
        return False
    outcome, evidence = record.get("outcome") or {}, record.get("evidence") or {}
    if not isinstance(outcome, dict) or not isinstance(evidence, dict):
        return False
    base = evidence.get("baseline_model_id")
    if not isinstance(base, str) or not base:
        return False
    gain = outcome.get("gain")
    if isinstance(gain, bool) or not isinstance(gain, (int, float)):
        return False
    if not math.isfinite(float(gain)):
        return False
    if evidence.get("grade") != "real":
        return False
    return evidence.get("benchmark_version") == benchmark_version


def _run_order(run_id: str) -> tuple[int, str]:
    match = re.match(r"^run-([0-9]+)$", run_id)
    return (int(match.group(1)), run_id) if match else (-1, run_id)


def recompute_entries(
    records: list[dict],
    existing: list[EntryState],
    *,
    benchmark_version: str,
) -> list[EntryState]:
    """由全部记录与基准版本重算条目。纯函数。

    - 只有 attribution 为 medium、有可比增益、real 证据、同基准、且带对照基线的记录参与聚合。
    - 每个对照基线各自成桶:不同基座的证据不会互相覆盖。
    - 求和与反例排序都按 run 编号数值序,与输入顺序无关。
    - 废弃阈值用全部反例计数;载荷只存最近五条。平均增益不为正也不给 effective。
    - 本轮没有匹配记录、而旧条目属于别的基准版本时,旧条目转 stale,但人写过的字段不动。
    """
    grouped: dict[Bucket, list[dict]] = {}
    for record in records:
        if not isinstance(record, dict) or not _eligible(record, benchmark_version=benchmark_version):
            continue
        base = record["evidence"]["baseline_model_id"]
        grouped.setdefault(bucket_key(record["strategy_key"], base), []).append(record)

    out: list[EntryState] = []
    for bucket, group in sorted(grouped.items()):
        ordered = sorted(group, key=lambda r: _run_order(r["run_id"]))
        gains = [float(r["outcome"]["gain"]) for r in ordered]
        wins = sum(1 for g in gains if g > 0)
        losses = sum(1 for g in gains if g < 0)
        mean = round(sum(gains) / len(gains), 6)
        all_contradictions = [
            {
                "run_id": r["run_id"],
                "gain": float(r["outcome"]["gain"]),
                "regression_kinds": sorted(r["outcome"].get("regressions") or []),
            }
            for r in ordered
            if float(r["outcome"]["gain"]) < 0 or (r["outcome"].get("regressions") or [])
        ]
        # 阈值用全部反例计数;只把最近五条存进载荷
        contradictions = tuple(all_contradictions[-MAX_CONTRADICTIONS:])
        support = len(ordered)
        if support < MIN_SUPPORT:
            status = STATUS_INSUFFICIENT
        elif len(all_contradictions) * 2 >= support:
            status = STATUS_DEPRECATED
        elif mean <= 0:
            # 支持数够但平均增益不为正:它没有变好,不给注入
            status = STATUS_DEPRECATED
        else:
            status = STATUS_EFFECTIVE
        dimension = bucket[0]
        state = EntryState(
            entry_id=entry_id(dimension, bucket),
            bucket=bucket,
            support=support,
            wins=wins,
            losses=losses,
            mean_gain=mean,
            contradictions=contradictions,
            value_examples=_value_examples(ordered),
            status=status,
            evidence_scope="real",
            baseline_model_id=bucket[5],
            benchmark_version=benchmark_version,
        )
        previous = next((e for e in existing if e.entry_id == state.entry_id), None)
        if previous is not None:
            # 数值字段取新算的,覆盖表沿用旧的:人写过的状态不会被重算冲掉
            state = state.with_overrides(previous.overrides)
        out.append(state)

    touched = {state.entry_id for state in out}
    for previous in existing:
        if previous.entry_id in touched:
            continue
        if previous.benchmark_version != benchmark_version:
            # 转 stale 也不能盖掉人写过的状态
            out.append(replace(previous, status=STATUS_STALE).with_overrides(previous.overrides))
    return out
```

注意 `state = state.with_overrides(previous.overrides)` 一行：旧条目存在时，重算出的数值字段取新的，覆盖表里列出的字段取人写的值；覆盖表本身原样保留。

- [ ] **Step 4: 运行确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_se_experience.py -q`
Expected: PASS（24 个用例左右，含参数化展开）

- [ ] **Step 5: 提交**

```bash
git add src/trainscheduler/experience.py tests/test_se_experience.py
git commit -m "feat(trainscheduler): 策略经验的纯函数层:签名、分桶、归因与重算"
```

---

### Task 4: 读写信封层

**Files:**
- Create: `src/trainscheduler/experience_port.py`
- Test: `tests/test_se_port.py`（新建）

**Interfaces:**
- Consumes: `experience.*`（Task 3）、`recipes_port.compute_gain`、`controlplane.envelope`、`controlplane.gitstore`、`controlplane.paths`、`controlplane.evidence`
- Produces:
  - `StrategyExperienceSummary`（冻结）：`entries: tuple[EntryState, ...]`、`rendered: tuple[str, ...]`、`truncated: int`、`ignored_records: tuple[str, ...]`
  - `collect_record(store, run_id) -> tuple[dict | None, list[str]]`：记录与忽略原因
  - `record_and_recompute(store, run_id, *, config) -> None`
  - `select_entries(store, *, dimension, benchmark_version, base_model_id, limit=3) -> StrategyExperienceSummary`
  - `load_entries(store) -> tuple[list[EntryState], list[str]]`：条目与读不出的文件

- [ ] **Step 1: 写失败测试**

```python
# tests/test_se_port.py
"""读写信层:落账幂等、信封形状、条目聚合、选取与渲染(设计 4、5、6.1、6.2)。

增益只由 compute_gain 算:它读候选报告的 baseline_ref 找基线模型的 run,再取那份报告的同维度分。
所以每个用例都要先写一条基线轮并登记 zengen-v1,否则 gain 恒为空、条目永远进不了聚合。
"""
import pytest

from controlplane.paths import strategy_entry_file, strategy_experience_note_file
from trainscheduler.config import SchedulerConfig
from trainscheduler.experience_port import (
    StrategyExperienceSummary,
    load_entries,
    record_and_recompute,
    select_entries,
)
from ts_fixtures import env, make_store, write_decision, write_report, write_round, write_trace

BENCH = "bench-v1"
BASE = "zengen-v1"


def _previous_run(run_id: str) -> str:
    """产出这一轮的那次规划的来源 run:run-005 -> run-004。测试里编号都是整数。"""
    number = int(run_id.split("-")[1])
    return f"run-{number - 1:03d}"


def _baseline(store):
    """基线轮:分数 0.40,晋级为 zengen-v1。后面各轮的增益都以它为对照。"""
    write_round(store, "run-001", {"tom": 0.40, "empathy": 0.70}, model_n=1)


def _link_directive(store, run_id, dimension="tom"):
    """把指令的 planner_decision_ref 指回产出这一轮的那次规划。

    write_round 写的指令没有这个引用(人写指令的形状),而落账靠它找到本轮策略。
    """
    write_directive(store, run_id, dimension=dimension,
                    extra={"planner_decision_ref": f"planner-{_previous_run(run_id)}"})


def _round(store, run_id, *, score=0.45, perturbation=None, dimension="tom",
           flags=(), backends="real", decision=True):
    write_round(store, run_id, {dimension: score, "empathy": 0.70}, backends=backends,
                flags=list(flags))
    if decision:
        # 决策属于产出这一轮的那次规划:source 是上一个 run,next_run 是本 run。
        # 指令里的 planner_decision_ref 指回它,落账靠这条链找到本轮的策略。
        write_decision(store, _previous_run(run_id), run_id, action="clone",
                       dimension=dimension,
                       perturbation={"opd_coefficient": 0.2} if perturbation is None else perturbation,
                       source_recipe_id="recipe-run-002")
    write_report(store, run_id, {dimension: score, "empathy": 0.70}, baseline_ref=BASE,
                 benchmark_version=BENCH, backends=backends, flags=list(flags))
    write_trace(store, run_id, [{"task_id": "train", "cost_type": "gpu", "calls": 6}])


def test_record_is_an_envelope(tmp_path):
    store = make_store(tmp_path)
    _baseline(store)
    _round(store, "run-003")
    record_and_recompute(store, "run-003", config=SchedulerConfig(strategy_experience=True))
    envelope = store.read_product(strategy_experience_note_file("run-003"))
    assert envelope.id == "strategy-run-003"
    assert envelope.type == "strategy_experience_note"
    assert envelope.status == "executed"
    assert envelope.links == ["run-003"]
    assert envelope.payload["evidence"]["grade"] == "real"
    assert envelope.payload["outcome"]["gain"] == pytest.approx(0.05)


def test_record_is_idempotent(tmp_path):
    store = make_store(tmp_path)
    _baseline(store)
    _round(store, "run-003")
    cfg = SchedulerConfig(strategy_experience=True)
    record_and_recompute(store, "run-003", config=cfg)
    head = store._git("rev-parse", "HEAD").strip()
    record_and_recompute(store, "run-003", config=cfg)
    assert store._git("rev-parse", "HEAD").strip() == head


def test_switch_off_writes_nothing(tmp_path):
    store = make_store(tmp_path)
    _baseline(store)
    _round(store, "run-003")
    record_and_recompute(store, "run-003", config=SchedulerConfig())
    assert not store.exists(strategy_experience_note_file("run-003"))
    assert load_entries(store) == ([], [])


def test_single_witness_writes_an_insufficient_entry(tmp_path):
    store = make_store(tmp_path)
    _baseline(store)
    _round(store, "run-003")
    record_and_recompute(store, "run-003", config=SchedulerConfig(strategy_experience=True))
    entries, broken = load_entries(store)
    assert not broken
    assert len(entries) == 1 and entries[0].status == "insufficient"
    assert entries[0].support == 1
    assert store.exists(strategy_entry_file(entries[0].entry_id))


def test_second_witness_promotes_the_entry(tmp_path):
    store = make_store(tmp_path)
    cfg = SchedulerConfig(strategy_experience=True)
    _baseline(store)
    _round(store, "run-003", score=0.45)
    record_and_recompute(store, "run-003", config=cfg)
    _round(store, "run-005", score=0.43)
    record_and_recompute(store, "run-005", config=cfg)
    entries, broken = load_entries(store)
    assert not broken
    [entry] = entries
    assert entry.status == "effective" and entry.support == 2
    assert entry.value_examples == {"opd_coefficient": {"0.2": 2}}


def test_stub_report_is_ignored_not_crashed(tmp_path):
    store = make_store(tmp_path)
    _baseline(store)
    _round(store, "run-003")
    write_report(store, "run-003", {"tom": 0.45}, stub=True)
    record_and_recompute(store, "run-003", config=SchedulerConfig(strategy_experience=True))
    assert not store.exists(strategy_experience_note_file("run-003"))
    assert load_entries(store) == ([], [])


def test_missing_decision_is_ignored(tmp_path):
    store = make_store(tmp_path)
    _baseline(store)
    _round(store, "run-003", decision=False)
    record_and_recompute(store, "run-003", config=SchedulerConfig(strategy_experience=True))
    assert not store.exists(strategy_experience_note_file("run-003"))


def test_corrupt_record_is_reported_not_swallowed(tmp_path):
    store = make_store(tmp_path)
    _baseline(store)
    _round(store, "run-003")
    store.write_product(
        strategy_experience_note_file("run-003"),
        env("strategy-run-003", "strategy_experience_note", "executed", {"junk": 1}),
        "seed junk",
    )
    summary = select_entries(store, dimension="tom", benchmark_version=BENCH, base_model_id=BASE)
    assert summary.ignored_records


def test_select_filters_by_base_and_benchmark(tmp_path):
    store = make_store(tmp_path)
    cfg = SchedulerConfig(strategy_experience=True)
    _baseline(store)
    _round(store, "run-003", score=0.45)
    record_and_recompute(store, "run-003", config=cfg)
    _round(store, "run-005", score=0.43)
    record_and_recompute(store, "run-005", config=cfg)
    assert select_entries(store, dimension="tom", benchmark_version="bench-v0",
                          base_model_id=BASE).entries == ()
    assert select_entries(store, dimension="tom", benchmark_version=BENCH,
                          base_model_id="zengen-v9").entries == ()
    assert len(select_entries(store, dimension="tom", benchmark_version=BENCH,
                              base_model_id=BASE).entries) == 1


def test_select_caps_at_three_and_reports_truncation(tmp_path):
    store = make_store(tmp_path)
    cfg = SchedulerConfig(strategy_experience=True)
    _baseline(store)
    keys = ("opd_coefficient", "sft_steps", "grpo_steps", "grpo_config.learning_rate")
    for i, key in enumerate(keys):
        run_id = f"run-{10 + i:03d}"
        _round(store, run_id, score=0.45, perturbation={key: 2})
        record_and_recompute(store, run_id, config=cfg)
        twin = f"run-{20 + i:03d}"
        _round(store, twin, score=0.45, perturbation={key: 2})
        record_and_recompute(store, twin, config=cfg)
    summary = select_entries(store, dimension="tom", benchmark_version=BENCH,
                             base_model_id=BASE, limit=3)
    assert len(summary.entries) == 3
    assert summary.truncated == 1


def test_rendered_line_carries_the_perturbation_value(tmp_path):
    store = make_store(tmp_path)
    cfg = SchedulerConfig(strategy_experience=True)
    _baseline(store)
    _round(store, "run-003", score=0.45)
    record_and_recompute(store, "run-003", config=cfg)
    _round(store, "run-005", score=0.43)
    record_and_recompute(store, "run-005", config=cfg)
    summary = select_entries(store, dimension="tom", benchmark_version=BENCH, base_model_id=BASE)
    assert summary.rendered
    assert "opd_coefficient" in summary.rendered[0]
    assert "0.2" in summary.rendered[0]


def test_a_truncated_record_file_does_not_crash_selection(tmp_path):
    """半截 YAML(崩溃留下的)要降级成 ignored,不能让 select_entries 抛。"""
    store = make_store(tmp_path)
    _baseline(store)
    _round(store, "run-003")
    record_and_recompute(store, "run-003", config=SchedulerConfig(strategy_experience=True))
    rel = strategy_experience_note_file("run-003")
    (store.repo_root / rel).write_text("id: strategy-run-003\npayload: {broken", encoding="utf-8")
    summary = select_entries(store, dimension="tom", benchmark_version=BENCH, base_model_id=BASE)
    assert summary.ignored_records


def test_a_record_missing_run_id_is_skipped_not_fatal(tmp_path):
    store = make_store(tmp_path)
    _baseline(store)
    _round(store, "run-003")
    cfg = SchedulerConfig(strategy_experience=True)
    record_and_recompute(store, "run-003", config=cfg)
    rel = strategy_experience_note_file("run-003")
    env_ = store.read_product(rel)
    payload = dict(env_.payload)
    payload.pop("run_id", None)
    store.write_product(rel, env(env_.id, env_.type, env_.status, payload, [payload.get("run_id") or "run-003"]),
                        "テスト: drop run_id")
    _round(store, "run-005")
    record_and_recompute(store, "run-005", config=cfg)      # 不许抛
    summary = select_entries(store, dimension="tom", benchmark_version=BENCH, base_model_id=BASE)
    assert summary.ignored_records


def test_an_unparseable_entry_is_not_overwritten(tmp_path):
    """手改坏的条目文件原样留着:重算跳过它,不重建、不丢覆盖表。"""
    store = make_store(tmp_path)
    _baseline(store)
    cfg = SchedulerConfig(strategy_experience=True)
    _round(store, "run-003", score=0.45)
    record_and_recompute(store, "run-003", config=cfg)
    entries, _ = load_entries(store)
    [entry] = entries
    rel = strategy_entry_file(entry.entry_id)
    (store.repo_root / rel).write_text("not: a valid entry\n", encoding="utf-8")
    before = (store.repo_root / rel).read_text(encoding="utf-8")
    _round(store, "run-005", score=0.43)
    record_and_recompute(store, "run-005", config=cfg)
    assert (store.repo_root / rel).read_text(encoding="utf-8") == before


def test_empty_store_renders_nothing(tmp_path):
    store = make_store(tmp_path)
    summary = select_entries(store, dimension="tom", benchmark_version=BENCH, base_model_id=BASE)
    assert summary == StrategyExperienceSummary((), (), 0, ())
```

上面用到的四个构造器签名已对着 `tests/ts_fixtures.py` 核实：`write_round(store, run_id, scores, *, model_n=None, baseline_ref="", candidate_status="effective", backends="mock", flags=None, dimension="tom")`；`write_decision(store, source, next_run, *, status="executed", action="new", dimension="tom", source_recipe_id=None, mode="batch", perturbation=None)`；`write_report(store, run_id, scores=None, *, baseline_ref="", flags=None, backends="mock", stub=False, benchmark_version="bench-v1")`；`write_trace(store, run_id, records)`，`records` 是 `cost_records` 的列表。

- [ ] **Step 2: 运行确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_se_port.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'trainscheduler.experience_port'`

- [ ] **Step 3: 实现**

```python
# src/trainscheduler/experience_port.py
"""策略经验的读写层(设计 4、5、6)。唯一的 I/O 入口;增益只经 recipes_port 转导。"""
from __future__ import annotations

from dataclasses import dataclass

from controlplane.envelope import CURRENT_SCHEMA_VERSION, Envelope, OwnerRef
from controlplane.gitstore import GitStore
from controlplane.paths import (
    candidate_checkpoint_file,
    planner_decision_file,
    run_trace_file,
    strategy_entry_file,
    strategy_experience_note_file,
)
from trainscheduler.config import SchedulerConfig
from trainscheduler.run_ids import list_run_dirs
from trainscheduler.experience import (
    EntryState,
    attribution_for,
    bucket_key,
    recompute_entries,
    safe_dimension,
    strategy_key,
)
from trainscheduler.recipes_port import compute_gain, read_report_payload, regression_flags

NOW = "2026-10-20T00:00:00Z"
NOTE_TYPE = "strategy_experience_note"
ENTRY_TYPE = "strategy_entry"
ENTRY_STATUS = "executed"
RENDER_BUDGET = 900
EXPERIENCE_DIR = "control-plane/experience"


@dataclass(frozen=True)
class StrategyExperienceSummary:
    entries: tuple
    rendered: tuple[str, ...]
    truncated: int
    ignored_records: tuple[str, ...]


def _cost(store: GitStore, run_id: str) -> dict:
    """调用次数求和;没有记录时写 None(不写 0,0 会被读成免费)。"""
    path = run_trace_file(run_id)
    if not store.exists(path):
        return {"calls": None}
    records = store.read_product(path).payload.get("cost_records") or []
    calls = [r.get("calls") for r in records if isinstance(r.get("calls"), (int, float))]
    return {"calls": int(sum(calls)) if calls else None}


def _decision_that_produced(store: GitStore, run_id: str) -> dict | None:
    """找到产出这一轮的决策。

    不能读 runs/<run_id>/planner_decision.yaml:那是"这一轮结束后为下一轮做的规划",
    在规划这一轮的当下还不存在(plan_handler 只有在它为某 run 尚无决策时才做新规划)。
    真正的来源是指令里的 planner_decision_ref——它指向的决策,其 next_run_id 正是这一轮。
    人写的指令没有这个引用,返回 None(人写的命令没有策略可归因)。
    """
    path = f"{run_dir(run_id)}/directive.yaml"
    if not store.exists(path):
        return None
    ref = store.read_product(path).payload.get("planner_decision_ref")
    if not isinstance(ref, str) or not ref.startswith("planner-"):
        return None
    source = ref[len("planner-"):]
    decision_path = planner_decision_file(source)
    if not store.exists(decision_path):
        return None
    decision = store.read_product(decision_path).payload.get("decision")
    return decision if isinstance(decision, dict) else None


def collect_record(store: GitStore, run_id: str) -> tuple[dict | None, list[str]]:
    """把一轮压成记录载荷。返回 (载荷或 None, 忽略原因)。只读。"""
    ignored: list[str] = []
    decision = _decision_that_produced(store, run_id)
    if decision is None:
        return None, [f"{run_id}: no planner decision produced this run"]

    payload = read_report_payload(store, run_id)
    if payload is None:
        return None, [f"{run_id}: no diagnostic report"]
    grade = payload.get("backends")
    if payload.get("stub") or grade not in ("mock", "real"):
        return None, [f"{run_id}: diagnostic report is not usable evidence"]

    dimension = decision.get("target_dimension")
    if not safe_dimension(dimension):
        # 失败关闭:不安全的维度不进记录。记成 "None" 会造出一个假的桶,比丢掉这一轮更糟
        return None, [f"{run_id}: dimension {dimension!r} is not a safe path segment"]

    action = str(decision.get("action") or "")
    perturbation = decision.get("perturbation") or {}
    if not isinstance(perturbation, dict):
        return None, [f"{run_id}: perturbation is not a mapping"]

    promoted = False
    candidate_path = candidate_checkpoint_file(run_id)
    if store.exists(candidate_path):
        promoted = store.read_product(candidate_path).status == "effective"

    gain_result = compute_gain(store, run_id, dimension)
    record = {
        "run_id": run_id,
        "strategy_key": strategy_key(
            dimension=str(dimension),
            action=action,
            mode=str(decision.get("mode") or ""),
            perturbation=perturbation,
            source_recipe_id=str(decision.get("source_recipe_id") or ""),
        ),
        "outcome": {
            "gain": gain_result.gain,
            "promoted": promoted,
            "regressions": sorted(
                {flag.get("kind") for flag in regression_flags(payload) if flag.get("kind")}
            ),
            "cost": _cost(store, run_id),
        },
        "evidence": {
            "grade": grade,
            # 对照基线取自报告:增益是"候选减它",条目必须按它分桶,而不是按当前晋级模型
            "baseline_ref": payload.get("baseline_ref"),
            "baseline_model_id": payload.get("baseline_ref"),
            "benchmark_version": payload.get("benchmark_version"),
        },
        "attribution": attribution_for(action, perturbation),
        "ignored": ignored,
    }
    return record, ignored


def _write(store: GitStore, rel: str, *, id_: str, type_: str, links: list[str],
           payload: dict, message: str) -> None:
    env = Envelope(
        id=id_, type=type_, version=1, created_at=NOW, created_by=OwnerRef(),
        status=ENTRY_STATUS, schema_version=CURRENT_SCHEMA_VERSION,
        links=links, payload=payload,
    )
    store.write_product(rel, env, message)


def load_entries(store: GitStore) -> tuple[list[EntryState], list[str]]:
    """读全部能解析的条目。返回 (条目, 读不出的文件名)。

    读不出的条目不许静默消失:调用方要把它当"不许覆盖"的名单,否则一次手编坏文件
    会在下一次重算里被无痕重建,人写过的覆盖表随之丢失。
    """
    entries: list[EntryState] = []
    broken: list[str] = []
    for rel in store.list_products(EXPERIENCE_DIR):
        try:
            env = store.read_product(rel)
            entries.append(EntryState.from_payload(env.id, env.payload))
        except Exception:                       # YAML 解析失败、缺键、类型错,一律当读不出
            broken.append(rel)
    return entries, broken


def _record_is_usable(record: object) -> bool:
    """记录必须正好具备 recompute_entries 消费的形状;缺一项就整条跳过,不让它终止整次重算。"""
    if not isinstance(record, dict):
        return False
    run_id = record.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return False
    evidence, outcome = record.get("evidence"), record.get("outcome")
    if not isinstance(evidence, dict) or not isinstance(outcome, dict):
        return False
    base = evidence.get("baseline_model_id")
    if not isinstance(base, str) or not base:
        return False
    key = record.get("strategy_key")
    if not isinstance(key, dict):
        return False
    try:
        bucket_key(key, base)
    except (KeyError, TypeError, ValueError):
        return False
    perturbation = key.get("perturbation")
    if not isinstance(perturbation, dict):
        return False
    for encoded in perturbation.values():
        # _value_examples 只会读 bool / num / str 三种标记之一;缺了它会在聚合处 KeyError
        if not isinstance(encoded, dict):
            return False
        marks = [m for m in ("bool", "num", "str") if m in encoded]
        if len(marks) != 1 or not isinstance(encoded[marks[0]], (bool, str)):
            return False
    return True


def _all_records(store: GitStore) -> tuple[list[dict], list[str]]:
    """全部可用记录。list_products 不递归,所以先列 run 目录再逐目录读记录文件。"""
    records: list[dict] = []
    ignored: list[str] = []
    for run_id in list_run_dirs(store):
        rel = strategy_experience_note_file(run_id)
        if not store.exists(rel):
            continue
        try:
            env = store.read_product(rel)
        except Exception:                       # 半截 YAML 之类:跳过,不抛
            ignored.append(f"{rel}: unreadable file")
            continue
        if not _record_is_usable(env.payload):
            ignored.append(f"{env.id}: unusable record payload")
            continue
        records.append(env.payload)
    return records, ignored


def record_and_recompute(store: GitStore, run_id: str, *, config: SchedulerConfig) -> None:
    """落账并在同一调用内重算条目。开关关闭时不写任何东西。"""
    if not config.strategy_experience:
        return
    record, _ = collect_record(store, run_id)
    if record is None:
        return
    _write(
        store, strategy_experience_note_file(run_id),
        id_=f"strategy-{run_id}", type_=NOTE_TYPE, links=[run_id],
        payload=record, message=f"trainscheduler: strategy experience for {run_id}",
    )

    benchmark = (record["evidence"] or {}).get("benchmark_version")
    if not isinstance(benchmark, str):
        return
    records, _ = _all_records(store)
    existing, broken = load_entries(store)
    # 读不出的条目按文件名记下来:id 相同的那条不许被重算覆盖
    broken_ids = {rel.rsplit("/", 1)[-1][:-5] for rel in broken}
    states = recompute_entries(records, existing, benchmark_version=benchmark)
    for state in states:
        if state.entry_id in broken_ids:
            continue                            # 手改坏的文件留给人看,不覆盖
        previous = next((e for e in existing if e.entry_id == state.entry_id), None)
        if previous is not None and previous.to_payload() == state.to_payload():
            continue
        _write(
            store, strategy_entry_file(state.entry_id),
            id_=state.entry_id, type_=ENTRY_TYPE, links=[run_id],
            payload=state.to_payload(), message=f"trainscheduler: strategy entry {state.entry_id}",
        )


def _render(entry: EntryState) -> str:
    """value_examples 是 {键名: {值: 次数}};渲染成 name=值×次数,值按字符串排序。"""
    values = [
        f"{name}=" + "/".join(f"{value}×{count}" for value, count in counts.items())
        for name, counts in entry.value_examples.items()
    ]
    shown = ", ".join(values) if values else "无"
    return (
        f"策略 {entry.bucket[1]} {entry.bucket[2]} 扰动 {shown}"
        f" · 证据 {entry.support} 条 · 平均增益 {entry.mean_gain:+.3f}"
        f" · 反例 {len(entry.contradictions)}"
    )


def select_entries(
    store: GitStore, *, dimension: str, benchmark_version: str,
    base_model_id: str, limit: int = 3,
) -> StrategyExperienceSummary:
    """筛可用条目,排序截断,渲染。从不抛:任何读不出的文件都降级成 ignored。"""
    _, ignored = _all_records(store)
    entries, broken = load_entries(store)
    usable = [
        e for e in entries
        if e.status == "effective" and e.bucket[0] == dimension
        and e.benchmark_version == benchmark_version
        and e.baseline_model_id == base_model_id
    ]
    usable.sort(key=lambda e: (-e.support, -e.mean_gain, e.entry_id))
    limit = max(0, limit)
    chosen: list = []
    lines: list[str] = []
    used = 0
    for entry in usable[:limit]:
        line = _render(entry)
        if used + len(line) > RENDER_BUDGET:
            break                               # 预算用尽就停,不放矮的进来挤掉高的
        chosen.append(entry)
        lines.append(line)
        used += len(line)
    truncated = len(usable) - len(chosen)
    return StrategyExperienceSummary(tuple(chosen), tuple(lines), truncated,
                                     tuple(ignored) + tuple(broken))
```

- [ ] **Step 4: 运行确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_se_port.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/trainscheduler/experience_port.py tests/test_se_port.py
git commit -m "feat(trainscheduler): 策略经验的读写层:落账幂等、重算条目、选取渲染"
```

---

### Task 5: 上下文第七段与模板

**Files:**
- Modify: `src/trainplanner/context.py`, `src/trainplanner/prompt.py`, `src/trainplanner/templates.py`, `src/trainplanner/examples.py`
- Test: `tests/test_se_context.py`（新建）, `tests/test_tp_context.py`, `tests/test_tp_prompt.py`, `tests/test_tp_templates.py`

**Interfaces:**
- Consumes: `StrategyExperienceSummary.rendered`
- Produces:
  - `PlanningContext.strategy_experience: list[str] = []`，`to_dict()` 含该键
  - `PlanningContext.from_dict` 接受 6 键或 7 键（向后兼容）
  - `prompt.PLACEHOLDERS` 含 `"strategy_experience"`；`placeholder_values` 提供对应 JSON
  - `templates.PLANNER_V2`、`templates.PLANNER_V2_VERSION == "v2"`，`install_default_template` 同时安装 v1 与 v2

- [ ] **Step 1: 写失败测试**

```python
# tests/test_se_context.py
"""上下文第七段与 PLANNER_V2:向后兼容、渲染稳定(设计 5、6.2)。"""
import pytest

from trainplanner.context import InvalidContextError, PlanningContext
from trainplanner.examples import example_context_dict
from trainplanner.prompt import PLACEHOLDERS, render_prompt
from trainplanner.templates import PLANNER_V1, PLANNER_V2, PLANNER_V2_VERSION


def test_six_key_context_still_parses():
    """旧产物没有第七键。示例字典自己已带这一键,所以要真的删掉再解析。"""
    raw = example_context_dict()
    del raw["strategy_experience"]
    ctx = PlanningContext.from_dict(raw)
    assert ctx.strategy_experience == []


@pytest.mark.parametrize("bad", ["abc", {"a": "b"}, None, 5, 0, ""])
def test_a_non_list_seventh_value_is_rejected(bad):
    """非 list 的第七段要被拒,不许静默强转:字符串会拆成字符,None 会变成空表。"""
    raw = example_context_dict()
    raw["strategy_experience"] = bad
    with pytest.raises(InvalidContextError):
        PlanningContext.from_dict(raw)


def test_seventh_key_round_trips():
    raw = example_context_dict()
    raw["strategy_experience"] = ["策略 clone batch 扰动 opd_coefficient=0.2 · 证据 2 条"]
    ctx = PlanningContext.from_dict(raw)
    assert ctx.to_dict()["strategy_experience"] == raw["strategy_experience"]


def test_unknown_keys_are_still_rejected():
    raw = example_context_dict()
    raw["strategy_experiance"] = []
    with pytest.raises(InvalidContextError):
        PlanningContext.from_dict(raw)


def test_entries_must_be_non_empty_strings():
    raw = example_context_dict()
    raw["strategy_experience"] = [""]
    with pytest.raises(InvalidContextError):
        PlanningContext.from_dict(raw)


def test_v2_declares_the_placeholder_and_v1_does_not():
    assert "{{strategy_experience}}" in PLANNER_V2
    assert "{{strategy_experience}}" not in PLANNER_V1
    assert "strategy_experience" in PLACEHOLDERS
    assert PLANNER_V2_VERSION == "v2"


def test_empty_section_renders_as_an_empty_list():
    ctx = PlanningContext.from_dict(example_context_dict())
    packed = render_prompt(ctx, PLANNER_V2, PLANNER_V2_VERSION)
    assert "[]" in packed.text


def test_v2_renders_the_entry_between_the_cost_section_and_the_answer():
    """第七段真的出现在提示里,位置在成本段之后、作答要求之前。"""
    raw = example_context_dict()
    raw["strategy_experience"] = ["策略 clone batch 扰动 opd_coefficient=0.2 · 证据 2 条"]
    text = render_prompt(PlanningContext.from_dict(raw), PLANNER_V2, PLANNER_V2_VERSION).text
    assert "opd_coefficient=0.2" in text
    assert text.index("## Cost and gain") < text.index("opd_coefficient=0.2") < text.index("## Your answer")


def test_v1_rendering_is_unchanged_by_the_new_field():
    ctx = PlanningContext.from_dict(example_context_dict())
    before = render_prompt(ctx, PLANNER_V1, "v1").text
    raw = example_context_dict()
    raw["strategy_experience"] = ["策略 clone batch 扰动 opd_coefficient=0.2"]
    after = render_prompt(PlanningContext.from_dict(raw), PLANNER_V1, "v1").text
    assert before == after
```

- [ ] **Step 2: 运行确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_se_context.py -q`
Expected: FAIL，`AttributeError: module 'trainplanner.templates' has no attribute 'PLANNER_V2'`

- [ ] **Step 3: 实现**

`src/trainplanner/context.py`：字段、键表与向后兼容的 `from_dict`。

```python
    cost_gain: list[CostGainEntry] = field(default_factory=list)
    strategy_experience: list[str] = field(default_factory=list)

    _KEYS = ("run_id", "candidate", "diagnostic", "recipes", "recent_decisions", "cost_gain",
             "strategy_experience")
    # 第七段是本轮新增的可选键:旧决策的上下文没有它,仍要能读回
    _OPTIONAL_KEYS = ("strategy_experience",)
```

`__post_init__` 追加：

```python
        if not isinstance(self.strategy_experience, list):
            raise InvalidContextError("strategy_experience: expected a list")
        for line in self.strategy_experience:
            if not isinstance(line, str) or not line.strip():
                raise InvalidContextError(
                    "strategy_experience: every entry must be a non-empty string"
                )
```

`to_dict()` 追加：

```python
            "strategy_experience": list(self.strategy_experience),
```

`from_dict` 改成允许 6 键或 7 键：

```python
    @classmethod
    def from_dict(cls, d: dict) -> PlanningContext:
        # 第七段是新增的可选键:旧产物没有它也要读得回。按"字典里有没有这个键"选键集合,
        # 而不是把校验从 _exact_keys 里拿掉——后者会丢掉"missing keys / unknown keys"的报错措辞,
        # 而既有测试正是按那两句话断言的。
        keys = cls._KEYS if isinstance(d, dict) and "strategy_experience" in d \
            else tuple(k for k in cls._KEYS if k not in cls._OPTIONAL_KEYS)
        _exact_keys(d, keys, "context")
        # 第七段:list 复制一份;非 list 原样交给 __post_init__ 去拒绝,
        # 不在 from_dict 里静默强转(否则 "abc" 会被拆成 ['a','b','c'],None 会变成 [])
        raw_experience = d["strategy_experience"] if "strategy_experience" in d else []
        return cls(
            run_id=d["run_id"],
            candidate=CandidateSummary.from_dict(d["candidate"]),
            diagnostic=DiagnosticSummary.from_dict(d["diagnostic"]),
            recipes=[RecipeSummary.from_dict(x) for x in _list_field(d, "recipes", "context")],
            recent_decisions=[
                DecisionOutcome.from_dict(x) for x in _list_field(d, "recent_decisions", "context")
            ],
            cost_gain=[CostGainEntry.from_dict(x) for x in _list_field(d, "cost_gain", "context")],
            strategy_experience=(
                list(raw_experience) if isinstance(raw_experience, list) else raw_experience
            ),
        )
```

`src/trainplanner/prompt.py`：占位符表与取值。

```python
PLACEHOLDERS = (
    "run_id",
    "candidate",
    "diagnostic",
    "recipes",
    "recent_decisions",
    "cost_gain",
    "strategy_experience",
    "context",
)
```

`placeholder_values` 的返回字典追加一行：

```python
        "strategy_experience": _json(list(ctx.strategy_experience)),
```

`src/trainplanner/templates.py`：版本常量与 V2 模板。V2 是 V1 加一节，插在 `## Cost and gain of recent rounds` 一节之后、`## Your answer` 之前：

```python
PLANNER_V2_VERSION = "v2"

_STRATEGY_SECTION = """
## Strategy experience from earlier rounds
Each line summarises a past strategy family: the action and mode, the exact
perturbation values that were tried, how many comparable rounds support it, the
mean gain, and how many of those rounds contradicted it. These lines come only
from rounds whose evidence grade was real and whose baseline model and benchmark
version match the current ones. Treat them as prior evidence, not as rules:
a strategy with many contradictions is weak, and a strategy that is absent may
simply be untested. Reuse a perturbation value that worked rather than inventing
a new one when the evidence points that way.
{{strategy_experience}}
"""

PLANNER_V2 = PLANNER_V1.replace(
    "\n## Your answer\n", _STRATEGY_SECTION + "\n## Your answer\n"
)
```

`install_default_template` 同时安装两份，但**返回值仍是 v1 的路径**：既有测试
`test_tp_templates.py::test_the_default_template_is_installed_as_an_envelope` 断言
`rel == planner_prompt_file(PLANNER_V1_VERSION)` 并逐字段检查那份信封。改返回值会破坏这个契约，
而且"默认模板"本来就是 v1——v2 是给显式选择用的。

```python
def install_default_template(store: GitStore) -> str:
    """装好 v1 与 v2,返回 v1 的路径:调用方与既有测试都按 v1 的契约走。"""
    rel = install_template(store, PLANNER_V1_VERSION, PLANNER_V1)
    install_template(store, PLANNER_V2_VERSION, PLANNER_V2)
    return rel
```

`src/trainplanner/examples.py`：`_EXAMPLE_CONTEXT` 的返回字典里加 `"strategy_experience": []`。

这一处不能跳过：`tests/test_tp_context.py::test_the_example_round_trips_exactly` 断言
`ctx.to_dict() == example_context_dict()`。`to_dict()` 加上第七个键之后，示例字典没有这个键，
往返就不等了。同批把它补上，测试才能过。

- [ ] **Step 4: 运行确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_se_context.py tests/test_tp_context.py tests/test_tp_prompt.py tests/test_tp_templates.py -q`
Expected: PASS。两处既有测试需要同批改动,改法如下。

`tests/test_tp_context.py` 不需要改:`from_dict` 仍走 `_exact_keys`,`missing keys` /
`unknown keys` 的报错措辞不变,缺键与拼错键的断言照旧成立。

`tests/test_tp_templates.py` 第 32 行必须改。它断言 v1 用满了除 `context` 之外的每个占位符,
而 `PLACEHOLDERS` 现在多了 `strategy_experience`,v1 又不该引用它:

```python
def test_planner_v1_uses_every_section_of_the_context():
    # v1 早于第七段:它不引用 strategy_experience,所以模板契约是"除这两项之外的全部"
    assert set(check_template(PLANNER_V1)) == set(PLACEHOLDERS) - {"context", "strategy_experience"}


def test_planner_v2_uses_every_section_of_the_context():
    assert set(check_template(PLANNER_V2)) == set(PLACEHOLDERS) - {"context"}
```

第二条是新增的:v2 必须用满除 `context` 以外的全部占位符,否则以后再加占位符时没人会发现 v2 漏了它。
`check_template` 与 `PLACEHOLDERS` 该文件已经导入。

若改完仍有红,停下来报 NEEDS_CONTEXT。

- [ ] **Step 5: 提交**

```bash
git add src/trainplanner tests/test_se_context.py
git commit -m "feat(trainplanner): 规划上下文加策略经验第七段与 PLANNER_V2 模板"
```

---

### Task 6: 接入装配与规划处理器

**Files:**
- Modify: `src/trainscheduler/assemble.py`, `src/trainscheduler/plan_handler.py`
- Test: `tests/test_se_loop.py`（新建）

**Interfaces:**
- Consumes: `experience_port.record_and_recompute`、`experience_port.select_entries`、`controlplane.models.latest_promoted_checkpoint`
- Produces:
  - `assemble_context(store, run_id, *, config=None)` 在 `config.strategy_experience` 为真时填第七段；关闭时与现状相同
  - `plan_next_round` 的处理器在 `assemble_context` **之前**调 `record_and_recompute`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_se_loop.py
"""端到端:落账先于装配,提示里出现前几轮的有效扰动值(设计 6.1、6.2)。"""
import pytest

from controlplane.reconcile import Action
from trainscheduler.assemble import assemble_context
from trainscheduler.config import SchedulerConfig
from trainscheduler.experience_port import record_and_recompute
from trainscheduler.plan_handler import make_plan_handler
from trainplanner.backends import HeuristicBackend
from ts_fixtures import make_store, write_decision, write_report, write_round, write_trace

BENCH = "bench-v1"
BASE = "zengen-v1"
ON = SchedulerConfig(strategy_experience=True)


def _baseline(store):
    """基线轮必须在 real 级:compute_gain 拒绝"real 候选对非 real 基线"的跨级比较(R17),
    基线若是默认的 mock,增益恒为空,条目一条也写不出来。"""
    write_round(store, "run-001", {"tom": 0.40, "empathy": 0.70}, model_n=1, backends="real")


def _round(store, run_id, *, score=0.45, dimension="tom", perturbation=None, backends="real"):
    """一整轮加一条已执行的规划决策。基线固定是 zengen-v1,所以增益 = score - 0.40。"""
    write_round(store, run_id, {dimension: score, "empathy": 0.70}, backends=backends)
    # 决策属于产出这一轮的那次规划:source 是上一个 run,next_run 是本 run
    write_decision(store, _previous_run(run_id), run_id, action="clone", dimension=dimension,
                   perturbation={"opd_coefficient": 0.2} if perturbation is None else perturbation,
                   source_recipe_id="recipe-run-002")
    write_report(store, run_id, {dimension: score, "empathy": 0.70}, baseline_ref=BASE,
                 benchmark_version=BENCH, backends=backends)
    write_trace(store, run_id, [{"task_id": "train", "cost_type": "gpu", "calls": 6}])


def test_off_keeps_the_context_seventh_section_empty(tmp_path):
    store = make_store(tmp_path)
    _baseline(store)
    _round(store, "run-003")
    ctx = assemble_context(store, "run-003", config=SchedulerConfig())
    assert ctx.strategy_experience == []


def test_on_injects_matching_entries(tmp_path):
    store = make_store(tmp_path)
    _baseline(store)
    for run_id, score in (("run-003", 0.45), ("run-005", 0.43)):
        _round(store, run_id, score=score)
        record_and_recompute(store, run_id, config=ON)
    ctx = assemble_context(store, "run-005", config=ON)
    assert ctx.strategy_experience
    assert "opd_coefficient" in ctx.strategy_experience[0]
    assert "0.2" in ctx.strategy_experience[0]


def test_handler_records_before_it_assembles(tmp_path):
    """落账在装配之前:即使后面规划失败,这一轮的记录也已经写下。

    run-005 自己的决策必须还不存在,否则处理器走 (c) 分支直接返回,什么都不做。
    """
    store = make_store(tmp_path)
    _baseline(store)
    _round(store, "run-005")
    handler = make_plan_handler(
        backend=HeuristicBackend(),
        config_loader=lambda _store: ON,
        defaults_loader=lambda _store: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    with pytest.raises(RuntimeError):
        handler(store, Action(kind="plan_next_round", target_id="run-005",
                              detail={"run_id": "run-005"}))
    from controlplane.paths import strategy_experience_note_file
    assert store.exists(strategy_experience_note_file("run-005"))
```

这里用到 `write_round`、`write_decision`、`write_report`、`write_trace` 四个构造器，签名与 Task 4 里核实的同一份（见 Task 4 Step 1 末尾）。`make_plan_handler` 必须注入 `config_loader`，否则处理器从树里读到的是默认配置，开关恒为关。

- [ ] **Step 2: 运行确认失败**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_se_loop.py -q`
Expected: FAIL，`assert ctx.strategy_experience` 为空

- [ ] **Step 3: 实现**

`src/trainscheduler/assemble.py`：加两处导入与一段取材。

```python
from controlplane.models import latest_promoted_checkpoint
from trainscheduler.experience_port import select_entries
```

（`read_report_payload` 已由 `trainscheduler.recipes_port` 导入，`l1_scores` 已在本模块定义，都不必再加。）

在 `assemble_context` 的返回值里加第七项：

```python
        return PlanningContext(
            run_id=run_id,
            candidate=_candidate(store, run_id),
            diagnostic=_diagnostic(store, run_id),
            recipes=_recipes(store),
            recent_decisions=_recent_decisions(store, runs, config.recent_decision_window),
            cost_gain=_cost_gain(store, runs, config.cost_gain_window),
            strategy_experience=_strategy_experience(store, run_id, config),
        )
```

```python
def _strategy_experience(store: GitStore, run_id: str, config: SchedulerConfig) -> list[str]:
    """设计 6.2:最低一级分维度、同基座同基准、最多三条。关闭时为空。"""
    if not config.strategy_experience:
        return []
    base = latest_promoted_checkpoint(store)
    if base is None:
        return []
    payload = read_report_payload(store, run_id)
    benchmark = payload.get("benchmark_version") if isinstance(payload, dict) else None
    if not isinstance(benchmark, str) or not benchmark:
        return []
    scores = l1_scores(payload)
    dimension = min(scores, key=lambda name: (scores[name], name))
    summary = select_entries(
        store, dimension=dimension, benchmark_version=benchmark, base_model_id=base.model_id
    )
    return list(summary.rendered)
```

`src/trainscheduler/plan_handler.py`：在装配之前落账。

```python
from trainscheduler.experience_port import record_and_recompute
```

插入位置是 `handle` 里 `config = config_loader(store)` 之后、`defaults = defaults_loader(store)` 之前：
落账只依赖配置与树里的产物，不依赖训练默认值与提示模板，放在最靠前的位置才能保证
"后面的步骤失败也不影响这一轮的记录"。

```python
    def handle(store: GitStore, action: Action) -> None:
        run_id = action.target_id
        path = planner_decision_file(run_id)
        if store.exists(path):
            ...
            return

        config = config_loader(store)
        try:
            record_and_recompute(store, run_id, config=config)
        except Exception:
            # 落账是旁路:它坏了不能挡住规划。坏输入已在 collect_record 里降级为 ignored
            pass
        defaults = defaults_loader(store)
        ...
```

`except Exception` 只包住落账这一句；规划本身的异常照旧由后面的 `except PlannerFailedError` 等分支处理。

- [ ] **Step 4: 运行确认通过**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_se_loop.py tests/test_ts_assemble.py tests/test_ts_plan_handler.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add src/trainscheduler/assemble.py src/trainscheduler/plan_handler.py tests/test_se_loop.py
git commit -m "feat(trainscheduler): 规划处理器落账,装配上下文注入策略经验第七段"
```

---

### Task 7: 边界守卫与收尾

**Files:**
- Create: `tests/test_se_boundaries.py`
- Modify: `pyproject.toml`（若 `trainscheduler` 已在 wheel 包列表里，无需改动——该包已存在）

**Interfaces:**
- Consumes: 全部前序任务
- Produces: 依赖方向与文件头的机器检查

- [ ] **Step 1: 写测试**

```python
# tests/test_se_boundaries.py
"""策略经验的边界:依赖方向、文件头、无外部 agent 写入路径。"""
import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FILES = [REPO / "src/trainscheduler/experience.py", REPO / "src/trainscheduler/experience_port.py"]
FORBIDDEN = {"actio", "mindagent", "diagnostic", "simulator", "factory", "inference_bridge",
             "trainplanner"}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    tops: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            tops |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            tops.add(node.module.split(".")[0])
    return tops


def test_no_forbidden_imports():
    for path in FILES:
        assert not (_imports(path) & FORBIDDEN), path.name


def test_future_import_comes_before_any_other_statement():
    """仓库的写法是模块 docstring 在前、__future__ 紧随其后,所以断言的是"文档字符串之后的第一个语句"。"""
    for path in FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        body = list(tree.body)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]          # 跳过模块文档字符串
        assert body, path.name
        first = body[0]
        assert isinstance(first, ast.ImportFrom) and first.module == "__future__", path.name


def test_recipe_imports_go_through_the_port():
    source = (REPO / "src/trainscheduler/experience_port.py").read_text(encoding="utf-8")
    assert "from trainscheduler.recipes_port import" in source
    assert "from recipestore" not in source
```

- [ ] **Step 2: 运行**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_se_boundaries.py -q`
Expected: PASS

- [ ] **Step 3: 全量相关测试**

Run: `PYTHONDONTWRITEBYTECODE=1 uv run --extra dev pytest tests/test_se_*.py tests/test_ts_*.py tests/test_tp_*.py tests/test_paths.py -q`
Expected: PASS

- [ ] **Step 4: 提交**

```bash
git add tests/test_se_boundaries.py
git commit -m "test(trainscheduler): 策略经验的依赖方向与文件头守卫"
```

---

## 自审记录

| 检查项 | 结果 |
|---|---|
| 设计 §2 目标 G1 至 G7 | G1 Task 4 幂等；G2/G3/G4 Task 3 重算；G5 Task 5；G6 Task 7；G7 Task 4 的 `ignored_records` |
| 设计 §4 两类产物与信封 | Task 1 路径、Task 4 信封与类型 |
| 设计 §5 四个入口函数 | Task 3 三个纯函数、Task 4 两个 I/O 入口 |
| 设计 §6.1 归因六步 | Task 3 `attribution_for`、Task 4 `collect_record` |
| 设计 §6.2 注入四步 | Task 4 `select_entries`、Task 5 第七段 |
| 设计 §6.3 聚合资格表 | Task 3 `_eligible` 与单测逐条覆盖 |
| 设计 §7 SE-1 至 SE-10 | SE-1 Task 3/4 不新建包；SE-2 Task 3 归因；SE-3 Task 7 无桥接导入；SE-4 Task 4 经 `recipes_port`；SE-5 Task 3 全量重算；SE-6 Task 3 基座过滤；SE-7 Task 3 `overrides`；SE-8 Task 4 上限与预算；SE-9 Task 2 配置开关；SE-10 Task 4 信封 |
| 设计 §9 测试分层 | 114 个计划用例分布在 `test_se_paths` 4、`test_se_config` 5、`test_se_experience` 约 30（含参数化）、`test_se_port` 10、`test_se_context` 7、`test_se_loop` 3、`test_se_boundaries` 3 |
| 占位符扫描 | 无 TBD／TODO；所有代码步骤给了可直接粘贴的实现 |
| 类型一致性 | `EntryState.to_payload`／`from_payload`、`StrategyExperienceSummary` 四个字段、`record_and_recompute(store, run_id, *, config)` 在 Task 4 与 Task 6 中一致 |
