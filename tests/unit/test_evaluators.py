"""Tests for evaluators, promotion judge, and the research orchestrator.

Sections:
    - Forward return construction (build_forward_returns)
    - Metric computation (IC, RankIC, quantile spread)
    - Real SignalQualityEvaluator end-to-end
    - StubSignalQualityEvaluator (backward compat)
    - NoveltyEvaluator
    - PromotionJudge
    - ResearchOrchestrator smoke tests
"""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from alpha_harness.evaluators.novelty import NoveltyEvaluator
from alpha_harness.evaluators.promotion_judge import PromotionJudge
from alpha_harness.evaluators.signal_quality import (
    SignalQualityEvaluator,
    build_forward_returns,
    compute_mean_ic,
    compute_mean_rank_ic,
    compute_quantile_spread,
)
from alpha_harness.factors.compiler import FactorDslCompiler
from alpha_harness.orchestrator.research_loop import ResearchOrchestrator
from alpha_harness.registries.experiment import ExperimentRegistry
from alpha_harness.registries.hypothesis import HypothesisRegistry
from alpha_harness.schemas.evaluation import (
    EvaluationBundle,
    EvaluationProfile,
    EvaluationRequest,
    LabelDefinition,
)
from alpha_harness.schemas.experiment import (
    ExperimentDecision,
    FailureCategory,
)
from alpha_harness.schemas.factor import FactorSpec
from alpha_harness.schemas.hypothesis import Hypothesis, HypothesisStatus
from alpha_harness.service import AlphaHarnessService
from tests.helpers.stubs import StubSignalQualityEvaluator

# ── Shared fixtures ──────────────────────────────────────────────────────────


def _default_eval_request() -> EvaluationRequest:
    return EvaluationRequest(
        factor_id="placeholder",
        universe_id="test_universe",
        eval_start=date(2020, 1, 1),
        eval_end=date(2023, 12, 31),
    )


def _make_perfect_signal_panel() -> pd.DataFrame:
    """Panel where 'close' perfectly predicts forward-return rank ordering.

    5 symbols with deterministic exponential growth at rates [1%, 2%, 3%, 4%, 5%]
    per bar. At every cross-section:
      - close_A < close_B < close_C < close_D < close_E
      - fwd_ret_A < fwd_ret_B < fwd_ret_C < fwd_ret_D < fwd_ret_E
    So Spearman RankIC = 1.0 at every date, and Pearson IC > 0.9.
    """
    n_dates = 20
    timestamps = pd.date_range("2023-01-01", periods=n_dates, freq="D", tz="UTC")
    symbols = ["A", "B", "C", "D", "E"]
    growth_rates = [0.01, 0.02, 0.03, 0.04, 0.05]

    frames: list[pd.DataFrame] = []
    for sym, g in zip(symbols, growth_rates, strict=True):
        close = 100.0 * np.power(1 + g, np.arange(n_dates, dtype=float))
        frames.append(pd.DataFrame({
            "timestamp": timestamps,
            "symbol": sym,
            "close": close,
            "open": close,
            "high": close,
            "low": close,
            "volume": [1e6] * n_dates,
        }))

    return pd.concat(frames, ignore_index=True)


def _make_flat_panel() -> pd.DataFrame:
    """Panel where all symbols have identical growth -> IC is undefined.

    All symbols grow at the same rate, so cross-sectional forward returns
    are identical -> std(fwd) = 0 -> correlation undefined -> IC = None.
    """
    n_dates = 20
    timestamps = pd.date_range("2023-01-01", periods=n_dates, freq="D", tz="UTC")
    symbols = ["A", "B", "C", "D", "E"]

    frames: list[pd.DataFrame] = []
    for sym in symbols:
        # All symbols have the same growth rate
        close = 100.0 * np.power(1.02, np.arange(n_dates, dtype=float))
        frames.append(pd.DataFrame({
            "timestamp": timestamps,
            "symbol": sym,
            "close": close,
            "open": close,
            "high": close,
            "low": close,
            "volume": [1e6] * n_dates,
        }))

    return pd.concat(frames, ignore_index=True)


# ── Forward return construction ──────────────────────────────────────────────


