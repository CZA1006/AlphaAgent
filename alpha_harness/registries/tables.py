"""SQLAlchemy Core table definitions for all registries.

This is the single source of truth for registry table schemas.
bootstrap_db.py and registry accessors both import from here.

The ``data`` column on each table holds the full Pydantic model serialized
as JSON.  Indexed columns (``decision``, ``status``, ``name``, etc.) are
denormalized copies kept in sync by the SQL registry implementations so
that common queries can be answered without parsing JSON.
"""

from __future__ import annotations

from sqlalchemy import Column, DateTime, String, Table, Text, func

from alpha_harness.db.connection import metadata

experiments = Table(
    "experiments",
    metadata,
    Column("id", String(12), primary_key=True),
    Column("data", Text, nullable=False),  # JSON-serialized ExperimentRecord
    Column("decision", String(32), nullable=False),
    Column("created_at", DateTime, server_default=func.now()),
    Column("updated_at", DateTime, server_default=func.now(), onupdate=func.now()),
)

hypotheses = Table(
    "hypotheses",
    metadata,
    Column("id", String(12), primary_key=True),
    Column("data", Text, nullable=False),  # JSON-serialized Hypothesis
    Column("status", String(32), nullable=False),
    Column("created_at", DateTime, server_default=func.now()),
    Column("updated_at", DateTime, server_default=func.now(), onupdate=func.now()),
)

factors = Table(
    "factors",
    metadata,
    Column("id", String(12), primary_key=True),
    Column("data", Text, nullable=False),  # JSON-serialized FactorSpec
    Column("name", String(256), nullable=False),
    Column("universe_id", String(12), nullable=False, server_default=""),
    Column("created_at", DateTime, server_default=func.now()),
)

universes = Table(
    "universes",
    metadata,
    Column("id", String(12), primary_key=True),
    Column("data", Text, nullable=False),  # JSON-serialized UniverseSpec
    Column("name", String(256), nullable=False),
    Column("asset_class", String(32), nullable=False),
    Column("created_at", DateTime, server_default=func.now()),
)

skills = Table(
    "skills",
    metadata,
    Column("id", String(12), primary_key=True),
    Column("data", Text, nullable=False),  # JSON-serialized Skill
    Column("name", String(256), nullable=False),
    Column("created_at", DateTime, server_default=func.now()),
)

memories = Table(
    "memories",
    metadata,
    Column("id", String(12), primary_key=True),
    Column("data", Text, nullable=False),  # JSON-serialized MemoryEntry
    Column("category", String(64), nullable=False),
    Column("created_at", DateTime, server_default=func.now()),
)
