"""SQL-backed memory registry — Postgres persistence for ``MemoryEntry``.

Uses SQLAlchemy Core against the ``memories`` table defined in
``tables.py``.  The full :class:`MemoryEntry` is stored as JSON in the
``data`` column; ``category`` is denormalized for efficient filtering.

The class mirrors the in-memory :class:`MemoryRegistry` API so orchestrator
code that writes lineage entries works against either backend without
changes.
"""

from __future__ import annotations

from sqlalchemy import desc, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine

from alpha_harness.registries.tables import memories
from alpha_harness.schemas.memory import MemoryCategory, MemoryEntry


class SqlMemoryRegistry:
    """Postgres-backed registry for structured research memory."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # ── Core CRUD ────────────────────────────────────────────────────

    def save(self, entity: MemoryEntry) -> str:
        data_json = entity.model_dump_json()
        stmt = pg_insert(memories).values(
            id=entity.id,
            data=data_json,
            category=entity.category.value,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["id"],
            set_={"data": data_json, "category": entity.category.value},
        )
        with self._engine.begin() as conn:
            conn.execute(stmt)
        return entity.id

    def get(self, entity_id: str) -> MemoryEntry | None:
        stmt = select(memories.c.data).where(memories.c.id == entity_id)
        with self._engine.connect() as conn:
            row = conn.execute(stmt).fetchone()
        if row is None:
            return None
        return MemoryEntry.model_validate_json(row[0])

    def list_all(self) -> list[MemoryEntry]:
        stmt = select(memories.c.data).order_by(desc(memories.c.created_at))
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [MemoryEntry.model_validate_json(r[0]) for r in rows]

    def search(self, **filters: str) -> list[MemoryEntry]:
        """Indexed filter on ``category``; any other key falls back to Python."""
        if set(filters.keys()) == {"category"}:
            stmt = (
                select(memories.c.data)
                .where(memories.c.category == filters["category"])
                .order_by(desc(memories.c.created_at))
            )
            with self._engine.connect() as conn:
                rows = conn.execute(stmt).fetchall()
            return [MemoryEntry.model_validate_json(r[0]) for r in rows]

        all_records = self.list_all()
        return [
            m for m in all_records
            if all(
                str(getattr(m, field, None)) == value
                for field, value in filters.items()
            )
        ]

    # ── Domain-specific queries ──────────────────────────────────────

    def list_by_category(self, category: MemoryCategory) -> list[MemoryEntry]:
        stmt = (
            select(memories.c.data)
            .where(memories.c.category == category.value)
            .order_by(desc(memories.c.created_at))
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).fetchall()
        return [MemoryEntry.model_validate_json(r[0]) for r in rows]

    def list_by_experiment(self, experiment_id: str) -> list[MemoryEntry]:
        """Return memory entries whose ``source_experiment_ids`` contains ``id``.

        JSON-array membership queries vary across Postgres versions; to keep
        the registry portable we load all entries and filter in Python.
        Lineage memory is small (a few entries per experiment), so this is
        fine at current scale.
        """
        return [
            m for m in self.list_all()
            if experiment_id in m.source_experiment_ids
        ]

    def list_by_tag(self, tag: str) -> list[MemoryEntry]:
        return [m for m in self.list_all() if tag in m.tags]
