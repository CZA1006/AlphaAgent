from __future__ import annotations

from datetime import date

import pytest

from alpha_harness.evaluators.promotion_judge import PromotionJudge
from alpha_harness.multiple_testing import bonferroni_z_threshold_multiplier
from alpha_harness.schemas.evaluation import EvaluationBundle, EvaluationRequest
from alpha_harness.schemas.experiment import ExperimentDecision
from alpha_harness.schemas.factor import FactorSpec
from alpha_harness.schemas.hypothesis import Hypothesis


def _request(n_proposals: int) -> EvaluationRequest:
    return EvaluationRequest(
        factor_id="f",
        universe_id="u",
        eval_start=date(2024, 1, 1),
        eval_end=date(2024, 12, 31),
        n_proposals_in_session=n_proposals,
    )


def _bundle(*, ic: float, rank_ic: float) -> EvaluationBundle:
    return EvaluationBundle(
        ic=ic,
        rank_ic=rank_ic,
        quantile_spread=0.01,
        n_periods=100,
        n_assets=50,
    )


def test_bonferroni_z_multiplier_is_backwards_compatible_and_monotone() -> None:
    assert bonferroni_z_threshold_multiplier(1) == 1.0
    assert bonferroni_z_threshold_multiplier(6) == pytest.approx(1.4554363748)
    assert bonferroni_z_threshold_multiplier(18) == pytest.approx(1.6858164454)
    assert bonferroni_z_threshold_multiplier(36) > bonferroni_z_threshold_multiplier(18)


def test_session_pressure_rejects_single_test_threshold_pass() -> None:
    detail = PromotionJudge().judge(
        Hypothesis(text="x"),
        FactorSpec(name="f", expression="rank(close)"),
        _bundle(ic=0.03, rank_ic=0.06),
        _request(18),
    )
    assert detail.decision == ExperimentDecision.REJECT
    assert detail.failure is not None
    assert "n_proposals_in_session=18" in detail.failure.detail
    assert "threshold=0.0337" in detail.failure.detail


def test_strong_factor_can_clear_session_adjusted_thresholds() -> None:
    detail = PromotionJudge().judge(
        Hypothesis(text="x"),
        FactorSpec(name="f", expression="rank(close)"),
        _bundle(ic=0.05, rank_ic=0.08),
        _request(18),
    )
    assert detail.decision == ExperimentDecision.PROMOTE_CANDIDATE
    assert detail.promotion_trail is not None
    assert detail.promotion_trail.n_proposals_in_session == 18
    assert detail.promotion_trail.ic_threshold_multiplier == pytest.approx(1.6858164454)


@pytest.mark.parametrize("n_proposals", [0, -1])
def test_family_size_must_be_positive(n_proposals: int) -> None:
    with pytest.raises(ValueError):
        _request(n_proposals)
