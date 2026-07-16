"""Unit tests for BackendConfig and the registry factory (memory path only)."""

from __future__ import annotations

import pytest

from alpha_harness.config import BackendConfig, PostgresSettings
from alpha_harness.registries.experiment import ExperimentRegistry
from alpha_harness.registries.factory import build_registries
from alpha_harness.registries.hypothesis import HypothesisRegistry
from alpha_harness.registries.memory import MemoryRegistry
from alpha_harness.registries.protocols import (
    ExperimentRegistryProtocol,
    HypothesisRegistryProtocol,
    MemoryRegistryProtocol,
)

# ── BackendConfig ────────────────────────────────────────────────────────────


def test_default_backend_is_memory():
    assert BackendConfig().backend == "memory"


def test_rejects_invalid_backend():
    with pytest.raises(ValueError, match="Invalid backend"):
        BackendConfig(backend="sqlite")  # type: ignore[arg-type]


def test_from_env_defaults_to_memory(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ALPHA_AGENT_BACKEND", raising=False)
    cfg = BackendConfig.from_env()
    assert cfg.backend == "memory"


def test_from_env_reads_alpha_agent_backend(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALPHA_AGENT_BACKEND", "sql")
    cfg = BackendConfig.from_env()
    assert cfg.backend == "sql"


def test_from_env_explicit_override_wins(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALPHA_AGENT_BACKEND", "sql")
    cfg = BackendConfig.from_env(override="memory")
    assert cfg.backend == "memory"


def test_from_env_rejects_unknown_value(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALPHA_AGENT_BACKEND", "redis")
    with pytest.raises(ValueError, match="Invalid backend"):
        BackendConfig.from_env()


def test_postgres_settings_build_url():
    s = PostgresSettings(
        user="u",
        password="p",
        host="h",
        port="5432",
        database="db",
    )
    assert s.url == "postgresql+psycopg://u:p@h:5432/db"


def test_postgres_settings_from_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("POSTGRES_USER", "ci")
    monkeypatch.setenv("POSTGRES_HOST", "pg.internal")
    s = PostgresSettings.from_env()
    assert s.user == "ci"
    assert s.host == "pg.internal"


# ── Factory (memory backend; SQL backend has its own integration test) ──────


def test_factory_memory_backend_returns_in_memory_registries():
    bundle = build_registries(BackendConfig.memory())

    assert bundle.engine is None
    assert isinstance(bundle.experiments, ExperimentRegistry)
    assert isinstance(bundle.hypotheses, HypothesisRegistry)
    assert isinstance(bundle.memories, MemoryRegistry)
    # And they also honour the shared protocol shape.
    assert isinstance(bundle.experiments, ExperimentRegistryProtocol)
    assert isinstance(bundle.hypotheses, HypothesisRegistryProtocol)
    assert isinstance(bundle.memories, MemoryRegistryProtocol)


def test_factory_memory_backend_is_isolated_between_calls():
    """Two calls should yield distinct registry instances, not a shared singleton."""
    first = build_registries(BackendConfig.memory())
    second = build_registries(BackendConfig.memory())

    assert first.experiments is not second.experiments
    assert first.hypotheses is not second.hypotheses
    assert first.memories is not second.memories
