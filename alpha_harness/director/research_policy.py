"""Deterministic post-run policy for autonomous research loops."""

from __future__ import annotations

from collections import Counter
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class NextResearchAction(StrEnum):
    """Operator-safe action emitted after an autonomous validation run."""

    CONTINUE_TOPIC = "continue_topic"
    SWITCH_TOPIC = "switch_topic"
    OPEN_DATA_REVIEW = "open_data_review"
    STOP_COMPLETED = "stop_completed"
    STOP_FAILED = "stop_failed"
    STOP_NO_PROGRESS = "stop_no_progress"


class ValidationReportSummary(BaseModel):
    """Compact deterministic summary of one validation report."""

    cycle_id: str
    n_proposals: int = 0
    n_promoted: int = 0
    n_rejected: int = 0
    rejected_by_gate: dict[str, int] = Field(default_factory=dict)


class ResearchTaskReportSummary(BaseModel):
    """Compact deterministic summary of a non-factor research task."""

    task_id: str
    executor: str
    status: str
    blocking_issue_count: int = 0
    review_issue_count: int = 0


class ResearchRunSummary(BaseModel):
    """Inputs the post-run policy needs, without depending on CLI models."""

    market: str
    selected_topic_id: str
    status: Literal["planned", "completed", "failed", "no_progress", "stopped"]
    validation_reports: list[ValidationReportSummary] = Field(default_factory=list)
    task_reports: list[ResearchTaskReportSummary] = Field(default_factory=list)
    data_gap_names: list[str] = Field(default_factory=list)


class PostRunDecision(BaseModel):
    """Machine-readable next-step decision for the autonomous researcher."""

    action: NextResearchAction
    next_topic_id: str | None = None
    rationale: str
    evidence: list[str] = Field(default_factory=list)
    total_promoted: int = 0
    total_rejected: int = 0
    rejected_by_gate: dict[str, int] = Field(default_factory=dict)


