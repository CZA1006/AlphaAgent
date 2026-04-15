"""Novelty evaluator — checks whether a factor is sufficiently distinct.

Guards against re-testing rejected ideas or promoting near-identical factors.
Called by PromotionJudge during the judge phase; returns a NoveltyVerdict
(not an EvaluationBundle). Currently uses exact string match; real
expression-tree and correlation-based comparison is planned for Round 2.
"""

from __future__ import annotations

from dataclasses import dataclass

from alpha_harness.schemas.factor import FactorSpec


@dataclass(frozen=True)
class NoveltyVerdict:
    """Result of a novelty check."""

    is_novel: bool
    similarity_score: float  # 0.0 = completely unique, 1.0 = exact duplicate
    most_similar_factor_id: str | None  # id of the closest existing factor
    detail: str  # human-readable explanation


class NoveltyEvaluator:
    """Check whether a factor is sufficiently distinct from known factors.

    Currently uses exact string match on expressions. Replace
    ``_compute_similarity`` with AST or return-correlation comparison
    once the factor DSL is available.
    """

    def __init__(
        self,
        existing_expressions: list[tuple[str, str]] | None = None,
        similarity_threshold: float = 0.85,
    ) -> None:
        """Initialize with known factor expressions.

        Parameters
        ----------
        existing_expressions:
            List of (factor_id, expression) tuples for already-evaluated factors.
            In production, populated from the ExperimentRegistry.
        similarity_threshold:
            Factors with similarity above this are considered duplicates.
        """
        self._existing = existing_expressions or []
        self._threshold = similarity_threshold

    def check_novelty(self, factor: FactorSpec) -> NoveltyVerdict:
        """Evaluate whether the factor is sufficiently novel.

        Parameters
        ----------
        factor:
            The compiled factor to check against known factors.

        Returns
        -------
        NoveltyVerdict with is_novel flag, similarity score, and detail.
        """
        if not self._existing:
            return NoveltyVerdict(
                is_novel=True,
                similarity_score=0.0,
                most_similar_factor_id=None,
                detail="No existing factors to compare against.",
            )

        best_score = 0.0
        best_id: str | None = None

        for factor_id, expression in self._existing:
            score = self._compute_similarity(factor.expression, expression)
            if score > best_score:
                best_score = score
                best_id = factor_id

        is_novel = best_score < self._threshold
        return NoveltyVerdict(
            is_novel=is_novel,
            similarity_score=round(best_score, 4),
            most_similar_factor_id=best_id,
            detail=(
                f"Most similar factor: {best_id} (score={best_score:.4f}). "
                f"{'Novel — below' if is_novel else 'Duplicate — above'} "
                f"threshold {self._threshold}."
            ),
        )

    def _compute_similarity(self, expr_a: str, expr_b: str) -> float:
        """Compute similarity between two factor expressions.

        Round 1: exact string match (0.0 or 1.0).
        Round 2+: Jaccard over tokenised operator trees, or return-correlation.
        """
        if expr_a.strip() == expr_b.strip():
            return 1.0
        return 0.0
