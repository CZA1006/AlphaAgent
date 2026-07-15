#!/usr/bin/env python3
"""Selector backtest: persistence-ranked vs train-IC-ranked basket selection.

The HK IPO microstructure case study showed 10/12 candidate factors
persisting train → test while the strict-gate basket (built from the
highest-train-IC factors) failed OOS.  This script tests the *selector*
itself on that exact answer key:

1. For each candidate factor, split the TRAIN window into contiguous
   sub-windows (embargoed by lag + horizon) and compute per-sub-window
   rank-IC → a :class:`PersistenceScore`.
2. Select two top-k baskets from TRAIN information only:
   ``by-trainIC`` (the old behaviour) vs ``by-persistence`` (the fix).
3. Compare both baskets on the held-out TEST window: rank-IC and
   long-only market-hedged net excess (the case study's decisive form).
4. Measure the tail-concentration gate's misfire rate: tail share with
   and without each stock's first trading days (IPO debut spikes).

Usage::

    GOOGLE_CLOUD_PROJECT=bloomberg-database-0629 \\
    uv run python -m scripts.analysis.hk_ipo_persistence_selection \\
        --factors-file scripts/analysis/hk_ipo_micro_factors.txt
"""

from __future__ import annotations

import argparse
import os
from datetime import date

import numpy as np
import pandas as pd

from alpha_harness.combination import (
    CombinationMethod,
    combine_signals,
    compute_signal,
    pairwise_rank_corr,
)
from alpha_harness.data.loader_factory import create_equities_loader
from alpha_harness.data.models import BarFrequency, DataRequest
from alpha_harness.evaluators.persistence import (
    PersistenceScore,
    rank_by_persistence,
    score_from_folds,
)
from alpha_harness.evaluators.portfolio import (
    compute_long_short_returns,
    compute_portfolio_metrics,
)
from alpha_harness.evaluators.signal_quality import compute_mean_rank_ic
from scripts.analysis.hk_ipo_micro_oos import (
    HORIZON,
    LAG,
    _forward_returns,
    _hsi_forward,
    _load_universe,
    _long_only,
    _win,
)

EMBARGO_DAYS = LAG + HORIZON


