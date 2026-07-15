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
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

import pandas as pd

from alpha_harness.artifacts.promoted import (
    DEFAULT_PROMOTED_DIR,
    PromotedArtifactWriter,
)
from alpha_harness.artifacts.trail_registry import (
    DEFAULT_TRAIL_DIR,
    TrailRegistryWriter,
)
from alpha_harness.combination import (
    CombinationMethod,
    CombinationRecipe,
    combine_signals,
    compute_signal,
    pairwise_rank_corr,
)
from alpha_harness.data.fingerprint import dataframe_fingerprint
from alpha_harness.data.synthetic import generate_price_panel
from alpha_harness.evaluators.persistence import (
    PERSISTENCE_SCORE_VERSION,
    FactorSelectionStrategy,
    rank_by_persistence,
    score_from_walk_forward,
)
from alpha_harness.evaluators.promotion_judge import PromotionJudge
from alpha_harness.evaluators.signal_quality import evaluate_precomputed_signal
from alpha_harness.evaluators.walk_forward import WalkForwardEvaluator
from alpha_harness.multiple_testing import bonferroni_z_threshold_multiplier
from alpha_harness.regimes import StrictRegime, get_regime
from alpha_harness.reports import (
    DEFAULT_COMBINATION_DIR,
    CombinationReportWriter,
    FactorThumbnail,
    build_combination_report,
)
from alpha_harness.schemas.evaluation import EvaluationBundle, EvaluationRequest
from alpha_harness.schemas.experiment import (
    ExperimentDecision,
    ExperimentRecord,
    JudgmentDetail,
    PromotionTrail,
)
from alpha_harness.schemas.factor import FactorSpec
from alpha_harness.schemas.hypothesis import Hypothesis

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


# ── Precomputed-signal adapter (Round 7.1) ──────────────────────────────────


class _PrecomputedSignalEvaluator:
    """``FactorEvaluator`` that scores a precomputed signal series.

    Lets us run a basket signal through ``WalkForwardEvaluator`` (and
    therefore through every strict-regime gate that the validator uses).
    The adapter ignores the ``factor`` argument — the signal is captured
    at construction.  The signal must align row-for-row with the full
    ``df``; the adapter slices both to ``request.eval_start /
    eval_end`` for each fold the wrapper hands it.
    """

    def __init__(self, *, signal: pd.Series, df: pd.DataFrame) -> None:
        if len(signal) != len(df):
            raise ValueError(
                f"signal length {len(signal)} != df length {len(df)}",
            )
        self._signal = signal.reset_index(drop=True)
        self._df = df.reset_index(drop=True)
        # Pre-compute date index once so per-fold slicing is cheap.
        self._dates = pd.to_datetime(self._df["timestamp"]).dt.date

    def evaluate(
        self,
        factor: FactorSpec,
        request: EvaluationRequest,
    ) -> EvaluationBundle:
        mask = (self._dates >= request.eval_start) & (self._dates <= request.eval_end)
        sub_df = self._df.loc[mask].reset_index(drop=True)
        sub_sig = self._signal.loc[mask].reset_index(drop=True)
        if len(sub_df) == 0:
            return EvaluationBundle(
                n_periods=0,
                n_assets=0,
                eval_start=request.eval_start,
                eval_end=request.eval_end,
                forecast_horizon_bars=request.label.forecast_horizon_bars,
                metadata={"evaluator": "precomputed_signal", "mode": "real"},
            )
        return evaluate_precomputed_signal(
            signal=sub_sig,
            df=sub_df,
            request=request,
        )


# ── Expression source ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class _ResolvedExpressions:
    expressions: list[str]
    source_cycle_ids: list[str]
    source_data_fingerprints: list[str]


def _resolve_expressions(args: argparse.Namespace) -> _ResolvedExpressions:
    exprs: list[str] = list(args.expr or [])
    source_cycle_ids: list[str] = []
    source_data_fingerprints: list[str] = []
    if args.expressions_file:
        path = Path(args.expressions_file)
        if not path.is_file():
            raise FileNotFoundError(f"expressions file not found: {path}")
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                exprs.append(line)
    if args.from_validation_report:
        report_expressions, cycle_ids, fingerprints = _load_from_validation_report(args)
        exprs.extend(report_expressions)
        source_cycle_ids.extend(cycle_ids)
        source_data_fingerprints.extend(fingerprints)
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
    return _ResolvedExpressions(
        expressions=deduped,
        source_cycle_ids=list(dict.fromkeys(source_cycle_ids)),
        source_data_fingerprints=list(dict.fromkeys(source_data_fingerprints)),
    )


