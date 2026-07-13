"""Persistence scoring — order factors by sub-window stability, not mean IC.

The HK IPO microstructure case study exposed the selection failure mode:
gates threshold on train-window *mean* metrics, and downstream selection
(``combine_factors`` filters, proposer digests) orders by train IC — so
the promoted basket was built from the hottest-mean factors, one of
which flipped sign out-of-sample, while several factors whose rank-IC
was *persistently* positive across sub-windows were left out.  On a
short, noisy panel the mean is dominated by a few hot stretches; the
fraction of sub-windows that agree on sign is the better survival
signal (10/12 train→test sign persistence vs a failed top-train-IC
basket in the case study).

:class:`PersistenceScore` reduces per-fold rank-ICs to a sortable key:

1. ``fraction_positive`` — share of folds with positive rank-IC;
2. ``stability`` — t-like ``mean / (std / sqrt(n))``;
3. ``mean_rank_ic`` — tie-break only.

Build one with :func:`score_from_folds` (raw per-fold values) or
:func:`score_from_walk_forward` (an ``EvaluationBundle``'s metadata —
the :class:`~alpha_harness.evaluators.walk_forward.WalkForwardEvaluator`
already records the inputs).  :func:`rank_by_persistence` orders any
scored collection, unscored items last.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class PersistenceScore:
    """Sub-window rank-IC summary with a persistence-first sort key."""

    n_folds: int
    fraction_positive: float
    mean_rank_ic: float
    std_rank_ic: float

    @property
    def stability(self) -> float:
        """t-like ``mean / standard error``; signed infinity when std is 0."""
        if self.std_rank_ic > 0:
            return self.mean_rank_ic / (self.std_rank_ic / math.sqrt(self.n_folds))
        if self.mean_rank_ic > 0:
            return math.inf
        if self.mean_rank_ic < 0:
            return -math.inf
        return 0.0

    @property
    def sort_key(self) -> tuple[float, float, float]:
        """Descending sort ranks the most persistent factor first."""
        return (self.fraction_positive, self.stability, self.mean_rank_ic)


def score_from_folds(rank_ics: Sequence[float | None]) -> PersistenceScore | None:
    """Score a factor from its per-fold rank-ICs; None when nothing usable."""
    vals = [float(v) for v in rank_ics if v is not None and not math.isnan(v)]
    if not vals:
        return None
    n = len(vals)
    mean = sum(vals) / n
    var = sum((v - mean) ** 2 for v in vals) / n
    return PersistenceScore(
        n_folds=n,
        fraction_positive=sum(1 for v in vals if v > 0) / n,
        mean_rank_ic=mean,
        std_rank_ic=math.sqrt(var),
    )


def score_from_walk_forward(metadata: Mapping[str, object]) -> PersistenceScore | None:
    """Score from an ``EvaluationBundle.metadata`` produced by walk-forward.

    Prefers the raw ``per_fold`` payload; falls back to the
    ``walk_forward`` summary block.  Returns None for single-fold or
    legacy bundles — callers should treat those as unranked, not as
    zero-persistence.
    """
    per_fold = metadata.get("per_fold")
    if isinstance(per_fold, list) and len(per_fold) >= 2:
        rank_ics = [
            row.get("rank_ic")
            for row in per_fold
            if isinstance(row, dict) and isinstance(row.get("rank_ic"), int | float)
        ]
        if len(rank_ics) >= 2:
            return score_from_folds(rank_ics)

    wf = metadata.get("walk_forward")
    if not isinstance(wf, Mapping):
        return None
    n_folds = wf.get("n_folds")
    frac = wf.get("fraction_positive_rank_ic")
    mean = wf.get("mean_rank_ic")
    std = wf.get("std_rank_ic")
    if (
        not isinstance(n_folds, int)
        or n_folds < 2
        or not isinstance(frac, int | float)
        or not isinstance(mean, int | float)
        or not isinstance(std, int | float)
    ):
        return None
    return PersistenceScore(
        n_folds=n_folds,
        fraction_positive=float(frac),
        mean_rank_ic=float(mean),
        std_rank_ic=float(std),
    )


def rank_by_persistence(
    scored: Iterable[tuple[T, PersistenceScore | None]],
) -> list[T]:
    """Order items most-persistent first; unscored items keep order, at the end."""
    items = list(scored)
    with_score = [(item, s) for item, s in items if s is not None]
    without = [item for item, s in items if s is None]
    with_score.sort(key=lambda pair: pair[1].sort_key, reverse=True)
    return [item for item, _ in with_score] + without
