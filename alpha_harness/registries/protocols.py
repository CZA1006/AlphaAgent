"""Backend-agnostic registry protocols.

The orchestrator, retrieval, and refinement layers only care about *behaviour*:
they save records, list them, and occasionally filter by status or decision.
By typing those collaborators against a Protocol we can swap between the
in-memory implementations (fast, zero-setup, used for tests and local runs)
and the SQL-backed implementations (used when persistence is enabled) without
any call-site changes.

Protocols here define the *minimum surface* relied on by the research loop.
Concrete registries may expose richer query helpers — those are fine to use
directly when the caller is explicitly talking to one backend.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from alpha_harness.schemas.experiment import ExperimentDecision, ExperimentRecord
from alpha_harness.schemas.hypothesis import Hypothesis, HypothesisStatus
from alpha_harness.schemas.memory import MemoryCategory, MemoryEntry


@runtime_checkable
class ExperimentRegistryProtocol(Protocol):
    """Common API over in-memory and SQL experiment registries."""

    def save(self, entity: ExperimentRecord) -> str: ...
    def get(self, entity_id: str) -> ExperimentRecord | None: ...
    def list_all(self) -> list[ExperimentRecord]: ...
    def list_by_decision(
        self,
        decision: ExperimentDecision,
    ) -> list[ExperimentRecord]: ...
    def list_by_hypothesis(
        self,
        hypothesis_id: str,
    ) -> list[ExperimentRecord]: ...
    def list_promoted(self) -> list[ExperimentRecord]: ...
    def list_rejected(self) -> list[ExperimentRecord]: ...
    def list_recent(self, limit: int = 20) -> list[ExperimentRecord]: ...


@runtime_checkable
class HypothesisRegistryProtocol(Protocol):
    """Common API over in-memory and SQL hypothesis registries."""

    def save(self, entity: Hypothesis) -> str: ...
    def get(self, entity_id: str) -> Hypothesis | None: ...
    def list_all(self) -> list[Hypothesis]: ...
    def list_by_status(
        self,
        status: HypothesisStatus,
    ) -> list[Hypothesis]: ...
    def list_actionable(self) -> list[Hypothesis]: ...
    def list_recent(self, limit: int = 20) -> list[Hypothesis]: ...


@runtime_checkable
class MemoryRegistryProtocol(Protocol):
    """Common API over in-memory and SQL memory registries."""

    def save(self, entity: MemoryEntry) -> str: ...
    def get(self, entity_id: str) -> MemoryEntry | None: ...
    def list_all(self) -> list[MemoryEntry]: ...
    def list_by_category(
        self,
        category: MemoryCategory,
    ) -> list[MemoryEntry]: ...
    def list_by_experiment(self, experiment_id: str) -> list[MemoryEntry]: ...
    def list_by_tag(self, tag: str) -> list[MemoryEntry]: ...