class TestBuildForwardReturns:
    def test_single_symbol_simple(self) -> None:
        """Forward return with lag=1, horizon=2 on known prices.

        future_end   = close.shift(-3) -> [106, 108, 110, 112, NaN, NaN, NaN]
        future_start = close.shift(-1) -> [102, 104, 106, 108, 110, 112, NaN]
        fwd = future_end / future_start - 1
        """
        close = pd.Series([100.0, 102.0, 104.0, 106.0, 108.0, 110.0, 112.0])
        label = LabelDefinition(lag_bars=1, forecast_horizon_bars=2, return_type="simple")
        result = build_forward_returns(close, groups=None, label=label)

        # fwd[0] = close[3] / close[1] - 1 = 106/102 - 1
        assert result.iloc[0] == pytest.approx(106.0 / 102.0 - 1)
        # fwd[1] = close[4] / close[2] - 1 = 108/104 - 1
        assert result.iloc[1] == pytest.approx(108.0 / 104.0 - 1)
        # fwd[3] = close[6] / close[4] - 1 = 112/108 - 1
        assert result.iloc[3] == pytest.approx(112.0 / 108.0 - 1)
        # Last 3 values should be NaN (future_end unavailable)
        assert np.isnan(result.iloc[-1])
        assert np.isnan(result.iloc[-2])
        assert np.isnan(result.iloc[-3])

    def test_multi_symbol_grouped(self) -> None:
        """Forward returns should not leak across symbols.

        With lag=0, horizon=1: fwd[t] = close[t+1]/close[t] - 1
        Symbols are interleaved in the DataFrame but groupby ensures
        shifts happen within each symbol independently.
        """
        # Sorted by (symbol, timestamp) for correct groupby behavior
        close = pd.Series([100.0, 105.0, 110.0, 200.0, 210.0, 220.0])
        groups = pd.Series(["A", "A", "A", "B", "B", "B"])
        label = LabelDefinition(lag_bars=0, forecast_horizon_bars=1, return_type="simple")
        result = build_forward_returns(close, groups=groups, label=label)

        # Symbol A: close=[100, 105, 110] -> fwd[0]=105/100-1=0.05, fwd[1]=110/105-1
        assert result.iloc[0] == pytest.approx(0.05)
        assert result.iloc[1] == pytest.approx(110.0 / 105.0 - 1)
        assert np.isnan(result.iloc[2])  # last A row

        # Symbol B: close=[200, 210, 220] -> fwd[0]=210/200-1=0.05
        assert result.iloc[3] == pytest.approx(0.05)
        assert result.iloc[4] == pytest.approx(220.0 / 210.0 - 1)
        assert np.isnan(result.iloc[5])  # last B row

    def test_log_returns(self) -> None:
        """Log return type."""
        close = pd.Series([100.0, 110.0, 121.0])
        label = LabelDefinition(lag_bars=0, forecast_horizon_bars=1, return_type="log")
        result = build_forward_returns(close, groups=None, label=label)

        assert result.iloc[0] == pytest.approx(np.log(110.0 / 100.0))
        assert result.iloc[1] == pytest.approx(np.log(121.0 / 110.0))
        assert np.isnan(result.iloc[2])

    def test_nan_when_no_future(self) -> None:
        """With lag=1, horizon=5, the last 6 rows should be NaN."""
        close = pd.Series(range(10), dtype=float)
        label = LabelDefinition(lag_bars=1, forecast_horizon_bars=5)
        result = build_forward_returns(close, groups=None, label=label)

        # Valid: indices 0..3 (need close[t+6] and close[t+1])
        assert not np.isnan(result.iloc[0])
        assert not np.isnan(result.iloc[3])
        # NaN: indices 4..9
        assert np.isnan(result.iloc[4])
        assert np.isnan(result.iloc[9])


# ── Metric computation ───────────────────────────────────────────────────────


