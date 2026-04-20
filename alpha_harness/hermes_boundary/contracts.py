"""Boundary contracts — shared types for adapter communication.

These types define the data that flows across the Hermes / Alpha Harness
boundary.  They are plain Pydantic models with NO dependency on either
Hermes internals or Alpha Harness evaluators.  Both sides can import
these freely.

Design constraints:
    - No LLM-specific types (no Message, no ToolCall, no Prompt).
    - No evaluator internals (no EvaluationBundle fields leaking out).
    - Serialisable to JSON for inter-process / HTTP boundary if needed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

# ── Requests from Hermes into Alpha Harness ──────────────────────────────────


class CycleGoal(StrEnum):
    """What the agent wants the harness to do in this cycle."""

    EXPLORE = "explore"          # test a new hypothesis
    REFINE = "refine"            # iterate on an existing hypothesis
    AUDIT = "audit"              # re-evaluate an existing factor with new data
    SUMMARISE = "summarise"      # produce a research summary (no new evaluation)


class ResearchCycleRequest(BaseModel):
    """A request from the Hermes agent to run one research cycle.

    This is the primary inbound contract.  The Hermes adapter translates
    an agent's natural-language intent into this typed request, then
    passes it to the ResearchOrchestrator.

    Everything the orchestrator needs must be present here or derivable
    from harness-owned state (registries, data loaders).  The agent
    should NOT embed evaluation parameters in its prompt — those come
    from the EvaluationProfile.
    """

    hypothesis_text: str
    hypothesis_rationale: str = ""
    asset_class: str = "us_equity"
    goal: CycleGoal = CycleGoal.EXPLORE
    parent_hypothesis_id: str | None = None     # for REFINE cycles
    tags: list[str] = Field(default_factory=list)
    requested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ── Responses from Alpha Harness back to Hermes ─────────────────────────────


class CycleOutcome(StrEnum):
    """Simplified outcome for the agent to reason about."""

    PROMOTED = "promoted"
    REFINED = "refined"
    REJECTED = "rejected"
    ERROR = "error"


class ResearchCycleResponse(BaseModel):
    """A response from the harness back to the Hermes agent.

    This is a simplified, agent-friendly view of an ExperimentRecord.
    It contains just enough information for the LLM to decide what to
    do next (propose a new hypothesis, refine, or move on) without
    leaking evaluator internals into the prompt.

    The full ExperimentRecord is always available in the registry for
    programmatic access — this response is for the LLM context window.
    """

    experiment_id: str
    hypothesis_id: str
    factor_name: str
    outcome: CycleOutcome
    failure_category: str | None = None    # from FailureCategory enum
    failure_detail: str = ""
    notes: str = ""

    # Summary metrics — enough for the agent to learn, not enough to
    # reconstruct the evaluation (that would bloat the context window).
    ic: float | None = None
    rank_ic: float | None = None
    sharpe: float | None = None

    completed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ── Memory context for prompt injection ──────────────────────────────────────


class ThemeCycleRequest(BaseModel):
    """High-level request: take a research theme, run the full Round 3 loop.

    Used by the concrete :class:`HarnessAgentAdapter` — the Hermes agent
    (or a script standing in for it) hands over a free-form research theme
    and a handful of knobs; the adapter owns the theme → proposer → cycle
    → refinement pipeline.  Evaluation parameters are never part of this
    request: they come from the harness-owned ``EvaluationRequest``.
    """

    theme: str
    asset_class: str = "us_equity"
    n_candidates: int = 3
    extra_guidance: str = ""
    tags: list[str] = Field(default_factory=list)
    # Optional rolling-memory digest built from the experiment registry.
    # Empty string means "no memory context" — proposer prompt stays
    # byte-identical to pre-4A.4 behaviour.
    prior_memory: str = ""
    requested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ThemeCycleResponse(BaseModel):
    """Structured summary of a theme-level run.

    Contains one :class:`ResearchCycleResponse` per root hypothesis that
    actually made it into the research loop, plus aggregate counts the
    agent can reason about without pulling full records.
    """

    theme: str
    proposals_requested: int
    proposals_accepted: int
    proposals_dropped: int
    roots: list[ResearchCycleResponse] = Field(default_factory=list)
    refinements: list[ResearchCycleResponse] = Field(default_factory=list)
    dropped_reasons: list[str] = Field(default_factory=list)
    completed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def total_cycles(self) -> int:
        return len(self.roots) + len(self.refinements)


# ── Memory context for prompt injection ──────────────────────────────────────


class MemoryContext(BaseModel):
    """A curated package of research memory for injection into agent context.

    The ContextInjectionAdapter selects relevant memory entries and packs
    them into this structure.  The Hermes prompt assembler can then inject
    it as a system-message section.

    This is NOT the full memory store — it is a filtered, token-budgeted
    snapshot designed for a single agent step.
    """

    success_patterns: list[str] = Field(default_factory=list)
    failure_patterns: list[str] = Field(default_factory=list)
    recent_experiment_summaries: list[str] = Field(default_factory=list)
    active_hypotheses_count: int = 0
    promoted_factors_count: int = 0
    total_experiments: int = 0
    token_budget_used: int = 0     # approximate tokens consumed by this context
