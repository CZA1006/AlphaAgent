"""Tests for evaluators, promotion judge, and the research orchestrator.

This module contains:
    - Unit tests for SignalQualityEvaluator
    - Unit tests for NoveltyEvaluator
    - Unit tests for PromotionJudge
    - End-to-end smoke test for ResearchOrchestrator
"""

from datetime import date

from alpha_harness.evaluators.novelty import NoveltyEvaluator
from alpha_harness.evaluators.promotion_judge import PromotionJudge
from alpha_harness.evaluators.signal_quality import SignalQualityEvaluator
from alpha_harness.factors.stub_compiler import StubFactorCompiler
from alpha_harness.orchestrator.research_loop import ResearchOrchestrator
from alpha_harness.registries.experiment import ExperimentRegistry
from alpha_harness.registries.hypothesis import HypothesisRegistry
from alpha_harness.schemas.evaluation import (
    EvaluationBundle,
    EvaluationRequest,
)
from alpha_harness.schemas.experiment import (
    ExperimentDecision,
    FailureCategory,
)
from alpha_harness.schemas.factor import FactorSpec
from alpha_harness.schemas.hypothesis import Hypothesis, HypothesisStatus
from alpha_harness.service import AlphaHarnessService

# ── Shared fixtures ──────────────────────────────────────────────────────────

def _default_eval_request() -> EvaluationRequest:
    return EvaluationRequest(
        factor_id="placeholder",
        universe_id="test_universe",
        eval_start=date(2020, 1, 1),
        eval_end=date(2023, 12, 31),
    )


# ── SignalQualityEvaluator ───────────────────────────────────────────────────


class TestSignalQualityEvaluator:
    def test_returns_all_metrics(self) -> None:
        evaluator = SignalQualityEvaluator()
        factor = FactorSpec(name="momentum_20d", expression="rank(ts_mean(close, 20))")
        request = _default_eval_request()

        bundle = evaluator.evaluate(factor, request)

        assert bundle.ic is not None
        assert bundle.rank_ic is not None
        assert bundle.quantile_spread is not None
        assert bundle.monotonicity is not None
        assert bundle.turnover is not None
        assert bundle.sharpe is not None

    def test_deterministic_same_name(self) -> None:
        evaluator = SignalQualityEvaluator()
        factor = FactorSpec(name="test_factor", expression="close")
        request = _default_eval_request()

        bundle_1 = evaluator.evaluate(factor, request)
        bundle_2 = evaluator.evaluate(factor, request)

        assert bundle_1.ic == bundle_2.ic
        assert bundle_1.rank_ic == bundle_2.rank_ic
        assert bundle_1.sharpe == bundle_2.sharpe

    def test_different_names_different_results(self) -> None:
        evaluator = SignalQualityEvaluator()
        request = _default_eval_request()

        bundle_a = evaluator.evaluate(
            FactorSpec(name="alpha", expression="close"), request
        )
        bundle_b = evaluator.evaluate(
            FactorSpec(name="beta", expression="close"), request
        )

        # Different names → different seed → at least some metrics differ
        assert bundle_a.ic != bundle_b.ic or bundle_a.sharpe != bundle_b.sharpe

    def test_populates_eval_window(self) -> None:
        evaluator = SignalQualityEvaluator()
        factor = FactorSpec(name="test", expression="close")
        request = _default_eval_request()

        bundle = evaluator.evaluate(factor, request)

        assert bundle.eval_start == date(2020, 1, 1)
        assert bundle.eval_end == date(2023, 12, 31)
        assert bundle.forecast_horizon_bars == 5
        assert bundle.metadata.get("evaluator") == "signal_quality"


# ── NoveltyEvaluator ─────────────────────────────────────────────────────────


