"""Integration tests for SQL-backed registries.

These tests require a running Postgres instance. They are guarded by the
``integration`` pytest marker and skipped by default.

How to run
----------
1. Start Postgres (Docker example)::

    docker run --rm -d --name alphaagent-pg \
        -e POSTGRES_USER=alphaagent \
        -e POSTGRES_PASSWORD=alphaagent_dev \
        -e POSTGRES_DB=alphaagent_test \
        -p 5432:5432 \
        postgres:16

2. Set environment variables (or use the defaults)::

    export POSTGRES_DB=alphaagent_test

3. Run integration tests::

    pytest tests/integration/ -m integration -v

The tests create tables at the start of each test and drop them afterward,
so they are safe to run repeatedly against the same database.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from alpha_harness.db.connection import metadata
from alpha_harness.registries.sql_experiment import SqlExperimentRegistry
from alpha_harness.registries.sql_hypothesis import SqlHypothesisRegistry
from alpha_harness.schemas.evaluation import EvaluationBundle
from alpha_harness.schemas.experiment import ExperimentDecision, ExperimentRecord
from alpha_harness.schemas.factor import FactorSpec
from alpha_harness.schemas.hypothesis import Hypothesis, HypothesisStatus


def _pg_available() -> bool:
    """Check whether Postgres is reachable."""
    try:
        url = _build_url()
        eng = create_engine(url)
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        eng.dispose()
        return True
    except Exception:
        return False


def _build_url() -> str:
    user = os.environ.get("POSTGRES_USER", "alphaagent")
    password = os.environ.get("POSTGRES_PASSWORD", "alphaagent_dev")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "alphaagent_test")
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{db}"


_skip_reason = "Postgres not available (set POSTGRES_* env vars and start the DB)"
_pg_ok = _pg_available()


@pytest.fixture()
def engine() -> Engine:  # type: ignore[misc]
    """Create tables before each test and drop them afterward."""
    eng = create_engine(_build_url())
    metadata.create_all(eng)
    yield eng  # type: ignore[misc]
    metadata.drop_all(eng)
    eng.dispose()


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_experiment(
    decision: ExperimentDecision = ExperimentDecision.ARCHIVE_ONLY,
    hypothesis_text: str = "test",
) -> ExperimentRecord:
    h = Hypothesis(text=hypothesis_text)
    f = FactorSpec(name="f", expression="close", hypothesis_id=h.id)
    ev = EvaluationBundle(
        ic=0.05 if decision != ExperimentDecision.REJECT else 0.01,
    )
    return ExperimentRecord(
        hypothesis=h,
        factor=f,
        evaluation=ev,
        decision=decision,
    )


# ── SqlExperimentRegistry ────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.skipif(not _pg_ok, reason=_skip_reason)
class TestSqlExperimentRegistry:
    def test_save_and_get(self, engine: Engine) -> None:
        reg = SqlExperimentRegistry(engine)
        exp = _make_experiment()
        saved_id = reg.save(exp)

        assert saved_id == exp.id
        retrieved = reg.get(exp.id)
        assert retrieved is not None
        assert retrieved.id == exp.id
        assert retrieved.decision == ExperimentDecision.ARCHIVE_ONLY
        assert retrieved.hypothesis.text == "test"

    def test_get_missing(self, engine: Engine) -> None:
        reg = SqlExperimentRegistry(engine)
        assert reg.get("nonexistent") is None

    def test_upsert(self, engine: Engine) -> None:
        """Saving the same id twice updates the record."""
        reg = SqlExperimentRegistry(engine)
        exp = _make_experiment(ExperimentDecision.ARCHIVE_ONLY)
        reg.save(exp)

        # Update decision
        updated = exp.model_copy(update={"decision": ExperimentDecision.REJECT})
        reg.save(updated)

        retrieved = reg.get(exp.id)
        assert retrieved is not None
        assert retrieved.decision == ExperimentDecision.REJECT
        assert len(reg.list_all()) == 1

    def test_list_all(self, engine: Engine) -> None:
        reg = SqlExperimentRegistry(engine)
        reg.save(_make_experiment(hypothesis_text="alpha"))
        reg.save(_make_experiment(hypothesis_text="beta"))
        reg.save(_make_experiment(hypothesis_text="gamma"))

        all_records = reg.list_all()
        assert len(all_records) == 3

    def test_list_by_decision(self, engine: Engine) -> None:
        reg = SqlExperimentRegistry(engine)
        reg.save(_make_experiment(ExperimentDecision.REJECT))
        reg.save(_make_experiment(ExperimentDecision.PROMOTE_CANDIDATE))
        reg.save(_make_experiment(ExperimentDecision.REJECT))

        assert len(reg.list_rejected()) == 2
        assert len(reg.list_promoted()) == 1

    def test_list_by_hypothesis(self, engine: Engine) -> None:
        reg = SqlExperimentRegistry(engine)
        e1 = _make_experiment(hypothesis_text="alpha")
        e2 = _make_experiment(hypothesis_text="beta")
        reg.save(e1)
        reg.save(e2)

        results = reg.list_by_hypothesis(e1.hypothesis.id)
        assert len(results) == 1
        assert results[0].hypothesis.text == "alpha"

    def test_list_recent(self, engine: Engine) -> None:
        reg = SqlExperimentRegistry(engine)
        for i in range(5):
            reg.save(_make_experiment(hypothesis_text=f"idea_{i}"))

        recent = reg.list_recent(limit=3)
        assert len(recent) == 3

    def test_search_by_decision(self, engine: Engine) -> None:
        reg = SqlExperimentRegistry(engine)
        reg.save(_make_experiment(ExperimentDecision.REJECT))
        reg.save(_make_experiment(ExperimentDecision.PROMOTE_CANDIDATE))

        results = reg.search(decision="reject")
        assert len(results) == 1
        assert results[0].decision == ExperimentDecision.REJECT

    def test_full_round_trip_fidelity(self, engine: Engine) -> None:
        """All fields survive the JSON round-trip."""
        reg = SqlExperimentRegistry(engine)
        exp = _make_experiment(ExperimentDecision.PROMOTE_CANDIDATE)
        reg.save(exp)

        retrieved = reg.get(exp.id)
        assert retrieved is not None
        assert retrieved.factor.expression == "close"
        assert retrieved.evaluation.ic == 0.05
        assert retrieved.hypothesis.id == exp.hypothesis.id


# ── SqlHypothesisRegistry ────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.skipif(not _pg_ok, reason=_skip_reason)
class TestSqlHypothesisRegistry:
    def test_save_and_get(self, engine: Engine) -> None:
        reg = SqlHypothesisRegistry(engine)
        h = Hypothesis(text="momentum reversal")
        saved_id = reg.save(h)

        assert saved_id == h.id
        retrieved = reg.get(h.id)
        assert retrieved is not None
        assert retrieved.text == "momentum reversal"
        assert retrieved.status == HypothesisStatus.DRAFT

    def test_get_missing(self, engine: Engine) -> None:
        reg = SqlHypothesisRegistry(engine)
        assert reg.get("nonexistent") is None

    def test_upsert_status_change(self, engine: Engine) -> None:
        """Saving with an updated status overwrites the previous row."""
        reg = SqlHypothesisRegistry(engine)
        h = Hypothesis(text="original idea")
        reg.save(h)

        h_testing = h.model_copy(update={"status": HypothesisStatus.TESTING})
        reg.save(h_testing)

        retrieved = reg.get(h.id)
        assert retrieved is not None
        assert retrieved.status == HypothesisStatus.TESTING
        assert len(reg.list_all()) == 1

    def test_list_all(self, engine: Engine) -> None:
        reg = SqlHypothesisRegistry(engine)
        reg.save(Hypothesis(text="first"))
        reg.save(Hypothesis(text="second"))
        reg.save(Hypothesis(text="third"))

        assert len(reg.list_all()) == 3

    def test_list_by_status(self, engine: Engine) -> None:
        reg = SqlHypothesisRegistry(engine)
        reg.save(Hypothesis(text="draft one"))
        reg.save(Hypothesis(text="testing", status=HypothesisStatus.TESTING))
        reg.save(Hypothesis(text="draft two"))

        assert len(reg.list_actionable()) == 2
        assert len(reg.list_by_status(HypothesisStatus.TESTING)) == 1

    def test_list_recent(self, engine: Engine) -> None:
        reg = SqlHypothesisRegistry(engine)
        for i in range(5):
            reg.save(Hypothesis(text=f"idea_{i}"))

        recent = reg.list_recent(limit=3)
        assert len(recent) == 3

    def test_search_by_status(self, engine: Engine) -> None:
        reg = SqlHypothesisRegistry(engine)
        reg.save(Hypothesis(text="draft", status=HypothesisStatus.DRAFT))
        reg.save(Hypothesis(text="testing", status=HypothesisStatus.TESTING))

        results = reg.search(status="draft")
        assert len(results) == 1
        assert results[0].text == "draft"

    def test_full_round_trip_fidelity(self, engine: Engine) -> None:
        """All fields survive the JSON round-trip."""
        reg = SqlHypothesisRegistry(engine)
        h = Hypothesis(
            text="complex hypothesis",
            rationale="backed by research",
            tags=["momentum", "us_equity"],
            status=HypothesisStatus.PROMISING,
        )
        reg.save(h)

        retrieved = reg.get(h.id)
        assert retrieved is not None
        assert retrieved.rationale == "backed by research"
        assert retrieved.tags == ["momentum", "us_equity"]
        assert retrieved.status == HypothesisStatus.PROMISING
