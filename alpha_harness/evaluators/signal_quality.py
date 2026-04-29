"""Signal-quality evaluator — real IC, RankIC, and quantile-spread computation.

Implements the FactorEvaluator protocol with deterministic metric computation.
All metrics are reproducible given the same inputs.

Metric definitions
------------------
IC (Information Coefficient):
    Mean cross-sectional Pearson correlation between the factor signal and
    forward returns, averaged across dates.

RankIC:
    Mean cross-sectional Spearman rank correlation between the factor signal
    and forward returns, averaged across dates. More robust to outliers than
    Pearson IC.

Quantile spread:
    At each date, sort assets into N quantile buckets by signal value.
    Compute mean forward return per bucket. Spread = top bucket mean minus
    bottom bucket mean, averaged across dates.

Forward returns:
    Constructed from close prices using the LabelDefinition contract:
    fwd_return[t] = close[t + lag + horizon] / close[t + lag] - 1  (simple)
    fwd_return[t] = ln(close[t + lag + horizon] / close[t + lag])  (log)
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from alpha_harness.evaluators.neutralize import (
    apply_cost,
    compute_factor_turnover,
    neutralize_forward_returns,
)
from alpha_harness.factors.dsl_executor import DslExecutor
from alpha_harness.factors.dsl_parser import parse_expression
from alpha_harness.schemas.evaluation import (
    EvaluationBundle,
    EvaluationRequest,
    LabelDefinition,
)
from alpha_harness.schemas.factor import FactorSpec

# ── Forward return construction ──────────────────────────────────────────────


def build_forward_returns(
    close: pd.Series,
    groups: pd.Series | None,
    label: LabelDefinition,
) -> pd.Series:
    """Construct forward returns from close prices.

    Parameters
    ----------
    close:
        Close price series, aligned to the panel index.
    groups:
        Symbol grouping series. When provided, shifts are applied per-group
        so that one symbol's future prices don't leak into another's.
        Pass ``None`` for single-symbol data.
    label:
        Defines ``lag_bars``, ``forecast_horizon_bars``, and ``return_type``.

    Returns
    -------
    Series of forward returns aligned to the input index. Rows near the end
    of each symbol's history are ``NaN`` because future prices are unavailable.
    """
    lag = label.lag_bars
    horizon = label.forecast_horizon_bars
    total_shift = lag + horizon

    if groups is not None:
        future_end: pd.Series = close.groupby(groups).shift(-total_shift)
        future_start: pd.Series = close.groupby(groups).shift(-lag)
    else:
        future_end = close.shift(-total_shift)
        future_start = close.shift(-lag)

    if label.return_type == "log":
        result: pd.Series = pd.Series(np.log(future_end / future_start), index=close.index)
    else:
        result = future_end / future_start - 1

    # Replace inf/-inf with NaN (e.g. if future_start is 0)
    result = result.replace([np.inf, -np.inf], np.nan)
    return result


# ── Cross-sectional metric functions ─────────────────────────────────────────
# All functions are pure: no side effects, no global state.
# They take aligned Series and return a single float or None.


def compute_mean_ic(
    signal: pd.Series,
    fwd_returns: pd.Series,
    timestamps: pd.Series,
    min_obs: int = 3,
) -> float | None:
    """Mean cross-sectional Pearson IC.

    At each timestamp, compute the Pearson correlation between the signal
    values and the forward returns across all assets. Return the mean of
    these per-date ICs. Timestamps with fewer than ``min_obs`` valid
    observations are skipped.
    """
    valid = ~(signal.isna() | fwd_returns.isna())
    s = signal[valid].reset_index(drop=True)
    f = fwd_returns[valid].reset_index(drop=True)
    t = timestamps[valid].reset_index(drop=True)

    ics: list[float] = []
    for ts_val in t.unique():
        mask = t == ts_val
        s_cs = s[mask]
        f_cs = f[mask]
        if len(s_cs) < min_obs:
            continue
        if s_cs.std() == 0 or f_cs.std() == 0:
            continue
        ic_val = float(s_cs.corr(f_cs))
        if not np.isnan(ic_val):
            ics.append(ic_val)

    if not ics:
        return None
    return float(np.mean(ics))


def compute_mean_rank_ic(
    signal: pd.Series,
    fwd_returns: pd.Series,
    timestamps: pd.Series,
    min_obs: int = 3,
) -> float | None:
    """Mean cross-sectional Spearman rank IC.

    Same as ``compute_mean_ic`` but uses rank correlation. Computed by
    ranking both signal and forward returns within each cross-section,
    then taking Pearson correlation of the ranks.
    """
    valid = ~(signal.isna() | fwd_returns.isna())
    s = signal[valid].reset_index(drop=True)
    f = fwd_returns[valid].reset_index(drop=True)
    t = timestamps[valid].reset_index(drop=True)

    ics: list[float] = []
    for ts_val in t.unique():
        mask = t == ts_val
        s_cs = s[mask]
        f_cs = f[mask]
        if len(s_cs) < min_obs:
            continue
        s_ranked = s_cs.rank()
        f_ranked = f_cs.rank()
        if s_ranked.std() == 0 or f_ranked.std() == 0:
            continue
        ic_val = float(s_ranked.corr(f_ranked))
        if not np.isnan(ic_val):
            ics.append(ic_val)

    if not ics:
        return None
    return float(np.mean(ics))


def compute_quantile_spread(
    signal: pd.Series,
    fwd_returns: pd.Series,
    timestamps: pd.Series,
    n_quantiles: int = 5,
) -> float | None:
    """Mean cross-sectional quantile spread (long-short).

    At each timestamp, sort assets into ``n_quantiles`` buckets by signal
    value. Compute the mean forward return in each bucket. The spread is
    ``top_bucket_mean - bottom_bucket_mean``, averaged across dates.

    A positive spread indicates that higher signal values predict higher
    forward returns — the fundamental property of a useful alpha signal.
    """
    valid = ~(signal.isna() | fwd_returns.isna())
    s = signal[valid].reset_index(drop=True)
    f = fwd_returns[valid].reset_index(drop=True)
    t = timestamps[valid].reset_index(drop=True)

    spreads: list[float] = []
    for ts_val in t.unique():
        mask = t == ts_val
        s_cs = s[mask]
        f_cs = f[mask]
        if len(s_cs) < n_quantiles:
            continue
        try:
            q_labels: pd.Series = pd.qcut(s_cs, n_quantiles, labels=False, duplicates="drop")
        except ValueError:
            continue
        q_means: pd.Series = f_cs.groupby(q_labels).mean()
        if len(q_means) < 2:
            continue
        spread = float(q_means.iloc[-1] - q_means.iloc[0])
        if not np.isnan(spread):
            spreads.append(spread)

    if not spreads:
        return None
    return float(np.mean(spreads))


# ── Evaluator class ──────────────────────────────────────────────────────────


class SignalQualityEvaluator:
    """Deterministic signal-quality evaluator (``FactorEvaluator`` protocol).

    Computes real IC, RankIC, and quantile-spread metrics by:
        1. Executing the factor DSL on the price DataFrame to get a signal.
        2. Constructing forward returns from close prices per the label contract.
        3. Computing cross-sectional correlations per date and averaging.

    Parameters
    ----------
    price_data:
        Panel DataFrame with at least: ``timestamp``, ``close``.
        For meaningful cross-sectional metrics, also: ``symbol`` and OHLCV.
        Must be sorted by ``(symbol, timestamp)``.
    """

    def __init__(self, price_data: pd.DataFrame) -> None:
        self._data = price_data
        self._validate_data()

    def _validate_data(self) -> None:
        required = {"timestamp", "close"}
        missing = required - set(self._data.columns)
        if missing:
            msg = f"Price data missing required columns: {sorted(missing)}"
            raise ValueError(msg)

    def evaluate(self, factor: FactorSpec, request: EvaluationRequest) -> EvaluationBundle:
        """Run evaluation and return an EvaluationBundle.

        Steps:
            1. Filter data to the ``[eval_start, eval_end]`` date window.
            2. Execute the factor DSL to produce a signal Series.
            3. Construct forward returns from close prices.
            4. Compute IC, RankIC, quantile-spread cross-sectionally.
            5. Return an EvaluationBundle with metrics and coverage stats.
        """
        # ── 1. Filter to evaluation window ────────────────────────────
        df = self._filter_to_window(request)

        if len(df) == 0:
            return EvaluationBundle(
                n_periods=0,
                n_assets=0,
                eval_start=request.eval_start,
                eval_end=request.eval_end,
                forecast_horizon_bars=request.label.forecast_horizon_bars,
                metadata={"evaluator": "signal_quality", "mode": "real"},
            )

        # ── 2. Execute the factor DSL ─────────────────────────────────
        ast: dict[str, Any] = factor.operator_tree or parse_expression(factor.expression)
        executor = DslExecutor(df)
        signal = executor.execute(ast)

        # ── 3. Build forward returns ──────────────────────────────────
        groups = df["symbol"] if "symbol" in df.columns else None
        fwd_returns = build_forward_returns(df["close"].astype(float), groups, request.label)

        # ── 3b. Cross-sectional neutralization ────────────────────────
        timestamps = df["timestamp"]
        fwd_returns = neutralize_forward_returns(
            fwd_returns,
            timestamps=timestamps,
            symbols=groups,
            mode=request.neutralize,
            sector_map=request.sector_map,
        )

        # ── 4. Compute primary metrics ────────────────────────────────
        ic = compute_mean_ic(signal, fwd_returns, timestamps)
        rank_ic = compute_mean_rank_ic(signal, fwd_returns, timestamps)
        qs = compute_quantile_spread(signal, fwd_returns, timestamps, request.profile.n_quantiles)

        # ── 4b. Turnover + cost-adjusted quantile spread ──────────────
        turnover = compute_factor_turnover(signal, timestamps, groups)
        net_qs = apply_cost(qs, turnover, request.cost_bps)

        # ── 4b'. Risk-aware portfolio metrics (Round 4C) ──────────────
        # The same per-date long-short series that powers the spread mean
        # also feeds Sharpe / drawdown / hit-rate / tail-concentration.
        from alpha_harness.evaluators.portfolio import (
            compute_long_short_returns,
            compute_portfolio_metrics,
        )

        ls_returns = compute_long_short_returns(
            signal,
            fwd_returns,
            timestamps,
            request.profile.n_quantiles,
        )
        portfolio_metrics = compute_portfolio_metrics(ls_returns)

        # ── 4c. Auxiliary horizons (optional, for sign-consistency) ──
        ic_by_horizon: dict[str, float] = {}
        rank_ic_by_horizon: dict[str, float] = {}
        primary_h = request.label.forecast_horizon_bars
        if ic is not None:
            ic_by_horizon[str(primary_h)] = ic
        if rank_ic is not None:
            rank_ic_by_horizon[str(primary_h)] = rank_ic

        for h in request.label.extra_horizons:
            if h == primary_h:
                continue
            aux_label = LabelDefinition(
                forecast_horizon_bars=h,
                lag_bars=request.label.lag_bars,
                return_type=request.label.return_type,
            )
            aux_fwd = build_forward_returns(df["close"].astype(float), groups, aux_label)
            aux_fwd = neutralize_forward_returns(
                aux_fwd,
                timestamps=timestamps,
                symbols=groups,
                mode=request.neutralize,
                sector_map=request.sector_map,
            )
            aux_ic = compute_mean_ic(signal, aux_fwd, timestamps)
            aux_rank = compute_mean_rank_ic(signal, aux_fwd, timestamps)
            if aux_ic is not None:
                ic_by_horizon[str(h)] = aux_ic
            if aux_rank is not None:
                rank_ic_by_horizon[str(h)] = aux_rank

        # ── 5. Coverage stats ─────────────────────────────────────────
        n_periods = int(timestamps.nunique())
        n_assets = int(df["symbol"].nunique()) if "symbol" in df.columns else 1

        metadata: dict[str, Any] = {
            "evaluator": "signal_quality",
            "mode": "real",
            "neutralize": request.neutralize.value,
            "cost_bps": float(request.cost_bps),
            "portfolio": portfolio_metrics,
        }
        if len(ic_by_horizon) > 1:
            metadata["ic_by_horizon"] = ic_by_horizon
            metadata["rank_ic_by_horizon"] = rank_ic_by_horizon
            # Sign-consistency: count horizons where IC sign matches the
            # primary horizon.  Judges can consume this without re-reading
            # the raw dict.
            primary_ic = ic_by_horizon.get(str(primary_h))
            if primary_ic is not None:
                same_sign = sum(1 for v in ic_by_horizon.values() if (v > 0) == (primary_ic > 0))
                metadata["ic_sign_consistent_horizons"] = int(same_sign)

        sharpe_val = portfolio_metrics.get("sharpe")
        sharpe = float(sharpe_val) if isinstance(sharpe_val, int | float) else None

        return EvaluationBundle(
            ic=ic,
            rank_ic=rank_ic,
            quantile_spread=qs,
            turnover=turnover,
            net_quantile_spread=net_qs,
            sharpe=sharpe,
            n_periods=n_periods,
            n_assets=n_assets,
            eval_start=request.eval_start,
            eval_end=request.eval_end,
            forecast_horizon_bars=request.label.forecast_horizon_bars,
            metadata=metadata,
        )

    def _filter_to_window(self, request: EvaluationRequest) -> pd.DataFrame:
        """Filter data to the ``[eval_start, eval_end]`` date range."""
        df = self._data.copy()
        ts_dates = pd.to_datetime(df["timestamp"]).dt.date
        mask = (ts_dates >= request.eval_start) & (ts_dates <= request.eval_end)
        filtered: pd.DataFrame = df.loc[mask].reset_index(drop=True)
        return filtered
