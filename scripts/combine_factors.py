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
from datetime import UTC, date, datetime
from pathlib import Path

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
from alpha_harness.data.synthetic import generate_price_panel
from alpha_harness.evaluators.signal_quality import evaluate_precomputed_signal
from alpha_harness.evaluators.walk_forward import WalkForwardEvaluator
from alpha_harness.regimes import get_regime
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
        mask = (self._dates >= request.eval_start) & (
            self._dates <= request.eval_end
        )
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
            signal=sub_sig, df=sub_df, request=request,
        )


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
    *, df: pd.DataFrame, regime, factor_id: str, universe_id: str,
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
        cost_bps=regime.cost_bps,
        holdout=regime.holdout_policy(),
    )


def _score_signal(
    *,
    signal: pd.Series,
    df: pd.DataFrame,
    regime,
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
    components_str = ", ".join(recipe.components)
    nice_expression = f"combine.{recipe.method.value}([{components_str}])"
    # Deterministic id (Round 9 A.2): re-promoting the same recipe under
    # the same regime must overwrite the existing artifact, not create a
    # parallel one.  Keying on recipe_id alone would collapse two
    # legitimate promotions under different regimes; keying on
    # recipe_id + first 6 of the trail_id is the safer default.
    composite_id = f"composite_{recipe.recipe_id}_{regime_trail.trail_id[:6]}"
    factor = FactorSpec(
        id=composite_id,
        name=f"composite_{recipe.recipe_id}",
        expression=nice_expression,
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
    *, factor_id: str, expression: str, decision: str, bundle: EvaluationBundle,
) -> FactorThumbnail:
    """Squash one EvaluationBundle into the persistable thumbnail shape."""
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
    )


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        expressions = _resolve_expressions(args)
        df = _load_data(args)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    regime = get_regime(args.regime)
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
            df=df, regime=regime, factor_id=factor_id, universe_id=args.universe_id,
        )
        bundle = _score_signal(signal=sig, df=df, regime=regime, request=request)
        passes_ic = bundle.ic is not None and bundle.ic >= regime.ic_threshold
        passes_rank_ic = (
            bundle.rank_ic is not None and bundle.rank_ic >= regime.rank_ic_threshold
        )
        individuals.append(
            {
                "expression": expr,
                "ic": bundle.ic,
                "rank_ic": bundle.rank_ic,
                "quantile_spread": bundle.quantile_spread,
                "net_quantile_spread": bundle.net_quantile_spread,
                "passes_ic": passes_ic,
                "passes_rank_ic": passes_rank_ic,
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

    # Combination
    method = CombinationMethod(args.method)
    basket_sig = combine_signals(signals, timestamps, method)
    basket_request = _build_eval_request(
        df=df, regime=regime, factor_id="basket", universe_id=args.universe_id,
    )
    basket_bundle = _score_signal(
        signal=basket_sig, df=df, regime=regime, request=basket_request,
    )
    basket: dict[str, object] = {
        "method": method.value,
        "ic": basket_bundle.ic,
        "rank_ic": basket_bundle.rank_ic,
        "quantile_spread": basket_bundle.quantile_spread,
        "net_quantile_spread": basket_bundle.net_quantile_spread,
    }
    basket["passes_ic"] = (
        basket_bundle.ic is not None and basket_bundle.ic >= regime.ic_threshold
    )
    basket["passes_rank_ic"] = (
        basket_bundle.rank_ic is not None
        and basket_bundle.rank_ic >= regime.rank_ic_threshold
    )

    # Pairwise correlation matrix — tells us *why* combination did/didn't help.
    corr = pairwise_rank_corr(signals, timestamps)
    avg_off_diag = (
        float((corr.values.sum() - corr.values.trace()) / (corr.size - len(corr)))
        if len(corr) > 1
        else float("nan")
    )

    # ── Round 8 Phase A: persist a CombinationReport ─────────────────────
    passes_regime = bool(basket["passes_ic"]) and bool(basket["passes_rank_ic"])
    regime_trail = PromotionTrail.from_inputs(
        evaluation_request=basket_request,
        judge_thresholds=regime.judge_thresholds(),
        walk_forward={
            "n_folds": regime.n_folds,
            "fold_size_days": regime.fold_size_days,
            "step_days": regime.step_days,
            "embargo_days": regime.embargo_days,
        },
    )
    basket_thumb = _thumbnail(
        factor_id="basket",
        expression=f"<combine:{method.value}({len(expressions)})>",
        decision="basket",
        bundle=basket_bundle,
    )
    cycle_id = args.cycle_id or f"combine-{started_at.strftime('%Y%m%dT%H%M%SZ')}"
    report = build_combination_report(
        cycle_id=cycle_id,
        regime_trail_id=regime_trail.trail_id,
        universe_id=args.universe_id,
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
                "--promote set but basket failed regime "
                "(ic=%s rank_ic=%s); skipping promotion.",
                basket_bundle.ic,
                basket_bundle.rank_ic,
            )
        else:
            promoted_artifact_path = _promote_basket(
                args=args,
                recipe=report.recipe,
                basket_bundle=basket_bundle,
                regime_trail=regime_trail,
                cycle_id=cycle_id,
            )
            if promoted_artifact_path is not None:
                logger.info(
                    "Basket promoted as composite factor: %s", promoted_artifact_path,
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
        "cycle_id": cycle_id,
        "regime_trail_id": regime_trail.trail_id,
        "recipe_id": report.recipe.recipe_id,
        "passes_regime": passes_regime,
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
