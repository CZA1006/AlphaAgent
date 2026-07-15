"""Round 9 A.2 — composite promotion is idempotent on recipe + regime."""

from __future__ import annotations

import json
from argparse import Namespace
from datetime import date
from pathlib import Path

import pytest

from alpha_harness.combination import CombinationMethod, CombinationRecipe
from alpha_harness.evaluators.persistence import FactorSelectionStrategy
from alpha_harness.regimes import StrictRegime
from alpha_harness.schemas.evaluation import EvaluationBundle, EvaluationProfile, EvaluationRequest
from alpha_harness.schemas.experiment import ExperimentDecision, PromotionTrail
from scripts.combine_factors import (
    _build_eval_request,
    _judge_basket,
    _promote_basket,
    _resolve_expressions,
    _select_component_indices,
)


class _Args:
    """Minimal argparse.Namespace stand-in."""

    def __init__(self, *, promoted_dir: Path, trail_dir: Path) -> None:
        self.promoted_dir = str(promoted_dir)
        self.trail_dir = str(trail_dir)


def _make_trail(*, neutralize: str = "none") -> PromotionTrail:
    """Build a real PromotionTrail with a real trail_id hash."""

    class _Stub:
        label = None
        holdout = None
        cost_bps = 0.0

        def __init__(self, neutralize: str) -> None:
            self.sector_map: dict = {}
            self.neutralize = neutralize

    stub = _Stub(neutralize)
    return PromotionTrail.from_inputs(
        evaluation_request=stub,
        judge_thresholds={},
        walk_forward={},
    )


def _make_bundle() -> EvaluationBundle:
    return EvaluationBundle(ic=0.04, rank_ic=0.05, n_periods=200, n_assets=50)


def _selection_bundle(rank_ic: float, folds: list[float]) -> EvaluationBundle:
    return EvaluationBundle(
        rank_ic=rank_ic,
        metadata={"per_fold": [{"rank_ic": value} for value in folds]},
    )


def test_persistence_selection_is_opt_in_and_top_k() -> None:
    bundles = [
        _selection_bundle(0.20, [0.30, -0.05, 0.25, 0.06]),
        _selection_bundle(0.04, [0.04, 0.03, 0.05, 0.02]),
        _selection_bundle(0.03, [0.03, 0.02, 0.01, 0.02]),
    ]
    assert _select_component_indices(
        strategy=FactorSelectionStrategy.INPUT_ORDER,
        top_k=2,
        bundles=bundles,
    ) == [0, 1]
    assert _select_component_indices(
        strategy=FactorSelectionStrategy.TRAIN_RANK_IC,
        top_k=2,
        bundles=bundles,
    ) == [0, 1]
    assert _select_component_indices(
        strategy=FactorSelectionStrategy.PERSISTENCE,
        top_k=2,
        bundles=bundles,
    ) == [1, 2]


def test_nondefault_selection_requires_top_k() -> None:
    with pytest.raises(ValueError, match="--top-k is required"):
        _select_component_indices(
            strategy=FactorSelectionStrategy.PERSISTENCE,
            top_k=None,
            bundles=[_selection_bundle(0.02, [0.01, 0.02])],
        )


def _request() -> EvaluationRequest:
    return EvaluationRequest(
        factor_id="basket",
        universe_id="test",
        eval_start=date(2026, 1, 1),
        eval_end=date(2026, 6, 30),
        profile=EvaluationProfile(
            thresholds={"ic": 0.02, "rank_ic": 0.03, "quantile_spread": 0.005},
            min_periods=60,
            min_assets=10,
        ),
    )


def _recipe() -> CombinationRecipe:
    return CombinationRecipe.build(
        method=CombinationMethod.EQUAL_WEIGHT,
        components=["rank(close)", "rank(volume)"],
    )


def test_judge_basket_uses_full_profile_not_only_ic_rank_ic() -> None:
    bundle = EvaluationBundle(
        ic=0.04,
        rank_ic=0.05,
        quantile_spread=0.001,
        n_periods=200,
        n_assets=50,
    )

    detail = _judge_basket(
        recipe=_recipe(),
        basket_bundle=bundle,
        request=_request(),
        judge_thresholds={},
    )

    assert detail.decision == ExperimentDecision.REJECT
    assert detail.failure is not None
    assert "quantile_spread" in detail.failure.detail


def test_judge_basket_rejects_holdout_sign_flip() -> None:
    bundle = EvaluationBundle(
        ic=0.04,
        rank_ic=0.05,
        quantile_spread=0.02,
        n_periods=200,
        n_assets=50,
        metadata={"holdout": {"rank_ic": -0.01}},
    )

    detail = _judge_basket(
        recipe=_recipe(),
        basket_bundle=bundle,
        request=_request(),
        judge_thresholds={},
    )

    assert detail.decision == ExperimentDecision.REJECT
    assert detail.failure is not None
    assert "holdout rank_ic" in detail.failure.detail


def test_judge_basket_applies_candidate_family_pressure() -> None:
    bundle = EvaluationBundle(
        ic=0.04,
        rank_ic=0.04,
        quantile_spread=0.02,
        n_periods=200,
        n_assets=50,
    )
    request = _request().model_copy(update={"n_proposals_in_session": 7})

    detail = _judge_basket(
        recipe=_recipe(),
        basket_bundle=bundle,
        request=request,
        judge_thresholds={"multiple_testing_familywise_alpha": 0.05},
    )

    assert detail.decision == ExperimentDecision.REJECT
    assert detail.failure is not None
    assert "rank_ic" in detail.failure.detail
    assert "n_proposals_in_session=7" in detail.failure.detail