class ResearchPostRunPolicy:
    """Map deterministic validation outcomes to the next research action."""

    def decide(self, summary: ResearchRunSummary) -> PostRunDecision:
        if summary.market != "hk_ipo":
            raise ValueError(f"unsupported research market: {summary.market}")

        total_promoted = sum(report.n_promoted for report in summary.validation_reports)
        total_rejected = sum(report.n_rejected for report in summary.validation_reports)
        gates = Counter[str]()
        for report in summary.validation_reports:
            gates.update(report.rejected_by_gate)
        rejected_by_gate = dict(sorted(gates.items()))
        evidence = self._evidence(summary, total_promoted, total_rejected, rejected_by_gate)

        if summary.status == "failed":
            return PostRunDecision(
                action=NextResearchAction.STOP_FAILED,
                rationale="Validation execution failed; stop before spending more budget.",
                evidence=evidence,
                total_promoted=total_promoted,
                total_rejected=total_rejected,
                rejected_by_gate=rejected_by_gate,
            )
        if summary.status == "planned":
            return PostRunDecision(
                action=NextResearchAction.CONTINUE_TOPIC,
                next_topic_id=summary.selected_topic_id,
                rationale="Dry-run planned the next validation command but did not execute it.",
                evidence=evidence,
                total_promoted=total_promoted,
                total_rejected=total_rejected,
                rejected_by_gate=rejected_by_gate,
            )
        if summary.status == "no_progress" or (
            not summary.validation_reports and not summary.task_reports
        ):
            return PostRunDecision(
                action=NextResearchAction.STOP_NO_PROGRESS,
                rationale=(
                    "Validation wrote no usable reports; stop and inspect execution plumbing."
                ),
                evidence=evidence,
                total_promoted=total_promoted,
                total_rejected=total_rejected,
                rejected_by_gate=rejected_by_gate,
            )
        if summary.selected_topic_id == "hk_ipo_event_truth_review" and summary.task_reports:
            blocking = sum(report.blocking_issue_count for report in summary.task_reports)
            review = sum(report.review_issue_count for report in summary.task_reports)
            return PostRunDecision(
                action=NextResearchAction.STOP_COMPLETED,
                rationale=(
                    "Event-truth audit completed; stop the bounded run for deterministic "
                    "review of blocking and backlog findings."
                ),
                evidence=[*evidence, f"task_blocking={blocking}", f"task_review={review}"],
                total_promoted=total_promoted,
                total_rejected=total_rejected,
                rejected_by_gate=rejected_by_gate,
            )
        if (
            summary.selected_topic_id == "hk_ipo_raw_tick_intraday_features"
            and summary.task_reports
        ):
            blocking = sum(report.blocking_issue_count for report in summary.task_reports)
            review = sum(report.review_issue_count for report in summary.task_reports)
            return PostRunDecision(
                action=NextResearchAction.STOP_COMPLETED,
                rationale=(
                    "Raw-tick SQL was validated and dry-run with read-only QA; stop before "
                    "any operator-approved BigQuery materialization."
                ),
                evidence=[*evidence, f"task_blocking={blocking}", f"task_review={review}"],
                total_promoted=total_promoted,
                total_rejected=total_rejected,
                rejected_by_gate=rejected_by_gate,
            )
        if (
            total_promoted > 0
            and summary.selected_topic_id == "hk_ipo_event_conditioned_microstructure"
        ):
            return PostRunDecision(
                action=NextResearchAction.SWITCH_TOPIC,
                next_topic_id="hk_ipo_cost_realism_oos",
                rationale=(
                    "Event-conditioned microstructure produced promotions; next step is "
                    "implementability, turnover, cost, and holdout stress rather than "
                    "repeating the same candidate family."
                ),
                evidence=evidence,
                total_promoted=total_promoted,
                total_rejected=total_rejected,
                rejected_by_gate=rejected_by_gate,
            )
        if summary.selected_topic_id == "hk_ipo_cost_realism_oos":
            return PostRunDecision(
                action=NextResearchAction.STOP_COMPLETED,
                rationale=(
                    "Promoted discovery candidates were replayed under the cost-stress "
                    "contract; stop this bounded run and inspect the deterministic report."
                ),
                evidence=evidence,
                total_promoted=total_promoted,
                total_rejected=total_rejected,
                rejected_by_gate=rejected_by_gate,
            )
        if total_promoted == 0 and self._event_truth_blocked(rejected_by_gate):
            return PostRunDecision(
                action=NextResearchAction.OPEN_DATA_REVIEW,
                next_topic_id="hk_ipo_event_truth_review",
                rationale=(
                    "No candidates promoted and failures point to missing metrics or event "
                    "label sparsity; inspect event truth before more factor search."
                ),
                evidence=evidence,
                total_promoted=total_promoted,
                total_rejected=total_rejected,
                rejected_by_gate=rejected_by_gate,
            )
        return PostRunDecision(
            action=NextResearchAction.CONTINUE_TOPIC,
            next_topic_id=summary.selected_topic_id,
            rationale="Validation remains productive enough to continue the selected topic.",
            evidence=evidence,
            total_promoted=total_promoted,
            total_rejected=total_rejected,
            rejected_by_gate=rejected_by_gate,
        )

    def _evidence(
        self,
        summary: ResearchRunSummary,
        total_promoted: int,
        total_rejected: int,
        rejected_by_gate: dict[str, int],
    ) -> list[str]:
        evidence = [
            f"reports={len(summary.validation_reports)}",
            f"promoted={total_promoted}",
            f"rejected={total_rejected}",
        ]
        if rejected_by_gate:
            gate_text = ", ".join(f"{gate}={count}" for gate, count in rejected_by_gate.items())
            evidence.append(f"rejected_by_gate: {gate_text}")
        if summary.data_gap_names:
            evidence.append(f"data_gaps: {', '.join(summary.data_gap_names)}")
        return evidence

    def _event_truth_blocked(self, rejected_by_gate: dict[str, int]) -> bool:
        data_sensitive_failures = {
            "missing_metric",
            "insufficient_data",
            "threshold_ic",
            "threshold_rank_ic",
        }
        return bool(data_sensitive_failures.intersection(rejected_by_gate))


def validation_report_summary_from_payload(payload: dict[str, Any]) -> ValidationReportSummary:
    """Build a compact policy summary from a validation report or index row."""
    return ValidationReportSummary(
        cycle_id=str(payload.get("cycle_id", "unknown")),
        n_proposals=int(payload.get("n_proposals") or 0),
        n_promoted=int(payload.get("n_promoted") or 0),
        n_rejected=int(payload.get("n_rejected") or 0),
        rejected_by_gate={
            str(gate): int(count)
            for gate, count in (payload.get("n_rejected_by_gate") or {}).items()
        },
    )


def research_task_report_summary_from_payload(
    payload: dict[str, Any],
) -> ResearchTaskReportSummary:
    return ResearchTaskReportSummary(
        task_id=str(payload.get("task_id", "unknown")),
        executor=str(payload.get("executor", "unknown")),
        status=str(payload.get("status", "unknown")),
        blocking_issue_count=int(payload.get("blocking_issue_count") or 0),
        review_issue_count=int(payload.get("review_issue_count") or 0),
    )