def _load_from_validation_report(
    args: argparse.Namespace,
) -> tuple[list[str], list[str], list[str]]:
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
    cycle_ids: list[str] = []
    fingerprints: list[str] = []
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Skipping %s: %s", path, exc)
            continue
        before = len(out)
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
        if len(out) > before:
            cycle_id = payload.get("cycle_id")
            fingerprint = payload.get("data_fingerprint")
            if getattr(args, "promote", False) and (
                not isinstance(cycle_id, str)
                or not cycle_id
                or not isinstance(fingerprint, str)
                or not fingerprint
            ):
                raise ValueError(
                    f"promotion source report {path} must include cycle_id "
                    "and data_fingerprint"
                )
            if isinstance(cycle_id, str) and cycle_id:
                cycle_ids.append(cycle_id)
            if isinstance(fingerprint, str) and fingerprint:
                fingerprints.append(fingerprint)
    if not out:
        logger.warning(
            "No factors loaded from %s after filters (passes_ic=%s, passes_rank_ic=%s, min_ic=%s).",
            src,
            args.filter_passes_ic,
            args.filter_passes_rank_ic,
            args.filter_min_ic,
        )
    return out, cycle_ids, fingerprints


# ── CLI ─────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--data-source",
        choices=["synthetic", "parquet", "polygon", "bigquery"],
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
        "--selection-strategy",
        choices=[strategy.value for strategy in FactorSelectionStrategy],
        default=FactorSelectionStrategy.INPUT_ORDER.value,
        help=(
            "How to rank candidate expressions before --top-k is applied. "
            "Persistence is experimental and remains opt-in."
        ),
    )
    p.add_argument(
        "--top-k",
        type=int,
        default=None,
        help="Keep only the top K candidates after selection (minimum 2).",
    )
    p.add_argument(
        "--regime",
        choices=["strict", "lenient"],
        default="strict",
    )
    p.add_argument(
        "--cost-bps",
        type=float,
        default=None,
        help="Override the regime cost assumption for deterministic stress replay.",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of a text block.",
    )

    # Round 8 Phase A — persist a CombinationReport per run.
    p.add_argument(
        "--out-dir",
        default=str(DEFAULT_COMBINATION_DIR),
        help=(
            "Directory for persisted CombinationReport JSONs (default: "
            f"{DEFAULT_COMBINATION_DIR}).  Mirrors the on-disk shape of "
            "artifacts/validations/."
        ),
    )
    p.add_argument(
        "--cycle-id",
        default=None,
        help=(
            "Identifier for this combination run.  Becomes the report "
            "filename ({cycle_id}.json).  Defaults to combine-<ts>."
        ),
    )
    p.add_argument(
        "--universe-id",
        default="combine_factors",
        help="Label recorded in the report (informational; no resolution).",
    )
    p.add_argument(
        "--no-write",
        action="store_true",
        help="Skip writing the CombinationReport to disk.",
    )

    # Round 8 Phase B — promotion path.
    p.add_argument(
        "--promote",
        action="store_true",
        help=(
            "If the basket clears the regime, register it as a "
            "composite FactorSpec (PromotedArtifact + PromotionTrail).  "
            "No-op when the basket fails the regime."
        ),
    )
    p.add_argument(
        "--promoted-dir",
        default=str(DEFAULT_PROMOTED_DIR),
        help="Directory for PromotedArtifact JSONs (when --promote).",
    )
    p.add_argument(
        "--trail-dir",
        default=str(DEFAULT_TRAIL_DIR),
        help="Directory for PromotionTrail JSONs (when --promote).",
    )
    return p


