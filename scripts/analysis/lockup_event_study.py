#!/usr/bin/env python3
"""HK IPO 6-month lockup-expiry event study (tick order flow).

MVP for the design in ``docs/DESIGN_LOCKUP_EVENT_STUDY.md``.  Aligns each
IPO by event time τ = (trading_date − lockup_expiry) in trading days and
averages across events to test:

  H1  selling pressure at expiry — CAR[−1,+3] < 0 and net-sell ofi
  H2  overhang scaling — per-event CAR vs cornerstone unlock %
  H3  pre-positioning — mean ofi < 0 for τ < 0

plus a **placebo** (same statistic at a non-event date) to confirm the
effect is specific to expiry.  Standalone — no harness change; reuses
the BigQuery data + the already-built ``micro_features_daily``.

Honest by construction: only ~19 IPOs have their expiry inside the tick
window, so this detects a strong average effect only.  Every output
prints N.

Usage::

    GOOGLE_CLOUD_PROJECT=bloomberg-database-0629 \\
    uv run python -m scripts.analysis.lockup_event_study
"""

from __future__ import annotations

import argparse
import math

import numpy as np
import pandas as pd

WIN = 10            # event window τ ∈ [−WIN, +WIN]
CAR_LO, CAR_HI = -1, 3   # CAR accumulation window for H1/H2
PLACEBO_SHIFT = 40       # trading days before expiry → non-event date


def _client(project: str):
    from google.cloud import bigquery

    return bigquery.Client(project=project)


def _pull(project: str, tick_lo: str, tick_hi: str):
    bq = _client(project)
    events = bq.query(
        "SELECT m.stock_code, "
        "DATE_ADD(m.listing_date, INTERVAL 6 MONTH) AS expiry, "
        "a.cornerstone_pct_of_offer_total AS overhang "
        "FROM hk_ipo_research.ipo_master m "
        "LEFT JOIN hk_ipo_research.hkex_ipo_allotment_summary a USING (stock_code) "
        f"WHERE DATE_ADD(m.listing_date, INTERVAL 6 MONTH) "
        f"BETWEEN DATE '{tick_lo}' AND DATE '{tick_hi}'",
    ).to_dataframe()
    codes = events["stock_code"].tolist()
    daily = bq.query(
        "SELECT p.stock_code, p.date, p.chg_pct_1d, mf.ofi "
        "FROM hk_ipo_research.ipo_daily_prices p "
        "LEFT JOIN hk_ipo_research.micro_features_daily mf "
        "  ON p.stock_code = mf.stock_code AND p.date = mf.trading_date "
        f"WHERE p.stock_code IN UNNEST({codes})",
    ).to_dataframe()
    hsi = bq.query(
        "SELECT date, chg_pct_1d AS hsi_ret "
        "FROM hk_ipo_research.market_factors_daily "
        "WHERE factor_name='hang_seng_index'",
    ).to_dataframe()
    return events, daily, hsi


def _event_panel(events, daily, hsi, expiry_col="expiry"):
    """Long panel: one row per (stock, τ) with abnormal return + ofi."""
    daily = daily.merge(hsi, on="date", how="left")
    daily["AR"] = daily["chg_pct_1d"] - daily["hsi_ret"]   # abnormal return, %
    daily["date"] = pd.to_datetime(daily["date"])
    rows = []
    for _, ev in events.iterrows():
        exp = pd.Timestamp(ev[expiry_col])
        g = daily[daily["stock_code"] == ev["stock_code"]].sort_values("date")
        if g.empty:
            continue
        g = g.reset_index(drop=True)
        after = g.index[g["date"] >= exp]
        if len(after) == 0:
            continue
        idx0 = int(after[0])
        g = g.assign(tau=g.index - idx0)
        g = g[(g["tau"] >= -WIN) & (g["tau"] <= WIN)]
        g = g.assign(stock_code=ev["stock_code"], overhang=ev["overhang"])
        rows.append(g[["stock_code", "tau", "AR", "ofi", "overhang"]])
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def _t_stat(x: pd.Series) -> tuple[float, float, int]:
    x = x.dropna()
    n = len(x)
    if n < 2:
        return float("nan"), float("nan"), n
    m = x.mean()
    se = x.std(ddof=1) / math.sqrt(n)
    return m, (m / se if se else float("nan")), n


def _per_event_car(panel: pd.DataFrame) -> pd.DataFrame:
    w = panel[(panel["tau"] >= CAR_LO) & (panel["tau"] <= CAR_HI)]
    return w.groupby("stock_code").agg(
        car=("AR", "sum"), overhang=("overhang", "first"),
    ).reset_index()


