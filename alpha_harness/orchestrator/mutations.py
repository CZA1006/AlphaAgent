"""Deterministic mutation templates for refinement.

Given a validated DSL expression, produce a small, ordered list of
*structurally related* candidate expressions.  These are syntactic edits
only — no semantic reasoning and no LLM — so the output is fully
reproducible.  Every candidate is still re-validated by the DSL compiler
downstream; this module simply *proposes* the variants.

The supported templates are:

    * window_halve / window_double
        For each time-series / lag function with an integer window
        argument, scale the window by 0.5 or 2.  Windows are clamped to
        a minimum of 1.

    * wrap_rank / wrap_zscore
        Wrap the whole expression with a cross-sectional transform, but
        only when it is not already the outer transform.

    * unwrap_outer
        Strip an outer ``rank(...)`` or ``zscore(...)`` to expose the
        underlying time-series signal.

Templates are intentionally conservative — each one changes the
expression in a way a human researcher would recognize as a "nearby"
hypothesis, not a fundamentally new one.
"""

from __future__ import annotations

import copy
from typing import Any

from alpha_harness.factors.dsl_parser import DslParseError, parse_expression
from alpha_harness.refiner import RefinementBrief

# ── Public API ──────────────────────────────────────────────────────────────


def propose_mutations(
    expression: str,
    brief: RefinementBrief | None = None,
) -> list[tuple[str, str]]:
    """Return an ordered, deduplicated list of ``(expression, label)`` mutations.

    The input may be any string; if it fails to parse as DSL, the function
    returns an empty list rather than raising — callers treat "no mutations
    available" as a termination signal.

    Ordering prioritises the most meaningful edits first so that callers
    who only take the first ``k`` variants still get a representative
    sample.

    When a :class:`~alpha_harness.refiner.RefinementBrief` is supplied, the
    candidate list is re-sorted (stably) by how well each template targets
    the flagged weakness — a ``turnover_high`` brief, for example, promotes
    ``window_double`` ahead of ``window_halve``.  Absent a brief, ordering
    is the legacy default.
    """
    try:
        ast = parse_expression(expression)
    except DslParseError:
        return []

    candidates: list[tuple[str, str]] = []
    seen: set[str] = {_normalize(expression)}

    def _emit(new_ast: dict[str, Any], label: str) -> None:
        rendered = render(new_ast)
        key = _normalize(rendered)
        if key in seen:
            return
        seen.add(key)
        candidates.append((rendered, label))

    # 1. Unwrap an outer rank/zscore first (produces the "raw signal" view).
    unwrapped = _unwrap_outer(ast)
    if unwrapped is not None:
        _emit(unwrapped, "unwrap_outer")

    # 2. Window scaling — first the first window we find (halve, then double).
    for new_ast, label in _scale_first_window(ast):
        _emit(new_ast, label)

    # 3. Wrap with a cross-sectional transform if not already applied.
    for new_ast, label in _wrap_cross_sectional(ast):
        _emit(new_ast, label)

    if brief is not None and not brief.is_empty:
        candidates = _prioritize(candidates, brief)

    return candidates


def _prioritize(
    candidates: list[tuple[str, str]],
    brief: RefinementBrief,
) -> list[tuple[str, str]]:
    """Re-order ``candidates`` by how well each label targets the brief.

    Higher score = runs first.  Python's ``sorted`` is stable so equal-
    scored candidates preserve the legacy pre-order.
    """

    def score(item: tuple[str, str]) -> int:
        _, label = item
        s = 0
        # Smoothing (window_double) helps turnover, sign-flipping, cost drag.
        if label.startswith("window_double"):
            if brief.turnover_high:
                s += 3
            if brief.sign_inconsistent:
                s += 2
            if brief.cost_drag_large:
                s += 2
        # Sharper windows usually make things worse when those flags are set.
        if label.startswith("window_halve"):
            if brief.turnover_high:
                s -= 3
            if brief.sign_inconsistent:
                s -= 2
            if brief.cost_drag_large:
                s -= 1
        # Cross-sectional wrapping is the standard lever for weak IC.
        if label in ("wrap_rank", "wrap_zscore") and brief.weak_cross_sectional:
            s += 3
        # Unwrapping exposes a raw signal — rarely what you want when the
        # cross-sectional rank is already borderline or turnover is hot.
        if label == "unwrap_outer":
            if brief.weak_cross_sectional:
                s -= 2
            if brief.turnover_high:
                s -= 1
        return -s  # sorted ascending; negate so higher-score comes first

    return sorted(candidates, key=score)


