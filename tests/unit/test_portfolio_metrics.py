"""Round 4C — risk-aware portfolio metrics + tail-concentration gate."""

from __future__ import annotations

import math
from datetime import date

import pandas as pd
import pytest

from alpha_harness.evaluators.portfolio import (
    compute_long_short_returns,
    compute_portfolio_metrics,
)
from alpha_harness.evaluators.promotion_judge import PromotionJudge
from alpha_harness.reports.cycle_report import _thumbnail
from alpha_harness.schemas.evaluation import (
    EvaluationBundle,
    EvaluationProfile,
    EvaluationRequest,
)
from alpha_harness.schemas.experiment import (
    ExperimentDecision,
    ExperimentRecord,
    FailureCategory,
)
from alpha_harness.schemas.factor import FactorSpec
from alpha_harness.schemas.hypothesis import Hypothesis

# ── compute_portfolio_metrics ───────────────────────────────────────────────


def test_metrics_on_constant_positive_series() -> None:
    s = pd.Series([0.01] * 10)
    m = compute_portfolio_metrics(s)
    assert m["mean_return"] == pytest.approx(0.01)
    # Constant series → vol ~= 0 → sharpe is None (undefined).
    assert m["vol"] == pytest.approx(0.0, abs=1e-12)
    assert m["sharpe"] is None
    assert m["hit_rate"] == 1.0
    assert m["max_drawdown"] == pytest.approx(0.0, abs=1e-12)


def test_sharpe_annualised() -> None:
    # Mean 0.001, vol 0.01 → daily Sharpe ~0.1; annualised by sqrt(252).
    s = pd.Series([0.011, -0.009] * 50)
    m = compute_portfolio_metrics(s)
    assert m["mean_return"] == pytest.approx(0.001)
    expected = (m["mean_return"] / m["vol"]) * math.sqrt(252)
    assert m["sharpe"] == pytest.approx(expected)


def test_max_drawdown_on_known_path() -> None:
    # Cumulative: 1, 2, 1, 0, 1.  Running max: 1, 2, 2, 2, 2.
    # Drawdowns: 0, 0, 1, 2, 1.  Peak drawdown = 2.
    s = pd.Series([1, 1, -1, -1, 1])
    m = compute_portfolio_metrics(s)
    assert m["max_drawdown"] == pytest.approx(2.0)


def test_tail_concentration_flags_3_day_pump() -> None:
    # 30 days: 3 huge positives, 27 small negatives.
    s = pd.Series([1.0] * 3 + [-0.01] * 27)
    m = compute_portfolio_metrics(s)
    # Top-3 sum = 3; total = 3 - 0.27 = 2.73; concentration > 1.0.
    assert m["tail_concentration"] is not None
    assert m["tail_concentration"] > 1.0


def test_tail_concentration_none_when_total_negative() -> None:
    s = pd.Series([-0.05, -0.02, -0.01, -0.03])
    m = compute_portfolio_metrics(s)
    assert m["tail_concentration"] is None


def test_metrics_handle_empty_series() -> None:
    m = compute_portfolio_metrics(pd.Series(dtype=float))
    assert m["mean_return"] is None
    assert m["sharpe"] is None
    assert m["n_periods"] == 0


# ── compute_long_short_returns ──────────────────────────────────────────────


def test_long_short_returns_per_date() -> None:
    # Two dates, five symbols each: signal correlates positively with return.
    sig = pd.Series([1, 2, 3, 4, 5, 1, 2, 3, 4, 5], dtype=float)
    fwd = pd.Series([0.0, 0.0, 0.0, 0.0, 0.05, 0.0, 0.0, 0.0, 0.0, 0.10])
    ts = pd.Series(["d1"] * 5 + ["d2"] * 5)
    out = compute_long_short_returns(sig, fwd, ts, n_quantiles=5)
    assert len(out) == 2
    assert out.iloc[0] == pytest.approx(0.05)
    assert out.iloc[1] == pytest.approx(0.10)


# ── Judge integration ──────────────────────────────────────────────────────


