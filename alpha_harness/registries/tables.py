"""SQLAlchemy Core table definitions for all registries.

This is the single source of truth for registry table schemas.
bootstrap_db.py and registry accessors both import from here.

TODO: Postgres-backed registry implementations will use these tables
via insert/select queries. For Milestone 1, registries use InMemoryRegistry.
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
)

hypotheses = Table(
    "hypotheses",
    metadata,
    Column("id", String(12), primary_key=True),
    Column("data", Text, nullable=False),  # JSON-serialized Hypothesis
    Column("status", String(32), nullable=False),
    Column("created_at", DateTime, server_default=func.now()),
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