def run(project: str, tick_lo: str, tick_hi: str) -> None:
    events, daily, hsi = _pull(project, tick_lo, tick_hi)
    print(f"events with expiry in [{tick_lo}, {tick_hi}]: {len(events)}")
    panel = _event_panel(events, daily, hsi)
    n_ev = panel["stock_code"].nunique()
    print(f"events with usable tick/daily coverage in window: {n_ev}\n")

    # Event-time profile
    prof = panel.groupby("tau").agg(
        mean_AR=("AR", "mean"), mean_OFI=("ofi", "mean"), n=("AR", "count"),
    ).reset_index().sort_values("tau")
    prof["CAR"] = prof["mean_AR"].cumsum()
    print("τ   meanAR%   CAR%    meanOFI   n")
    for _, r in prof.iterrows():
        ofi = f"{r['mean_OFI']:+.4f}" if pd.notna(r["mean_OFI"]) else "  n/a"
        print(f"{int(r['tau']):+3d}  {r['mean_AR']:+6.3f}  {r['CAR']:+6.3f}  {ofi:>8}  {int(r['n']):3d}")

    # H1: CAR[-1,+3] across events
    pe = _per_event_car(panel)
    m, t, n = _t_stat(pe["car"])
    print(f"\nH1 selling pressure: mean CAR[{CAR_LO},{CAR_HI}] = {m:+.3f}%  "
          f"(t={t:+.2f}, N={n})  -> {'supports' if (not math.isnan(t) and t < -1.0) else 'inconclusive'}")

    # H2: CAR ~ overhang
    pe2 = pe.dropna(subset=["overhang"])
    if len(pe2) >= 3:
        corr = pe2["car"].corr(pe2["overhang"])
        print(f"H2 overhang scaling: corr(CAR, cornerstone%) = {corr:+.2f}  (N={len(pe2)})  "
              f"-> {'supports (more overhang, more selling)' if corr < -0.1 else 'inconclusive'}")
    else:
        print("H2 overhang scaling: too few events with overhang data")

    # H3: pre-event ofi
    pre = panel[(panel["tau"] >= -5) & (panel["tau"] <= -1)]["ofi"]
    m3, t3, n3 = _t_stat(pre)
    print(f"H3 pre-positioning: mean ofi(τ∈[-5,-1]) = {m3:+.4f}  (t={t3:+.2f}, N={n3})  "
          f"-> {'supports (net selling before expiry)' if (not math.isnan(t3) and m3 < 0) else 'inconclusive'}")

    # Placebo: same CAR statistic at a non-event date (expiry − PLACEBO_SHIFT trading days)
    pl_events = events.copy()
    # shift each event's τ=0 back by PLACEBO_SHIFT trading days via a synthetic 'expiry'
    daily_s = daily.merge(hsi, on="date", how="left")
    daily_s["AR"] = daily_s["chg_pct_1d"] - daily_s["hsi_ret"]
    daily_s["date"] = pd.to_datetime(daily_s["date"])
    rows = []
    for _, ev in pl_events.iterrows():
        g = daily_s[daily_s["stock_code"] == ev["stock_code"]].sort_values("date").reset_index(drop=True)
        exp = pd.Timestamp(ev["expiry"])
        after = g.index[g["date"] >= exp]
        if len(after) == 0:
            continue
        idx0 = int(after[0]) - PLACEBO_SHIFT
        if idx0 < WIN:
            continue
        g = g.assign(tau=g.index - idx0)
        w = g[(g["tau"] >= CAR_LO) & (g["tau"] <= CAR_HI)]
        if len(w):
            rows.append(w["AR"].sum())
    if rows:
        mp, tp, npl = _t_stat(pd.Series(rows))
        print(f"\nplacebo CAR[{CAR_LO},{CAR_HI}] at τ0−{PLACEBO_SHIFT}d = {mp:+.3f}%  (t={tp:+.2f}, N={npl})  "
              f"(should be ~0 / insignificant if the effect is expiry-specific)")


def main(argv: list[str] | None = None) -> int:
    import os

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--project", default=os.environ.get("GCP_PROJECT", "bloomberg-database-0629"))
    p.add_argument("--tick-lo", default="2025-12-12")
    p.add_argument("--tick-hi", default="2026-06-26")
    args = p.parse_args(argv)
    run(args.project, args.tick_lo, args.tick_hi)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
