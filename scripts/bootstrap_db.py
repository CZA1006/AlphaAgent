"""Bootstrap the Postgres database with registry tables.

Usage:
    uv run python -m scripts.bootstrap_db

Invoking this as a plain script (``python scripts/bootstrap_db.py``)
does not add the project root to ``sys.path`` and fails with
``ModuleNotFoundError: No module named 'alpha_harness'`` — always use
the ``-m`` form (or ``make db-bootstrap``).

Requires a running Postgres instance (see: make db-up).
"""

from __future__ import annotations

import alpha_harness.registries.tables  # noqa: F401  # registers tables with metadata
from alpha_harness.db.connection import get_engine, metadata


def bootstrap() -> None:
    """Create all registry tables if they don't exist."""
    engine = get_engine()
    metadata.create_all(engine)
    print("Registry tables created successfully.")


if __name__ == "__main__":
    bootstrap()
