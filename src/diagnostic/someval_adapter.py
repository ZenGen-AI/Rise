from __future__ import annotations

from typing import Protocol

from diagnostic.datamodel import DiagnosticItem, ItemResult
from diagnostic.stats import pass_rate, variance


class SoMEvalRunner(Protocol):
    """Protocol for evaluating items through SoMEval."""

    def evaluate(
        self, items: list[DiagnosticItem], candidate_ref: str, k: int
    ) -> dict[str, list[str]]:
        """
        Evaluate items and return raw outputs.

        Args:
            items: List of diagnostic items to evaluate
            candidate_ref: Reference identifier for the candidate
            k: Number of samples per item

        Returns:
            Dictionary mapping item_id to list of k raw outputs
        """
        ...


class MockSoMEvalRunner:
    """Mock SoMEval runner that returns fixed outputs."""

    def __init__(self, fixed: dict[str, list[str]]):
        """
        Initialize with fixed outputs.

        Args:
            fixed: Dictionary mapping item_id to list of raw outputs
        """
        self.fixed = fixed

    def evaluate(
        self, items: list[DiagnosticItem], candidate_ref: str, k: int
    ) -> dict[str, list[str]]:
        """
        Return fixed outputs for items.

        Args:
            items: List of diagnostic items
            candidate_ref: Reference identifier for the candidate (unused)
            k: Number of samples (unused)

        Returns:
            Dictionary mapping item_id to fixed output list
        """
        return {item.id: self.fixed[item.id] for item in items}


def run_static_items(
    items: list[DiagnosticItem], runner: SoMEvalRunner, candidate_ref: str, k: int
) -> list[ItemResult]:
    """
    Run static items through the runner and convert to ItemResult objects.

    Args:
        items: List of diagnostic items
        runner: SoMEvalRunner instance
        candidate_ref: Reference identifier for the candidate
        k: Number of samples per item

    Returns:
        List of ItemResult objects with scores and stats
    """
    raw = runner.evaluate(items, candidate_ref, k)
    results = []

    for item in items:
        outputs = raw[item.id]
        scores = [_rule_hit(o, item.reference_answer) for o in outputs]
        result = ItemResult(
            item_id=item.id,
            raw_outputs=list(outputs),
            per_sample_scores=scores,
            pass_rate=pass_rate(scores),
            variance=variance(scores),
            track="static",
        )
        results.append(result)

    return results


def _rule_hit(raw: str, reference_answer: str | None) -> float:
    """
    Compute rule-based match score.

    Args:
        raw: Raw output string
        reference_answer: Expected reference answer

    Returns:
        1.0 if exact match, 0.0 otherwise
    """
    return 1.0 if raw == reference_answer else 0.0
