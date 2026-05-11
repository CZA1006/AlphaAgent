#!/usr/bin/env python3
"""Round 6 — combine N factor expressions into one basket and evaluate it.

Individual factors that don't survive the strict regime sometimes do
when combined.  This CLI takes a handful of DSL expressions (or a file
of them, one per line), computes each as a per-(date,asset) signal,
combines via rank aggregation / z-score average / equal weight, then
runs the basket through the same SignalQualityEvaluator + regime that
:mod:`scripts.validate_strict` uses.

No LLM is invoked — the whole point is to test whether previously
*proposed* factors (mock or LLM-generated) work better in concert.

Usage::

    # Combine three factors via rank-aggregation (the default)
    uv run python -m scripts.combine_factors \\
        --data-source parquet --universe configs/universes/sp50.txt \\
        --start-date 2024-04-19 --end-date 2026-04-17 \\
        --expr 'rank(ts_mean(close, 20))' \\
        --expr 'rank(ts_std(close, 20))' \\
        --expr 'zscore(ts_mean(volume, 10))'

    # ...or read expressions from a file (one per line)
    uv run python -m scripts.combine_factors --expressions-file expressions.txt
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

import pandas as pd

from alpha_harness.combination import (
    CombinationMethod,
    combine_signals,
    compute_signal,
    pairwise_rank_corr,
)
from alpha_harness.data.synthetic import generate_price_panel
from alpha_harness.evaluators.signal_quality import (
    build_forward_returns,
    compute_mean_ic,
    compute_mean_rank_ic,
    compute_quantile_spread,
)
from alpha_harness.regimes import get_regime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger("combine_factors")


# ── Data loaders (mirror validate_strict) ───────────────────────────────────


def _load_universe(path: Path) -> list[str]:
    if not path.is_file():
        raise FileNotFoundError(f"universe file not found: {path}")
    return [
        s.strip()
        for s in path.read_text(encoding="utf-8").splitlines()
        if s.strip() and not s.strip().startswith("#")
    ]


def _load_data(args: argparse.Namespace) -> pd.DataFrame:
    if args.data_source == "synthetic":
        return generate_price_panel(
            n_days=args.n_days,
            symbols=[f"S{i}" for i in range(args.n_symbols)],
            seed=args.seed,
        )
    if not args.universe:
        raise ValueError("--universe is required for --data-source parquet/polygon")
    symbols = _load_universe(Path(args.universe))
    start = date.fromisoformat(args.start_date)
    end = date.fromisoformat(args.end_date)
    from alpha_harness.data.loader_factory import create_equities_loader
    from alpha_harness.data.models import BarFrequency, DataRequest

    loader = create_equities_loader(source=args.data_source, base_path=args.data_path)
    df, _ = loader.load_bars(
        DataRequest(symbols=symbols, start=start, end=end, frequency=BarFrequency.DAILY),
    )
    if df.empty:
        raise RuntimeError(
            f"loader returned empty frame for {len(symbols)} symbols",
        )
    return df


# ── Expression source ───────────────────────────────────────────────────────


def _resolve_expressions(args: argparse.Namespace) -> list[str]:
    exprs: list[str] = list(args.expr or [])
    if args.expressions_file:
        path = Path(args.expressions_file)
        if not path.is_file():
            raise FileNotFoundError(f"expressions file not found: {path}")
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                exprs.append(line)
    if args.from_validation_report:
        exprs.extend(_load_from_validation_report(args))
    if len(exprs) < 2:
        raise ValueError("need at least 2 expressions to combine; got " + str(len(exprs)))
    # Deduplicate while preserving order so the same factor isn't loaded
    # twice when the same expression appears in multiple sources.
    seen: set[str] = set()
    deduped: list[str] = []
    for e in exprs:
        if e not in seen:
            seen.add(e)
            deduped.append(e)
    return deduped


def _load_from_validation_report(args: argparse.Namespace) -> list[str]:
    """Pull factor expressions from one or more StrictValidationReport JSONs.

    ``--from-validation-report`` accepts a path to a single
    ``{cycle_id}.json`` file, or a directory containing them — typically
    ``artifacts/validations/`` or a sub-tree.  Filters via
    ``--filter-passes-ic`` / ``--filter-min-ic`` keep only the factors
    most likely to combine productively.
    """
    src = Path(args.from_validation_report)
    if not src.exists():
        raise FileNotFoundError(f"validation-report path not found: {src}")
    files: list[Path] = sorted(src.glob("*.json")) if src.is_dir() else [src]

    threshold_ic = args.filter_min_ic if args.filter_min_ic is not None else 0.0
    out: list[str] = []
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Skipping %s: %s", path, exc)
            continue
        for thumb in payload.get("factors", []):
            ic = thumb.get("ic")
            ric = thumb.get("rank_ic")
            if args.filter_passes_ic and not (isinstance(ic, int | float) and ic >= threshold_ic):
                continue
            if args.filter_passes_rank_ic and not (
                isinstance(ric, int | float) and ric >= threshold_ic
            ):
                continue
            expr = thumb.get("expression")
            if expr:
                out.append(str(expr))
    if not out:
        logger.warning(
            "No factors loaded from %s after filters (passes_ic=%s, passes_rank_ic=%s, min_ic=%s).",
            src,
            args.filter_passes_ic,
            args.filter_passes_rank_ic,
            args.filter_min_ic,
        )
    return out


# ── CLI ─────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--data-source",
        choices=["synthetic", "parquet", "polygon"],
        default="parquet",
    )
    p.add_argument("--data-path", default="data/silver/equities")
    p.add_argument("--universe", default=None)
    p.add_argument("--start-date", default="2024-04-19")
    p.add_argument("--end-date", default="2026-04-17")
    p.add_argument("--n-days", type=int, default=240)
    p.add_argument("--n-symbols", type=int, default=10)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument(
        "--expr",
        action="append",
        help="A single DSL expression; repeat for each factor.",
    )
    p.add_argument(
        "--expressions-file",
        default=None,
        help="Path to a newline-separated file of DSL expressions.",
    )
    p.add_argument(
        "--from-validation-report",
        default=None,
        help=(
            "Path to a StrictValidationReport JSON, or a directory of "
            "them (e.g. artifacts/validations/).  Loads every "
            "factor.expression from the report's 'factors' block."
        ),
    )
    p.add_argument(
        "--filter-passes-ic",
        action="store_true",
        help="When loading from a validation report, drop factors whose IC < --filter-min-ic.",
    )
    p.add_argument(
        "--filter-passes-rank-ic",
        action="store_true",
        help="When loading from a validation report, drop factors whose rank_IC < --filter-min-ic.",
    )
    p.add_argument(
        "--filter-min-ic",
        type=float,
        default=None,
        help="Threshold for the --filter-passes-{ic,rank-ic} filters (default 0.0).",
    )
    p.add_argument(
        "--method",
        choices=[m.value for m in CombinationMethod],
        default=CombinationMethod.RANK_AGGREGATE.value,
    )
    p.add_argument(
        "--regime",
        choices=["strict", "lenient"],
        default="strict",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of a text block.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        expressions = _resolve_expressions(args)
        df = _load_data(args)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    regime = get_regime(args.regime)
    logger.info(
        "Combining %d factors via %s under '%s' regime (ic>=%.4f rank_ic>=%.4f)",
        len(expressions),
        args.method,
        args.regime,
        regime.ic_threshold,
        regime.rank_ic_threshold,
    )

    # Per-factor signals + per-factor metrics so the operator can compare
    # individuals against the basket.
    timestamps = df["timestamp"]
    groups = df["symbol"] if "symbol" in df.columns else None
    fwd_returns = build_forward_returns(
        df["close"].astype(float),
        groups,
        regime.label_definition(),
    )

    individuals = []
    signals: list[pd.Series] = []
    for expr in expressions:
        try:
            sig = compute_signal(expr, df)
        except Exception as exc:
            print(f"error: failed to compile {expr!r}: {exc}", file=sys.stderr)
            return 3
        signals.append(sig)
        ic = compute_mean_ic(sig, fwd_returns, timestamps)
        ric = compute_mean_rank_ic(sig, fwd_returns, timestamps)
        qs = compute_quantile_spread(sig, fwd_returns, timestamps, regime.n_quantiles)
        individuals.append(
            {
                "expression": expr,
                "ic": ic,
                "rank_ic": ric,
                "quantile_spread": qs,
                "passes_ic": ic is not None and ic >= regime.ic_threshold,
                "passes_rank_ic": ric is not None and ric >= regime.rank_ic_threshold,
            }
        )

    # Combination
    method = CombinationMethod(args.method)
    basket_sig = combine_signals(signals, timestamps, method)
    basket = {
        "method": method.value,
        "ic": compute_mean_ic(basket_sig, fwd_returns, timestamps),
        "rank_ic": compute_mean_rank_ic(basket_sig, fwd_returns, timestamps),
        "quantile_spread": compute_quantile_spread(
            basket_sig,
            fwd_returns,
            timestamps,
            regime.n_quantiles,
        ),
    }
    basket["passes_ic"] = basket["ic"] is not None and basket["ic"] >= regime.ic_threshold
    basket["passes_rank_ic"] = (
        basket["rank_ic"] is not None and basket["rank_ic"] >= regime.rank_ic_threshold
    )

    # Pairwise correlation matrix — tells us *why* combination did/didn't help.
    corr = pairwise_rank_corr(signals, timestamps)
    avg_off_diag = (
        float((corr.values.sum() - corr.values.trace()) / (corr.size - len(corr)))
        if len(corr) > 1
        else float("nan")
    )

    summary = {
        "regime": args.regime,
        "method": method.value,
        "n_factors": len(expressions),
        "individuals": individuals,
        "basket": basket,
        "avg_pairwise_rank_corr": avg_off_diag,
        "thresholds": {
            "ic": regime.ic_threshold,
            "rank_ic": regime.rank_ic_threshold,
            "quantile_spread": regime.quantile_spread_threshold,
        },
    }

    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        _print_summary(summary)
    return 0


def _print_summary(s: dict[str, object]) -> None:
    border = "=" * 78
    print(f"\n{border}")
    print(f"  COMBINATION RESULT  ({s['method']}, regime={s['regime']})")
    print(border)
    print(f"  {'expression':<48}  {'ic':>8}  {'rank_ic':>8}  pass")
    print(f"  {'-' * 48}  {'-' * 8}  {'-' * 8}  ----")
    for f in s["individuals"]:  # type: ignore[union-attr]
        ic = f"{f['ic']:+.4f}" if f.get("ic") is not None else "   n/a"
        ric = f"{f['rank_ic']:+.4f}" if f.get("rank_ic") is not None else "   n/a"
        flags = ("ic " if f["passes_ic"] else "   ") + ("rank_ic" if f["passes_rank_ic"] else "")
        print(f"  {str(f['expression'])[:48]:<48}  {ic:>8}  {ric:>8}  {flags.strip() or '-'}")
    print(f"  {'-' * 48}  {'-' * 8}  {'-' * 8}  ----")
    b = s["basket"]  # type: ignore[index]
    bic = f"{b['ic']:+.4f}" if b.get("ic") is not None else "   n/a"
    bric = f"{b['rank_ic']:+.4f}" if b.get("rank_ic") is not None else "   n/a"
    bflags = ("ic " if b["passes_ic"] else "   ") + ("rank_ic" if b["passes_rank_ic"] else "")
    print(f"  {'BASKET':<48}  {bic:>8}  {bric:>8}  {bflags.strip() or '-'}")
    print()
    print(f"  avg pairwise rank-correlation : {s['avg_pairwise_rank_corr']:+.4f}")
    print(
        f"  thresholds                    : ic>={s['thresholds']['ic']:.4f}  "  # type: ignore[index]
        f"rank_ic>={s['thresholds']['rank_ic']:.4f}",
    )
    print(f"{border}\n")


if __name__ == "__main__":
    sys.exit(main())
