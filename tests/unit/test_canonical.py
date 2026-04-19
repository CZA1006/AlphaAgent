"""Tests for canonical AST normalization and structural similarity.

Covers:
    * Whitespace/formatting equivalence
    * Commutative-operator order insensitivity
    * Double-negation and literal-negation folding
    * Subtraction rewritten as addition with a negated operand
    * Near-duplicate scoring (window size variations)
    * Clearly-different factors score low
"""

from __future__ import annotations

from alpha_harness.factors.canonical import (
    ast_similarity,
    canon_similarity,
    canonicalize,
)
from alpha_harness.factors.dsl_parser import parse_expression


def _canon(expr: str) -> tuple:
    return canonicalize(parse_expression(expr))


# ── Canonical equality ───────────────────────────────────────────────────────


class TestCanonicalEquality:
    def test_whitespace_insensitive(self) -> None:
        a = _canon("ts_mean(close, 20)")
        b = _canon(" ts_mean( close , 20 ) ")
        assert a == b

    def test_addition_commutative(self) -> None:
        assert _canon("close + volume") == _canon("volume + close")

    def test_multiplication_commutative(self) -> None:
        assert _canon("close * volume") == _canon("volume * close")

    def test_division_non_commutative(self) -> None:
        assert _canon("close / volume") != _canon("volume / close")

    def test_addition_associative(self) -> None:
        # (a + b) + c  ==  a + (b + c)  ==  c + a + b
        a = _canon("(close + volume) + open")
        b = _canon("close + (volume + open)")
        c = _canon("open + close + volume")
        assert a == b == c

    def test_multiplication_associative(self) -> None:
        a = _canon("(close * volume) * open")
        b = _canon("close * (volume * open)")
        assert a == b

    def test_subtraction_as_addition_of_negation(self) -> None:
        # a - b  ≡  a + (-b) — but a - b ≠ b - a
        assert _canon("close - volume") != _canon("volume - close")
        # Canonical rewrites the RHS through negation, so order still matters.
        expected = _canon("close + (-volume)")
        assert _canon("close - volume") == expected

    def test_double_negation_folds(self) -> None:
        assert _canon("--close") == _canon("close")

    def test_negation_of_literal_folds(self) -> None:
        # -5 should become a single numeric node with value -5.0
        canon = _canon("-5")
        assert canon == ("num", -5.0)

    def test_identifier_case_normalized(self) -> None:
        # Parser accepts lowercase fields, but canonical form lowers any case
        # we may receive via hand-built ASTs.
        handbuilt = {"type": "field", "name": "CLOSE"}
        assert canonicalize(handbuilt) == ("field", "close")

    def test_function_name_case_normalized(self) -> None:
        handbuilt = {
            "type": "function",
            "name": "TS_MEAN",
            "args": [
                {"type": "field", "name": "close"},
                {"type": "number", "value": 20.0},
            ],
        }
        assert canonicalize(handbuilt) == _canon("ts_mean(close, 20)")

    def test_canonical_form_is_hashable(self) -> None:
        # Nested tuples are hashable — useful as dict keys for deduping.
        seen = {_canon("ts_mean(close, 20)")}
        assert _canon(" ts_mean(close , 20) ") in seen


# ── Structural similarity ────────────────────────────────────────────────────


class TestStructuralSimilarity:
    def test_identical_expressions_score_one(self) -> None:
        score = ast_similarity(
            parse_expression("rank(close)"),
            parse_expression("rank(close)"),
        )
        assert score == 1.0

    def test_whitespace_variants_score_one(self) -> None:
        score = ast_similarity(
            parse_expression("ts_mean(close, 20)"),
            parse_expression("  ts_mean( close ,  20 )"),
        )
        assert score == 1.0

    def test_commutative_variants_score_one(self) -> None:
        score = ast_similarity(
            parse_expression("close + volume"),
            parse_expression("volume + close"),
        )
        assert score == 1.0

    def test_near_duplicate_window_high(self) -> None:
        # ts_mean(close, 20) vs ts_mean(close, 21) — structurally identical.
        score = ast_similarity(
            parse_expression("ts_mean(close, 20)"),
            parse_expression("ts_mean(close, 21)"),
        )
        assert score >= 0.85

    def test_different_functions_score_low(self) -> None:
        score = ast_similarity(
            parse_expression("rank(close)"),
            parse_expression("ts_std(volume, 10)"),
        )
        # No overlapping features at all → Jaccard = 0.
        assert score < 0.5

    def test_shared_field_partial_overlap(self) -> None:
        # Both reference `close` but through different operators.
        score = ast_similarity(
            parse_expression("rank(close)"),
            parse_expression("ts_mean(close, 20)"),
        )
        # Feature A = {(fn,rank), (field,close)}          size 2
        # Feature B = {(fn,ts_mean), (field,close), (num,)} size 3
        # Intersection = {(field,close)} = 1; union size = 4 → 0.25
        assert 0.0 < score < 0.85

    def test_similarity_is_symmetric(self) -> None:
        a = parse_expression("rank(ts_mean(close, 20))")
        b = parse_expression("ts_mean(volume, 10)")
        assert ast_similarity(a, b) == ast_similarity(b, a)

    def test_canon_similarity_accepts_precomputed_nodes(self) -> None:
        ca = _canon("ts_mean(close, 20)")
        cb = _canon("ts_mean(close, 21)")
        assert canon_similarity(ca, cb) >= 0.85

    def test_window_halve_is_below_refinement_threshold(self) -> None:
        """Window-halve mutation must not score as an exact duplicate.

        The refinement novelty gate sits at 0.95. If halve (20 → 10) scored
        ≥ 0.95, every window-scaling mutation would be silently dropped.
        """
        score = ast_similarity(
            parse_expression("ts_mean(close, 20)"),
            parse_expression("ts_mean(close, 10)"),
        )
        assert score < 0.95

    def test_window_double_is_below_refinement_threshold(self) -> None:
        score = ast_similarity(
            parse_expression("ts_mean(close, 20)"),
            parse_expression("ts_mean(close, 40)"),
        )
        assert score < 0.95

    def test_same_features_different_tree_shape_score_low(self) -> None:
        """``rank(ts_mean(close, 20))`` and ``ts_mean(rank(close), 20)``
        share every identifier but differ in nesting — they must not
        collapse to a duplicate."""
        score = ast_similarity(
            parse_expression("rank(ts_mean(close, 20))"),
            parse_expression("ts_mean(rank(close), 20)"),
        )
        assert score < 0.85