def _build_eval_request(
    *,
    df: pd.DataFrame,
    regime: StrictRegime,
    factor_id: str,
    universe_id: str,
    n_proposals_in_session: int = 1,
) -> EvaluationRequest:
    """Construct the EvaluationRequest used for one combine_factors run.

    Shared between per-factor scoring, basket scoring, and the
    regime-trail hash so all three see byte-identical regime knobs.
    """
    ts_dates = pd.to_datetime(df["timestamp"]).dt.date
    return EvaluationRequest(
        factor_id=factor_id,
        universe_id=universe_id,
        eval_start=ts_dates.min(),
        eval_end=ts_dates.max(),
        label=regime.label_definition(),
        profile=regime.evaluation_profile(),
        neutralize=regime.neutralize,
        beta_lookback_bars=regime.beta_lookback_bars,
        beta_min_periods=regime.beta_min_periods,
        cost_bps=regime.cost_bps,
        holdout=regime.holdout_policy(),
        n_proposals_in_session=n_proposals_in_session,
    )


def _score_signal(
    *,
    signal: pd.Series,
    df: pd.DataFrame,
    regime: StrictRegime,
    request: EvaluationRequest,
) -> EvaluationBundle:
    """Run a precomputed signal through the strict-regime walk-forward stack.

    Same evaluator wiring as :mod:`scripts.validate_strict` —
    ``WalkForwardEvaluator(SignalQualityEvaluator)`` with embargo, sector
    neutralization, real cost, and a TAIL holdout — so the metrics this
    CLI prints are directly comparable to a validation report's
    thumbnails.  The only swap is the inner evaluator: instead of the
    DSL-executing ``SignalQualityEvaluator`` we hand the wrapper a
    ``_PrecomputedSignalEvaluator`` so the basket signal (which has no
    DSL form) can ride the same pipeline.
    """
    inner = _PrecomputedSignalEvaluator(signal=signal, df=df)
    wf = WalkForwardEvaluator(inner, regime.walk_forward_config())
    return wf.evaluate(
        FactorSpec(name=request.factor_id, expression="<precomputed>"),
        request,
    )


def _basket_expression(recipe: CombinationRecipe) -> str:
    components_str = ", ".join(recipe.components)
    return f"combine.{recipe.method.value}([{components_str}])"


def _judge_basket(
    *,
    recipe: CombinationRecipe,
    basket_bundle: EvaluationBundle,
    request: EvaluationRequest,
    judge_thresholds: dict[str, float],
) -> JudgmentDetail:
    """Apply the full production promotion judge to a composite basket."""
    factor = FactorSpec(
        id="basket",
        name=f"composite_{recipe.recipe_id}",
        expression=_basket_expression(recipe),
        composite_recipe=recipe,
    )
    hypothesis = Hypothesis(
        text=f"Composite basket generated by combine_factors recipe {recipe.recipe_id}.",
        source="combine_factors",
    )
    return PromotionJudge(
        refine_margin=judge_thresholds.get("refine_margin", 0.20),
        min_fraction_positive_folds=judge_thresholds.get("min_fraction_positive_folds", 0.6),
        max_tail_concentration=judge_thresholds.get("max_tail_concentration", 0.5),
        min_holdout_decay_ratio=judge_thresholds.get("min_holdout_decay_ratio", 0.5),
        multiple_testing_familywise_alpha=judge_thresholds.get(
            "multiple_testing_familywise_alpha",
            0.05,
        ),
    ).judge(
        hypothesis=hypothesis,
        factor=factor,
        evaluation=basket_bundle,
        request=request,
    )


def _promote_basket(
    *,
    args: argparse.Namespace,
    recipe: CombinationRecipe,
    basket_bundle: EvaluationBundle,
    regime_trail: PromotionTrail,
    cycle_id: str,
) -> Path | None:
    """Register a passing basket as a composite-factor PromotedArtifact.

    Builds a synthetic ExperimentRecord whose ``factor`` carries the
    recipe under ``composite_recipe``.  The ``expression`` placeholder
    is intentionally human-readable so it surfaces nicely in the
    proposer's memory digest in a later cycle (``combine.<method>([
    expr, expr, expr ])``).

    Returns the path of the written PromotedArtifact, or ``None`` if
    the writer chose to skip (which it would not, since we just gated
    on ``passes_regime``).
    """
    # Deterministic id (Round 9 A.2): re-promoting the same recipe under
    # the same regime must overwrite the existing artifact, not create a
    # parallel one.  Keying on recipe_id alone would collapse two
    # legitimate promotions under different regimes; keying on
    # recipe_id + first 6 of the trail_id is the safer default.
    composite_id = f"composite_{recipe.recipe_id}_{regime_trail.trail_id[:6]}"
    factor = FactorSpec(
        id=composite_id,
        name=f"composite_{recipe.recipe_id}",
        expression=_basket_expression(recipe),
        composite_recipe=recipe,
    )
    hypothesis = Hypothesis(
        text=(
            f"Basket of {len(recipe.components)} components combined via "
            f"{recipe.method.value}.  Promoted from combine_factors "
            f"under cycle {cycle_id}."
        ),
        source="combine_factors",
    )
    record = ExperimentRecord(
        hypothesis=hypothesis,
        factor=factor,
        evaluation=basket_bundle,
        decision=ExperimentDecision.PROMOTE_CANDIDATE,
        promotion_trail=regime_trail,
        tags=["composite", f"recipe:{recipe.recipe_id}"],
        notes=(
            f"Promoted by scripts.combine_factors --promote; "
            f"recipe_id={recipe.recipe_id}, trail={regime_trail.trail_id}."
        ),
    )
    trail_registry = TrailRegistryWriter(args.trail_dir)
    writer = PromotedArtifactWriter(
        base_dir=args.promoted_dir,
        cycle_id=cycle_id,
        trail_registry=trail_registry,
    )
    return writer.maybe_write(record)


