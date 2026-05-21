"""Hashable description of a basket (Round 8).

A :class:`CombinationRecipe` captures *what* a basket is — the
combination method plus the canonical component expressions — without
saying anything about how it was evaluated.  Two recipes that describe
the same basket (e.g. ``equal_weight(A, B, C)`` and
``equal_weight(C, B, A)``) share the same ``recipe_id`` so the
novelty check can't be tricked by permuted order.

This module lives in :mod:`alpha_harness.combination` (not
:mod:`alpha_harness.reports`) because the recipe is a value type used
by both ``FactorSpec.composite_recipe`` (registry-side) and the
combination report (audit-side).  Keeping it here avoids the
``schemas → reports`` import cycle.
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, Field

from alpha_harness.combination.combiner import CombinationMethod
from alpha_harness.factors.canonical import canonicalize
from alpha_harness.factors.dsl_parser import DslParseError, parse_expression


def _canonical_hash(expression: str) -> str:
    """Return a stable 16-hex-char digest for a single DSL expression.

    Uses the same canonicalizer the novelty check uses so two expressions
    that differ only in commutative-operand order collapse to the same
    component hash.  Raises ``ValueError`` if the expression won't parse —
    re-wrapping ``DslParseError`` keeps callers from having to depend on
    DSL internals.
    """
    try:
        ast = parse_expression(expression)
    except DslParseError as exc:
        raise ValueError(
            f"unparseable component expression {expression!r}: {exc}",
        ) from exc
    canon_repr = repr(canonicalize(ast))
    return hashlib.sha256(canon_repr.encode("utf-8")).hexdigest()[:16]


def recipe_id_for(method: CombinationMethod, components: list[str]) -> str:
    """SHA-256 over ``(method, sorted component hashes)``.

    Sorting is the load-bearing step: ``equal_weight(A, B, C)`` and
    ``equal_weight(C, B, A)`` describe the same basket and must hash
    identically, otherwise the novelty check would let the proposer
    re-promote the same recipe under a permuted order.
    """
    component_hashes = sorted(_canonical_hash(e) for e in components)
    payload = method.value + "|" + "|".join(component_hashes)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class CombinationRecipe(BaseModel):
    """Hashable description of one basket.

    ``component_factor_ids`` is populated when the components were
    loaded from a registry / validation report (so promotion can cite
    their lineage); otherwise it's an empty list and the components
    are anonymous.

    ``recipe_id`` must equal ``recipe_id_for(method, components)`` —
    callers should construct via :meth:`build` to enforce this rather
    than passing the id directly.
    """

    method: CombinationMethod
    components: list[str]
    component_factor_ids: list[str] = Field(default_factory=list)
    recipe_id: str

    @classmethod
    def build(
        cls,
        *,
        method: CombinationMethod,
        components: list[str],
        component_factor_ids: list[str] | None = None,
    ) -> CombinationRecipe:
        """Construct a recipe with ``recipe_id`` derived from the inputs."""
        return cls(
            method=method,
            components=list(components),
            component_factor_ids=list(component_factor_ids or []),
            recipe_id=recipe_id_for(method, components),
        )
