"""Round 4E — out-of-sample holdout reservation."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from alpha_harness.combination import compute_signal
from alpha_harness.evaluators.promotion_judge import PromotionJudge
from alpha_harness.evaluators.signal_quality import (
    SignalQualityEvaluator,
    evaluate_precomputed_signal,
)
from alpha_harness.factors.compiler import FactorDslCompiler
from alpha_harness.reports.cycle_report import _holdout_summary, _thumbnail
from alpha_harness.schemas.evaluation import (
    EvaluationBundle,
    EvaluationProfile,
    EvaluationRequest,
    HoldoutPolicy,
    HoldoutStrategy,
)
from alpha_harness.schemas.experiment import (
    ExperimentDecision,
    ExperimentRecord,
    FailureCategory,
)
from alpha_harness.schemas.factor import FactorSpec
from alpha_harness.schemas.hypothesis import Hypothesis

# ── HoldoutPolicy ───────────────────────────────────────────────────────────


def test_default_policy_is_disabled() -> None:
    p = HoldoutPolicy()
    assert p.strategy == HoldoutStrategy.NONE
    assert p.holdout_fraction == pytest.approx(0.20)


def test_holdout_policy_validates_fraction() -> None:
    HoldoutPolicy(holdout_fraction=0.0)  # OK
    HoldoutPolicy(holdout_fraction=0.99)  # OK
    with pytest.raises(ValueError):
        HoldoutPolicy(holdout_fraction=1.0)
    with pytest.raises(ValueError):
        HoldoutPolicy(holdout_fraction=-0.1)


# ── Evaluator split math ────────────────────────────────────────────────────


def _toy_panel(n_days: int, n_symbols: int = 5, seed: int = 7) -> pd.DataFrame:
    """Synthetic OHLCV panel where signal `close` correlates with future close."""
    import numpy as np

    rng = np.random.default_rng(seed)
    start = date(2024, 1, 1)
    rows = []
    for i in range(n_symbols):
        base = 100 + i
        for d in range(n_days):
            rows.append(
                {
                    "symbol": f"S{i}",
                    "timestamp": pd.Timestamp(start + timedelta(days=d)),
                    "close": float(base + d * 0.1 + rng.normal(0, 0.5)),
                    "volume": float(rng.integers(1000, 5000)),
                }
            )
    return pd.DataFrame(rows).sort_values(["symbol", "timestamp"]).reset_index(drop=True)


def test_evaluator_splits_window_when_holdout_active() -> None:
    df = _toy_panel(n_days=60)
    evaluator = SignalQualityEvaluator(df)
    req = EvaluationRequest(
        factor_id="f",
        universe_id="u",
        eval_start=date(2024, 1, 1),
        eval_end=date(2024, 2, 29),
        profile=EvaluationProfile(min_periods=5, min_assets=2),
        holdout=HoldoutPolicy(strategy=HoldoutStrategy.TAIL, holdout_fraction=0.2),
    )
    factor = FactorDslCompiler().compile(Hypothesis(text="rank(close)"))
    out = evaluator.evaluate(factor, req)
    assert "holdout" in out.metadata
    holdout = out.metadata["holdout"]
    # 60-day window, 20% holdout → 12 days reserved.
    assert holdout["holdout_days"] == 12
    # Holdout starts at end - 11 = Feb 18.
    assert holdout["holdout_start"] == "2024-02-18"
    assert holdout["holdout_end"] == "2024-02-29"
    assert holdout["embargo_bars"] == 6
    assert holdout["embargo_mode"] == "window_local_forward_returns"


def test_holdout_prices_cannot_change_in_sample_metrics() -> None:
    import numpy as np

    df = _toy_panel(n_days=80, seed=19)
    req = EvaluationRequest(
        factor_id="f",
        universe_id="u",
        eval_start=date(2024, 1, 1),
        eval_end=date(2024, 3, 20),
        profile=EvaluationProfile(min_periods=5, min_assets=2),
        holdout=HoldoutPolicy(strategy=HoldoutStrategy.TAIL, holdout_fraction=0.25),
    )
    factor = FactorDslCompiler().compile(Hypothesis(text="rank(close)"))
    baseline = SignalQualityEvaluator(df).evaluate(factor, req)

    changed = df.copy()
    holdout_start = date.fromisoformat(baseline.metadata["holdout"]["holdout_start"])
    mask = pd.to_datetime(changed["timestamp"]).dt.date >= holdout_start
    rng = np.random.default_rng(123)
    changed.loc[mask, "close"] = rng.lognormal(mean=4.5, sigma=1.0, size=int(mask.sum()))
    perturbed = SignalQualityEvaluator(changed).evaluate(factor, req)

    assert perturbed.ic == pytest.approx(baseline.ic)
    assert perturbed.rank_ic == pytest.approx(baseline.rank_ic)
    assert perturbed.quantile_spread == pytest.approx(baseline.quantile_spread)
    assert perturbed.metadata["holdout"]["rank_ic"] != pytest.approx(
        baseline.metadata["holdout"]["rank_ic"],
    )


def test_precomputed_holdout_uses_same_embargo_contract() -> None:
    df = _toy_panel(n_days=80, seed=23)
    req = EvaluationRequest(
        factor_id="f",
        universe_id="u",
        eval_start=date(2024, 1, 1),
        eval_end=date(2024, 3, 20),
        profile=EvaluationProfile(min_periods=5, min_assets=2),
        holdout=HoldoutPolicy(strategy=HoldoutStrategy.TAIL, holdout_fraction=0.25),
    )
    factor = FactorDslCompiler().compile(Hypothesis(text="rank(close)"))
    scalar = SignalQualityEvaluator(df).evaluate(factor, req)
    precomputed = evaluate_precomputed_signal(
        signal=compute_signal("rank(close)", df),
        df=df,
        request=req,
    )

    assert precomputed.ic == pytest.approx(scalar.ic)
    assert precomputed.rank_ic == pytest.approx(scalar.rank_ic)
    assert precomputed.metadata["holdout"]["embargo_bars"] == 6
    assert precomputed.metadata["holdout"]["embargo_mode"] == "window_local_forward_returns"


def test_evaluator_holdout_none_is_passthrough() -> None:
    df = _toy_panel(n_days=30)
    evaluator = SignalQualityEvaluator(df)
    req = EvaluationRequest(
        factor_id="f",
        universe_id="u",
        eval_start=date(2024, 1, 1),
        eval_end=date(2024, 1, 30),
        profile=EvaluationProfile(min_periods=5, min_assets=2),
    )
    factor = FactorDslCompiler().compile(Hypothesis(text="rank(close)"))
    out = evaluator.evaluate(factor, req)
    assert "holdout" not in out.metadata


def test_evaluator_carries_decay_ratio() -> None:
    df = _toy_panel(n_days=80, seed=11)
    evaluator = SignalQualityEvaluator(df)
    req = EvaluationRequest(
        factor_id="f",
        universe_id="u",
        eval_start=date(2024, 1, 1),
        eval_end=date(2024, 3, 20),
        profile=EvaluationProfile(min_periods=5, min_assets=2),
        holdout=HoldoutPolicy(strategy=HoldoutStrategy.TAIL, holdout_fraction=0.25),
    )
    factor = FactorDslCompiler().compile(Hypothesis(text="rank(close)"))
    out = evaluator.evaluate(factor, req)
    holdout = out.metadata["holdout"]
    # decay_ratio = holdout.rank_ic / in_sample.rank_ic; both must be present.
    assert holdout["rank_ic"] is not None
    assert holdout["decay_ratio"] is not None


# ── Judge gate ──────────────────────────────────────────────────────────────


def _bundle(*, in_sample_rank: float, holdout_rank: float | None) -> EvaluationBundle:
    return EvaluationBundle(
        ic=0.05,
        rank_ic=in_sample_rank,
        quantile_spread=0.01,
        net_quantile_spread=0.009,
        turnover=0.4,
        n_periods=400,
        n_assets=10,
        metadata={
            "holdout": {
                "rank_ic": holdout_rank,
                "decay_ratio": (
                    holdout_rank / in_sample_rank
                    if holdout_rank is not None and in_sample_rank
                    else None
                ),
                "holdout_start": "2024-10-01",
                "holdout_end": "2024-12-31",
            },
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


def test_judge_promotes_when_holdout_matches_in_sample() -> None:
    detail = PromotionJudge().judge(
        Hypothesis(text="x"),
        _factor(),
        _bundle(in_sample_rank=0.06, holdout_rank=0.05),
        _request(),
    )
    assert detail.decision == ExperimentDecision.PROMOTE_CANDIDATE


def test_judge_rejects_on_sign_flip() -> None:
    detail = PromotionJudge().judge(
        Hypothesis(text="x"),
        _factor(),
        _bundle(in_sample_rank=0.06, holdout_rank=-0.04),
        _request(),
    )
    assert detail.decision == ExperimentDecision.REJECT
    assert detail.failure is not None
    assert detail.failure.category == FailureCategory.WEAK_SIGNAL
    assert "sign" in detail.failure.detail.lower()


def test_judge_rejects_on_steep_decay() -> None:
    # holdout/in-sample = 0.005/0.06 = 0.083 < 0.5
    detail = PromotionJudge().judge(
        Hypothesis(text="x"),
        _factor(),
        _bundle(in_sample_rank=0.06, holdout_rank=0.005),
        _request(),
    )
    assert detail.decision == ExperimentDecision.REJECT
    assert detail.failure is not None
    assert "ratio" in detail.failure.detail


def test_judge_skips_holdout_check_when_legacy_bundle() -> None:
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
    # ratio = 0.5 with stricter threshold = 0.6 → reject.
    j = PromotionJudge(min_holdout_decay_ratio=0.6)
    detail = j.judge(
        Hypothesis(text="x"),
        _factor(),
        _bundle(in_sample_rank=0.06, holdout_rank=0.03),
        _request(),
    )
    assert detail.decision == ExperimentDecision.REJECT


# ── Thumbnail ──────────────────────────────────────────────────────────────


def test_holdout_summary_extracts_compact_view() -> None:
    raw = {
        "rank_ic": 0.05,
        "decay_ratio": 0.83,
        "holdout_start": "2024-10-01",
        "holdout_end": "2024-12-31",
        "ic": 0.04,  # excluded from the slim view
        "n_periods": 60,  # excluded
    }
    summary = _holdout_summary(raw)
    assert summary == {
        "rank_ic": 0.05,
        "decay_ratio": 0.83,
        "holdout_start": "2024-10-01",
        "holdout_end": "2024-12-31",
    }


def test_holdout_summary_handles_missing() -> None:
    assert _holdout_summary(None) is None
    assert _holdout_summary({}) is None


def test_thumbnail_carries_holdout_block() -> None:
    record = ExperimentRecord(
        hypothesis=Hypothesis(text="x"),
        factor=_factor(),
        evaluation=EvaluationBundle(
            ic=0.05,
            rank_ic=0.06,
            quantile_spread=0.01,
            metadata={
                "holdout": {
                    "rank_ic": 0.05,
                    "decay_ratio": 0.83,
                    "holdout_start": "2024-10-01",
                    "holdout_end": "2024-12-31",
                },
            },
        ),
        decision=ExperimentDecision.PROMOTE_CANDIDATE,
    )
    thumb = _thumbnail(record)
    assert thumb.holdout is not None
    assert thumb.holdout["decay_ratio"] == pytest.approx(0.83)
