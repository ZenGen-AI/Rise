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
    # only judge prompt version changes -> not comparable
    b = BenchmarkVersion("tax-v1", "pool-v1", "sv-1", "jv-2", "cv-1")
    assert is_comparable(a, b) is False
