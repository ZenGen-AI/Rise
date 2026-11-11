from diagnostic.stats import (
    wilson_interval, pass_rate, variance, aggregate_pass_rates,
    difference_is_significant,
)


def test_wilson_interval_known_value():
    # 8/10 pass, 95% Wilson interval approx [0.49, 0.94]
    low, high = wilson_interval(8, 10)
    assert 0.48 < low < 0.50
    assert 0.93 < high < 0.95
    assert low < high


def test_wilson_interval_zero_samples_returns_full_range():
    assert wilson_interval(0, 0) == (0.0, 1.0)


def test_wilson_interval_all_pass_upper_bounded_below_one():
    low, high = wilson_interval(10, 10)
    assert high <= 1.0
    assert low < 1.0          # Wilson lower bound strictly below 1 (finite-sample uncertainty)


def test_wilson_interval_is_deterministic():
    assert wilson_interval(7, 20) == wilson_interval(7, 20)


def test_pass_rate_and_variance():
    scores = [1.0, 1.0, 0.0, 1.0]
    assert pass_rate(scores) == 0.75
    assert abs(variance(scores) - 0.1875) < 1e-9


def test_pass_rate_empty_is_zero():
    assert pass_rate([]) == 0.0


def test_aggregate_pass_rates_pools_samples():
    # pool sample counts, not average of rates (sample-size weighted)
    agg = aggregate_pass_rates([(0.75, 4), (0.5, 2)])
    # (3 + 1) / (4 + 2) = 0.6667
    assert abs(agg[0] - 4/6) < 1e-9
    assert agg[1] == 6       # total sample count


def test_difference_is_significant_within_ci_is_not():
    # candidate [0.70,0.85] vs baseline [0.72,0.88] intervals overlap -> not significant
    assert difference_is_significant((0.70, 0.85), (0.72, 0.88)) is False


def test_difference_is_significant_disjoint_ci_is():
    # candidate [0.86,0.94] vs baseline [0.60,0.75] disjoint -> significant
    assert difference_is_significant((0.86, 0.94), (0.60, 0.75)) is True
