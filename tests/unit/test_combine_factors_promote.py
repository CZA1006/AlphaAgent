"""Round 9 A.2 — composite promotion is idempotent on recipe + regime."""

from __future__ import annotations

import json
from pathlib import Path

from alpha_harness.combination import CombinationMethod, CombinationRecipe
from alpha_harness.schemas.evaluation import EvaluationBundle
from alpha_harness.schemas.experiment import PromotionTrail
from scripts.combine_factors import _promote_basket


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