def test_combination_request_records_cost_and_candidate_family() -> None:
    import pandas as pd

    request = _build_eval_request(
        df=pd.DataFrame({"timestamp": ["2026-01-01", "2026-06-30"]}),
        regime=StrictRegime(cost_bps=15.0),
        factor_id="basket",
        universe_id="hk_ipo",
        n_proposals_in_session=7,
    )

    assert request.cost_bps == 15.0
    assert request.n_proposals_in_session == 7


def test_report_expression_source_preserves_provenance(tmp_path: Path) -> None:
    report_path = tmp_path / "source.json"
    report_path.write_text(
        json.dumps(
            {
                "cycle_id": "source-cycle",
                "data_fingerprint": "source-panel",
                "factors": [
                    {"expression": "rank(close)", "ic": 0.1, "rank_ic": 0.1},
                    {"expression": "rank(volume)", "ic": 0.1, "rank_ic": 0.1},
                ],
            }
        ),
        encoding="utf-8",
    )
    args = Namespace(
        expr=[],
        expressions_file=None,
        from_validation_report=str(report_path),
        filter_min_ic=None,
        filter_passes_ic=True,
        filter_passes_rank_ic=True,
        promote=False,
    )

    resolved = _resolve_expressions(args)

    assert resolved.expressions == ["rank(close)", "rank(volume)"]
    assert resolved.source_cycle_ids == ["source-cycle"]
    assert resolved.source_data_fingerprints == ["source-panel"]


def test_promotion_source_requires_fingerprint(tmp_path: Path) -> None:
    report_path = tmp_path / "legacy.json"
    report_path.write_text(
        json.dumps(
            {
                "cycle_id": "legacy",
                "factors": [
                    {"expression": "rank(close)"},
                    {"expression": "rank(volume)"},
                ],
            }
        ),
        encoding="utf-8",
    )
    args = Namespace(
        expr=[],
        expressions_file=None,
        from_validation_report=str(report_path),
        filter_min_ic=None,
        filter_passes_ic=False,
        filter_passes_rank_ic=False,
        promote=True,
    )

    with pytest.raises(ValueError, match="must include cycle_id and data_fingerprint"):
        _resolve_expressions(args)


def test_promote_basket_writes_deterministic_factor_id(tmp_path: Path) -> None:
    """factor_id encodes recipe_id + trail prefix so re-promoting the same
    recipe under the same regime collapses to one artifact, not many.
    """
    promoted_dir = tmp_path / "promoted"
    trail_dir = tmp_path / "trails"
    args = _Args(promoted_dir=promoted_dir, trail_dir=trail_dir)
    recipe = CombinationRecipe.build(
        method=CombinationMethod.EQUAL_WEIGHT,
        components=["rank(close)", "rank(volume)"],
    )
    trail = _make_trail()

    p1 = _promote_basket(
        args=args,
        recipe=recipe,
        basket_bundle=_make_bundle(),
        regime_trail=trail,
        cycle_id="cycle-1",
    )
    p2 = _promote_basket(
        args=args,
        recipe=recipe,
        basket_bundle=_make_bundle(),
        regime_trail=trail,  # same regime → same trail_id
        cycle_id="cycle-2",  # different cycle
    )
    assert p1 is not None and p2 is not None
    assert p1 == p2  # same file overwritten
    # Only one artifact file on disk (plus _index.jsonl).
    artifacts = sorted(f.name for f in promoted_dir.glob("*.json"))
    assert artifacts == [p1.name]


def test_promote_basket_splits_artifact_per_regime(tmp_path: Path) -> None:
    """Same recipe under two different regimes ⇒ two distinct artifacts."""
    promoted_dir = tmp_path / "promoted"
    trail_dir = tmp_path / "trails"
    args = _Args(promoted_dir=promoted_dir, trail_dir=trail_dir)
    recipe = CombinationRecipe.build(
        method=CombinationMethod.EQUAL_WEIGHT,
        components=["rank(close)", "rank(volume)"],
    )
    p1 = _promote_basket(
        args=args,
        recipe=recipe,
        basket_bundle=_make_bundle(),
        regime_trail=_make_trail(neutralize="none"),
        cycle_id="cycle-1",
    )
    p2 = _promote_basket(
        args=args,
        recipe=recipe,
        basket_bundle=_make_bundle(),
        regime_trail=_make_trail(neutralize="sector"),
        cycle_id="cycle-2",
    )
    assert p1 is not None and p2 is not None
    assert p1 != p2


def test_promote_basket_payload_includes_composite_recipe(tmp_path: Path) -> None:
    promoted_dir = tmp_path / "promoted"
    trail_dir = tmp_path / "trails"
    args = _Args(promoted_dir=promoted_dir, trail_dir=trail_dir)
    recipe = CombinationRecipe.build(
        method=CombinationMethod.ZSCORE_AVERAGE,
        components=["rank(close)", "rank(high)"],
    )
    path = _promote_basket(
        args=args,
        recipe=recipe,
        basket_bundle=_make_bundle(),
        regime_trail=_make_trail(),
        cycle_id="cycle-1",
    )
    assert path is not None
    payload = json.loads(path.read_text())
    assert payload["composite_recipe"]["method"] == "zscore_average"
    assert payload["composite_recipe"]["recipe_id"] == recipe.recipe_id
    assert payload["decision"] == "promote_candidate"
