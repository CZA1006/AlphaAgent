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
import uuid
from datetime import UTC, date, datetime

import pandas as pd

from alpha_harness.artifacts import (
    DEFAULT_PROMOTED_DIR,
    DEFAULT_TRAIL_DIR,
    PromotedArtifactWriter,
    TrailRegistryWriter,
)
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
from alpha_harness.llm import (
    BudgetedLLMClient,
    BudgetExceededError,
    LLMCallLogger,
    LLMClient,
    LoggingLLMClient,
    MockLLMClient,
    TokenBudget,
    default_log_path,
)
from alpha_harness.orchestrator.refinement import RefinementConfig, RefinementRunner
from alpha_harness.orchestrator.research_loop import ResearchOrchestrator
from alpha_harness.proposer import HypothesisProposer
from alpha_harness.proposer.memory import (
    DEFAULT_MEMORY_DEPTH,
    build_memory_digest,
)
from alpha_harness.proposer.schemas import RawProposal, RawProposalBatch
from alpha_harness.registries.factory import build_registries
from alpha_harness.reports import (
    DEFAULT_REPORT_DIR,
    CycleReportWriter,
    build_cycle_report,
)
from alpha_harness.reports.cycle_report import snapshot_budget
from alpha_harness.schemas.evaluation import (
    EvaluationProfile,
    EvaluationRequest,
    HoldoutPolicy,
    HoldoutStrategy,
    LabelDefinition,
    NeutralizeMode,
)
from alpha_harness.service import AlphaHarnessService, FactorEvaluator

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
    p.add_argument(
        "--data-path", default="data/silver/equities", help="Base path for --data-source parquet."
    )
    p.add_argument(
        "--symbols",
        default=None,
        help="Comma-separated tickers for parquet/polygon sources.",
    )
    p.add_argument(
        "--start-date", default="2024-07-01", help="Start date for real data (YYYY-MM-DD)."
    )
    p.add_argument("--end-date", default="2024-12-31", help="End date for real data (YYYY-MM-DD).")
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

    # Evaluator richness (Round 4A.3)
    p.add_argument(
        "--neutralize",
        choices=[m.value for m in NeutralizeMode],
        default=NeutralizeMode.NONE.value,
        help=(
            "Cross-sectional neutralization applied to forward returns. "
            "'sector' demeans by (date, sector); 'beta' subtracts "
            "beta*universe_mean; 'both' stacks them.  Default: none."
        ),
    )
    p.add_argument(
        "--sector-map",
        default=None,
        help=(
            "Path to a {symbol,sector} CSV (e.g. "
            "configs/universes/sp50_sectors.csv).  Required for "
            "--neutralize {sector,both}."
        ),
    )
    p.add_argument(
        "--cost-bps",
        type=float,
        default=0.0,
        help=(
            "Round-trip trading cost in basis points applied via turnover "
            "to produce net_quantile_spread.  Default: 0."
        ),
    )
    p.add_argument(
        "--extra-horizons",
        default="",
        help=(
            "Comma-separated extra forward horizons (bars) to evaluate "
            "alongside the primary 5-bar horizon, e.g. '1,20'.  When set, "
            "the judge also enforces IC-sign consistency across horizons."
        ),
    )

    # Walk-forward (Round 4B)
    p.add_argument(
        "--walk-forward",
        action="store_true",
        help=(
            "Wrap the evaluator with WalkForwardEvaluator: split the eval "
            "window into rolling folds and require fraction_positive_rank_ic "
            ">= 0.6 for promotion.  Off by default — single-window eval."
        ),
    )
    p.add_argument(
        "--n-folds",
        type=int,
        default=4,
        help="Walk-forward fold count (default: 4).",
    )
    p.add_argument(
        "--fold-size-days",
        type=int,
        default=60,
        help="Walk-forward per-fold window in calendar days (default: 60).",
    )
    p.add_argument(
        "--step-days",
        type=int,
        default=20,
        help="Walk-forward stride between fold starts (default: 20).",
    )

    # Holdout reservation (Round 4E)
    p.add_argument(
        "--holdout-fraction",
        type=float,
        default=0.0,
        help=(
            "Reserve the trailing fraction of [eval_start, eval_end] as a "
            "holdout slice; primary metrics use the in-sample remainder, "
            "and the judge cross-checks rank-IC sign + decay.  0 disables."
        ),
    )
    p.add_argument(
        "--holdout-strategy",
        choices=["none", "tail"],
        default="tail",
        help=(
            "How the holdout is carved (default: tail).  Only meaningful "
            "when --holdout-fraction > 0."
        ),
    )

    # Promotion artifacts (Round 4A.5)
    p.add_argument(
        "--promoted-dir",
        default=str(DEFAULT_PROMOTED_DIR),
        help=(
            f"Directory for per-promotion JSON artifacts + index (default: {DEFAULT_PROMOTED_DIR})."
        ),
    )
    p.add_argument(
        "--no-promoted-artifacts",
        action="store_true",
        help="Skip writing promotion artifacts even when a factor is promoted.",
    )

    # Trail registry (Round 4J)
    p.add_argument(
        "--trail-dir",
        default=None,
        help="Directory for the standalone trail registry (default: artifacts/trails).",
    )
    p.add_argument(
        "--no-trail-registry",
        action="store_true",
        help="Skip writing trail-registry rows alongside promotion artifacts.",
    )

    # Cycle reports (Round 4A.8)
    p.add_argument(
        "--report-dir",
        default=None,
        help=("Directory for per-cycle audit reports + index (default: artifacts/reports)."),
    )
    p.add_argument(
        "--no-report",
        action="store_true",
        help="Skip writing the cycle audit report.",
    )

    # Memory (Round 4A.4)
    p.add_argument(
        "--memory-depth",
        type=int,
        default=DEFAULT_MEMORY_DEPTH,
        help=(
            f"Number of most-recent experiments summarized into the "
            f"proposer memory digest (default: {DEFAULT_MEMORY_DEPTH}).  "
            "Only meaningful when the registry has prior entries "
            "(i.e. --backend sql or a reused in-memory process)."
        ),
    )
    p.add_argument(
        "--no-memory",
        action="store_true",
        help=(
            "Disable the memory digest even when prior experiments exist. "
            "Useful for A/B comparisons against memory-aware runs."
        ),
    )

    # Refinement budgets
    p.add_argument(
        "--max-refinement-rounds",
        "--max-depth",
        dest="max_refinement_rounds",
        type=int,
        default=1,
        help=(
            "Hard cap on refinement depth (root = 0).  Kept as "
            "--max-depth for backwards compatibility."
        ),
    )
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

    # Guardrails (Round 4A.1) — budget + call logging.
    p.add_argument(
        "--token-budget",
        type=int,
        default=None,
        help=(
            "Hard cap on cumulative total_tokens for this cycle. "
            "Falls back to ALPHA_AGENT_TOKEN_BUDGET.  Unset = no cap."
        ),
    )
    p.add_argument(
        "--cost-budget-usd",
        type=float,
        default=None,
        help=(
            "Hard cap on cumulative LLM cost in USD.  Falls back to "
            "ALPHA_AGENT_COST_BUDGET_USD.  Requires cost-per-1k env vars "
            "to be meaningful: ALPHA_AGENT_PROMPT_COST_PER_1K / "
            "ALPHA_AGENT_COMPLETION_COST_PER_1K."
        ),
    )
    p.add_argument(
        "--llm-log-dir",
        default=None,
        help=(
            "Directory for per-cycle LLM call JSONL logs.  Falls back to "
            "ALPHA_AGENT_LLM_LOG_DIR, then artifacts/llm_calls/."
        ),
    )
    p.add_argument(
        "--cycle-id",
        default=None,
        help="Override the auto-generated cycle id (used in the log filename).",
    )

    # Output
    p.add_argument(
        "--json",
        action="store_true",
        help="Print the ThemeCycleResponse as JSON instead of formatted text.",
    )
    return p


