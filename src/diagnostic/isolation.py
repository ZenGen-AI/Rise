"""
Eval-set isolation validator — text + structural contamination filter (§2.5.7).

Hard constraint: training contexts must pass a similarity check against the diagnostic
question pool before ingestion (text similarity + key structure matching). Contaminated
samples are rejected and logged.

V1 implementation uses stdlib token-Jaccard approximation for text similarity. The
backend can later be swapped for an embedding model without changing the caller contract.
"""


def token_similarity(a: str, b: str) -> float:
    """
    Compute Jaccard similarity between two strings using token-level split.

    Args:
        a: First string.
        b: Second string.

    Returns:
        Jaccard similarity (|intersection| / |union|) in [0, 1].
        Returns 0.0 if both strings are empty (union is empty).
    """
    tokens_a = set(a.lower().split())
    tokens_b = set(b.lower().split())

    union = tokens_a | tokens_b
    if not union:
        return 0.0

    intersection = tokens_a & tokens_b
    return len(intersection) / len(union)


def structural_overlap(a: dict, b: dict) -> float:
    """
    Compute structural overlap as the fraction of shared keys whose values match.

    Args:
        a: First dict.
        b: Second dict.

    Returns:
        Fraction of shared keys with equal values in [0, 1].
        Returns 0.0 if there are no shared keys.
    """
    shared_keys = set(a.keys()) & set(b.keys())
    if not shared_keys:
        return 0.0

    matching_count = sum(1 for k in shared_keys if a[k] == b[k])
    return matching_count / len(shared_keys)


def is_contaminated(
    candidate: dict,
    pool: list[dict],
    *,
    text_threshold: float,
    struct_threshold: float,
) -> bool:
    """
    Check if a candidate is contaminated (too similar to any pool item).

    A candidate is contaminated if ANY pool item matches BOTH thresholds:
    - token_similarity(candidate["text"], item["text"]) >= text_threshold
    - structural_overlap(candidate["structure"], item["structure"]) >= struct_threshold

    Args:
        candidate: Candidate dict with "text" and "structure" keys.
        pool: List of pool dicts, each with "text" and "structure" keys.
        text_threshold: Minimum text similarity to flag contamination.
        struct_threshold: Minimum structural overlap to flag contamination.

    Returns:
        True if contaminated (matches pool item), False otherwise.
    """
    for item in pool:
        text_sim = token_similarity(candidate["text"], item["text"])
        struct_sim = structural_overlap(candidate["structure"], item["structure"])

        if text_sim >= text_threshold and struct_sim >= struct_threshold:
            return True

    return False


def filter_contaminated(
    candidates: list[dict],
    pool: list[dict],
    *,
    text_threshold: float,
    struct_threshold: float,
) -> tuple[list[dict], list[dict]]:
    """
    Filter candidates by contamination check; return clean and rejected lists.

    Hard constraint (§2.5.7): rejected items must carry a "reason" field recording
    why they were rejected (matching pool item id and/or similarity scores).

    Args:
        candidates: List of candidate dicts, each with "id", "text", "structure".
        pool: List of pool dicts, each with "text", "structure" (and optionally "id").
        text_threshold: Minimum text similarity to flag contamination.
        struct_threshold: Minimum structural overlap to flag contamination.

    Returns:
        (kept, rejected) tuple:
        - kept: list of clean candidates (unchanged, order preserved)
        - rejected: list of rejected dicts with original fields + "reason" key
    """
    kept = []
    rejected = []

    for candidate in candidates:
        contaminated = False
        reason = None

        for item in pool:
            text_sim = token_similarity(candidate["text"], item["text"])
            struct_sim = structural_overlap(candidate["structure"], item["structure"])

            if text_sim >= text_threshold and struct_sim >= struct_threshold:
                contaminated = True
                item_id = item.get("id", "unknown")
                reason = f"matched pool item {item_id} (text_sim={text_sim:.3f}, struct_sim={struct_sim:.3f})"
                break

        if contaminated:
            rejected_entry = dict(candidate)
            rejected_entry["reason"] = reason
            rejected.append(rejected_entry)
        else:
            kept.append(candidate)

    return kept, rejected
