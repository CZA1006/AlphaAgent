"""Tests for Round 4A.6 — RefinementBrief + brief-aware mutations + lineage."""

from __future__ import annotations

from datetime import date

from alpha_harness.evaluators.promotion_judge import PromotionJudge
from alpha_harness.factors.compiler import FactorDslCompiler
from alpha_harness.orchestrator.mutations import propose_mutations
from alpha_harness.orchestrator.refinement import (
    RefinementConfig,
    RefinementRunner,
)
from alpha_harness.orchestrator.research_loop import ResearchOrchestrator
from alpha_harness.refiner import RefinementBrief, build_brief
from alpha_harness.registries.experiment import ExperimentRegistry
from alpha_harness.registries.hypothesis import HypothesisRegistry
from alpha_harness.schemas.evaluation import (
    EvaluationBundle,
    EvaluationProfile,
    EvaluationRequest,
)
from alpha_harness.schemas.experiment import (
    ExperimentDecision,
    ExperimentRecord,
)
from alpha_harness.schemas.factor import FactorSpec
from alpha_harness.schemas.hypothesis import Hypothesis
from alpha_harness.service import AlphaHarnessService

DEFAULT_REQ = EvaluationRequest(
    factor_id="placeholder",
    universe_id="u",
    eval_start=date(2020, 1, 1),
    eval_end=date(2023, 12, 31),
)


# ── build_brief ─────────────────────────────────────────────────────────────


def _record(eval_bundle: EvaluationBundle) -> ExperimentRecord:
    return ExperimentRecord(
        hypothesis=Hypothesis(text="rank(close)", rationale="r"),
        factor=FactorSpec(name="f", expression="rank(close)"),
        evaluation=eval_bundle,
        decision=ExperimentDecision.REFINE,
    )


def test_brief_empty_when_evaluation_is_strong() -> None:
    # Well clear of every threshold; no diagnostics should fire.
    ev = EvaluationBundle(
        ic=0.10,
        rank_ic=0.11,
        quantile_spread=0.02,
        net_quantile_spread=0.019,
        turnover=0.3,
    )
    brief = build_brief(_record(ev), EvaluationProfile())
    assert brief.is_empty
    assert "(no diagnostic)" in brief.describe()


def test_brief_flags_weak_cross_sectional_when_rank_ic_borderline() -> None:
    # rank_ic just above threshold → borderline gate, weak_cross_sectional=True.
    ev = EvaluationBundle(
        ic=0.05,
        rank_ic=0.032,
        quantile_spread=0.02,
        turnover=0.3,
    )
    brief = build_brief(_record(ev), EvaluationProfile())
    assert brief.weak_cross_sectional is True
    assert any(g.name == "rank_ic" for g in brief.borderline_gates)


def test_brief_flags_turnover_high() -> None:
    ev = EvaluationBundle(
        ic=0.10,
        rank_ic=0.11,
        quantile_spread=0.02,
        turnover=1.5,
    )
    brief = build_brief(_record(ev), EvaluationProfile())
    assert brief.turnover_high is True


def test_brief_flags_cost_drag() -> None:
    # Gross spread 0.02, net 0.005 → drag = 75% of gross.
    ev = EvaluationBundle(
        ic=0.10,
        rank_ic=0.11,
        quantile_spread=0.02,
        net_quantile_spread=0.005,
        turnover=0.5,
    )
    brief = build_brief(_record(ev), EvaluationProfile())
    assert brief.cost_drag_large is True


def test_brief_flags_sign_inconsistency_from_metadata() -> None:
    ev = EvaluationBundle(
        ic=0.05,
        rank_ic=0.06,
        quantile_spread=0.01,
        metadata={
            "ic_by_horizon": {"1": 0.05, "5": -0.01, "20": 0.04},
            "ic_sign_consistent_horizons": 2,
        },
    )
    brief = build_brief(_record(ev), EvaluationProfile())
    assert brief.sign_inconsistent is True


def test_brief_describe_lists_flags() -> None:
    ev = EvaluationBundle(
        ic=0.05,
        rank_ic=0.032,
        quantile_spread=0.02,
        net_quantile_spread=0.004,
        turnover=1.5,
    )
    desc = build_brief(_record(ev), EvaluationProfile()).describe()
    assert "turnover_high" in desc
    assert "cost_drag_large" in desc
    assert "weak_cross_sectional" in desc


# ── propose_mutations with brief ────────────────────────────────────────────


def test_mutations_without_brief_preserves_legacy_order() -> None:
    labels_a = [lbl for _, lbl in propose_mutations("ts_mean(close, 20)")]
    labels_b = [lbl for _, lbl in propose_mutations("ts_mean(close, 20)", brief=None)]
    assert labels_a == labels_b