# ── Entry point ─────────────────────────────────────────────────────────────


def _resolve_token_budget(args: argparse.Namespace) -> TokenBudget | None:
    """Build a :class:`TokenBudget` from CLI + env, or ``None`` if no cap is set."""
    token_cap = args.token_budget
    if token_cap is None:
        raw = os.environ.get("ALPHA_AGENT_TOKEN_BUDGET", "").strip()
        token_cap = int(raw) if raw else None

    cost_cap = args.cost_budget_usd
    if cost_cap is None:
        raw = os.environ.get("ALPHA_AGENT_COST_BUDGET_USD", "").strip()
        cost_cap = float(raw) if raw else None

    if token_cap is None and cost_cap is None:
        return None

    prompt_rate = float(os.environ.get("ALPHA_AGENT_PROMPT_COST_PER_1K", "0") or "0")
    completion_rate = float(os.environ.get("ALPHA_AGENT_COMPLETION_COST_PER_1K", "0") or "0")
    return TokenBudget(
        max_total_tokens=token_cap,
        max_cost_usd=cost_cap,
        prompt_cost_per_1k=prompt_rate,
        completion_cost_per_1k=completion_rate,
    )


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    # Cycle id drives the LLM call log filename.  Stable across the cycle.
    cycle_id: str = args.cycle_id or f"cycle-{uuid.uuid4().hex[:12]}"
    logger.info("Cycle id: %s", cycle_id)

    # Capture wall-clock start before any I/O so the cycle report's
    # duration covers data load, LLM calls, evaluation, and persistence.
    started_at = datetime.now(UTC)

    # ── 1. Data ────────────────────────────────────────────────────────────
    if args.data_source in ("parquet", "polygon"):
        from alpha_harness.data.loader_factory import create_equities_loader
        from alpha_harness.data.models import DataRequest

        symbols_list = args.symbols.split(",") if args.symbols else ["AAPL", "MSFT", "GOOG"]
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
                args.data_source,
                symbols_list,
                start,
                end,
            )
            return 1
        logger.info(
            "Loaded %d bars for %d symbols from %s",
            meta.bars_returned,
            meta.symbols_returned,
            args.data_source,
        )
    else:
        symbols: list[str] | None = None
        if args.symbols:
            symbols = args.symbols.split(",")
        elif args.n_symbols != 10:
            symbols = [f"SYM_{i:02d}" for i in range(args.n_symbols)]
        price_data = generate_price_panel(
            symbols=symbols,
            n_days=args.n_days,
            seed=args.seed,
        )
        logger.info(
            "Generated synthetic panel: %d symbols x %d dates",
            price_data["symbol"].nunique(),
            price_data["timestamp"].nunique(),
        )

    # ── 2. Deterministic core (compiler + evaluator + judge) ──────────────
    compiler = FactorDslCompiler()
    evaluator: FactorEvaluator = SignalQualityEvaluator(price_data)
    if args.walk_forward:
        from alpha_harness.evaluators.walk_forward import (
            WalkForwardConfig,
            WalkForwardEvaluator,
        )

        evaluator = WalkForwardEvaluator(
            evaluator,
            config=WalkForwardConfig(
                n_folds=args.n_folds,
                fold_size_days=args.fold_size_days,
                step_days=args.step_days,
            ),
        )
        logger.info(
            "Walk-forward enabled: n_folds=%d fold_size_days=%d step_days=%d",
            args.n_folds,
            args.fold_size_days,
            args.step_days,
        )
    judge = PromotionJudge(refine_margin=0.20)
    service = AlphaHarnessService(
        compiler=compiler,
        evaluator=evaluator,
        judge=judge,
    )

    # ── 3. Registries (memory by default, SQL opt-in) ─────────────────────
    backend_config = BackendConfig.from_env(override=args.backend)
    logger.info("Using %s backend.", backend_config.backend)
    registries = build_registries(backend_config)

    # ── 4. Orchestrator + refinement runner ───────────────────────────────
    trail_registry: TrailRegistryWriter | None = None
    if not args.no_trail_registry:
        trail_registry = TrailRegistryWriter(
            base_dir=args.trail_dir or str(DEFAULT_TRAIL_DIR),
        )
    artifact_writer: PromotedArtifactWriter | None = None
    if not args.no_promoted_artifacts:
        artifact_writer = PromotedArtifactWriter(
            base_dir=args.promoted_dir,
            cycle_id=cycle_id,
            trail_registry=trail_registry,
        )
    orchestrator = ResearchOrchestrator(
        service=service,
        experiment_registry=registries.experiments,
        hypothesis_registry=registries.hypotheses,
        memory_registry=registries.memories,
        artifact_writer=artifact_writer,
    )
    refinement_runner = RefinementRunner(
        orchestrator,
        config=RefinementConfig(
            max_depth=args.max_refinement_rounds,
            max_variants_per_step=args.max_variants_per_step,
            max_total_children=args.max_total_children,
        ),
        # Round 4G — let the runner compute the current trail_id so any
        # seeded refinement (refine_record) can detect regime drift.
        judge_thresholds={
            "refine_margin": 0.20,
            "min_fraction_positive_folds": 0.6,
            "max_tail_concentration": 0.5,
            "min_holdout_decay_ratio": 0.5,
        },
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

    # Round 4A.1 guardrails — applied to every real *and* mock path so
    # every cycle writes a call log and every cycle can be budget-capped.
    log_path = default_log_path(cycle_id, args.llm_log_dir)
    call_logger = LLMCallLogger(path=log_path, cycle_id=cycle_id)
    logger.info("LLM call log: %s", log_path)
    llm_client = LoggingLLMClient(
        llm_client,
        call_logger,
        purpose="autonomous_cycle",
    )
    budget = _resolve_token_budget(args)
    if budget is not None:
        logger.info(
            "Token budget: max_tokens=%s max_cost_usd=%s",
            budget.max_total_tokens,
            budget.max_cost_usd,
        )
        llm_client = BudgetedLLMClient(llm_client, budget)

    proposer = HypothesisProposer(llm_client=llm_client, compiler=compiler)

    # ── 6. Evaluation request ─────────────────────────────────────────────
    ts_dates = pd.to_datetime(price_data["timestamp"]).dt.date

    # Extra forecast horizons for sign-consistency checks.
    extra_horizons: list[int] = []
    if args.extra_horizons.strip():
        try:
            extra_horizons = [int(h) for h in args.extra_horizons.split(",") if h.strip()]
        except ValueError:
            logger.error("--extra-horizons must be a comma list of ints.")
            return 2

    # Sector map (only meaningful when --neutralize uses sector info).
    sector_map: dict[str, str] = {}
    if args.sector_map:
        from pathlib import Path

        sm_path = Path(args.sector_map)
        if not sm_path.is_file():
            logger.error("--sector-map file not found: %s", sm_path)
            return 2
        sm_df = pd.read_csv(sm_path, comment="#")
        if {"symbol", "sector"} - set(sm_df.columns):
            logger.error("--sector-map CSV must have 'symbol' and 'sector' columns.")
            return 2
        sector_map = dict(
            zip(sm_df["symbol"].astype(str), sm_df["sector"].astype(str), strict=False)
        )

    eval_request = EvaluationRequest(
        factor_id="pending",
        universe_id=f"{args.data_source}_us_equity",
        eval_start=ts_dates.min(),
        eval_end=ts_dates.max(),
        label=LabelDefinition(
            forecast_horizon_bars=5,
            lag_bars=1,
            return_type="simple",
            extra_horizons=extra_horizons,
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
        neutralize=NeutralizeMode(args.neutralize),
        sector_map=sector_map,
        cost_bps=args.cost_bps,
        holdout=HoldoutPolicy(
            strategy=HoldoutStrategy(args.holdout_strategy),
            holdout_fraction=args.holdout_fraction,
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

    # ── 6b. Build the rolling-memory digest (Round 4A.4) ─────────────────
    if args.no_memory:
        prior_memory = ""
    else:
        recent = registries.experiments.list_recent(limit=args.memory_depth)
        prior_memory = build_memory_digest(recent, depth=args.memory_depth)
        if prior_memory:
            logger.info(
                "Memory digest: %d chars from %d prior experiments.",
                len(prior_memory),
                len(recent),
            )

    logger.info("Dispatching theme %r to HarnessAgentAdapter.", args.theme)
    try:
        response = adapter.run_theme(
            ThemeCycleRequest(
                theme=args.theme,
                n_candidates=args.n_candidates,
                extra_guidance=args.extra_guidance,
                tags=["autonomous_cycle"],
                prior_memory=prior_memory,
            )
        )
    except BudgetExceededError as exc:
        logger.error("Cycle halted by budget guard: %s", exc)
        print(
            f"error: cycle halted — {exc}\n"
            f"       see LLM call log at {log_path} for per-call detail.",
            file=sys.stderr,
        )
        return 3

    # ── 7b. Cycle report (Round 4A.8) ─────────────────────────────────────
    if not args.no_report:
        experiment_ids = [r.experiment_id for r in response.roots]
        experiment_ids.extend(r.experiment_id for r in response.refinements)
        report = build_cycle_report(
            cycle_id=cycle_id,
            theme=args.theme,
            started_at=started_at,
            experiment_registry=registries.experiments,
            experiment_ids=experiment_ids,
            budget=snapshot_budget(budget),
            llm_log_path=str(log_path),
        )
        report_dir = args.report_dir or str(DEFAULT_REPORT_DIR)
        path = CycleReportWriter(base_dir=report_dir).write(report)
        if path is not None:
            logger.info("cycle report: %s", path)

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
                f"    [{r.outcome.value:<8}] {r.factor_name:<32} ic={ic} rank_ic={ric}",
            )

    _show("Roots", list(response.roots))
    _show("Refinements", list(response.refinements))
    print(f"{border}\n")

    # Also emit a compact JSON blob on a single line to stderr-free stdout so
    # downstream scripts can capture it without parsing the pretty header.
    logger.info(
        "theme_response_summary=%s",
        json.dumps(
            {
                "theme": response.theme,
                "total_cycles": response.total_cycles,
                "proposals_accepted": response.proposals_accepted,
                "proposals_dropped": response.proposals_dropped,
            }
        ),
    )


if __name__ == "__main__":
    sys.exit(main())
