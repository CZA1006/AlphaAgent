"""Round 4B — walk-forward evaluator + judge stability check."""

from __future__ import annotations

from datetime import date

import pytest

from alpha_harness.evaluators.promotion_judge import PromotionJudge
from alpha_harness.evaluators.walk_forward import (
    WalkForwardConfig,
    WalkForwardEvaluator,
    fold_windows,
)
from alpha_harness.schemas.evaluation import (
    EvaluationBundle,
    EvaluationProfile,
    EvaluationRequest,
    HoldoutPolicy,
    HoldoutStrategy,
)
from alpha_harness.schemas.experiment import ExperimentDecision, FailureCategory
from alpha_harness.schemas.factor import FactorSpec
from alpha_harness.schemas.hypothesis import Hypothesis

# ── fold_windows ────────────────────────────────────────────────────────────


def test_fold_windows_disjoint() -> None:
    cfg = WalkForwardConfig(
        n_folds=4,
        fold_size_days=10,
        step_days=10,
        min_fold_days=1,
    )
    spans = fold_windows(date(2024, 1, 1), date(2024, 12, 31), cfg)
    assert len(spans) == 4
    # Disjoint: each fold start = previous start + 10 days
    starts = [s for s, _ in spans]
    diffs = [(starts[i + 1] - starts[i]).days for i in range(len(starts) - 1)]
    assert diffs == [10, 10, 10]


def test_fold_windows_overlapping() -> None:
    cfg = WalkForwardConfig(
        n_folds=3,
        fold_size_days=20,
        step_days=5,
        min_fold_days=1,
    )
    spans = fold_windows(date(2024, 1, 1), date(2024, 12, 31), cfg)
    assert len(spans) == 3
    # Overlapping: end of first > start of second
    assert spans[0][1] > spans[1][0]


def test_fold_windows_drops_when_short() -> None:
    cfg = WalkForwardConfig(n_folds=4, fold_size_days=60, step_days=20)
    spans = fold_windows(date(2024, 1, 1), date(2024, 1, 30), cfg)
    assert spans == []  # 60-day fold can't fit in a 30-day span


def test_walk_forward_config_validates() -> None:
    with pytest.raises(ValueError):
        WalkForwardConfig(n_folds=0)
    with pytest.raises(ValueError):
        WalkForwardConfig(fold_size_days=0)
    with pytest.raises(ValueError):
        WalkForwardConfig(step_days=0)


# ── Aggregation ─────────────────────────────────────────────────────────────


class _ScriptedEvaluator:
    """Returns a pre-canned bundle keyed by (eval_start, eval_end)."""

    def __init__(
        self,
        per_fold: dict[tuple[date, date], EvaluationBundle],
        default: EvaluationBundle | None = None,
    ) -> None:
        self._per_fold = per_fold
        self._default = default
        self.requests: list[EvaluationRequest] = []

    def evaluate(
        self,
        factor: FactorSpec,
        request: EvaluationRequest,
    ) -> EvaluationBundle:
        self.requests.append(request)
        key = (request.eval_start, request.eval_end)
        b = self._per_fold.get(key)
        if b is not None:
            return b
        if self._default is not None:
            return self._default
        raise AssertionError(f"unexpected fold {key}")


def _bundle(ic: float, rank_ic: float, **kw: float) -> EvaluationBundle:
    return EvaluationBundle(
        ic=ic,
        rank_ic=rank_ic,
        quantile_spread=kw.get("qs", 0.01),
        net_quantile_spread=kw.get("net_qs", 0.009),
        turnover=kw.get("turnover", 0.4),
        n_periods=20,
        n_assets=10,
    )


def _request(start: date, end: date) -> EvaluationRequest:
    return EvaluationRequest(
        factor_id="f",
        universe_id="u",
        eval_start=start,
        eval_end=end,
    )


