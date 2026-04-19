"""Experiment registry — stores and queries ExperimentRecords.

In-memory implementation for Milestone 1. Postgres-backed implementation
will use the table definition in tables.py and JSON-serialize the full record.
"""

from __future__ import annotations

from alpha_harness.registries.base import InMemoryRegistry
from alpha_harness.schemas.experiment import ExperimentDecision, ExperimentRecord


class ExperimentRegistry(InMemoryRegistry[ExperimentRecord]):
    """Registry for experiment records with domain-specific queries."""

    def list_by_decision(self, decision: ExperimentDecision) -> list[ExperimentRecord]:
        """Return all experiments with a given decision."""
        return [e for e in self.list_all() if e.decision == decision]

    def list_by_hypothesis(self, hypothesis_id: str) -> list[ExperimentRecord]:
        """Return all experiments derived from a specific hypothesis."""
        return [e for e in self.list_all() if e.hypothesis.id == hypothesis_id]

    def list_promoted(self) -> list[ExperimentRecord]:
        """Return all experiments that were promoted."""
        return self.list_by_decision(ExperimentDecision.PROMOTE_CANDIDATE)

    def list_rejected(self) -> list[ExperimentRecord]:
        """Return all experiments that were rejected."""
        return self.list_by_decision(ExperimentDecision.REJECT)

    def list_recent(self, limit: int = 20) -> list[ExperimentRecord]:
        """Return the most recent experiments, newest first.

        Mirrors the SQL-backed registry's method so retrieval / context
        code can call either implementation interchangeably.
        """
        return sorted(
            self.list_all(), key=lambda e: e.created_at, reverse=True
        )[:limit]
