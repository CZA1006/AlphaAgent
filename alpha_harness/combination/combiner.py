"""Combine N factor signals into a single basket signal.

Three strategies, each parameter-free so the combination is reproducible:

* ``rank_aggregate`` — Borda count: at every (date, asset) we take the
  cross-sectional rank of each factor's signal and sum the ranks.  This
  is the most robust ensemble — outliers in any one factor can't
  swamp the basket because ranks are bounded.
* ``zscore_average`` — at every date we cross-sectionally z-score each
  factor and take the mean.  Sensitive to outliers but preserves more
  information than ranks when the underlying factors are well-behaved.
* ``equal_weight`` — naive mean of the raw signals.  Worst statistical
  properties; included as a baseline.

All three return a single ``pd.Series`` aligned to the input panel that
:class:`alpha_harness.evaluators.signal_quality.SignalQualityEvaluator`
can score directly — there's no special "combined factor" type, the
basket *is* a factor under the existing contract.
"""

from __future__ import annotations

from enum import StrEnum

import numpy as np
import pandas as pd

from alpha_harness.factors.dsl_executor import DslExecutor
from alpha_harness.factors.dsl_parser import parse_expression


class CombinationMethod(StrEnum):
    RANK_AGGREGATE = "rank_aggregate"
    ZSCORE_AVERAGE = "zscore_average"
    EQUAL_WEIGHT = "equal_weight"


def compute_signal(
    expression: str,
    df: pd.DataFrame,
    *,
    extra_fields: frozenset[str] | None = None,
) -> pd.Series:
    """Compile + execute a DSL expression on ``df``; return the signal series.

    Mirrors the inner step of
    :class:`alpha_harness.evaluators.signal_quality.SignalQualityEvaluator.evaluate`
    so combiners can produce per-factor series without spinning up a full
    research cycle.
    """
    ast = parse_expression(expression, extra_fields=extra_fields)
    return DslExecutor(df).execute(ast)


def _cross_sectional_rank(signal: pd.Series, timestamps: pd.Series) -> pd.Series:
    """Per-date dense ranks of the signal; NaNs preserved."""
    out = pd.Series(np.nan, index=signal.index, dtype=float)
    for ts in timestamps.unique():
        mask = timestamps == ts
        out.loc[mask] = signal.loc[mask].rank(method="average")
    return out


def _cross_sectional_zscore(signal: pd.Series, timestamps: pd.Series) -> pd.Series:
    out = pd.Series(np.nan, index=signal.index, dtype=float)
    for ts in timestamps.unique():
        mask = timestamps == ts
        block = signal.loc[mask]
        sd = block.std()
        if sd == 0 or pd.isna(sd):
            continue
        out.loc[mask] = (block - block.mean()) / sd
    return out


def combine_signals(
    signals: list[pd.Series],
    timestamps: pd.Series,
    method: CombinationMethod = CombinationMethod.RANK_AGGREGATE,
) -> pd.Series:
    """Reduce ``N`` factor signals into one basket signal.

    All inputs must share the same row alignment (same length, same
    implicit index ordering) — typically because they all came from
    :func:`compute_signal` against the same ``df``.
    """
    if not signals:
        raise ValueError("combine_signals: at least one signal required")
    if not all(len(s) == len(signals[0]) for s in signals):
        raise ValueError("combine_signals: all signals must share length")

    if method is CombinationMethod.RANK_AGGREGATE:
        ranked = [_cross_sectional_rank(s, timestamps) for s in signals]
        # Sum of ranks: NaN-aware so missing values don't poison the basket.
        stacked = pd.concat(ranked, axis=1)
        return stacked.sum(axis=1, min_count=1)

    if method is CombinationMethod.ZSCORE_AVERAGE:
        zs = [_cross_sectional_zscore(s, timestamps) for s in signals]
        stacked = pd.concat(zs, axis=1)
        return stacked.mean(axis=1)

    # EQUAL_WEIGHT
    stacked = pd.concat(signals, axis=1)
    return stacked.mean(axis=1)


def pairwise_rank_corr(
    signals: list[pd.Series],
    timestamps: pd.Series,
) -> pd.DataFrame:
    """Mean cross-sectional Spearman correlation between every pair of signals.

    A diagonal of ``1.0`` plus small off-diagonal values means the
    factors are decorrelated — exactly the condition under which
    rank-aggregation gains the most.
    """
    n = len(signals)
    out = np.full((n, n), np.nan)
    ranked = [_cross_sectional_rank(s, timestamps) for s in signals]
    dates = timestamps.unique()
    for i in range(n):
        for j in range(i, n):
            per_date_corrs: list[float] = []
            for ts in dates:
                mask = timestamps == ts
                a = ranked[i].loc[mask]
                b = ranked[j].loc[mask]
                if a.std() == 0 or b.std() == 0:
                    continue
                c = a.corr(b)
                if not pd.isna(c):
                    per_date_corrs.append(float(c))
            mean = float(np.mean(per_date_corrs)) if per_date_corrs else np.nan
            out[i, j] = mean
            out[j, i] = mean
    return pd.DataFrame(out)