def test_aggregate_means_match_per_fold() -> None:
    cfg = WalkForwardConfig(
        n_folds=3,
        fold_size_days=10,
        step_days=10,
        embargo_days=0,
        min_fold_days=1,
    )
    s = date(2024, 1, 1)
    spans = fold_windows(s, date(2024, 12, 31), cfg)
    inner = _ScriptedEvaluator(
        {
            spans[0]: _bundle(0.05, 0.06),
            spans[1]: _bundle(-0.01, 0.04),
            spans[2]: _bundle(0.10, 0.08),
        }
    )
    wf = WalkForwardEvaluator(inner, cfg)
    out = wf.evaluate(
        FactorSpec(name="f", expression="rank(close)"),
        _request(s, date(2024, 12, 31)),
    )
    assert out.ic == pytest.approx((0.05 - 0.01 + 0.10) / 3)
    assert out.rank_ic == pytest.approx((0.06 + 0.04 + 0.08) / 3)
    wf_meta = out.metadata["walk_forward"]
    assert wf_meta["n_folds"] == 3
    assert wf_meta["fraction_positive_ic"] == pytest.approx(2 / 3)
    assert wf_meta["fraction_positive_rank_ic"] == pytest.approx(1.0)
    assert len(out.metadata["per_fold"]) == 3


def test_walk_forward_reserves_one_global_tail_holdout() -> None:
    cfg = WalkForwardConfig(
        n_folds=2,
        fold_size_days=20,
        step_days=20,
        embargo_days=0,
        min_fold_days=1,
    )
    start = date(2024, 1, 1)
    end = date(2024, 4, 9)
    training_end = date(2024, 3, 20)
    spans = fold_windows(start, training_end, cfg)
    holdout_span = (date(2024, 3, 21), end)
    inner = _ScriptedEvaluator(
        {
            spans[0]: _bundle(0.04, 0.04),
            spans[1]: _bundle(0.06, 0.06),
            holdout_span: _bundle(0.02, 0.025),
        }
    )
    request = _request(start, end).model_copy(
        update={
            "holdout": HoldoutPolicy(
                strategy=HoldoutStrategy.TAIL,
                holdout_fraction=0.2,
            )
        }
    )

    out = WalkForwardEvaluator(inner, cfg).evaluate(
        FactorSpec(name="f", expression="rank(close)"),
        request,
    )

    assert [(r.eval_start, r.eval_end) for r in inner.requests] == [*spans, holdout_span]
    assert all(r.holdout.strategy is HoldoutStrategy.NONE for r in inner.requests)
    assert out.rank_ic == pytest.approx(0.05)
    assert out.metadata["holdout"] == {
        "holdout_start": "2024-03-21",
        "holdout_end": "2024-04-09",
        "holdout_days": 20,
        "embargo_bars": 6,
        "embargo_mode": "window_local_forward_returns",
        "ic": 0.02,
        "rank_ic": 0.025,
        "quantile_spread": 0.01,
        "net_quantile_spread": 0.009,
        "turnover": 0.4,
        "n_periods": 20,
        "decay_ratio": pytest.approx(0.5),
    }


