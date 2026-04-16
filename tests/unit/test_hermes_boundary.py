"""Tests for the Hermes integration boundary — contracts, adapters, and stubs."""

from datetime import date

from alpha_harness.evaluators.promotion_judge import PromotionJudge
from alpha_harness.factors.compiler import FactorDslCompiler
from alpha_harness.hermes_boundary.contracts import (
    CycleGoal,
    CycleOutcome,
    MemoryContext,
    ResearchCycleRequest,
    ResearchCycleResponse,
)
from alpha_harness.hermes_boundary.stubs import (
    StubAgentRuntimeAdapter,
    StubContextInjector,
    StubMemoryProvider,
)
from alpha_harness.orchestrator.research_loop import ResearchOrchestrator
from alpha_harness.registries.experiment import ExperimentRegistry
from alpha_harness.registries.hypothesis import HypothesisRegistry
from alpha_harness.registries.memory import MemoryRegistry
from alpha_harness.schemas.evaluation import EvaluationRequest
from alpha_harness.schemas.memory import MemoryCategory, MemoryEntry
from alpha_harness.service import AlphaHarnessService
from tests.helpers.stubs import StubSignalQualityEvaluator

# ── Helpers ──────────────────────────────────────────────────────────────────


def _default_eval_request() -> EvaluationRequest:
    return EvaluationRequest(
        factor_id="placeholder",
        universe_id="test_universe",
        eval_start=date(2020, 1, 1),
        eval_end=date(2023, 12, 31),
    )


def _build_stack() -> (
    tuple[ResearchOrchestrator, ExperimentRegistry, HypothesisRegistry, MemoryRegistry]
):
    """Build a full harness stack with stub implementations."""
    compiler = FactorDslCompiler()
    evaluator = StubSignalQualityEvaluator()
    judge = PromotionJudge()
    service = AlphaHarnessService(compiler=compiler, evaluator=evaluator, judge=judge)
    exp_reg = ExperimentRegistry()
    hyp_reg = HypothesisRegistry()
    mem_reg = MemoryRegistry()
    orch = ResearchOrchestrator(
        service=service,
        judge=judge,
        experiment_registry=exp_reg,
        hypothesis_registry=hyp_reg,
    )
    return orch, exp_reg, hyp_reg, mem_reg


# ── Contract models ─────────────────────────────────────────────────────────


class TestContracts:
    def test_research_cycle_request_defaults(self) -> None:
        req = ResearchCycleRequest(hypothesis_text="momentum reversal")
        assert req.hypothesis_text == "momentum reversal"
        assert req.goal == CycleGoal.EXPLORE
        assert req.asset_class == "us_equity"
        assert req.parent_hypothesis_id is None

    def test_research_cycle_response_serialisable(self) -> None:
        resp = ResearchCycleResponse(
            experiment_id="exp001",
            hypothesis_id="hyp001",
            factor_name="momentum_20d",
            outcome=CycleOutcome.PROMOTED,
            ic=0.05,
            rank_ic=0.04,
        )
        data = resp.model_dump()
        assert data["outcome"] == "promoted"
        assert data["ic"] == 0.05

    def test_memory_context_defaults(self) -> None:
        ctx = MemoryContext()
        assert ctx.success_patterns == []
        assert ctx.total_experiments == 0
        assert ctx.token_budget_used == 0


# ── StubAgentRuntimeAdapter ─────────────────────────────────────────────────


class TestStubAgentRuntimeAdapter:
    def test_translate_to_request(self) -> None:
        _, exp_reg, _, _ = _build_stack()
        adapter = StubAgentRuntimeAdapter(experiment_registry=exp_reg)

        request = adapter.translate_to_request("  momentum in large caps  ")

        assert request.hypothesis_text == "momentum in large caps"
        assert request.goal == CycleGoal.EXPLORE

    def test_translate_to_response_missing(self) -> None:
        _, exp_reg, _, _ = _build_stack()
        adapter = StubAgentRuntimeAdapter(experiment_registry=exp_reg)

        response = adapter.translate_to_response("nonexistent_id")

        assert response.outcome == CycleOutcome.ERROR
        assert "not found" in response.failure_detail

    def test_translate_to_response_after_cycle(self) -> None:
        """Run a cycle, then translate the result to an agent-friendly response."""
        from alpha_harness.schemas.hypothesis import Hypothesis

        orch, exp_reg, _, _ = _build_stack()
        adapter = StubAgentRuntimeAdapter(experiment_registry=exp_reg)

        # Run a research cycle
        hypothesis = Hypothesis(text="ts_delta(volume, 5)")
        record = orch.run_cycle(hypothesis, _default_eval_request())

        # Translate the result
        response = adapter.translate_to_response(record.id)

        assert response.experiment_id == record.id
        assert response.hypothesis_id == hypothesis.id
        assert response.factor_name != ""
        assert response.outcome in list(CycleOutcome)
        assert response.ic is not None


# ── StubMemoryProvider ───────────────────────────────────────────────────────


