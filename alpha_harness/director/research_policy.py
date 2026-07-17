"""Deterministic post-run policy for autonomous research loops."""

from __future__ import annotations

from collections import Counter
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from alpha_harness.markets.models import MarketPack, PostRunTransition
from alpha_harness.markets.registry import load_market_pack


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
    candidate_source: str = "propose"
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
    replay_survived: int = 0
    total_rejected: int = 0
    rejected_by_gate: dict[str, int] = Field(default_factory=dict)


class ResearchPostRunPolicy:
    """Map deterministic validation outcomes to the next research action."""

    def decide(
        self,
        summary: ResearchRunSummary,
        *,
        pack: MarketPack | None = None,
    ) -> PostRunDecision:
        if pack is None:
            try:
                pack = load_market_pack(summary.market)
            except LookupError as exc:
                raise ValueError(f"unsupported research market: {summary.market}") from exc
        if pack.market_id != summary.market:
            raise ValueError(
                f"market summary mismatch: pack={pack.market_id!r}, summary={summary.market!r}"
            )

        total_promoted = sum(
            report.n_promoted
            for report in summary.validation_reports
            if report.candidate_source != "replay_promoted"
        )
        replay_survived = sum(
            report.n_promoted
            for report in summary.validation_reports
            if report.candidate_source == "replay_promoted"
        )
        total_rejected = sum(report.n_rejected for report in summary.validation_reports)
        gates = Counter[str]()
        for report in summary.validation_reports:
            gates.update(report.rejected_by_gate)
        rejected_by_gate = dict(sorted(gates.items()))
        evidence = self._evidence(
            summary,
            total_promoted,
            replay_survived,
            total_rejected,
            rejected_by_gate,
        )

        if summary.status == "failed":
            return PostRunDecision(
                action=NextResearchAction.STOP_FAILED,
                rationale="Validation execution failed; stop before spending more budget.",
                evidence=evidence,
                total_promoted=total_promoted,
                replay_survived=replay_survived,
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
                replay_survived=replay_survived,
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
                replay_survived=replay_survived,
                total_rejected=total_rejected,
                rejected_by_gate=rejected_by_gate,
            )
        after_topic = pack.post_run_transitions.after_topic.get(summary.selected_topic_id)
        if after_topic is not None:
            return self._transition_decision(
                after_topic,
                summary=summary,
                evidence=evidence,
                total_promoted=total_promoted,
                replay_survived=replay_survived,
                total_rejected=total_rejected,
                rejected_by_gate=rejected_by_gate,
            )
        if total_promoted > 0:
            transition = pack.post_run_transitions.on_promotion.get(summary.selected_topic_id)
            if transition is not None:
                return self._transition_decision(
                    transition,
                    summary=summary,
                    evidence=evidence,
                    total_promoted=total_promoted,
                    replay_survived=replay_survived,
                    total_rejected=total_rejected,
                    rejected_by_gate=rejected_by_gate,
                )
        if total_promoted == 0 and self._event_truth_blocked(rejected_by_gate):
            transition = pack.post_run_transitions.on_data_gap.get(summary.selected_topic_id)
            if transition is not None:
                return self._transition_decision(
                    transition,
                    summary=summary,
                    evidence=evidence,
                    total_promoted=total_promoted,
                    replay_survived=replay_survived,
                    total_rejected=total_rejected,
                    rejected_by_gate=rejected_by_gate,
                )
        return PostRunDecision(
            action=NextResearchAction.CONTINUE_TOPIC,
            next_topic_id=summary.selected_topic_id,
            rationale="Validation remains productive enough to continue the selected topic.",
            evidence=evidence,
            total_promoted=total_promoted,
            replay_survived=replay_survived,
            total_rejected=total_rejected,
            rejected_by_gate=rejected_by_gate,
        )

    def _transition_decision(
        self,
        transition: PostRunTransition,
        *,
        summary: ResearchRunSummary,
        evidence: list[str],
        total_promoted: int,
        replay_survived: int,
        total_rejected: int,
        rejected_by_gate: dict[str, int],
    ) -> PostRunDecision:
        transition_evidence = list(evidence)
        if transition.include_task_counts:
            blocking = sum(report.blocking_issue_count for report in summary.task_reports)
            review = sum(report.review_issue_count for report in summary.task_reports)
            transition_evidence.extend([f"task_blocking={blocking}", f"task_review={review}"])
        return PostRunDecision(
            action=NextResearchAction(transition.action),
            next_topic_id=transition.next_topic_id,
            rationale=transition.rationale,
            evidence=transition_evidence,
            total_promoted=total_promoted,
            replay_survived=replay_survived,
            total_rejected=total_rejected,
            rejected_by_gate=rejected_by_gate,
        )

    def _evidence(
        self,
        summary: ResearchRunSummary,
        total_promoted: int,
        replay_survived: int,
        total_rejected: int,
        rejected_by_gate: dict[str, int],
    ) -> list[str]:
        evidence = [
            f"reports={len(summary.validation_reports)}",
            f"promoted={total_promoted}",
            f"replay_survived={replay_survived}",
            f"rejected={total_rejected}",
        ]
        if rejected_by_gate:
            gate_text = ", ".join(f"{gate}={count}" for gate, count in rejected_by_gate.items())
            evidence.append(f"rejected_by_gate: {gate_text}")
        if summary.data_gap_names:
            evidence.append(f"data_gaps: {', '.join(summary.data_gap_names)}")
        return evidence

    def _event_truth_blocked(self, rejected_by_gate: dict[str, int]) -> bool:
        # Weak IC is a research result, not evidence that event truth is broken.
        # Open the data-review path only for failures that directly indicate
        # unusable cross-sections or insufficient observations.
        data_sensitive_failures = {
            "missing_metric",
            "data_insufficient",
        }
        return bool(data_sensitive_failures.intersection(rejected_by_gate))


def validation_report_summary_from_payload(payload: dict[str, Any]) -> ValidationReportSummary:
    """Build a compact policy summary from a validation report or index row."""
    return ValidationReportSummary(
        cycle_id=str(payload.get("cycle_id", "unknown")),
        candidate_source=str(payload.get("candidate_source", "propose")),
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
