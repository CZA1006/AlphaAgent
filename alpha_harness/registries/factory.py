"""Registry factory — build a registry bundle for a given backend.

One call, one bundle, no backend-specific branching in the caller.  The
research loop, CLI scripts, and Hermes-boundary adapters all go through
this factory so that swapping backends is a single config change.

Example
-------
::

    from alpha_harness.config import BackendConfig
    from alpha_harness.registries.factory import build_registries

    # In-memory (default — no DB needed):
    bundle = build_registries(BackendConfig.memory())

    # SQL-backed (requires a reachable Postgres):
    bundle = build_registries(BackendConfig.sql())
    orchestrator = ResearchOrchestrator(
        service=service,
        experiment_registry=bundle.experiments,
        hypothesis_registry=bundle.hypotheses,
        memory_registry=bundle.memories,
    )

The returned objects are typed against the registry protocols so callers
see the same method surface regardless of backend.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.engine import Engine

from alpha_harness.config import BackendConfig
from alpha_harness.registries.experiment import ExperimentRegistry
from alpha_harness.registries.hypothesis import HypothesisRegistry
from alpha_harness.registries.memory import MemoryRegistry
from alpha_harness.registries.protocols import (
    ExperimentRegistryProtocol,
    HypothesisRegistryProtocol,
    MemoryRegistryProtocol,
)


@dataclass(frozen=True)
class RegistryBundle:
    """Three registry handles plus the optional SQL engine.

    ``engine`` is ``None`` in memory mode and a live SQLAlchemy ``Engine``
    in SQL mode — callers can use it for ad-hoc queries or hand it to
    additional registries (e.g. :class:`SqlFactorRegistry`) not needed by
    the core research loop.
    """

    experiments: ExperimentRegistryProtocol
    hypotheses: HypothesisRegistryProtocol
    memories: MemoryRegistryProtocol
    engine: Engine | None


def build_registries(config: BackendConfig) -> RegistryBundle:
    """Construct registry implementations for the configured backend."""
    if config.backend == "memory":
        return _build_memory_bundle()
    if config.backend == "sql":
        return _build_sql_bundle(config)
    # ``BackendConfig`` validates on construction, so this is unreachable;
    # include the raise for exhaustiveness-safety against future enum values.
    raise ValueError(f"Unsupported backend: {config.backend!r}")


# ── Backend builders ────────────────────────────────────────────────────────


def _build_memory_bundle() -> RegistryBundle:
    return RegistryBundle(
        experiments=ExperimentRegistry(),
        hypotheses=HypothesisRegistry(),
        memories=MemoryRegistry(),
        engine=None,
    )


def _build_sql_bundle(config: BackendConfig) -> RegistryBundle:
    # Imported lazily so the memory backend has no SQLAlchemy overhead.
    from sqlalchemy import create_engine

    from alpha_harness.db.connection import metadata
    from alpha_harness.registries import tables as _tables  # noqa: F401
    from alpha_harness.registries.sql_experiment import SqlExperimentRegistry
    from alpha_harness.registries.sql_hypothesis import SqlHypothesisRegistry
    from alpha_harness.registries.sql_memory import SqlMemoryRegistry

    engine = create_engine(config.postgres.url, echo=False)
    if config.auto_create_tables:
        metadata.create_all(engine)

    return RegistryBundle(
        experiments=SqlExperimentRegistry(engine),
        hypotheses=SqlHypothesisRegistry(engine),
        memories=SqlMemoryRegistry(engine),
        engine=engine,
    )
