"""Tests for the AlphaHarnessService domain interface."""

from datetime import date

from alpha_harness.schemas.evaluation import EvaluationBundle, EvaluationRequest
from alpha_harness.schemas.experiment import ExperimentDecision, JudgmentDetail
from alpha_harness.schemas.factor import FactorSpec
from alpha_harness.schemas.hypothesis import Hypothesis
from alpha_harness.service import AlphaHarnessService

# ── Shared fixtures ──────────────────────────────────────────────────────────

DEFAULT_EVAL_REQUEST = EvaluationRequest(
    factor_id="placeholder",
    universe_id="test_universe",
    eval_start=date(2020, 1, 1),
    eval_end=date(2023, 12, 31),
)


# ── Stub implementations for testing ────────────────────────────────────────


class StubCompiler:
    def compile(self, hypothesis: Hypothesis) -> FactorSpec:
        return FactorSpec(
            name="stub_factor",
            expression="rank(close)",
            hypothesis_id=hypothesis.id,
        )


class StubEvaluator:
    def evaluate(self, factor: FactorSpec, request: EvaluationRequest) -> EvaluationBundle:
        return EvaluationBundle(
            ic=0.05,
            rank_ic=0.04,
            quantile_spread=0.01,
            forecast_horizon_bars=request.label.forecast_horizon_bars,
        )


class StubJudge:
    def judge(
        self,
        hypothesis: Hypothesis,
        factor: FactorSpec,
        evaluation: EvaluationBundle,
        request: EvaluationRequest,
    ) -> JudgmentDetail:
        if evaluation.passes_profile(request.profile):
            return JudgmentDetail(decision=ExperimentDecision.PROMOTE_CANDIDATE)
        return JudgmentDetail(decision=ExperimentDecision.REJECT)


# ── Tests ────────────────────────────────────────────────────────────────────


def test_compile_factor():
    svc = AlphaHarnessService(
        compiler=StubCompiler(),
        evaluator=StubEvaluator(),
        judge=StubJudge(),
    )
    h = Hypothesis(text="momentum in large caps")
    factor = svc.compile_factor(h)
    assert factor.hypothesis_id == h.id
    assert factor.name == "stub_factor"


def test_evaluate_factor():
    svc = AlphaHarnessService(
        compiler=StubCompiler(),
        evaluator=StubEvaluator(),
        judge=StubJudge(),
    )
    factor = FactorSpec(name="test", expression="close")
    evaluation = svc.evaluate_factor(factor, DEFAULT_EVAL_REQUEST)
    assert evaluation.ic == 0.05
    assert evaluation.forecast_horizon_bars == 5


def test_run_research_cycle_promotes():
    svc = AlphaHarnessService(
        compiler=StubCompiler(),
        evaluator=StubEvaluator(),
        judge=StubJudge(),
    )
    h = Hypothesis(text="momentum reversal")
    record = svc.run_research_cycle(h, DEFAULT_EVAL_REQUEST)
    assert record.decision == ExperimentDecision.PROMOTE_CANDIDATE
    assert record.hypothesis.text == "momentum reversal"
    assert record.factor.hypothesis_id == h.id
    assert record.evaluation.ic == 0.05
    assert record.eval_request is not None
    assert record.eval_request.universe_id == "test_universe"


def test_run_research_cycle_rejects():
    class WeakEvaluator:
        def evaluate(self, factor: FactorSpec, request: EvaluationRequest) -> EvaluationBundle:
            return EvaluationBundle(ic=0.01, rank_ic=0.01, quantile_spread=0.001)

    svc = AlphaHarnessService(
        compiler=StubCompiler(),
        evaluator=WeakEvaluator(),
        judge=StubJudge(),
    )
    h = Hypothesis(text="noise factor")
    record = svc.run_research_cycle(h, DEFAULT_EVAL_REQUEST)
    assert record.decision == ExperimentDecision.REJECT
