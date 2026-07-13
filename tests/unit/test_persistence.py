"""Persistence scoring — the selection fix from the HK IPO case study.

The failure being pinned: ordering candidates by train-window mean IC
promoted a hot-but-flippy factor over persistently-positive ones.  The
persistence sort key must prefer "modest but always positive" over
"big mean from one hot fold".
"""

from __future__ import annotations

import math

from alpha_harness.evaluators.persistence import (
    PersistenceScore,
    rank_by_persistence,
    score_from_folds,
    score_from_walk_forward,
)

# ── score_from_folds ────────────────────────────────────────────────────────


def test_score_basic_fields() -> None:
    s = score_from_folds([0.02, 0.04, -0.01, 0.03])
    assert s is not None
    assert s.n_folds == 4
    assert s.fraction_positive == 0.75
    assert math.isclose(s.mean_rank_ic, 0.02)


def test_score_ignores_missing_folds() -> None:
    s = score_from_folds([0.02, None, float("nan"), 0.04])
    assert s is not None
    assert s.n_folds == 2
    assert s.fraction_positive == 1.0


def test_score_empty_is_none() -> None:
    assert score_from_folds([]) is None
    assert score_from_folds([None, float("nan")]) is None


def test_zero_std_stability_is_signed_infinity() -> None:
    pos = score_from_folds([0.03, 0.03])
    neg = score_from_folds([-0.03, -0.03])
    assert pos is not None and pos.stability == math.inf
    assert neg is not None and neg.stability == -math.inf


# ── The case-study ordering property ────────────────────────────────────────


def test_persistent_modest_beats_hot_but_flippy() -> None:
    # "high-low - rel_spread" shape: huge mean carried by hot folds, one flip.
    hot_flippy = score_from_folds([0.30, 0.25, -0.05, 0.06])
    # OFI shape: modest but positive in every fold.
    persistent = score_from_folds([0.04, 0.03, 0.05, 0.02])
    assert hot_flippy is not None and persistent is not None
    assert hot_flippy.mean_rank_ic > persistent.mean_rank_ic  # the trap
    assert persistent.sort_key > hot_flippy.sort_key  # the fix


def test_rank_by_persistence_orders_and_appends_unscored() -> None:
    a = ("A", score_from_folds([0.30, -0.05, 0.25, 0.06]))
    b = ("B", score_from_folds([0.04, 0.03, 0.05, 0.02]))
    c = ("C", None)
    assert rank_by_persistence([a, c, b]) == ["B", "A", "C"]


# ── score_from_walk_forward ─────────────────────────────────────────────────


def test_walk_forward_prefers_per_fold_payload() -> None:
    md = {
        "per_fold": [{"rank_ic": 0.02}, {"rank_ic": 0.04}, {"rank_ic": -0.01}],
        "walk_forward": {
            "n_folds": 3,
            "fraction_positive_rank_ic": 0.0,  # deliberately wrong summary
            "mean_rank_ic": 0.0,
            "std_rank_ic": 0.0,
        },
    }
    s = score_from_walk_forward(md)
    assert s is not None
    assert s.fraction_positive == 2 / 3


def test_walk_forward_summary_fallback() -> None:
    md = {
        "walk_forward": {
            "n_folds": 4,
            "fraction_positive_rank_ic": 0.75,
            "mean_rank_ic": 0.02,
            "std_rank_ic": 0.01,
        },
    }
    s = score_from_walk_forward(md)
    assert s == PersistenceScore(4, 0.75, 0.02, 0.01)


def test_walk_forward_single_fold_is_unranked() -> None:
    md = {"walk_forward": {"n_folds": 1, "skipped_reason": "span_too_short"}}
    assert score_from_walk_forward(md) is None
    assert score_from_walk_forward({}) is None
