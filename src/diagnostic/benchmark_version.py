from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BenchmarkVersion:
    """Version record for benchmark components that affect comparability."""

    taxonomy_version: str
    item_pool_version: str
    someval_version: str
    judge_prompt_version: str
    scoring_config_version: str

    def to_dict(self) -> dict:
        """Return all fields as a dict keyed by name."""
        return {
            "taxonomy_version": self.taxonomy_version,
            "item_pool_version": self.item_pool_version,
            "someval_version": self.someval_version,
            "judge_prompt_version": self.judge_prompt_version,
            "scoring_config_version": self.scoring_config_version,
        }

    @classmethod
    def from_dict(cls, d: dict) -> BenchmarkVersion:
        """Create a BenchmarkVersion from a dict keyed by field name."""
        return cls(
            taxonomy_version=d["taxonomy_version"],
            item_pool_version=d["item_pool_version"],
            someval_version=d["someval_version"],
            judge_prompt_version=d["judge_prompt_version"],
            scoring_config_version=d["scoring_config_version"],
        )

    def version_id(self) -> str:
        """Return a deterministic composite version ID.

        Any component change breaks comparability — cross-version scores
        are not directly comparable (§2.5.8 hard constraint).
        """
        return f"{self.taxonomy_version}|{self.item_pool_version}|{self.someval_version}|{self.judge_prompt_version}|{self.scoring_config_version}"


def is_comparable(a: BenchmarkVersion, b: BenchmarkVersion) -> bool:
    """Check if two BenchmarkVersions are comparable.

    Returns True iff all components match (version_id() is identical).
    Any component change breaks comparability (§2.5.8).
    """
    return a.version_id() == b.version_id()
