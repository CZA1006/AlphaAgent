"""Factor specification schema — a compiled, executable factor definition."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


class FactorSpec(BaseModel):
    """A safe, declarative factor specification.

    The expression field holds a DSL string (e.g. "rank(ts_mean(close, 20))").
    The operator_tree field will hold the parsed AST once the factor DSL is built.
    """

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str
    expression: str  # DSL expression string
    operator_tree: dict[str, Any] | None = None  # parsed AST — populated by factor compiler
    universe_id: str = ""  # references a UniverseSpec.id
    params: dict[str, float | int | str] = Field(default_factory=dict)
    hypothesis_id: str | None = None
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
    )
