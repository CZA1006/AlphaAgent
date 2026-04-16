#!/usr/bin/env python3
"""Run one full research cycle end-to-end.

This is the Round 2 MVP demonstration script.  It wires every real
component — compiler, DSL executor, signal-quality evaluator, promotion
judge, and registries — into a single research cycle and prints the
resulting ExperimentRecord.

Usage::

    # Default: synthetic data, rank(ts_mean(close, 20)) signal
    uv run python -m scripts.run_research_cycle

    # Custom expression
    uv run python -m scripts.run_research_cycle \
        --expression "rank(ts_mean(close, 10))" \
        --n-days 200 --n-symbols 15

    # Load from local Parquet instead of synthetic data
    uv run python -m scripts.run_research_cycle \
        --data-source parquet --data-path data/silver/equities \
        --symbols AAPL,MSFT,GOOG

No external services (Postgres, APIs) are required — the script uses
in-memory registries and synthetic price data by default.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

import pandas as pd

from alpha_harness.data.synthetic import generate_price_panel
from alpha_harness.evaluators.promotion_judge import PromotionJudge
from alpha_harness.evaluators.signal_quality import SignalQualityEvaluator
from alpha_harness.factors.compiler import FactorDslCompiler
from alpha_harness.orchestrator.research_loop import ResearchOrchestrator
from alpha_harness.registries.experiment import ExperimentRegistry
from alpha_harness.registries.hypothesis import HypothesisRegistry
from alpha_harness.schemas.evaluation import (
    EvaluationProfile,
    EvaluationRequest,
    LabelDefinition,
)
from alpha_harness.schemas.experiment import ExperimentRecord
from alpha_harness.schemas.hypothesis import Hypothesis
from alpha_harness.service import AlphaHarnessService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger("run_research_cycle")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Run one AlphaAgent research cycle end-to-end.",
    )

    # Hypothesis / factor
    p.add_argument(
        "--expression",
        default="rank(ts_mean(close, 20))",
        help="Factor DSL expression to evaluate (default: rank(ts_mean(close, 20))).",
    )
    p.add_argument(
        "--rationale",
        default="Cross-sectional momentum: stocks with higher 20-day mean close rank higher.",
        help="Human-readable rationale for the hypothesis.",
    )

    # Data
    p.add_argument(
        "--data-source",
        choices=["synthetic", "parquet"],
        default="synthetic",
        help="Where to load price data from (default: synthetic).",
    )
    p.add_argument(
        "--data-path",
        default="data/silver/equities",
        help="Base path for local Parquet data (only used with --data-source parquet).",
    )
    p.add_argument(
        "--symbols",
        default=None,
        help="Comma-separated symbol list (default: 10 large-cap tickers for synthetic).",
    )
    p.add_argument("--n-days", type=int, default=180, help="Days of synthetic data.")
    p.add_argument("--n-symbols", type=int, default=10, help="Symbols for synthetic data.")
    p.add_argument("--seed", type=int, default=42, help="Random seed for synthetic data.")

    # Evaluation profile
    p.add_argument(
        "--ic-threshold",
        type=float,
        default=0.02,
        help="Minimum IC to pass (default: 0.02).",
    )
    p.add_argument(
        "--min-periods",
        type=int,
        default=20,
        help="Minimum date periods for data sufficiency (default: 20).",
    )
    p.add_argument(
        "--min-assets",
        type=int,
        default=3,
        help="Minimum assets for data sufficiency (default: 3).",
    )

    # Output
    p.add_argument(
        "--json",
        action="store_true",
        help="Print the ExperimentRecord as JSON instead of formatted text.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    """Entry point — returns 0 on success."""
    args = _build_parser().parse_args(argv)

    # ── 1. Load or generate price data ─────────────────────────────────
    if args.data_source == "parquet":
        from alpha_harness.data.equities_loader import LocalEquitiesLoader
        from alpha_harness.data.models import DataRequest

        symbols = args.symbols.split(",") if args.symbols else ["AAPL", "MSFT", "GOOG"]
        loader = LocalEquitiesLoader(base_path=args.data_path)
        request = DataRequest(
            symbols=symbols,
            start=date(2023, 1, 1),
            end=date(2024, 12, 31),
        )
        price_data, meta = loader.load_bars(request)
        if meta.bars_returned == 0:
            logger.error("No data found at %s for symbols %s", args.data_path, symbols)
            return 1
        logger.info(
            "Loaded %d bars for %d symbols from %s",
            meta.bars_returned,
            meta.symbols_returned,
            args.data_path,
        )
    else:
        symbols_list: list[str] | None = None
        if args.symbols:
            symbols_list = args.symbols.split(",")
        elif args.n_symbols != 10:
            # Generate custom number of symbols
            symbols_list = [f"SYM_{i:02d}" for i in range(args.n_symbols)]

        price_data = generate_price_panel(
            symbols=symbols_list,
            n_days=args.n_days,
            seed=args.seed,
        )
        n_syms = price_data["symbol"].nunique()
        n_dates = price_data["timestamp"].nunique()
        logger.info(
            "Generated synthetic panel: %d symbols x %d dates = %d rows",
            n_syms,
            n_dates,
            len(price_data),
        )

    # ── 2. Build hypothesis ────────────────────────────────────────────
    hypothesis = Hypothesis(
        text=args.expression,
        rationale=args.rationale,
        tags=["round2_mvp"],
    )
    logger.info("Hypothesis %s: %s", hypothesis.id, hypothesis.text)

    # ── 3. Wire the full pipeline ──────────────────────────────────────
    compiler = FactorDslCompiler()
    evaluator = SignalQualityEvaluator(price_data)
    judge = PromotionJudge(refine_margin=0.20)
    service = AlphaHarnessService(compiler=compiler, evaluator=evaluator, judge=judge)

    experiment_registry = ExperimentRegistry()
    hypothesis_registry = HypothesisRegistry()

    orchestrator = ResearchOrchestrator(
        service=service,
        judge=judge,
        experiment_registry=experiment_registry,
        hypothesis_registry=hypothesis_registry,
    )

    # ── 4. Build evaluation request ────────────────────────────────────
    ts_dates = pd.to_datetime(price_data["timestamp"]).dt.date  # type: ignore[union-attr]
    eval_start = ts_dates.min()
    eval_end = ts_dates.max()

    eval_request = EvaluationRequest(
        factor_id="pending",
        universe_id="synthetic_us_equity",
        eval_start=eval_start,
        eval_end=eval_end,
        label=LabelDefinition(
            forecast_horizon_bars=5,
            lag_bars=1,
            return_type="simple",
        ),
        profile=EvaluationProfile(
            thresholds={
                "ic": args.ic_threshold,
                "rank_ic": args.ic_threshold,
                "quantile_spread": 0.001,
            },
            min_periods=args.min_periods,
            min_assets=args.min_assets,
            n_quantiles=5,
        ),
    )

    # ── 5. Run the cycle ───────────────────────────────────────────────
    logger.info("Running research cycle...")
    record = orchestrator.run_cycle(hypothesis, eval_request)

    # ── 6. Display results ─────────────────────────────────────────────
    if args.json:
        print(record.model_dump_json(indent=2))
    else:
        _print_summary(record)

    # Show registry state
    summary = orchestrator.summary()
    logger.info("Registry summary: %s", summary)

    return 0


def _print_summary(record: ExperimentRecord) -> None:
    """Pretty-print an experiment record."""
    border = "=" * 72
    print(f"\n{border}")
    print("  RESEARCH CYCLE RESULT")
    print(border)
    print(f"  Experiment ID   : {record.id}")
    print(f"  Hypothesis      : {record.hypothesis.text}")
    print(f"  Factor          : {record.factor.name}")
    print(f"  Expression      : {record.factor.expression}")
    print(f"  Decision        : {record.decision.value.upper()}")
    print()
    ev = record.evaluation
    print("  Evaluation Metrics:")
    ic_str = f"{ev.ic:.6f}" if ev.ic is not None else "N/A"
    ric_str = f"{ev.rank_ic:.6f}" if ev.rank_ic is not None else "N/A"
    qs_str = f"{ev.quantile_spread:.6f}" if ev.quantile_spread is not None else "N/A"
    print(f"    IC              : {ic_str}")
    print(f"    Rank IC         : {ric_str}")
    print(f"    Quantile Spread : {qs_str}")
    print(f"    N periods       : {ev.n_periods}")
    print(f"    N assets        : {ev.n_assets}")
    if record.failure:
        print(f"\n  Failure: [{record.failure.category}] {record.failure.detail}")
    if record.notes:
        print(f"  Notes: {record.notes}")
    print(f"{border}\n")


if __name__ == "__main__":
    sys.exit(main())
