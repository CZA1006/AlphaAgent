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

from alpha_harness.schemas.evaluation import (
    EvaluationBundle,
    EvaluationRequest,
    HoldoutPolicy,
    HoldoutStrategy,
)
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

    ``embargo_days`` strips that many days off the *end* of every fold
    before evaluation.  This prevents the overlapping forward-return
    label that closes fold N from leaking into fold N+1's signal.  The
    default ``None`` lets :class:`WalkForwardEvaluator` derive the
    embargo from the request's :class:`LabelDefinition`
    (``lag_bars + forecast_horizon_bars``).  ``0`` disables the embargo
    explicitly — pre-4D behaviour.

    ``min_fold_days`` is the smallest *post-embargo* span that still
    counts as a usable fold; smaller spans are *purged* (dropped) and
    counted under ``metadata.walk_forward.purged_folds``.
    """

    n_folds: int = 4
    fold_size_days: int = 60
    step_days: int = 20
    embargo_days: int | None = None
    min_fold_days: int = 20

    def __post_init__(self) -> None:
        if self.n_folds < 1:
            raise ValueError("n_folds must be >= 1")
        if self.fold_size_days < 1:
            raise ValueError("fold_size_days must be >= 1")
        if self.step_days < 1:
            raise ValueError("step_days must be >= 1")
        if self.embargo_days is not None and self.embargo_days < 0:
            raise ValueError("embargo_days must be >= 0 when set")
        if self.min_fold_days < 1:
            raise ValueError("min_fold_days must be >= 1")


# ── Public API ──────────────────────────────────────────────────────────────


def fold_windows(
    eval_start: date,
    eval_end: date,
    config: WalkForwardConfig,
    *,
    embargo_days: int = 0,
) -> list[tuple[date, date]]:
    """Return the per-fold ``[start, end]`` calendar ranges, embargoed.

    A fold is constructed by advancing ``step_days`` from the previous
    fold's start and taking ``fold_size_days`` calendar days.  After
    construction, ``embargo_days`` is trimmed off the *end* so the
    forward-return label that closes the fold cannot leak into a
    subsequent fold's signal.  Folds that shrink below
    ``config.min_fold_days`` are dropped.

    Returns an empty list when the span is too short for even one
    usable fold; callers fall back to the inner evaluator's normal
    path.

    The function never raises for unreasonable embargoes — over-aggressive
    settings simply produce zero folds, which the evaluator surfaces via
    ``metadata.walk_forward.purged_folds``.
    """
    spans: list[tuple[date, date]] = []
    cursor = eval_start
    for _ in range(config.n_folds):
        gross_end = cursor + timedelta(days=config.fold_size_days - 1)
        if gross_end > eval_end:
            break
        net_end = gross_end - timedelta(days=embargo_days)
        if (net_end - cursor).days + 1 < config.min_fold_days:
            cursor = cursor + timedelta(days=config.step_days)
            continue
        spans.append((cursor, net_end))
        cursor = cursor + timedelta(days=config.step_days)
    return spans


def _count_attempted_folds(
    eval_start: date,
    eval_end: date,
    config: WalkForwardConfig,
) -> int:
    """How many folds *would* have fit ignoring embargo / min-size purges.

    Used to compute ``purged_folds`` — attempted minus retained.
    """
    cursor = eval_start
    count = 0
    for _ in range(config.n_folds):
        gross_end = cursor + timedelta(days=config.fold_size_days - 1)
        if gross_end > eval_end:
            break
        count += 1
        cursor = cursor + timedelta(days=config.step_days)
    return count


def _derive_embargo_days(
    config: WalkForwardConfig,
    request: EvaluationRequest,
) -> int:
    """Resolve the effective embargo, falling back to the label spec."""
    if config.embargo_days is not None:
        return int(config.embargo_days)
    return int(request.label.lag_bars) + int(request.label.forecast_horizon_bars)


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
        if (
            request.holdout.strategy is HoldoutStrategy.TAIL
            and request.holdout.holdout_fraction > 0
        ):
            return self._evaluate_with_global_holdout(factor, request)

        embargo_days = _derive_embargo_days(self._config, request)
        spans = fold_windows(
            request.eval_start,
            request.eval_end,
            self._config,
            embargo_days=embargo_days,
        )
        attempted = _count_attempted_folds(
            request.eval_start,
            request.eval_end,
            self._config,
        )
        purged = max(0, attempted - len(spans))

        if len(spans) < 2:
            # One fold (or zero) means walk-forward isn't meaningful;
            # delegate to the inner evaluator and tag the bundle so
            # downstream code can tell why.
            bundle = self._inner.evaluate(factor, request)
            md = dict(bundle.metadata)
            md.setdefault(
                "walk_forward",
                {
                    "n_folds": len(spans),
                    "embargo_days": embargo_days,
                    "purged_folds": purged,
                    "skipped_reason": "span_too_short",
                },
            )
            return bundle.model_copy(update={"metadata": md})

        per_fold: list[EvaluationBundle] = []
        for fstart, fend in spans:
            sub = request.model_copy(update={"eval_start": fstart, "eval_end": fend})
            per_fold.append(self._inner.evaluate(factor, sub))

        return _aggregate(
            request,
            per_fold,
            self._config,
            embargo_days=embargo_days,
            purged_folds=purged,
        )

    def _evaluate_with_global_holdout(
        self,
        factor: FactorSpec,
        request: EvaluationRequest,
    ) -> EvaluationBundle:
        """Reserve one trailing holdout after walk-forward aggregation."""
        total_days = (request.eval_end - request.eval_start).days + 1
        holdout_days = max(1, round(total_days * request.holdout.holdout_fraction))
        holdout_days = min(holdout_days, total_days - 1)
        disabled = HoldoutPolicy(strategy=HoldoutStrategy.NONE)
        if holdout_days < 1:
            return self.evaluate(
                factor,
                request.model_copy(update={"holdout": disabled}),
            )

        split_start = request.eval_end - timedelta(days=holdout_days - 1)
        in_sample = self.evaluate(
            factor,
            request.model_copy(
                update={
                    "eval_end": split_start - timedelta(days=1),
                    "holdout": disabled,
                },
            ),
        )
        held_out = self._inner.evaluate(
            factor,
            request.model_copy(
                update={
                    "eval_start": split_start,
                    "holdout": disabled,
                },
            ),
        )

        decay_ratio: float | None = None
        if (
            in_sample.rank_ic is not None
            and held_out.rank_ic is not None
            and in_sample.rank_ic != 0
        ):
            decay_ratio = held_out.rank_ic / in_sample.rank_ic

        metadata = dict(in_sample.metadata)
        metadata["holdout"] = {
            "holdout_start": str(split_start),
            "holdout_end": str(request.eval_end),
            "holdout_days": holdout_days,
            "embargo_bars": request.label.lag_bars
            + request.label.forecast_horizon_bars,
            "embargo_mode": "window_local_forward_returns",
            "ic": held_out.ic,
            "rank_ic": held_out.rank_ic,
            "quantile_spread": held_out.quantile_spread,
            "net_quantile_spread": held_out.net_quantile_spread,
            "turnover": held_out.turnover,
            "n_periods": held_out.n_periods,
            "decay_ratio": decay_ratio,
        }
        return in_sample.model_copy(
            update={
                "eval_start": request.eval_start,
                "eval_end": request.eval_end,
                "metadata": metadata,
            },
        )


# ── Aggregation ─────────────────────────────────────────────────────────────


def _aggregate(
    request: EvaluationRequest,
    folds: list[EvaluationBundle],
    config: WalkForwardConfig,
    *,
    embargo_days: int = 0,
    purged_folds: int = 0,
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
        "embargo_days": embargo_days,
        "purged_folds": purged_folds,
        "mean_ic": _mean("ic"),
        "mean_rank_ic": _mean("rank_ic"),
        "std_ic": _stdev("ic"),
        "std_rank_ic": _stdev("rank_ic"),
        "fraction_positive_ic": _frac_positive("ic"),
        "fraction_positive_rank_ic": _frac_positive("rank_ic"),
    }

    base_meta = _aggregate_metadata(folds, request)
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


def _aggregate_metadata(
    folds: list[EvaluationBundle],
    request: EvaluationRequest,
) -> dict[str, object]:
    """Aggregate judge-relevant metadata without leaking the first fold."""
    metadata: dict[str, object] = {}
    for key in ("evaluator", "mode", "neutralize", "cost_bps", "beta_estimation"):
        stable_values = [fold.metadata.get(key) for fold in folds if key in fold.metadata]
        if stable_values and all(value == stable_values[0] for value in stable_values):
            metadata[key] = stable_values[0]

    for key in ("ic_by_horizon", "rank_ic_by_horizon"):
        horizons: dict[str, list[float]] = {}
        for fold in folds:
            payload = fold.metadata.get(key)
            if not isinstance(payload, dict):
                continue
            for horizon, value in payload.items():
                if isinstance(value, int | float):
                    horizons.setdefault(str(horizon), []).append(float(value))
        if horizons:
            metadata[key] = {
                horizon: statistics.fmean(horizon_values)
                for horizon, horizon_values in sorted(
                    horizons.items(), key=lambda item: int(item[0])
                )
            }

    ic_by_horizon = metadata.get("ic_by_horizon")
    if isinstance(ic_by_horizon, dict):
        primary = ic_by_horizon.get(str(request.label.forecast_horizon_bars))
        if isinstance(primary, int | float) and primary != 0:
            metadata["ic_sign_consistent_horizons"] = sum(
                1
                for value in ic_by_horizon.values()
                if isinstance(value, int | float) and value != 0 and (value > 0) == (primary > 0)
            )

    portfolios: list[dict[str, object]] = []
    for fold in folds:
        payload = fold.metadata.get("portfolio")
        if isinstance(payload, dict):
            portfolios.append({str(key): value for key, value in payload.items()})
    if portfolios:
        portfolio: dict[str, float] = {}
        keys = set().union(*(payload.keys() for payload in portfolios))
        for key in keys:
            portfolio_values: list[float] = []
            for payload in portfolios:
                value = payload.get(key)
                if isinstance(value, int | float):
                    portfolio_values.append(float(value))
            if portfolio_values:
                if key in {
                    "tail_concentration",
                    "episode_top3_positive_share",
                    "episode_top3_positive_share_max",
                }:
                    portfolio[key] = max(portfolio_values)
                elif key == "episode_min_positive_count":
                    portfolio[key] = min(portfolio_values)
                else:
                    portfolio[key] = statistics.fmean(portfolio_values)
        metadata["portfolio"] = portfolio
    return metadata
