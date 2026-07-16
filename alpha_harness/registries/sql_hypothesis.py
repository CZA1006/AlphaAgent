"""SQL-backed hypothesis registry — Postgres persistence for Hypotheses.

Uses SQLAlchemy Core against the ``hypotheses`` table defined in ``tables.py``.
The full Hypothesis is stored as JSON in the ``data`` column; the ``status``
column is denormalized for efficient query filtering.
"""

from __future__ import annotations

from sqlalchemy import desc, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from alpha_harness.registries.tables import hypotheses
from alpha_harness.schemas.hypothesis import Hypothesis, HypothesisStatus


class SqlHypothesisRegistry:
    """Postgres-backed registry for research hypotheses.

    Parameters
    ----------
    engine:
        SQLAlchemy engine connected to the target Postgres database.
        Tables must already exist (use ``metadata.create_all(engine)``).
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # ── Core CRUD ────────────────────────────────────────────────────

    def save(self, entity: Hypothesis) -> str:
        """Upsert a hypothesis.

        Inserts a new row or updates the existing row if a hypothesis with
        the same ``id`` already exists.  Status transitions (DRAFT -> TESTING
        -> REJECTED/PROMISING) are tracked via the denormalized ``status``
        column.
        """
        data_json = entity.model_dump_json()
        stmt = pg_insert(hypotheses).values(
            id=entity.id,
            data=data_json,
            status=entity.status.value,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["id"],
            set_={"data": data_json, "status": entity.status.value},
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)
        return entity.id

    def get(self, entity_id: str) -> Hypothesis | None:
        """Retrieve a single hypothesis by id."""
        stmt = select(hypotheses.c.data).where(hypotheses.c.id == entity_id)
        with self._engine.connect() as conn:
            row = conn.execute(stmt).fetchone()
        if row is None:
            return None
        return Hypothesis.model_validate_json(row[0])

    def list_all(self) -> list[Hypothesis]:
        """Return all hypotheses ordered by creation time (newest first)."""
        stmt = select(hypotheses.c.data).order_by(desc(hypotheses.c.created_at))
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [Hypothesis.model_validate_json(r[0]) for r in rows]

    def search(self, **filters: str) -> list[Hypothesis]:
        """Search hypotheses by field values.

        Supported indexed filters (fast):
            - ``status``: filter by status value

        Other filters fall back to Python-side filtering.
        """
        if set(filters.keys()) == {"status"}:
            stmt = (
                select(hypotheses.c.data)
                .where(hypotheses.c.status == filters["status"])
                .order_by(desc(hypotheses.c.created_at))
            )
            with self._engine.connect() as conn:
                rows = conn.execute(stmt).fetchall()
            return [Hypothesis.model_validate_json(r[0]) for r in rows]

        all_records = self.list_all()
        return [
            h
            for h in all_records
            if all(str(getattr(h, field, None)) == value for field, value in filters.items())
        ]

    # ── Domain-specific queries ──────────────────────────────────────

    def list_by_status(self, status: HypothesisStatus) -> list[Hypothesis]:
        """Return all hypotheses with a given status."""
        stmt = (
            select(hypotheses.c.data)
            .where(hypotheses.c.status == status.value)
            .order_by(desc(hypotheses.c.created_at))
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [Hypothesis.model_validate_json(r[0]) for r in rows]

    def list_actionable(self) -> list[Hypothesis]:
        """Return hypotheses that are ready for testing (DRAFT status)."""
        return self.list_by_status(HypothesisStatus.DRAFT)

    def list_recent(self, limit: int = 20) -> list[Hypothesis]:
        """Return the most recent hypotheses."""
        stmt = select(hypotheses.c.data).order_by(desc(hypotheses.c.created_at)).limit(limit)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [Hypothesis.model_validate_json(r[0]) for r in rows]
