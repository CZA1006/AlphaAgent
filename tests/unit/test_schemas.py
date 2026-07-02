"""Smoke tests — verify all core schemas instantiate and serialize correctly."""

from datetime import date

from alpha_harness.schemas.evaluation import (
    EvaluationBundle,
    EvaluationProfile,
    EvaluationRequest,
    LabelDefinition,
    MetricName,
)
from alpha_harness.schemas.experiment import (
    ExperimentDecision,
    ExperimentRecord,
    FailureCategory,
    FailureRecord,
    ReproducibilityInfo,
)
from alpha_harness.schemas.factor import FactorSpec
from alpha_harness.schemas.hypothesis import AssetClass, Hypothesis, HypothesisStatus
from alpha_harness.schemas.regime import RegimeState
from alpha_harness.schemas.skill import Skill
from alpha_harness.schemas.universe import MembershipSource, UniverseSpec


def test_hypothesis_defaults():
    h = Hypothesis(text="momentum reversal in large caps")
    assert h.status == HypothesisStatus.DRAFT
    assert h.asset_class == AssetClass.US_EQUITY
    assert len(h.id) == 12
    assert h.created_at.tzinfo is not None


def test_hk_equity_is_first_class_asset_class():
    h = Hypothesis(text="HK IPO microstructure reversal", asset_class=AssetClass.HK_EQUITY)
    assert h.asset_class == AssetClass.HK_EQUITY
    assert h.model_dump()["asset_class"] == "hk_equity"


def test_factor_spec_defaults():
    f = FactorSpec(name="mom_20d", expression="rank(ts_mean(close, 20))")
    assert f.universe_id == ""
    assert f.operator_tree is None
    assert f.created_at.tzinfo is not None


def test_evaluation_bundle_partial():
    e = EvaluationBundle(ic=0.05, rank_ic=0.04)
    assert e.quantile_spread is None
    assert e.ic == 0.05
    assert e.computed_at.tzinfo is not None


def test_evaluation_bundle_passes_profile():
    profile = EvaluationProfile(
        required_metrics=[MetricName.IC, MetricName.RANK_IC],
        thresholds={"ic": 0.02, "rank_ic": 0.03},
    )
    passing = EvaluationBundle(ic=0.05, rank_ic=0.04)
    assert passing.passes_profile(profile)

    failing_value = EvaluationBundle(ic=0.01, rank_ic=0.04)
    assert not failing_value.passes_profile(profile)

    missing_metric = EvaluationBundle(ic=0.05)
    assert not missing_metric.passes_profile(profile)


def test_evaluation_request_defaults():
    req = EvaluationRequest(
        factor_id="abc",
        universe_id="u1",
        eval_start=date(2020, 1, 1),
        eval_end=date(2023, 12, 31),
    )
    assert req.label.forecast_horizon_bars == 5
    assert req.label.lag_bars == 1
    assert req.profile.n_quantiles == 5
    assert req.rebalance_frequency == "daily"


def test_label_definition():
    label = LabelDefinition(forecast_horizon_bars=10, lag_bars=2, return_type="log")
    assert label.forecast_horizon_bars == 10
    assert label.return_type == "log"


def test_universe_spec():
    u = UniverseSpec(
        name="sp500_test",
        asset_class="us_equity",
        membership_source=MembershipSource.STATIC_LIST,
        symbols=["AAPL", "MSFT", "GOOG"],
        as_of_date=date(2023, 1, 1),
    )
    assert len(u.symbols) == 3
    assert u.include_delisted is False
    assert u.exchange is None
    assert u.created_at.tzinfo is not None


def test_universe_spec_crypto():
    u = UniverseSpec(
        name="binance_btc_eth",
        asset_class="crypto",
        membership_source=MembershipSource.EXCHANGE_LISTED,
        exchange="binance",
        symbols=["BTC/USDT", "ETH/USDT"],
    )
    assert u.exchange == "binance"


def test_experiment_record_round_trip():
    h = Hypothesis(text="test")
    f = FactorSpec(name="test_factor", expression="close")
    ev = EvaluationBundle(ic=0.03)
    failure = FailureRecord(
        category=FailureCategory.WEAK_SIGNAL,
        detail="IC below threshold",
    )
    rec = ExperimentRecord(
        hypothesis=h,
        factor=f,
        evaluation=ev,
        decision=ExperimentDecision.REJECT,
        failure=failure,
        reproducibility=ReproducibilityInfo(code_version="abc123"),
    )
    # Round-trip through JSON
    data = rec.model_dump_json()
    restored = ExperimentRecord.model_validate_json(data)
    assert restored.decision == ExperimentDecision.REJECT
    assert restored.hypothesis.text == "test"
    assert restored.failure is not None
    assert restored.failure.category == FailureCategory.WEAK_SIGNAL
    assert restored.reproducibility.code_version == "abc123"


def test_experiment_record_with_eval_request():
    h = Hypothesis(text="test")
    f = FactorSpec(name="test_factor", expression="close")
    ev = EvaluationBundle(ic=0.05)
    req = EvaluationRequest(
        factor_id=f.id,
        universe_id="u1",
        eval_start=date(2020, 1, 1),
        eval_end=date(2023, 12, 31),
    )
    rec = ExperimentRecord(
        hypothesis=h,
        factor=f,
        evaluation=ev,
        eval_request=req,
        decision=ExperimentDecision.PROMOTE_CANDIDATE,
    )
    assert rec.eval_request is not None
    assert rec.eval_request.label.forecast_horizon_bars == 5
    assert rec.created_at.tzinfo is not None


def test_failure_taxonomy():
    f = FailureRecord(category=FailureCategory.HIGH_TURNOVER, detail="turnover > 80%")
    assert f.category == FailureCategory.HIGH_TURNOVER
    assert "80%" in f.detail


def test_skill_defaults():
    s = Skill(name="mean_reversion_pattern", description="short-term mean reversion")
    assert not s.promoted
    assert s.source_experiment_ids == []
    assert s.created_at.tzinfo is not None


def test_regime_state():
    r = RegimeState(label="risk_off", features={"vix": 28.5, "spread": 1.2})
    assert r.label == "risk_off"
    assert r.features["vix"] == 28.5
    assert r.timestamp.tzinfo is not None