class TestMetricComputation:
    """Test pure metric functions on hand-crafted data."""

    def test_perfect_positive_ic(self) -> None:
        """When signal perfectly predicts returns, IC = 1.0."""
        signal = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        fwd = pd.Series([0.01, 0.02, 0.03, 0.04, 0.05])
        timestamps = pd.Series(["t1"] * 5)

        ic = compute_mean_ic(signal, fwd, timestamps)
        assert ic is not None
        assert ic == pytest.approx(1.0)

    def test_perfect_negative_ic(self) -> None:
        """When signal inversely predicts returns, IC = -1.0."""
        signal = pd.Series([5.0, 4.0, 3.0, 2.0, 1.0])
        fwd = pd.Series([0.01, 0.02, 0.03, 0.04, 0.05])
        timestamps = pd.Series(["t1"] * 5)

        ic = compute_mean_ic(signal, fwd, timestamps)
        assert ic is not None
        assert ic == pytest.approx(-1.0)

    def test_perfect_rank_ic(self) -> None:
        """Perfect monotone relationship -> RankIC = 1.0."""
        signal = pd.Series([10.0, 20.0, 30.0, 40.0, 50.0])
        fwd = pd.Series([0.01, 0.04, 0.09, 0.16, 0.25])  # nonlinear but monotone
        timestamps = pd.Series(["t1"] * 5)

        rank_ic = compute_mean_rank_ic(signal, fwd, timestamps)
        assert rank_ic is not None
        assert rank_ic == pytest.approx(1.0)

    def test_ic_averaged_across_dates(self) -> None:
        """IC is the mean of per-date ICs."""
        # Two dates: date 1 has IC=1.0, date 2 has IC=1.0
        signal = pd.Series([1.0, 2.0, 3.0, 10.0, 20.0, 30.0])
        fwd = pd.Series([0.1, 0.2, 0.3, 1.0, 2.0, 3.0])
        timestamps = pd.Series(["d1", "d1", "d1", "d2", "d2", "d2"])

        ic = compute_mean_ic(signal, fwd, timestamps)
        assert ic is not None
        assert ic == pytest.approx(1.0)

    def test_ic_too_few_assets_returns_none(self) -> None:
        """With fewer than min_obs=3 assets, IC should be None."""
        signal = pd.Series([1.0, 2.0])
        fwd = pd.Series([0.1, 0.2])
        timestamps = pd.Series(["t1", "t1"])

        ic = compute_mean_ic(signal, fwd, timestamps, min_obs=3)
        assert ic is None

    def test_ic_constant_signal_returns_none(self) -> None:
        """Constant signal -> std=0 -> skip -> None."""
        signal = pd.Series([5.0, 5.0, 5.0, 5.0, 5.0])
        fwd = pd.Series([0.01, 0.02, 0.03, 0.04, 0.05])
        timestamps = pd.Series(["t1"] * 5)

        ic = compute_mean_ic(signal, fwd, timestamps)
        assert ic is None

    def test_ic_nan_in_data(self) -> None:
        """NaN values are excluded before computation."""
        signal = pd.Series([1.0, np.nan, 3.0, 4.0, 5.0])
        fwd = pd.Series([0.01, 0.02, 0.03, np.nan, 0.05])
        timestamps = pd.Series(["t1"] * 5)

        # Only 3 valid rows: (1, 0.01), (3, 0.03), (5, 0.05) -> IC = 1.0
        ic = compute_mean_ic(signal, fwd, timestamps, min_obs=3)
        assert ic is not None
        assert ic == pytest.approx(1.0)

    def test_quantile_spread_positive(self) -> None:
        """Positive signal -> higher returns in top quantile."""
        # 10 assets at one timestamp
        signal = pd.Series(range(1, 11), dtype=float)
        # Returns proportional to signal
        fwd = pd.Series([0.01 * i for i in range(1, 11)])
        timestamps = pd.Series(["t1"] * 10)

        qs = compute_quantile_spread(signal, fwd, timestamps, n_quantiles=5)
        assert qs is not None
        assert qs > 0  # Top quantile has higher returns than bottom

    def test_quantile_spread_negative(self) -> None:
        """Inverse signal -> negative spread."""
        signal = pd.Series(range(10, 0, -1), dtype=float)
        fwd = pd.Series([0.01 * i for i in range(1, 11)])
        timestamps = pd.Series(["t1"] * 10)

        qs = compute_quantile_spread(signal, fwd, timestamps, n_quantiles=5)
        assert qs is not None
        assert qs < 0

    def test_quantile_spread_too_few_assets(self) -> None:
        """Fewer assets than quantiles -> None."""
        signal = pd.Series([1.0, 2.0, 3.0])
        fwd = pd.Series([0.01, 0.02, 0.03])
        timestamps = pd.Series(["t1"] * 3)

        qs = compute_quantile_spread(signal, fwd, timestamps, n_quantiles=5)
        assert qs is None


