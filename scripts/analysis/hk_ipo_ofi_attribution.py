#!/usr/bin/env python3
"""Attribute HK IPO OFI tail concentration without changing promotion rules."""

from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from alpha_harness.combination import compute_signal
from alpha_harness.data.loader_factory import create_equities_loader
from alpha_harness.data.models import BarFrequency, DataRequest
from alpha_harness.evaluators.neutralize import neutralize_forward_returns
from alpha_harness.evaluators.portfolio import (
    compute_long_short_returns,
    compute_portfolio_metrics,
)
from alpha_harness.evaluators.signal_quality import build_forward_returns
from alpha_harness.evaluators.walk_forward import fold_windows
from alpha_harness.regimes import get_regime

EXPRESSIONS = ("rank(ts_mean(ofi, 10))", "rank(ts_mean(ofi, 20))")
EVENT_DISTANCE_COLUMNS = (
    "days_to_next_cornerstone_lockup",
    "days_since_prev_cornerstone_lockup",
    "days_to_next_greenshoe_expiry",
    "days_since_prev_greenshoe_expiry",
    "days_to_next_greenshoe_exercise",
    "days_since_prev_greenshoe_exercise",
    "days_to_next_stabilization_end",
    "days_since_prev_stabilization_end",
)


def _listing_age_bucket(value: Any) -> str:
    if pd.isna(value):
        return "unknown"
    age = float(value)
    if age <= 30:
        return "0_30"
    if age <= 90:
        return "31_90"
    return "91_plus"


def _event_proximity_bucket(row: pd.Series) -> str:
    distances = [
        abs(float(row[column]))
        for column in EVENT_DISTANCE_COLUMNS
        if column in row.index and not pd.isna(row[column])
    ]
    if not distances:
        return "no_event"
    nearest = min(distances)
    if nearest <= 5:
        return "0_5"
    if nearest <= 30:
        return "6_30"
    return "31_plus"


def _metrics(
    signal: pd.Series,
    returns: pd.Series,
    timestamps: pd.Series,
    *,
    overlap_horizon_bars: int = 5,
) -> tuple[pd.Series, dict[str, float | None]]:
    stream = compute_long_short_returns(signal, returns, timestamps)
    return stream, compute_portfolio_metrics(
        stream,
        overlap_horizon_bars=overlap_horizon_bars,
    )


def _top_dates(stream: pd.Series, limit: int = 3) -> list[dict[str, object]]:
    top = stream.sort_values(ascending=False).head(limit)
    output: list[dict[str, object]] = []
    for timestamp, value in top.items():
        timestamp_value: Any = timestamp
        output.append(
            {
                "date": str(pd.Timestamp(timestamp_value).date()),
                "return": float(value),
            },
        )
    return output


def _group_attribution(
    df: pd.DataFrame,
    signal: pd.Series,
    returns: pd.Series,
    column: str,
) -> dict[str, dict[str, object]]:
    output: dict[str, dict[str, object]] = {}
    for label in sorted(str(value) for value in df[column].dropna().unique()):
        group_mask = df[column] == label
        stream, metrics = _metrics(
            signal[group_mask].reset_index(drop=True),
            returns[group_mask].reset_index(drop=True),
            df.loc[group_mask, "timestamp"].reset_index(drop=True),
        )
        output[label] = {
            "n_rows": int(group_mask.sum()),
            "n_return_dates": len(stream),
            "metrics": metrics,
        }
    return output


def _window_inputs(
    df: pd.DataFrame,
    full_signal: pd.Series,
    *,
    start: date,
    end: date,
    regime: Any,
) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    dates = pd.to_datetime(df["timestamp"]).dt.date
    mask = (dates >= start) & (dates <= end)
    window = df.loc[mask].reset_index(drop=True)
    signal = full_signal[mask].reset_index(drop=True)
    returns = build_forward_returns(
        window["close"].astype(float),
        window["symbol"],
        regime.label_definition(),
    )
    returns = neutralize_forward_returns(
        returns,
        timestamps=window["timestamp"],
        symbols=window["symbol"],
        mode=regime.neutralize,
    )
    return window, signal, returns


def _load_universe(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe", type=Path, default=Path("configs/universes/hk_ipo.txt"))
    parser.add_argument("--start-date", type=date.fromisoformat, default=date(2025, 12, 12))
    parser.add_argument("--end-date", type=date.fromisoformat, default=date(2026, 6, 26))
    parser.add_argument("--regime", default="lenient")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/validations/hk-ipo-ofi-attribution-20260714.json"),
    )
    args = parser.parse_args(argv)

    symbols = _load_universe(args.universe)
    loader = create_equities_loader(source="bigquery")
    df, _ = loader.load_bars(
        DataRequest(
            symbols=symbols,
            start=args.start_date,
            end=args.end_date,
            frequency=BarFrequency.DAILY,
        ),
    )
    df = df.sort_values(["symbol", "timestamp"]).reset_index(drop=True)
    regime = get_regime(args.regime)
    df["listing_age_bucket"] = df["days_since_listing"].map(_listing_age_bucket)
    df["event_proximity_bucket"] = df.apply(_event_proximity_bucket, axis=1)

    total_days = (args.end_date - args.start_date).days + 1
    holdout_days = max(1, round(total_days * regime.holdout_fraction))
    holdout_start = args.end_date - timedelta(days=holdout_days - 1)
    in_sample_end = holdout_start - timedelta(days=1)
    spans = fold_windows(
        args.start_date,
        in_sample_end,
        regime.walk_forward_config(),
        embargo_days=regime.embargo_days,
    )

    factors: list[dict[str, Any]] = []
    for expression in EXPRESSIONS:
        full_signal = compute_signal(expression, df)
        fold_payloads: list[dict[str, Any]] = []
        for fold_start, fold_end in spans:
            fold, signal, returns = _window_inputs(
                df,
                full_signal,
                start=fold_start,
                end=fold_end,
                regime=regime,
            )
            stream, metrics = _metrics(
                signal,
                returns,
                fold["timestamp"],
            )
            fold_payloads.append(
                {
                    "start": str(fold_start),
                    "end": str(fold_end),
                    "metrics": metrics,
                    "top_dates": _top_dates(stream),
                },
            )

        in_sample, signal, returns = _window_inputs(
            df,
            full_signal,
            start=args.start_date,
            end=in_sample_end,
            regime=regime,
        )
        full_stream, full_metrics = _metrics(
            signal,
            returns,
            in_sample["timestamp"],
        )
        factors.append(
            {
                "expression": expression,
                "aggregate_in_sample": {
                    "metrics": full_metrics,
                    "top_dates": _top_dates(full_stream),
                },
                "folds": fold_payloads,
                "listing_age_attribution": _group_attribution(
                    in_sample,
                    signal,
                    returns,
                    "listing_age_bucket",
                ),
                "event_proximity_attribution": _group_attribution(
                    in_sample,
                    signal,
                    returns,
                    "event_proximity_bucket",
                ),
            },
        )

    payload = {
        "schema_version": 1,
        "purpose": "diagnostic_only_no_promotion_override",
        "universe": str(args.universe),
        "n_symbols": len(symbols),
        "start_date": str(args.start_date),
        "end_date": str(args.end_date),
        "regime": args.regime,
        "holdout_start": str(holdout_start),
        "in_sample_end": str(in_sample_end),
        "folds": [{"start": str(start), "end": str(end)} for start, end in spans],
        "factors": factors,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