def _bundle_with_tail(tail: float) -> EvaluationBundle:
    return EvaluationBundle(
        ic=0.05,
        rank_ic=0.06,
        quantile_spread=0.01,
        net_quantile_spread=0.009,
        turnover=0.4,
        n_periods=400,
        n_assets=10,
        metadata={
            "portfolio": {
                "tail_concentration": tail,
                "sharpe": 1.0,
                "max_drawdown": 0.05,
                "hit_rate": 0.55,
                "mean_return": 0.001,
                "vol": 0.01,
                "n_periods": 400,
            }
        },
    )


def _request() -> EvaluationRequest:
    return EvaluationRequest(
        factor_id="f",
        universe_id="u",
        eval_start=date(2024, 1, 1),
        eval_end=date(2024, 12, 31),
        profile=EvaluationProfile(min_periods=10),
    )


def _factor() -> FactorSpec:
    return FactorSpec(name="f", expression="rank(close)")


def test_judge_rejects_when_tail_concentration_high() -> None:
    detail = PromotionJudge().judge(
        Hypothesis(text="x"),
        _factor(),
        _bundle_with_tail(0.85),
        _request(),
    )
    assert detail.decision == ExperimentDecision.REJECT
    assert detail.failure is not None
    assert detail.failure.category == FailureCategory.OTHER
    assert "tail_concentration" in detail.failure.detail


def test_judge_promotes_when_tail_concentration_modest() -> None:
    detail = PromotionJudge().judge(
        Hypothesis(text="x"),
        _factor(),
        _bundle_with_tail(0.30),
        _request(),
    )
    assert detail.decision == ExperimentDecision.PROMOTE_CANDIDATE


def test_judge_skips_tail_check_on_legacy_bundle() -> None:
    """Bundle with no portfolio metadata bypasses the new gate."""
    bundle = EvaluationBundle(
        ic=0.05,
        rank_ic=0.06,
        quantile_spread=0.01,
        net_quantile_spread=0.009,
        turnover=0.4,
        n_periods=400,
        n_assets=10,
    )
    detail = PromotionJudge().judge(
        Hypothesis(text="x"),
        _factor(),
        bundle,
        _request(),
    )
    assert detail.decision == ExperimentDecision.PROMOTE_CANDIDATE


def test_judge_threshold_is_configurable() -> None:
    # tail=0.40 with stricter threshold=0.35 → reject.
    j = PromotionJudge(max_tail_concentration=0.35)
    detail = j.judge(
        Hypothesis(text="x"),
        _factor(),
        _bundle_with_tail(0.40),
        _request(),
    )
    assert detail.decision == ExperimentDecision.REJECT


# ── Thumbnail surfaces portfolio metrics ───────────────────────────────────


def test_thumbnail_carries_portfolio_metrics() -> None:
    record = ExperimentRecord(
        hypothesis=Hypothesis(text="x"),
        factor=_factor(),
        evaluation=EvaluationBundle(
            ic=0.05,
            rank_ic=0.06,
            quantile_spread=0.01,
            sharpe=1.5,
            metadata={
                "portfolio": {
                    "max_drawdown": 0.04,
                    "hit_rate": 0.58,
                    "tail_concentration": 0.32,
                    "sharpe": 1.5,
                    "mean_return": 0.002,
                    "vol": 0.012,
                    "n_periods": 200,
                },
            },
        ),
        decision=ExperimentDecision.PROMOTE_CANDIDATE,
    )
    thumb = _thumbnail(record)
    assert thumb.sharpe == pytest.approx(1.5)
    assert thumb.max_drawdown == pytest.approx(0.04)
    assert thumb.hit_rate == pytest.approx(0.58)


def test_thumbnail_handles_missing_portfolio_block() -> None:
    record = ExperimentRecord(
        hypothesis=Hypothesis(text="x"),
        factor=_factor(),
        evaluation=EvaluationBundle(ic=0.05, rank_ic=0.06, quantile_spread=0.01),
        decision=ExperimentDecision.ARCHIVE_ONLY,
    )
    thumb = _thumbnail(record)
    assert thumb.sharpe is None
    assert thumb.max_drawdown is None
    assert thumb.hit_rate is None