# ── Real SignalQualityEvaluator ──────────────────────────────────────────────


class TestSignalQualityEvaluator:
    def test_strong_signal_positive_ic(self) -> None:
        """With perfectly rank-ordered data, IC and RankIC should be positive."""
        panel = _make_perfect_signal_panel()
        evaluator = SignalQualityEvaluator(panel)
        factor = FactorSpec(name="close_signal", expression="close")
        request = EvaluationRequest(
            factor_id="test",
            universe_id="test",
            eval_start=date(2023, 1, 1),
            eval_end=date(2023, 1, 20),
        )

        bundle = evaluator.evaluate(factor, request)

        assert bundle.ic is not None
        assert bundle.ic > 0.5  # strongly positive
        assert bundle.rank_ic is not None
        assert bundle.rank_ic == pytest.approx(1.0)  # perfect rank ordering

    def test_flat_panel_no_signal(self) -> None:
        """When all symbols have identical growth, IC is None."""
        panel = _make_flat_panel()
        evaluator = SignalQualityEvaluator(panel)
        factor = FactorSpec(name="close_signal", expression="close")
        request = EvaluationRequest(
            factor_id="test",
            universe_id="test",
            eval_start=date(2023, 1, 1),
            eval_end=date(2023, 1, 20),
        )

        bundle = evaluator.evaluate(factor, request)

        # All forward returns identical -> IC undefined
        assert bundle.ic is None
        assert bundle.rank_ic is None

    def test_deterministic(self) -> None:
        """Same inputs produce identical results."""
        panel = _make_perfect_signal_panel()
        factor = FactorSpec(name="close_signal", expression="close")
        request = EvaluationRequest(
            factor_id="test",
            universe_id="test",
            eval_start=date(2023, 1, 1),
            eval_end=date(2023, 1, 20),
        )

        b1 = SignalQualityEvaluator(panel).evaluate(factor, request)
        b2 = SignalQualityEvaluator(panel).evaluate(factor, request)

        assert b1.ic == b2.ic
        assert b1.rank_ic == b2.rank_ic
        assert b1.quantile_spread == b2.quantile_spread

    def test_populates_metadata(self) -> None:
        """EvaluationBundle carries correct provenance."""
        panel = _make_perfect_signal_panel()
        evaluator = SignalQualityEvaluator(panel)
        factor = FactorSpec(name="close_signal", expression="close")
        request = EvaluationRequest(
            factor_id="test",
            universe_id="test",
            eval_start=date(2023, 1, 1),
            eval_end=date(2023, 1, 20),
        )

        bundle = evaluator.evaluate(factor, request)

        assert bundle.eval_start == date(2023, 1, 1)
        assert bundle.eval_end == date(2023, 1, 20)
        assert bundle.forecast_horizon_bars == 5
        assert bundle.n_assets == 5
        assert bundle.n_periods == 20
        assert bundle.metadata.get("evaluator") == "signal_quality"
        assert bundle.metadata.get("mode") == "real"

    def test_date_filtering(self) -> None:
        """Only data within eval window is used."""
        panel = _make_perfect_signal_panel()
        evaluator = SignalQualityEvaluator(panel)
        factor = FactorSpec(name="close_signal", expression="close")

        # Narrow window: only first 10 days
        request = EvaluationRequest(
            factor_id="test",
            universe_id="test",
            eval_start=date(2023, 1, 1),
            eval_end=date(2023, 1, 10),
        )
        bundle = evaluator.evaluate(factor, request)
        assert bundle.n_periods == 10

    def test_profile_passes_strong_signal(self) -> None:
        """Strong signal should pass the default evaluation profile."""
        panel = _make_perfect_signal_panel()
        evaluator = SignalQualityEvaluator(panel)
        factor = FactorSpec(name="close_signal", expression="close")
        profile = EvaluationProfile(
            min_periods=5,
            min_assets=3,
            n_quantiles=5,
        )
        request = EvaluationRequest(
            factor_id="test",
            universe_id="test",
            eval_start=date(2023, 1, 1),
            eval_end=date(2023, 1, 20),
            profile=profile,
        )

        bundle = evaluator.evaluate(factor, request)
        assert bundle.passes_profile(profile)

    def test_profile_fails_no_signal(self) -> None:
        """Flat panel produces None metrics -> profile fails."""
        panel = _make_flat_panel()
        evaluator = SignalQualityEvaluator(panel)
        factor = FactorSpec(name="close_signal", expression="close")
        profile = EvaluationProfile(min_periods=5, min_assets=3)
        request = EvaluationRequest(
            factor_id="test",
            universe_id="test",
            eval_start=date(2023, 1, 1),
            eval_end=date(2023, 1, 20),
            profile=profile,
        )

        bundle = evaluator.evaluate(factor, request)
        assert not bundle.passes_profile(profile)

    def test_missing_price_data_raises(self) -> None:
        """Price data without 'close' column raises ValueError."""
        bad_data = pd.DataFrame({"timestamp": [1, 2], "price": [100, 200]})
        with pytest.raises(ValueError, match="missing required columns"):
            SignalQualityEvaluator(bad_data)

    def test_empty_window_returns_empty_bundle(self) -> None:
        """If eval window matches no data, return a bundle with n_periods=0."""
        panel = _make_perfect_signal_panel()
        evaluator = SignalQualityEvaluator(panel)
        factor = FactorSpec(name="close_signal", expression="close")
        request = EvaluationRequest(
            factor_id="test",
            universe_id="test",
            eval_start=date(2020, 1, 1),  # no data in 2020
            eval_end=date(2020, 12, 31),
        )

        bundle = evaluator.evaluate(factor, request)
        assert bundle.n_periods == 0
        assert bundle.ic is None

    def test_with_dsl_expression(self) -> None:
        """Evaluator works with a DSL expression, not just raw 'close'."""
        panel = _make_perfect_signal_panel()
        evaluator = SignalQualityEvaluator(panel)
        factor = FactorSpec(
            name="mean_reversion",
            expression="close / ts_mean(close, 5)",
        )
        request = EvaluationRequest(
            factor_id="test",
            universe_id="test",
            eval_start=date(2023, 1, 1),
            eval_end=date(2023, 1, 20),
            profile=EvaluationProfile(min_periods=5, min_assets=3),
        )

        bundle = evaluator.evaluate(factor, request)
        # Should compute without error and return valid metrics
        assert bundle.ic is not None or bundle.rank_ic is not None
        assert bundle.n_periods == 20
        assert bundle.n_assets == 5


