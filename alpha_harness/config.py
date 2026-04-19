"""Runtime configuration for AlphaAgent.

Small, typed, and explicit.  The only setting that actually matters for
routing is :attr:`BackendConfig.backend` — either ``"memory"`` (the zero-
setup default) or ``"sql"`` (Postgres-backed persistence).  Everything
else is plumbing.

Selection precedence (highest wins):

    1. Explicit argument to :meth:`BackendConfig.from_env` / constructor.
    2. ``ALPHA_AGENT_BACKEND`` environment variable.
    3. Module default (``"memory"``).

This module does *not* import SQLAlchemy or open connections — that happens
in :mod:`alpha_harness.registries.factory` so tests that never touch SQL
don't pay the import cost.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

Backend = Literal["memory", "sql"]

_VALID_BACKENDS: tuple[Backend, ...] = ("memory", "sql")


@dataclass(frozen=True)
class PostgresSettings:
    """Connection parameters for the SQL backend.

    Defaults mirror :func:`alpha_harness.db.connection.get_database_url` so
    existing scripts and Docker recipes keep working unchanged.
    """

    user: str = "alphaagent"
    password: str = "alphaagent_dev"
    host: str = "localhost"
    port: str = "5432"
    database: str = "alphaagent"

    @classmethod
    def from_env(cls) -> PostgresSettings:
        return cls(
            user=os.environ.get("POSTGRES_USER", cls.user),
            password=os.environ.get("POSTGRES_PASSWORD", cls.password),
            host=os.environ.get("POSTGRES_HOST", cls.host),
            port=os.environ.get("POSTGRES_PORT", cls.port),
            database=os.environ.get("POSTGRES_DB", cls.database),
        )

    @property
    def url(self) -> str:
        return (
            f"postgresql+psycopg://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.database}"
        )


@dataclass(frozen=True)
class BackendConfig:
    """Top-level persistence configuration.

    Parameters
    ----------
    backend:
        ``"memory"`` for in-process registries (default) or ``"sql"`` for
        Postgres-backed registries.
    postgres:
        Connection settings used when ``backend == "sql"``.  Ignored in
        memory mode.
    auto_create_tables:
        When ``True`` and ``backend == "sql"``, the registry factory runs
        ``metadata.create_all(engine)`` on construction.  Convenient for
        local dev and tests; set to ``False`` in production where migrations
        own schema.
    """

    backend: Backend = "memory"
    postgres: PostgresSettings = PostgresSettings()
    auto_create_tables: bool = True

    def __post_init__(self) -> None:
        if self.backend not in _VALID_BACKENDS:
            raise ValueError(
                f"Invalid backend {self.backend!r}; "
                f"expected one of {_VALID_BACKENDS}.",
            )

    # ── Constructors ────────────────────────────────────────────────────

    @classmethod
    def memory(cls) -> BackendConfig:
        """Convenience constructor for the in-memory backend."""
        return cls(backend="memory")

    @classmethod
    def sql(
        cls,
        postgres: PostgresSettings | None = None,
        auto_create_tables: bool = True,
    ) -> BackendConfig:
        """Convenience constructor for the SQL backend."""
        return cls(
            backend="sql",
            postgres=postgres or PostgresSettings.from_env(),
            auto_create_tables=auto_create_tables,
        )

    @classmethod
    def from_env(
        cls,
        override: Backend | None = None,
    ) -> BackendConfig:
        """Resolve the backend from an explicit argument or environment.

        Accepts the literal strings ``"memory"`` and ``"sql"``.  Any other
        value raises :class:`ValueError` — silent fallback hides bugs.
        """
        raw: str | None = override or os.environ.get("ALPHA_AGENT_BACKEND")
        backend: Backend = "memory" if raw is None else _coerce_backend(raw)
        if backend == "sql":
            return cls.sql(PostgresSettings.from_env())
        return cls.memory()


def _coerce_backend(raw: str) -> Backend:
    normalised = raw.strip().lower()
    if normalised not in _VALID_BACKENDS:
        raise ValueError(
            f"Invalid backend {raw!r}; "
            f"expected one of {_VALID_BACKENDS}.",
        )
    # The membership check above narrows ``normalised`` to ``Backend``.
    if normalised == "memory":
        return "memory"
    return "sql"
