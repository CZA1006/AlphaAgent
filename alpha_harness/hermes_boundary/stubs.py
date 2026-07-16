"""Stub adapter implementations — work without Hermes installed.

These stubs allow the full Alpha Harness pipeline to run and be tested
without any Hermes dependency.  They implement the adapter protocols
with minimal, deterministic behaviour.

Usage:
    from alpha_harness.hermes_boundary.stubs import (
        StubAgentRuntimeAdapter,
        StubMemoryProvider,
        StubContextInjector,
    )

    adapter = StubAgentRuntimeAdapter(experiment_registry=registry)
    request = adapter.translate_to_request("momentum in large caps")
"""

from __future__ import annotations

from alpha_harness.hermes_boundary.contracts import (
    CycleGoal,
    CycleOutcome,
    MemoryContext,
    ResearchCycleRequest,
    ResearchCycleResponse,
)
from alpha_harness.registries.experiment import ExperimentRegistry
from alpha_harness.registries.memory import MemoryRegistry
from alpha_harness.schemas.experiment import ExperimentDecision
from alpha_harness.schemas.memory import MemoryCategory, MemoryEntry


class StubAgentRuntimeAdapter:
    """Minimal adapter that parses raw text into a ResearchCycleRequest.

    Implements the AgentRuntimeAdapter protocol without any LLM parsing.
    Treats the entire agent output string as the hypothesis text.

    In production, the real Hermes adapter would:
        - Parse structured JSON output from the agent.
        - Extract hypothesis, rationale, asset class, and goal.
        - Validate against agent-specific output schemas.
    """

    def __init__(self, experiment_registry: ExperimentRegistry) -> None:
        self._experiments = experiment_registry

    def translate_to_request(self, agent_output: str) -> ResearchCycleRequest:
        """Treat raw text as a hypothesis. No LLM parsing."""
        return ResearchCycleRequest(
            hypothesis_text=agent_output.strip(),
            hypothesis_rationale="(stub — no rationale extracted)",
            goal=CycleGoal.EXPLORE,
        )

    def translate_to_response(self, experiment_id: str) -> ResearchCycleResponse:
        """Look up the experiment and build a simplified response."""
        record = self._experiments.get(experiment_id)
        if record is None:
            return ResearchCycleResponse(
                experiment_id=experiment_id,
                hypothesis_id="unknown",
                factor_name="unknown",
                outcome=CycleOutcome.ERROR,
                failure_detail=f"Experiment {experiment_id} not found.",
            )

        outcome = _decision_to_outcome(record.decision)
        return ResearchCycleResponse(
            experiment_id=record.id,
            hypothesis_id=record.hypothesis.id,
            factor_name=record.factor.name,
            outcome=outcome,
            failure_category=(record.failure.category.value if record.failure else None),
            failure_detail=(record.failure.detail if record.failure else ""),
            notes=record.notes,
            ic=record.evaluation.ic,
            rank_ic=record.evaluation.rank_ic,
            sharpe=record.evaluation.sharpe,
        )


class StubMemoryProvider:
    """In-memory storage backed by the harness MemoryRegistry.

    Implements the MemoryProviderAdapter protocol using the existing
    registry infrastructure. No external storage system needed.
    """

    def __init__(self, memory_registry: MemoryRegistry) -> None:
        self._registry = memory_registry

    def store(self, entry: MemoryEntry) -> str:
        """Persist via the harness registry."""
        return self._registry.save(entry)

    def retrieve_by_tags(self, tags: list[str], limit: int = 10) -> list[MemoryEntry]:
        """Retrieve entries matching any tag."""
        results: list[MemoryEntry] = []
        for tag in tags:
            results.extend(self._registry.list_by_tag(tag))
        # Deduplicate by id, preserve order
        seen: set[str] = set()
        unique: list[MemoryEntry] = []
        for entry in results:
            if entry.id not in seen:
                seen.add(entry.id)
                unique.append(entry)
        return unique[:limit]

    def retrieve_recent(self, limit: int = 10) -> list[MemoryEntry]:
        """Retrieve most recent entries by creation time."""
        all_entries = self._registry.list_all()
        sorted_entries = sorted(all_entries, key=lambda e: e.created_at, reverse=True)
        return sorted_entries[:limit]


class StubContextInjector:
    """Builds MemoryContext from harness registries.

    Implements the ContextInjectionAdapter protocol. Queries the memory
    and experiment registries to build a research context snapshot.

    Token budget is approximate — each summary string is counted as
    ~4 chars per token (rough estimate for English text).
    """

    CHARS_PER_TOKEN = 4  # rough approximation

    def __init__(
        self,
        memory_registry: MemoryRegistry,
        experiment_registry: ExperimentRegistry,
    ) -> None:
        self._memory = memory_registry
        self._experiments = experiment_registry

    def build_context(self, token_budget: int = 2000) -> MemoryContext:
        """Build a token-budgeted research context snapshot."""
        char_budget = token_budget * self.CHARS_PER_TOKEN
        chars_used = 0

        # Collect success patterns
        success_patterns: list[str] = []
        for entry in self._memory.list_by_category(MemoryCategory.SUCCESS_PATTERN):
            if chars_used + len(entry.content) > char_budget:
                break
            success_patterns.append(entry.content)
            chars_used += len(entry.content)

        # Collect failure patterns
        failure_patterns: list[str] = []
        for entry in self._memory.list_by_category(MemoryCategory.FAILURE_PATTERN):
            if chars_used + len(entry.content) > char_budget:
                break
            failure_patterns.append(entry.content)
            chars_used += len(entry.content)

        # Recent experiment summaries
        recent_summaries: list[str] = []
        all_experiments = self._experiments.list_all()
        recent = sorted(all_experiments, key=lambda e: e.created_at, reverse=True)[:5]
        for exp in recent:
            summary = f"{exp.factor.name}: {exp.decision.value} (ic={exp.evaluation.ic})"
            if chars_used + len(summary) > char_budget:
                break
            recent_summaries.append(summary)
            chars_used += len(summary)

        # Counts (reuse all_experiments from above)
        promoted_count = len(self._experiments.list_promoted())

        return MemoryContext(
            success_patterns=success_patterns,
            failure_patterns=failure_patterns,
            recent_experiment_summaries=recent_summaries,
            active_hypotheses_count=0,  # would need HypothesisRegistry
            promoted_factors_count=promoted_count,
            total_experiments=len(all_experiments),
            token_budget_used=chars_used // self.CHARS_PER_TOKEN,
        )


def _decision_to_outcome(decision: ExperimentDecision) -> CycleOutcome:
    """Map internal decision enum to the agent-facing outcome enum."""
    mapping: dict[ExperimentDecision, CycleOutcome] = {
        ExperimentDecision.PROMOTE_CANDIDATE: CycleOutcome.PROMOTED,
        ExperimentDecision.REFINE: CycleOutcome.REFINED,
        ExperimentDecision.REJECT: CycleOutcome.REJECTED,
        ExperimentDecision.ARCHIVE_ONLY: CycleOutcome.REJECTED,
    }
    return mapping.get(decision, CycleOutcome.ERROR)
