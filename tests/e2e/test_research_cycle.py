"""End-to-end tests for the full research cycle.

These tests wire every real component — compiler, DSL executor, signal-quality
evaluator, promotion judge, and registries — into a single research cycle and
verify the output.  No mocks, no stubs, no external services.

The ``e2e`` marker allows running just these tests::

    uv run pytest tests/e2e/ -m e2e -v
"""

from __future__ import annotations

import pandas as pd
import pytest

from alpha_harness.data.synthetic import generate_price_panel
from alpha_harness.evaluators.promotion_judge import PromotionJudge
from alpha_harness.evaluators.signal_quality import SignalQualityEvaluator
from alpha_harness.factors.compiler import FactorDslCompiler
from alpha_harness.orchestrator.research_loop import ResearchOrchestrator
from alpha_harness.registries.experiment import ExperimentRegistry
from alpha_harness.registries.hypothesis import HypothesisRegistry
from alpha_harness.schemas.evaluation import (
    EvaluationProfile,
    EvaluationRequest,
    LabelDefinition,
)
from alpha_harness.schemas.experiment import ExperimentDecision, ExperimentRecord
from alpha_harness.schemas.hypothesis import Hypothesis, HypothesisStatus
from alpha_harness.service import AlphaHarnessService

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def price_panel() -> pd.DataFrame:
    """10-symbol x ~85-day synthetic price panel."""
    return generate_price_panel(n_days=120, seed=42)


@pytest.fixture()
def relaxed_profile() -> EvaluationProfile:
    """Evaluation profile with relaxed thresholds for testing."""
    return EvaluationProfile(
        thresholds={"ic": 0.001, "rank_ic": 0.001, "quantile_spread": 0.0001},
        min_periods=10,
        min_assets=3,
        n_quantiles=5,
    )


@pytest.fixture()
def strict_profile() -> EvaluationProfile:
    """Evaluation profile with strict thresholds that most signals will fail."""
    return EvaluationProfile(
        thresholds={"ic": 0.50, "rank_ic": 0.50, "quantile_spread": 0.10},
        min_periods=10,
        min_assets=3,
        n_quantiles=5,
    )


def _make_eval_request(
    panel: pd.DataFrame,
    profile: EvaluationProfile,
) -> EvaluationRequest:
    """Build an EvaluationRequest spanning the full panel."""
    ts_dates = pd.to_datetime(panel["timestamp"]).dt.date
    return EvaluationRequest(
        factor_id="pending",
        universe_id="synthetic",
        eval_start=ts_dates.min(),
        eval_end=ts_dates.max(),
        label=LabelDefinition(forecast_horizon_bars=5, lag_bars=1),
        profile=profile,
    )


def _build_orchestrator(
    panel: pd.DataFrame,
    judge: PromotionJudge,
) -> tuple[ResearchOrchestrator, ExperimentRegistry, HypothesisRegistry]:
    """Wire all real components into an orchestrator."""
    compiler = FactorDslCompiler()
    evaluator = SignalQualityEvaluator(panel)
    service = AlphaHarnessService(compiler=compiler, evaluator=evaluator, judge=judge)
    exp_reg = ExperimentRegistry()
    hyp_reg = HypothesisRegistry()
    orch = ResearchOrchestrator(
        service=service,
        experiment_registry=exp_reg,
        hypothesis_registry=hyp_reg,
    )
    return orch, exp_reg, hyp_reg


# ── Tests ───────────────────────────────────────────────────────────────────


