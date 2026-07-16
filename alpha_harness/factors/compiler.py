"""Factor compiler — parses DSL expressions and produces validated FactorSpecs.

Implements the FactorCompiler protocol. Replaces the Round 1 stub compiler.

The compiler:
    1. Extracts the DSL expression from the hypothesis text or a dedicated field.
    2. Parses it via the recursive-descent parser into an AST.
    3. Validates the AST (arity checks, window constraints).
    4. Stores the AST in FactorSpec.operator_tree.
    5. Returns a FactorSpec ready for execution.

If parsing or validation fails, raises DslCompilationError which the
orchestrator can catch and classify as FailureCategory.COMPILATION_ERROR.
"""

from __future__ import annotations

import re

from alpha_harness.factors.dsl_parser import (
    DslParseError,
    parse_expression,
    validate_ast,
)
from alpha_harness.schemas.factor import FactorSpec
from alpha_harness.schemas.hypothesis import Hypothesis


class DslCompilationError(Exception):
    """Raised when a hypothesis cannot be compiled into a valid factor."""


class FactorDslCompiler:
    """Compile hypotheses into validated FactorSpecs via the factor DSL.

    Implements the ``FactorCompiler`` protocol from ``service.py``.

    The compiler looks for a DSL expression in the hypothesis text. Expressions
    can be:
        - The full hypothesis text if it looks like a DSL expression
          (e.g. "rank(ts_mean(close, 20))")
        - Embedded in the hypothesis text after "expr:" or "expression:"
          (e.g. "Momentum signal: expr: rank(ts_mean(close, 20))")
    """

    def compile(self, hypothesis: Hypothesis) -> FactorSpec:
        """Parse, validate, and compile a hypothesis into a FactorSpec.

        Raises DslCompilationError if the expression is invalid.
        """
        expression = self._extract_expression(hypothesis)
        name = _expression_to_name(expression)

        # Parse into AST
        try:
            ast = parse_expression(expression)
        except DslParseError as e:
            raise DslCompilationError(f"Failed to parse expression {expression!r}: {e}") from e

        # Validate AST (reuse the already-parsed tree, no double parse)
        errors = validate_ast(ast)
        if errors:
            raise DslCompilationError(f"Validation errors in {expression!r}: {'; '.join(errors)}")

        return FactorSpec(
            name=name,
            expression=expression,
            operator_tree=ast,
            hypothesis_id=hypothesis.id,
        )

    def _extract_expression(self, hypothesis: Hypothesis) -> str:
        """Extract the DSL expression from hypothesis text.

        Looks for explicit markers first, falls back to using the full text.
        """
        text = hypothesis.text.strip()

        # Look for "expr:" or "expression:" marker
        for marker in ("expr:", "expression:"):
            idx = text.lower().find(marker)
            if idx >= 0:
                return text[idx + len(marker) :].strip()

        # Use the full text as the expression
        return text


def _expression_to_name(expression: str, max_length: int = 60) -> str:
    """Derive a readable factor name from the expression string."""
    name = expression.lower().strip()
    name = re.sub(r"[^a-z0-9_]+", "_", name)
    name = re.sub(r"_+", "_", name)
    name = name.strip("_")
    return name[:max_length] or "unnamed_factor"
