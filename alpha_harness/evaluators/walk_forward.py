"""Walk-forward wrapper around a :class:`FactorEvaluator`.

A single-window IC over Q3 2024 is overfitting bait — the same factor
might collapse in Q4.  :class:`WalkForwardEvaluator` splits the eval
window into rolling folds, runs the inner evaluator on each, and
returns an :class:`~alpha_harness.schemas.evaluation.EvaluationBundle`
whose primary fields hold the per-fold *means* while ``metadata.walk_forward``
carries the full per-fold detail.

The aggregate bundle stays backwards-compatible:

* ``ic``, ``rank_ic``, ``quantile_spread`` etc. are the per-fold means,
  so any existing judge or report can keep treating the bundle as a
  single scalar set.
* ``metadata["walk_forward"]`` exposes ``n_folds``, ``per_fold_ic``,
  ``fraction_positive_rank_ic``, …  Downstream callers that *do* know
  about walk-forward (notably the promotion judge in 4B) can promote
  on those richer signals.

Folds are constructed by date (not by row count) so different fold
sizes apply uniformly across the universe — every symbol in the panel
is evaluated on the same calendar slice.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from datetime import date, timedelta

from alpha_harness.schemas.evaluation import EvaluationBundle, EvaluationRequest
from alpha_harness.schemas.factor import FactorSpec
from alpha_harness.service import FactorEvaluator

logger = logging.getLogger(__name__)


# ── Configuration ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class WalkForwardConfig:
    """Sizing knobs for fold construction.

    ``fold_size_days`` is the *per-fold* eval-window length (calendar days).
    ``step_days`` is how far the start advances between consecutive folds —
    smaller than ``fold_size_days`` produces overlapping folds, equal
    produces disjoint folds, larger leaves gaps.  ``n_folds`` is the hard
    cap; the actual fold count may be lower when the requested span runs
    past ``request.eval_end``.
    """

    n_folds: int = 4
    fold_size_days: int = 60
    step_days: int = 20

    def __post_init__(self) -> None:
        if self.n_folds < 1:
            raise ValueError("n_folds must be >= 1")
        if self.fold_size_days < 1:
            raise ValueError("fold_size_days must be >= 1")
        if self.step_days < 1:
            raise ValueError("step_days must be >= 1")


# ── Public API ──────────────────────────────────────────────────────────────


def fold_windows(
    eval_start: date,
    eval_end: date,
    config: WalkForwardConfig,
) -> list[tuple[date, date]]:
    """Return the per-fold ``[start, end]`` calendar ranges.

    A fold is dropped when its end would exceed ``eval_end``; when the
    overall span is too short for even one fold, an empty list is
    returned and callers fall back to the inner evaluator's normal path.
    """
    spans: list[tuple[date, date]] = []
    cursor = eval_start
    for _ in range(config.n_folds):
        end = cursor + timedelta(days=config.fold_size_days - 1)
        if end > eval_end:
            break
        spans.append((cursor, end))
        cursor = cursor + timedelta(days=config.step_days)
    return spans


class WalkForwardEvaluator:
    """:class:`FactorEvaluator` that runs the inner evaluator over folds."""

    def __init__(
        self,
        inner: FactorEvaluator,
        config: WalkForwardConfig | None = None,
    ) -> None:
        self._inner = inner
        self._config = config or WalkForwardConfig()

    def evaluate(
        self,
        factor: FactorSpec,
        request: EvaluationRequest,
    ) -> EvaluationBundle:
        spans = fold_windows(request.eval_start, request.eval_end, self._config)
        if len(spans) < 2:
            # One fold (or zero) means walk-forward isn't meaningful;
            # delegate to the inner evaluator and tag the bundle so
            # downstream code can tell why.
            bundle = self._inner.evaluate(factor, request)
            md = dict(bundle.metadata)
            md.setdefault(
                "walk_forward",
                {"n_folds": len(spans), "skipped_reason": "span_too_short"},
            )
            return bundle.model_copy(update={"metadata": md})

        per_fold: list[EvaluationBundle] = []
        for fstart, fend in spans:
            sub = request.model_copy(update={"eval_start": fstart, "eval_end": fend})
            per_fold.append(self._inner.evaluate(factor, sub))

        return _aggregate(request, per_fold, self._config)


# ── Aggregation ─────────────────────────────────────────────────────────────


def _aggregate(
    request: EvaluationRequest,
    folds: list[EvaluationBundle],
    config: WalkForwardConfig,
) -> EvaluationBundle:
    """Combine per-fold bundles into one aggregate.

    Means use only folds where the metric is non-None.  When *every*
    fold reports None for a metric, the aggregate is None.  Standard
    deviation needs at least two values to mean anything; otherwise
    it's reported as 0.0.
    """

    def _mean(name: str) -> float | None:
        vals = [getattr(b, name) for b in folds if getattr(b, name) is not None]
        return statistics.fmean(vals) if vals else None

    def _frac_positive(name: str) -> float:
        vals = [getattr(b, name) for b in folds if getattr(b, name) is not None]
        if not vals:
            return 0.0
        return sum(1 for v in vals if v > 0) / len(vals)

    def _stdev(name: str) -> float:
        vals = [getattr(b, name) for b in folds if getattr(b, name) is not None]
        return statistics.pstdev(vals) if len(vals) >= 2 else 0.0

    per_fold_payload = [
        {
            "eval_start": str(b.eval_start) if b.eval_start else "",
            "eval_end": str(b.eval_end) if b.eval_end else "",
            "ic": b.ic,
            "rank_ic": b.rank_ic,
            "quantile_spread": b.quantile_spread,
            "net_quantile_spread": b.net_quantile_spread,
            "turnover": b.turnover,
            "n_periods": b.n_periods,
        }
        for b in folds
    ]

    walk_forward_meta = {
        "n_folds": len(folds),
        "fold_size_days": config.fold_size_days,
        "step_days": config.step_days,
        "mean_ic": _mean("ic"),
        "mean_rank_ic": _mean("rank_ic"),
        "std_ic": _stdev("ic"),
        "std_rank_ic": _stdev("rank_ic"),
        "fraction_positive_ic": _frac_positive("ic"),
        "fraction_positive_rank_ic": _frac_positive("rank_ic"),
    }

    # Pull through any non-walk-forward metadata from the first fold so
    # things like ``ic_by_horizon`` survive the wrapping.
    base_meta = dict(folds[0].metadata) if folds else {}
    base_meta["walk_forward"] = walk_forward_meta
    base_meta["per_fold"] = per_fold_payload

    n_periods_total = sum(int(b.n_periods or 0) for b in folds)
    n_assets = max((int(b.n_assets or 0) for b in folds), default=0) or None

    return EvaluationBundle(
        ic=_mean("ic"),
        rank_ic=_mean("rank_ic"),
        quantile_spread=_mean("quantile_spread"),
        net_quantile_spread=_mean("net_quantile_spread"),
        turnover=_mean("turnover"),
        sharpe=_mean("sharpe"),
        n_periods=n_periods_total or None,
        n_assets=n_assets,
        eval_start=request.eval_start,
        eval_end=request.eval_end,
        forecast_horizon_bars=folds[0].forecast_horizon_bars if folds else None,
        metadata=base_meta,
    )
