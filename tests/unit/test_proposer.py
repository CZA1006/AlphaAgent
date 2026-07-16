"""Tests for the hypothesis proposer.

All tests use ``MockLLMClient`` — no real LLM calls.  Validation of the
DSL runs through the real ``FactorDslCompiler`` so the test covers the
end-to-end safety contract: every returned candidate is compilable.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from alpha_harness.combination import CombinationMethod, CombinationRecipe
from alpha_harness.evaluators.promotion_judge import PromotionJudge
from alpha_harness.factors.compiler import FactorDslCompiler
from alpha_harness.llm import MockLLMClient
from alpha_harness.orchestrator.research_loop import ResearchOrchestrator
from alpha_harness.proposer import (
    CompositeAnchor,
    HypothesisProposer,
    ProposalCandidate,
    ProposalRequest,
    ProposalResult,
)
from alpha_harness.proposer.prompts import (
    build_system_prompt,
    build_user_prompt,
)
from alpha_harness.registries.experiment import ExperimentRegistry
from alpha_harness.registries.hypothesis import HypothesisRegistry
from alpha_harness.retrieval import RelatedExperiment
from alpha_harness.schemas.evaluation import (
    EvaluationProfile,
    EvaluationRequest,
)
from alpha_harness.schemas.experiment import ExperimentDecision
from alpha_harness.schemas.hypothesis import AssetClass
from alpha_harness.service import AlphaHarnessService
from tests.helpers.stubs import StubSignalQualityEvaluator

# ── Helpers ──────────────────────────────────────────────────────────────────


def _batch(*proposals: dict) -> str:
    """Render a ``RawProposalBatch`` JSON payload for the mock LLM."""
    return json.dumps({"proposals": list(proposals)})


def _anchor() -> CompositeAnchor:
    return CompositeAnchor(
        factor_id="composite_base",
        recipe=CombinationRecipe.build(
            method=CombinationMethod.RANK_AGGREGATE,
            components=["rank(close)", "rank(volume)"],
        ),
        ic=0.03,
        rank_ic=0.04,
    )


# ── Schema / result helpers ──────────────────────────────────────────────────


class TestResultToHypotheses:
    def test_bridges_candidates_into_hypotheses(self) -> None:
        result = ProposalResult(
            candidates=[
                ProposalCandidate(
                    expression="rank(close)",
                    rationale="cross-sectional level",
                    tags=["momentum"],
                ),
            ],
        )
        hypotheses = result.to_hypotheses(
            asset_class=AssetClass.US_EQUITY,
            extra_tags=("round3",),
        )
        assert len(hypotheses) == 1
        assert hypotheses[0].text == "rank(close)"
        assert hypotheses[0].rationale == "cross-sectional level"
        assert hypotheses[0].source == "llm_proposer"
        assert "momentum" in hypotheses[0].tags
        assert "round3" in hypotheses[0].tags


# ── Prompt assembly ──────────────────────────────────────────────────────────


class TestPrompts:
    def test_system_prompt_lists_allowed_functions_and_fields(self) -> None:
        prompt = build_system_prompt()
        # Every whitelisted function must appear so the model cannot claim
        # ignorance of what's permitted.
        for name in ("ts_mean", "rank", "zscore", "ts_delta", "event_decay"):
            assert name in prompt
        for field in ("close", "volume", "vwap"):
            assert field in prompt
        assert '"proposals"' in prompt  # JSON schema documented

    def test_system_prompt_can_be_scoped_to_pack_fields(self) -> None:
        prompt = build_system_prompt(
            extra_fields=frozenset({"custom_signal"}),
            extra_field_docs={"custom_signal": "Synthetic pack signal."},
        )
        assert "custom_signal  — Synthetic pack signal." in prompt
        assert "ofi" not in prompt

    def test_user_prompt_includes_theme_and_related(self) -> None:
        related = [
            RelatedExperiment(
                experiment_id="e1",
                factor_name="mom_rank_20",
                expression="rank(ts_mean(close, 20))",
                decision=ExperimentDecision.PROMOTE_CANDIDATE,
                tags=("momentum",),
                asset_class=AssetClass.US_EQUITY,
                created_at=datetime.now(UTC),
                score=0.9,
                ast_similarity=1.0,
                tag_overlap=0.5,
                recency=0.8,
                ic=0.07,
                rank_ic=0.08,
                sharpe=1.3,
                failure_category=None,
                notes="",
            ),
        ]
        request = ProposalRequest(
            theme="cross-sectional momentum",
            n_candidates=3,
            related=related,
            extra_guidance="prefer 20 to 60 bar windows",
        )
        prompt = build_user_prompt(request)
        assert "cross-sectional momentum" in prompt
        assert "us_equity" in prompt
        assert "mom_rank_20" in prompt
        assert "prefer 20 to 60 bar windows" in prompt

    def test_complement_prompt_requires_typed_anchor_target(self) -> None:
        anchor = _anchor()
        prompt = build_user_prompt(
            ProposalRequest(
                theme="add diversifying signal",
                composite_anchors=[anchor],
            )
        )
        assert "Mandatory composite-complement task" in prompt
        assert anchor.recipe.recipe_id in prompt
        assert "base_recipe_id" in prompt
        assert "reject" in prompt


# ── Validation: happy path + filtering ──────────────────────────────────────


class TestProposeValidation:
    def test_valid_candidates_pass_through(self) -> None:
        mock = MockLLMClient(
            responses=[
                _batch(
                    {"expression": "rank(ts_mean(close, 20))", "rationale": "mom"},
                    {"expression": "zscore(volume)", "rationale": "volume surprise"},
                )
            ]
        )
        proposer = HypothesisProposer(mock, max_rounds=1)
        result = proposer.propose(
            ProposalRequest(
                theme="momentum",
                n_candidates=2,
            )
        )

        assert len(result.candidates) == 2
        exprs = [c.expression for c in result.candidates]
        assert "rank(ts_mean(close, 20))" in exprs
        assert "zscore(volume)" in exprs
        assert result.dropped == []

    def test_invalid_expressions_are_dropped_with_reason(self) -> None:
        mock = MockLLMClient(
            responses=[
                _batch(
                    {"expression": "rank(ts_mean(close, 20))", "rationale": "ok"},
                    {"expression": "banned_fn(close)", "rationale": "bad — unknown fn"},
                    {"expression": "ts_mean(close, -5)", "rationale": "bad — neg window"},
                    {"expression": "close +", "rationale": "bad — syntax"},
                    {"expression": "", "rationale": "bad — empty"},
                )
            ]
        )
        proposer = HypothesisProposer(mock, max_rounds=1)
        result = proposer.propose(
            ProposalRequest(
                theme="momentum",
                n_candidates=5,
            )
        )

        assert len(result.candidates) == 1
        assert result.candidates[0].expression == "rank(ts_mean(close, 20))"
        assert len(result.dropped) == 4
        reasons = {d.expression: d.reason for d in result.dropped}
        assert "Unknown function" in reasons["banned_fn(close)"]
        assert "window" in reasons["ts_mean(close, -5)"].lower()
        assert reasons[""] == "Empty expression."

    def test_every_returned_candidate_compiles(self) -> None:
        """Acceptance test — no returned candidate may fail the compiler."""
        mock = MockLLMClient(
            responses=[
                _batch(
                    {"expression": "ts_mean(close, 10)", "rationale": "a"},
                    {"expression": "some_junk!!", "rationale": "b"},
                    {"expression": "rank(close - ts_mean(close, 5))", "rationale": "c"},
                )
            ]
        )
        proposer = HypothesisProposer(mock, max_rounds=1)
        result = proposer.propose(
            ProposalRequest(
                theme="reversion",
                n_candidates=3,
            )
        )

        compiler = FactorDslCompiler()
        from alpha_harness.schemas.hypothesis import Hypothesis

        for c in result.candidates:
            compiler.compile(Hypothesis(text=c.expression))  # must not raise

    def test_duplicate_expressions_dropped(self) -> None:
        mock = MockLLMClient(
            responses=[
                _batch(
                    {"expression": "rank(close)", "rationale": "a"},
                    {"expression": "rank(close)", "rationale": "b — dup"},
                )
            ]
        )
        result = HypothesisProposer(mock, max_rounds=1).propose(
            ProposalRequest(theme="x", n_candidates=5)
        )
        assert len(result.candidates) == 1
        assert len(result.dropped) == 1
        assert "Duplicate" in result.dropped[0].reason

    def test_respects_n_candidates_cap(self) -> None:
        mock = MockLLMClient(
            responses=[
                _batch(
                    {"expression": "rank(close)", "rationale": "1"},
                    {"expression": "zscore(close)", "rationale": "2"},
                    {"expression": "ts_mean(close, 5)", "rationale": "3"},
                )
            ]
        )
        result = HypothesisProposer(mock, max_rounds=1).propose(
            ProposalRequest(theme="x", n_candidates=2)
        )
        assert len(result.candidates) == 2

    def test_zero_candidates_rejected(self) -> None:
        mock = MockLLMClient(responses=[_batch()])
        with pytest.raises(ValueError, match="n_candidates"):
            HypothesisProposer(mock).propose(ProposalRequest(theme="x", n_candidates=0))

    def test_complement_mode_requires_known_novel_base_target(self) -> None:
        anchor = _anchor()
        mock = MockLLMClient(
            responses=[
                _batch(
                    {"expression": "rank(realized_vol)", "base_recipe_id": anchor.recipe.recipe_id},
                    {"expression": "rank(close)"},
                    {"expression": "rank(vwap)", "base_recipe_id": "unknown"},
                    {"expression": "rank(volume)", "base_recipe_id": anchor.recipe.recipe_id},
                )
            ]
        )
        result = HypothesisProposer(mock, max_rounds=1).propose(
            ProposalRequest(
                theme="complements",
                n_candidates=4,
                composite_anchors=[anchor],
            )
        )
        assert [candidate.expression for candidate in result.candidates] == ["rank(realized_vol)"]
        candidate = result.candidates[0]
        assert candidate.base_recipe_id == anchor.recipe.recipe_id
        assert "complement" in candidate.tags
        reasons = " ".join(item.reason for item in result.dropped)
        assert "requires a base_recipe_id" in reasons
        assert "Unknown base_recipe_id" in reasons
        assert "not structurally novel" in reasons


# ── Bounded repair round ─────────────────────────────────────────────────────


class TestRepairRound:
    def test_repair_round_fills_gap(self) -> None:
        """Round 1 under-delivers; round 2 repairs it."""
        mock = MockLLMClient(
            responses=[
                _batch(
                    {"expression": "rank(close)", "rationale": "ok"},
                    {"expression": "mystery_fn(close)", "rationale": "bad"},
                ),
                _batch(
                    {"expression": "ts_mean(volume, 10)", "rationale": "fixed"},
                    {"expression": "zscore(close)", "rationale": "also fresh"},
                ),
            ]
        )
        result = HypothesisProposer(mock, max_rounds=2).propose(
            ProposalRequest(theme="momentum", n_candidates=3)
        )
        assert result.attempts == 2
        assert len(result.candidates) == 3
        assert len(mock.calls) == 2

    def test_repair_skipped_when_round_one_satisfies(self) -> None:
        mock = MockLLMClient(
            responses=[
                _batch(
                    {"expression": "rank(close)", "rationale": "a"},
                    {"expression": "zscore(close)", "rationale": "b"},
                ),
            ]
        )
        result = HypothesisProposer(mock, max_rounds=2).propose(
            ProposalRequest(theme="x", n_candidates=2)
        )
        assert result.attempts == 1
        assert len(mock.calls) == 1

    def test_max_rounds_one_disables_repair(self) -> None:
        mock = MockLLMClient(
            responses=[
                _batch(
                    {"expression": "rank(close)", "rationale": "a"},
                    {"expression": "garbage_op(close)", "rationale": "bad"},
                ),
            ]
        )
        result = HypothesisProposer(mock, max_rounds=1).propose(
            ProposalRequest(theme="x", n_candidates=5)
        )
        # No second call issued even though count is short.
        assert len(mock.calls) == 1
        assert result.attempts == 1
        assert len(result.candidates) == 1

    def test_repair_does_not_reintroduce_duplicates(self) -> None:
        mock = MockLLMClient(
            responses=[
                _batch(
                    {"expression": "rank(close)", "rationale": "a"},
                    {"expression": "bogus_fn()", "rationale": "bad"},
                ),
                _batch(
                    {"expression": "rank(close)", "rationale": "dup on repair"},
                    {"expression": "ts_std(close, 10)", "rationale": "new"},
                ),
            ]
        )
        result = HypothesisProposer(mock, max_rounds=2).propose(
            ProposalRequest(theme="x", n_candidates=3)
        )
        expressions = {c.expression for c in result.candidates}
        assert expressions == {"rank(close)", "ts_std(close, 10)"}

    def test_schema_failure_surfaces_in_dropped(self) -> None:
        """LLM returns nothing parseable — result should be empty but clean."""
        mock = MockLLMClient(
            handler=lambda req: "not json",  # always bad
        )
        result = HypothesisProposer(mock, max_rounds=1).propose(
            ProposalRequest(theme="x", n_candidates=2)
        )
        assert result.candidates == []
        assert len(result.dropped) == 1
        assert "LLM schema failure" in result.dropped[0].reason


# ── Integration: proposer → research loop ───────────────────────────────────


class TestIntegrationWithResearchLoop:
    def test_full_theme_to_experiment_path(self) -> None:
        """Theme → proposer → hypotheses → orchestrator → experiment records."""
        mock = MockLLMClient(
            responses=[
                _batch(
                    {"expression": "rank(ts_mean(close, 10))", "rationale": "mom"},
                    {"expression": "ts_std(close, 20)", "rationale": "vol"},
                    {"expression": "not_a_real_fn(close)", "rationale": "bad"},
                )
            ]
        )
        proposer = HypothesisProposer(mock, max_rounds=1)
        result = proposer.propose(
            ProposalRequest(
                theme="cross-sectional momentum",
                n_candidates=3,
            )
        )

        # Only the two valid ones should have been converted.
        hypotheses = result.to_hypotheses(asset_class=AssetClass.US_EQUITY)
        assert len(hypotheses) == 2

        # Feed them into the real orchestrator + stub evaluator.
        orchestrator = ResearchOrchestrator(
            service=AlphaHarnessService(
                compiler=FactorDslCompiler(),
                evaluator=StubSignalQualityEvaluator(),
                judge=PromotionJudge(),
            ),
            experiment_registry=ExperimentRegistry(),
            hypothesis_registry=HypothesisRegistry(),
        )
        request = EvaluationRequest(
            factor_id="placeholder",
            universe_id="test",
            eval_start=datetime(2020, 1, 1).date(),
            eval_end=datetime(2023, 12, 31).date(),
            profile=EvaluationProfile(min_periods=5, min_assets=3),
        )
        records = orchestrator.run_batch(hypotheses, request)
        assert len(records) == 2
        for record in records:
            assert record.decision in list(ExperimentDecision)