class TestNoveltyEvaluator:
    def test_novel_when_no_existing(self) -> None:
        evaluator = NoveltyEvaluator()
        factor = FactorSpec(name="new_factor", expression="rank(close)")

        verdict = evaluator.check_novelty(factor)

        assert verdict.is_novel is True
        assert verdict.similarity_score == 0.0
        assert verdict.most_similar_factor_id is None

    def test_detects_exact_duplicate(self) -> None:
        existing = [("f001", "rank(close)")]
        evaluator = NoveltyEvaluator(existing_expressions=existing)
        factor = FactorSpec(name="dup", expression="rank(close)")

        verdict = evaluator.check_novelty(factor)

        assert verdict.is_novel is False
        assert verdict.similarity_score == 1.0
        assert verdict.most_similar_factor_id == "f001"

    def test_novel_with_different_expression(self) -> None:
        existing = [("f001", "rank(close)")]
        evaluator = NoveltyEvaluator(existing_expressions=existing)
        factor = FactorSpec(name="new", expression="ts_mean(volume, 10)")

        verdict = evaluator.check_novelty(factor)

        assert verdict.is_novel is True
        assert verdict.similarity_score == 0.0


# ── PromotionJudge ───────────────────────────────────────────────────────────


class TestPromotionJudge:
    def test_promotes_strong_signal(self) -> None:
        judge = PromotionJudge()
        hypothesis = Hypothesis(text="strong momentum")
        factor = FactorSpec(name="strong", expression="rank(close)")
        request = _default_eval_request()
        evaluation = EvaluationBundle(
            ic=0.08, rank_ic=0.10, quantile_spread=0.02,
            n_periods=100, n_assets=50,
        )

        decision = judge.judge(hypothesis, factor, evaluation, request)
        assert decision == ExperimentDecision.PROMOTE_CANDIDATE

    def test_rejects_weak_signal(self) -> None:
        judge = PromotionJudge()
        hypothesis = Hypothesis(text="weak idea")
        factor = FactorSpec(name="weak", expression="noise()")
        request = _default_eval_request()
        evaluation = EvaluationBundle(
            ic=0.001, rank_ic=0.002, quantile_spread=0.0001,
            n_periods=100, n_assets=50,
        )

        decision = judge.judge(hypothesis, factor, evaluation, request)
        assert decision == ExperimentDecision.REJECT

        detail = judge.last_detail
        assert detail is not None
        assert detail.failure is not None
        assert detail.failure.category == FailureCategory.WEAK_SIGNAL

    def test_rejects_insufficient_data(self) -> None:
        judge = PromotionJudge()
        hypothesis = Hypothesis(text="data sparse")
        factor = FactorSpec(name="sparse", expression="close")
        request = _default_eval_request()
        evaluation = EvaluationBundle(
            ic=0.05, rank_ic=0.06, quantile_spread=0.01,
            n_periods=10,  # below min_periods=60
            n_assets=50,
        )

        decision = judge.judge(hypothesis, factor, evaluation, request)
        assert decision == ExperimentDecision.REJECT

        detail = judge.last_detail
        assert detail is not None
        assert detail.failure is not None
        assert detail.failure.category == FailureCategory.DATA_INSUFFICIENT

    def test_rejects_duplicate(self) -> None:
        novelty = NoveltyEvaluator(
            existing_expressions=[("f001", "rank(close)")]
        )
        judge = PromotionJudge(novelty_evaluator=novelty)
        hypothesis = Hypothesis(text="same idea")
        factor = FactorSpec(name="dup", expression="rank(close)")
        request = _default_eval_request()
        evaluation = EvaluationBundle(
            ic=0.08, rank_ic=0.10, quantile_spread=0.02,
            n_periods=100, n_assets=50,
        )

        decision = judge.judge(hypothesis, factor, evaluation, request)
        assert decision == ExperimentDecision.REJECT

        detail = judge.last_detail
        assert detail is not None
        assert detail.failure is not None
        assert detail.failure.category == FailureCategory.DUPLICATE

    def test_refines_borderline(self) -> None:
        """Metrics that pass but are within 20% of threshold → REFINE."""
        judge = PromotionJudge(refine_margin=0.20)
        hypothesis = Hypothesis(text="borderline")
        factor = FactorSpec(name="border", expression="edge()")
        request = _default_eval_request()
        # Default thresholds: ic=0.02, rank_ic=0.03, quantile_spread=0.005
        # ic=0.022 passes (0.02) but margin = (0.022-0.02)/0.02 = 0.10 < 0.20
        evaluation = EvaluationBundle(
            ic=0.022, rank_ic=0.035, quantile_spread=0.0055,
            n_periods=100, n_assets=50,
        )

        decision = judge.judge(hypothesis, factor, evaluation, request)
        assert decision == ExperimentDecision.REFINE

    def test_missing_required_metric_rejects(self) -> None:
        judge = PromotionJudge()
        hypothesis = Hypothesis(text="partial eval")
        factor = FactorSpec(name="partial", expression="close")
        request = _default_eval_request()
        evaluation = EvaluationBundle(
            ic=0.05, rank_ic=None,  # missing required metric
            quantile_spread=0.01,
            n_periods=100, n_assets=50,
        )

        decision = judge.judge(hypothesis, factor, evaluation, request)
        assert decision == ExperimentDecision.REJECT


