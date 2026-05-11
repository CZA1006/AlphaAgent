"""Round 6 — multi-factor combination.

Individual factors that fail the strict regime sometimes survive when
combined: the IC of an *equal-weighted ranking* of N weakly-correlated
factors is roughly ``IC_avg * sqrt(N) / sqrt(1 + (N-1) * rho)``, where
``rho`` is the average pairwise rank correlation.  When ``rho`` is small,
even weak individuals can produce a basket whose IC clears the bar.

This module exposes the combination strategies plus a small helper
that turns a list of compiled factors into a single ``signal`` Series
the existing :class:`SignalQualityEvaluator` can then evaluate.
"""

from alpha_harness.combination.combiner import (
    CombinationMethod,
    combine_signals,
    compute_signal,
    pairwise_rank_corr,
)

__all__ = [
    "CombinationMethod",
    "combine_signals",
    "compute_signal",
    "pairwise_rank_corr",
]
