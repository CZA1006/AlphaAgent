#!/usr/bin/env python3
"""Run one *autonomous* research cycle end-to-end.

This is the Round 3 demonstration script.  Unlike
:mod:`scripts.run_research_cycle`, which takes a single hand-written DSL
expression, this script drives the full Round 3 stack:

    theme → HypothesisProposer → N validated candidates
          → HarnessAgentAdapter.run_theme
          → ResearchOrchestrator + RefinementRunner
          → structured ThemeCycleResponse summary

The adapter is the same concrete
:class:`~alpha_harness.hermes_boundary.harness_adapter.HarnessAgentAdapter`
that Hermes will use; the script just stands in for the Hermes runtime.

Usage::

    # No-key local run with the mock LLM
    uv run python -m scripts.autonomous_cycle --mock-llm

    # Live run (needs OPENROUTER_API_KEY in env)
    uv run python -m scripts.autonomous_cycle --theme "cross-sectional mean reversion"

    # SQL-backed persistence (needs Postgres)
    uv run python -m scripts.autonomous_cycle --mock-llm --backend sql
"""

from __future__ import annotations

import argparse
import json
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
from alpha_harness.hermes_boundary.contracts import (
    ResearchCycleResponse,
    ThemeCycleRequest,
    ThemeCycleResponse,
)
from alpha_harness.hermes_boundary.harness_adapter import HarnessAgentAdapter
from alpha_harness.llm import LLMClient, MockLLMClient
from alpha_harness.orchestrator.refinement import RefinementConfig, RefinementRunner
from alpha_harness.orchestrator.research_loop import ResearchOrchestrator
from alpha_harness.proposer import HypothesisProposer
from alpha_harness.proposer.schemas import RawProposal, RawProposalBatch
from alpha_harness.registries.factory import build_registries
from alpha_harness.schemas.evaluation import (
    EvaluationProfile,
    EvaluationRequest,
    LabelDefinition,
)
from alpha_harness.service import AlphaHarnessService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger("autonomous_cycle")


# ── Default DSL candidates used by the mock LLM ─────────────────────────────
#
# These are hand-picked safe expressions that cover the main mutation
# templates so the refinement runner has something interesting to do.
_MOCK_CANDIDATES: list[RawProposal] = [
    RawProposal(
        expression="rank(ts_mean(close, 20))",
        rationale="Cross-sectional 20-day mean reversion in price level.",
        tags=["mean_reversion"],
    ),
    RawProposal(
        expression="zscore(ts_delta(close, 5))",
        rationale="5-day price change standardised cross-sectionally.",
        tags=["momentum"],
    ),
    RawProposal(
        expression="rank(ts_std(close, 20))",
        rationale="Dispersion signal: rank stocks by realised volatility.",
        tags=["volatility"],
    ),
    RawProposal(
        expression="rank(volume)",
        rationale="Attention proxy — higher volume → higher rank.",
        tags=["volume"],
    ),
    RawProposal(
        expression="zscore(ts_mean(volume, 10))",
        rationale="10-day mean volume, standardised.",
        tags=["volume"],
    ),
]


def _make_mock_llm(n_candidates: int) -> LLMClient:
    """Return a :class:`MockLLMClient` that always emits a valid batch.

    The handler form is used so repeated LLM rounds (including the
    proposer's repair pass) keep working without running out of scripted
    responses.
    """
    batch = RawProposalBatch(proposals=_MOCK_CANDIDATES[:n_candidates])
    payload = batch.model_dump_json()
    return MockLLMClient(handler=lambda _req: payload)


