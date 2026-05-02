#!/usr/bin/env python3
"""Re-run refinement on a previously promoted factor under a new regime.

This is the operator surface for Round 4G's trail-aware refinement
guard.  Workflow:

    1. Load the per-factor JSON from --promoted-dir.
    2. Rehydrate it into an ExperimentRecord (carrying the original
       promotion_trail).
    3. Build an EvaluationRequest from the CLI flags — *this is the
       new regime*.
    4. Hand the seed to RefinementRunner.refine_record().  When the
       seed's trail matches the new regime, the runner skips
       refinement and reports it; otherwise it expands deterministic
       mutations under the new rules.

No LLM is invoked — the refinement engine uses syntactic mutations
(:mod:`alpha_harness.orchestrator.mutations`).  Synthetic price data
is generated locally so the script needs no API keys.

Usage::

    uv run python -m scripts.refine_factor --factor-id <id>
    uv run python -m scripts.refine_factor --factor-id <id> --cost-bps 5
    uv run python -m scripts.refine_factor --factor-id <id> --json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

import pandas as pd

from alpha_harness.artifacts import (
    DEFAULT_PROMOTED_DIR,
    read_artifact,
    record_from_payload,
)
from alpha_harness.data.synthetic import generate_price_panel
from alpha_harness.evaluators.promotion_judge import PromotionJudge
from alpha_harness.evaluators.signal_quality import SignalQualityEvaluator
from alpha_harness.factors.compiler import FactorDslCompiler
from alpha_harness.orchestrator.refinement import (
    RefinementConfig,
    RefinementRunner,
    trail_status,
)
from alpha_harness.orchestrator.research_loop import ResearchOrchestrator
from alpha_harness.registries.experiment import ExperimentRegistry
from alpha_harness.registries.hypothesis import HypothesisRegistry
from alpha_harness.schemas.evaluation import (
    EvaluationProfile,
    EvaluationRequest,
    HoldoutPolicy,
    HoldoutStrategy,
    LabelDefinition,
    NeutralizeMode,
)
from alpha_harness.service import AlphaHarnessService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger("refine_factor")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Re-run refinement on a promoted factor under a new evaluation "
            "regime.  Skips refinement when the new trail_id matches the "
            "seed's, expands deterministic mutations otherwise."
        ),
    )
    p.add_argument(
        "--factor-id",
        required=True,
        help="The factor_id from artifacts/promoted/<id>.json.",
    )
    p.add_argument(
        "--promoted-dir",
        default=str(DEFAULT_PROMOTED_DIR),
        help=f"Directory holding per-factor JSONs (default: {DEFAULT_PROMOTED_DIR}).",
    )

    # Synthetic data — the same knobs autonomous_cycle exposes.
    p.add_argument("--n-days", type=int, default=180)
    p.add_argument("--n-symbols", type=int, default=10)
    p.add_argument("--seed", type=int, default=42)

    # Evaluator regime — change ANY of these and the trail_id flips.
    p.add_argument("--ic-threshold", type=float, default=0.02)
    p.add_argument("--cost-bps", type=float, default=0.0)
    p.add_argument(
        "--neutralize",
        choices=[m.value for m in NeutralizeMode],
        default=NeutralizeMode.NONE.value,
    )
    p.add_argument("--holdout-fraction", type=float, default=0.0)
    p.add_argument(
        "--holdout-strategy",
        choices=["none", "tail"],
        default="tail",
    )

    # Refinement budgets
    p.add_argument("--max-refinement-rounds", type=int, default=1)
    p.add_argument("--max-variants-per-step", type=int, default=2)
    p.add_argument("--max-total-children", type=int, default=4)

    p.add_argument(
        "--json",
        action="store_true",
        help="Emit the structured summary as JSON instead of a text block.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    # ── 1. Load the seed artifact ───────────────────────────────────────
    payload = read_artifact(args.factor_id, args.promoted_dir)
    if payload is None:
        print(
            f"error: no artifact for factor_id={args.factor_id!r} in {args.promoted_dir}",
            file=sys.stderr,
        )
        return 2
    seed = record_from_payload(payload)
    logger.info(
        "Loaded seed factor %s (expression=%s); seed trail_id=%s",
        seed.factor.id,
        seed.factor.expression,
        seed.promotion_trail.trail_id if seed.promotion_trail else "(none)",
    )

    # ── 2. Synthetic data + evaluator ───────────────────────────────────
    price_data = generate_price_panel(
        n_days=args.n_days,
        symbols=[f"S{i}" for i in range(args.n_symbols)],
        seed=args.seed,
    )
    ts_dates = pd.to_datetime(price_data["timestamp"]).dt.date

    # ── 3. Build the new evaluation regime ──────────────────────────────
    judge_thresholds = {
        "refine_margin": 0.20,
        "min_fraction_positive_folds": 0.6,
        "max_tail_concentration": 0.5,
        "min_holdout_decay_ratio": 0.5,
    }
    eval_request = EvaluationRequest(
        factor_id=seed.factor.id,
        universe_id="synthetic_us_equity",
        eval_start=ts_dates.min(),
        eval_end=ts_dates.max(),
        label=LabelDefinition(forecast_horizon_bars=5, lag_bars=1),
        profile=EvaluationProfile(
            thresholds={
                "ic": args.ic_threshold,
                "rank_ic": args.ic_threshold,
                "quantile_spread": 0.001,
            },
            min_periods=20,
            min_assets=3,
            n_quantiles=5,
        ),
        neutralize=NeutralizeMode(args.neutralize),
        cost_bps=args.cost_bps,
        holdout=HoldoutPolicy(
            strategy=HoldoutStrategy(args.holdout_strategy),
            holdout_fraction=args.holdout_fraction,
        ),
    )

    # ── 4. Wire up the runner ───────────────────────────────────────────
    service = AlphaHarnessService(
        compiler=FactorDslCompiler(),
        evaluator=SignalQualityEvaluator(price_data),
        judge=PromotionJudge(**judge_thresholds),
    )
    orch = ResearchOrchestrator(
        service=service,
        experiment_registry=ExperimentRegistry(),
        hypothesis_registry=HypothesisRegistry(),
    )
    runner = RefinementRunner(
        orch,
        config=RefinementConfig(
            max_depth=args.max_refinement_rounds,
            max_variants_per_step=args.max_variants_per_step,
            max_total_children=args.max_total_children,
        ),
        judge_thresholds=judge_thresholds,
    )

    # ── 5. Drive the trail-aware guard ──────────────────────────────────
    result = runner.refine_record(seed, eval_request)
    status = trail_status(seed, result.current_trail_id)

    summary = {
        "factor_id": seed.factor.id,
        "seed_trail_id": (
            seed.promotion_trail.trail_id if seed.promotion_trail is not None else None
        ),
        "current_trail_id": result.current_trail_id,
        "trail_status": status,
        "regime_skips": result.regime_skips,
        "trail_mismatches": result.trail_mismatches,
        "n_children": len(result.children),
        "child_decisions": [c.decision.value for c in result.children],
    }

    if args.json:
        print(json.dumps(summary, indent=2, default=str))
    else:
        _print_summary(summary)
    return 0


def _print_summary(summary: dict[str, object]) -> None:
    border = "=" * 72
    print(f"\n{border}")
    print("  REFINEMENT RESULT")
    print(border)
    print(f"  factor_id        : {summary['factor_id']}")
    print(f"  seed trail_id    : {summary['seed_trail_id']}")
    print(f"  current trail_id : {summary['current_trail_id']}")
    print(f"  trail status     : {summary['trail_status']}")
    if summary["regime_skips"]:
        for fid, reason in summary["regime_skips"]:  # type: ignore[misc]
            print(f"  regime-skip      : {fid}  ({reason})")
    if summary["trail_mismatches"]:
        for expr, parent, current in summary["trail_mismatches"]:  # type: ignore[misc]
            print(
                f"  trail-mismatch   : {expr}  parent={parent} current={current}",
            )
    print(f"  children run     : {summary['n_children']}")
    if summary["child_decisions"]:
        for d in summary["child_decisions"]:  # type: ignore[union-attr]
            print(f"    - {d}")
    print(f"{border}\n")


if __name__ == "__main__":
    sys.exit(main())