def test_mutations_brief_turnover_high_prefers_window_double() -> None:
    brief = RefinementBrief(turnover_high=True)
    labels = [lbl for _, lbl in propose_mutations("ts_mean(close, 20)", brief=brief)]
    i_double = next(i for i, lbl in enumerate(labels) if lbl.startswith("window_double"))
    i_halve = next(i for i, lbl in enumerate(labels) if lbl.startswith("window_halve"))
    assert i_double < i_halve


def test_mutations_brief_weak_cross_sectional_prefers_wrap() -> None:
    brief = RefinementBrief(weak_cross_sectional=True)
    labels = [
        lbl
        for _, lbl in propose_mutations(
            "rank(ts_mean(close, 20))",
            brief=brief,
        )
    ]
    # unwrap_outer should fall behind wrap_zscore when rank_ic is weak.
    i_unwrap = labels.index("unwrap_outer")
    i_wrap_z = labels.index("wrap_zscore")
    assert i_wrap_z < i_unwrap


def test_mutations_empty_brief_does_not_reorder() -> None:
    brief = RefinementBrief()
    labels_a = [lbl for _, lbl in propose_mutations("ts_mean(close, 20)")]
    labels_b = [lbl for _, lbl in propose_mutations("ts_mean(close, 20)", brief=brief)]
    assert labels_a == labels_b


# ── Lineage fields on FactorSpec ────────────────────────────────────────────


class _ScriptedEvaluator:
    def __init__(
        self,
        mapping: dict[str, tuple[float, float, float]],
        default: tuple[float, float, float] = (0.0, 0.0, 0.0),
    ) -> None:
        self._mapping = mapping
        self._default = default

    def evaluate(
        self,
        factor: FactorSpec,
        request: EvaluationRequest,
    ) -> EvaluationBundle:
        ic, rank_ic, spread = self._mapping.get(
            factor.expression,
            self._default,
        )
        return EvaluationBundle(
            ic=ic,
            rank_ic=rank_ic,
            quantile_spread=spread,
        )


def _build_runner(
    evaluator: _ScriptedEvaluator,
    config: RefinementConfig | None = None,
) -> tuple[RefinementRunner, ExperimentRegistry]:
    service = AlphaHarnessService(
        compiler=FactorDslCompiler(),
        evaluator=evaluator,
        judge=PromotionJudge(),
    )
    experiments = ExperimentRegistry()
    orch = ResearchOrchestrator(
        service=service,
        experiment_registry=experiments,
        hypothesis_registry=HypothesisRegistry(),
    )
    return RefinementRunner(orch, config=config), experiments


def test_root_factor_has_no_refinement_lineage() -> None:
    evaluator = _ScriptedEvaluator(
        {"rank(ts_mean(close, 20))": (0.10, 0.11, 0.02)},
    )
    runner, _ = _build_runner(evaluator)
    result = runner.run(
        Hypothesis(text="rank(ts_mean(close, 20))"),
        DEFAULT_REQ,
    )
    assert result.root.factor.parent_factor_id is None
    assert result.root.factor.refinement_round == 0


def test_children_carry_parent_factor_id_and_round() -> None:
    borderline = (0.023, 0.035, 0.006)
    evaluator = _ScriptedEvaluator(
        {"rank(ts_mean(close, 20))": borderline},
        default=(0.0, 0.0, 0.0),
    )
    runner, _ = _build_runner(evaluator)
    result = runner.run(
        Hypothesis(text="rank(ts_mean(close, 20))"),
        DEFAULT_REQ,
    )
    assert result.root.decision == ExperimentDecision.REFINE
    assert len(result.children) >= 1
    root_factor_id = result.root.factor.id
    for child in result.children:
        assert child.factor.parent_factor_id == root_factor_id
        assert child.factor.refinement_round == 1


def test_max_refinement_rounds_zero_blocks_all_children() -> None:
    borderline = (0.023, 0.035, 0.006)
    evaluator = _ScriptedEvaluator(
        {"rank(ts_mean(close, 20))": borderline},
        default=borderline,
    )
    runner, _ = _build_runner(
        evaluator,
        config=RefinementConfig(max_depth=0),
    )
    result = runner.run(
        Hypothesis(text="rank(ts_mean(close, 20))"),
        DEFAULT_REQ,
    )
    assert result.root.decision == ExperimentDecision.REFINE
    assert result.children == []


def test_depth2_children_have_refinement_round_2() -> None:
    borderline = (0.023, 0.035, 0.006)
    evaluator = _ScriptedEvaluator(mapping={}, default=borderline)
    runner, _ = _build_runner(
        evaluator,
        config=RefinementConfig(
            max_depth=2,
            max_variants_per_step=1,
            max_total_children=3,
        ),
    )
    result = runner.run(
        Hypothesis(text="rank(ts_mean(close, 20))"),
        DEFAULT_REQ,
    )
    rounds = {c.factor.refinement_round for c in result.children}
    # With max_depth=2 we should see round 1 and round 2 children.
    assert 1 in rounds
    assert 2 in rounds
    assert max(rounds) == 2
