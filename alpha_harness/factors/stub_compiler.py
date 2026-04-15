"""Stub factor compiler — placeholder for the Factor DSL.

Implements the FactorCompiler protocol by slugifying hypothesis text into
a FactorSpec. Replace with a real DSL parser and AST builder in Round 2.
"""

from __future__ import annotations

import re

from alpha_harness.schemas.factor import FactorSpec
from alpha_harness.schemas.hypothesis import Hypothesis


class StubFactorCompiler:
    """Compile a hypothesis into a FactorSpec using simple text extraction.

    Implements the ``FactorCompiler`` protocol from ``service.py``.
    """

    def compile(self, hypothesis: Hypothesis) -> FactorSpec:
        """Generate a FactorSpec from hypothesis text.

        The expression is a sanitized, lowercased slug of the hypothesis text.
        This is a stub — the real compiler will parse DSL expressions and
        build operator trees.
        """
        name = _slugify(hypothesis.text)
        expression = f"stub({name})"

        return FactorSpec(
            name=name,
            expression=expression,
            hypothesis_id=hypothesis.id,
        )


def _slugify(text: str, max_length: int = 40) -> str:
    """Convert text to a clean identifier-style slug."""
    slug = text.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    slug = slug.strip("_")
    return slug[:max_length]
