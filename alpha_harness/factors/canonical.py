"""Canonical AST normalization and structural similarity for factor DSL.

Two factors that differ only in *syntactic trivia* (whitespace, operand
order of commutative operators, double negations, unary-minus on numbers)
should compare equal after canonicalization.  Two factors that differ
only in *small structural details* (e.g. a window size) should still score
high on structural similarity so the novelty check catches near-duplicates.

Canonical form
--------------
Each AST node is rewritten as a plain nested tuple — immutable, hashable,
and cheap to compare::

    ("num",    float)                     # numeric literal
    ("field",  "close")                   # market-data field
    ("fn",     "ts_mean", (arg1, arg2))   # whitelisted function call
    ("unary",  "-", operand)              # unary minus (only when unavoidable)
    ("binop",  "+", (a, b, …))            # flattened + chain, sorted
    ("binop",  "*", (a, b, …))            # flattened * chain, sorted
    ("binop",  "/", num, den)             # division — non-commutative

Normalizations applied:
    * Identifier case is lowered.
    * ``a - b`` is rewritten as ``a + (-b)`` and folded into an add-chain.
    * ``-(- x)`` collapses to ``x``; ``-(literal)`` folds into the literal.
    * Associative/commutative chains for ``+`` and ``*`` are flattened and
      sorted by a stable key so operand order doesn't matter.
    * Division is left alone (non-commutative, non-associative with ``*``).

The module is pure: no I/O, no mutation of the input AST, no randomness.

Structural similarity
---------------------
``ast_similarity(a, b)`` returns ``1.0`` when the two ASTs share the same
canonical form, else a weighted-Jaccard overlap on the multiset of
``(depth, node_type, name)`` features harvested from each canonical tree.

Two design choices keep the scorer honest:

    * Features carry the tree depth of each node, so structurally distinct
      factors like ``rank(ts_mean(close, 20))`` and ``ts_mean(rank(close),
      20)`` do not collapse to the same multiset.
    * Numeric literal *values* are bucketed by ``round(log(|x|))`` so close
      windows (``ts_mean(close, 20)`` vs ``ts_mean(close, 21)``) still count
      as near-duplicates, but halve/double mutations (20 → 10 or 40) move
      into a different bucket and land comfortably below the 0.95
      refinement novelty threshold.
"""

from __future__ import annotations

import math
from typing import Any

# Canonical nodes are nested tuples.  We keep the type alias loose (``tuple``)
# because ``mypy --strict`` can't express the recursive structure cleanly.
CanonNode = tuple[Any, ...]


# ── Canonicalization ─────────────────────────────────────────────────────────


def canonicalize(ast: dict[str, Any]) -> CanonNode:
    """Return the canonical tuple form of a DSL AST node.

    The input dict is not mutated.  Raises ``ValueError`` for unknown node
    types — parse errors should be surfaced earlier by the DSL parser.
    """
    node_type = ast.get("type")

    if node_type == "number":
        value = float(ast["value"])
        # Normalize -0.0 → 0.0 so hashes/compares are stable.
        if value == 0.0:
            value = 0.0
        return ("num", value)

    if node_type == "field":
        return ("field", str(ast["name"]).lower())

    if node_type == "function":
        name = str(ast["name"]).lower()
        args = tuple(canonicalize(a) for a in ast["args"])
        return ("fn", name, args)

    if node_type == "unary":
        op = ast["op"]
        operand = canonicalize(ast["operand"])
        if op == "-":
            return _negate(operand)
        # Future-proof: any other unary op is preserved verbatim.
        return ("unary", op, operand)

    if node_type == "binop":
        op = ast["op"]
        left = canonicalize(ast["left"])
        right = canonicalize(ast["right"])

        if op == "+":
            add_args = _flatten_add(left) + _flatten_add(right)
            return _build_add_chain(add_args)

        if op == "-":
            # a - b  ≡  a + (-b)
            add_args = _flatten_add(left) + _flatten_add(_negate(right))
            return _build_add_chain(add_args)

        if op == "*":
            mul_args = _flatten_mul(left) + _flatten_mul(right)
            return ("binop", "*", tuple(sorted(mul_args, key=_sort_key)))

        if op == "/":
            # Division is non-commutative; we leave it as a two-arg node.
            return ("binop", "/", left, right)

        # Unknown binary op — preserve verbatim rather than silently munge.
        return ("binop", op, left, right)

    raise ValueError(f"Unknown AST node type: {node_type!r}")


