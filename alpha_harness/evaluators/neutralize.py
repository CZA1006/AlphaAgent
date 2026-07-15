"""Cross-sectional neutralization helpers.

Operates on forward returns: subtracting a common component (sector mean,
universe mean scaled by beta, or both) leaves the factor's cross-sectional
discrimination intact while removing confounds that would otherwise pad
apparent IC.

Limitations
-----------
* Sector assignments come from a static ``dict[symbol, sector]`` passed by
  the caller.  Symbols missing from the map land in a single ``"UNKNOWN"``
  bucket; that bucket is still demeaned, but if every symbol is unknown the
  operation is a no-op.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from alpha_harness.schemas.evaluation import (
    DEFAULT_BETA_LOOKBACK_BARS,
    DEFAULT_BETA_MIN_PERIODS,
    NeutralizeMode,
)


def _sector_demean(
    fwd: pd.Series,
    timestamps: pd.Series,
    symbols: pd.Series,
    sector_map: dict[str, str],
) -> pd.Series:
    """Subtract, per (date, sector), the sector's mean forward return."""
    sectors = symbols.map(lambda s: sector_map.get(s, "UNKNOWN"))
    key = pd.MultiIndex.from_arrays(
        [timestamps.to_numpy(), sectors.to_numpy()],
        names=["_ts", "_sector"],
    )
    group_mean = fwd.groupby(key).transform("mean")
    result = fwd - group_mean
    result.index = fwd.index
    return result


def _beta_neutralize(
    fwd: pd.Series,
    timestamps: pd.Series,
    symbols: pd.Series,
    *,
    lookback_bars: int,
    min_periods: int,
) -> pd.Series:
    """Subtract ``beta_i * universe_mean[t]`` from each ``fwd[i, t]``.

    The coefficient applied at date ``t`` is estimated from at most
    ``lookback_bars`` paired observations strictly before ``t``. Rows without
    ``min_periods`` prior observations remain NaN so callers cannot mistake an
    unstable fallback for an out-of-sample residual. Symbols with zero market
    variance after warmup get beta zero.
    """
    if min_periods > lookback_bars:
        raise ValueError("min_periods must be <= lookback_bars")

    universe_mean = fwd.groupby(timestamps.to_numpy()).transform("mean")
    frame = pd.DataFrame({
        "_position": np.arange(len(fwd)),
        "_timestamp": timestamps.to_numpy(),
        "_symbol": symbols.to_numpy(),
        "_return": fwd.to_numpy(dtype=float),
        "_market": universe_mean.to_numpy(dtype=float),
    })
    result = np.full(len(frame), np.nan, dtype=float)

    for _, group in frame.groupby("_symbol", sort=False, dropna=False):
        ordered = group.sort_values(["_timestamp", "_position"], kind="stable")
        prior_return = ordered["_return"].shift(1)
        prior_market = ordered["_market"].shift(1)
        rolling = prior_return.rolling(window=lookback_bars, min_periods=min_periods)
        covariance = rolling.cov(prior_market)
        variance = prior_market.rolling(
            window=lookback_bars,
            min_periods=min_periods,
        ).var()
        beta = covariance / variance
        beta = beta.mask(variance == 0.0, 0.0)
        residual = ordered["_return"] - beta * ordered["_market"]
        zero_market = np.isclose(
            ordered["_market"].to_numpy(dtype=float),
            0.0,
            rtol=0.0,
            atol=1e-15,
        )
        residual = residual.mask(zero_market, ordered["_return"])
        result[ordered["_position"].to_numpy(dtype=int)] = residual.to_numpy()

    return pd.Series(result, index=fwd.index, name=fwd.name)


def neutralize_forward_returns(
    fwd: pd.Series,
    *,
    timestamps: pd.Series,
    symbols: pd.Series | None,
    mode: NeutralizeMode,
    sector_map: dict[str, str] | None = None,
    beta_lookback_bars: int = DEFAULT_BETA_LOOKBACK_BARS,
    beta_min_periods: int = DEFAULT_BETA_MIN_PERIODS,
) -> pd.Series:
    """Return forward returns with the requested neutralization applied.

    ``mode == NONE`` (or when ``symbols is None``) returns the input
    unchanged — the evaluator can call this unconditionally.
    """
    if mode == NeutralizeMode.NONE or symbols is None:
        return fwd
    if mode in (NeutralizeMode.SECTOR, NeutralizeMode.BOTH):
        fwd = _sector_demean(fwd, timestamps, symbols, sector_map or {})
    if mode in (NeutralizeMode.BETA, NeutralizeMode.BOTH):
        fwd = _beta_neutralize(
            fwd,
            timestamps,
            symbols,
            lookback_bars=beta_lookback_bars,
            min_periods=beta_min_periods,
        )
    return fwd


# ── Turnover ────────────────────────────────────────────────────────────────


def compute_factor_turnover(
    signal: pd.Series,
    timestamps: pd.Series,
    symbols: pd.Series | None,
) -> float | None:
    """Mean cross-sectional |Δ z-score(signal)| across consecutive dates.

    The z-score is taken per-date so the scale is consistent.  The per-date
    turnover is the mean absolute change in each symbol's z-score since the
    prior date; we then average across dates.  Returns ``None`` when fewer
    than two dates have usable data.
    """
    if symbols is None:
        return None

    df = pd.DataFrame({
        "ts": timestamps.to_numpy(),
        "sym": symbols.to_numpy(),
        "sig": signal.to_numpy(dtype=float),
    })
    # Per-date z-score.
    def _z(x: pd.Series) -> pd.Series:
        std = x.std()
        if std == 0 or np.isnan(std):
            return x * 0.0
        return (x - x.mean()) / std

    df["z"] = df.groupby("ts")["sig"].transform(_z)
    df = df.sort_values(["sym", "ts"])
    df["dz"] = df.groupby("sym")["z"].diff().abs()
    per_date = df.groupby("ts")["dz"].mean().dropna()
    if per_date.empty:
        return None
    return float(per_date.mean())


def apply_cost(
    quantile_spread: float | None,
    turnover: float | None,
    cost_bps: float,
) -> float | None:
    """Return ``quantile_spread - (cost_bps / 10000) * turnover``.

    If either input is ``None`` the original ``quantile_spread`` is returned
    unchanged (the caller is responsible for surfacing missing turnover).
    """
    if quantile_spread is None:
        return None
    if turnover is None or cost_bps == 0.0:
        return quantile_spread
    return float(quantile_spread - (cost_bps / 10_000.0) * turnover)
