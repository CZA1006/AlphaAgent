"""Evaluation schemas — request, bundle, and profile for deterministic evaluation."""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, Field, model_validator

DEFAULT_BETA_LOOKBACK_BARS = 60
DEFAULT_BETA_MIN_PERIODS = 20

# ── Evaluation profile ───────────────────────────────────────────────────────


class MetricName(StrEnum):
    """Canonical names for evaluation metrics."""

    IC = "ic"
    RANK_IC = "rank_ic"
    QUANTILE_SPREAD = "quantile_spread"
    MONOTONICITY = "monotonicity"
    TURNOVER = "turnover"
    SHARPE = "sharpe"
    NET_QUANTILE_SPREAD = "net_quantile_spread"


class HoldoutStrategy(StrEnum):
    """How to reserve an out-of-sample slice from the eval window.

    ``NONE``  — no reservation; the full ``[eval_start, eval_end]`` window
                contributes to the primary metrics (pre-4E behaviour).
    ``TAIL``  — reserve the trailing ``holdout_fraction`` of the window
                as a held-out slice.  Primary metrics are computed on the
                in-sample portion only; holdout metrics land under
                ``metadata.holdout`` and the judge enforces sign-match
                and a decay floor before promotion.
    """

    NONE = "none"
    TAIL = "tail"


class HoldoutPolicy(BaseModel):
    """How an out-of-sample holdout is carved off the eval window."""

    strategy: HoldoutStrategy = HoldoutStrategy.NONE
    holdout_fraction: float = 0.20

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        # Pydantic field validators would force a separate import; the
        # constructor check is simpler and runs once per request.
        if not (0.0 <= self.holdout_fraction < 1.0):
            raise ValueError(
                f"holdout_fraction must be in [0, 1); got {self.holdout_fraction}",
            )


class NeutralizeMode(StrEnum):
    """Cross-sectional neutralization applied to forward returns.

    ``NONE``    — raw returns (backwards-compatible default).
    ``SECTOR``  — subtract the per-date sector mean return.
    ``BETA``    — subtract ``beta * universe_mean_return`` using a strictly
                  lagged rolling beta estimated from prior dates only.
    ``BOTH``    — sector de-meaning, then beta neutralization on the residual.
    """

    NONE = "none"
    SECTOR = "sector"
    BETA = "beta"
    BOTH = "both"


class EvaluationProfile(BaseModel):
    """Declares which metrics are required and their pass thresholds.

    An evaluation can only be judged as "passing" if every required metric
    is present and meets its threshold. This prevents partial/ambiguous results
    from being silently promoted.
    """

    required_metrics: list[MetricName] = Field(
        default_factory=lambda: [MetricName.IC, MetricName.RANK_IC, MetricName.QUANTILE_SPREAD],
    )
    thresholds: dict[str, float] = Field(
        default_factory=lambda: {
            "ic": 0.02,
            "rank_ic": 0.03,
            "quantile_spread": 0.005,
        },
    )
    n_quantiles: int = 5
    min_periods: int = 60
    min_assets: int = 10


# ── Evaluation request ───────────────────────────────────────────────────────


class LabelDefinition(BaseModel):
    """How forward returns (labels) are constructed for evaluation.

    Explicit label construction prevents lookahead leakage by making the
    forecast horizon, lag, and return type part of the typed contract.
    """

    forecast_horizon_bars: int = 5  # forward return window in bars
    lag_bars: int = 1  # gap between signal and label start
    return_type: str = "simple"  # "simple" or "log"

    # Optional auxiliary horizons for sign-consistency checks.  When set,
    # the evaluator additionally computes IC / rank-IC at each horizon and
    # stores them under ``metadata["ic_by_horizon"]``.  The *primary* metrics
    # (``ic``, ``rank_ic``, ``quantile_spread``) always use
    # ``forecast_horizon_bars`` — keeping single-horizon behaviour intact.
    extra_horizons: list[int] = Field(default_factory=list)


