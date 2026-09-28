from __future__ import annotations

import math


def wilson_interval(passes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """
    Compute the Wilson score confidence interval for a proportion.

    Args:
        passes: Number of successes
        n: Total sample size
        z: Z-score for confidence level (default 1.96 for 95%)

    Returns:
        Tuple of (lower_bound, upper_bound)
    """
    if n == 0:
        return (0.0, 1.0)

    phat = passes / n
    denom = 1 + z**2 / n
    center = (phat + z**2 / (2 * n)) / denom
    half = (z * math.sqrt(phat * (1 - phat) / n + z**2 / (4 * n**2))) / denom
    low = max(0.0, center - half)
    high = min(1.0, center + half)
    return (low, high)


def pass_rate(scores: list[float]) -> float:
    """
    Compute the average pass rate from a list of scores.

    Args:
        scores: List of scores (typically 0.0 or 1.0)

    Returns:
        Average score, or 0.0 if empty
    """
    if not scores:
        return 0.0
    return sum(scores) / len(scores)


def variance(scores: list[float]) -> float:
    """
    Compute population variance of scores.

    Args:
        scores: List of scores

    Returns:
        Population variance, or 0.0 if empty
    """
    if not scores:
        return 0.0
    mu = pass_rate(scores)
    return sum((x - mu) ** 2 for x in scores) / len(scores)


def aggregate_pass_rates(rate_and_n: list[tuple[float, int]]) -> tuple[float, int]:
    """
    Aggregate pass rates from multiple samples by pooling counts.

    Args:
        rate_and_n: List of (pass_rate, sample_count) tuples

    Returns:
        Tuple of (aggregated_pass_rate, total_sample_count)
    """
    total_passes = 0
    total_n = 0

    for rate, n in rate_and_n:
        passes = round(rate * n)
        total_passes += passes
        total_n += n

    if total_n == 0:
        return (0.0, 0)

    return (total_passes / total_n, total_n)


def difference_is_significant(
    ci_a: tuple[float, float], ci_b: tuple[float, float]
) -> bool:
    """
    Check if two confidence intervals are disjoint (non-overlapping).

    Args:
        ci_a: Confidence interval (low, high) for candidate
        ci_b: Confidence interval (low, high) for baseline

    Returns:
        True if intervals do not overlap (difference is significant)
    """
    return ci_a[0] > ci_b[1] or ci_b[0] > ci_a[1]