def _thumbnail(
    *,
    factor_id: str,
    expression: str,
    decision: str,
    bundle: EvaluationBundle,
) -> FactorThumbnail:
    """Squash one EvaluationBundle into the persistable thumbnail shape.

    Round 9.1 — extracts the holdout block from ``bundle.metadata`` so
    out-of-sample IC survives serialization.  Returns ``None`` for each
    holdout field when no TAIL holdout was applied.
    """
    holdout = bundle.metadata.get("holdout") if isinstance(bundle.metadata, dict) else None
    holdout_ic = holdout_rank_ic = holdout_decay_ratio = None
    if isinstance(holdout, dict):
        holdout_ic = holdout.get("ic")
        holdout_rank_ic = holdout.get("rank_ic")
        holdout_decay_ratio = holdout.get("decay_ratio")
    return FactorThumbnail(
        factor_id=factor_id,
        expression=expression,
        decision=decision,
        ic=bundle.ic,
        rank_ic=bundle.rank_ic,
        quantile_spread=bundle.quantile_spread,
        net_quantile_spread=bundle.net_quantile_spread,
        sharpe=bundle.sharpe,
        turnover=bundle.turnover,
        holdout_ic=holdout_ic,
        holdout_rank_ic=holdout_rank_ic,
        holdout_decay_ratio=holdout_decay_ratio,
    )