class EvaluationRequest(BaseModel):
    """All inputs required to run a deterministic factor evaluation.

    This object makes the evaluation fully specified — no ambient state needed.
    The evaluator receives everything it needs through this typed contract.
    """

    factor_id: str
    universe_id: str

    # Time boundaries for the evaluation
    eval_start: date
    eval_end: date

    # Label construction
    label: LabelDefinition = Field(default_factory=LabelDefinition)

    # Data provenance
    dataset_snapshot_id: str = ""  # identifies which data version was used

    # Rebalance
    rebalance_frequency: str = "daily"  # "daily", "weekly", "monthly"

    # Evaluation strictness
    profile: EvaluationProfile = Field(default_factory=EvaluationProfile)

    # Predeclared family size for multiple-hypothesis pressure. A single-factor
    # caller remains fully backwards-compatible; theme/session runners set the
    # total number of proposal slots before any candidate is judged.
    n_proposals_in_session: int = Field(default=1, ge=1)

    # Cross-sectional neutralization applied to forward returns.  Default
    # ``NONE`` preserves legacy behaviour for every existing caller.
    neutralize: NeutralizeMode = NeutralizeMode.NONE

    # Causal beta-estimation policy. The coefficient applied at date t uses
    # only paired observations strictly before t.
    beta_lookback_bars: int = Field(default=DEFAULT_BETA_LOOKBACK_BARS, ge=2)
    beta_min_periods: int = Field(default=DEFAULT_BETA_MIN_PERIODS, ge=2)

    # Optional ``{symbol: sector}`` map.  Only used when ``neutralize`` is
    # ``SECTOR`` or ``BOTH``.  Symbols not present fall back to a single
    # ``"UNKNOWN"`` bucket (i.e. no effective sector neutralization).
    sector_map: dict[str, str] = Field(default_factory=dict)

    # Round-trip trading cost in basis points, applied to the quantile-spread
    # portfolio via turnover.  ``0.0`` (default) disables the cost adjustment,
    # so ``net_quantile_spread`` equals ``quantile_spread``.
    cost_bps: float = 0.0

    # Round 4E — out-of-sample reservation.  Default ``NONE`` preserves
    # legacy single-window evaluation; ``TAIL`` carves the trailing
    # ``holdout_fraction`` of the window into a held-out slice that
    # the judge cross-checks against the in-sample metrics.
    holdout: HoldoutPolicy = Field(default_factory=HoldoutPolicy)

    @model_validator(mode="after")
    def _validate_beta_window(self) -> Self:
        if self.beta_min_periods > self.beta_lookback_bars:
            raise ValueError("beta_min_periods must be <= beta_lookback_bars")
        return self


# ── Evaluation output ────────────────────────────────────────────────────────


class EvaluationBundle(BaseModel):
    """Deterministic evaluation results for a single factor run.

    Metrics are optional because not every evaluation computes every metric,
    but the EvaluationProfile determines which must be present for a valid judgment.
    """

    ic: float | None = None
    rank_ic: float | None = None
    quantile_spread: float | None = None
    monotonicity: float | None = None
    turnover: float | None = None
    sharpe: float | None = None

    # Quantile spread adjusted for round-trip trading cost.  Equals
    # ``quantile_spread`` when ``cost_bps`` is zero.
    net_quantile_spread: float | None = None

    n_periods: int | None = None
    n_assets: int | None = None
    eval_start: date | None = None
    eval_end: date | None = None
    forecast_horizon_bars: int | None = None

    # Free-form auxiliary metrics (multi-horizon ICs, walk-forward
    # per-fold breakdowns, evaluator name, etc.).  Kept as ``Any`` so
    # later rounds can attach richer payloads without schema churn.
    metadata: dict[str, Any] = Field(default_factory=dict)
    computed_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
    )

    def passes_profile(self, profile: EvaluationProfile) -> bool:
        """Check whether this bundle meets all required thresholds."""
        for metric in profile.required_metrics:
            value = getattr(self, metric.value, None)
            if value is None:
                return False
            threshold = profile.thresholds.get(metric.value)
            if threshold is not None and value < threshold:
                return False
        return True
