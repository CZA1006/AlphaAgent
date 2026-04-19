"""Tests for deterministic lineage-memory entries and orchestrator wiring."""

from __future__ import annotations

from datetime import date

import pytest

from alpha_harness.evaluators.promotion_judge import PromotionJudge
from alpha_harness.memory.lineage import build_lineage_entry
from alpha_harness.orchestrator.research_loop import ResearchOrchestrator
from alpha_harness.registries.experiment import ExperimentRegistry
from alpha_harness.registries.hypothesis import HypothesisRegistry
from alpha_harness.registries.memory import MemoryRegistry
from alpha_harness.schemas.evaluation import EvaluationBundle, EvaluationRequest
from alpha_harness.schemas.experiment import (
    ExperimentDecision,
    ExperimentRecord,
    FailureCategory,
    FailureRecord,
)
from alpha_harness.schemas.factor import FactorSpec
from alpha_harness.schemas.hypothesis import Hypothesis
from alpha_harness.schemas.memory import MemoryCategory
from alpha_harness.service import AlphaHarnessService

DEFAULT_EVAL_REQUEST = EvaluationRequest(
    factor_id="placeholder",
    universe_id="test_universe",
    eval_start=date(2020, 1, 1),
    eval_end=date(2023, 12, 31),
)


# ── build_lineage_entry ──────────────────────────────────────────────────────


def _make_record(
    *,
    decision: ExperimentDecision = ExperimentDecision.REFINE,
    parent_id: str | None = None,
    ic: float | None = 0.0321,
    rank_ic: float | None = 0.041,
    failure: FailureRecord | None = None,
) -> ExperimentRecord:
    h = Hypothesis(text="rank(close)", parent_id=parent_id)
    return ExperimentRecord(
        hypothesis=h,
        factor=FactorSpec(name="f_rank_close", expression="rank(close)"),
        evaluation=EvaluationBundle(ic=ic, rank_ic=rank_ic),
        decision=decision,
        failure=failure,
    )


def test_lineage_entry_root_has_root_tag():
    record = _make_record()
    entry = build_lineage_entry(record)

    assert entry.category is MemoryCategory.EXPERIMENT_LINEAGE
    assert entry.source_experiment_ids == [record.id]
    assert "lineage" in entry.tags
    assert "root" in entry.tags
    assert "child" not in entry.tags
    assert record.decision.value in entry.tags


def test_lineage_entry_child_has_child_tag_and_parent_in_content():
    record = _make_record(parent_id="parent123")
    entry = build_lineage_entry(record)

    assert "child" in entry.tags
    assert "root" not in entry.tags
    assert "parent=parent123" in entry.content


def test_lineage_entry_content_format_is_key_value_pairs():
    record = _make_record(
        decision=ExperimentDecision.PROMOTE_CANDIDATE,
        ic=0.12345,
        rank_ic=0.0678,
    )
    entry = build_lineage_entry(record)

    assert f"exp={record.id}" in entry.content
    assert f"hypothesis={record.hypothesis.id}" in entry.content
    assert "factor=f_rank_close" in entry.content
    assert "decision=promote_candidate" in entry.content
    assert "parent=-" in entry.content
    assert "ic=0.1235" in entry.content  # 4-decimal rounded
    assert "rank_ic=0.0678" in entry.content
    assert "failure=-" in entry.content


def test_lineage_entry_missing_metrics_render_as_dash():
    record = _make_record(ic=None, rank_ic=None)
    entry = build_lineage_entry(record)
    assert "ic=-" in entry.content
    assert "rank_ic=-" in entry.content


def test_lineage_entry_failure_is_surfaced():
    failure = FailureRecord(
        category=FailureCategory.WEAK_SIGNAL, detail="ic too low",
    )
    record = _make_record(
        decision=ExperimentDecision.REJECT,
        ic=0.001,
        rank_ic=0.001,
        failure=failure,
    )
    entry = build_lineage_entry(record)
    assert "failure=weak_signal" in entry.content


# ── Orchestrator wiring ──────────────────────────────────────────────────────


class _PromoteEvaluator:
    def evaluate(self, factor, request):
        return EvaluationBundle(
            ic=0.08, rank_ic=0.08, quantile_spread=0.02,
        )


class _WeakEvaluator:
    def evaluate(self, factor, request):
        return EvaluationBundle(
            ic=0.0001, rank_ic=0.0001, quantile_spread=0.0,
        )


def _build_orchestrator(
    evaluator,
    memory_registry: MemoryRegistry | None = None,
    write_lineage: bool = True,
) -> tuple[ResearchOrchestrator, ExperimentRegistry, MemoryRegistry | None]:
    from alpha_harness.factors.compiler import FactorDslCompiler

    judge = PromotionJudge()
    service = AlphaHarnessService(
        compiler=FactorDslCompiler(),
        evaluator=evaluator,
        judge=judge,
    )
    experiments = ExperimentRegistry()
    hypotheses = HypothesisRegistry()
    orch = ResearchOrchestrator(
        service=service,
        experiment_registry=experiments,
        hypothesis_registry=hypotheses,
        memory_registry=memory_registry,
        write_lineage=write_lineage,
    )
    return orch, experiments, memory_registry


def test_orchestrator_without_memory_registry_does_not_write():
    orch, experiments, _ = _build_orchestrator(_PromoteEvaluator())
    record = orch.run_cycle(
        Hypothesis(text="rank(close)"), DEFAULT_EVAL_REQUEST,
    )
    assert record.decision == ExperimentDecision.PROMOTE_CANDIDATE
    # No memory registry → nothing raised; experiment still persisted.
    assert len(experiments.list_all()) == 1


def test_orchestrator_writes_lineage_entry_when_memory_enabled():
    memory = MemoryRegistry()
    orch, _, _ = _build_orchestrator(_PromoteEvaluator(), memory_registry=memory)

    h = Hypothesis(text="rank(close)")
    record = orch.run_cycle(h, DEFAULT_EVAL_REQUEST)

    entries = memory.list_by_category(MemoryCategory.EXPERIMENT_LINEAGE)
    assert len(entries) == 1
    entry = entries[0]
    assert record.id in entry.source_experiment_ids
    assert f"exp={record.id}" in entry.content


def test_orchestrator_lineage_write_can_be_disabled():
    memory = MemoryRegistry()
    orch, _, _ = _build_orchestrator(
        _PromoteEvaluator(),
        memory_registry=memory,
        write_lineage=False,
    )
    orch.run_cycle(Hypothesis(text="rank(close)"), DEFAULT_EVAL_REQUEST)
    assert memory.list_all() == []


def test_lineage_retrieval_by_experiment_id():
    memory = MemoryRegistry()
    orch, _, _ = _build_orchestrator(_WeakEvaluator(), memory_registry=memory)

    record = orch.run_cycle(
        Hypothesis(text="rank(close)"), DEFAULT_EVAL_REQUEST,
    )
    hits = memory.list_by_experiment(record.id)
    assert len(hits) == 1
    assert hits[0].category is MemoryCategory.EXPERIMENT_LINEAGE


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
