"""Hypothesis registry — stores and queries research hypotheses."""

from __future__ import annotations

from alpha_harness.registries.base import InMemoryRegistry
from alpha_harness.schemas.hypothesis import Hypothesis, HypothesisStatus


class HypothesisRegistry(InMemoryRegistry[Hypothesis]):
    """Registry for hypotheses with domain-specific queries."""

    def list_by_status(self, status: HypothesisStatus) -> list[Hypothesis]:
        """Return all hypotheses with a given status."""
        return [h for h in self.list_all() if h.status == status]

    def list_actionable(self) -> list[Hypothesis]:
        """Return hypotheses that are ready for testing."""
        return self.list_by_status(HypothesisStatus.DRAFT)

    def list_recent(self, limit: int = 20) -> list[Hypothesis]:
        """Return the most recent hypotheses, newest first.

        Mirrors :class:`SqlHypothesisRegistry.list_recent` so the shared
        protocol is satisfied by both backends.
        """
        return sorted(
            self.list_all(), key=lambda h: h.created_at, reverse=True
        )[:limit]
