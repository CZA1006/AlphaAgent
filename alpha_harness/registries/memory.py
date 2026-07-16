"""Memory registry — stores structured research memory entries."""

from __future__ import annotations

from alpha_harness.registries.base import InMemoryRegistry
from alpha_harness.schemas.memory import MemoryCategory, MemoryEntry


class MemoryRegistry(InMemoryRegistry[MemoryEntry]):
    """Registry for research memory with domain-specific queries."""

    def list_by_category(self, category: MemoryCategory) -> list[MemoryEntry]:
        """Return all memory entries of a given category."""
        return [m for m in self.list_all() if m.category == category]

    def list_by_experiment(self, experiment_id: str) -> list[MemoryEntry]:
        """Return all memory entries linked to a specific experiment."""
        return [m for m in self.list_all() if experiment_id in m.source_experiment_ids]

    def list_by_tag(self, tag: str) -> list[MemoryEntry]:
        """Return all memory entries matching a tag."""
        return [m for m in self.list_all() if tag in m.tags]