def test_aggregate_metadata_uses_all_folds_not_first_fold() -> None:
    cfg = WalkForwardConfig(
        n_folds=2,
        fold_size_days=10,
        step_days=10,
        embargo_days=0,
        min_fold_days=1,
    )
    start = date(2024, 1, 1)
    spans = fold_windows(start, date(2024, 12, 31), cfg)
    first = _bundle(0.04, 0.05).model_copy(
        update={
            "metadata": {
                "ic_by_horizon": {"5": 0.04, "10": -0.02},
                "rank_ic_by_horizon": {"5": 0.05, "10": 0.01},
                "ic_sign_consistent_horizons": 1,
                "portfolio": {
                    "tail_concentration": 0.2,
                    "episode_top3_positive_share": 0.3,
                    "episode_top3_positive_share_max": 0.5,
                    "episode_positive_phase_count": 5.0,
                    "episode_min_positive_count": 4.0,
                    "hit_rate": 0.4,
                },
                "complement": {
                    "base_recipe_id": "base-1",
                    "candidate_expression": "rank(volume)",
                    "mean_rank_correlation": 0.2,
                    "rank_ic_lift": 0.01,
                },
            }
        }
    )
    second = _bundle(0.08, 0.07).model_copy(
        update={
            "metadata": {
                "ic_by_horizon": {"5": 0.08, "10": 0.06},
                "rank_ic_by_horizon": {"5": 0.07, "10": 0.03},
                "ic_sign_consistent_horizons": 2,
                "portfolio": {
                    "tail_concentration": 0.7,
                    "episode_top3_positive_share": 0.8,
                    "episode_top3_positive_share_max": 0.9,
                    "episode_positive_phase_count": 5.0,
                    "episode_min_positive_count": 2.0,
                    "hit_rate": 0.8,
                },
                "complement": {
                    "base_recipe_id": "base-1",
                    "candidate_expression": "rank(volume)",
                    "mean_rank_correlation": -0.4,
                    "rank_ic_lift": -0.02,
                },
            }
        }
    )
    out = WalkForwardEvaluator(
        _ScriptedEvaluator({spans[0]: first, spans[1]: second}),
        cfg,
    ).evaluate(
        FactorSpec(name="f", expression="rank(close)"),
        _request(start, date(2024, 12, 31)),
    )

    assert out.metadata["ic_by_horizon"] == pytest.approx({"5": 0.06, "10": 0.02})
    assert out.metadata["rank_ic_by_horizon"] == pytest.approx({"5": 0.06, "10": 0.02})
    assert out.metadata["ic_sign_consistent_horizons"] == 2
    assert out.metadata["portfolio"]["tail_concentration"] == pytest.approx(0.7)
    assert out.metadata["portfolio"]["episode_top3_positive_share"] == pytest.approx(0.8)
    assert out.metadata["portfolio"]["episode_top3_positive_share_max"] == pytest.approx(0.9)
    assert out.metadata["portfolio"]["episode_positive_phase_count"] == pytest.approx(5.0)
    assert out.metadata["portfolio"]["episode_min_positive_count"] == pytest.approx(2.0)
    assert out.metadata["portfolio"]["hit_rate"] == pytest.approx(0.6)
    complement = out.metadata["complement"]
    assert complement["base_recipe_id"] == "base-1"
    assert complement["mean_rank_correlation"] == pytest.approx(-0.1)
    assert complement["max_abs_rank_correlation"] == pytest.approx(0.4)
    assert complement["mean_rank_ic_lift"] == pytest.approx(-0.005)
    assert complement["fraction_positive_rank_ic_lift"] == pytest.approx(0.5)
    assert complement["n_folds"] == 2


def test_aggregate_passes_through_when_one_fold() -> None:
    """A single span shouldn't trigger the walk-forward path."""
    cfg = WalkForwardConfig(n_folds=4, fold_size_days=400, step_days=20)
    inner = _ScriptedEvaluator({}, default=_bundle(0.05, 0.06))
    wf = WalkForwardEvaluator(inner, cfg)
    out = wf.evaluate(
        FactorSpec(name="f", expression="rank(close)"),
        _request(date(2024, 1, 1), date(2024, 6, 30)),
    )
    assert out.ic == 0.05  # passthrough
    assert out.metadata["walk_forward"]["skipped_reason"] == "span_too_short"


# ── Judge integration ──────────────────────────────────────────────────────


def _judge_bundle(
    *,
    ic: float = 0.05,
    rank_ic: float = 0.06,
    fraction_positive_rank_ic: float = 1.0,
    n_folds: int = 4,
) -> EvaluationBundle:
    return EvaluationBundle(
        ic=ic,
        rank_ic=rank_ic,
        quantile_spread=0.01,
        net_quantile_spread=0.009,
        turnover=0.4,
        n_periods=400,
        n_assets=10,
        metadata={
            "walk_forward": {
                "n_folds": n_folds,
                "fraction_positive_rank_ic": fraction_positive_rank_ic,
                "mean_rank_ic": rank_ic,
                "fold_size_days": 60,
                "step_days": 20,
            },
        },
    )


def _judge() -> PromotionJudge:
    return PromotionJudge()


def _f() -> FactorSpec:
    return FactorSpec(name="f", expression="rank(close)")


