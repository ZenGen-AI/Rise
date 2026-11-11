from __future__ import annotations

from typing import Protocol
from diagnostic.datamodel import DiagnosticItem, ItemResult
from diagnostic.stats import pass_rate, variance


class RuleScorer:
    """Scorer for rule-based (reference answer) scoring mode."""

    def score(self, item: DiagnosticItem, raw: str) -> float:
        """
        Score a response against the item's reference answer.

        Normalizes both the response and reference answer with strip and lower.

        Args:
            item: DiagnosticItem with reference_answer (str)
            raw: Response string to score

        Returns:
            1.0 if normalized response matches reference_answer, 0.0 otherwise
        """
        normalized_raw = raw.strip().lower()
        normalized_ref = item.reference_answer.strip().lower()
        return 1.0 if normalized_raw == normalized_ref else 0.0

    def score_samples(self, item: DiagnosticItem, raws: list[str]) -> ItemResult:
        """
        Score multiple response samples for an item.

        Args:
            item: DiagnosticItem to score for
            raws: List of response strings

        Returns:
            ItemResult with computed pass_rate and variance
        """
        scores = [self.score(item, r) for r in raws]
        return ItemResult(
            item_id=item.id,
            raw_outputs=list(raws),
            per_sample_scores=scores,
            pass_rate=pass_rate(scores),
            variance=variance(scores),
            track=item.track,
        )


class JudgeBackend(Protocol):
    """Protocol for LLM judge backends."""

    model_id: str

    def judge(self, item: DiagnosticItem, response: str, sample_index: int) -> float:
        """
        Judge a response for an item.

        Args:
            item: DiagnosticItem being evaluated
            response: Response string to judge
            sample_index: Index of this sample within the set

        Returns:
            Score (typically 0.0 to 1.0)
        """
        ...


class MockJudgeBackend:
    """Mock judge backend with fixed per-sample scores."""

    def __init__(self, model_id: str, fixed: dict):
        """
        Initialize mock judge backend.

        Args:
            model_id: Identifier for this judge model
            fixed: Dict mapping (item_id, sample_index) -> score (float)
        """
        self.model_id = model_id
        self.fixed = fixed

    def judge(self, item: DiagnosticItem, response: str, sample_index: int) -> float:
        """
        Return fixed score for this (item_id, sample_index) pair.

        Raises KeyError if the pair is not in fixed dict.
        """
        return self.fixed[(item.id, sample_index)]


class SelfEvalBiasError(Exception):
    """Raised when judge model is too close to candidate model (self-eval bias risk)."""


def _is_near_relative(judge_model_id: str, candidate_model_id: str) -> bool:
    """
    Check if judge model is the same as or a close relative of candidate model.

    Self-eval bias (spec §2.5.2) is symmetric: a judge that is a fine-tune of
    the candidate ("cand-model-sft" judging "cand-model") is biased, and so is
    a judge that is the base model of the candidate ("cand-model" judging
    "cand-model-sft"). So the check is bidirectional: either id being a prefix
    of the other (which subsumes the equal case) counts as a near relative.

    Args:
        judge_model_id: Identifier for the judge model
        candidate_model_id: Identifier for the candidate model

    Returns:
        True if judge is too close to candidate (same, ancestor, or descendant)
    """
    return judge_model_id.startswith(candidate_model_id) or candidate_model_id.startswith(
        judge_model_id
    )


class LLMJudgeScorer:
    """Scorer using an LLM judge backend with self-eval bias guard."""

    def __init__(self, judge: JudgeBackend, candidate_model_id: str):
        """
        Initialize LLM judge scorer with bias guard.

        Raises SelfEvalBiasError if judge model is too close to candidate model.

        Args:
            judge: Judge backend implementing JudgeBackend protocol
            candidate_model_id: ID of the candidate model being evaluated

        Raises:
            SelfEvalBiasError: If judge_model_id is same as or prefix-matches candidate_model_id
        """
        if _is_near_relative(judge.model_id, candidate_model_id):
            raise SelfEvalBiasError(
                f"Judge model '{judge.model_id}' is too close to candidate model "
                f"'{candidate_model_id}' (self-eval bias risk)"
            )
        self.judge = judge

    def score_samples(self, item: DiagnosticItem, responses: list[str]) -> ItemResult:
        """
        Score multiple responses using the judge backend.

        Args:
            item: DiagnosticItem to score for
            responses: List of response strings

        Returns:
            ItemResult with per-sample judge scores and computed statistics
        """
        scores = [
            self.judge.judge(item, resp, idx) for idx, resp in enumerate(responses)
        ]
        return ItemResult(
            item_id=item.id,
            raw_outputs=list(responses),
            per_sample_scores=scores,
            pass_rate=pass_rate(scores),
            variance=variance(scores),
            track=item.track,
        )
