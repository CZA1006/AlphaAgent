"""Round 8 Phase A — CombinationReport schema + writer + recipe id tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from alpha_harness.combination import CombinationMethod
from alpha_harness.evaluators.persistence import FactorSelectionStrategy
from alpha_harness.reports.combination import (
    CombinationReportWriter,
    build_combination_report,
    recipe_id_for,
)
from alpha_harness.reports.combination import (
    read_index as read_combination_index,
)
from alpha_harness.reports.validation import FactorThumbnail

# ── recipe_id_for ──────────────────────────────────────────────────────────


def test_recipe_id_is_invariant_to_component_order() -> None:
    """Permuted components must collapse to the same recipe_id.

    Otherwise the proposer could "rediscover" the same basket under a
    permuted order and the novelty check would let it through.
    """
    a = recipe_id_for(
        CombinationMethod.EQUAL_WEIGHT,
        ["rank(ts_mean(close, 20))", "rank(ts_std(volume, 10))"],
    )
    b = recipe_id_for(
        CombinationMethod.EQUAL_WEIGHT,
        ["rank(ts_std(volume, 10))", "rank(ts_mean(close, 20))"],
    )
    assert a == b


def test_recipe_id_changes_with_method() -> None:
    components = ["rank(close)", "rank(volume)"]
    a = recipe_id_for(CombinationMethod.EQUAL_WEIGHT, components)
    b = recipe_id_for(CombinationMethod.ZSCORE_AVERAGE, components)
    assert a != b


def test_recipe_id_changes_with_components() -> None:
    a = recipe_id_for(
        CombinationMethod.EQUAL_WEIGHT,
        ["rank(close)", "rank(volume)"],
    )
    b = recipe_id_for(
        CombinationMethod.EQUAL_WEIGHT,
        ["rank(close)", "rank(high)"],
    )
    assert a != b


def test_recipe_id_collapses_commutative_trivia() -> None:
    """``a + b`` and ``b + a`` share a canonical AST, so the recipe id matches."""
    a = recipe_id_for(
        CombinationMethod.EQUAL_WEIGHT,
        ["rank(close + volume)"],
    )
    b = recipe_id_for(
        CombinationMethod.EQUAL_WEIGHT,
        ["rank(volume + close)"],
    )
    assert a == b


def test_recipe_id_rejects_unparseable_expression() -> None:
    with pytest.raises(ValueError):
        recipe_id_for(
            CombinationMethod.EQUAL_WEIGHT,
            ["this is not a valid expression !!"],
        )


# ── build_combination_report + writer round-trip ───────────────────────────


def _thumb(name: str, ic: float, ric: float, decision: str = "component") -> FactorThumbnail:
    return FactorThumbnail(
        factor_id=name,
        expression=f"rank({name})",
        decision=decision,
        ic=ic,
        rank_ic=ric,
        quantile_spread=0.01,
        net_quantile_spread=0.005,
        sharpe=0.5,
        turnover=0.2,
    )


def test_build_and_write_round_trip(tmp_path: Path) -> None:
    started = datetime.now(UTC)
    report = build_combination_report(
        cycle_id="cycle-abc",
        regime_trail_id="trail-deadbeef",
        universe_id="sp50",
        data_fingerprint="panel-abc",
        source_validation_cycle_ids=["source-1"],
        source_data_fingerprints=["source-panel"],
        cost_bps=15.0,
        n_proposals_in_session=7,
        ic_threshold_multiplier=1.4895,
        started_at=started,
        method=CombinationMethod.EQUAL_WEIGHT,
        components=["rank(close)", "rank(volume)"],
        component_factor_ids=["f1", "f2"],
        basket_metrics=_thumb("basket", ic=0.03, ric=0.04, decision="basket"),
        component_metrics=[_thumb("f1", 0.01, 0.02), _thumb("f2", 0.015, 0.025)],
        avg_pairwise_rank_corr=0.25,
        passes_regime=True,
        selection_strategy=FactorSelectionStrategy.PERSISTENCE,
        selection_score_version="rank_ic_sign_stability_v1",
        selection_top_k=2,
        selection_candidate_count=4,
    )

    writer = CombinationReportWriter(tmp_path)
    out_path = writer.write(report)
    assert out_path is not None and out_path.is_file()
    assert out_path.name == "cycle-abc.json"

    on_disk = json.loads(out_path.read_text(encoding="utf-8"))
    assert on_disk["cycle_id"] == "cycle-abc"
    assert on_disk["recipe"]["method"] == "equal_weight"
    assert on_disk["recipe"]["recipe_id"] == report.recipe.recipe_id
    assert len(on_disk["component_metrics"]) == 2
    assert on_disk["passes_regime"] is True
    assert on_disk["selection_strategy"] == "persistence"
    assert on_disk["selection_score_version"] == "rank_ic_sign_stability_v1"
    assert on_disk["selection_top_k"] == 2
    assert on_disk["selection_candidate_count"] == 4
    assert on_disk["data_fingerprint"] == "panel-abc"
    assert on_disk["source_validation_cycle_ids"] == ["source-1"]
    assert on_disk["source_data_fingerprints"] == ["source-panel"]
    assert on_disk["cost_bps"] == 15.0
    assert on_disk["n_proposals_in_session"] == 7
    assert on_disk["ic_threshold_multiplier"] == pytest.approx(1.4895)

    rows = read_combination_index(tmp_path)
    assert len(rows) == 1
    assert rows[0]["cycle_id"] == "cycle-abc"
    assert rows[0]["recipe_id"] == report.recipe.recipe_id
    assert rows[0]["passes_regime"] is True
    assert rows[0]["selection_strategy"] == "persistence"
    assert rows[0]["data_fingerprint"] == "panel-abc"
    assert rows[0]["cost_bps"] == 15.0
    assert rows[0]["n_proposals_in_session"] == 7


def test_writer_upserts_same_cycle_id(tmp_path: Path) -> None:
    """Re-writing the same cycle_id replaces the row, doesn't duplicate it."""
    started = datetime.now(UTC)
    r1 = build_combination_report(
        cycle_id="cycle-xyz",
        regime_trail_id="trail-1",
        universe_id="",
        started_at=started,
        method=CombinationMethod.RANK_AGGREGATE,
        components=["rank(close)", "rank(volume)"],
        component_factor_ids=None,
        basket_metrics=_thumb("basket", 0.0, 0.0, "basket"),
        component_metrics=[],
        avg_pairwise_rank_corr=None,
        passes_regime=False,
    )
    r2 = build_combination_report(
        cycle_id="cycle-xyz",  # same id
        regime_trail_id="trail-2",
        universe_id="",
        started_at=started,
        method=CombinationMethod.EQUAL_WEIGHT,  # different method
        components=["rank(high)", "rank(low)"],
        component_factor_ids=None,
        basket_metrics=_thumb("basket", 0.05, 0.06, "basket"),
        component_metrics=[],
        avg_pairwise_rank_corr=None,
        passes_regime=True,
    )
    writer = CombinationReportWriter(tmp_path)
    writer.write(r1)
    writer.write(r2)
    rows = read_combination_index(tmp_path)
    assert len(rows) == 1
    assert rows[0]["regime_trail_id"] == "trail-2"
    assert rows[0]["passes_regime"] is True