@pytest.mark.e2e
class TestFullResearchCycle:
    """End-to-end: hypothesis -> compile -> execute -> evaluate -> judge -> persist."""

    def test_single_cycle_returns_experiment_record(
        self, price_panel: pd.DataFrame, relaxed_profile: EvaluationProfile,
    ) -> None:
        """A single cycle produces a valid ExperimentRecord with all fields."""
        judge = PromotionJudge(refine_margin=0.20)
        orch, exp_reg, hyp_reg = _build_orchestrator(price_panel, judge)

        hypothesis = Hypothesis(
            text="rank(ts_mean(close, 20))",
            rationale="20-day moving average cross-sectional rank",
        )
        eval_req = _make_eval_request(price_panel, relaxed_profile)

        record = orch.run_cycle(hypothesis, eval_req)

        # Core assertions: we got a real ExperimentRecord
        assert isinstance(record, ExperimentRecord)
        assert record.hypothesis.id == hypothesis.id
        assert record.factor.expression == "rank(ts_mean(close, 20))"
        assert record.factor.operator_tree is not None
        assert record.evaluation.n_periods is not None
        assert record.evaluation.n_periods > 0
        assert record.evaluation.n_assets is not None
        assert record.evaluation.n_assets >= 3
        assert record.decision in list(ExperimentDecision)

        # Real metrics were computed (not None)
        assert record.evaluation.ic is not None
        assert record.evaluation.rank_ic is not None

        # Persisted in registries
        assert exp_reg.get(record.id) is not None
        assert hyp_reg.get(hypothesis.id) is not None

    def test_hypothesis_status_updated_after_cycle(
        self, price_panel: pd.DataFrame, relaxed_profile: EvaluationProfile,
    ) -> None:
        """The hypothesis status transitions from DRAFT through TESTING to a final state."""
        judge = PromotionJudge(refine_margin=0.20)
        orch, _exp_reg, hyp_reg = _build_orchestrator(price_panel, judge)

        hypothesis = Hypothesis(text="ts_mean(close, 10)")
        eval_req = _make_eval_request(price_panel, relaxed_profile)

        record = orch.run_cycle(hypothesis, eval_req)

        saved_hyp = hyp_reg.get(hypothesis.id)
        assert saved_hyp is not None
        # Final status should match decision
        if record.decision == ExperimentDecision.REJECT:
            assert saved_hyp.status == HypothesisStatus.REJECTED
        elif record.decision == ExperimentDecision.PROMOTE_CANDIDATE:
            assert saved_hyp.status == HypothesisStatus.PROMISING
        elif record.decision == ExperimentDecision.REFINE:
            assert saved_hyp.status == HypothesisStatus.TESTING
        elif record.decision == ExperimentDecision.ARCHIVE_ONLY:
            assert saved_hyp.status == HypothesisStatus.ARCHIVED

    def test_strict_profile_rejects(
        self, price_panel: pd.DataFrame, strict_profile: EvaluationProfile,
    ) -> None:
        """With unreachably strict thresholds, the signal is rejected."""
        judge = PromotionJudge(refine_margin=0.20)
        orch, _exp_reg, _hyp_reg = _build_orchestrator(price_panel, judge)

        hypothesis = Hypothesis(text="rank(close)")
        eval_req = _make_eval_request(price_panel, strict_profile)

        record = orch.run_cycle(hypothesis, eval_req)
        assert record.decision == ExperimentDecision.REJECT

    def test_invalid_expression_records_failure(
        self, price_panel: pd.DataFrame, relaxed_profile: EvaluationProfile,
    ) -> None:
        """An unparseable expression fails gracefully and records the error."""
        judge = PromotionJudge(refine_margin=0.20)
        orch, exp_reg, _hyp_reg = _build_orchestrator(price_panel, judge)

        hypothesis = Hypothesis(text="rank(ts_mean(close,))")  # bad syntax
        eval_req = _make_eval_request(price_panel, relaxed_profile)

        record = orch.run_cycle(hypothesis, eval_req)
        assert record.decision == ExperimentDecision.REJECT
        assert record.failure is not None
        is_comp_err = "compilation_error" in record.failure.category.value.lower()
        has_error = "error" in record.failure.detail.lower()
        assert is_comp_err or has_error

        # Still persisted
        assert exp_reg.get(record.id) is not None

    def test_batch_cycle(
        self, price_panel: pd.DataFrame, relaxed_profile: EvaluationProfile,
    ) -> None:
        """Batch mode evaluates multiple hypotheses and persists all results."""
        judge = PromotionJudge(refine_margin=0.20)
        orch, exp_reg, hyp_reg = _build_orchestrator(price_panel, judge)

        hypotheses = [
            Hypothesis(text="rank(ts_mean(close, 20))", rationale="momentum rank"),
            Hypothesis(text="zscore(close)", rationale="cross-sectional zscore"),
            Hypothesis(text="ts_delta(close, 5)", rationale="5-day price change"),
        ]
        eval_req = _make_eval_request(price_panel, relaxed_profile)

        records = orch.run_batch(hypotheses, eval_req)

        assert len(records) == 3
        assert all(isinstance(r, ExperimentRecord) for r in records)
        assert len(exp_reg.list_all()) == 3
        assert len(hyp_reg.list_all()) == 3

        # Each record has a unique id
        ids = {r.id for r in records}
        assert len(ids) == 3

    def test_multiple_expressions_produce_different_metrics(
        self, price_panel: pd.DataFrame, relaxed_profile: EvaluationProfile,
    ) -> None:
        """Different factor expressions produce different evaluation metrics."""
        judge = PromotionJudge(refine_margin=0.20)
        orch, _exp_reg, _hyp_reg = _build_orchestrator(price_panel, judge)
        eval_req = _make_eval_request(price_panel, relaxed_profile)

        r1 = orch.run_cycle(
            Hypothesis(text="rank(ts_mean(close, 5))"), eval_req,
        )
        r2 = orch.run_cycle(
            Hypothesis(text="rank(ts_mean(close, 40))"), eval_req,
        )

        # Different windows → different IC values (both should be non-None)
        assert r1.evaluation.ic is not None
        assert r2.evaluation.ic is not None
        # Very unlikely to be exactly equal with different window sizes
        assert r1.evaluation.ic != r2.evaluation.ic

    def test_round_trip_fidelity(
        self, price_panel: pd.DataFrame, relaxed_profile: EvaluationProfile,
    ) -> None:
        """All ExperimentRecord fields survive registry round-trip."""
        judge = PromotionJudge(refine_margin=0.20)
        orch, exp_reg, _hyp_reg = _build_orchestrator(price_panel, judge)

        hypothesis = Hypothesis(
            text="rank(ts_mean(close, 20))",
            rationale="test round-trip",
            tags=["e2e", "mvp"],
        )
        eval_req = _make_eval_request(price_panel, relaxed_profile)

        record = orch.run_cycle(hypothesis, eval_req)
        retrieved = exp_reg.get(record.id)

        assert retrieved is not None
        assert retrieved.id == record.id
        assert retrieved.factor.expression == record.factor.expression
        assert retrieved.evaluation.ic == record.evaluation.ic
        assert retrieved.evaluation.rank_ic == record.evaluation.rank_ic
        assert retrieved.hypothesis.rationale == "test round-trip"
        assert "e2e" in retrieved.hypothesis.tags


@pytest.mark.e2e
class TestCLIScript:
    """Verify the CLI script runs without errors."""

    def test_main_returns_zero(self) -> None:
        """The CLI script exits cleanly with default arguments."""
        from scripts.run_research_cycle import main

        exit_code = main(["--n-days", "120", "--seed", "42"])
        assert exit_code == 0

    def test_main_json_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        """The --json flag produces valid JSON output."""
        import json as json_mod

        from scripts.run_research_cycle import main

        exit_code = main(["--n-days", "120", "--seed", "42", "--json"])
        assert exit_code == 0

        captured = capsys.readouterr()
        # Should be valid JSON
        data = json_mod.loads(captured.out)
        assert "id" in data
        assert "decision" in data
        assert "evaluation" in data

    def test_main_custom_expression(self) -> None:
        """CLI with a custom expression runs successfully."""
        from scripts.run_research_cycle import main

        exit_code = main([
            "--expression", "zscore(ts_mean(close, 10))",
            "--n-days", "120",
            "--seed", "99",
        ])
        assert exit_code == 0
