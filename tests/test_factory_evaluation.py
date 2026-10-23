"""评估器装配:mock 后端的真实诊断评估器,报告标 backends: mock(R17)。"""
import pytest

from controlplane.config import write_config
from controlplane.evidence import diagnostic_grade
from controlplane.gitstore import GitStore
from controlplane.paths import (
    diagnostic_report_file, item_pool_file, persona_file, scenario_file, taxonomy_file,
)
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


def _hand_edit(store, rel, mutate):
    """像人那样改一份已登记的资产:改载荷,经 write_product 提交。"""
    env = store.read_product(rel)
    mutate(env.payload)
    store.write_product(rel, env, f"human: edit {rel}")


ASSET_EDITS = [
    pytest.param(lambda: taxonomy_file("factory-tax-v1"),
                 lambda p: p["l1_dimensions"].append("extra_dimension"), id="taxonomy"),
    pytest.param(lambda: item_pool_file("factory-pool-v1"),
                 lambda p: p["items"][0].update(reference_answer="C"), id="item-pool"),
    pytest.param(lambda: scenario_file("factory-deploy-s1"),
                 lambda p: p.update(background="a different background"), id="scenario"),
    pytest.param(lambda: persona_file("factory-user-persona"),
                 lambda p: p.update(motivation="a different motivation"), id="persona"),
]


@pytest.mark.parametrize("rel_of, mutate", ASSET_EDITS)
def test_install_defaults_refuses_to_overwrite_a_hand_edited_asset(store, rel_of, mutate):
    """同一版本号必须对应同一份内容(题库与分类表的版本进诊断报告,分数要可比):重跑不能
    静默还原人改过的资产,要拒绝并指出哪个文件,让人去升版本号。"""
    install_defaults(store)
    rel = rel_of()
    _hand_edit(store, rel, mutate)
    edited = (store.repo_root / rel).read_bytes()
    with pytest.raises(FactoryConfigError, match=rel.rsplit("/", 1)[-1]):
        install_defaults(store)
    assert (store.repo_root / rel).read_bytes() == edited


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
