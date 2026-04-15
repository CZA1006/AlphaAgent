"""Base registry protocol — typed interface shared by all registries.

Every registry provides save/get/list/search over a specific domain entity.
Implementations may be in-memory (for testing) or backed by Postgres.
"""

from __future__ import annotations

from typing import Generic, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


@runtime_checkable
class BaseRegistry(Protocol[T]):
    """Protocol for all Alpha Harness registries.

    Generic over the Pydantic model type it stores.
    """

    def save(self, entity: T) -> str:
        """Persist an entity. Returns its id."""
        ...

    def get(self, entity_id: str) -> T | None:
        """Retrieve a single entity by id, or None if not found."""
        ...

    def list_all(self) -> list[T]:
        """Return all stored entities."""
        ...

    def search(self, **filters: str) -> list[T]:
        """Search entities by field-value filters.

        Each keyword argument is matched against the corresponding field
        on the entity. Only exact string matches are supported in the
        base protocol; implementations may add richer query support.
        """
        ...


class InMemoryRegistry(Generic[T]):
    """In-memory registry implementation for testing and local development.

    Stores entities in a plain dict keyed by id. No persistence across restarts.
    """

    def __init__(self) -> None:
        self._store: dict[str, T] = {}

    def save(self, entity: T) -> str:
        entity_id: str = entity.id  # type: ignore[attr-defined]
        self._store[entity_id] = entity
        return entity_id

    def get(self, entity_id: str) -> T | None:
        return self._store.get(entity_id)

    def list_all(self) -> list[T]:
        return list(self._store.values())

    def search(self, **filters: str) -> list[T]:
        results: list[T] = []
        for entity in self._store.values():
            if all(
                str(getattr(entity, field, None)) == value
                for field, value in filters.items()
            ):
                results.append(entity)
        return results
