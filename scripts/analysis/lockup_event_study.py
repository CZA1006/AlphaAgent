#!/usr/bin/env python3
"""HK IPO event study using curated HKEX/prospectus event dates.

Examples:

    python -m scripts.analysis.lockup_event_study --event-type cornerstone_lockup_expiry
    python -m scripts.analysis.lockup_event_study --event-type greenshoe_expiry
"""

from __future__ import annotations

import argparse
import math
import os

import pandas as pd

WIN = 10
CAR_LO = -1
CAR_HI = 3
PLACEBO_SHIFT = 40


def _client(project: str):
    from google.cloud import bigquery

    return bigquery.Client(project=project)


def _pull(project: str, event_type: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    from google.cloud import bigquery

    bq = _client(project)
    events = bq.query(
        """
        SELECT
          stock_code,
          event_type,
          event_date,
          listing_date,
          primary_source_doc_id AS source_doc_id,
          primary_source_url AS source_url
        FROM `bloomberg-database-0629.hk_ipo_research.ipo_event_dates_curated`
        WHERE event_type = @event_type
          AND event_date IS NOT NULL
        ORDER BY stock_code, event_date
        """,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("event_type", "STRING", event_type),
            ],
            maximum_bytes_billed=200_000_000,
        ),
    ).to_dataframe()

    daily = bq.query(
        """
        SELECT
          p.stock_code,
          p.date,
          p.chg_pct_1d,
          mf.ofi,
          ef.next_cornerstone_unlock_pct_offer AS overhang
        FROM `bloomberg-database-0629.hk_ipo_research.ipo_daily_prices` p
        LEFT JOIN `bloomberg-database-0629.hk_ipo_research.micro_features_daily` mf
          ON p.stock_code = mf.stock_code AND p.date = mf.trading_date
        LEFT JOIN `bloomberg-database-0629.hk_ipo_research.ipo_event_features_daily` ef
          ON p.stock_code = ef.stock_code AND p.date = ef.date
        """,
        job_config=bigquery.QueryJobConfig(maximum_bytes_billed=400_000_000),
    ).to_dataframe()

    hsi = bq.query(
        """
        SELECT date, chg_pct_1d AS hsi_ret
        FROM `bloomberg-database-0629.hk_ipo_research.market_factors_daily`
        WHERE factor_name = 'hang_seng_index'
        """,
        job_config=bigquery.QueryJobConfig(maximum_bytes_billed=100_000_000),
    ).to_dataframe()
    return events, daily, hsi


def _event_panel(events: pd.DataFrame, daily: pd.DataFrame, hsi: pd.DataFrame) -> pd.DataFrame:
    daily = daily.merge(hsi, on="date", how="left")
    daily["AR"] = daily["chg_pct_1d"] - daily["hsi_ret"]
    daily["date"] = pd.to_datetime(daily["date"])

    rows: list[pd.DataFrame] = []
    for _, ev in events.iterrows():
        event_date = pd.Timestamp(ev["event_date"])
        stock = ev["stock_code"]
        g = daily[daily["stock_code"] == stock].sort_values("date").reset_index(drop=True)
        if g.empty:
            continue
        event_index = g.index[g["date"] >= event_date]
        if len(event_index) == 0:
            continue
        idx0 = int(event_index[0])
        window = g.assign(
            tau=g.index - idx0,
            event_date=event_date.date(),
            source_doc_id=ev.get("source_doc_id"),
            source_url=ev.get("source_url"),
        )
        window = window[(window["tau"] >= -WIN) & (window["tau"] <= WIN)]
        if len(window):
            rows.append(
                window[
                    [
                        "stock_code",
                        "date",
                        "event_date",
                        "tau",
                        "AR",
                        "ofi",
                        "overhang",
                        "source_doc_id",
                        "source_url",
                    ]
                ],
            )
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def _t_stat(x: pd.Series) -> tuple[float, float, int]:
    x = x.dropna()
    n = len(x)
    if n < 2:
        return float("nan"), float("nan"), n
    mean = float(x.mean())
    se = float(x.std(ddof=1) / math.sqrt(n))
    return mean, mean / se if se else float("nan"), n