# ── CLI ─────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Run one autonomous AlphaAgent research cycle end-to-end.",
    )
    p.add_argument(
        "--theme",
        default="cross-sectional equity signals derived from price and volume",
        help="Free-form research theme handed to the proposer.",
    )
    p.add_argument(
        "--n-candidates",
        type=int,
        default=3,
        help="How many hypotheses the proposer should return (default: 3).",
    )
    p.add_argument(
        "--extra-guidance",
        default="",
        help="Optional extra guidance text injected into the proposer prompt.",
    )

    # Data
    p.add_argument(
        "--data-source",
        choices=["synthetic", "parquet", "polygon"],
        default="synthetic",
        help=(
            "Price data source.  'synthetic' (default) needs no keys; "
            "'polygon' hits the real API (requires POLYGON_API_KEY); "
            "'parquet' reads a local Parquet store at --data-path."
        ),
    )
    p.add_argument("--data-path", default="data/silver/equities",
                   help="Base path for --data-source parquet.")
    p.add_argument(
        "--symbols", default=None,
        help="Comma-separated tickers for parquet/polygon sources.",
    )
    p.add_argument("--start-date", default="2024-07-01",
                   help="Start date for real data (YYYY-MM-DD).")
    p.add_argument("--end-date", default="2024-12-31",
                   help="End date for real data (YYYY-MM-DD).")
    p.add_argument("--n-days", type=int, default=180, help="Days of synthetic data.")
    p.add_argument("--n-symbols", type=int, default=10, help="Symbols for synthetic data.")
    p.add_argument("--seed", type=int, default=42, help="Random seed for synthetic data.")

    # Evaluation knobs
    p.add_argument(
        "--ic-threshold",
        type=float,
        default=0.02,
        help="Minimum IC (and rank IC) required to pass (default: 0.02).",
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

    # Refinement budgets
    p.add_argument("--max-depth", type=int, default=1)
    p.add_argument("--max-variants-per-step", type=int, default=3)
    p.add_argument("--max-total-children", type=int, default=6)

    # Persistence / LLM
    p.add_argument(
        "--backend",
        choices=["memory", "sql"],
        default=None,
        help="Registry backend; falls back to ALPHA_AGENT_BACKEND env var.",
    )
    p.add_argument(
        "--mock-llm",
        action="store_true",
        help="Use the offline mock LLM client (no API key required).",
    )

    # Output
    p.add_argument(
        "--json",
        action="store_true",
        help="Print the ThemeCycleResponse as JSON instead of formatted text.",
    )
    return p


# ── Entry point ─────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    # ── 1. Data ────────────────────────────────────────────────────────────
    if args.data_source in ("parquet", "polygon"):
        from alpha_harness.data.loader_factory import create_equities_loader
        from alpha_harness.data.models import DataRequest

        symbols_list = (
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
                "environment.  Run `make doctor-real` to diagnose.",
            )
            return 2

        loader = create_equities_loader(
            source=args.data_source,
            base_path=args.data_path,
        )
        request = DataRequest(symbols=symbols_list, start=start, end=end)
        price_data, meta = loader.load_bars(request)
        if meta.bars_returned == 0:
            logger.error(
                "No data returned from %s for symbols %s in %s..%s",
                args.data_source, symbols_list, start, end,
            )
            return 1
        logger.info(
            "Loaded %d bars for %d symbols from %s",
            meta.bars_returned, meta.symbols_returned, args.data_source,
        )
    else:
        symbols: list[str] | None = None
        if args.symbols:
            symbols = args.symbols.split(",")
        elif args.n_symbols != 10:
            symbols = [f"SYM_{i:02d}" for i in range(args.n_symbols)]
        price_data = generate_price_panel(
            symbols=symbols, n_days=args.n_days, seed=args.seed,
        )
        logger.info(
            "Generated synthetic panel: %d symbols x %d dates",
            price_data["symbol"].nunique(),
            price_data["timestamp"].nunique(),
        )

    # ── 2. Deterministic core (compiler + evaluator + judge) ──────────────
    compiler = FactorDslCompiler()
    evaluator = SignalQualityEvaluator(price_data)
    judge = PromotionJudge(refine_margin=0.20)
    service = AlphaHarnessService(
        compiler=compiler, evaluator=evaluator, judge=judge,
    )

    # ── 3. Registries (memory by default, SQL opt-in) ─────────────────────
    backend_config = BackendConfig.from_env(override=args.backend)
    logger.info("Using %s backend.", backend_config.backend)
    registries = build_registries(backend_config)

    # ── 4. Orchestrator + refinement runner ───────────────────────────────
    orchestrator = ResearchOrchestrator(
        service=service,
        experiment_registry=registries.experiments,
        hypothesis_registry=registries.hypotheses,
        memory_registry=registries.memories,
    )
    refinement_runner = RefinementRunner(
        orchestrator,
        config=RefinementConfig(
            max_depth=args.max_depth,
            max_variants_per_step=args.max_variants_per_step,
            max_total_children=args.max_total_children,
        ),
    )

    # ── 5. LLM + proposer ─────────────────────────────────────────────────
    if args.mock_llm:
        llm_client: LLMClient = _make_mock_llm(args.n_candidates)
    else:
        # Refuse silently-broken live runs: without an API key the
        # OpenRouterClient would only explode deep in the proposer's HTTP
        # call. Surface the configuration gap up-front with a clear hint.
        if not os.environ.get("OPENROUTER_API_KEY"):
            print(
                "error: live LLM requested but OPENROUTER_API_KEY is not set.\n"
                "       Re-run with --mock-llm for an offline demo, or export\n"
                "       OPENROUTER_API_KEY=... before invoking this script.",
                file=sys.stderr,
            )
            return 2
        # Lazy import so mock-LLM / CI paths don't need the optional deps.
        from alpha_harness.llm import OpenRouterClient, OpenRouterConfig

        llm_client = OpenRouterClient(OpenRouterConfig.from_env())

    proposer = HypothesisProposer(llm_client=llm_client, compiler=compiler)

    # ── 6. Evaluation request ─────────────────────────────────────────────
    ts_dates = pd.to_datetime(price_data["timestamp"]).dt.date
    eval_request = EvaluationRequest(
        factor_id="pending",
        universe_id=f"{args.data_source}_us_equity",
        eval_start=ts_dates.min(),
        eval_end=ts_dates.max(),
        label=LabelDefinition(
            forecast_horizon_bars=5, lag_bars=1, return_type="simple",
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

    # ── 7. Adapter.run_theme ──────────────────────────────────────────────
    adapter = HarnessAgentAdapter(
        orchestrator=orchestrator,
        eval_request=eval_request,
        experiment_registry=registries.experiments,
        proposer=proposer,
        refinement_runner=refinement_runner,
    )

    logger.info("Dispatching theme %r to HarnessAgentAdapter.", args.theme)
    response = adapter.run_theme(ThemeCycleRequest(
        theme=args.theme,
        n_candidates=args.n_candidates,
        extra_guidance=args.extra_guidance,
        tags=["autonomous_cycle"],
    ))

    # ── 8. Output ─────────────────────────────────────────────────────────
    if args.json:
        print(response.model_dump_json(indent=2))
    else:
        _print_summary(response)

    return 0


def _print_summary(response: ThemeCycleResponse) -> None:
    border = "=" * 72
    print(f"\n{border}")
    print("  AUTONOMOUS CYCLE RESULT")
    print(border)
    print(f"  Theme               : {response.theme}")
    print(f"  Proposals requested : {response.proposals_requested}")
    print(f"  Proposals accepted  : {response.proposals_accepted}")
    print(f"  Proposals dropped   : {response.proposals_dropped}")
    print(f"  Root cycles         : {len(response.roots)}")
    print(f"  Refinement cycles   : {len(response.refinements)}")
    print(f"  Total cycles        : {response.total_cycles}")

    if response.dropped_reasons:
        print("\n  Dropped proposal reasons:")
        for reason in response.dropped_reasons:
            print(f"    - {reason}")

    def _show(section: str, rows: list[ResearchCycleResponse]) -> None:
        if not rows:
            return
        print(f"\n  {section}:")
        for r in rows:
            ic = f"{r.ic:.4f}" if r.ic is not None else "   n/a"
            ric = f"{r.rank_ic:.4f}" if r.rank_ic is not None else "   n/a"
            print(
                f"    [{r.outcome.value:<8}] {r.factor_name:<32} "
                f"ic={ic} rank_ic={ric}",
            )

    _show("Roots", list(response.roots))
    _show("Refinements", list(response.refinements))
    print(f"{border}\n")

    # Also emit a compact JSON blob on a single line to stderr-free stdout so
    # downstream scripts can capture it without parsing the pretty header.
    logger.info(
        "theme_response_summary=%s",
        json.dumps({
            "theme": response.theme,
            "total_cycles": response.total_cycles,
            "proposals_accepted": response.proposals_accepted,
            "proposals_dropped": response.proposals_dropped,
        }),
    )


if __name__ == "__main__":
    sys.exit(main())
