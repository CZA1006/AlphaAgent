"""Market-pack registry and pre-migration reproducibility baselines."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alpha_harness.data.fingerprint import dataframe_fingerprint
from alpha_harness.data.synthetic import generate_price_panel
from alpha_harness.markets import (
    MarketPackNotFoundError,
    list_market_packs,
    load_market_pack,
)
from alpha_harness.regimes import STRICT_REGIME
from alpha_harness.schemas.experiment import PromotionTrail
from scripts.validate_strict import _build_eval_request


def test_builtin_market_packs_are_typed_and_deterministically_listed() -> None:
    assert list_market_packs() == ("hk_ipo", "us_equities_daily")

    hk_pack = load_market_pack("hk_ipo")
    us_pack = load_market_pack("us_equities_daily")

    assert hk_pack.data.loader == "bigquery"
    assert hk_pack.data.project == "bloomberg-database-0629"
    assert "ofi" in hk_pack.dsl_fields
    assert len(hk_pack.director_topics) == 4
    assert us_pack.data.loader == "parquet"
    assert us_pack.dsl_fields == frozenset()


def test_market_pack_registry_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(MarketPackNotFoundError, match="not found"):
        load_market_pack("missing", config_dir=tmp_path)

    payload = json.loads(Path("configs/markets/us_equities_daily.json").read_text())
    payload["market_id"] = "different_market"
    (tmp_path / "requested_market.json").write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="id mismatch"):
        load_market_pack("requested_market", config_dir=tmp_path)


def test_pre_market_pack_fingerprint_and_trail_baseline() -> None:
    """Pin quantitative identities before any hardcoded default is moved."""
    panel = generate_price_panel(symbols=["AAA", "BBB", "CCC"], n_days=40, seed=7)
    request = _build_eval_request(
        regime=STRICT_REGIME,
        factor_id="baseline",
        df=panel,
        n_proposals_in_session=18,
    )
    trail = PromotionTrail.from_inputs(
        evaluation_request=request,
        judge_thresholds=STRICT_REGIME.judge_thresholds(),
        walk_forward={
            "n_folds": STRICT_REGIME.n_folds,
            "fold_size_days": STRICT_REGIME.fold_size_days,
            "step_days": STRICT_REGIME.step_days,
            "embargo_days": STRICT_REGIME.embargo_days,
        },
    )

    assert dataframe_fingerprint(panel) == (
        "b6913c275483edadb84458fb79ea66ca89d23154f77c580240c6132aab3cf56e"
    )
    assert trail.trail_id == "5d7dda10ee5b7b90"
