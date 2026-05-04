"""Canonical evaluation regimes.

Round 5 introduces :data:`STRICT_REGIME` — the single source of truth for
"production-grade validation": every robustness gate the harness has
shipped (4A.3 sign-consistency, 4B walk-forward, 4C tail-concentration,
4D embargo, 4E holdout) plus the evaluator knobs (sector neutralization,
real cost in basis points, multi-horizon labels) bundled into one
immutable config.

CLIs / smoke tests / audits ask for the strict regime by name so that
"is this evaluator agreeing with the strict bar?" stays a single
attribute lookup, not a 14-field comparison.
"""

from __future__ import annotations

from dataclasses import dataclass

from alpha_harness.evaluators.walk_forward import WalkForwardConfig
from alpha_harness.schemas.evaluation import (
    EvaluationProfile,
    HoldoutPolicy,
    HoldoutStrategy,
    LabelDefinition,
    NeutralizeMode,
)


@dataclass(frozen=True)
class StrictRegime:
    """Bundle of evaluator + judge knobs for production-grade validation.

    All fields are immutable so a regime instance can be hashed into a
    trail_id (Round 4F) safely.  ``judge_thresholds`` is the dict the
    :class:`alpha_harness.evaluators.promotion_judge.PromotionJudge`
    constructor takes — having it on the regime keeps every CLI from
    re-typing the same numbers.
    """

    # Profile (rows reach the judge only when these clear)
    ic_threshold: float = 0.02
    rank_ic_threshold: float = 0.03
    quantile_spread_threshold: float = 0.005
    min_periods: int = 60
    min_assets: int = 10
    n_quantiles: int = 5

    # Evaluator richness
    cost_bps: float = 5.0
    neutralize: NeutralizeMode = NeutralizeMode.SECTOR
    extra_horizons: tuple[int, ...] = (1, 5, 20)
    forecast_horizon_bars: int = 5
    lag_bars: int = 1

    # Walk-forward + embargo + purge (Round 4B / 4D)
    n_folds: int = 4
    fold_size_days: int = 60
    step_days: int = 30
    embargo_days: int = 6  # = lag_bars + forecast_horizon_bars
    min_fold_days: int = 20

    # Holdout reservation (Round 4E)
    holdout_strategy: HoldoutStrategy = HoldoutStrategy.TAIL
    holdout_fraction: float = 0.20

    # Judge thresholds (Rounds 4A.3, 4B, 4C, 4E)
    refine_margin: float = 0.20
    min_fraction_positive_folds: float = 0.6
    max_tail_concentration: float = 0.5
    min_holdout_decay_ratio: float = 0.5

    # ── Convenience constructors ─────────────────────────────────────────

    def evaluation_profile(self) -> EvaluationProfile:
        """Build the profile every evaluator passes to the judge."""
        return EvaluationProfile(
            thresholds={
                "ic": self.ic_threshold,
                "rank_ic": self.rank_ic_threshold,
                "quantile_spread": self.quantile_spread_threshold,
            },
            min_periods=self.min_periods,
            min_assets=self.min_assets,
            n_quantiles=self.n_quantiles,
        )

    def label_definition(self) -> LabelDefinition:
        return LabelDefinition(
            forecast_horizon_bars=self.forecast_horizon_bars,
            lag_bars=self.lag_bars,
            return_type="simple",
            extra_horizons=list(self.extra_horizons),
        )

    def holdout_policy(self) -> HoldoutPolicy:
        return HoldoutPolicy(
            strategy=self.holdout_strategy,
            holdout_fraction=self.holdout_fraction,
        )

    def walk_forward_config(self) -> WalkForwardConfig:
        return WalkForwardConfig(
            n_folds=self.n_folds,
            fold_size_days=self.fold_size_days,
            step_days=self.step_days,
            embargo_days=self.embargo_days,
            min_fold_days=self.min_fold_days,
        )

    def judge_thresholds(self) -> dict[str, float]:
        """Kwargs for ``PromotionJudge(**thresholds)`` and the trail hash."""
        return {
            "refine_margin": self.refine_margin,
            "min_fraction_positive_folds": self.min_fraction_positive_folds,
            "max_tail_concentration": self.max_tail_concentration,
            "min_holdout_decay_ratio": self.min_holdout_decay_ratio,
        }


# The frozen default — every CLI uses this unless an operator explicitly
# overrides individual fields via the dataclass replace API.
STRICT_REGIME = StrictRegime()


# ── Naming registry (cheap; we'll grow it later) ─────────────────────────

_REGIMES: dict[str, StrictRegime] = {"strict": STRICT_REGIME}


def get_regime(name: str) -> StrictRegime:
    """Look up a registered regime by short name.

    Today only ``'strict'`` exists; the lookup keeps CLIs forward-
    compatible with future regimes (``'strict-v2'``, ``'lenient'``, ...)
    without changing their flag plumbing.
    """
    if name not in _REGIMES:
        raise KeyError(
            f"unknown regime {name!r}; known: {sorted(_REGIMES)}",
        )
    return _REGIMES[name]
