"""Evaluation schemas — request, bundle, and profile for deterministic evaluation."""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

# ── Evaluation profile ───────────────────────────────────────────────────────


class MetricName(StrEnum):
    """Canonical names for evaluation metrics."""

    IC = "ic"
    RANK_IC = "rank_ic"
    QUANTILE_SPREAD = "quantile_spread"
    MONOTONICITY = "monotonicity"
    TURNOVER = "turnover"
    SHARPE = "sharpe"


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

    forecast_horizon_bars: int = 5        # forward return window in bars
    lag_bars: int = 1                     # gap between signal and label start
    return_type: str = "simple"           # "simple" or "log"


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
    dataset_snapshot_id: str = ""         # identifies which data version was used

    # Rebalance
    rebalance_frequency: str = "daily"    # "daily", "weekly", "monthly"

    # Evaluation strictness
    profile: EvaluationProfile = Field(default_factory=EvaluationProfile)


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

    n_periods: int | None = None
    n_assets: int | None = None
    eval_start: date | None = None
    eval_end: date | None = None
    forecast_horizon_bars: int | None = None

    metadata: dict[str, str | float | int | bool] = Field(default_factory=dict)
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
