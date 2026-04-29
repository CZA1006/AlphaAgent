"""Per-date long-short returns + risk-aware portfolio metrics.

The quantile-spread mean already lives in
:func:`alpha_harness.evaluators.signal_quality.compute_quantile_spread`,
but that single number hides the *shape* of the return stream.  Two
factors with identical means can have very different drawdowns,
volatility, or tail concentration.  This module exposes the per-date
spread series and a small bag of derived statistics so the judge and
the cycle report can see beyond the average.

All metrics are deliberately simple and parameter-free — they live
behind ``metadata["portfolio"]`` and only the ``tail_concentration``
gate flips a decision; everything else is informational.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

# Trading-day annualisation factor used for Sharpe.
_TRADING_DAYS = 252


def compute_long_short_returns(
    signal: pd.Series,
    fwd_returns: pd.Series,
    timestamps: pd.Series,
    n_quantiles: int = 5,
) -> pd.Series:
    """Per-date long-short return: ``top_quantile_mean - bottom_quantile_mean``.

    Parallels :func:`compute_quantile_spread` but returns the underlying
    series instead of the mean, indexed by unique timestamp value.
    Dates whose cross-section is too small or has insufficient signal
    variation are dropped silently — the same defensive behaviour as
    the spread function.
    """
    valid = ~(signal.isna() | fwd_returns.isna())
    s = signal[valid].reset_index(drop=True)
    f = fwd_returns[valid].reset_index(drop=True)
    t = timestamps[valid].reset_index(drop=True)

    out_index: list[object] = []
    out_values: list[float] = []
    for ts_val in t.unique():
        mask = t == ts_val
        s_cs = s[mask]
        f_cs = f[mask]
        if len(s_cs) < n_quantiles:
            continue
        try:
            q_labels: pd.Series = pd.qcut(
                s_cs,
                n_quantiles,
                labels=False,
                duplicates="drop",
            )
        except ValueError:
            continue
        q_means: pd.Series = f_cs.groupby(q_labels).mean()
        if len(q_means) < 2:
            continue
        spread = float(q_means.iloc[-1] - q_means.iloc[0])
        if not np.isnan(spread):
            out_index.append(ts_val)
            out_values.append(spread)

    return pd.Series(out_values, index=out_index, name="long_short_return")


def compute_portfolio_metrics(
    returns: pd.Series,
    *,
    annualisation: int = _TRADING_DAYS,
) -> dict[str, float | None]:
    """Summarise a return stream with risk-aware statistics.

    ``returns`` is the per-date long-short series produced by
    :func:`compute_long_short_returns`.  Returns a dict with:

    * ``mean_return`` — arithmetic mean per period.
    * ``vol`` — standard deviation per period (sample, ddof=1).
    * ``sharpe`` — ``mean / vol * sqrt(annualisation)`` when ``vol > 0``.
    * ``max_drawdown`` — peak-to-trough drawdown of the cumulative
      arithmetic return path; reported as a non-negative number
      (0.05 means a 5% drawdown).
    * ``hit_rate`` — fraction of strictly-positive periods.
    * ``tail_concentration`` — ``sum(top-3 returns) / sum(returns)``
      when the total is positive; otherwise ``None``.  > 0.5 means
      three days carry the majority of the gross return.
    * ``n_periods`` — sample size used.
    """
    if returns is None or len(returns) == 0:
        return {
            "mean_return": None,
            "vol": None,
            "sharpe": None,
            "max_drawdown": None,
            "hit_rate": None,
            "tail_concentration": None,
            "n_periods": 0,
        }

    arr = np.asarray(returns, dtype=float)
    arr = arr[~np.isnan(arr)]
    n = int(arr.size)
    if n == 0:
        return {
            "mean_return": None,
            "vol": None,
            "sharpe": None,
            "max_drawdown": None,
            "hit_rate": None,
            "tail_concentration": None,
            "n_periods": 0,
        }

    mean_ret = float(arr.mean())
    vol = float(arr.std(ddof=1)) if n >= 2 else 0.0
    # Treat near-zero vol as undefined Sharpe — numpy can return tiny
    # floating-point residuals (1e-18) on a "constant" series.
    sharpe = float(mean_ret / vol * math.sqrt(annualisation)) if vol > 1e-12 else None
    hit_rate = float((arr > 0).sum() / n)

    # Max drawdown on cumulative arithmetic returns.  We cap the
    # running max at zero so a strategy that's never been positive
    # still yields a sensible (non-zero) drawdown reading.
    cumulative = np.cumsum(arr)
    running_max = np.maximum.accumulate(cumulative)
    drawdowns = running_max - cumulative
    max_drawdown = float(drawdowns.max()) if drawdowns.size else 0.0

    # Tail concentration — only meaningful when the total is positive.
    total = float(arr.sum())
    tail_concentration: float | None
    if total > 0 and n >= 3:
        top3 = float(np.sort(arr)[-3:].sum())
        tail_concentration = top3 / total
    else:
        tail_concentration = None

    return {
        "mean_return": mean_ret,
        "vol": vol,
        "sharpe": sharpe,
        "max_drawdown": max_drawdown,
        "hit_rate": hit_rate,
        "tail_concentration": tail_concentration,
        "n_periods": float(n),
    }
