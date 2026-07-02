#!/usr/bin/env python3
"""Round 5 — strict-regime validation harness.

Drives the autonomous-cycle stack against ``StrictRegime`` (every
robustness gate from Rounds 4A.3, 4B, 4C, 4D, 4E enabled with
production-grade thresholds) and reports how each candidate fared.

The script is intentionally information-only: exit code is always 0
unless a precondition (missing universe file, no data) fails.  The
return-shaped value of a strict run is the report itself —
``artifacts/validations/{cycle_id}.json`` plus the human-readable
summary printed to stdout.

Usage::

    # Synthetic data (no API keys)
    uv run python -m scripts.validate_strict --data-source synthetic --n-days 240

    # Local Parquet (after `make backfill-sp50`)
    uv run python -m scripts.validate_strict \\
        --data-source parquet --universe configs/universes/sp50.txt \\
        --start-date 2024-01-01 --end-date 2024-12-31

    # Live Polygon (requires POLYGON_API_KEY)
    uv run python -m scripts.validate_strict \\
        --data-source polygon --universe configs/universes/sp50.txt \\
        --start-date 2024-01-01 --end-date 2024-12-31
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

from alpha_harness.artifacts import (
    DEFAULT_PROMOTED_DIR,
    DEFAULT_TRAIL_DIR,
    PromotedArtifactWriter,
    TrailRegistryWriter,
)
from alpha_harness.data.synthetic import generate_price_panel
from alpha_harness.evaluators.promotion_judge import PromotionJudge
from alpha_harness.evaluators.signal_quality import SignalQualityEvaluator
from alpha_harness.evaluators.walk_forward import WalkForwardEvaluator
from alpha_harness.factors.compiler import FactorDslCompiler
from alpha_harness.hermes_boundary.contracts import ThemeCycleRequest
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
from alpha_harness.proposer.memory import DEFAULT_MEMORY_DEPTH, build_memory_digest
from alpha_harness.proposer.schemas import RawProposal, RawProposalBatch
from alpha_harness.regimes import StrictRegime, get_regime
from alpha_harness.registries.experiment import ExperimentRegistry
from alpha_harness.registries.hypothesis import HypothesisRegistry
from alpha_harness.reports.validation import (
    DEFAULT_VALIDATION_DIR,
    StrictValidationReportWriter,
    build_validation_report,
)
from alpha_harness.schemas.evaluation import EvaluationRequest
from alpha_harness.schemas.experiment import PromotionTrail
from alpha_harness.service import AlphaHarnessService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger("validate_strict")


# ── Default mock candidates (synthetic-data path) ───────────────────────────


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
        expression="zscore(ts_mean(volume, 10))",
        rationale="10-day mean volume, standardised.",
        tags=["volume"],
    ),
    RawProposal(
        expression="rank(volume)",
        rationale="Attention proxy — higher volume → higher rank.",
        tags=["volume"],
    ),
]

_HK_IPO_EVENT_MOCK_CANDIDATES: list[RawProposal] = [
    RawProposal(
        expression="rank(ofi) * is_pre_greenshoe_expiry_5d",
        rationale=(
            "Net buying immediately before greenshoe expiry may identify IPOs "
            "where support is ending without sell pressure."
        ),
        tags=["hk_ipo", "greenshoe", "ofi"],
    ),
    RawProposal(
        expression="rank(ts_mean(ofi, 3)) * (1 - rank(rel_spread))",
        rationale=(
            "Persistent positive order flow in tighter-spread IPOs should be "
            "more implementable than raw OFI alone."
        ),
        tags=["hk_ipo", "microstructure", "liquidity"],
    ),
    RawProposal(
        expression="is_stabilization_window_active * (rank(ofi) - rank(rel_spread))",
        rationale=(
            "During stabilization windows, positive OFI combined with tight "
            "spreads may proxy for orderly demand rather than noisy trading."
        ),
        tags=["hk_ipo", "stabilization", "ofi"],
    ),
    RawProposal(
        expression="is_pre_cornerstone_lockup_5d * (rank(rel_spread) - rank(ofi))",
        rationale=(
            "Before cornerstone lockup expiry, widening spreads and weak OFI "
            "may capture anticipated unlock selling pressure."
        ),
        tags=["hk_ipo", "cornerstone_lockup", "selling_pressure"],
    ),
    RawProposal(
        expression="rank(-days_to_next_cornerstone_lockup) * rank(-rel_spread)",
        rationale=(
            "IPO names closer to cornerstone unlock with better liquidity may "
            "price event risk more efficiently."
        ),
        tags=["hk_ipo", "cornerstone_lockup", "liquidity"],
    ),
]


def _mock_candidates_for_preset(preset: str) -> list[RawProposal]:
    if preset == "default":
        return _MOCK_CANDIDATES
    if preset == "hk_ipo_events":
        return _HK_IPO_EVENT_MOCK_CANDIDATES
    raise ValueError(f"unknown mock preset: {preset}")


def _make_mock_llm(n: int, *, preset: str) -> LLMClient:
    candidates = _mock_candidates_for_preset(preset)
    payload = RawProposalBatch(proposals=candidates[:n]).model_dump_json()
    return MockLLMClient(handler=lambda _req: payload)


def _resolve_token_budget(args: argparse.Namespace) -> TokenBudget | None:
    """Build a :class:`TokenBudget` from CLI + env, or ``None`` when uncapped.

    Mirrors the helper in ``scripts.autonomous_cycle`` so both real-LLM
    paths share the same budget contract (Round 4A.1).
    """
    import os as _os

    token_cap = args.token_budget
    if token_cap is None:
        raw = _os.environ.get("ALPHA_AGENT_TOKEN_BUDGET", "").strip()
        token_cap = int(raw) if raw else None

    cost_cap = args.cost_budget_usd
    if cost_cap is None:
        raw = _os.environ.get("ALPHA_AGENT_COST_BUDGET_USD", "").strip()
        cost_cap = float(raw) if raw else None

    if token_cap is None and cost_cap is None:
        return None

    prompt_rate = float(_os.environ.get("ALPHA_AGENT_PROMPT_COST_PER_1K", "0") or "0")
    completion_rate = float(_os.environ.get("ALPHA_AGENT_COMPLETION_COST_PER_1K", "0") or "0")
    return TokenBudget(
        max_total_tokens=token_cap,
        max_cost_usd=cost_cap,
        prompt_cost_per_1k=prompt_rate,
        completion_cost_per_1k=completion_rate,
    )


def _build_llm_client(args: argparse.Namespace, *, cycle_id: str) -> LLMClient:
    """Construct the proposer's LLM stack: backend → log → budget.

    The mock path needs no keys; the openrouter path requires
    ``OPENROUTER_API_KEY`` and produces a budget-guarded, call-logged
    real client identical to the one ``autonomous_cycle`` uses.
    """
    import os as _os

    if args.llm == "mock":
        base: LLMClient = _make_mock_llm(
            args.n_candidates,
            preset=args.mock_preset,
        )
    else:
        if not _os.environ.get("OPENROUTER_API_KEY"):
            raise RuntimeError(
                "live LLM requested but OPENROUTER_API_KEY is not set. "
                "Re-run with --llm mock for an offline pass, or export "
                "OPENROUTER_API_KEY=... before invoking this script.",
            )
        from alpha_harness.llm import OpenRouterClient, OpenRouterConfig

        base = OpenRouterClient(OpenRouterConfig.from_env())

    log_path = default_log_path(cycle_id, args.llm_log_dir)
    logger.info("LLM call log: %s", log_path)
    call_logger = LLMCallLogger(path=log_path, cycle_id=cycle_id)
    wrapped: LLMClient = LoggingLLMClient(
        base,
        call_logger,
        purpose="validate_strict",
    )
    budget = _resolve_token_budget(args)
    if budget is not None:
        logger.info(
            "Token budget: max_tokens=%s max_cost_usd=%s",
            budget.max_total_tokens,
            budget.max_cost_usd,
        )
        wrapped = BudgetedLLMClient(wrapped, budget)
    return wrapped


# ── Data loading ────────────────────────────────────────────────────────────


def _load_universe(path: Path) -> list[str]:
    if not path.is_file():
        raise FileNotFoundError(f"universe file not found: {path}")
    symbols: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            symbols.append(s)
    return symbols


def _load_data(args: argparse.Namespace) -> pd.DataFrame:
    if args.data_source == "synthetic":
        return generate_price_panel(
            n_days=args.n_days,
            symbols=[f"S{i}" for i in range(args.n_symbols)],
            seed=args.seed,
        )

    # Real data path — universe + dates required.
    if not args.universe:
        raise ValueError(
            "--universe is required for --data-source parquet/polygon",
        )
    symbols = _load_universe(Path(args.universe))
    start = date.fromisoformat(args.start_date)
    end = date.fromisoformat(args.end_date)

    from alpha_harness.data.loader_factory import create_equities_loader
    from alpha_harness.data.models import BarFrequency, DataRequest

    loader = create_equities_loader(
        source=args.data_source,
        base_path=args.data_path,
    )
    request = DataRequest(
        symbols=symbols,
        start=start,
        end=end,
        frequency=BarFrequency.DAILY,
    )
    df, _meta = loader.load_bars(request)
    if df.empty:
        raise RuntimeError(
            f"loader returned empty frame for {len(symbols)} symbols between {start} and {end}",
        )
    return df


# ── CLI ─────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)

    p.add_argument(
        "--data-source",
        choices=["synthetic", "parquet", "polygon", "bigquery"],
        default="synthetic",
    )
    p.add_argument(
        "--data-path",
        default="data/silver/equities",
        help="Base path for --data-source parquet.",
    )
    p.add_argument(
        "--universe",
        default=None,
        help="Path to a newline-separated universe file (parquet/polygon only).",
    )
    p.add_argument("--start-date", default="2024-01-01")
    p.add_argument("--end-date", default="2024-12-31")
    p.add_argument("--n-days", type=int, default=240, help="Synthetic path only.")
    p.add_argument("--n-symbols", type=int, default=10, help="Synthetic path only.")
    p.add_argument("--seed", type=int, default=42, help="Synthetic path only.")

    p.add_argument(
        "--llm",
        choices=["mock", "openrouter"],
        default="mock",
        help=(
            "Which LLM client backs the proposer.  'mock' replays the "
            "hardcoded _MOCK_CANDIDATES and needs no keys.  'openrouter' "
            "calls the real OpenRouter API (requires OPENROUTER_API_KEY) — "
            "this is what tests the *agent*, not just the harness."
        ),
    )
    p.add_argument(
        "--mock-preset",
        choices=["default", "hk_ipo_events"],
        default="default",
        help=(
            "Which offline mock candidate set to use when --llm mock. "
            "'hk_ipo_events' exercises BigQuery microstructure + curated "
            "HKEX event fields without making an LLM call."
        ),
    )
    p.add_argument(
        "--n-candidates",
        type=int,
        default=5,
        help="How many hypotheses to draw from the proposer.",
    )

    # Budget guardrails (Round 4A.1) — apply to the real-LLM path.
    p.add_argument(
        "--token-budget",
        type=int,
        default=None,
        help="Hard cap on total tokens this cycle (also reads ALPHA_AGENT_TOKEN_BUDGET).",
    )
    p.add_argument(
        "--cost-budget-usd",
        type=float,
        default=None,
        help="Hard cap on $ cost this cycle (also reads ALPHA_AGENT_COST_BUDGET_USD).",
    )
    p.add_argument(
        "--llm-log-dir",
        default=None,
        help=(
            "Per-cycle LLM call-log directory "
            "(default: ALPHA_AGENT_LLM_LOG_DIR or artifacts/llm_calls)."
        ),
    )
    p.add_argument(
        "--extra-guidance",
        default="",
        help="Optional extra guidance text injected into the proposer prompt.",
    )
    p.add_argument(
        "--theme",
        default="cross-sectional equity signals derived from price and volume",
    )
    p.add_argument("--cycle-id", default=None)
    p.add_argument(
        "--regime",
        choices=["strict", "lenient"],
        default="strict",
        help=(
            "Named regime preset.  'strict' (default) uses production-grade "
            "thresholds; 'lenient' halves the IC / rank-IC bar to expose "
            "the deeper gates (walk-forward, tail concentration, holdout) "
            "to near-miss candidates."
        ),
    )

    # Round 4A.4 — proposer memory digest across multi-cycle runs.
    p.add_argument(
        "--n-cycles",
        type=int,
        default=1,
        help=(
            "Run N back-to-back cycles, sharing the experiment registry "
            "so the proposer's memory digest grows over time.  When N>1, "
            "--cycle-id becomes a prefix and each cycle gets a -<i> suffix."
        ),
    )
    p.add_argument(
        "--memory-depth",
        type=int,
        default=DEFAULT_MEMORY_DEPTH,
        help=(
            f"Most-recent experiments summarized into the proposer memory "
            f"digest before each cycle (default: {DEFAULT_MEMORY_DEPTH})."
        ),
    )
    p.add_argument(
        "--no-memory",
        action="store_true",
        help="Disable the memory digest even on multi-cycle runs.",
    )
    p.add_argument(
        "--validation-dir",
        default=str(DEFAULT_VALIDATION_DIR),
    )
    p.add_argument(
        "--promoted-dir",
        default=str(DEFAULT_PROMOTED_DIR),
    )
    p.add_argument("--trail-dir", default=str(DEFAULT_TRAIL_DIR))
    p.add_argument(
        "--no-write",
        action="store_true",
        help="Don't persist promoted-artifact / trail / validation files.",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit the report as JSON instead of a text block.",
    )
    return p


def _build_eval_request(
    *,
    regime: StrictRegime,
    factor_id: str,
    df: pd.DataFrame,
) -> EvaluationRequest:
    ts_dates = pd.to_datetime(df["timestamp"]).dt.date
    return EvaluationRequest(
        factor_id=factor_id,
        universe_id="strict",
        eval_start=ts_dates.min(),
        eval_end=ts_dates.max(),
        label=regime.label_definition(),
        profile=regime.evaluation_profile(),
        neutralize=regime.neutralize,
        cost_bps=regime.cost_bps,
        holdout=regime.holdout_policy(),
    )


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    cycle_id = args.cycle_id or f"strict-{uuid.uuid4().hex[:12]}"

    # ── Data ────────────────────────────────────────────────────────────
    try:
        price_data = _load_data(args)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # ── Build the orchestrator under StrictRegime ───────────────────────
    regime = get_regime(args.regime)
    logger.info(
        "Regime=%s ic>=%.4f rank_ic>=%.4f cost_bps=%.2f",
        args.regime, regime.ic_threshold, regime.rank_ic_threshold, regime.cost_bps,
    )
    judge_thresholds = regime.judge_thresholds()
    inner_evaluator = SignalQualityEvaluator(price_data)
    evaluator = WalkForwardEvaluator(inner_evaluator, regime.walk_forward_config())

    service = AlphaHarnessService(
        compiler=FactorDslCompiler(),
        evaluator=evaluator,
        judge=PromotionJudge(**judge_thresholds),
    )

    trail_registry: TrailRegistryWriter | None = None
    artifact_writer: PromotedArtifactWriter | None = None
    if not args.no_write:
        trail_registry = TrailRegistryWriter(args.trail_dir)
        artifact_writer = PromotedArtifactWriter(
            base_dir=args.promoted_dir,
            cycle_id=cycle_id,
            trail_registry=trail_registry,
        )

    experiments = ExperimentRegistry()
    orch = ResearchOrchestrator(
        service=service,
        experiment_registry=experiments,
        hypothesis_registry=HypothesisRegistry(),
        artifact_writer=artifact_writer,
    )
    runner = RefinementRunner(
        orch,
        config=RefinementConfig(
            max_depth=1,
            max_variants_per_step=2,
            max_total_children=3,
        ),
        judge_thresholds=judge_thresholds,
    )

    # ── Proposer (mock by default; --llm openrouter calls the real API) ──
    try:
        llm_client = _build_llm_client(args, cycle_id=cycle_id)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    proposer = HypothesisProposer(
        llm_client=llm_client,
        compiler=FactorDslCompiler(),
    )

    # ── Build the eval request once; HarnessAgentAdapter reuses it ──────
    eval_request = _build_eval_request(
        regime=regime,
        factor_id="pending",
        df=price_data,
    )
    adapter = HarnessAgentAdapter(
        orchestrator=orch,
        eval_request=eval_request,
        experiment_registry=experiments,
        proposer=proposer,
        refinement_runner=runner,
    )
    # ── Multi-cycle loop with proposer memory (Round 4A.4) ──────────────
    n_cycles = max(1, args.n_cycles)
    reports: list = []
    for i in range(n_cycles):
        sub_cycle_id = cycle_id if n_cycles == 1 else f"{cycle_id}-c{i + 1:02d}"
        sub_started = datetime.now(UTC)

        # Build memory digest from accumulated experiments BEFORE this cycle.
        if args.no_memory:
            prior_memory = ""
        else:
            recent = experiments.list_recent(limit=args.memory_depth)
            prior_memory = build_memory_digest(
                recent,
                depth=args.memory_depth,
                # Round 9 A.1: surface promoted composites from the
                # durable artifact index so the proposer sees baskets
                # promoted by previous `combine_factors --promote` runs
                # even when this cycle starts with a fresh registry.
                promoted_index_path=Path(args.promoted_dir) / "_index.jsonl",
            )
            if prior_memory:
                logger.info(
                    "Cycle %d/%d memory digest: %d chars from %d prior experiments",
                    i + 1,
                    n_cycles,
                    len(prior_memory),
                    len(recent),
                )

        try:
            adapter.run_theme(
                ThemeCycleRequest(
                    theme=args.theme,
                    n_candidates=args.n_candidates,
                    extra_guidance=args.extra_guidance,
                    tags=["validate_strict"],
                    prior_memory=prior_memory,
                ),
            )
        except BudgetExceededError as exc:
            logger.error("Cycle %d halted by budget guard: %s", i + 1, exc)
            print(f"error: cycle halted — {exc}", file=sys.stderr)
            return 3
        except Exception as exc:
            from alpha_harness.llm.openrouter import OpenRouterError

            if isinstance(exc, OpenRouterError):
                print(f"error: LLM call failed — {exc}", file=sys.stderr)
                return 4
            raise

        regime_trail = PromotionTrail.from_inputs(
            evaluation_request=eval_request,
            judge_thresholds=judge_thresholds,
            walk_forward={
                "n_folds": regime.n_folds,
                "fold_size_days": regime.fold_size_days,
                "step_days": regime.step_days,
                "embargo_days": regime.embargo_days,
            },
        )
        # Report only the records produced *in this sub-cycle* — the
        # registry accumulates, so we slice off everything that existed
        # before we entered the cycle.
        all_records = experiments.list_all()
        records_this_cycle = [r for r in all_records if r.created_at >= sub_started]
        report = build_validation_report(
            cycle_id=sub_cycle_id,
            regime_trail_id=regime_trail.trail_id,
            universe_id=eval_request.universe_id,
            started_at=sub_started,
            records=records_this_cycle,
        )
        if not args.no_write:
            StrictValidationReportWriter(args.validation_dir).write(report)
        reports.append(report)

    # ── Output ──────────────────────────────────────────────────────────
    if args.json:
        print(json.dumps([json.loads(r.model_dump_json()) for r in reports], indent=2))
    elif n_cycles == 1:
        _print_summary(reports[0])
    else:
        _print_multi_summary(reports)
    return 0


def _print_multi_summary(reports: list) -> None:
    """Compact one-line-per-cycle summary for ``--n-cycles N``."""
    border = "=" * 72
    print(f"\n{border}")
    print(f"  STRICT MULTI-CYCLE VALIDATION  ({len(reports)} cycles)")
    print(border)
    print(
        f"  {'cycle_id':<28}  {'prop':>4}  {'prom':>4}  "
        f"{'refi':>4}  {'rej':>4}  rejection breakdown"
    )
    print(f"  {'-' * 28}  {'-' * 4}  {'-' * 4}  {'-' * 4}  {'-' * 4}  {'-' * 18}")
    totals = {"proposals": 0, "promoted": 0, "refined": 0, "rejected": 0}
    cum_gates: dict[str, int] = {}
    for r in reports:
        breakdown = ", ".join(f"{k}={v}" for k, v in (r.n_rejected_by_gate or {}).items()) or "—"
        print(
            f"  {r.cycle_id[:28]:<28}  {r.n_proposals:>4}  "
            f"{r.n_promoted:>4}  {r.n_refined:>4}  {r.n_rejected:>4}  {breakdown}",
        )
        totals["proposals"] += r.n_proposals
        totals["promoted"] += r.n_promoted
        totals["refined"] += r.n_refined
        totals["rejected"] += r.n_rejected
        for k, v in (r.n_rejected_by_gate or {}).items():
            cum_gates[k] = cum_gates.get(k, 0) + v
    print(f"  {'-' * 28}  {'-' * 4}  {'-' * 4}  {'-' * 4}  {'-' * 4}  {'-' * 18}")
    print(
        f"  {'TOTAL':<28}  {totals['proposals']:>4}  {totals['promoted']:>4}  "
        f"{totals['refined']:>4}  {totals['rejected']:>4}",
    )
    print("\n  Cumulative rejection breakdown:")
    for gate, n in sorted(cum_gates.items(), key=lambda kv: -kv[1]):
        print(f"    {gate:<32} {n}")
    promoted_ids = [fid for r in reports for fid in r.promoted_factor_ids]
    if promoted_ids:
        print("\n  All promoted factor_ids:")
        for fid in promoted_ids:
            print(f"    - {fid}")
    print(f"{border}\n")


def _print_summary(report: object) -> None:
    border = "=" * 72
    print(f"\n{border}")
    print("  STRICT VALIDATION RESULT")
    print(border)
    print(f"  cycle_id          : {report.cycle_id}")  # type: ignore[attr-defined]
    print(f"  regime trail_id   : {report.regime_trail_id}")  # type: ignore[attr-defined]
    print(f"  universe_id       : {report.universe_id}")  # type: ignore[attr-defined]
    print(f"  proposals tried   : {report.n_proposals}")  # type: ignore[attr-defined]
    print(f"  promoted          : {report.n_promoted}")  # type: ignore[attr-defined]
    print(f"  refined           : {report.n_refined}")  # type: ignore[attr-defined]
    print(f"  rejected          : {report.n_rejected}")  # type: ignore[attr-defined]
    if report.n_rejected_by_gate:  # type: ignore[attr-defined]
        print("  rejection breakdown:")
        for gate, n in report.n_rejected_by_gate.items():  # type: ignore[attr-defined]
            print(f"    {gate:<32} {n}")
    if report.promoted_factor_ids:  # type: ignore[attr-defined]
        print("  promoted factor_ids:")
        for fid in report.promoted_factor_ids:  # type: ignore[attr-defined]
            print(f"    - {fid}")
    print(f"{border}\n")
    # Compact one-liner for grep / dashboards.
    logger.info(
        "strict_validation_summary=%s",
        json.dumps(
            {
                "cycle_id": report.cycle_id,  # type: ignore[attr-defined]
                "n_proposals": report.n_proposals,  # type: ignore[attr-defined]
                "n_promoted": report.n_promoted,  # type: ignore[attr-defined]
                "n_rejected": report.n_rejected,  # type: ignore[attr-defined]
            }
        ),
    )


if __name__ == "__main__":
    sys.exit(main())
