"""Stub implementations used only by tests.

These stubs were previously kept alongside production code for historical
reasons. They're now scoped to ``tests/`` so production modules don't ship
with test-only helpers.
"""

from __future__ import annotations

from alpha_harness.schemas.evaluation import (
    EvaluationBundle,
    EvaluationRequest,
    MetricName,
)
from alpha_harness.schemas.factor import FactorSpec


class StubSignalQualityEvaluator:
    """Stub evaluator returning synthetic metrics derived from factor name.

    Deterministic: same factor name always produces same metrics. Useful for
    testing the orchestrator, promotion judge, and other components that need
    an ``EvaluationBundle`` without real price data.
    """

    def evaluate(
        self, factor: FactorSpec, request: EvaluationRequest
    ) -> EvaluationBundle:
        metrics = self._compute_stub_metrics(factor)
        return EvaluationBundle(
            ic=metrics.get(MetricName.IC),
            rank_ic=metrics.get(MetricName.RANK_IC),
            quantile_spread=metrics.get(MetricName.QUANTILE_SPREAD),
            monotonicity=metrics.get(MetricName.MONOTONICITY),
            turnover=metrics.get(MetricName.TURNOVER),
            sharpe=metrics.get(MetricName.SHARPE),
            n_periods=request.profile.min_periods,
            n_assets=request.profile.min_assets,
            eval_start=request.eval_start,
            eval_end=request.eval_end,
            forecast_horizon_bars=request.label.forecast_horizon_bars,
            metadata={"evaluator": "signal_quality", "mode": "stub"},
        )

    def _compute_stub_metrics(
        self, factor: FactorSpec
    ) -> dict[MetricName, float]:
        """Deterministic synthetic values derived from the factor name."""
        seed = sum(ord(c) for c in factor.name) % 100
        base = seed / 100.0
        return {
            MetricName.IC: round(base * 0.10, 4),
            MetricName.RANK_IC: round(base * 0.12, 4),
            MetricName.QUANTILE_SPREAD: round(base * 0.02, 4),
            MetricName.MONOTONICITY: round(0.5 + base * 0.5, 4),
            MetricName.TURNOVER: round(0.05 + (1 - base) * 0.30, 4),
            MetricName.SHARPE: round(base * 2.0 - 0.5, 4),
        }
