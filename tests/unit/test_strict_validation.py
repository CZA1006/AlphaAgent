"""Round 5 — strict-regime + validation-report unit tests."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from alpha_harness.llm import TokenBudget
from alpha_harness.regimes import STRICT_REGIME, StrictRegime, get_regime
from alpha_harness.reports.validation import (
    FactorThumbnail,
    StrictValidationReport,
    StrictValidationReportWriter,
    build_validation_report,
    classify_failure,
    read_reports,
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
from alpha_harness.schemas.hypothesis import AssetClass, Hypothesis
from scripts.validate_strict import (
    _dataframe_fingerprint,
    _load_replay_hypotheses,
    _validation_memory_scope_id,
)

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


def test_build_report_persists_budget_pricing_and_spend() -> None:
    budget = TokenBudget(
        max_total_tokens=10_000,
        max_cost_usd=2.0,
        prompt_cost_per_1k=0.001,
        completion_cost_per_1k=0.002,
    )
    budget.debit(prompt_tokens=100, completion_tokens=50, total_tokens=150)

    report = build_validation_report(
        cycle_id="budgeted",
        regime_trail_id="trail-x",
        universe_id="strict",
        started_at=datetime.now(UTC),
        records=[],
        budget=budget,
    )

    assert report.budget is not None
    assert report.budget.calls == 1
    assert report.budget.total_tokens_spent == 150
    assert report.budget.cost_usd_spent == pytest.approx(0.0002)
    assert report.budget.prompt_cost_per_1k == pytest.approx(0.001)
    assert report.budget.completion_cost_per_1k == pytest.approx(0.002)


# ── Writer round-trip ──────────────────────────────────────────────────────


def _minimal_report(cycle_id: str = "c1") -> StrictValidationReport:
    now = datetime.now(UTC)
    return StrictValidationReport(
        cycle_id=cycle_id,
        regime_trail_id="t1",
        memory_scope_id="scope-1",
        data_fingerprint="data-1",
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
    assert payload["schema_version"] == 4
    assert payload["memory_scope_id"] == "scope-1"
    assert payload["data_fingerprint"] == "data-1"
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


def test_read_reports_filters_trail_excludes_current_and_sorts(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    writer = StrictValidationReportWriter(tmp_path)
    older = _minimal_report("older").model_copy(
        update={"finished_at": now - timedelta(days=2)},
    )
    newer = _minimal_report("newer").model_copy(
        update={"finished_at": now - timedelta(days=1)},
    )
    other_trail = _minimal_report("other-trail").model_copy(
        update={"regime_trail_id": "t2", "finished_at": now},
    )
    other_scope = _minimal_report("other-scope").model_copy(
        update={"memory_scope_id": "scope-2", "finished_at": now},
    )
    current = _minimal_report("current-c01").model_copy(
        update={"finished_at": now + timedelta(days=1)},
    )
    for report in (older, newer, other_trail, other_scope, current):
        writer.write(report)

    reports = read_reports(
        tmp_path,
        regime_trail_id="t1",
        memory_scope_id="scope-1",
        exclude_cycle_prefix="current",
        limit=2,
    )

    assert [report.cycle_id for report in reports] == ["newer", "older"]


def test_read_reports_rejects_negative_limit(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="limit must be >= 0"):
        read_reports(tmp_path, limit=-1)


def test_load_replay_hypotheses_uses_exact_promoted_sources(tmp_path: Path) -> None:
    report = _minimal_report("source-1").model_copy(
        update={
            "factors": [
                FactorThumbnail(
                    factor_id="promoted",
                    expression="rank(close)",
                    decision=ExperimentDecision.PROMOTE_CANDIDATE.value,
                ),
                FactorThumbnail(
                    factor_id="rejected",
                    expression="rank(volume)",
                    decision=ExperimentDecision.REJECT.value,
                ),
            ],
        },
    )
    StrictValidationReportWriter(tmp_path).write(report)

    hypotheses = _load_replay_hypotheses(
        validation_dir=tmp_path,
        source_cycle_ids=["source-1"],
        data_fingerprint="data-1",
        limit=4,
        asset_class=AssetClass.HK_EQUITY,
    )

    assert [hypothesis.text for hypothesis in hypotheses] == ["rank(close)"]
    assert hypotheses[0].source == "validation_replay"
    assert hypotheses[0].asset_class is AssetClass.HK_EQUITY
    assert "source_factor:promoted" in hypotheses[0].tags


def test_load_replay_hypotheses_rejects_snapshot_mismatch(tmp_path: Path) -> None:
    StrictValidationReportWriter(tmp_path).write(_minimal_report("source-1"))

    with pytest.raises(ValueError, match="different data snapshot"):
        _load_replay_hypotheses(
            validation_dir=tmp_path,
            source_cycle_ids=["source-1"],
            data_fingerprint="other-data",
            limit=4,
            asset_class=AssetClass.HK_EQUITY,
        )


def test_dataframe_fingerprint_is_row_order_invariant_and_content_sensitive() -> None:
    frame = pd.DataFrame(
        {
            "symbol": ["A", "B"],
            "timestamp": pd.to_datetime(["2026-01-01", "2026-01-02"]),
            "close": [10.0, 20.0],
        },
    )
    reordered = frame.iloc[::-1].reset_index(drop=True)
    changed = frame.copy()
    changed.loc[0, "close"] = 10.1

    assert _dataframe_fingerprint(frame) == _dataframe_fingerprint(reordered)
    assert _dataframe_fingerprint(frame) != _dataframe_fingerprint(changed)


def test_validation_memory_scope_changes_with_evaluation_contract() -> None:
    request = EvaluationRequest(
        factor_id="ignored",
        universe_id="u",
        eval_start=date(2026, 1, 1),
        eval_end=date(2026, 1, 31),
    )
    changed = request.model_copy(update={"eval_end": date(2026, 2, 1)})

    baseline = _validation_memory_scope_id(
        request,
        regime_trail_id="trail",
        data_fingerprint="data",
    )
    assert baseline == _validation_memory_scope_id(
        request.model_copy(update={"factor_id": "another"}),
        regime_trail_id="trail",
        data_fingerprint="data",
    )
    assert baseline != _validation_memory_scope_id(
        changed,
        regime_trail_id="trail",
        data_fingerprint="data",
    )


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