def _negate(node: CanonNode) -> CanonNode:
    """Apply a unary minus with folding: ``-(-x) → x``, ``-(num) → -num``."""
    kind = node[0]
    if kind == "unary" and node[1] == "-":
        # -(-x) → x
        return node[2]  # type: ignore[no-any-return]
    if kind == "num":
        return ("num", -node[1])
    return ("unary", "-", node)


def _flatten_add(node: CanonNode) -> list[CanonNode]:
    """Flatten a canonical ``+`` chain into a list of summands."""
    if node[0] == "binop" and node[1] == "+":
        return list(node[2])
    return [node]


def _flatten_mul(node: CanonNode) -> list[CanonNode]:
    """Flatten a canonical ``*`` chain into a list of factors."""
    if node[0] == "binop" and node[1] == "*":
        return list(node[2])
    return [node]


def _build_add_chain(args: list[CanonNode]) -> CanonNode:
    """Build an add-chain node, short-circuiting the single-operand case."""
    if len(args) == 1:
        return args[0]
    return ("binop", "+", tuple(sorted(args, key=_sort_key)))


def _sort_key(node: CanonNode) -> tuple[Any, ...]:
    """Deterministic sort key used for commutative chains.

    We sort by the node's ``repr`` — it's stable, total, and doesn't require
    a custom comparator per node kind.  This is only used for ordering within
    a chain and never leaks into user-visible output.
    """
    return (repr(node),)


# ── Structural similarity ────────────────────────────────────────────────────


def ast_similarity(a: dict[str, Any], b: dict[str, Any]) -> float:
    """Return a similarity score in ``[0.0, 1.0]`` for two DSL ASTs.

    ``1.0`` means the canonical forms are identical.  Lower scores use a
    weighted Jaccard overlap of ``(node_type, name)`` multisets collected
    across the canonical tree — this catches near-duplicates that differ
    only in window sizes or other numeric constants.
    """
    ca = canonicalize(a)
    cb = canonicalize(b)
    return _canon_similarity(ca, cb)


def canon_similarity(ca: CanonNode, cb: CanonNode) -> float:
    """Similarity score between two already-canonicalized nodes.

    Exposed for callers that canonicalize once and compare many times.
    """
    return _canon_similarity(ca, cb)


def _canon_similarity(ca: CanonNode, cb: CanonNode) -> float:
    if ca == cb:
        return 1.0
    fa = _feature_multiset(ca)
    fb = _feature_multiset(cb)
    keys = set(fa) | set(fb)
    if not keys:
        return 0.0
    inter = sum(min(fa.get(k, 0), fb.get(k, 0)) for k in keys)
    union = sum(max(fa.get(k, 0), fb.get(k, 0)) for k in keys)
    if union == 0:
        return 0.0
    return inter / union


FeatureKey = tuple[Any, ...]


def _feature_multiset(node: CanonNode) -> dict[FeatureKey, int]:
    """Walk a canonical tree and count ``(depth, kind, name)`` features."""
    counts: dict[FeatureKey, int] = {}
    _walk_features(node, counts, depth=0)
    return counts


def _walk_features(
    node: CanonNode, counts: dict[FeatureKey, int], *, depth: int,
) -> None:
    kind = node[0]
    key: FeatureKey

    if kind == "num":
        # Bucket numeric literals by order-of-magnitude so close windows
        # (20 vs 21) still collide but halve/double mutations (20 vs 10/40)
        # land in a different bucket.
        key = (depth, "num", _num_bucket(float(node[1])))
    elif kind == "field":
        key = (depth, "field", str(node[1]))
    elif kind == "fn":
        key = (depth, "fn", str(node[1]))
        for child in node[2]:
            _walk_features(child, counts, depth=depth + 1)
    elif kind == "unary":
        key = (depth, "unary", str(node[1]))
        _walk_features(node[2], counts, depth=depth + 1)
    elif kind == "binop":
        op = str(node[1])
        key = (depth, "binop", op)
        if op in ("+", "*"):
            for child in node[2]:
                _walk_features(child, counts, depth=depth + 1)
        else:
            _walk_features(node[2], counts, depth=depth + 1)
            _walk_features(node[3], counts, depth=depth + 1)
    else:
        key = (depth, str(kind))

    counts[key] = counts.get(key, 0) + 1


def _num_bucket(value: float) -> int:
    """Map a numeric literal to a coarse order-of-magnitude bucket.

    Uses natural log, so the bucket ladder roughly doubles every step:
    values within a ~1.65x ratio share a bucket (20/21/30 collide;
    10/20/40 do not). Zero and near-zero values collapse to bucket ``0``
    to keep the key hashable and stable.
    """
    magnitude = abs(value)
    if magnitude < 1.0:
        return 0
    return round(math.log(magnitude))