# ── StubSignalQualityEvaluator ───────────────────────────────────────────────


class TestStubSignalQualityEvaluator:
    def test_returns_all_metrics(self) -> None:
        evaluator = StubSignalQualityEvaluator()
        factor = FactorSpec(name="momentum_20d", expression="rank(ts_mean(close, 20))")
        request = _default_eval_request()

        bundle = evaluator.evaluate(factor, request)

        assert bundle.ic is not None
        assert bundle.rank_ic is not None
        assert bundle.quantile_spread is not None
        assert bundle.monotonicity is not None
        assert bundle.turnover is not None
        assert bundle.sharpe is not None

    def test_deterministic_same_name(self) -> None:
        evaluator = StubSignalQualityEvaluator()
        factor = FactorSpec(name="test_factor", expression="close")
        request = _default_eval_request()

        bundle_1 = evaluator.evaluate(factor, request)
        bundle_2 = evaluator.evaluate(factor, request)

        assert bundle_1.ic == bundle_2.ic
        assert bundle_1.rank_ic == bundle_2.rank_ic
        assert bundle_1.sharpe == bundle_2.sharpe

    def test_different_names_different_results(self) -> None:
        evaluator = StubSignalQualityEvaluator()
        request = _default_eval_request()

        bundle_a = evaluator.evaluate(
            FactorSpec(name="alpha", expression="close"), request
        )
        bundle_b = evaluator.evaluate(
            FactorSpec(name="beta", expression="close"), request
        )

        assert bundle_a.ic != bundle_b.ic or bundle_a.sharpe != bundle_b.sharpe

    def test_populates_eval_window(self) -> None:
        evaluator = StubSignalQualityEvaluator()
        factor = FactorSpec(name="test", expression="close")
        request = _default_eval_request()

        bundle = evaluator.evaluate(factor, request)

        assert bundle.eval_start == date(2020, 1, 1)
        assert bundle.eval_end == date(2023, 12, 31)
        assert bundle.forecast_horizon_bars == 5
        assert bundle.metadata.get("evaluator") == "signal_quality"