def test_judge_promotes_when_fraction_positive_meets_threshold() -> None:
    j = _judge()
    detail = j.judge(
        Hypothesis(text="x"),
        _f(),
        _judge_bundle(fraction_positive_rank_ic=0.75),
        EvaluationRequest(
            factor_id="f",
            universe_id="u",
            eval_start=date(2024, 1, 1),
            eval_end=date(2024, 12, 31),
            profile=EvaluationProfile(min_periods=10),
        ),
    )
    assert detail.decision == ExperimentDecision.PROMOTE_CANDIDATE


def test_judge_rejects_when_too_few_positive_folds() -> None:
    j = _judge()
    # 1/4 folds positive — high mean rank_ic but unstable.
    detail = j.judge(
        Hypothesis(text="x"),
        _f(),
        _judge_bundle(rank_ic=0.20, fraction_positive_rank_ic=0.25),
        EvaluationRequest(
            factor_id="f",
            universe_id="u",
            eval_start=date(2024, 1, 1),
            eval_end=date(2024, 12, 31),
            profile=EvaluationProfile(min_periods=10),
        ),
    )
    assert detail.decision == ExperimentDecision.REJECT
    assert detail.failure is not None
    assert detail.failure.category == FailureCategory.WEAK_SIGNAL
    assert "fraction_positive_rank_ic" in detail.failure.detail


def test_judge_skips_walk_forward_check_when_legacy_bundle() -> None:
    """Single-fold / legacy bundles must not trigger the new check."""
    bundle = EvaluationBundle(
        ic=0.05,
        rank_ic=0.06,
        quantile_spread=0.01,
        net_quantile_spread=0.009,
        turnover=0.4,
        n_periods=400,
        n_assets=10,
    )
    detail = _judge().judge(
        Hypothesis(text="x"),
        _f(),
        bundle,
        EvaluationRequest(
            factor_id="f",
            universe_id="u",
            eval_start=date(2024, 1, 1),
            eval_end=date(2024, 12, 31),
            profile=EvaluationProfile(min_periods=10),
        ),
    )
    # No walk-forward metadata → judge follows the legacy path; bundle
    # is well above thresholds with no horizon data, so promote.
    assert detail.decision == ExperimentDecision.PROMOTE_CANDIDATE


def test_judge_skips_walk_forward_check_when_n_folds_lt_2() -> None:
    bundle = _judge_bundle(n_folds=1)
    detail = _judge().judge(
        Hypothesis(text="x"),
        _f(),
        bundle,
        EvaluationRequest(
            factor_id="f",
            universe_id="u",
            eval_start=date(2024, 1, 1),
            eval_end=date(2024, 12, 31),
            profile=EvaluationProfile(min_periods=10),
        ),
    )
    assert detail.decision == ExperimentDecision.PROMOTE_CANDIDATE


# ── Round-trip ──────────────────────────────────────────────────────────────


def test_walk_forward_bundle_serialises_through_pydantic() -> None:
    """Aggregate bundle must survive model_dump_json round-trip."""
    cfg = WalkForwardConfig(
        n_folds=2,
        fold_size_days=10,
        step_days=10,
        embargo_days=0,
        min_fold_days=1,
    )
    s = date(2024, 1, 1)
    spans = fold_windows(s, date(2024, 12, 31), cfg)
    inner = _ScriptedEvaluator(
        {
            spans[0]: _bundle(0.05, 0.06),
            spans[1]: _bundle(0.04, 0.05),
        }
    )
    wf = WalkForwardEvaluator(inner, cfg)
    bundle = wf.evaluate(
        FactorSpec(name="f", expression="rank(close)"),
        _request(s, date(2024, 12, 31)),
    )
    payload = bundle.model_dump_json()
    restored = EvaluationBundle.model_validate_json(payload)
    assert restored.metadata["walk_forward"]["n_folds"] == 2
    assert restored.ic == pytest.approx(bundle.ic)


# ── Embargo + purge (Round 4D) ──────────────────────────────────────────────


