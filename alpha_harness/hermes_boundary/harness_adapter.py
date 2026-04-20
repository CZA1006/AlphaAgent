"""Concrete Hermes-facing adapter — composes Round 3 components.

This is the *real* :class:`AgentRuntimeAdapter` used by the autonomous
cycle script and (eventually) by Hermes itself.  It does three things
and nothing else:

    1. translate a raw agent output into a typed
       :class:`ResearchCycleRequest` (so it still satisfies the stub
       contract used by earlier tests);
    2. run a single cycle through the orchestrator + refinement runner;
    3. run a theme-level pipeline: proposer → multiple cycles, each
       optionally auto-refined.

Everything deterministic stays where it already lives — the factor
compiler, the evaluator, the judge, the novelty evaluator, the
refinement budgets.  The adapter never sees raw evaluation metrics, never
mutates an experiment record, and never overrides a decision.  It only
arranges the calls.

Boundary rule (AGENTS.md #8): the adapter calls *into* Alpha Harness
services.  Alpha Harness never calls back into Hermes.  The LLM enters
this module only through :class:`HypothesisProposer`, which itself goes
through ``request_structured`` so the raw model text is Pydantic-validated
before reaching the research loop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from alpha_harness.hermes_boundary.contracts import (
    CycleGoal,
    CycleOutcome,
    ResearchCycleRequest,
    ResearchCycleResponse,
    ThemeCycleRequest,
    ThemeCycleResponse,
)
from alpha_harness.orchestrator.refinement import RefinementRunner
from alpha_harness.orchestrator.research_loop import ResearchOrchestrator
from alpha_harness.proposer import (
    HypothesisProposer,
    ProposalRequest,
)
from alpha_harness.registries.protocols import ExperimentRegistryProtocol
from alpha_harness.schemas.evaluation import EvaluationRequest
from alpha_harness.schemas.experiment import ExperimentDecision, ExperimentRecord
from alpha_harness.schemas.hypothesis import AssetClass, Hypothesis

logger = logging.getLogger(__name__)


# ── Helpers ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _RunResult:
    """Tuple-ish pair used internally when we run one mutation-aware cycle."""

    root: ExperimentRecord
    children: list[ExperimentRecord]


# ── Adapter ──────────────────────────────────────────────────────────────────


class HarnessAgentAdapter:
    """Thin composition adapter in front of the Round 3 research stack.

    Parameters
    ----------
    orchestrator:
        The :class:`ResearchOrchestrator` that actually runs a single
        cycle (compile → evaluate → judge → persist).
    eval_request:
        The :class:`EvaluationRequest` to pass through on every cycle.
        Fixing it at adapter-construction time keeps the request surface
        agent-friendly (no evaluation knobs leaking into the prompt).
    experiment_registry:
        Used to look up experiments for :meth:`translate_to_response`.
    proposer:
        Optional :class:`HypothesisProposer`.  Required only for
        :meth:`run_theme`; single-cycle translation works without it.
    refinement_runner:
        Optional :class:`RefinementRunner`.  When provided, a REFINE root
        verdict automatically expands into bounded variants; when absent,
        REFINE is treated as a terminal state for that cycle.
    """

    def __init__(
        self,
        orchestrator: ResearchOrchestrator,
        eval_request: EvaluationRequest,
        experiment_registry: ExperimentRegistryProtocol,
        *,
        proposer: HypothesisProposer | None = None,
        refinement_runner: RefinementRunner | None = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._eval_request = eval_request
        self._experiments = experiment_registry
        self._proposer = proposer
        self._refinement = refinement_runner

    # ── AgentRuntimeAdapter protocol methods ─────────────────────────────

    def translate_to_request(self, agent_output: str) -> ResearchCycleRequest:
        """Treat raw agent output as a DSL hypothesis.

        Matches :class:`StubAgentRuntimeAdapter` so call sites that already
        use the stub work unchanged.  Structured hypothesis extraction
        (rationale, tags, goal) is the Hermes runtime's responsibility —
        we do not re-parse natural language here.
        """
        return ResearchCycleRequest(
            hypothesis_text=agent_output.strip(),
            hypothesis_rationale="",
            goal=CycleGoal.EXPLORE,
        )

    def translate_to_response(
        self, experiment_id: str,
    ) -> ResearchCycleResponse:
        """Build an agent-friendly response from a stored experiment."""
        record = self._experiments.get(experiment_id)
        if record is None:
            return ResearchCycleResponse(
                experiment_id=experiment_id,
                hypothesis_id="unknown",
                factor_name="unknown",
                outcome=CycleOutcome.ERROR,
                failure_detail=f"Experiment {experiment_id} not found.",
            )
        return _record_to_response(record)

    # ── Single-cycle entry point ─────────────────────────────────────────

    def run_cycle(
        self, request: ResearchCycleRequest,
    ) -> ResearchCycleResponse:
        """Run a single research cycle from a typed request.

        The adapter does not branch on ``request.goal`` beyond wiring the
        hypothesis; whether to REFINE is the :class:`PromotionJudge`'s
        decision, not the agent's.  If a :class:`RefinementRunner` is
        available, REFINE verdicts still expand into child experiments —
        but only the root response is returned here, mirroring the
        single-cycle contract.

        Goals supported in Round 3: ``EXPLORE`` (new hypothesis) and
        ``REFINE`` (iterate on a parent).  ``AUDIT`` and ``SUMMARISE`` are
        reserved for later rounds and raise :class:`ValueError` so agents
        surface the mismatch instead of silently running the wrong path.
        """
        if request.goal not in (CycleGoal.EXPLORE, CycleGoal.REFINE):
            raise ValueError(
                f"Unsupported CycleGoal {request.goal.value!r}; "
                "adapter handles only 'explore' and 'refine' in Round 3.",
            )
        hypothesis = _request_to_hypothesis(request)
        root = self._run_hypothesis(hypothesis).root
        return _record_to_response(root)

    # ── Theme-level entry point ──────────────────────────────────────────

    def run_theme(self, request: ThemeCycleRequest) -> ThemeCycleResponse:
        """Propose → run → auto-refine for a single research theme.

        Requires a :class:`HypothesisProposer`.  Each surviving proposal
        becomes a root hypothesis; every root goes through the same
        compile/evaluate/judge/persist path as a single cycle, plus the
        refinement loop when one is configured.
        """
        if self._proposer is None:
            raise RuntimeError(
                "run_theme requires a HypothesisProposer; none configured.",
            )

        proposal_request = ProposalRequest(
            theme=request.theme,
            asset_class=_coerce_asset_class(request.asset_class),
            n_candidates=request.n_candidates,
            extra_guidance=request.extra_guidance,
            prior_memory=request.prior_memory,
        )
        logger.info(
            "Theme cycle: theme=%r n_candidates=%d",
            request.theme, request.n_candidates,
        )
        proposal_result = self._proposer.propose(proposal_request)

        roots: list[ResearchCycleResponse] = []
        refinements: list[ResearchCycleResponse] = []

        extra_tags = (*request.tags, "theme_cycle")
        hypotheses = proposal_result.to_hypotheses(
            asset_class=_coerce_asset_class(request.asset_class),
            source="harness_adapter",
            extra_tags=extra_tags,
        )

        for hypothesis in hypotheses:
            outcome = self._run_hypothesis(hypothesis)
            roots.append(_record_to_response(outcome.root))
            refinements.extend(
                _record_to_response(child) for child in outcome.children
            )

        return ThemeCycleResponse(
            theme=request.theme,
            proposals_requested=request.n_candidates,
            proposals_accepted=len(hypotheses),
            proposals_dropped=len(proposal_result.dropped),
            roots=roots,
            refinements=refinements,
            dropped_reasons=[d.reason for d in proposal_result.dropped],
        )

    # ── Internals ────────────────────────────────────────────────────────

    def _run_hypothesis(self, hypothesis: Hypothesis) -> _RunResult:
        """Run one hypothesis through the orchestrator (+ refinement if any)."""
        if self._refinement is not None:
            result = self._refinement.run(hypothesis, self._eval_request)
            return _RunResult(root=result.root, children=list(result.children))
        record = self._orchestrator.run_cycle(hypothesis, self._eval_request)
        return _RunResult(root=record, children=[])


# ── Module-level helpers ────────────────────────────────────────────────────


def _coerce_asset_class(raw: str) -> AssetClass:
    """Coerce a boundary-level string into the typed enum; default US equity."""
    try:
        return AssetClass(raw)
    except ValueError:
        logger.warning(
            "Unknown asset_class %r; falling back to us_equity.", raw,
        )
        return AssetClass.US_EQUITY


def _request_to_hypothesis(request: ResearchCycleRequest) -> Hypothesis:
    """Translate a boundary-level request into a domain :class:`Hypothesis`."""
    return Hypothesis(
        text=request.hypothesis_text,
        rationale=request.hypothesis_rationale,
        source="hermes_adapter",
        asset_class=_coerce_asset_class(request.asset_class),
        tags=list(request.tags),
        parent_id=request.parent_hypothesis_id,
    )


def _record_to_response(record: ExperimentRecord) -> ResearchCycleResponse:
    """Flatten an :class:`ExperimentRecord` into an agent-friendly response."""
    return ResearchCycleResponse(
        experiment_id=record.id,
        hypothesis_id=record.hypothesis.id,
        factor_name=record.factor.name,
        outcome=_decision_to_outcome(record.decision),
        failure_category=(
            record.failure.category.value if record.failure else None
        ),
        failure_detail=(record.failure.detail if record.failure else ""),
        notes=record.notes,
        ic=record.evaluation.ic,
        rank_ic=record.evaluation.rank_ic,
        sharpe=record.evaluation.sharpe,
    )


def _decision_to_outcome(decision: ExperimentDecision) -> CycleOutcome:
    mapping: dict[ExperimentDecision, CycleOutcome] = {
        ExperimentDecision.PROMOTE_CANDIDATE: CycleOutcome.PROMOTED,
        ExperimentDecision.REFINE: CycleOutcome.REFINED,
        ExperimentDecision.REJECT: CycleOutcome.REJECTED,
        ExperimentDecision.ARCHIVE_ONLY: CycleOutcome.REJECTED,
    }
    return mapping.get(decision, CycleOutcome.ERROR)
