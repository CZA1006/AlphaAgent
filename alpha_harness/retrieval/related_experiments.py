"""Related experiment retrieval — structured, deterministic, registry-agnostic.

Given a candidate factor and/or a set of tags, rank prior experiments by a
transparent weighted combination of:

    1. canonical AST similarity (primary signal)
    2. tag overlap (Jaccard on hypothesis + experiment tags)
    3. recency (exponential decay on age-in-days)

The retriever is decoupled from any specific registry — it consumes anything
that satisfies :class:`ExperimentSource`, which both
:class:`~alpha_harness.registries.experiment.ExperimentRegistry` and
:class:`~alpha_harness.registries.sql_experiment.SqlExperimentRegistry`
already satisfy via their ``list_all()`` methods.

Results are returned as small typed :class:`RelatedExperiment` summaries —
intended to be handed to a proposer/refiner without leaking full
``ExperimentRecord`` internals.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from alpha_harness.factors.canonical import (
    CanonNode,
    canon_similarity,
    canonicalize,
)
from alpha_harness.factors.dsl_parser import DslParseError, parse_expression
from alpha_harness.schemas.experiment import (
    ExperimentDecision,
    ExperimentRecord,
)
from alpha_harness.schemas.factor import FactorSpec
from alpha_harness.schemas.hypothesis import AssetClass

# ── Registry protocol ───────────────────────────────────────────────────────


class ExperimentSource(Protocol):
    """Minimal structural interface the retriever needs from a registry."""

    def list_all(self) -> list[ExperimentRecord]:
        ...


# ── Typed result ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RelatedExperiment:
    """Concise, self-describing summary of a single related experiment.

    Carries enough provenance for a proposer/refiner to reason about the
    experiment without needing to reload the full ``ExperimentRecord``.

    Sub-score fields (``ast_similarity``, ``tag_overlap``, ``recency``) are
    exposed so callers can inspect *why* the ranking came out the way it did.
    """

    experiment_id: str
    factor_name: str
    expression: str
    decision: ExperimentDecision
    tags: tuple[str, ...]
    asset_class: AssetClass
    created_at: datetime

    # Scoring breakdown — all in [0.0, 1.0]
    score: float
    ast_similarity: float
    tag_overlap: float
    recency: float

    # Compact metric snapshot for downstream prompts / heuristics
    ic: float | None
    rank_ic: float | None
    sharpe: float | None

    # Failure summary (only populated for REJECTED experiments)
    failure_category: str | None
    notes: str


# ── Scoring configuration ────────────────────────────────────────────────────


@dataclass(frozen=True)
class ScoreWeights:
    """Linear-combination weights for the three retrieval signals.

    Weights are applied as-is (not normalized) so callers can intentionally
    run an AST-only search by setting the others to zero.  The public
    :meth:`RelatedExperiment.score` already contains the combined total.
    """

    ast_similarity: float = 0.6
    tag_overlap: float = 0.3
    recency: float = 0.1


@dataclass(frozen=True)
class RelatedQuery:
    """All the inputs that shape a retrieval call.

    At least one of ``factor`` or ``tags`` should be non-empty for the
    scoring to be meaningful; otherwise every candidate scores purely on
    recency and the top-N becomes "most recent".

    Filters (``asset_class`` / ``decisions``) are applied *before* scoring —
    they shrink the candidate pool rather than influence the score.
    """

    factor: FactorSpec | None = None
    tags: tuple[str, ...] = ()
    asset_class: AssetClass | None = None
    decisions: tuple[ExperimentDecision, ...] | None = None
    top_n: int = 5
    recency_half_life_days: float = 30.0
    min_score: float = 0.0
    weights: ScoreWeights = field(default_factory=ScoreWeights)


# ── Retriever ────────────────────────────────────────────────────────────────


class RelatedExperimentRetriever:
    """Rank prior experiments by relevance to a query.

    The retriever is stateless beyond its ``source`` reference, so a single
    instance can serve arbitrary queries.  Scoring is deterministic given a
    fixed ``now`` clock (pass ``now`` to :meth:`search` in tests to pin it).
    """

    def __init__(self, source: ExperimentSource) -> None:
        self._source = source

    # ── Public API ───────────────────────────────────────────────────────

    def search(
        self,
        query: RelatedQuery,
        *,
        now: datetime | None = None,
    ) -> list[RelatedExperiment]:
        """Return up to ``query.top_n`` related experiments, sorted best-first."""
        clock = now or datetime.now(UTC)
        candidate_canon = _canonicalize_query_factor(query.factor)
        tag_query = _normalize_tags(query.tags)

        scored: list[RelatedExperiment] = []
        for record in self._source.list_all():
            if not _passes_filters(record, query):
                continue
            summary = self._score_record(
                record=record,
                candidate_canon=candidate_canon,
                tag_query=tag_query,
                weights=query.weights,
                half_life_days=query.recency_half_life_days,
                now=clock,
            )
            if summary.score < query.min_score:
                continue
            scored.append(summary)

        scored.sort(key=_ranking_key, reverse=True)
        return scored[: query.top_n]

    # ── Internals ────────────────────────────────────────────────────────

    def _score_record(
        self,
        *,
        record: ExperimentRecord,
        candidate_canon: CanonNode | None,
        tag_query: frozenset[str],
        weights: ScoreWeights,
        half_life_days: float,
        now: datetime,
    ) -> RelatedExperiment:
        ast_sim = _ast_similarity_score(candidate_canon, record.factor)
        tag_sim = _tag_jaccard(tag_query, _record_tags(record))
        recency = _recency_score(record.created_at, now, half_life_days)

        total = (
            weights.ast_similarity * ast_sim
            + weights.tag_overlap * tag_sim
            + weights.recency * recency
        )

        ev = record.evaluation
        return RelatedExperiment(
            experiment_id=record.id,
            factor_name=record.factor.name,
            expression=record.factor.expression,
            decision=record.decision,
            tags=tuple(record.tags),
            asset_class=record.hypothesis.asset_class,
            created_at=record.created_at,
            score=round(total, 6),
            ast_similarity=round(ast_sim, 6),
            tag_overlap=round(tag_sim, 6),
            recency=round(recency, 6),
            ic=ev.ic,
            rank_ic=ev.rank_ic,
            sharpe=ev.sharpe,
            failure_category=(
                record.failure.category.value if record.failure else None
            ),
            notes=record.notes,
        )


# ── Filtering ────────────────────────────────────────────────────────────────


def _passes_filters(record: ExperimentRecord, query: RelatedQuery) -> bool:
    if query.decisions is not None and record.decision not in query.decisions:
        return False
    return not (
        query.asset_class is not None
        and record.hypothesis.asset_class != query.asset_class
    )


# ── Scoring primitives ───────────────────────────────────────────────────────


def _canonicalize_query_factor(factor: FactorSpec | None) -> CanonNode | None:
    """Best-effort canonicalization of the query factor."""
    if factor is None:
        return None
    if factor.operator_tree is not None:
        try:
            return canonicalize(factor.operator_tree)
        except (ValueError, KeyError, TypeError):
            pass
    try:
        return canonicalize(parse_expression(factor.expression))
    except (DslParseError, ValueError, KeyError, TypeError):
        return None


def _ast_similarity_score(
    candidate_canon: CanonNode | None, other: FactorSpec
) -> float:
    """Similarity between the query factor and a candidate's factor."""
    if candidate_canon is None:
        return 0.0
    other_canon: CanonNode | None = None
    if other.operator_tree is not None:
        try:
            other_canon = canonicalize(other.operator_tree)
        except (ValueError, KeyError, TypeError):
            other_canon = None
    if other_canon is None:
        try:
            other_canon = canonicalize(parse_expression(other.expression))
        except (DslParseError, ValueError, KeyError, TypeError):
            return 0.0
    return canon_similarity(candidate_canon, other_canon)


def _tag_jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """Jaccard index on two tag sets, with empty-set convention → 0.0."""
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    union = len(a | b)
    return intersection / union if union else 0.0


def _recency_score(
    created_at: datetime, now: datetime, half_life_days: float
) -> float:
    """Exponential decay: score halves every ``half_life_days`` of age."""
    if half_life_days <= 0:
        return 0.0
    age_seconds = max(0.0, (now - created_at).total_seconds())
    age_days = age_seconds / 86_400.0
    return math.pow(0.5, age_days / half_life_days)


def _record_tags(record: ExperimentRecord) -> frozenset[str]:
    """Union of hypothesis tags and experiment-level tags (normalized)."""
    return _normalize_tags(tuple(record.hypothesis.tags) + tuple(record.tags))


def _normalize_tags(tags: tuple[str, ...]) -> frozenset[str]:
    return frozenset(t.strip().lower() for t in tags if t and t.strip())


def _ranking_key(summary: RelatedExperiment) -> tuple[float, float, float, float]:
    """Tie-breaking: score → ast_similarity → tag_overlap → recency."""
    return (
        summary.score,
        summary.ast_similarity,
        summary.tag_overlap,
        summary.recency,
    )
