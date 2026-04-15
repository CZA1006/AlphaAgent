"""Factor registry — stores and queries compiled factor specifications."""

from __future__ import annotations

from alpha_harness.registries.base import InMemoryRegistry
from alpha_harness.schemas.factor import FactorSpec


class FactorRegistry(InMemoryRegistry[FactorSpec]):
    """Registry for factor specs with domain-specific queries."""

    def list_by_universe(self, universe_id: str) -> list[FactorSpec]:
        """Return all factors targeting a specific universe."""
        return [f for f in self.list_all() if f.universe_id == universe_id]

    def list_by_hypothesis(self, hypothesis_id: str) -> list[FactorSpec]:
        """Return all factors derived from a specific hypothesis."""
        return [f for f in self.list_all() if f.hypothesis_id == hypothesis_id]
