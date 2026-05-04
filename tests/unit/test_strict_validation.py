"""Round 5 — strict-regime + validation-report unit tests."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from alpha_harness.regimes import STRICT_REGIME, StrictRegime, get_regime
from alpha_harness.reports.validation import (
    StrictValidationReport,
    StrictValidationReportWriter,
    build_validation_report,
    classify_failure,
)
from alpha_harness.reports.validation import (
    read_index as read_validation_index,
)
from alpha_harness.schemas.evaluation import (
    EvaluationBundle,
    EvaluationRequest,
    HoldoutStrategy,
    NeutralizeMode,
)
from alpha_harness.schemas.experiment import (
    ExperimentDecision,
    ExperimentRecord,
    FailureCategory,
    FailureRecord,
    PromotionTrail,
)
from alpha_harness.schemas.factor import FactorSpec
from alpha_harness.schemas.hypothesis import Hypothesis

# ── StrictRegime ────────────────────────────────────────────────────────────


def test_strict_regime_defaults_match_proposal() -> None:
    r = STRICT_REGIME
    assert r.cost_bps == 5.0
    assert r.neutralize is NeutralizeMode.SECTOR
    assert r.holdout_strategy is HoldoutStrategy.TAIL
    assert r.holdout_fraction == 0.20
    assert r.embargo_days == r.lag_bars + r.forecast_horizon_bars
    assert r.extra_horizons == (1, 5, 20)


def test_strict_regime_yields_consistent_trail_id() -> None:
    """Building two requests + trails from the same regime must agree."""
    req = EvaluationRequest(
        factor_id="f",
        universe_id="u",
        eval_start=date(2024, 1, 1),
        eval_end=date(2024, 12, 31),
        label=STRICT_REGIME.label_definition(),
        profile=STRICT_REGIME.evaluation_profile(),
        neutralize=STRICT_REGIME.neutralize,
        cost_bps=STRICT_REGIME.cost_bps,
        holdout=STRICT_REGIME.holdout_policy(),
    )
    a = PromotionTrail.from_inputs(
        evaluation_request=req,
        judge_thresholds=STRICT_REGIME.judge_thresholds(),
    )
    b = PromotionTrail.from_inputs(
        evaluation_request=req,
        judge_thresholds=STRICT_REGIME.judge_thresholds(),
    )
    assert a.trail_id == b.trail_id


def test_strict_regime_helpers_round_trip() -> None:
    r = STRICT_REGIME
    profile = r.evaluation_profile()
    assert profile.thresholds["ic"] == r.ic_threshold
    label = r.label_definition()
    assert label.extra_horizons == list(r.extra_horizons)
    holdout = r.holdout_policy()
    assert holdout.holdout_fraction == r.holdout_fraction
    wf = r.walk_forward_config()
    assert wf.embargo_days == r.embargo_days


def test_get_regime_unknown_raises() -> None:
    with pytest.raises(KeyError):
        get_regime("not-a-real-regime")


def test_get_regime_strict() -> None:
    assert get_regime("strict") is STRICT_REGIME


# ── classify_failure ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "detail,expected",
    [
        ("n_periods=10 < min_periods=60", "data_insufficient"),
        ("ic=0.0100 < threshold=0.0200", "threshold_ic"),
        ("rank_ic=0.0250 < threshold=0.0300", "threshold_rank_ic"),
        ("quantile_spread=0.0010 < threshold=0.0050", "threshold_quantile_spread"),
        ("Required metric 'turnover' is missing.", "missing_metric"),
        ("ic_sign_consistent_horizons=1 across 3 horizons", "sign_consistency"),
        ("fraction_positive_rank_ic=0.25 < 0.6", "walk_forward_stability"),
        ("tail_concentration=0.85 > 0.50", "tail_concentration"),
        (
            "holdout rank_ic=-0.04 disagrees in sign with in-sample rank_ic=0.06",
            "holdout_sign_flip",
        ),
        ("holdout/in-sample rank_ic ratio=0.30 < 0.50", "holdout_decay"),
        ("Too similar to factor abc123", "duplicate"),
        ("", "other"),
        ("garbled text we don't recognise", "other"),
    ],
)
def test_classify_failure(detail: str, expected: str) -> None:
    assert classify_failure(detail) == expected


# ── build_validation_report ─────────────────────────────────────────────────


def _record(
    *,
    factor_id: str,
    decision: ExperimentDecision,
    failure_detail: str = "",
) -> ExperimentRecord:
    failure = (
        FailureRecord(category=FailureCategory.WEAK_SIGNAL, detail=failure_detail)
        if decision == ExperimentDecision.REJECT
        else None
    )
    trail = (
        PromotionTrail(trail_id="abc123")
        if decision == ExperimentDecision.PROMOTE_CANDIDATE
        else None
    )
    return ExperimentRecord(
        hypothesis=Hypothesis(text="rank(close)"),
        factor=FactorSpec(id=factor_id, name="f", expression="rank(close)"),
        evaluation=EvaluationBundle(),
        decision=decision,
        failure=failure,
        promotion_trail=trail,
    )


def test_build_report_aggregates_counts_and_gates() -> None:
    records = [
        _record(factor_id="p1", decision=ExperimentDecision.PROMOTE_CANDIDATE),
        _record(
            factor_id="r1",
            decision=ExperimentDecision.REJECT,
            failure_detail="ic=0.01 < threshold=0.02",
        ),
        _record(
            factor_id="r2",
            decision=ExperimentDecision.REJECT,
            failure_detail="tail_concentration=0.9 > 0.5",
        ),
        _record(
            factor_id="r3",
            decision=ExperimentDecision.REJECT,
            failure_detail="ic=0.005 < threshold=0.02",
        ),
        _record(factor_id="ref", decision=ExperimentDecision.REFINE),
    ]
    report = build_validation_report(
        cycle_id="c1",
        regime_trail_id="trail-x",
        universe_id="strict",
        started_at=datetime.now(UTC),
        records=records,
    )
    assert report.n_proposals == 5
    assert report.n_promoted == 1
    assert report.n_rejected == 3
    assert report.n_refined == 1
    assert report.n_rejected_by_gate["threshold_ic"] == 2
    assert report.n_rejected_by_gate["tail_concentration"] == 1
    assert report.promoted_factor_ids == ["p1"]
    assert report.promoted_trail_ids == ["abc123"]


# ── Writer round-trip ──────────────────────────────────────────────────────


def _minimal_report(cycle_id: str = "c1") -> StrictValidationReport:
    now = datetime.now(UTC)
    return StrictValidationReport(
        cycle_id=cycle_id,
        regime_trail_id="t1",
        started_at=now,
        finished_at=now,
        n_proposals=2,
        n_promoted=1,
        n_refined=0,
        n_rejected=1,
        n_rejected_by_gate={"threshold_ic": 1},
        promoted_factor_ids=["fct_p"],
        promoted_trail_ids=["t1"],
    )


def test_writer_round_trips_payload(tmp_path: Path) -> None:
    writer = StrictValidationReportWriter(tmp_path)
    path = writer.write(_minimal_report("c-rt"))
    assert path is not None and path.is_file()
    payload = json.loads(path.read_text())
    assert payload["cycle_id"] == "c-rt"
    assert payload["n_promoted"] == 1
    rows = read_validation_index(tmp_path)
    assert len(rows) == 1
    assert rows[0]["cycle_id"] == "c-rt"


def test_writer_idempotent_on_same_cycle(tmp_path: Path) -> None:
    writer = StrictValidationReportWriter(tmp_path)
    writer.write(_minimal_report("dup"))
    writer.write(_minimal_report("dup"))
    assert len(read_validation_index(tmp_path)) == 1


def test_writer_appends_distinct_cycles(tmp_path: Path) -> None:
    writer = StrictValidationReportWriter(tmp_path)
    writer.write(_minimal_report("c-1"))
    writer.write(_minimal_report("c-2"))
    rows = read_validation_index(tmp_path)
    assert {r["cycle_id"] for r in rows} == {"c-1", "c-2"}


# ── Doctor probe ───────────────────────────────────────────────────────────


def test_doctor_strict_regime_probe_passes() -> None:
    from scripts.doctor import _check_strict_regime_resolves

    res = _check_strict_regime_resolves()
    assert res.passed is True
    assert "trail_id=" in res.detail


# ── Regime is frozen (hashable) ────────────────────────────────────────────


def test_strict_regime_is_immutable() -> None:
    with pytest.raises(Exception):  # noqa: B017
        STRICT_REGIME.cost_bps = 99.0  # type: ignore[misc]


def test_two_regime_instances_with_same_fields_are_equal() -> None:
    a = StrictRegime()
    b = StrictRegime()
    assert a == b
    assert hash(a) == hash(b)
