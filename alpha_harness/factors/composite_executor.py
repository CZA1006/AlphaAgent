"""Execute a basket :class:`CombinationRecipe` against a bar panel.

Composite factors (Round 8) don't have a DSL string — they're recipes
that combine N component DSL expressions.  This module is the surgical
adapter that turns a recipe into a per-(date, asset) signal series the
existing :class:`SignalQualityEvaluator` (and therefore every Round
4 gate) can score.

Pure: no I/O, no mutation of inputs.  Nested recipes are rejected — a
component must be a plain DSL expression, never another composite.
We can lift that restriction in a later round; for now it's a guard
against accidental infinite chains.
"""

from __future__ import annotations

import pandas as pd

from alpha_harness.combination import (
    CombinationRecipe,
    combine_signals,
    compute_signal,
)


def execute_composite(recipe: CombinationRecipe, df: pd.DataFrame) -> pd.Series:
    """Return the basket signal for ``recipe`` evaluated over ``df``.

    The output series has the same length as ``df`` and is aligned by
    row position — same contract every other ``compute_signal`` caller
    in the harness assumes.

    Raises ``ValueError`` if the recipe has no components or if any
    component expression fails to parse / execute.
    """
    if not recipe.components:
        raise ValueError("composite recipe has no components")
    signals: list[pd.Series] = []
    for i, expr in enumerate(recipe.components):
        try:
            signals.append(compute_signal(expr, df))
        except Exception as exc:
            raise ValueError(
                f"composite component {i} ({expr!r}) failed to execute: {exc}",
            ) from exc
    return combine_signals(signals, df["timestamp"], recipe.method)