def _per_event_car(panel: pd.DataFrame) -> pd.DataFrame:
    w = panel[(panel["tau"] >= CAR_LO) & (panel["tau"] <= CAR_HI)]
    return (
        w.groupby(["stock_code", "event_date"])
        .agg(car=("AR", "sum"), overhang=("overhang", "first"))
        .reset_index()
    )


def _placebo_cars(events: pd.DataFrame, daily: pd.DataFrame, hsi: pd.DataFrame) -> pd.Series:
    daily = daily.merge(hsi, on="date", how="left")
    daily["AR"] = daily["chg_pct_1d"] - daily["hsi_ret"]
    daily["date"] = pd.to_datetime(daily["date"])
    cars: list[float] = []
    for _, ev in events.iterrows():
        g = (
            daily[daily["stock_code"] == ev["stock_code"]]
            .sort_values("date")
            .reset_index(drop=True)
        )
        if g.empty:
            continue
        event_index = g.index[g["date"] >= pd.Timestamp(ev["event_date"])]
        if len(event_index) == 0:
            continue
        idx0 = int(event_index[0]) - PLACEBO_SHIFT
        if idx0 < WIN:
            continue
        w = g.assign(tau=g.index - idx0)
        w = w[(w["tau"] >= CAR_LO) & (w["tau"] <= CAR_HI)]
        if len(w):
            cars.append(float(w["AR"].sum()))
    return pd.Series(cars, dtype="float64")


def run(project: str, event_type: str) -> None:
    events, daily, hsi = _pull(project, event_type)
    print(f"event_type: {event_type}")
    print(f"curated event dates: {len(events)}")

    panel = _event_panel(events, daily, hsi)
    if panel.empty:
        print("no usable daily/tick coverage in event window")
        return
    n_events = panel[["stock_code", "event_date"]].drop_duplicates().shape[0]
    n_stocks = panel["stock_code"].nunique()
    print(f"usable events in window: {n_events} / stocks: {n_stocks}\n")

    prof = (
        panel.groupby("tau")
        .agg(mean_AR=("AR", "mean"), mean_OFI=("ofi", "mean"), n=("AR", "count"))
        .reset_index()
        .sort_values("tau")
    )
    prof["CAR"] = prof["mean_AR"].cumsum()
    print("tau  meanAR%   CAR%    meanOFI   n")
    for _, row in prof.iterrows():
        ofi = f"{row['mean_OFI']:+.4f}" if pd.notna(row["mean_OFI"]) else "  n/a"
        print(
            f"{int(row['tau']):+3d}  {row['mean_AR']:+6.3f}  "
            f"{row['CAR']:+6.3f}  {ofi:>8}  {int(row['n']):3d}",
        )

    pe = _per_event_car(panel)
    mean_car, t_car, n_car = _t_stat(pe["car"])
    print(
        f"\nH1 CAR[{CAR_LO},{CAR_HI}] = {mean_car:+.3f}% "
        f"(t={t_car:+.2f}, N={n_car})",
    )

    with_overhang = pe.dropna(subset=["overhang"])
    if len(with_overhang) >= 3:
        corr = with_overhang["car"].corr(with_overhang["overhang"])
        print(f"H2 corr(CAR, cornerstone overhang) = {corr:+.2f} (N={len(with_overhang)})")
    else:
        print("H2 overhang scaling: too few events with overhang data")

    pre = panel[(panel["tau"] >= -5) & (panel["tau"] <= -1)]["ofi"]
    mean_ofi, t_ofi, n_ofi = _t_stat(pre)
    print(f"H3 pre-event OFI[-5,-1] = {mean_ofi:+.4f} (t={t_ofi:+.2f}, N={n_ofi})")

    placebo = _placebo_cars(events, daily, hsi)
    mean_p, t_p, n_p = _t_stat(placebo)
    print(
        f"placebo CAR[{CAR_LO},{CAR_HI}] at tau0-{PLACEBO_SHIFT}d = "
        f"{mean_p:+.3f}% (t={t_p:+.2f}, N={n_p})",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project",
        default=os.environ.get("GCP_PROJECT", "bloomberg-database-0629"),
    )
    parser.add_argument("--event-type", default="cornerstone_lockup_expiry")
    args = parser.parse_args(argv)
    run(args.project, args.event_type)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
