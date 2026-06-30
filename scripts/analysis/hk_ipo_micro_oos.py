#!/usr/bin/env python3
"""HK IPO microstructure: disjoint OOS + cost-realism + long-only analysis.

Reproduces the analysis behind ``docs/CASE_STUDY_HK_IPO_MICRO.md``.  For a
list of candidate DSL factor expressions, it reports — separately on a
TRAIN and a held-out TEST window — three things the strict-regime gates
alone don't surface:

1. **Per-factor persistence** — rank-IC sign agreement train → test.
2. **Cost realism** — break-even cost vs the *measured* IPO half-spread
   (``rel_spread`` / 2), not the harness's optimistic flat 5 bps.
3. **Long-only, market-hedged net excess** — long the top-quintile
   basket, hedge with short HSI futures (IPOs are hard to short during
   lockup), net of the real round-trip IPO spread.

Reads the HK IPO panel via the BigQuery loader (microstructure features
joined in) and HSI from ``market_factors_daily``.  Needs ADC auth
(``gcloud auth application-default login``) and the ``gcp`` extra.

Usage::

    GOOGLE_CLOUD_PROJECT=bloomberg-database-0629 \\
    uv run python -m scripts.analysis.hk_ipo_micro_oos \\
        --factors-file factors.txt \\
        --train 2025-12-12:2026-04-30 --test 2026-05-01:2026-06-26
"""

from __future__ import annotations

import argparse
from datetime import date

import numpy as np
import pandas as pd

from alpha_harness.combination import compute_signal
from alpha_harness.data.loader_factory import create_equities_loader
from alpha_harness.data.models import BarFrequency, DataRequest

LAG, HORIZON, N_QUANTILES = 1, 5, 5


def _win(s: str) -> tuple[date, date]:
    a, b = s.split(":")
    return date.fromisoformat(a), date.fromisoformat(b)


def _load_universe(path: str) -> list[str]:
    return [
        ln.strip()
        for ln in open(path, encoding="utf-8")
        if ln.strip() and not ln.startswith("#")
    ]


def _hsi_forward(project: str) -> dict[date, float]:
    """HSI forward return per trading date, keyed by plain date (tz-robust)."""
    from google.cloud import bigquery

    bq = bigquery.Client(project=project)
    df = bq.query(
        "SELECT date, px_last FROM hk_ipo_research.market_factors_daily "
        "WHERE factor_name='hang_seng_index' ORDER BY date",
    ).to_dataframe()
    s = df.assign(date=pd.to_datetime(df["date"])).set_index("date")["px_last"].sort_index()
    fwd = s.shift(-(LAG + HORIZON)) / s.shift(-LAG) - 1
    return {ts.date(): v for ts, v in fwd.items()}


def _forward_returns(df: pd.DataFrame) -> pd.Series:
    g = df.groupby("symbol")["close"]
    return g.shift(-(LAG + HORIZON)) / g.shift(-LAG) - 1


def per_factor_oos(
    train: pd.DataFrame,
    test: pd.DataFrame,
    hsi_fwd: dict[date, float],
    exprs: list[str],
    half_spread_bps: float | None,
) -> None:
    """Print persistence + cost + long-only-hedged net excess, train vs test."""
    if half_spread_bps is None:
        half_spread_bps = float(np.nanmean(test["rel_spread"])) * 1e4 / 2
    full_spread = 2 * half_spread_bps * 1e-4  # round-trip on changed names
    print(f"measured half-spread = {half_spread_bps:.1f} bps "
          f"(round-trip cost {full_spread * 1e4:.0f} bps)\n")

    for name, df in [("TRAIN", train), ("TEST", test)]:
        df = df.sort_values(["timestamp", "symbol"]).reset_index(drop=True)
        df = df.assign(fwd=_forward_returns(df))
        df["dd"] = pd.to_datetime(df["timestamp"]).dt.date
        ts = df["timestamp"]
        print(f"===== {name} =====")
        print(f"{'factor':<52}{'rank_ic':>8}{'longNet5d':>10}{'hit%':>6}")
        for e in exprs:
            sig = compute_signal(e, df)
            ric = _rank_ic(sig, df["fwd"], ts)
            net5d, hit = _long_only(df, sig, hsi_fwd, full_spread)
            ric_s = f"{ric:+.4f}" if ric is not None else "   n/a"
            net_s = f"{net5d:+.4f}" if net5d is not None else "    n/a"
            hit_s = f"{hit:.0f}" if hit is not None else " n/a"
            print(f"{e[:52]:<52}{ric_s:>8}{net_s:>10}{hit_s:>6}")
        print()


def _rank_ic(sig: pd.Series, fwd: pd.Series, ts: pd.Series) -> float | None:
    from alpha_harness.evaluators.signal_quality import compute_mean_rank_ic

    return compute_mean_rank_ic(sig, fwd, ts)


def _long_only(
    df: pd.DataFrame,
    sig: pd.Series,
    hsi_fwd: dict[date, float],
    full_spread: float,
) -> tuple[float | None, float | None]:
    work = df.assign(sig=sig)
    rows: list[float] = []
    turns: list[float] = []
    prev: set[str] = set()
    for d, grp in work.groupby("dd"):
        gg = grp.dropna(subset=["sig", "fwd"])
        if len(gg) < 10:
            continue
        top = gg[gg["sig"] >= gg["sig"].quantile(1 - 1 / N_QUANTILES)]
        hv = hsi_fwd.get(d, np.nan)
        if len(top) == 0 or pd.isna(hv):
            continue
        names = set(top["symbol"])
        turns.append(len(names - prev) / max(len(names), 1) if prev else 0.0)
        prev = names
        rows.append(float(top["fwd"].mean() - hv))
    if len(rows) < 3:
        return None, None
    ex = pd.Series(rows)
    net = ex.mean() - (np.mean(turns) if turns else 0.0) * full_spread
    return net, float((ex > 0).mean() * 100)


def main(argv: list[str] | None = None) -> int:
    import os

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--factors-file", required=True)
    p.add_argument("--universe", default="configs/universes/hk_ipo.txt")
    p.add_argument("--train", default="2025-12-12:2026-04-30")
    p.add_argument("--test", default="2026-05-01:2026-06-26")
    p.add_argument(
        "--half-spread-bps",
        type=float,
        default=None,
        help="Override; default = measured mean rel_spread/2 on the test window.",
    )
    p.add_argument("--project", default=os.environ.get("GCP_PROJECT", "bloomberg-database-0629"))
    args = p.parse_args(argv)

    syms = _load_universe(args.universe)
    exprs = _load_universe(args.factors_file)
    loader = create_equities_loader(source="bigquery")

    def panel(win: str) -> pd.DataFrame:
        s, e = _win(win)
        df, _ = loader.load_bars(
            DataRequest(symbols=syms, start=s, end=e, frequency=BarFrequency.DAILY),
        )
        return df

    hsi_fwd = _hsi_forward(args.project)
    per_factor_oos(
        panel(args.train), panel(args.test), hsi_fwd, exprs, args.half_spread_bps,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