def _prepare(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values(["timestamp", "symbol"]).reset_index(drop=True)
    df = df.assign(fwd=_forward_returns(df))
    df["dd"] = pd.to_datetime(df["timestamp"]).dt.date
    return df


def _fold_date_blocks(dates: list[object], n_folds: int) -> list[set[object]]:
    """Split sorted trading dates into contiguous blocks, embargoing the
    last ``EMBARGO_DAYS`` dates of every block so the forward label that
    closes one block cannot leak into the next."""
    blocks: list[set[object]] = []
    for chunk in np.array_split(np.asarray(dates, dtype=object), n_folds):
        kept = list(chunk[:-EMBARGO_DAYS]) if len(chunk) > EMBARGO_DAYS else []
        if len(kept) >= 5:
            blocks.append(set(kept))
    return blocks


def _per_fold_rank_ics(
    df: pd.DataFrame,
    sig: pd.Series,
    blocks: list[set[object]],
) -> list[float | None]:
    out: list[float | None] = []
    for block in blocks:
        mask = df["dd"].isin(block)
        out.append(compute_mean_rank_ic(sig[mask], df.loc[mask, "fwd"], df.loc[mask, "timestamp"]))
    return out


def _tail_concentration(
    df: pd.DataFrame,
    sig: pd.Series,
    *,
    min_days_listed: int = 0,
) -> float | None:
    """Tail share of the long-short return stream, optionally excluding
    each symbol's first ``min_days_listed`` trading days (IPO debut)."""
    if min_days_listed > 0:
        day_rank = df.groupby("symbol")["dd"].rank(method="dense")
        keep = day_rank > min_days_listed
        df = df[keep]
        sig = sig[keep]
    ls = compute_long_short_returns(sig, df["fwd"], df["timestamp"])
    metrics = compute_portfolio_metrics(ls, overlap_horizon_bars=HORIZON)
    return metrics["tail_concentration"]


def _basket_signal(df: pd.DataFrame, exprs: list[str]) -> pd.Series:
    signals = [compute_signal(e, df) for e in exprs]
    return combine_signals(signals, df["timestamp"], CombinationMethod.RANK_AGGREGATE)


def _mean_offdiag(corr: pd.DataFrame) -> float:
    vals = corr.to_numpy()
    n = vals.shape[0]
    off = [vals[i, j] for i in range(n) for j in range(n) if i != j and not np.isnan(vals[i, j])]
    return float(np.mean(off)) if off else float("nan")


def _report_basket(
    label: str,
    exprs: list[str],
    train: pd.DataFrame,
    test: pd.DataFrame,
    hsi_fwd: dict[date, float],
    full_spread: float,
) -> None:
    print(f"\n--- basket: {label} (k={len(exprs)}) ---")
    for e in exprs:
        print(f"  {e}")
    signals = [compute_signal(e, train) for e in exprs]
    rho = _mean_offdiag(pairwise_rank_corr(signals, train["timestamp"]))
    print(f"  mean pairwise rank corr (train): {rho:+.2f}")
    for name, df in (("TRAIN", train), ("TEST", test)):
        sig = _basket_signal(df, exprs)
        ric = compute_mean_rank_ic(sig, df["fwd"], df["timestamp"])
        net, hit = _long_only(df, sig, hsi_fwd, full_spread)
        ric_s = f"{ric:+.4f}" if ric is not None else "n/a"
        net_s = f"{net:+.4f}" if net is not None else "n/a"
        hit_s = f"{hit:.0f}%" if hit is not None else "n/a"
        print(f"  {name}: rank_ic={ric_s}  longNet5d={net_s}  hit={hit_s}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--factors-file", default="scripts/analysis/hk_ipo_micro_factors.txt")
    p.add_argument("--universe", default="configs/universes/hk_ipo.txt")
    p.add_argument("--train", default="2025-12-12:2026-04-30")
    p.add_argument("--test", default="2026-05-01:2026-06-26")
    p.add_argument("--n-folds", type=int, default=4)
    p.add_argument("--top-k", type=int, default=4)
    p.add_argument("--exclude-first-days", type=int, default=5)
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
        return _prepare(df)

    train, test = panel(args.train), panel(args.test)
    hsi_fwd = _hsi_forward(args.project)
    half_spread = args.half_spread_bps
    if half_spread is None:
        half_spread = float(np.nanmean(test["rel_spread"])) * 1e4 / 2
    full_spread = 2 * half_spread * 1e-4

    train_dates = sorted(train["dd"].unique())
    blocks = _fold_date_blocks(train_dates, args.n_folds)
    print(
        f"train {args.train} -> {len(blocks)} sub-windows "
        f"(embargo {EMBARGO_DAYS}d each); test {args.test}; "
        f"round-trip cost {full_spread * 1e4:.0f} bps"
    )

    # ── Per-factor: train-IC vs persistence, and the OOS answer key ──────
    rows: list[dict[str, object]] = []
    print(f"\n{'factor':<52}{'trainRIC':>9}{'foldRICs':>28}{'frac+':>6}{'stab':>7}{'testRIC':>9}")
    for e in exprs:
        sig_tr = compute_signal(e, train)
        train_ric = compute_mean_rank_ic(sig_tr, train["fwd"], train["timestamp"])
        fold_rics = _per_fold_rank_ics(train, sig_tr, blocks)
        score = score_from_folds(fold_rics)
        sig_te = compute_signal(e, test)
        test_ric = compute_mean_rank_ic(sig_te, test["fwd"], test["timestamp"])
        rows.append(
            {"expr": e, "train_ric": train_ric, "score": score, "test_ric": test_ric},
        )
        folds_s = " ".join(f"{v:+.2f}" if v is not None else " n/a" for v in fold_rics)
        frac_s = f"{score.fraction_positive:.2f}" if score else "n/a"
        stab_s = f"{score.stability:+.2f}" if score else "n/a"
        tr_s = f"{train_ric:+.4f}" if train_ric is not None else "n/a"
        te_s = f"{test_ric:+.4f}" if test_ric is not None else "n/a"
        print(f"{e[:52]:<52}{tr_s:>9}{folds_s:>28}{frac_s:>6}{stab_s:>7}{te_s:>9}")

    # Selector quality: how well does each train-side ordering predict the
    # test-side rank-IC ordering?  (Spearman across the candidate set.)
    frame = pd.DataFrame(
        {
            "train_ric": [r["train_ric"] for r in rows],
            "persistence": [
                r["score"].sort_key[0] * 1e6 + min(max(r["score"].stability, -1e3), 1e3)
                if isinstance(r["score"], PersistenceScore)
                else np.nan
                for r in rows
            ],
            "test_ric": [r["test_ric"] for r in rows],
        },
    )
    # Spearman via rank + Pearson (scipy is not a project dependency).
    test_rank = frame["test_ric"].rank()
    corr_train = frame["train_ric"].rank().corr(test_rank)
    corr_pers = frame["persistence"].rank().corr(test_rank)
    print(f"\nselector→OOS Spearman: train_ric {corr_train:+.2f}  persistence {corr_pers:+.2f}")

    # ── Tail-concentration gate misfire measurement ──────────────────────
    print(
        f"\ntail_concentration (gate fires > 0.50): all days vs "
        f"excluding first {args.exclude_first_days} trading days per stock"
    )
    flips = 0
    for r in rows:
        sig_tr = compute_signal(str(r["expr"]), train)
        t_all = _tail_concentration(train, sig_tr)
        t_ex = _tail_concentration(train, sig_tr, min_days_listed=args.exclude_first_days)
        fire_all = t_all is not None and t_all > 0.5
        fire_ex = t_ex is not None and t_ex > 0.5
        flips += int(fire_all and not fire_ex)
        a = f"{t_all:.2f}" if t_all is not None else " n/a"
        b = f"{t_ex:.2f}" if t_ex is not None else " n/a"
        mark = "  <- gate misfire" if fire_all and not fire_ex else ""
        print(f"  {str(r['expr'])[:52]:<52}{a:>6}{b:>6}{mark}")
    print(f"gate would flip on {flips}/{len(rows)} factors after debut exclusion")

    # ── The head-to-head basket test ──────────────────────────────────────
    by_train = sorted(
        [r for r in rows if r["train_ric"] is not None],
        key=lambda r: float(r["train_ric"]),  # type: ignore[arg-type]
        reverse=True,
    )[: args.top_k]
    scored_rows = [
        (r, r["score"] if isinstance(r["score"], PersistenceScore) else None) for r in rows
    ]
    by_pers = rank_by_persistence(scored_rows)[: args.top_k]

    _report_basket(
        "by-trainIC (old selector)",
        [str(r["expr"]) for r in by_train],
        train,
        test,
        hsi_fwd,
        full_spread,
    )
    _report_basket(
        "by-persistence (new selector)",
        [str(r["expr"]) for r in by_pers],
        train,
        test,
        hsi_fwd,
        full_spread,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
