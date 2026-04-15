"""Skill registry — stores reusable research patterns distilled from experiments."""

from __future__ import annotations

from alpha_harness.registries.base import InMemoryRegistry
from alpha_harness.schemas.skill import Skill


class SkillRegistry(InMemoryRegistry[Skill]):
    """Registry for research skills with domain-specific queries."""

    def list_promoted(self) -> list[Skill]:
        """Return all skills that have been promoted to active use."""
        return [s for s in self.list_all() if s.promoted]

    def list_by_tag(self, tag: str) -> list[Skill]:
        """Return all skills matching a tag."""
        return [s for s in self.list_all() if tag in s.tags]