def _select_component_indices(
    *,
    strategy: FactorSelectionStrategy,
    top_k: int | None,
    bundles: list[EvaluationBundle],
) -> list[int]:
    """Rank candidate bundles deterministically, then apply an optional top-k."""
    if strategy != FactorSelectionStrategy.INPUT_ORDER and top_k is None:
        raise ValueError(f"--top-k is required with --selection-strategy {strategy.value}")
    if top_k is not None and (top_k < 2 or top_k > len(bundles)):
        raise ValueError(f"--top-k must be between 2 and {len(bundles)}; got {top_k}")

    indices = list(range(len(bundles)))
    if strategy == FactorSelectionStrategy.TRAIN_RANK_IC:
        indices.sort(
            key=lambda idx: (
                bundles[idx].rank_ic is not None,
                bundles[idx].rank_ic if bundles[idx].rank_ic is not None else float("-inf"),
            ),
            reverse=True,
        )
    elif strategy == FactorSelectionStrategy.PERSISTENCE:
        indices = rank_by_persistence(
            [(idx, score_from_walk_forward(bundle.metadata)) for idx, bundle in enumerate(bundles)]
        )
    return indices[:top_k] if top_k is not None else indices


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.cost_bps is not None and args.cost_bps < 0:
        print("error: --cost-bps must be >= 0", file=sys.stderr)
        return 2

    try:
        resolved = _resolve_expressions(args)
        df = _load_data(args)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    expressions = resolved.expressions
    data_fingerprint = dataframe_fingerprint(df)
    regime = get_regime(args.regime)
    if args.cost_bps is not None:
        regime = replace(regime, cost_bps=args.cost_bps)
    started_at = datetime.now(UTC)
    logger.info(
        "Combining %d factors via %s under '%s' regime (ic>=%.4f rank_ic>=%.4f)",
        len(expressions),
        args.method,
        args.regime,
        regime.ic_threshold,
        regime.rank_ic_threshold,
    )

    # Per-factor signals + per-factor metrics so the operator can compare
    # individuals against the basket.  Same evaluator as validate_strict
    # (Round 7.1): every IC printed here is the walk-forward, embargoed,
    # sector-neutralized, cost-adjusted IC the validator would report.
    timestamps = df["timestamp"]

    individuals: list[dict[str, object]] = []
    signals: list[pd.Series] = []
    bundles: list[EvaluationBundle] = []
    component_thumbs: list[FactorThumbnail] = []
    for i, expr in enumerate(expressions):
        try:
            sig = compute_signal(expr, df)
        except Exception as exc:
            print(f"error: failed to compile {expr!r}: {exc}", file=sys.stderr)
            return 3
        signals.append(sig)
        factor_id = f"individual_{i}"
        request = _build_eval_request(
            df=df,
            regime=regime,
            factor_id=factor_id,
            universe_id=args.universe_id,
        )
        bundle = _score_signal(signal=sig, df=df, regime=regime, request=request)
        bundles.append(bundle)
        persistence = score_from_walk_forward(bundle.metadata)
        passes_ic = bundle.ic is not None and bundle.ic >= regime.ic_threshold
        passes_rank_ic = bundle.rank_ic is not None and bundle.rank_ic >= regime.rank_ic_threshold
        individuals.append(
            {
                "expression": expr,
                "ic": bundle.ic,
                "rank_ic": bundle.rank_ic,
                "quantile_spread": bundle.quantile_spread,
                "net_quantile_spread": bundle.net_quantile_spread,
                "passes_ic": passes_ic,
                "passes_rank_ic": passes_rank_ic,
                "persistence": (
                    {
                        "n_folds": persistence.n_folds,
                        "fraction_positive": persistence.fraction_positive,
                        "stability": persistence.stability,
                        "mean_rank_ic": persistence.mean_rank_ic,
                    }
                    if persistence is not None
                    else None
                ),
            },
        )
        component_thumbs.append(
            _thumbnail(
                factor_id=factor_id,
                expression=expr,
                decision="component",
                bundle=bundle,
            ),
        )

    candidate_count = len(expressions)
    selection_strategy = FactorSelectionStrategy(args.selection_strategy)
    try:
        selected_indices = _select_component_indices(
            strategy=selection_strategy,
            top_k=args.top_k,
            bundles=bundles,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    selected_set = set(selected_indices)
    for idx, item in enumerate(individuals):
        item["selected"] = idx in selected_set
    expressions = [expressions[idx] for idx in selected_indices]
    signals = [signals[idx] for idx in selected_indices]
    component_thumbs = [component_thumbs[idx] for idx in selected_indices]

    # Combination
    method = CombinationMethod(args.method)
    recipe = CombinationRecipe.build(method=method, components=expressions)
    basket_sig = combine_signals(signals, timestamps, method)
    basket_request = _build_eval_request(
        df=df,
        regime=regime,
        factor_id="basket",
        universe_id=args.universe_id,
        n_proposals_in_session=candidate_count,
    )
    basket_bundle = _score_signal(
        signal=basket_sig,
        df=df,
        regime=regime,
        request=basket_request,
    )
    basket_metadata = dict(basket_bundle.metadata)
    basket_metadata["data_fingerprint"] = data_fingerprint
    basket_metadata["source_validation_cycle_ids"] = resolved.source_cycle_ids
    basket_metadata["source_data_fingerprints"] = resolved.source_data_fingerprints
    basket_bundle = basket_bundle.model_copy(update={"metadata": basket_metadata})
    threshold_multiplier = bonferroni_z_threshold_multiplier(
        candidate_count,
        familywise_alpha=regime.multiple_testing_familywise_alpha,
    )
    adjusted_ic_threshold = regime.ic_threshold * threshold_multiplier
    adjusted_rank_ic_threshold = regime.rank_ic_threshold * threshold_multiplier
    basket: dict[str, object] = {
        "method": method.value,
        "ic": basket_bundle.ic,
        "rank_ic": basket_bundle.rank_ic,
        "quantile_spread": basket_bundle.quantile_spread,
        "net_quantile_spread": basket_bundle.net_quantile_spread,
    }
    basket["passes_ic"] = (
        basket_bundle.ic is not None and basket_bundle.ic >= adjusted_ic_threshold
    )
    basket["passes_rank_ic"] = (
        basket_bundle.rank_ic is not None
        and basket_bundle.rank_ic >= adjusted_rank_ic_threshold
    )
    basket["passes_quantile_spread"] = (
        basket_bundle.quantile_spread is not None
        and basket_bundle.quantile_spread >= regime.quantile_spread_threshold
    )

    # Pairwise correlation matrix — tells us *why* combination did/didn't help.
    corr = pairwise_rank_corr(signals, timestamps)
    avg_off_diag = (
        float((corr.values.sum() - corr.values.trace()) / (corr.size - len(corr)))
        if len(corr) > 1
        else float("nan")
    )

    # ── Round 8 Phase A: persist a CombinationReport ─────────────────────
    judge_thresholds = regime.judge_thresholds()
    judgment = _judge_basket(
        recipe=recipe,
        basket_bundle=basket_bundle,
        request=basket_request,
        judge_thresholds=judge_thresholds,
    )
    passes_regime = judgment.decision == ExperimentDecision.PROMOTE_CANDIDATE
    selection_config: dict[str, str | int | float] = {
        "strategy": selection_strategy.value,
        "candidate_count": candidate_count,
        "selected_count": len(expressions),
    }
    selection_score_version = (
        PERSISTENCE_SCORE_VERSION
        if selection_strategy == FactorSelectionStrategy.PERSISTENCE
        else ""
    )
    if selection_score_version:
        selection_config["score_version"] = selection_score_version
    if args.top_k is not None:
        selection_config["top_k"] = args.top_k
    trail_selection = (
        selection_config
        if selection_strategy != FactorSelectionStrategy.INPUT_ORDER or args.top_k is not None
        else None
    )
    fallback_trail = PromotionTrail.from_inputs(
        evaluation_request=basket_request,
        judge_thresholds=judge_thresholds,
        walk_forward={
            "n_folds": regime.n_folds,
            "fold_size_days": regime.fold_size_days,
            "step_days": regime.step_days,
            "embargo_days": regime.embargo_days,
        },
        selection=trail_selection,
    )
    regime_trail = (
        fallback_trail
        if trail_selection is not None
        else (judgment.promotion_trail or fallback_trail)
    )
    basket_thumb = _thumbnail(
        factor_id="basket",
        expression=f"<combine:{method.value}({len(expressions)})>",
        decision=judgment.decision.value,
        bundle=basket_bundle,
    )
    cycle_id = args.cycle_id or f"combine-{started_at.strftime('%Y%m%dT%H%M%SZ')}"
    report = build_combination_report(
        cycle_id=cycle_id,
        regime_trail_id=regime_trail.trail_id,
        universe_id=args.universe_id,
        data_fingerprint=data_fingerprint,
        source_validation_cycle_ids=resolved.source_cycle_ids,
        source_data_fingerprints=resolved.source_data_fingerprints,
        cost_bps=basket_request.cost_bps,
        n_proposals_in_session=candidate_count,
        ic_threshold_multiplier=threshold_multiplier,
        started_at=started_at,
        method=method,
        components=expressions,
        component_factor_ids=None,
        basket_metrics=basket_thumb,
        component_metrics=component_thumbs,
        avg_pairwise_rank_corr=(
            None if avg_off_diag != avg_off_diag else avg_off_diag  # NaN guard
        ),
        passes_regime=passes_regime,
        selection_strategy=selection_strategy,
        selection_score_version=selection_score_version,
        selection_top_k=args.top_k,
        selection_candidate_count=candidate_count,
    )
    if not args.no_write:
        report_path = CombinationReportWriter(args.out_dir).write(report)
        if report_path is not None:
            logger.info("Combination report persisted to %s", report_path)

    # ── Round 8 Phase B: optional promotion ──────────────────────────────
    promoted_artifact_path: Path | None = None
    if args.promote:
        if not passes_regime:
            logger.info(
                "--promote set but basket failed regime decision=%s failure=%s "
                "(ic=%s rank_ic=%s quantile_spread=%s); skipping promotion.",
                judgment.decision.value,
                judgment.failure.detail if judgment.failure else None,
                basket_bundle.ic,
                basket_bundle.rank_ic,
                basket_bundle.quantile_spread,
            )
        else:
            promoted_artifact_path = _promote_basket(
                args=args,
                recipe=recipe,
                basket_bundle=basket_bundle,
                regime_trail=regime_trail,
                cycle_id=cycle_id,
            )
            if promoted_artifact_path is not None:
                logger.info(
                    "Basket promoted as composite factor: %s",
                    promoted_artifact_path,
                )

    summary = {
        "regime": args.regime,
        "method": method.value,
        "n_factors": len(expressions),
        "n_candidates": candidate_count,
        "selection_strategy": selection_strategy.value,
        "selection_score_version": selection_score_version,
        "selection_top_k": args.top_k,
        "data_fingerprint": data_fingerprint,
        "source_validation_cycle_ids": resolved.source_cycle_ids,
        "source_data_fingerprints": resolved.source_data_fingerprints,
        "cost_bps": basket_request.cost_bps,
        "n_proposals_in_session": candidate_count,
        "ic_threshold_multiplier": threshold_multiplier,
        "individuals": individuals,
        "basket": basket,
        "avg_pairwise_rank_corr": avg_off_diag,
        "thresholds": {
            "ic": regime.ic_threshold,
            "rank_ic": regime.rank_ic_threshold,
            "adjusted_ic": adjusted_ic_threshold,
            "adjusted_rank_ic": adjusted_rank_ic_threshold,
            "quantile_spread": regime.quantile_spread_threshold,
        },
        "cycle_id": cycle_id,
        "regime_trail_id": regime_trail.trail_id,
        "recipe_id": report.recipe.recipe_id,
        "passes_regime": passes_regime,
        "regime_decision": judgment.decision.value,
        "regime_failure": judgment.failure.detail if judgment.failure else None,
        "regime_notes": judgment.notes,
        "promoted_artifact": (
            str(promoted_artifact_path) if promoted_artifact_path is not None else None
        ),
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
    print(
        f"  selection={s['selection_strategy']} "
        f"candidates={s['n_candidates']} selected={s['n_factors']}"
    )
    print(border)
    print(f"  {'expression':<48}  {'ic':>8}  {'rank_ic':>8}  pass")
    print(f"  {'-' * 48}  {'-' * 8}  {'-' * 8}  ----")
    individuals = cast(list[dict[str, object]], s["individuals"])
    for f in individuals:
        ic_value = f.get("ic")
        rank_ic_value = f.get("rank_ic")
        ic = f"{cast(float, ic_value):+.4f}" if ic_value is not None else "   n/a"
        ric = f"{cast(float, rank_ic_value):+.4f}" if rank_ic_value is not None else "   n/a"
        flags = ("ic " if bool(f["passes_ic"]) else "   ") + (
            "rank_ic" if bool(f["passes_rank_ic"]) else ""
        )
        print(f"  {str(f['expression'])[:48]:<48}  {ic:>8}  {ric:>8}  {flags.strip() or '-'}")
    print(f"  {'-' * 48}  {'-' * 8}  {'-' * 8}  ----")
    basket = cast(dict[str, object], s["basket"])
    basket_ic = basket.get("ic")
    basket_rank_ic = basket.get("rank_ic")
    bic = f"{cast(float, basket_ic):+.4f}" if basket_ic is not None else "   n/a"
    bric = f"{cast(float, basket_rank_ic):+.4f}" if basket_rank_ic is not None else "   n/a"
    bflags = ("ic " if bool(basket["passes_ic"]) else "   ") + (
        "rank_ic" if bool(basket["passes_rank_ic"]) else ""
    )
    print(f"  {'BASKET':<48}  {bic:>8}  {bric:>8}  {bflags.strip() or '-'}")
    print()
    avg_corr = float(cast(float, s["avg_pairwise_rank_corr"]))
    print(f"  avg pairwise rank-correlation : {avg_corr:+.4f}")
    thresholds = cast(dict[str, float], s["thresholds"])
    print(
        f"  thresholds                    : ic>={thresholds['ic']:.4f}  "
        f"rank_ic>={thresholds['rank_ic']:.4f}",
    )
    if cast(float, s["ic_threshold_multiplier"]) > 1:
        print(
            f"  family-adjusted (N={s['n_proposals_in_session']})      : "
            f"ic>={thresholds['adjusted_ic']:.4f}  "
            f"rank_ic>={thresholds['adjusted_rank_ic']:.4f}",
        )
    print(f"{border}\n")


if __name__ == "__main__":
    sys.exit(main())
