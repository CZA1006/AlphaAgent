"""Integration test: full research loop against the SQL backend.

Requires a running Postgres reachable via ``POSTGRES_*`` env vars.  Gated
by the ``integration`` marker and skipped automatically when the database
is unreachable.

What this covers (that the pure-registry tests do not):

    * ``build_registries(BackendConfig.sql())`` produces usable registries.
    * A ``ResearchOrchestrator`` wired with SQL registries persists both the
      experiment and the matching lineage memory entry.
    * ``RefinementRunner`` drives a REFINE verdict through the SQL path and
      every child hypothesis shows up in Postgres with the correct
      ``parent_id``.

Keeping the test narrow avoids paying the DB round-trip cost for behaviour
already covered by the unit suite.
"""

from __future__ import annotations

import os
from datetime import date

import pytest
from sqlalchemy import create_engine, text

from alpha_harness.config import BackendConfig, PostgresSettings
from alpha_harness.db.connection import metadata
from alpha_harness.evaluators.promotion_judge import PromotionJudge
from alpha_harness.factors.compiler import FactorDslCompiler
from alpha_harness.orchestrator.refinement import (
    RefinementConfig,
    RefinementRunner,
)
from alpha_harness.orchestrator.research_loop import ResearchOrchestrator
from alpha_harness.registries.factory import build_registries
from alpha_harness.schemas.evaluation import EvaluationBundle, EvaluationRequest
from alpha_harness.schemas.experiment import ExperimentDecision
from alpha_harness.schemas.factor import FactorSpec
from alpha_harness.schemas.hypothesis import Hypothesis
from alpha_harness.schemas.memory import MemoryCategory
from alpha_harness.service import AlphaHarnessService


def _pg_settings() -> PostgresSettings:
    return PostgresSettings(
        user=os.environ.get("POSTGRES_USER", "alphaagent"),
        password=os.environ.get("POSTGRES_PASSWORD", "alphaagent_dev"),
        host=os.environ.get("POSTGRES_HOST", "localhost"),
        port=os.environ.get("POSTGRES_PORT", "5432"),
        database=os.environ.get("POSTGRES_DB", "alphaagent_test"),
    )


def _pg_available() -> bool:
    try:
        eng = create_engine(_pg_settings().url)
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        eng.dispose()
        return True
    except Exception:
        return False


_pg_ok = _pg_available()
_skip_reason = "Postgres not available (set POSTGRES_* env vars and start the DB)"


DEFAULT_EVAL_REQUEST = EvaluationRequest(
    factor_id="placeholder",
    universe_id="sql_integration",
    eval_start=date(2020, 1, 1),
    eval_end=date(2023, 12, 31),
)


@pytest.fixture()
def sql_config():  # type: ignore[no-untyped-def]
    """Yield a SQL BackendConfig, dropping tables afterward."""
    cfg = BackendConfig.sql(_pg_settings())
    yield cfg
    # Cleanup — drop tables via a throwaway engine.
    eng = create_engine(cfg.postgres.url)
    metadata.drop_all(eng)
    eng.dispose()


# ── Evaluators ──────────────────────────────────────────────────────────────


class _PromoteEvaluator:
    def evaluate(self, factor: FactorSpec, request: EvaluationRequest) -> EvaluationBundle:
        return EvaluationBundle(ic=0.08, rank_ic=0.08, quantile_spread=0.02)


class _BorderlineEvaluator:
    """Root expression borderlines → REFINE; mutations default to REJECT."""

    def __init__(self, refine_expr: str) -> None:
        self._refine_expr = refine_expr

    def evaluate(self, factor: FactorSpec, request: EvaluationRequest) -> EvaluationBundle:
        if factor.expression == self._refine_expr:
            return EvaluationBundle(ic=0.023, rank_ic=0.035, quantile_spread=0.006)
        return EvaluationBundle(ic=0.0, rank_ic=0.0, quantile_spread=0.0)


# ── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.skipif(not _pg_ok, reason=_skip_reason)
def test_factory_builds_sql_backend(sql_config: BackendConfig) -> None:
    bundle = build_registries(sql_config)
    assert bundle.engine is not None
    # Round-trip a hypothesis to prove the registries actually speak SQL.
    h = Hypothesis(text="rank(close)")
    bundle.hypotheses.save(h)
    fetched = bundle.hypotheses.get(h.id)
    assert fetched is not None
    assert fetched.text == "rank(close)"


@pytest.mark.integration
@pytest.mark.skipif(not _pg_ok, reason=_skip_reason)
def test_orchestrator_persists_to_sql(sql_config: BackendConfig) -> None:
    bundle = build_registries(sql_config)
    judge = PromotionJudge()
    service = AlphaHarnessService(
        compiler=FactorDslCompiler(),
        evaluator=_PromoteEvaluator(),
        judge=judge,
    )
    orch = ResearchOrchestrator(
        service=service,
        experiment_registry=bundle.experiments,
        hypothesis_registry=bundle.hypotheses,
        memory_registry=bundle.memories,
    )

    h = Hypothesis(text="rank(close)")
    record = orch.run_cycle(h, DEFAULT_EVAL_REQUEST)

    # Experiment round-trips through the SQL registry.
    stored = bundle.experiments.get(record.id)
    assert stored is not None
    assert stored.decision == ExperimentDecision.PROMOTE_CANDIDATE

    # Lineage memory entry lands in the memories table.
    lineage = bundle.memories.list_by_category(
        MemoryCategory.EXPERIMENT_LINEAGE,
    )
    assert len(lineage) == 1
    assert record.id in lineage[0].source_experiment_ids


@pytest.mark.integration
@pytest.mark.skipif(not _pg_ok, reason=_skip_reason)
def test_refinement_lineage_persists_to_sql(sql_config: BackendConfig) -> None:
    refine_expr = "rank(ts_mean(close, 20))"
    bundle = build_registries(sql_config)
    judge = PromotionJudge()
    service = AlphaHarnessService(
        compiler=FactorDslCompiler(),
        evaluator=_BorderlineEvaluator(refine_expr),
        judge=judge,
    )
    orch = ResearchOrchestrator(
        service=service,
        experiment_registry=bundle.experiments,
        hypothesis_registry=bundle.hypotheses,
        memory_registry=bundle.memories,
    )
    runner = RefinementRunner(
        orch,
        config=RefinementConfig(
            max_depth=1, max_variants_per_step=2, max_total_children=2,
        ),
    )

    root = Hypothesis(text=refine_expr)
    result = runner.run(root, DEFAULT_EVAL_REQUEST)

    assert result.root.decision == ExperimentDecision.REFINE
    assert len(result.children) >= 1

    # Every child record made it to SQL with correct lineage.
    for child in result.children:
        fetched = bundle.experiments.get(child.id)
        assert fetched is not None
        assert fetched.hypothesis.parent_id == root.id

    # Lineage entries cover root + every child.
    lineage = bundle.memories.list_by_category(
        MemoryCategory.EXPERIMENT_LINEAGE,
    )
    assert len(lineage) == 1 + len(result.children)

    # Specifically: each child's lineage entry is retrievable by experiment id.
    for child in result.children:
        hits = bundle.memories.list_by_experiment(child.id)
        assert len(hits) == 1
        assert f"parent={root.id}" in hits[0].content
