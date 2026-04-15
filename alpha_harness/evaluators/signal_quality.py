"""Signal-quality evaluator — deterministic factor evaluation.

Implements the FactorEvaluator protocol. Currently returns synthetic
metrics; real IC / rank-IC / quantile-spread computation requires data
loaders and the factor DSL (Round 2).
"""

from __future__ import annotations

from alpha_harness.schemas.evaluation import (
    EvaluationBundle,
    EvaluationRequest,
    MetricName,
)
from alpha_harness.schemas.factor import FactorSpec


class SignalQualityEvaluator:
    """Deterministic signal-quality evaluator (``FactorEvaluator`` protocol).

    Currently returns synthetic stub metrics. Replace ``_compute_stub_metrics``
    with real cross-sectional IC computation once data loaders are wired.
    """

    def evaluate(
        self, factor: FactorSpec, request: EvaluationRequest
    ) -> EvaluationBundle:
        """Run evaluation and return an EvaluationBundle.

        Parameters
        ----------
        factor:
            Compiled factor specification (expression + optional AST).
        request:
            Fully-specified evaluation context including universe, date range,
            label definition, and dataset snapshot id.

        Returns
        -------
        EvaluationBundle with all metrics requested by the profile.
        """
        metrics = self._compute_stub_metrics(factor, request)
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
        self, factor: FactorSpec, request: EvaluationRequest
    ) -> dict[MetricName, float]:
        """Placeholder metric computation.

        Generates deterministic synthetic values derived from the factor name
        so that the same factor always yields the same evaluation result.
        This makes tests deterministic without any data dependencies.

        Will be replaced by real cross-sectional IC computation in Round 2.
        """
        # Deterministic seed from factor name — same name → same metrics
        seed = sum(ord(c) for c in factor.name) % 100
        base = seed / 100.0  # 0.0 to 0.99

        return {
            MetricName.IC: round(base * 0.10, 4),          # 0.00 to 0.10
            MetricName.RANK_IC: round(base * 0.12, 4),     # 0.00 to 0.12
            MetricName.QUANTILE_SPREAD: round(base * 0.02, 4),  # 0.00 to 0.02
            MetricName.MONOTONICITY: round(0.5 + base * 0.5, 4),  # 0.50 to 1.00
            MetricName.TURNOVER: round(0.05 + (1 - base) * 0.30, 4),  # 0.05 to 0.35
            MetricName.SHARPE: round(base * 2.0 - 0.5, 4),  # -0.50 to 1.50
        }