def test_fold_windows_embargo_trims_each_fold_end() -> None:
    cfg = WalkForwardConfig(
        n_folds=3,
        fold_size_days=30,
        step_days=30,
        min_fold_days=1,
    )
    spans = fold_windows(
        date(2024, 1, 1),
        date(2024, 12, 31),
        cfg,
        embargo_days=5,
    )
    assert len(spans) == 3
    # Each fold's net span = fold_size_days - embargo = 25 days inclusive.
    for start, end in spans:
        assert (end - start).days + 1 == 25


def test_fold_windows_purges_when_embargo_exceeds_fold() -> None:
    cfg = WalkForwardConfig(
        n_folds=4,
        fold_size_days=10,
        step_days=10,
        min_fold_days=20,
    )
    spans = fold_windows(
        date(2024, 1, 1),
        date(2024, 12, 31),
        cfg,
        embargo_days=8,
    )
    # 10 - 8 = 2 days, below min_fold_days → all folds purged.
    assert spans == []


def test_walk_forward_config_validates_embargo() -> None:
    with pytest.raises(ValueError):
        WalkForwardConfig(embargo_days=-1)
    with pytest.raises(ValueError):
        WalkForwardConfig(min_fold_days=0)


def test_evaluator_derives_embargo_from_label() -> None:
    """Default embargo = lag_bars + forecast_horizon_bars = 1 + 5 = 6."""
    cfg = WalkForwardConfig(
        n_folds=3,
        fold_size_days=30,
        step_days=30,
        min_fold_days=1,
    )
    s = date(2024, 1, 1)
    inner = _ScriptedEvaluator({}, default=_bundle(0.05, 0.06))
    out = WalkForwardEvaluator(inner, cfg).evaluate(
        FactorSpec(name="f", expression="rank(close)"),
        _request(s, date(2024, 12, 31)),
    )
    wf_meta = out.metadata["walk_forward"]
    assert wf_meta["embargo_days"] == 6
    assert wf_meta["n_folds"] == 3
    assert wf_meta["purged_folds"] == 0


def test_evaluator_records_purged_folds() -> None:
    """Embargo too large → some folds drop, count surfaces in metadata."""
    cfg = WalkForwardConfig(
        n_folds=4,
        fold_size_days=10,
        step_days=10,
        embargo_days=8,
        min_fold_days=20,
    )
    inner = _ScriptedEvaluator({}, default=_bundle(0.05, 0.06))
    out = WalkForwardEvaluator(inner, cfg).evaluate(
        FactorSpec(name="f", expression="rank(close)"),
        _request(date(2024, 1, 1), date(2024, 12, 31)),
    )
    wf_meta = out.metadata["walk_forward"]
    # Every fold purged → falls back to inner-evaluator passthrough,
    # but purged_folds is still surfaced for diagnostics.
    assert wf_meta["purged_folds"] >= 1
    assert wf_meta["embargo_days"] == 8


def test_overlapping_folds_no_longer_share_labelled_rows() -> None:
    """With embargo >= step, fold N's net end < fold N+1's start."""
    cfg = WalkForwardConfig(
        n_folds=3,
        fold_size_days=20,
        step_days=10,
        min_fold_days=1,
    )
    spans = fold_windows(
        date(2024, 1, 1),
        date(2024, 12, 31),
        cfg,
        embargo_days=10,
    )
    assert len(spans) >= 2
    for i in range(len(spans) - 1):
        # Net end of fold i must be strictly before start of fold i+1.
        assert spans[i][1] < spans[i + 1][0]


def test_explicit_embargo_zero_preserves_legacy_behaviour() -> None:
    cfg = WalkForwardConfig(
        n_folds=3,
        fold_size_days=10,
        step_days=10,
        embargo_days=0,
        min_fold_days=1,
    )
    s = date(2024, 1, 1)
    spans_default = fold_windows(s, date(2024, 12, 31), cfg)
    spans_zero = fold_windows(s, date(2024, 12, 31), cfg, embargo_days=0)
    assert spans_default == spans_zero
    # Each fold spans a full 10 days inclusive.
    for start, end in spans_default:
        assert (end - start).days + 1 == 10
