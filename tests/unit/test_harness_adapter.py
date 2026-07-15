"""Tests for :class:`HarnessAgentAdapter`.

The adapter is a composition layer — every assertion here is about
*orchestration shape*, not about evaluator truth.  We use stubs for the
evaluator (so REFINE/PROMOTE/REJECT verdicts are deterministic) and a
:class:`MockLLMClient` for the proposer.
"""

from __future__ import annotations

from datetime import date

import pytest

from alpha_harness.evaluators.promotion_judge import PromotionJudge
from alpha_harness.factors.compiler import FactorDslCompiler
from alpha_harness.hermes_boundary.contracts import (
    CycleGoal,
    CycleOutcome,
    ResearchCycleRequest,
    ThemeCycleRequest,
)
from alpha_harness.hermes_boundary.harness_adapter import HarnessAgentAdapter
from alpha_harness.llm import MockLLMClient
from alpha_harness.orchestrator.refinement import RefinementConfig, RefinementRunner
from alpha_harness.orchestrator.research_loop import ResearchOrchestrator
from alpha_harness.proposer import HypothesisProposer
from alpha_harness.proposer.schemas import RawProposal, RawProposalBatch
from alpha_harness.registries.experiment import ExperimentRegistry
from alpha_harness.registries.hypothesis import HypothesisRegistry
from alpha_harness.registries.memory import MemoryRegistry
from alpha_harness.schemas.evaluation import EvaluationBundle, EvaluationRequest
from alpha_harness.schemas.experiment import ExperimentDecision
from alpha_harness.schemas.factor import FactorSpec
from alpha_harness.service import AlphaHarnessService

DEFAULT_EVAL_REQUEST = EvaluationRequest(
    factor_id="placeholder",
    universe_id="test_universe",
    eval_start=date(2020, 1, 1),
    eval_end=date(2023, 12, 31),
)


# ── Helpers ─────────────────────────────────────────────────────────────────


class _ScriptedEvaluator:
    """Map expressions to (ic, rank_ic, quantile_spread)."""

    def __init__(
        self,
        mapping: dict[str, tuple[float, float, float]],
        default: tuple[float, float, float] = (0.0, 0.0, 0.0),
    ) -> None:
        self._mapping = mapping
        self._default = default

    def evaluate(
        self, factor: FactorSpec, request: EvaluationRequest,
    ) -> EvaluationBundle:
        ic, rank_ic, spread = self._mapping.get(
            factor.expression, self._default,
        )
        return EvaluationBundle(ic=ic, rank_ic=rank_ic, quantile_spread=spread)


def _build_stack(
    evaluator: _ScriptedEvaluator,
    *,
    with_refinement: bool = True,
    with_proposer: bool = False,
    mock_proposals: list[RawProposal] | None = None,
) -> tuple[HarnessAgentAdapter, ExperimentRegistry]:
    compiler = FactorDslCompiler()
    judge = PromotionJudge()
    service = AlphaHarnessService(
        compiler=compiler, evaluator=evaluator, judge=judge,
    )
    experiments = ExperimentRegistry()
    orchestrator = ResearchOrchestrator(
        service=service,
        experiment_registry=experiments,
        hypothesis_registry=HypothesisRegistry(),
        memory_registry=MemoryRegistry(),
    )
    refinement = (
        RefinementRunner(
            orchestrator,
            config=RefinementConfig(max_depth=1, max_variants_per_step=2),
        )
        if with_refinement
        else None
    )

    proposer: HypothesisProposer | None = None
    if with_proposer:
        batch = RawProposalBatch(proposals=mock_proposals or [])
        llm = MockLLMClient(handler=lambda _req: batch.model_dump_json())
        proposer = HypothesisProposer(llm_client=llm, compiler=compiler)

    adapter = HarnessAgentAdapter(
        orchestrator=orchestrator,
        eval_request=DEFAULT_EVAL_REQUEST,
        experiment_registry=experiments,
        proposer=proposer,
        refinement_runner=refinement,
    )
    return adapter, experiments


