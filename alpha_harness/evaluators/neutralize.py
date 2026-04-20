"""Cross-sectional neutralization helpers.

Operates on forward returns: subtracting a common component (sector mean,
universe mean scaled by beta, or both) leaves the factor's cross-sectional
discrimination intact while removing confounds that would otherwise pad
apparent IC.

Limitations
-----------
* Beta is estimated *in-sample* over the evaluation window (one coefficient
  per symbol).  This is a deliberate first cut — full rolling / out-of-sample
  beta can replace it without changing the caller-facing API.
* Sector assignments come from a static ``dict[symbol, sector]`` passed by
  the caller.  Symbols missing from the map land in a single ``"UNKNOWN"``
  bucket; that bucket is still demeaned, but if every symbol is unknown the
  operation is a no-op.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from alpha_harness.schemas.evaluation import NeutralizeMode


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
) -> pd.Series:
    """Subtract ``beta_i * universe_mean[t]`` from each ``fwd[i, t]``.

    ``beta_i`` is the in-sample OLS slope of ``fwd_i`` against the
    cross-sectional mean return, estimated over the entire eval window.
    Symbols with zero universe-return variance get ``beta = 0`` (i.e. no
    adjustment).
    """
    universe_mean = fwd.groupby(timestamps.to_numpy()).transform("mean")
    resid = fwd.copy()

    var_univ = float(np.nanvar(universe_mean.to_numpy()))
    if var_univ == 0.0 or np.isnan(var_univ):
        return resid

    # Per-symbol beta via OLS: cov(fwd_i, mean) / var(mean) over dates.
    for sym in symbols.unique():
        mask = symbols == sym
        y = fwd[mask].to_numpy(dtype=float)
        x = universe_mean[mask].to_numpy(dtype=float)
        valid = ~(np.isnan(y) | np.isnan(x))
        if valid.sum() < 2:
            continue
        x_v = x[valid]
        y_v = y[valid]
        x_var = float(np.var(x_v))
        if x_var == 0.0:
            continue
        beta = float(np.cov(y_v, x_v, ddof=0)[0, 1] / x_var)
        resid.loc[mask] = fwd[mask] - beta * universe_mean[mask]

    return resid


def neutralize_forward_returns(
    fwd: pd.Series,
    *,
    timestamps: pd.Series,
    symbols: pd.Series | None,
    mode: NeutralizeMode,
    sector_map: dict[str, str] | None = None,
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
        fwd = _beta_neutralize(fwd, timestamps, symbols)
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
