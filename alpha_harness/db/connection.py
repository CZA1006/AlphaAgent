"""Postgres connection helper.

Uses SQLAlchemy Core (not ORM) for lightweight, typed database access.
Registry table definitions live in alpha_harness.registries.tables.
"""

from __future__ import annotations

import os

from sqlalchemy import MetaData, create_engine
from sqlalchemy.engine import Engine


def get_database_url() -> str:
    """Build Postgres connection URL from environment variables."""
    user = os.environ.get("POSTGRES_USER", "alphaagent")
    password = os.environ.get("POSTGRES_PASSWORD", "alphaagent_dev")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "alphaagent")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


def get_engine() -> Engine:
    """Create a SQLAlchemy engine from environment config."""
    return create_engine(get_database_url(), echo=False)


# Shared metadata instance for all registry tables.
metadata = MetaData()
