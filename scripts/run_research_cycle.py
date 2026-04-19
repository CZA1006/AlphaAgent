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
import os
import sys
from datetime import date

import pandas as pd

from alpha_harness.config import BackendConfig
from alpha_harness.data.synthetic import generate_price_panel
from alpha_harness.evaluators.promotion_judge import PromotionJudge
from alpha_harness.evaluators.signal_quality import SignalQualityEvaluator
from alpha_harness.factors.compiler import FactorDslCompiler
from alpha_harness.orchestrator.research_loop import ResearchOrchestrator
from alpha_harness.registries.factory import build_registries
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
        choices=["synthetic", "parquet", "polygon"],
        default="synthetic",
        help=(
            "Where to load price data from (default: synthetic). "
            "'polygon' requires POLYGON_API_KEY and issues real API calls."
        ),
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
    p.add_argument(
        "--start-date",
        default="2024-01-01",
        help="Start date for real/parquet data (YYYY-MM-DD, default 2024-01-01).",
    )
    p.add_argument(
        "--end-date",
        default="2024-06-30",
        help="End date for real/parquet data (YYYY-MM-DD, default 2024-06-30).",
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

    # Persistence backend
    p.add_argument(
        "--backend",
        choices=["memory", "sql"],
        default=None,
        help=(
            "Persistence backend. 'memory' (default) needs no services; "
            "'sql' uses Postgres via POSTGRES_* env vars. Falls back to "
            "the ALPHA_AGENT_BACKEND env var when unset."
        ),
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
    if args.data_source in ("parquet", "polygon"):
        from alpha_harness.data.loader_factory import create_equities_loader
        from alpha_harness.data.models import DataRequest

        symbols = (
            args.symbols.split(",") if args.symbols else ["AAPL", "MSFT", "GOOG"]
        )
        try:
            start = date.fromisoformat(args.start_date)
            end = date.fromisoformat(args.end_date)
        except ValueError as exc:
            logger.error("Bad date format (expected YYYY-MM-DD): %s", exc)
            return 2

        if args.data_source == "polygon" and not os.environ.get("POLYGON_API_KEY"):
            logger.error(
                "--data-source polygon requires POLYGON_API_KEY in the "
                "environment.  Run `make doctor --mode data` to diagnose.",
            )
            return 2

        loader = create_equities_loader(
            source=args.data_source,
            base_path=args.data_path,
        )
        request = DataRequest(symbols=symbols, start=start, end=end)
        price_data, meta = loader.load_bars(request)
        if meta.bars_returned == 0:
            logger.error(
                "No data returned from %s for symbols %s in %s..%s",
                args.data_source, symbols, start, end,
            )
            return 1
        logger.info(
            "Loaded %d bars for %d symbols from %s",
            meta.bars_returned,
            meta.symbols_returned,
            args.data_source,
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

    backend_config = BackendConfig.from_env(override=args.backend)
    logger.info("Using %s backend for registries.", backend_config.backend)
    registries = build_registries(backend_config)

    orchestrator = ResearchOrchestrator(
        service=service,
        experiment_registry=registries.experiments,
        hypothesis_registry=registries.hypotheses,
        memory_registry=registries.memories,
    )

    # ── 4. Build evaluation request ────────────────────────────────────
    ts_dates = pd.to_datetime(price_data["timestamp"]).dt.date
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