# ── ResearchOrchestrator (end-to-end smoke test) ─────────────────────────────


class TestResearchOrchestrator:
    def _build_orchestrator(
        self,
        novelty: NoveltyEvaluator | None = None,
    ) -> ResearchOrchestrator:
        compiler = StubFactorCompiler()
        evaluator = SignalQualityEvaluator()
        judge = PromotionJudge(novelty_evaluator=novelty)
        service = AlphaHarnessService(
            compiler=compiler,
            evaluator=evaluator,
            judge=judge,
        )
        return ResearchOrchestrator(
            service=service,
            judge=judge,
            experiment_registry=ExperimentRegistry(),
            hypothesis_registry=HypothesisRegistry(),
        )

    def test_single_cycle_end_to_end(self) -> None:
        """Smoke test: hypothesis → compile → evaluate → judge → record."""
        orch = self._build_orchestrator()
        hypothesis = Hypothesis(text="momentum reversal in large caps")
        request = _default_eval_request()

        record = orch.run_cycle(hypothesis, request)

        # The cycle completed and produced a valid ExperimentRecord
        assert record.hypothesis.text == "momentum reversal in large caps"
        assert record.factor.hypothesis_id == hypothesis.id
        assert record.factor.name != ""
        assert record.evaluation.ic is not None
        assert record.evaluation.eval_start == date(2020, 1, 1)
        assert record.decision in list(ExperimentDecision)

    def test_batch_processes_all(self) -> None:
        orch = self._build_orchestrator()
        hypotheses = [
            Hypothesis(text="mean reversion"),
            Hypothesis(text="momentum"),
            Hypothesis(text="volatility clustering"),
        ]
        request = _default_eval_request()

        records = orch.run_batch(hypotheses, request)

        assert len(records) == 3
        for record in records:
            assert record.decision in list(ExperimentDecision)

    def test_summary_counts(self) -> None:
        orch = self._build_orchestrator()
        hypotheses = [
            Hypothesis(text="idea_one"),
            Hypothesis(text="idea_two"),
        ]
        request = _default_eval_request()

        orch.run_batch(hypotheses, request)
        summary = orch.summary()

        # Total experiments should equal number of hypotheses
        total = sum(summary.values())
        assert total == 2

    def test_hypothesis_status_updated(self) -> None:
        """After a cycle, the hypothesis status should reflect the decision."""
        orch = self._build_orchestrator()
        hypothesis = Hypothesis(text="status tracking test")
        request = _default_eval_request()

        record = orch.run_cycle(hypothesis, request)

        # The hypothesis in the registry should have an updated status
        stored = orch._hypotheses.get(hypothesis.id)
        assert stored is not None
        if record.decision == ExperimentDecision.PROMOTE_CANDIDATE:
            assert stored.status == HypothesisStatus.PROMISING
        elif record.decision == ExperimentDecision.REJECT:
            assert stored.status == HypothesisStatus.REJECTED
        elif record.decision == ExperimentDecision.REFINE:
            assert stored.status == HypothesisStatus.TESTING

    def test_experiment_persisted_in_registry(self) -> None:
        orch = self._build_orchestrator()
        hypothesis = Hypothesis(text="persistence test")
        request = _default_eval_request()

        record = orch.run_cycle(hypothesis, request)

        stored = orch._experiments.get(record.id)
        assert stored is not None
        assert stored.id == record.id
        assert stored.decision == record.decision
