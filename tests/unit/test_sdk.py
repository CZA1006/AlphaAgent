from __future__ import annotations

from datetime import UTC, datetime

from alpha_harness import sdk
from alpha_harness.combination import CombinationMethod, CombinationRecipe
from alpha_harness.markets import load_market_pack
from alpha_harness.reports import CombinationReport, StrictValidationReport
from alpha_harness.reports.combination import FactorThumbnail


def _validation_report() -> StrictValidationReport:
    now = datetime(2026, 7, 16, tzinfo=UTC)
    return StrictValidationReport(
        cycle_id="sdk-validation",
        regime_trail_id="trail",
        memory_scope_id="scope",
        data_fingerprint="data",
        started_at=now,
        finished_at=now,
        n_proposals=0,
        n_promoted=0,
        n_refined=0,
        n_rejected=0,
    )


def _combination_report() -> CombinationReport:
    now = datetime(2026, 7, 16, tzinfo=UTC)
    return CombinationReport(
        cycle_id="sdk-combination",
        regime_trail_id="trail",
        universe_id="universe",
        data_fingerprint="data",
        started_at=now,
        finished_at=now,
        recipe=CombinationRecipe.build(
            method=CombinationMethod.RANK_AGGREGATE,
            components=["rank(close)", "rank(volume)"],
        ),
        basket_metrics=FactorThumbnail(
            factor_id="basket",
            expression="basket",
            decision="reject",
        ),
    )


def test_sdk_validation_threads_only_selected_pack_fields(monkeypatch) -> None:
    pack = load_market_pack("us_equities_daily")
    report = _validation_report()
    observed = None

    def execute(argv, *, dsl_fields, emit_output):
        nonlocal observed
        observed = dsl_fields
        return 0, [report]

    monkeypatch.setattr("scripts.validate_strict._execute_validation", execute)

    assert sdk.run_validation(pack.market_id, sdk.ValidationRequest()) is report
    assert observed == pack.dsl_fields == frozenset()


def test_sdk_combination_threads_only_selected_pack_fields(monkeypatch) -> None:
    pack = load_market_pack("hk_ipo")
    report = _combination_report()
    observed = None

    def execute(argv, *, dsl_fields, emit_output):
        nonlocal observed
        observed = dsl_fields
        return 0, report

    monkeypatch.setattr("scripts.combine_factors._execute_combination", execute)

    assert sdk.combine(pack.market_id, sdk.CombinationRequest()) is report
    assert observed == pack.dsl_fields


def test_sdk_plan_uses_registered_pack() -> None:
    director_plan = sdk.plan("us_equities_daily")

    assert director_plan.market == "us_equities_daily"
    assert director_plan.selected_topic_id == "us_equities_daily_price_volume"
