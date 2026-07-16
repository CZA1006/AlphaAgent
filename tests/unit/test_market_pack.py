"""Market-pack registry and pre-migration reproducibility baselines."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from alpha_harness.data.fingerprint import dataframe_fingerprint
from alpha_harness.data.loader_factory import create_equities_loader
from alpha_harness.data.models import DataRequest
from alpha_harness.data.synthetic import generate_price_panel
from alpha_harness.data.tick_materialization import render_sql_template
from alpha_harness.evaluators.promotion_judge import PromotionJudge
from alpha_harness.evaluators.signal_quality import SignalQualityEvaluator
from alpha_harness.factors.compiler import FactorDslCompiler
from alpha_harness.llm import MockLLMClient
from alpha_harness.markets import (
    MarketPackNotFoundError,
    list_market_packs,
    load_market_pack,
)
from alpha_harness.proposer import HypothesisProposer, ProposalRequest
from alpha_harness.regimes import STRICT_REGIME
from alpha_harness.schemas.evaluation import EvaluationRequest
from alpha_harness.schemas.experiment import PromotionTrail
from alpha_harness.service import AlphaHarnessService
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


def test_all_pack_sql_templates_render_without_market_literals() -> None:
    pack = load_market_pack("hk_ipo")
    for path_value in pack.sql_templates.values():
        template = Path(path_value).read_text(encoding="utf-8")
        rendered = render_sql_template(
            template,
            project="configured-project",
            dataset="configured_dataset",
            end_date=date(2026, 6, 26),
        )
        assert "{{" not in rendered
        assert "configured-project.configured_dataset" in rendered


def test_third_pack_runs_mock_llm_evaluation_cycle(tmp_path: Path) -> None:
    config_dir = tmp_path / "configs" / "markets"
    data_dir = tmp_path / "synthetic_parquet"
    config_dir.mkdir(parents=True)
    data_dir.mkdir()

    payload = json.loads(Path("configs/markets/us_equities_daily.json").read_text())
    payload.update(
        {
            "market_id": "synthetic_smoke",
            "display_name": "Synthetic Smoke",
            "universe_file": str(tmp_path / "universe.txt"),
            "data": {"loader": "parquet", "base_path": str(data_dir)},
            "extra_dsl_fields": {"custom_signal": "Synthetic test signal."},
        }
    )
    (config_dir / "synthetic_smoke.json").write_text(json.dumps(payload), encoding="utf-8")
    pack = load_market_pack("synthetic_smoke", config_dir=config_dir)

    symbols = [f"S{i:02d}" for i in range(12)]
    panel = generate_price_panel(symbols=symbols, n_days=50, seed=19)
    panel["custom_signal"] = panel.groupby("symbol")["close"].pct_change().fillna(0.0)
    for symbol, rows in panel.groupby("symbol"):
        rows.drop(columns="symbol").to_parquet(data_dir / f"{symbol}.parquet", index=False)

    start = panel["timestamp"].min().date()
    end = panel["timestamp"].max().date()
    loader = create_equities_loader(market_pack=pack)
    loaded, metadata = loader.load_bars(DataRequest(symbols=symbols, start=start, end=end))
    assert metadata.symbols_returned == len(symbols)
    assert "custom_signal" in loaded.columns

    compiler = FactorDslCompiler(extra_fields=pack.dsl_fields)
    proposer = HypothesisProposer(
        MockLLMClient(
            responses=[
                json.dumps(
                    {
                        "proposals": [
                            {
                                "expression": "rank(custom_signal)",
                                "rationale": "exercise a pack-defined field",
                            }
                        ]
                    }
                )
            ]
        ),
        compiler=compiler,
        max_rounds=1,
    )
    proposal = proposer.propose(ProposalRequest(theme="synthetic smoke", n_candidates=1))
    hypothesis = proposal.to_hypotheses()[0]
    service = AlphaHarnessService(
        compiler=compiler,
        evaluator=SignalQualityEvaluator(loaded),
        judge=PromotionJudge(),
    )
    record = service.run_research_cycle(
        hypothesis,
        EvaluationRequest(
            factor_id="synthetic_smoke",
            universe_id="synthetic_smoke",
            eval_start=start,
            eval_end=end,
        ),
    )

    assert record.factor.expression == "rank(custom_signal)"
    assert record.factor.operator_tree is not None
    assert record.evaluation.n_assets == len(symbols)