# ── NoveltyEvaluator ─────────────────────────────────────────────────────────


class TestNoveltyEvaluator:
    def test_novel_when_no_existing(self) -> None:
        evaluator = NoveltyEvaluator()
        factor = FactorSpec(name="new_factor", expression="rank(close)")

        verdict = evaluator.check_novelty(factor)

        assert verdict.is_novel is True
        assert verdict.similarity_score == 0.0
        assert verdict.most_similar_factor_id is None

    def test_detects_exact_duplicate(self) -> None:
        existing = [("f001", "rank(close)")]
        evaluator = NoveltyEvaluator(existing_expressions=existing)
        factor = FactorSpec(name="dup", expression="rank(close)")

        verdict = evaluator.check_novelty(factor)

        assert verdict.is_novel is False
        assert verdict.similarity_score == 1.0
        assert verdict.most_similar_factor_id == "f001"

    def test_novel_with_different_expression(self) -> None:
        existing = [("f001", "rank(close)")]
        evaluator = NoveltyEvaluator(existing_expressions=existing)
        factor = FactorSpec(name="new", expression="ts_mean(volume, 10)")

        verdict = evaluator.check_novelty(factor)

        assert verdict.is_novel is True
        assert verdict.similarity_score == 0.0

    def test_whitespace_variant_is_duplicate(self) -> None:
        """Whitespace-only differences should still flag as duplicates."""
        existing = [("f001", "ts_mean(close, 20)")]
        evaluator = NoveltyEvaluator(existing_expressions=existing)
        factor = FactorSpec(
            name="dup", expression=" ts_mean( close , 20 ) "
        )

        verdict = evaluator.check_novelty(factor)
        assert verdict.is_novel is False
        assert verdict.similarity_score == 1.0
        assert verdict.most_similar_factor_id == "f001"

    def test_commutative_variant_is_duplicate(self) -> None:
        """Operand order in commutative ops shouldn't matter."""
        existing = [("f001", "close + volume")]
        evaluator = NoveltyEvaluator(existing_expressions=existing)
        factor = FactorSpec(name="dup", expression="volume + close")

        verdict = evaluator.check_novelty(factor)
        assert verdict.is_novel is False
        assert verdict.similarity_score == 1.0

    def test_near_duplicate_window_variation(self) -> None:
        """ts_mean(close, 20) vs ts_mean(close, 21) → high similarity."""
        existing = [("f001", "ts_mean(close, 20)")]
        evaluator = NoveltyEvaluator(existing_expressions=existing)
        factor = FactorSpec(name="near", expression="ts_mean(close, 21)")

        verdict = evaluator.check_novelty(factor)
        assert verdict.similarity_score >= 0.85
        assert verdict.is_novel is False

    def test_registry_backed_comparison(self) -> None:
        """Registry-sourced experiments participate in the novelty check."""
        from alpha_harness.registries.experiment import ExperimentRegistry
        from alpha_harness.schemas.evaluation import EvaluationBundle
        from alpha_harness.schemas.experiment import ExperimentRecord

        registry = ExperimentRegistry()
        seed_factor = FactorSpec(
            name="seed_rank_close", expression="rank(close)"
        )
        seed_record = ExperimentRecord(
            hypothesis=Hypothesis(text="rank(close)"),
            factor=seed_factor,
            evaluation=EvaluationBundle(n_periods=100, n_assets=50),
            decision=ExperimentDecision.PROMOTE_CANDIDATE,
        )
        registry.save(seed_record)

        evaluator = NoveltyEvaluator(experiment_registry=registry)
        duplicate = FactorSpec(name="candidate", expression="rank(close)")

        verdict = evaluator.check_novelty(duplicate)
        assert verdict.is_novel is False
        assert verdict.most_similar_factor_id == "seed_rank_close"

    def test_unparseable_expression_falls_back_to_string(self) -> None:
        """Expressions that don't parse still compare via string equality."""
        existing = [("f001", "noise()")]  # noise() is not a whitelisted fn
        evaluator = NoveltyEvaluator(existing_expressions=existing)
        factor = FactorSpec(name="dup", expression="noise()")

        verdict = evaluator.check_novelty(factor)
        assert verdict.is_novel is False
        assert verdict.similarity_score == 1.0


