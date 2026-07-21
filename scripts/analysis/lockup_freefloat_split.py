#!/usr/bin/env python3
"""Free-float-conditioned cornerstone lockup event study (exploratory).

Pre-registered in docs/PREREGISTRATION_FREEFLOAT_LOCKUP_2026Q3.md.  This
script runs ONLY the exploratory (<= 2026-07-17) read, which is
contaminated (splitting a known-null aggregate by a new variable) and
cannot promote anything.  The clean confirmation is a one-shot
median-split test on cornerstone expiries occurring after 2026-07-17,
run later once those events have tick coverage.

Reuses the event-study primitives from ``lockup_event_study`` so the AR
/ OFI / CAR construction is byte-identical to the aggregate study.
"""

from __future__ import annotations

import argparse

import pandas as pd

from scripts.analysis.lockup_event_study import (
    CAR_HI,
    CAR_LO,
    DEFAULT_DATASET,
    DEFAULT_PROJECT,
    _drop_implausible_events,
    _event_panel,
    _per_event_car,
    _pull,
    _sign_test,
    _t_stat,
)

EXPLORATORY_END = "2026-07-17"


def _free_float(project: str, dataset: str) -> pd.DataFrame:
    """One free_float_pct per stock (median over its panel history)."""
    from google.cloud import bigquery

    bq = bigquery.Client(project=project)
    return bq.query(
        f"""
        SELECT stock_code,
               APPROX_QUANTILES(free_float_pct, 2)[OFFSET(1)] AS free_float_pct
        FROM `{project}.{dataset}.ipo_daily_prices`
        WHERE free_float_pct IS NOT NULL
        GROUP BY stock_code
        """,
        job_config=bigquery.QueryJobConfig(maximum_bytes_billed=200_000_000),
    ).to_dataframe()


def _bucket_report(label: str, cars: pd.Series) -> None:
    mean, t, n = _t_stat(cars)
    pos, n_sign, p = _sign_test(cars)
    print(
        f"  {label:<12} CAR[{CAR_LO},{CAR_HI}] = {mean:+.3f}% "
        f"(t={t:+.2f}, N={n}); median={cars.median():+.3f}%, "
        f"sign {pos}/{n_sign} pos (p={p:.2f})",
    )


def run(project: str, dataset: str) -> None:
    events, daily, hsi = _pull(project, dataset, "cornerstone_lockup_expiry")
    events = _drop_implausible_events(events, "cornerstone_lockup_expiry")
    # Exploratory window only.
    events = events[pd.to_datetime(events["event_date"]) <= EXPLORATORY_END]
    panel = _event_panel(events, daily, hsi)
    if panel.empty:
        print("no usable events")
        return

    pe = _per_event_car(panel)  # one row per (stock, event_date)
    ff = _free_float(project, dataset)
    pe = pe.merge(ff, on="stock_code", how="left")
    pe = pe.dropna(subset=["free_float_pct"])

    median_ff = float(pe["free_float_pct"].median())
    pe["bucket"] = pe["free_float_pct"].apply(
        lambda x: "low_float" if x <= median_ff else "high_float",
    )
    low = pe[pe["bucket"] == "low_float"]
    high = pe[pe["bucket"] == "high_float"]

    print("EXPLORATORY (<= 2026-07-17, CONTAMINATED — cannot promote)")
    print(f"events with free float: {len(pe)}; median free_float_pct = {median_ff:.1f}%")
    print(f"buckets: low_float N={len(low)}, high_float N={len(high)}\n")

    print("Per-bucket event-window CAR:")
    _bucket_report("low_float", low["car"])
    _bucket_report("high_float", high["car"])

    # low - high interaction (Welch two-sample on per-event CARs)
    lm, _, ln = _t_stat(low["car"])
    hm, _, hn = _t_stat(high["car"])
    ls = low["car"].std(ddof=1)
    hs = high["car"].std(ddof=1)
    se = (ls**2 / ln + hs**2 / hn) ** 0.5 if ln and hn else float("nan")
    diff = lm - hm
    t_int = diff / se if se else float("nan")
    print(f"\n  low-high interaction: {diff:+.3f}% (t={t_int:+.2f})")

    # event-window OFI (tau 0..+3) per bucket
    bmap = dict(
        zip(
            pe["stock_code"] + "|" + pe["event_date"].astype(str),
            pe["bucket"],
            strict=False,
        )
    )
    panel = panel.assign(
        key=panel["stock_code"] + "|" + panel["event_date"].astype(str),
    )
    panel["bucket"] = panel["key"].map(bmap)
    post = panel[(panel["tau"] >= 0) & (panel["tau"] <= CAR_HI)]
    print("\n  post-event OFI (tau 0..+3):")
    for b in ("low_float", "high_float"):
        vals = post[post["bucket"] == b]["ofi"].dropna()
        m, t, n = _t_stat(vals)
        print(f"    {b:<12} mean OFI = {m:+.4f} (t={t:+.2f}, N={n})")

    print(
        "\nNOTE: exploratory only. The registered decision rule is evaluated "
        "on cornerstone expiries AFTER 2026-07-17 (see the pre-registration).",
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--project", default=DEFAULT_PROJECT)
    p.add_argument("--dataset", default=DEFAULT_DATASET)
    args = p.parse_args(argv)
    run(args.project, args.dataset)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