# ── translate_to_request / translate_to_response ────────────────────────────


class TestTranslation:
    def test_translate_to_request_treats_text_as_dsl(self) -> None:
        adapter, _ = _build_stack(_ScriptedEvaluator({}))
        req = adapter.translate_to_request("  rank(close)  ")
        assert isinstance(req, ResearchCycleRequest)
        assert req.hypothesis_text == "rank(close)"
        assert req.goal == CycleGoal.EXPLORE

    def test_translate_to_response_for_unknown_id_returns_error(self) -> None:
        adapter, _ = _build_stack(_ScriptedEvaluator({}))
        resp = adapter.translate_to_response("nope")
        assert resp.outcome == CycleOutcome.ERROR
        assert "not found" in resp.failure_detail.lower()

    def test_translate_to_response_round_trips_known_experiment(self) -> None:
        evaluator = _ScriptedEvaluator(
            {"rank(close)": (0.10, 0.12, 0.02)},
        )
        adapter, experiments = _build_stack(evaluator)
        adapter.run_cycle(
            ResearchCycleRequest(hypothesis_text="rank(close)"),
        )
        [record] = experiments.list_all()
        resp = adapter.translate_to_response(record.id)
        assert resp.experiment_id == record.id
        assert resp.outcome == CycleOutcome.PROMOTED


# ── run_cycle ───────────────────────────────────────────────────────────────


class TestRunCycle:
    def test_promote_maps_to_promoted_outcome(self) -> None:
        evaluator = _ScriptedEvaluator(
            {"rank(close)": (0.10, 0.12, 0.02)},
        )
        adapter, _ = _build_stack(evaluator)
        resp = adapter.run_cycle(
            ResearchCycleRequest(hypothesis_text="rank(close)"),
        )
        assert resp.outcome == CycleOutcome.PROMOTED
        assert resp.ic == pytest.approx(0.10)

    def test_reject_maps_to_rejected_outcome(self) -> None:
        evaluator = _ScriptedEvaluator(
            {"rank(close)": (0.0, 0.0, 0.0)},
        )
        adapter, _ = _build_stack(evaluator)
        resp = adapter.run_cycle(
            ResearchCycleRequest(hypothesis_text="rank(close)"),
        )
        assert resp.outcome == CycleOutcome.REJECTED
        assert resp.failure_category is not None

    def test_single_cycle_returns_root_even_when_refinement_expands(self) -> None:
        # Borderline → REFINE. Children fall back to default (reject).
        borderline = (0.023, 0.035, 0.006)
        evaluator = _ScriptedEvaluator(
            {"rank(ts_mean(close, 20))": borderline},
        )
        adapter, experiments = _build_stack(evaluator, with_refinement=True)
        resp = adapter.run_cycle(
            ResearchCycleRequest(hypothesis_text="rank(ts_mean(close, 20))"),
        )
        # The *returned* response is the root record.
        assert resp.outcome == CycleOutcome.REFINED
        # But the registry contains the root + refined children.
        assert len(experiments.list_all()) > 1

    def test_single_cycle_without_refinement_runner_stops_at_root(self) -> None:
        borderline = (0.023, 0.035, 0.006)
        evaluator = _ScriptedEvaluator(
            {"rank(ts_mean(close, 20))": borderline},
        )
        adapter, experiments = _build_stack(evaluator, with_refinement=False)
        resp = adapter.run_cycle(
            ResearchCycleRequest(hypothesis_text="rank(ts_mean(close, 20))"),
        )
        assert resp.outcome == CycleOutcome.REFINED
        assert len(experiments.list_all()) == 1  # root only

    def test_unsupported_goal_raises(self) -> None:
        """AUDIT and SUMMARISE are reserved; the adapter must reject them."""
        adapter, _ = _build_stack(_ScriptedEvaluator({}))
        for goal in (CycleGoal.AUDIT, CycleGoal.SUMMARISE):
            with pytest.raises(ValueError, match="Unsupported CycleGoal"):
                adapter.run_cycle(
                    ResearchCycleRequest(
                        hypothesis_text="rank(close)", goal=goal,
                    ),
                )