# ── PromotionJudge ───────────────────────────────────────────────────────────


class TestPromotionJudge:
    def test_promotes_strong_signal(self) -> None:
        judge = PromotionJudge()
        hypothesis = Hypothesis(text="strong momentum")
        factor = FactorSpec(name="strong", expression="rank(close)")
        request = _default_eval_request()
        evaluation = EvaluationBundle(
            ic=0.08, rank_ic=0.10, quantile_spread=0.02,
            n_periods=100, n_assets=50,
        )

        detail = judge.judge(hypothesis, factor, evaluation, request)
        assert detail.decision == ExperimentDecision.PROMOTE_CANDIDATE

    def test_rejects_weak_signal(self) -> None:
        judge = PromotionJudge()
        hypothesis = Hypothesis(text="weak idea")
        factor = FactorSpec(name="weak", expression="noise()")
        request = _default_eval_request()
        evaluation = EvaluationBundle(
            ic=0.001, rank_ic=0.002, quantile_spread=0.0001,
            n_periods=100, n_assets=50,
        )

        detail = judge.judge(hypothesis, factor, evaluation, request)
        assert detail.decision == ExperimentDecision.REJECT
        assert detail.failure is not None
        assert detail.failure.category == FailureCategory.WEAK_SIGNAL

    def test_rejects_insufficient_data(self) -> None:
        judge = PromotionJudge()
        hypothesis = Hypothesis(text="data sparse")
        factor = FactorSpec(name="sparse", expression="close")
        request = _default_eval_request()
        evaluation = EvaluationBundle(
            ic=0.05, rank_ic=0.06, quantile_spread=0.01,
            n_periods=10,  # below min_periods=60
            n_assets=50,
        )

        detail = judge.judge(hypothesis, factor, evaluation, request)
        assert detail.decision == ExperimentDecision.REJECT
        assert detail.failure is not None
        assert detail.failure.category == FailureCategory.DATA_INSUFFICIENT

    def test_rejects_duplicate(self) -> None:
        novelty = NoveltyEvaluator(
            existing_expressions=[("f001", "rank(close)")]
        )
        judge = PromotionJudge(novelty_evaluator=novelty)
        hypothesis = Hypothesis(text="same idea")
        factor = FactorSpec(name="dup", expression="rank(close)")
        request = _default_eval_request()
        evaluation = EvaluationBundle(
            ic=0.08, rank_ic=0.10, quantile_spread=0.02,
            n_periods=100, n_assets=50,
        )

        detail = judge.judge(hypothesis, factor, evaluation, request)
        assert detail.decision == ExperimentDecision.REJECT
        assert detail.failure is not None
        assert detail.failure.category == FailureCategory.DUPLICATE

    def test_refines_borderline(self) -> None:
        """Metrics that pass but are within 20% of threshold -> REFINE."""
        judge = PromotionJudge(refine_margin=0.20)
        hypothesis = Hypothesis(text="borderline")
        factor = FactorSpec(name="border", expression="edge()")
        request = _default_eval_request()
        # Default thresholds: ic=0.02, rank_ic=0.03, quantile_spread=0.005
        evaluation = EvaluationBundle(
            ic=0.022, rank_ic=0.035, quantile_spread=0.0055,
            n_periods=100, n_assets=50,
        )

        detail = judge.judge(hypothesis, factor, evaluation, request)
        assert detail.decision == ExperimentDecision.REFINE

    def test_missing_required_metric_rejects(self) -> None:
        judge = PromotionJudge()
        hypothesis = Hypothesis(text="partial eval")
        factor = FactorSpec(name="partial", expression="close")
        request = _default_eval_request()
        evaluation = EvaluationBundle(
            ic=0.05, rank_ic=None,  # missing required metric
            quantile_spread=0.01,
            n_periods=100, n_assets=50,
        )

        detail = judge.judge(hypothesis, factor, evaluation, request)
        assert detail.decision == ExperimentDecision.REJECT


