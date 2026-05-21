"""Factor specification schema — a compiled, executable factor definition."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from alpha_harness.combination.recipe import CombinationRecipe


class FactorSpec(BaseModel):
    """A safe, declarative factor specification.

    The expression field holds a DSL string (e.g. "rank(ts_mean(close, 20))").
    The operator_tree field will hold the parsed AST once the factor DSL is built.

    Round 8: composite factors (baskets) are first-class registry citizens.
    When ``composite_recipe`` is populated, the factor is a basket of
    component DSL expressions combined by the named method.  In that case
    ``expression`` is a placeholder like ``"<composite:{recipe_id}>"``
    (kept non-empty for downstream tooling that requires it) and
    ``operator_tree`` is ``None`` — the executor dispatches on
    ``composite_recipe`` instead of parsing the expression.  Scalar
    factors leave ``composite_recipe=None`` and behave exactly as before.
    """

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str
    expression: str  # DSL expression string (or "<composite:...>" placeholder)
    operator_tree: dict[str, Any] | None = None  # parsed AST — populated by factor compiler
    universe_id: str = ""  # references a UniverseSpec.id
    params: dict[str, float | int | str] = Field(default_factory=dict)
    hypothesis_id: str | None = None
    # Refinement lineage — populated when this factor was produced by a
    # bounded mutation of another factor under ``RefinementRunner``.
    # Root factors have ``parent_factor_id=None`` and ``refinement_round=0``.
    parent_factor_id: str | None = None
    refinement_round: int = 0
    # Round 8 — basket recipe when this factor is a composite.  None for
    # ordinary DSL factors; non-None for promoted combinations.
    composite_recipe: CombinationRecipe | None = None
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
    )