# ── run_theme ───────────────────────────────────────────────────────────────


class TestRunTheme:
    def test_without_proposer_raises(self) -> None:
        adapter, _ = _build_stack(_ScriptedEvaluator({}))
        with pytest.raises(RuntimeError, match="HypothesisProposer"):
            adapter.run_theme(ThemeCycleRequest(theme="x"))

    def test_runs_each_accepted_proposal(self) -> None:
        evaluator = _ScriptedEvaluator({
            "rank(close)": (0.10, 0.12, 0.02),
            "zscore(close)": (0.0, 0.0, 0.0),
        })
        adapter, experiments = _build_stack(
            evaluator,
            with_proposer=True,
            mock_proposals=[
                RawProposal(expression="rank(close)"),
                RawProposal(expression="zscore(close)"),
            ],
        )
        resp = adapter.run_theme(
            ThemeCycleRequest(theme="levels", n_candidates=2),
        )
        assert resp.proposals_requested == 2
        assert resp.proposals_accepted == 2
        assert resp.proposals_dropped == 0
        assert len(resp.roots) == 2
        outcomes = {r.outcome for r in resp.roots}
        assert outcomes == {CycleOutcome.PROMOTED, CycleOutcome.REJECTED}
        # Registry ends up with at least the two root experiments.
        assert len(experiments.list_all()) >= 2
        assert all(
            record.eval_request is not None
            and record.eval_request.n_proposals_in_session == 2
            for record in experiments.list_all()
        )

    def test_drops_invalid_dsl_candidates(self) -> None:
        evaluator = _ScriptedEvaluator({"rank(close)": (0.10, 0.12, 0.02)})
        adapter, _ = _build_stack(
            evaluator,
            with_proposer=True,
            mock_proposals=[
                RawProposal(expression="rank(close)"),
                RawProposal(expression="this is not dsl @@@"),
            ],
        )
        resp = adapter.run_theme(
            ThemeCycleRequest(theme="levels", n_candidates=2),
        )
        assert resp.proposals_accepted == 1
        assert resp.proposals_dropped >= 1
        assert resp.dropped_reasons  # non-empty explanations

    def test_refine_verdict_expands_into_refinements_bucket(self) -> None:
        borderline = (0.023, 0.035, 0.006)
        evaluator = _ScriptedEvaluator(
            {"rank(ts_mean(close, 20))": borderline},
        )
        adapter, _ = _build_stack(
            evaluator,
            with_refinement=True,
            with_proposer=True,
            mock_proposals=[
                RawProposal(expression="rank(ts_mean(close, 20))"),
            ],
        )
        resp = adapter.run_theme(
            ThemeCycleRequest(theme="mean reversion", n_candidates=1),
        )
        assert len(resp.roots) == 1
        assert resp.roots[0].outcome == CycleOutcome.REFINED
        # Refinement runner produced at least one child under the root.
        assert len(resp.refinements) >= 1

    def test_adapter_does_not_override_judge_decision(self) -> None:
        """Adapter must not promote a rejected factor or vice versa."""
        evaluator = _ScriptedEvaluator({"rank(close)": (0.0, 0.0, 0.0)})
        adapter, experiments = _build_stack(
            evaluator,
            with_proposer=True,
            mock_proposals=[RawProposal(expression="rank(close)")],
        )
        resp = adapter.run_theme(
            ThemeCycleRequest(theme="x", n_candidates=1),
        )
        # Adapter shipped out the judge's verdict verbatim.
        assert resp.roots[0].outcome == CycleOutcome.REJECTED
        [record] = experiments.list_all()
        assert record.decision == ExperimentDecision.REJECT
