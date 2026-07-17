#!/usr/bin/env python3
"""HK IPO event study using curated HKEX/prospectus event dates.

Examples:

    python -m scripts.analysis.lockup_event_study --event-type cornerstone_lockup_expiry
    python -m scripts.analysis.lockup_event_study --event-type greenshoe_expiry
"""

from __future__ import annotations

import argparse
import math

import pandas as pd

from alpha_harness.data.loader_factory import resolve_market_data_location
from alpha_harness.markets import load_market_pack

WIN = 10
CAR_LO = -1
CAR_HI = 3
PLACEBO_SHIFT = 40
DEFAULT_PROJECT, DEFAULT_DATASET = resolve_market_data_location(load_market_pack("hk_ipo"))

# Curated extraction errors (e.g. a greenshoe "expiry" dated before listing)
# otherwise snap onto the IPO day-1 pop at tau=0 and dominate the mean AR.
# Stabilization end / greenshoe expiry sit ~30 days after listing under the
# HK price-stabilizing rules, so anything under 20 days is treated as bad.
MIN_DAYS_FROM_LISTING = {
    "greenshoe_expiry": 20,
    "stabilization_end": 20,
}


def _client(project: str):
    from google.cloud import bigquery

    return bigquery.Client(project=project)


def _pull(
    project: str,
    dataset: str,
    event_type: str,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    from google.cloud import bigquery

    bq = _client(project)
    events = bq.query(
        f"""
        SELECT
          stock_code,
          event_type,
          event_date,
          listing_date,
          primary_source_doc_id AS source_doc_id,
          primary_source_url AS source_url
        FROM `{project}.{dataset}.ipo_event_dates_curated`
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
        f"""
        SELECT
          p.stock_code,
          p.date,
          p.chg_pct_1d,
          mf.ofi,
          ef.next_cornerstone_unlock_pct_offer AS overhang
        FROM `{project}.{dataset}.ipo_daily_prices` p
        LEFT JOIN `{project}.{dataset}.micro_features_daily` mf
          ON p.stock_code = mf.stock_code AND p.date = mf.trading_date
        LEFT JOIN `{project}.{dataset}.ipo_event_features_daily` ef
          ON p.stock_code = ef.stock_code AND p.date = ef.date
        """,
        job_config=bigquery.QueryJobConfig(maximum_bytes_billed=400_000_000),
    ).to_dataframe()

    hsi = bq.query(
        f"""
        SELECT date, chg_pct_1d AS hsi_ret
        FROM `{project}.{dataset}.market_factors_daily`
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


def _drop_implausible_events(events: pd.DataFrame, event_type: str) -> pd.DataFrame:
    if events.empty or "listing_date" not in events.columns:
        return events
    min_days = MIN_DAYS_FROM_LISTING.get(event_type, 0)
    days = (pd.to_datetime(events["event_date"]) - pd.to_datetime(events["listing_date"])).dt.days
    ok = days.isna() | (days >= min_days)
    for _, row in events[~ok].iterrows():
        print(
            f"dropping implausible event date: {row['stock_code']} "
            f"{row['event_date']} (listed {row['listing_date']})",
        )
    return events[ok].reset_index(drop=True)


def _sign_test(x: pd.Series) -> tuple[int, int, float]:
    """Two-sided binomial sign test — robust to the fat right tail of IPO returns."""
    # Exact zeroes are ties, not negative outcomes.
    x = x.dropna()
    x = x[x != 0]
    n = len(x)
    if n == 0:
        return 0, 0, float("nan")
    pos = int((x > 0).sum())
    tail = min(pos, n - pos)
    p = min(1.0, 2 * sum(math.comb(n, i) for i in range(tail + 1)) / 2**n)
    return pos, n, p


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


def _placebo_cars(
    events: pd.DataFrame,
    daily: pd.DataFrame,
    hsi: pd.DataFrame,
    shift: int = -PLACEBO_SHIFT,
) -> pd.Series:
    """CAR at a non-event date shifted `shift` trading days from each event.

    Early-life events (stabilization end / greenshoe expiry sit ~30 days
    after listing) have no pre-event history for a negative shift, so
    callers should also run the positive-shift control.
    """
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
        idx0 = int(event_index[0]) + shift
        if idx0 + CAR_LO < 0 or idx0 + CAR_HI >= len(g):
            continue
        w = g.assign(tau=g.index - idx0)
        w = w[(w["tau"] >= CAR_LO) & (w["tau"] <= CAR_HI)]
        if len(w):
            cars.append(float(w["AR"].sum()))
    return pd.Series(cars, dtype="float64")


def run(project: str, dataset: str, event_type: str) -> None:
    events, daily, hsi = _pull(project, dataset, event_type)
    print(f"event_type: {event_type}")
    print(f"curated event dates: {len(events)}")
    events = _drop_implausible_events(events, event_type)

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
    pos, n_sign, p_sign = _sign_test(pe["car"])
    print(
        f"\nH1 CAR[{CAR_LO},{CAR_HI}] = {mean_car:+.3f}% (t={t_car:+.2f}, N={n_car})",
    )
    print(
        f"H1 robust: median = {pe['car'].median():+.3f}%, "
        f"sign test {pos}/{n_sign} positive (p={p_sign:.2f})",
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

    for label, shift in (
        (f"tau0-{PLACEBO_SHIFT}d", -PLACEBO_SHIFT),
        (f"tau0+{PLACEBO_SHIFT}d", PLACEBO_SHIFT),
    ):
        placebo = _placebo_cars(events, daily, hsi, shift=shift)
        mean_p, t_p, n_p = _t_stat(placebo)
        median_p = placebo.median() if n_p else float("nan")
        print(
            f"placebo CAR[{CAR_LO},{CAR_HI}] at {label} = "
            f"{mean_p:+.3f}% (t={t_p:+.2f}, N={n_p}, median={median_p:+.3f}%)",
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project",
        default=DEFAULT_PROJECT,
    )
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--event-type", default="cornerstone_lockup_expiry")
    args = parser.parse_args(argv)
    run(args.project, args.dataset, args.event_type)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
