"""Error classification for diagnostic variant families and dynamic consistency.

Implements the pass pattern analysis for variant-family classification (§2.5.3)
and dynamic consistency detection for utterance sequences.
"""

from diagnostic.datamodel import ItemResult, VariantFamily, ErrorProfile


def classify_static_family(
    family: VariantFamily,
    results: dict[str, ItemResult],
    *,
    pass_threshold: float = 0.5,
    var_threshold: float = 0.2,
    format_validator=None,
    refusal_detector=None,
) -> str:
    """Classify error pattern for a variant family based on pass rates and variance.

    Decision order (fixed priority):
    1. refusal: if refusal_detector(o) is True for any output o in base item → "refusal"
    2. format_fail: if format_validator(o) is True (malformed) for any output o → "format_fail"
    3. unstable: if base item variance > var_threshold → "unstable"
    4. surface_match: if base pass_rate >= pass_threshold AND at least one variant
       has pass_rate < pass_threshold → "surface_match"
    5. reasoning_break: if base pass_rate < pass_threshold → "reasoning_break"
    6. otherwise (base passes, no failing variants, stable) → "" (empty string)
    """
    base_result = results[family.base_item_id]

    # 1. Check for refusal first
    if refusal_detector is not None:
        for output in base_result.raw_outputs:
            if refusal_detector(output):
                return "refusal"

    # 2. Check for format_fail (validator returns True means malformed)
    if format_validator is not None:
        for output in base_result.raw_outputs:
            if format_validator(output):
                return "format_fail"

    # 3. Check for unstable (high variance)
    if base_result.variance > var_threshold:
        return "unstable"

    # 4. Check for surface_match (base passes, at least one variant fails)
    if base_result.pass_rate >= pass_threshold:
        for variant_id in family.variant_item_ids:
            if variant_id in results:
                variant_result = results[variant_id]
                if variant_result.pass_rate < pass_threshold:
                    return "surface_match"

    # 5. Check for reasoning_break (base fails)
    if base_result.pass_rate < pass_threshold:
        return "reasoning_break"

    # 6. Default: no error classified
    return ""


def classify_dynamic_consistency(utterances: list[str], stance_fn) -> str | None:
    """Classify dynamic consistency of a sequence of utterances.

    Maps each utterance through stance_fn (returns boolean). If the stance
    is not constant throughout the sequence (contains both True and False),
    return "inconsistency". If constant, return None.
    """
    stances = [stance_fn(u) for u in utterances]

    # Check if stances contain both True and False
    has_true = True in stances
    has_false = False in stances

    if has_true and has_false:
        return "inconsistency"

    return None


def aggregate_error_profile(
    l2_dimension: str,
    error_labels: list[str],
    track: str,
) -> ErrorProfile:
    """Aggregate error labels into a normalized error type distribution.

    Counts frequency of each label and normalizes to sum to 1.0.
    Returns an ErrorProfile with the distribution.
    """
    # Count occurrences of each label
    label_counts = {}
    for label in error_labels:
        label_counts[label] = label_counts.get(label, 0) + 1

    # Normalize to distribution
    total = sum(label_counts.values())
    error_type_distribution = {
        label: count / total for label, count in label_counts.items()
    }

    return ErrorProfile(
        l2_dimension=l2_dimension,
        error_type_distribution=error_type_distribution,
        track=track,
    )
