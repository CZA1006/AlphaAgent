"""SQL-backed experiment registry — Postgres persistence for ExperimentRecords.

Uses SQLAlchemy Core (not ORM) against the ``experiments`` table defined in
``tables.py``.  The full ExperimentRecord is stored as JSON in the ``data``
column; the ``decision`` column is denormalized for efficient query filtering.

The class mirrors the in-memory ``ExperimentRegistry`` API so callers can
swap implementations without code changes.
"""

from __future__ import annotations

from sqlalchemy import desc, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from alpha_harness.registries.tables import experiments
from alpha_harness.schemas.experiment import ExperimentDecision, ExperimentRecord


class SqlExperimentRegistry:
    """Postgres-backed registry for experiment records.

    Parameters
    ----------
    engine:
        SQLAlchemy engine connected to the target Postgres database.
        Tables must already exist (use ``metadata.create_all(engine)``).
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # ── Core CRUD ────────────────────────────────────────────────────

    def save(self, entity: ExperimentRecord) -> str:
        """Upsert an experiment record.

        Inserts a new row or updates the existing row if an experiment with
        the same ``id`` already exists.  Returns the entity id.
        """
        data_json = entity.model_dump_json()
        stmt = pg_insert(experiments).values(
            id=entity.id,
            data=data_json,
            decision=entity.decision.value,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["id"],
            set_={"data": data_json, "decision": entity.decision.value},
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)
        return entity.id

    def get(self, entity_id: str) -> ExperimentRecord | None:
        """Retrieve a single experiment by id."""
        stmt = select(experiments.c.data).where(experiments.c.id == entity_id)
        with self._engine.connect() as conn:
            row = conn.execute(stmt).fetchone()
        if row is None:
            return None
        return ExperimentRecord.model_validate_json(row[0])

    def list_all(self) -> list[ExperimentRecord]:
        """Return all experiment records ordered by creation time (newest first)."""
        stmt = select(experiments.c.data).order_by(desc(experiments.c.created_at))
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [ExperimentRecord.model_validate_json(r[0]) for r in rows]

    def search(self, **filters: str) -> list[ExperimentRecord]:
        """Search experiments by field values.

        Supported indexed filters (fast, uses SQL WHERE):
            - ``decision``: filter by decision value

        Any other filter falls back to loading all records and filtering
        in Python (slow but correct).
        """
        if set(filters.keys()) == {"decision"}:
            stmt = (
                select(experiments.c.data)
                .where(experiments.c.decision == filters["decision"])
                .order_by(desc(experiments.c.created_at))
            )
            with self._engine.connect() as conn:
                rows = conn.execute(stmt).fetchall()
            return [ExperimentRecord.model_validate_json(r[0]) for r in rows]

        # Fallback: load all and filter in Python
        all_records = self.list_all()
        return [
            e for e in all_records
            if all(
                str(getattr(e, field, None)) == value
                for field, value in filters.items()
            )
        ]

    # ── Domain-specific queries ──────────────────────────────────────

    def list_by_decision(
        self, decision: ExperimentDecision
    ) -> list[ExperimentRecord]:
        """Return all experiments with a given decision."""
        stmt = (
            select(experiments.c.data)
            .where(experiments.c.decision == decision.value)
            .order_by(desc(experiments.c.created_at))
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [ExperimentRecord.model_validate_json(r[0]) for r in rows]

    def list_by_hypothesis(
        self, hypothesis_id: str
    ) -> list[ExperimentRecord]:
        """Return all experiments derived from a specific hypothesis.

        Requires deserializing all records because hypothesis_id is inside
        the JSON blob.  For production-scale usage, add a denormalized
        ``hypothesis_id`` column.
        """
        return [
            e for e in self.list_all() if e.hypothesis.id == hypothesis_id
        ]

    def list_promoted(self) -> list[ExperimentRecord]:
        """Return all experiments that were promoted."""
        return self.list_by_decision(ExperimentDecision.PROMOTE_CANDIDATE)

    def list_rejected(self) -> list[ExperimentRecord]:
        """Return all experiments that were rejected."""
        return self.list_by_decision(ExperimentDecision.REJECT)

    def list_recent(self, limit: int = 20) -> list[ExperimentRecord]:
        """Return the most recent experiments."""
        stmt = (
            select(experiments.c.data)
            .order_by(desc(experiments.c.created_at))
            .limit(limit)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [ExperimentRecord.model_validate_json(r[0]) for r in rows]