# ── ResearchOrchestrator (end-to-end smoke test) ─────────────────────────────


class TestResearchOrchestrator:
    def _build_orchestrator(
        self,
        novelty: NoveltyEvaluator | None = None,
    ) -> ResearchOrchestrator:
        compiler = FactorDslCompiler()
        evaluator = StubSignalQualityEvaluator()
        judge = PromotionJudge(novelty_evaluator=novelty)
        service = AlphaHarnessService(
            compiler=compiler,
            evaluator=evaluator,
            judge=judge,
        )
        return ResearchOrchestrator(
            service=service,
            experiment_registry=ExperimentRegistry(),
            hypothesis_registry=HypothesisRegistry(),
        )

    def test_single_cycle_end_to_end(self) -> None:
        """Smoke test: hypothesis -> compile -> evaluate -> judge -> record."""
        orch = self._build_orchestrator()
        hypothesis = Hypothesis(text="ts_mean(close, 20)")
        request = _default_eval_request()

        record = orch.run_cycle(hypothesis, request)

        assert record.hypothesis.text == "ts_mean(close, 20)"
        assert record.factor.hypothesis_id == hypothesis.id
        assert record.factor.name != ""
        assert record.evaluation.ic is not None
        assert record.evaluation.eval_start == date(2020, 1, 1)
        assert record.decision in list(ExperimentDecision)

    def test_batch_processes_all(self) -> None:
        orch = self._build_orchestrator()
        hypotheses = [
            Hypothesis(text="close / ts_mean(close, 5)"),
            Hypothesis(text="ts_delta(close, 10)"),
            Hypothesis(text="ts_std(close, 20)"),
        ]
        request = _default_eval_request()

        records = orch.run_batch(hypotheses, request)

        assert len(records) == 3
        for record in records:
            assert record.decision in list(ExperimentDecision)

    def test_summary_counts(self) -> None:
        orch = self._build_orchestrator()
        hypotheses = [
            Hypothesis(text="rank(close)"),
            Hypothesis(text="rank(volume)"),
        ]
        request = _default_eval_request()

        orch.run_batch(hypotheses, request)
        summary = orch.summary()

        total = sum(summary.values())
        assert total == 2

    def test_hypothesis_status_updated(self) -> None:
        """After a cycle, the hypothesis status should reflect the decision."""
        orch = self._build_orchestrator()
        hypothesis = Hypothesis(text="zscore(close, 20)")
        request = _default_eval_request()

        record = orch.run_cycle(hypothesis, request)

        stored = orch._hypotheses.get(hypothesis.id)
        assert stored is not None
        if record.decision == ExperimentDecision.PROMOTE_CANDIDATE:
            assert stored.status == HypothesisStatus.PROMISING
        elif record.decision == ExperimentDecision.REJECT:
            assert stored.status == HypothesisStatus.REJECTED
        elif record.decision == ExperimentDecision.REFINE:
            assert stored.status == HypothesisStatus.TESTING

    def test_experiment_persisted_in_registry(self) -> None:
        orch = self._build_orchestrator()
        hypothesis = Hypothesis(text="ts_sum(volume, 10)")
        request = _default_eval_request()

        record = orch.run_cycle(hypothesis, request)

        stored = orch._experiments.get(record.id)
        assert stored is not None
        assert stored.id == record.id
        assert stored.decision == record.decision