def render(ast: dict[str, Any]) -> str:
    """Render a DSL AST back into a parseable expression string.

    Binary operators and unary minus are always parenthesized; this is
    syntactically redundant but keeps the output unambiguous and easy to
    eyeball.
    """
    node_type = ast.get("type")

    if node_type == "number":
        value = float(ast["value"])
        if value.is_integer():
            return str(int(value))
        return repr(value)

    if node_type == "field":
        return str(ast["name"])

    if node_type == "function":
        name = str(ast["name"])
        args = ", ".join(render(a) for a in ast["args"])
        return f"{name}({args})"

    if node_type == "unary":
        return f"(-{render(ast['operand'])})"

    if node_type == "binop":
        left = render(ast["left"])
        right = render(ast["right"])
        return f"({left} {ast['op']} {right})"

    raise ValueError(f"Unknown AST node type: {node_type!r}")


# ── Internal: individual templates ──────────────────────────────────────────


_WINDOWED_FUNCTIONS = frozenset(
    {
        "lag",
        "ts_lag",
        "ts_mean",
        "ts_std",
        "ts_sum",
        "ts_min",
        "ts_max",
        "ts_delta",
    }
)
_CROSS_SECTIONAL = frozenset({"rank", "zscore"})


def _unwrap_outer(ast: dict[str, Any]) -> dict[str, Any] | None:
    """If the root is a cross-sectional transform, return its single argument."""
    if (
        ast.get("type") == "function"
        and ast.get("name") in _CROSS_SECTIONAL
        and len(ast.get("args", [])) == 1
    ):
        inner: dict[str, Any] = copy.deepcopy(ast["args"][0])
        return inner
    return None


def _wrap_cross_sectional(
    ast: dict[str, Any],
) -> list[tuple[dict[str, Any], str]]:
    """Wrap the expression in ``rank(...)`` and ``zscore(...)`` as appropriate."""
    out: list[tuple[dict[str, Any], str]] = []
    current_outer = ast.get("name") if ast.get("type") == "function" else None
    for wrapper in ("rank", "zscore"):
        if wrapper == current_outer:
            continue
        wrapped: dict[str, Any] = {
            "type": "function",
            "name": wrapper,
            "args": [copy.deepcopy(ast)],
        }
        out.append((wrapped, f"wrap_{wrapper}"))
    return out


def _scale_first_window(
    ast: dict[str, Any],
) -> list[tuple[dict[str, Any], str]]:
    """Scale the first encountered window argument by 0.5x and 2x."""
    found = _find_first_window_path(ast)
    if found is None:
        return []
    path, current = found
    halved = max(1, int(current // 2))
    doubled = max(1, int(current * 2))

    variants: list[tuple[dict[str, Any], str]] = []
    if halved != int(current):
        variants.append(
            (
                _set_at_path(ast, path, float(halved)),
                f"window_halve:{int(current)}->{halved}",
            )
        )
    if doubled != int(current):
        variants.append(
            (
                _set_at_path(ast, path, float(doubled)),
                f"window_double:{int(current)}->{doubled}",
            )
        )
    return variants


def _find_first_window_path(
    ast: dict[str, Any],
) -> tuple[list[Any], float] | None:
    """Locate the first window literal under a windowed function.

    Returns ``(path, current_value)`` where ``path`` is a sequence of
    ``dict``-keys / ``list``-indices navigating from ``ast`` to the literal
    number node.  Deterministic pre-order traversal.
    """

    def _walk(node: dict[str, Any], path: list[Any]) -> tuple[list[Any], float] | None:
        ntype = node.get("type")
        if ntype == "function":
            name = node.get("name")
            args = node.get("args", [])
            if name in _WINDOWED_FUNCTIONS and len(args) >= 2 and args[1].get("type") == "number":
                return (
                    [*path, "args", 1, "value"],
                    float(args[1]["value"]),
                )
            for i, arg in enumerate(args):
                hit = _walk(arg, [*path, "args", i])
                if hit is not None:
                    return hit
        elif ntype == "binop":
            for side in ("left", "right"):
                hit = _walk(node[side], [*path, side])
                if hit is not None:
                    return hit
        elif ntype == "unary":
            return _walk(node["operand"], [*path, "operand"])
        return None

    return _walk(ast, [])


def _set_at_path(
    ast: dict[str, Any],
    path: list[Any],
    value: float,
) -> dict[str, Any]:
    """Return a deep-copy of ``ast`` with ``value`` written at ``path``."""
    clone = copy.deepcopy(ast)
    cursor: Any = clone
    for step in path[:-1]:
        cursor = cursor[step]
    cursor[path[-1]] = value
    return clone


def _normalize(expression: str) -> str:
    """Whitespace-insensitive key for de-duplicating candidate strings."""
    return "".join(expression.split())
