from __future__ import annotations

import pytest

from alpha_harness.director import (
    NextResearchAction,
    ResearchPostRunPolicy,
    ResearchRunSummary,
    ResearchTaskReportSummary,
    ValidationReportSummary,
    validation_report_summary_from_payload,
)


def test_policy_switches_successful_event_microstructure_to_cost_realism() -> None:
    decision = ResearchPostRunPolicy().decide(
        ResearchRunSummary(
            market="hk_ipo",
            selected_topic_id="hk_ipo_event_conditioned_microstructure",
            status="completed",
            validation_reports=[
                ValidationReportSummary(
                    cycle_id="cycle-1",
                    n_proposals=5,
                    n_promoted=3,
                    n_rejected=2,
                    rejected_by_gate={"threshold_rank_ic": 1, "missing_metric": 1},
                )
            ],
            data_gap_names=["event_terms_needs_review"],
        )
    )

    assert decision.action == NextResearchAction.SWITCH_TOPIC
    assert decision.next_topic_id == "hk_ipo_cost_realism_oos"
    assert decision.total_promoted == 3
    assert decision.rejected_by_gate == {"missing_metric": 1, "threshold_rank_ic": 1}
    assert "promoted=3" in decision.evidence


def test_policy_opens_data_review_when_no_promotions_and_data_sensitive_failures() -> None:
    decision = ResearchPostRunPolicy().decide(
        ResearchRunSummary(
            market="hk_ipo",
            selected_topic_id="hk_ipo_event_conditioned_microstructure",
            status="completed",
            validation_reports=[
                ValidationReportSummary(
                    cycle_id="cycle-1",
                    n_proposals=5,
                    n_promoted=0,
                    n_rejected=5,
                    rejected_by_gate={"missing_metric": 3, "threshold_rank_ic": 2},
                )
            ],
        )
    )

    assert decision.action == NextResearchAction.OPEN_DATA_REVIEW
    assert decision.next_topic_id == "hk_ipo_event_truth_review"


def test_policy_stops_failed_or_no_progress_runs() -> None:
    failed = ResearchPostRunPolicy().decide(
        ResearchRunSummary(
            market="hk_ipo",
            selected_topic_id="hk_ipo_event_conditioned_microstructure",
            status="failed",
        )
    )
    no_progress = ResearchPostRunPolicy().decide(
        ResearchRunSummary(
            market="hk_ipo",
            selected_topic_id="hk_ipo_event_conditioned_microstructure",
            status="no_progress",
        )
    )

    assert failed.action == NextResearchAction.STOP_FAILED
    assert no_progress.action == NextResearchAction.STOP_NO_PROGRESS


def test_policy_stops_after_bounded_cost_replay() -> None:
    decision = ResearchPostRunPolicy().decide(
        ResearchRunSummary(
            market="hk_ipo",
            selected_topic_id="hk_ipo_cost_realism_oos",
            status="completed",
            validation_reports=[
                ValidationReportSummary(
                    cycle_id="cost-1",
                    n_proposals=3,
                    n_promoted=1,
                    n_rejected=2,
                ),
            ],
        ),
    )

    assert decision.action == NextResearchAction.STOP_COMPLETED
    assert decision.next_topic_id is None


def test_policy_stops_after_event_truth_task_report() -> None:
    decision = ResearchPostRunPolicy().decide(
        ResearchRunSummary(
            market="hk_ipo",
            selected_topic_id="hk_ipo_event_truth_review",
            status="completed",
            task_reports=[
                ResearchTaskReportSummary(
                    task_id="event-audit",
                    executor="event_truth_audit",
                    status="review_required",
                    blocking_issue_count=0,
                    review_issue_count=12,
                ),
            ],
        ),
    )

    assert decision.action == NextResearchAction.STOP_COMPLETED
    assert "task_blocking=0" in decision.evidence
    assert "task_review=12" in decision.evidence


def test_policy_stops_after_raw_tick_materialization_plan() -> None:
    decision = ResearchPostRunPolicy().decide(
        ResearchRunSummary(
            market="hk_ipo",
            selected_topic_id="hk_ipo_raw_tick_intraday_features",
            status="completed",
            task_reports=[
                ResearchTaskReportSummary(
                    task_id="tick-plan",
                    executor="raw_tick_materialization_plan",
                    status="review_required",
                    blocking_issue_count=0,
                    review_issue_count=364770,
                ),
            ],
        ),
    )

    assert decision.action == NextResearchAction.STOP_COMPLETED
    assert decision.next_topic_id is None
    assert "task_blocking=0" in decision.evidence
    assert "task_review=364770" in decision.evidence


def test_policy_treats_dry_run_as_planned_not_no_progress() -> None:
    decision = ResearchPostRunPolicy().decide(
        ResearchRunSummary(
            market="hk_ipo",
            selected_topic_id="hk_ipo_event_conditioned_microstructure",
            status="planned",
        )
    )

    assert decision.action == NextResearchAction.CONTINUE_TOPIC
    assert decision.next_topic_id == "hk_ipo_event_conditioned_microstructure"


def test_validation_report_summary_accepts_report_payload_or_index_row() -> None:
    summary = validation_report_summary_from_payload(
        {
            "cycle_id": "cycle-1",
            "n_proposals": 5,
            "n_promoted": 2,
            "n_rejected": 3,
            "n_rejected_by_gate": {"threshold_ic": 2},
        }
    )
    fallback = validation_report_summary_from_payload(
        {
            "cycle_id": "cycle-2",
            "n_promoted": 1,
            "n_rejected": 4,
        }
    )

    assert summary.rejected_by_gate == {"threshold_ic": 2}
    assert fallback.n_proposals == 0
    assert fallback.n_promoted == 1


def test_policy_rejects_unknown_market() -> None:
    with pytest.raises(ValueError, match="unsupported research market"):
        ResearchPostRunPolicy().decide(
            ResearchRunSummary(
                market="crypto",
                selected_topic_id="crypto_topic",
                status="completed",
            )
        )
