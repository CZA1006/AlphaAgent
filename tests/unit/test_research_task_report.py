from __future__ import annotations

import json
from datetime import UTC, datetime

from alpha_harness.reports.research_task import (
    ResearchTaskIssue,
    ResearchTaskIssueSeverity,
    ResearchTaskReport,
    ResearchTaskReportWriter,
    ResearchTaskStatus,
    read_index,
)


def _report(task_id: str = "task-1") -> ResearchTaskReport:
    now = datetime.now(UTC)
    return ResearchTaskReport(
        task_id=task_id,
        executor="event_truth_audit",
        status=ResearchTaskStatus.REVIEW_REQUIRED,
        started_at=now,
        finished_at=now,
        issues=[
            ResearchTaskIssue(
                code="review_backlog",
                severity=ResearchTaskIssueSeverity.WARNING,
                count=3,
            ),
            ResearchTaskIssue(
                code="missing_evidence",
                severity=ResearchTaskIssueSeverity.BLOCKING,
                count=1,
            ),
        ],
    )


def test_task_report_counts_issue_severity() -> None:
    report = _report()
    assert report.blocking_issue_count == 1
    assert report.review_issue_count == 3


def test_task_writer_round_trips_and_upserts_index(tmp_path) -> None:
    writer = ResearchTaskReportWriter(tmp_path)
    path = writer.write(_report("same"))
    writer.write(_report("same"))

    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = read_index(tmp_path)
    assert payload["schema_version"] == 1
    assert payload["task_id"] == "same"
    assert len(rows) == 1
    assert rows[0]["blocking_issue_count"] == 1
    assert rows[0]["review_issue_count"] == 3
