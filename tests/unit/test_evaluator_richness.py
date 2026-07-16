"""Unit tests for Round 4A.3 evaluator additions.

Covers:
* Sector / beta neutralization math.
* Factor turnover + cost-adjusted quantile spread.
* Multi-horizon IC metadata + sign-consistency judge check.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from alpha_harness.combination import CombinationMethod, CombinationRecipe
from alpha_harness.evaluators.neutralize import (
    apply_cost,
    compute_factor_turnover,
    neutralize_forward_returns,
)
from alpha_harness.evaluators.promotion_judge import PromotionJudge
from alpha_harness.evaluators.signal_quality import SignalQualityEvaluator
from alpha_harness.schemas.evaluation import (
    EvaluationBundle,
    EvaluationProfile,
    EvaluationRequest,
    LabelDefinition,
    NeutralizeMode,
)
from alpha_harness.schemas.experiment import ExperimentDecision
from alpha_harness.schemas.factor import FactorSpec
from alpha_harness.schemas.hypothesis import Hypothesis

# ── Fixtures ────────────────────────────────────────────────────────────────


def _panel(n_days: int = 80, symbols: tuple[str, ...] = ("A", "B", "C", "D")) -> pd.DataFrame:
    """Build a deterministic OHLCV panel for tests."""
    rng = np.random.default_rng(7)
    start = datetime(2024, 1, 1, tzinfo=UTC)
    rows: list[dict[str, object]] = []
    for sym in symbols:
        base = 100.0
        for i in range(n_days):
            base *= 1.0 + rng.normal(0.0, 0.01)
            rows.append(
                {
                    "symbol": sym,
                    "timestamp": start + timedelta(days=i),
                    "open": base,
                    "high": base * 1.01,
                    "low": base * 0.99,
                    "close": base,
                    "volume": 1_000_000.0 + rng.normal(0, 10_000),
                }
            )
    return pd.DataFrame(rows)


# ── Neutralization ──────────────────────────────────────────────────────────


def test_sector_demean_removes_pure_sector_signal() -> None:
    """A return that is exactly the sector mean should zero out."""
    timestamps = pd.Series(pd.date_range("2024-01-01", periods=4).repeat(2))
    symbols = pd.Series(["A", "B"] * 4)
    # Both symbols share sector "X"; returns identical per date => residual zero.
    fwd = pd.Series([0.01, 0.01, -0.02, -0.02, 0.03, 0.03, 0.00, 0.00])
    out = neutralize_forward_returns(
        fwd,
        timestamps=timestamps,
        symbols=symbols,
        mode=NeutralizeMode.SECTOR,
        sector_map={"A": "X", "B": "X"},
    )
    assert np.allclose(out.to_numpy(), 0.0, atol=1e-12)


def test_sector_demean_preserves_intra_sector_dispersion() -> None:
    timestamps = pd.Series(pd.date_range("2024-01-01", periods=2).repeat(2))
    symbols = pd.Series(["A", "B", "A", "B"])
    fwd = pd.Series([0.02, -0.02, 0.04, 0.00])
    out = neutralize_forward_returns(
        fwd,
        timestamps=timestamps,
        symbols=symbols,
        mode=NeutralizeMode.SECTOR,
        sector_map={"A": "X", "B": "X"},
    )
    # Per-date demean: [0.02,-0.02]→mean 0→[0.02,-0.02]; [0.04,0]→mean 0.02→[0.02,-0.02]
    assert out.iloc[0] == pytest.approx(0.02)
    assert out.iloc[1] == pytest.approx(-0.02)
    assert out.iloc[2] == pytest.approx(0.02)
    assert out.iloc[3] == pytest.approx(-0.02)


def test_beta_neutralization_zeros_pure_market_mover() -> None:
    """A symbol whose returns equal the universe mean times 2 should
    be reduced to ~zero after beta subtraction."""
    n = 30
    ts = pd.date_range("2024-01-01", periods=n)
    rng = np.random.default_rng(0)
    mkt = rng.normal(0.0, 0.01, size=n)

    rows = []
    for i, t in enumerate(ts):
        rows.append({"ts": t, "sym": "A", "fwd": mkt[i]})
        rows.append({"ts": t, "sym": "B", "fwd": 2.0 * mkt[i]})
        rows.append({"ts": t, "sym": "C", "fwd": -1.0 * mkt[i]})
    df = pd.DataFrame(rows)

    out = neutralize_forward_returns(
        pd.Series(df["fwd"].values),
        timestamps=pd.Series(df["ts"].values),
        symbols=pd.Series(df["sym"].values),
        mode=NeutralizeMode.BETA,
    )
    df["resid"] = out.to_numpy()
    for sym in ["A", "B", "C"]:
        assert df[df["sym"] == sym]["resid"].abs().mean() < 1e-9


def test_beta_neutralization_does_not_read_future_returns() -> None:
    """Changing future labels must not alter already-produced residuals."""
    n = 50
    timestamps = pd.Series(pd.date_range("2024-01-01", periods=n).repeat(3))
    symbols = pd.Series(["A", "B", "C"] * n)
    rng = np.random.default_rng(17)
    market = rng.normal(0.0, 0.01, size=n)
    loadings = {"A": 0.7, "B": 1.2, "C": 1.8}
    returns = pd.Series(
        [
            loadings[symbol] * market[day] + rng.normal(0.0, 0.001)
            for day in range(n)
            for symbol in ("A", "B", "C")
        ]
    )

    baseline = neutralize_forward_returns(
        returns,
        timestamps=timestamps,
        symbols=symbols,
        mode=NeutralizeMode.BETA,
        beta_lookback_bars=10,
        beta_min_periods=5,
    )
    cutoff = pd.Timestamp("2024-02-05")
    future_mask = timestamps >= cutoff
    mutated_returns = returns.copy()
    mutated_returns.loc[future_mask & (symbols == "A")] += 0.25
    mutated_returns.loc[future_mask & (symbols == "C")] -= 0.20
    mutated = neutralize_forward_returns(
        mutated_returns,
        timestamps=timestamps,
        symbols=symbols,
        mode=NeutralizeMode.BETA,
        beta_lookback_bars=10,
        beta_min_periods=5,
    )

    pd.testing.assert_series_equal(
        baseline.loc[~future_mask],
        mutated.loc[~future_mask],
    )
    assert not np.allclose(
        baseline.loc[future_mask].to_numpy(),
        mutated.loc[future_mask].to_numpy(),
        equal_nan=True,
    )


def test_beta_neutralization_requires_prior_warmup() -> None:
    timestamps = pd.Series(pd.date_range("2024-01-01", periods=6).repeat(2))
    symbols = pd.Series(["A", "B"] * 6)
    returns = pd.Series([0.01, 0.02, -0.01, -0.03, 0.02, 0.01] * 2)
    out = neutralize_forward_returns(
        returns,
        timestamps=timestamps,
        symbols=symbols,
        mode=NeutralizeMode.BETA,
        beta_lookback_bars=4,
        beta_min_periods=3,
    )

    assert out.loc[timestamps < pd.Timestamp("2024-01-04")].isna().all()


def test_beta_request_rejects_min_periods_above_lookback() -> None:
    with pytest.raises(ValueError, match="beta_min_periods"):
        EvaluationRequest(
            factor_id="f",
            universe_id="u",
            eval_start=date(2024, 1, 1),
            eval_end=date(2024, 12, 31),
            neutralize=NeutralizeMode.BETA,
            beta_lookback_bars=10,
            beta_min_periods=11,
        )


def test_both_mode_does_not_drop_zero_market_sector_residuals() -> None:
    timestamps = pd.Series(pd.date_range("2024-01-01", periods=3).repeat(2))
    symbols = pd.Series(["A", "B"] * 3)
    returns = pd.Series([0.02, -0.02, 0.04, 0.00, -0.01, 0.03])
    sector_map = {"A": "X", "B": "X"}
    sector = neutralize_forward_returns(
        returns,
        timestamps=timestamps,
        symbols=symbols,
        mode=NeutralizeMode.SECTOR,
        sector_map=sector_map,
    )
    both = neutralize_forward_returns(
        returns,
        timestamps=timestamps,
        symbols=symbols,
        mode=NeutralizeMode.BOTH,
        sector_map=sector_map,
    )

    pd.testing.assert_series_equal(both, sector)


def test_none_mode_is_identity() -> None:
    fwd = pd.Series([0.1, -0.2, 0.3])
    out = neutralize_forward_returns(
        fwd,
        timestamps=pd.Series(["t", "t", "t"]),
        symbols=pd.Series(["A", "B", "C"]),
        mode=NeutralizeMode.NONE,
        sector_map={},
    )
    assert out.equals(fwd)


def test_neutralize_no_symbols_is_noop() -> None:
    fwd = pd.Series([0.1, -0.2])
    out = neutralize_forward_returns(
        fwd,
        timestamps=pd.Series(["t1", "t2"]),
        symbols=None,
        mode=NeutralizeMode.SECTOR,
    )
    assert out.equals(fwd)


# ── Turnover + cost ─────────────────────────────────────────────────────────


def test_turnover_constant_signal_is_zero() -> None:
    ts = pd.Series(pd.date_range("2024-01-01", periods=3).repeat(3))
    syms = pd.Series(["A", "B", "C"] * 3)
    # Per-date z-scores are constant -> dz = 0 -> turnover = 0.
    sig = pd.Series([1.0, 2.0, 3.0] * 3)
    t = compute_factor_turnover(sig, ts, syms)
    assert t == pytest.approx(0.0, abs=1e-12)


def test_turnover_handles_reshuffled_signal() -> None:
    ts = pd.Series(pd.date_range("2024-01-01", periods=2).repeat(3))
    syms = pd.Series(["A", "B", "C"] * 2)
    sig = pd.Series([1.0, 2.0, 3.0, 3.0, 2.0, 1.0])  # rank flips
    t = compute_factor_turnover(sig, ts, syms)
    assert t is not None and t > 0.5  # meaningful rotation


def test_apply_cost_subtracts_turnover_times_bps() -> None:
    # 10 bps times 0.5 turnover = 5e-4 reduction.
    assert apply_cost(0.01, 0.5, 10.0) == pytest.approx(0.01 - 5e-4)


def test_apply_cost_noop_when_zero() -> None:
    assert apply_cost(0.02, 0.3, 0.0) == 0.02


def test_apply_cost_none_passthrough() -> None:
    assert apply_cost(None, 0.3, 10.0) is None
    assert apply_cost(0.01, None, 10.0) == 0.01


# ── Multi-horizon integration ───────────────────────────────────────────────


def _eval_request(
    neutralize: NeutralizeMode = NeutralizeMode.NONE,
    extra_horizons: list[int] | None = None,
    cost_bps: float = 0.0,
    sector_map: dict[str, str] | None = None,
) -> EvaluationRequest:
    return EvaluationRequest(
        factor_id="f1",
        universe_id="test",
        eval_start=date(2024, 1, 1),
        eval_end=date(2024, 12, 31),
        label=LabelDefinition(
            forecast_horizon_bars=5,
            lag_bars=1,
            extra_horizons=extra_horizons or [],
        ),
        profile=EvaluationProfile(min_periods=10, min_assets=2, n_quantiles=4),
        neutralize=neutralize,
        sector_map=sector_map or {},
        cost_bps=cost_bps,
    )


def _factor() -> FactorSpec:
    # operator_tree=None → evaluator parses the expression itself.
    return FactorSpec(id="f1", name="zscore_close", expression="zscore(close)")


def test_evaluator_populates_multi_horizon_metadata() -> None:
    panel = _panel()
    evaluator = SignalQualityEvaluator(panel)
    request = _eval_request(extra_horizons=[1, 20])
    bundle = evaluator.evaluate(_factor(), request)

    assert "ic_by_horizon" in bundle.metadata
    horizons = bundle.metadata["ic_by_horizon"]
    assert isinstance(horizons, dict)
    # Primary 5 plus extras 1 and 20 (assuming data supports each).
    assert "5" in horizons
    assert "ic_sign_consistent_horizons" in bundle.metadata


def test_evaluator_default_omits_multi_horizon_metadata() -> None:
    panel = _panel()
    evaluator = SignalQualityEvaluator(panel)
    bundle = evaluator.evaluate(_factor(), _eval_request())
    assert "ic_by_horizon" not in bundle.metadata


def test_evaluator_records_causal_beta_policy() -> None:
    panel = _panel()
    evaluator = SignalQualityEvaluator(panel)
    request = _eval_request(neutralize=NeutralizeMode.BETA).model_copy(
        update={"beta_lookback_bars": 12, "beta_min_periods": 6},
    )
    bundle = evaluator.evaluate(_factor(), request)

    assert bundle.metadata["beta_estimation"] == {
        "method": "rolling_ols_lagged_1",
        "lookback_bars": 12,
        "min_periods": 6,
    }


def test_evaluator_records_complement_incremental_diagnostics() -> None:
    panel = _panel()
    base = CombinationRecipe.build(
        method=CombinationMethod.RANK_AGGREGATE,
        components=["rank(close)", "rank(volume)"],
    )
    augmented = CombinationRecipe.build(
        method=base.method,
        components=[*base.components, "rank(high - low)"],
    )
    factor = FactorSpec(
        name="complement",
        expression=f"<composite:{augmented.recipe_id}>",
        composite_recipe=augmented,
        params={
            "complement_base_recipe_id": base.recipe_id,
            "complement_base_size": 2,
        },
    )
    bundle = SignalQualityEvaluator(panel).evaluate(factor, _eval_request())
    diagnostics = bundle.metadata["complement"]
    assert isinstance(diagnostics, dict)
    assert diagnostics["base_recipe_id"] == base.recipe_id
    assert diagnostics["candidate_expression"] == "rank(high - low)"
    assert diagnostics["n_folds"] == 1
    assert diagnostics["rank_ic_lift"] == pytest.approx(
        bundle.rank_ic - diagnostics["base_rank_ic"]
    )


def test_evaluator_turnover_and_cost_recorded() -> None:
    panel = _panel()
    evaluator = SignalQualityEvaluator(panel)
    bundle = evaluator.evaluate(_factor(), _eval_request(cost_bps=5.0))
    assert bundle.turnover is not None
    assert bundle.net_quantile_spread is not None
    if bundle.quantile_spread is not None:
        # Non-zero cost should reduce the spread (or leave it equal if
        # turnover happens to be zero).
        assert bundle.net_quantile_spread <= bundle.quantile_spread + 1e-12


def test_evaluator_backwards_compatible_defaults() -> None:
    """Default request (no neutralize, no horizons, no cost) leaves the
    legacy-visible fields populated exactly as before."""
    panel = _panel()
    evaluator = SignalQualityEvaluator(panel)
    bundle = evaluator.evaluate(_factor(), _eval_request())
    assert bundle.metadata["neutralize"] == "none"
    assert bundle.metadata["cost_bps"] == 0.0


# ── Judge sign-consistency check ────────────────────────────────────────────


def _hypothesis() -> Hypothesis:
    return Hypothesis(id="h1", text="test hypothesis", rationale="r")


def _bundle_passing() -> EvaluationBundle:
    return EvaluationBundle(
        ic=0.05,
        rank_ic=0.06,
        quantile_spread=0.01,
        n_periods=120,
        n_assets=20,
    )


def test_judge_rejects_when_ic_sign_flips_across_horizons() -> None:
    bundle = _bundle_passing()
    bundle.metadata = {
        "ic_by_horizon": {"1": -0.04, "5": 0.05, "20": -0.03},
        "ic_sign_consistent_horizons": 1,
    }
    judge = PromotionJudge()
    detail = judge.judge(_hypothesis(), _factor(), bundle, _eval_request())
    assert detail.decision == ExperimentDecision.REJECT
    assert detail.failure is not None
    assert "sign_consistent" in detail.failure.detail


def test_judge_accepts_when_two_of_three_horizons_agree() -> None:
    bundle = _bundle_passing()
    bundle.metadata = {
        "ic_by_horizon": {"1": 0.04, "5": 0.05, "20": -0.01},
        "ic_sign_consistent_horizons": 2,
    }
    judge = PromotionJudge(refine_margin=0.0)
    detail = judge.judge(_hypothesis(), _factor(), bundle, _eval_request())
    assert detail.decision in (
        ExperimentDecision.PROMOTE_CANDIDATE,
        ExperimentDecision.REFINE,
    )


def test_judge_single_horizon_unaffected() -> None:
    bundle = _bundle_passing()
    # No multi-horizon metadata → sign-consistency check is a no-op.
    judge = PromotionJudge(refine_margin=0.0)
    detail = judge.judge(_hypothesis(), _factor(), bundle, _eval_request())
    assert detail.decision in (
        ExperimentDecision.PROMOTE_CANDIDATE,
        ExperimentDecision.REFINE,
    )


@pytest.mark.parametrize(
    ("complement", "holdout", "expected"),
    [
        (
            {},
            None,
            "complement_max_abs_rank_correlation is missing",
        ),
        (
            {"max_abs_rank_correlation": 0.20},
            None,
            "complement_positive_rank_ic_lift_fraction is missing",
        ),
        (
            {"max_abs_rank_correlation": 0.20, "fraction_positive_rank_ic_lift": 0.75},
            None,
            "complement_holdout_rank_ic_lift is missing",
        ),
        (
            {"max_abs_rank_correlation": 0.70, "fraction_positive_rank_ic_lift": 1.0},
            None,
            "complement_max_abs_rank_correlation",
        ),
        (
            {"max_abs_rank_correlation": 0.20, "fraction_positive_rank_ic_lift": 0.50},
            None,
            "complement_positive_rank_ic_lift_fraction",
        ),
        (
            {"max_abs_rank_correlation": 0.20, "fraction_positive_rank_ic_lift": 0.75},
            {"complement": {"rank_ic_lift": -0.01}},
            "complement_holdout_rank_ic_lift",
        ),
    ],
)
def test_judge_rejects_non_complements(
    complement: dict[str, float],
    holdout: dict[str, object] | None,
    expected: str,
) -> None:
    bundle = _bundle_passing()
    bundle.metadata = {"complement": complement}
    if holdout is not None:
        bundle.metadata["holdout"] = holdout
    detail = PromotionJudge().judge(_hypothesis(), _factor(), bundle, _eval_request())
    assert detail.decision is ExperimentDecision.REJECT
    assert detail.failure is not None
    assert expected in detail.failure.detail


def test_judge_promotes_robust_complement_and_records_selection_policy() -> None:
    bundle = _bundle_passing()
    bundle.metadata = {
        "complement": {
            "base_recipe_id": "base-1",
            "max_abs_rank_correlation": 0.20,
            "fraction_positive_rank_ic_lift": 0.75,
        },
        "holdout": {"complement": {"rank_ic_lift": 0.01}},
    }
    detail = PromotionJudge(refine_margin=0.0).judge(
        _hypothesis(), _factor(), bundle, _eval_request()
    )
    assert detail.decision is ExperimentDecision.PROMOTE_CANDIDATE
    assert detail.promotion_trail is not None
    assert detail.promotion_trail.selection["strategy"] == "composite_complement"
    assert detail.promotion_trail.selection["base_recipe_id"] == "base-1"
