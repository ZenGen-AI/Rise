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
from ts_fixtures import (
    env,
    make_store,
    write_decision,
    write_directive,
    write_report,
    write_round,
    write_trace,
)

BENCH = "bench-v1"
BASE = "zengen-v1"


def _baseline(store):
    """基线轮必须在 real 级:compute_gain 拒绝"real 候选对非 real 基线"的跨级比较(R17),
    基线若是默认的 mock,增益恒为空,条目一条也写不出来。"""
    write_round(store, "run-001", {"tom": 0.40, "empathy": 0.70}, model_n=1, backends="real")


def _previous_run(run_id: str) -> str:
    """产出这一轮的那次规划的来源 run:run-005 -> run-004。测试里编号都是整数。"""
    number = int(run_id.split("-")[1])
    return f"run-{number - 1:03d}"


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
        _link_directive(store, run_id, dimension=dimension)
        write_decision(store, _previous_run(run_id), run_id, action="clone", dimension=dimension,
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


def test_writes_outside_the_artifact_tree_are_refused(tmp_path):
    """写入点自己把关:调用方传了越界路径也不写。"""
    from trainscheduler.experience_port import _write

    store = make_store(tmp_path)
    for bad in ("/etc/passwd", "control-plane/../../escape.yaml",
                "control-plane/experience/../x.yaml", "elsewhere/x.yaml"):
        with pytest.raises(ValueError, match="outside the artifact tree"):
            _write(store, bad, id_="mine", type_="t", links=[], payload={}, message="m")


def test_a_record_with_a_markerless_perturbation_value_is_skipped(tmp_path):
    """扰动值没有 bool/num/str 标记时整条记录跳过,不许在聚合处 KeyError。"""
    store = make_store(tmp_path)
    _baseline(store)
    _round(store, "run-003")
    cfg = SchedulerConfig(strategy_experience=True)
    record_and_recompute(store, "run-003", config=cfg)
    rel = strategy_experience_note_file("run-003")
    envelope = store.read_product(rel)
    payload = dict(envelope.payload)
    key = dict(payload["strategy_key"])
    key["perturbation"] = {name: {} for name in key["perturbation"]}
    payload["strategy_key"] = key
    store.write_product(rel, env(envelope.id, envelope.type, envelope.status, payload, [envelope.id]),
                        "test: markerless perturbation value")
    summary = select_entries(store, dimension="tom", benchmark_version=BENCH, base_model_id=BASE)
    assert summary.ignored_records
    assert summary.entries == ()


def test_record_is_usable_accepts_only_well_formed_values(tmp_path):
    from trainscheduler.experience_port import _record_is_usable

    good = {
        "run_id": "run-003",
        "strategy_key": {"dimension": "tom", "action": "clone", "mode": "batch",
                         "perturbation": {"opd_coefficient": {"num": "0.2"}},
                         "source_recipe_id": "recipe-run-002"},
        "outcome": {"gain": 0.05, "promoted": True, "regressions": [], "cost": {"calls": 1}},
        "evidence": {"grade": "real", "baseline_ref": BASE, "baseline_model_id": BASE,
                     "benchmark_version": BENCH},
        "attribution": "medium",
        "ignored": [],
    }
    assert _record_is_usable(good)

    for marks in ({"bool": True}, {"num": "4"}, {"str": "x"}):
        variant = dict(good)
        variant["strategy_key"] = dict(good["strategy_key"], perturbation={"k": marks})
        assert _record_is_usable(variant)

    for bad_marks in ({}, {"bool": True, "num": "4"}):
        variant = dict(good)
        variant["strategy_key"] = dict(good["strategy_key"], perturbation={"k": bad_marks})
        assert not _record_is_usable(variant)


def test_a_directive_without_a_decision_ref_is_not_recorded(tmp_path):
    """人写的指令没有 planner_decision_ref:没有策略可归因,如实不落账。"""
    store = make_store(tmp_path)
    _baseline(store)
    write_round(store, "run-003", {"tom": 0.45, "empathy": 0.70}, backends="real")
    record_and_recompute(store, "run-003", config=SchedulerConfig(strategy_experience=True))
    assert not store.exists(strategy_experience_note_file("run-003"))


def test_empty_store_renders_nothing(tmp_path):
    store = make_store(tmp_path)
    summary = select_entries(store, dimension="tom", benchmark_version=BENCH, base_model_id=BASE)
    assert summary == StrategyExperienceSummary((), (), 0, ())
