"""纯函数层:签名、归因、路径安全、重算(设计 4、6.1、6.3)。"""

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


def test_stale_wins_over_a_human_status_but_keeps_the_other_overrides():
    """证据不在了是事实,不是质量判断:它压过人工设定的状态,但覆盖表本身保留。"""
    [old] = recompute_entries([rec("run-003"), rec("run-005", benchmark="bench-v0")], [],
                              benchmark_version="bench-v0")
    pinned = old.with_overrides({"status": "deprecated", "mean_gain": 9.0})
    [now] = recompute_entries([], [pinned], benchmark_version="bench-v1")
    assert now.status == "stale"
    assert now.overrides == {"status": "deprecated", "mean_gain": 9.0}
    assert now.mean_gain == 9.0


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


def test_malformed_entry_payloads_are_rejected():
    """条目会被排序与渲染:非数字的 support 必须在读入时就被拒,不能留到 sort 处抛 TypeError。"""
    [good] = recompute_entries([rec("run-003"), rec("run-005")], [],
                               benchmark_version="bench-v1")
    payload = good.to_payload()

    for field_name, bad in (("support", "two"), ("mean_gain", "x"), ("wins", None),
                            ("status", "weird"), ("value_examples", [])):
        broken = dict(payload)
        broken[field_name] = bad
        with pytest.raises(ValueError):
            EntryState.from_payload("exp-x", broken)

    broken_key = dict(payload)
    broken_key["strategy_key"] = dict(payload["strategy_key"], perturbation_keys=5)
    with pytest.raises(ValueError):
        EntryState.from_payload("exp-x", broken_key)


def test_identity_fields_cannot_be_overridden():
    """人写覆盖表能改数字,但不能改这条证据属于哪个实验。"""
    [entry] = recompute_entries([rec("run-003"), rec("run-005")], [],
                                benchmark_version="bench-v1")
    pinned = entry.with_overrides({
        "baseline_model_id": "zengen-v9",
        "benchmark_version": "bench-v9",
        "mean_gain": 9.0,
    })
    assert pinned.baseline_model_id == entry.baseline_model_id
    assert pinned.benchmark_version == entry.benchmark_version
    assert pinned.mean_gain == 9.0          # 非身份字段仍可覆盖


def test_a_mistyped_override_is_kept_but_not_applied_or_fatal():
    """人手写错一个覆盖值,不能把条目变成读不出:读不出的文件不会被重算覆盖,
    等于这条证据被永久钉死在树上。坏值留痕、不生效。"""
    [entry] = recompute_entries([rec("run-003"), rec("run-005")], [],
                                benchmark_version="bench-v1")
    payload = entry.to_payload()
    payload["overrides"] = {"support": "two", "mean_gain": 9.0}

    again = EntryState.from_payload(entry.entry_id, payload)   # 不许抛
    assert again.overrides["support"] == "two"                 # 留给人看

    # 真正应用覆盖表的是重算:坏值必须在这里被挡下,否则 support 变成字符串,
    # 排序处的 -support 会抛 TypeError,整个选取流程连坐。
    records = [rec("run-003"), rec("run-005")]
    [applied] = recompute_entries(records, [again], benchmark_version="bench-v1")
    assert applied.support == 2                                # 坏值不生效
    assert applied.mean_gain == 9.0                            # 好值照常生效


def test_an_entry_whose_evidence_disappeared_goes_stale():
    """记录被删或损坏后,旧条目不能再以 effective 留在树上被选中。"""
    [entry] = recompute_entries([rec("run-003"), rec("run-005")], [],
                                benchmark_version="bench-v1")
    [now] = recompute_entries([], [entry], benchmark_version="bench-v1")
    assert now.status == "stale"
