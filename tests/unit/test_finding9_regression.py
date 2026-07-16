"""Regression test for audit Finding 9 — per-fold IC parity.

Pre-fix: SignalQualityEvaluator filtered df to [fold_start, fold_end]
BEFORE the DSL ran, so rolling operators (ts_mean etc.) at the fold
boundary saw zero prior history and produced degenerate signal values
under `min_periods=1`.  Same factor through `compute_signal` + slice
(combiner) gave different per-fold ICs.

This test runs the same expression through both paths and asserts
fold-by-fold parity.  Fails loudly if the bug regresses.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from alpha_harness.combination import compute_signal
from alpha_harness.evaluators.signal_quality import (
    SignalQualityEvaluator,
    evaluate_precomputed_signal,
)
from alpha_harness.evaluators.walk_forward import (
    WalkForwardConfig,
    WalkForwardEvaluator,
)
from alpha_harness.factors.compiler import FactorDslCompiler
from alpha_harness.schemas.evaluation import (
    EvaluationRequest,
    HoldoutPolicy,
    HoldoutStrategy,
)
from alpha_harness.schemas.hypothesis import Hypothesis


def _make_panel(n_dates: int = 250, n_symbols: int = 20, seed: int = 0) -> pd.DataFrame:
    """Deterministic panel with mild cross-sectional structure."""
    rng = np.random.default_rng(seed)
    timestamps = pd.date_range("2024-01-01", periods=n_dates, freq="D", tz="UTC")
    symbols = [f"S{i:02d}" for i in range(n_symbols)]
    frames = []
    for i, sym in enumerate(symbols):
        # Each symbol gets a slightly different drift + noise.
        drift = 0.0001 + 0.0001 * i
        returns = rng.normal(drift, 0.02, n_dates)
        close = 100.0 * np.exp(returns.cumsum())
        frames.append(
            pd.DataFrame(
                {
                    "timestamp": timestamps,
                    "symbol": sym,
                    "close": close,
                    "open": close,
                    "high": close * 1.01,
                    "low": close * 0.99,
                    "volume": (1e6 + rng.standard_normal(n_dates) * 1e5).clip(min=1),
                },
            ),
        )
    return pd.concat(frames, ignore_index=True)


class _PrecomputedInner:
    """Inner evaluator that mirrors what combine_factors does."""

    def __init__(self, signal: pd.Series, df: pd.DataFrame) -> None:
        self._s = signal.reset_index(drop=True)
        self._d = df.reset_index(drop=True)
        self._dates = pd.to_datetime(self._d["timestamp"]).dt.date

    def evaluate(self, factor, request):
        m = (self._dates >= request.eval_start) & (self._dates <= request.eval_end)
        return evaluate_precomputed_signal(
            signal=self._s.loc[m].reset_index(drop=True),
            df=self._d.loc[m].reset_index(drop=True),
            request=request,
        )


def test_sqe_and_precomputed_per_fold_ic_match() -> None:
    """The two walk-forward inner evaluators must produce identical per-fold ICs.

    Regression guard for audit Finding 9 — pre-fix, the SignalQualityEvaluator
    path inflated fold 2+ ICs because it recomputed the signal per-fold without
    prior history.
    """
    df = _make_panel()
    # An expression that exercises rolling operators with non-trivial windows
    # so the fold-boundary degeneracy would matter pre-fix.
    expr = "rank(ts_mean(close, 10) / ts_mean(close, 5) - 1)"
    factor = FactorDslCompiler().compile(Hypothesis(text=expr))

    ts_dates = pd.to_datetime(df["timestamp"]).dt.date
    request = EvaluationRequest(
        factor_id="t",
        universe_id="t",
        eval_start=ts_dates.min(),
        eval_end=ts_dates.max(),
        holdout=HoldoutPolicy(strategy=HoldoutStrategy.NONE),
    )
    wf_config = WalkForwardConfig(
        n_folds=4,
        fold_size_days=60,
        step_days=30,
        embargo_days=0,
    )

    # Path A: SignalQualityEvaluator → WalkForwardEvaluator
    bundle_sqe = WalkForwardEvaluator(SignalQualityEvaluator(df), wf_config).evaluate(
        factor,
        request,
    )
    # Path B: precomputed signal → WalkForwardEvaluator
    sig = compute_signal(expr, df)
    bundle_pre = WalkForwardEvaluator(_PrecomputedInner(sig, df), wf_config).evaluate(
        factor,
        request,
    )

    per_fold_sqe = [f["ic"] for f in bundle_sqe.metadata["per_fold"]]
    per_fold_pre = [f["ic"] for f in bundle_pre.metadata["per_fold"]]
    assert len(per_fold_sqe) == len(per_fold_pre) == 4

    for i, (a, b) in enumerate(zip(per_fold_sqe, per_fold_pre, strict=True)):
        # Allow tiny floating-point noise but reject substantive divergence.
        if a is None or b is None:
            assert a is None and b is None, f"fold {i}: one None, one not"
            continue
        assert abs(a - b) < 1e-9, (
            f"fold {i}: SQE ic={a:.6f}, precomputed ic={b:.6f}, "
            f"diff={abs(a - b):.6f} — Finding 9 regressed"
        )

    # Aggregated bundle IC must match too.
    assert bundle_sqe.ic is not None and bundle_pre.ic is not None
    assert abs(bundle_sqe.ic - bundle_pre.ic) < 1e-9
    assert abs(bundle_sqe.rank_ic - bundle_pre.rank_ic) < 1e-9