class TestStubMemoryProvider:
    def test_store_and_retrieve(self) -> None:
        mem_reg = MemoryRegistry()
        provider = StubMemoryProvider(memory_registry=mem_reg)

        entry = MemoryEntry(
            category=MemoryCategory.SUCCESS_PATTERN,
            content="Momentum factors work best in trending markets.",
            tags=["momentum", "regime"],
        )
        entry_id = provider.store(entry)

        assert entry_id == entry.id
        assert mem_reg.get(entry_id) is not None

    def test_retrieve_by_tags(self) -> None:
        mem_reg = MemoryRegistry()
        provider = StubMemoryProvider(memory_registry=mem_reg)

        provider.store(MemoryEntry(
            category=MemoryCategory.SUCCESS_PATTERN,
            content="Pattern A",
            tags=["momentum"],
        ))
        provider.store(MemoryEntry(
            category=MemoryCategory.FAILURE_PATTERN,
            content="Pattern B",
            tags=["mean_reversion"],
        ))

        results = provider.retrieve_by_tags(["momentum"])
        assert len(results) == 1
        assert results[0].content == "Pattern A"

    def test_retrieve_recent(self) -> None:
        mem_reg = MemoryRegistry()
        provider = StubMemoryProvider(memory_registry=mem_reg)

        for i in range(5):
            provider.store(MemoryEntry(
                category=MemoryCategory.EXPERIMENT_LINEAGE,
                content=f"Entry {i}",
            ))

        recent = provider.retrieve_recent(limit=3)
        assert len(recent) == 3


# ── StubContextInjector ──────────────────────────────────────────────────────


class TestStubContextInjector:
    def test_empty_context(self) -> None:
        mem_reg = MemoryRegistry()
        exp_reg = ExperimentRegistry()
        injector = StubContextInjector(
            memory_registry=mem_reg,
            experiment_registry=exp_reg,
        )

        ctx = injector.build_context()

        assert ctx.success_patterns == []
        assert ctx.failure_patterns == []
        assert ctx.recent_experiment_summaries == []
        assert ctx.total_experiments == 0
        assert ctx.promoted_factors_count == 0

    def test_context_with_memory_and_experiments(self) -> None:
        """Build context after populating memory and running experiments."""
        from alpha_harness.schemas.hypothesis import Hypothesis

        orch, exp_reg, _, mem_reg = _build_stack()
        injector = StubContextInjector(
            memory_registry=mem_reg,
            experiment_registry=exp_reg,
        )

        # Add some memory entries
        mem_reg.save(MemoryEntry(
            category=MemoryCategory.SUCCESS_PATTERN,
            content="High IC in momentum factors during uptrends.",
        ))
        mem_reg.save(MemoryEntry(
            category=MemoryCategory.FAILURE_PATTERN,
            content="Mean reversion fails in trending regimes.",
        ))

        # Run a research cycle to populate experiments
        hypothesis = Hypothesis(text="ts_mean(close, 10)")
        orch.run_cycle(hypothesis, _default_eval_request())

        ctx = injector.build_context()

        assert len(ctx.success_patterns) == 1
        assert len(ctx.failure_patterns) == 1
        assert ctx.total_experiments == 1
        assert len(ctx.recent_experiment_summaries) == 1
        assert ctx.token_budget_used > 0

    def test_respects_token_budget(self) -> None:
        mem_reg = MemoryRegistry()
        exp_reg = ExperimentRegistry()
        injector = StubContextInjector(
            memory_registry=mem_reg,
            experiment_registry=exp_reg,
        )

        # Add a large memory entry
        mem_reg.save(MemoryEntry(
            category=MemoryCategory.SUCCESS_PATTERN,
            content="x" * 10000,  # very long entry
        ))

        # Small budget should truncate
        ctx = injector.build_context(token_budget=100)
        # With 100 tokens * 4 chars = 400 char budget, the 10k entry is skipped
        assert len(ctx.success_patterns) == 0


# ── End-to-end: agent → adapter → orchestrator → response ───────────────────


class TestEndToEndBoundary:
    def test_full_agent_cycle(self) -> None:
        """Simulate: agent proposes → adapter translates → orchestrator runs → response."""
        from alpha_harness.schemas.hypothesis import Hypothesis

        orch, exp_reg, _, _ = _build_stack()
        adapter = StubAgentRuntimeAdapter(experiment_registry=exp_reg)

        # 1. Agent "says" a hypothesis
        agent_text = "zscore(volume, 20)"

        # 2. Adapter translates to request
        request = adapter.translate_to_request(agent_text)
        assert request.hypothesis_text == agent_text

        # 3. Orchestrator runs the cycle
        hypothesis = Hypothesis(
            text=request.hypothesis_text,
            rationale=request.hypothesis_rationale,
        )
        record = orch.run_cycle(hypothesis, _default_eval_request())

        # 4. Adapter translates result for the agent
        response = adapter.translate_to_response(record.id)

        # 5. Verify the response is agent-friendly
        assert response.outcome in list(CycleOutcome)
        assert response.experiment_id == record.id
        assert response.factor_name != ""
        # The agent can now reason about the outcome
        if response.outcome == CycleOutcome.REJECTED:
            assert response.failure_category is not None or response.failure_detail != ""
