"""Tests for related-experiment retrieval.

Covers:
    * AST similarity as primary signal
    * Tag overlap secondary signal
    * Recency decay
    * Filtering by decision and asset class
    * Top-N bounding and deterministic sort
    * Weight overrides
    * Works against ``ExperimentRegistry`` (the other integration path —
      SQL-backed — is covered structurally via the ``ExperimentSource``
      Protocol; both registries ship a ``list_all()`` that returns the
      same model type).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from alpha_harness.registries.experiment import ExperimentRegistry
from alpha_harness.retrieval import (
    RelatedExperimentRetriever,
    RelatedQuery,
    ScoreWeights,
)
from alpha_harness.schemas.evaluation import EvaluationBundle
from alpha_harness.schemas.experiment import (
    ExperimentDecision,
    ExperimentRecord,
    FailureCategory,
    FailureRecord,
)
from alpha_harness.schemas.factor import FactorSpec
from alpha_harness.schemas.hypothesis import AssetClass, Hypothesis

# ── Builders ─────────────────────────────────────────────────────────────────


def _make_record(
    *,
    expression: str,
    factor_name: str,
    tags: list[str] | None = None,
    hypothesis_tags: list[str] | None = None,
    decision: ExperimentDecision = ExperimentDecision.ARCHIVE_ONLY,
    asset_class: AssetClass = AssetClass.US_EQUITY,
    created_at: datetime | None = None,
    ic: float | None = 0.05,
    rank_ic: float | None = 0.06,
    sharpe: float | None = 1.1,
    failure: FailureRecord | None = None,
    notes: str = "",
) -> ExperimentRecord:
    hypothesis = Hypothesis(
        text=expression,
        tags=hypothesis_tags or [],
        asset_class=asset_class,
    )
    factor = FactorSpec(
        name=factor_name,
        expression=expression,
        hypothesis_id=hypothesis.id,
    )
    return ExperimentRecord(
        hypothesis=hypothesis,
        factor=factor,
        evaluation=EvaluationBundle(
            ic=ic, rank_ic=rank_ic, sharpe=sharpe,
            n_periods=100, n_assets=50,
        ),
        decision=decision,
        failure=failure,
        tags=tags or [],
        notes=notes,
        created_at=created_at or datetime.now(UTC),
    )


def _seed_registry() -> tuple[ExperimentRegistry, datetime]:
    """Registry with a deliberately diverse set of records."""
    now = datetime(2026, 4, 1, tzinfo=UTC)
    registry = ExperimentRegistry()

    # Exact-canonical duplicate of the query
    registry.save(_make_record(
        expression="rank(ts_mean(close, 20))",
        factor_name="mom_rank_20",
        tags=["momentum"],
        hypothesis_tags=["equities"],
        decision=ExperimentDecision.PROMOTE_CANDIDATE,
        created_at=now - timedelta(days=5),
    ))
    # Near-duplicate (different window)
    registry.save(_make_record(
        expression="rank(ts_mean(close, 30))",
        factor_name="mom_rank_30",
        tags=["momentum"],
        decision=ExperimentDecision.REFINE,
        created_at=now - timedelta(days=10),
    ))
    # Tag-overlap only, unrelated expression
    registry.save(_make_record(
        expression="ts_std(volume, 10)",
        factor_name="vol_std_10",
        tags=["momentum", "volume"],
        decision=ExperimentDecision.REJECT,
        failure=FailureRecord(
            category=FailureCategory.WEAK_SIGNAL, detail="ic too low"
        ),
        created_at=now - timedelta(days=2),
    ))
    # Completely unrelated, also different asset class
    registry.save(_make_record(
        expression="zscore(close)",
        factor_name="crypto_z",
        tags=["crypto_only"],
        asset_class=AssetClass.CRYPTO,
        decision=ExperimentDecision.PROMOTE_CANDIDATE,
        created_at=now - timedelta(days=1),
    ))
    # Very old but structurally identical
    registry.save(_make_record(
        expression="rank(ts_mean(close, 20))",
        factor_name="mom_rank_old",
        decision=ExperimentDecision.PROMOTE_CANDIDATE,
        created_at=now - timedelta(days=365),
    ))
    return registry, now


# ── Ranking ──────────────────────────────────────────────────────────────────


class TestRanking:
    def test_exact_ast_match_ranks_first(self) -> None:
        registry, now = _seed_registry()
        retriever = RelatedExperimentRetriever(registry)
        query_factor = FactorSpec(
            name="candidate", expression="rank(ts_mean(close, 20))"
        )
        query = RelatedQuery(factor=query_factor, top_n=3)

        results = retriever.search(query, now=now)

        assert len(results) == 3
        # Top result must be a canonical match (score >= ast weight).
        assert results[0].ast_similarity == 1.0
        # Sorted best-first.
        scores = [r.score for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_top_n_bounds_results(self) -> None:
        registry, now = _seed_registry()
        retriever = RelatedExperimentRetriever(registry)
        query = RelatedQuery(
            factor=FactorSpec(name="q", expression="rank(ts_mean(close, 20))"),
            top_n=2,
        )
        assert len(retriever.search(query, now=now)) == 2

    def test_near_duplicate_scores_high(self) -> None:
        """ts_mean(close, 20) vs ts_mean(close, 30) — same shape, different window."""
        registry, now = _seed_registry()
        retriever = RelatedExperimentRetriever(registry)
        query_factor = FactorSpec(
            name="q", expression="rank(ts_mean(close, 20))"
        )
        query = RelatedQuery(factor=query_factor, top_n=5)

        results = retriever.search(query, now=now)
        near = next(r for r in results if r.factor_name == "mom_rank_30")
        assert near.ast_similarity >= 0.85

    def test_recency_breaks_ast_ties(self) -> None:
        """Two canonically-identical records — newer should rank higher."""
        registry, now = _seed_registry()
        retriever = RelatedExperimentRetriever(registry)
        query_factor = FactorSpec(
            name="q", expression="rank(ts_mean(close, 20))"
        )
        query = RelatedQuery(
            factor=query_factor,
            top_n=5,
            recency_half_life_days=30.0,
        )

        results = retriever.search(query, now=now)
        fresh_idx = next(
            i for i, r in enumerate(results) if r.factor_name == "mom_rank_20"
        )
        stale_idx = next(
            i for i, r in enumerate(results) if r.factor_name == "mom_rank_old"
        )
        assert fresh_idx < stale_idx

    def test_tag_only_query_returns_tagged_records(self) -> None:
        """With no factor, tag overlap drives ranking."""
        registry, now = _seed_registry()
        retriever = RelatedExperimentRetriever(registry)
        query = RelatedQuery(tags=("momentum",), top_n=5)

        results = retriever.search(query, now=now)
        tagged = {r.factor_name for r in results if r.tag_overlap > 0}
        # All three momentum-tagged records should register overlap.
        assert {"mom_rank_20", "mom_rank_30", "vol_std_10"} <= tagged

    def test_score_is_weighted_sum_of_subscores(self) -> None:
        """Verify the score equals the declared linear combination."""
        registry, now = _seed_registry()
        retriever = RelatedExperimentRetriever(registry)
        weights = ScoreWeights(
            ast_similarity=0.5, tag_overlap=0.3, recency=0.2
        )
        query = RelatedQuery(
            factor=FactorSpec(name="q", expression="rank(ts_mean(close, 20))"),
            tags=("momentum",),
            weights=weights,
            top_n=10,
            recency_half_life_days=30.0,
        )

        for result in retriever.search(query, now=now):
            expected = (
                weights.ast_similarity * result.ast_similarity
                + weights.tag_overlap * result.tag_overlap
                + weights.recency * result.recency
            )
            assert result.score == round(expected, 6)

    def test_empty_registry_returns_empty_list(self) -> None:
        retriever = RelatedExperimentRetriever(ExperimentRegistry())
        query = RelatedQuery(
            factor=FactorSpec(name="q", expression="rank(close)"),
        )
        assert retriever.search(query) == []

    def test_min_score_filters_weak_matches(self) -> None:
        registry, now = _seed_registry()
        retriever = RelatedExperimentRetriever(registry)
        query = RelatedQuery(
            factor=FactorSpec(name="q", expression="rank(ts_mean(close, 20))"),
            min_score=0.5,
            top_n=10,
        )
        results = retriever.search(query, now=now)
        assert all(r.score >= 0.5 for r in results)


# ── Filtering ────────────────────────────────────────────────────────────────


class TestFiltering:
    def test_filter_by_decision(self) -> None:
        registry, now = _seed_registry()
        retriever = RelatedExperimentRetriever(registry)
        query = RelatedQuery(
            factor=FactorSpec(name="q", expression="rank(ts_mean(close, 20))"),
            decisions=(ExperimentDecision.REJECT,),
            top_n=10,
        )
        results = retriever.search(query, now=now)
        assert len(results) == 1
        assert results[0].decision == ExperimentDecision.REJECT
        assert results[0].failure_category == FailureCategory.WEAK_SIGNAL.value

    def test_filter_by_asset_class(self) -> None:
        registry, now = _seed_registry()
        retriever = RelatedExperimentRetriever(registry)
        query = RelatedQuery(
            tags=("crypto_only",),
            asset_class=AssetClass.CRYPTO,
            top_n=10,
        )
        results = retriever.search(query, now=now)
        assert len(results) == 1
        assert results[0].factor_name == "crypto_z"
        assert results[0].asset_class == AssetClass.CRYPTO

    def test_asset_class_filter_excludes_others(self) -> None:
        """Equity-only search must exclude the crypto experiment."""
        registry, now = _seed_registry()
        retriever = RelatedExperimentRetriever(registry)
        query = RelatedQuery(
            factor=FactorSpec(name="q", expression="rank(ts_mean(close, 20))"),
            asset_class=AssetClass.US_EQUITY,
            top_n=10,
        )
        results = retriever.search(query, now=now)
        assert all(r.asset_class == AssetClass.US_EQUITY for r in results)
        assert "crypto_z" not in {r.factor_name for r in results}

    def test_multiple_decisions(self) -> None:
        registry, now = _seed_registry()
        retriever = RelatedExperimentRetriever(registry)
        query = RelatedQuery(
            factor=FactorSpec(name="q", expression="rank(ts_mean(close, 20))"),
            decisions=(
                ExperimentDecision.PROMOTE_CANDIDATE,
                ExperimentDecision.REFINE,
            ),
            top_n=10,
        )
        results = retriever.search(query, now=now)
        decisions = {r.decision for r in results}
        assert ExperimentDecision.REJECT not in decisions


# ── Summary shape ────────────────────────────────────────────────────────────


class TestSummaryShape:
    def test_summary_carries_provenance_and_subscores(self) -> None:
        registry, now = _seed_registry()
        retriever = RelatedExperimentRetriever(registry)
        query = RelatedQuery(
            factor=FactorSpec(name="q", expression="rank(ts_mean(close, 20))"),
            top_n=1,
        )
        [summary] = retriever.search(query, now=now)

        assert summary.expression == "rank(ts_mean(close, 20))"
        assert summary.factor_name in {"mom_rank_20", "mom_rank_old"}
        assert 0.0 <= summary.ast_similarity <= 1.0
        assert 0.0 <= summary.tag_overlap <= 1.0
        assert 0.0 <= summary.recency <= 1.0
        assert summary.ic is not None
        assert summary.rank_ic is not None
        assert summary.sharpe is not None

    def test_failure_info_present_for_rejected(self) -> None:
        registry, now = _seed_registry()
        retriever = RelatedExperimentRetriever(registry)
        query = RelatedQuery(
            tags=("volume",),
            decisions=(ExperimentDecision.REJECT,),
            top_n=5,
        )
        [summary] = retriever.search(query, now=now)
        assert summary.failure_category == FailureCategory.WEAK_SIGNAL.value
        assert summary.notes == ""


# ── Integration path ─────────────────────────────────────────────────────────


class TestIntegrationWithOrchestrator:
    def test_end_to_end_against_orchestrator_run(self) -> None:
        """Seed a registry via the real orchestrator, then retrieve."""
        from alpha_harness.evaluators.promotion_judge import PromotionJudge
        from alpha_harness.factors.compiler import FactorDslCompiler
        from alpha_harness.orchestrator.research_loop import ResearchOrchestrator
        from alpha_harness.registries.hypothesis import HypothesisRegistry
        from alpha_harness.schemas.evaluation import (
            EvaluationProfile,
            EvaluationRequest,
        )
        from alpha_harness.service import AlphaHarnessService
        from tests.helpers.stubs import StubSignalQualityEvaluator

        exp_registry = ExperimentRegistry()
        orchestrator = ResearchOrchestrator(
            service=AlphaHarnessService(
                compiler=FactorDslCompiler(),
                evaluator=StubSignalQualityEvaluator(),
                judge=PromotionJudge(),
            ),
            experiment_registry=exp_registry,
            hypothesis_registry=HypothesisRegistry(),
        )
        request = EvaluationRequest(
            factor_id="placeholder",
            universe_id="test",
            eval_start=datetime(2020, 1, 1).date(),
            eval_end=datetime(2023, 12, 31).date(),
            profile=EvaluationProfile(min_periods=5, min_assets=3),
        )
        for text in (
            "rank(ts_mean(close, 20))",
            "ts_std(volume, 10)",
            "zscore(close)",
        ):
            orchestrator.run_cycle(Hypothesis(text=text), request)

        retriever = RelatedExperimentRetriever(exp_registry)
        results = retriever.search(RelatedQuery(
            factor=FactorSpec(name="q", expression="rank(ts_mean(close, 20))"),
            top_n=2,
        ))
        assert len(results) == 2
        assert results[0].ast_similarity == 1.0
