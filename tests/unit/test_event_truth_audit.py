from __future__ import annotations

import json

import pytest

import scripts.audit_hk_ipo_event_truth as audit_script
from alpha_harness.data.event_truth import (
    build_event_truth_checks,
    run_event_truth_audit,
)
from alpha_harness.reports.research_task import ResearchTaskStatus, read_index


def test_event_truth_queries_are_read_only() -> None:
    checks = build_event_truth_checks("project", "dataset")
    for check in checks:
        sql = check.sql.upper()
        assert "SELECT" in sql
        assert "CREATE " not in sql
        assert "INSERT " not in sql
        assert "UPDATE " not in sql
        assert "DELETE " not in sql
        assert "MERGE " not in sql
    coverage = next(check for check in checks if check.code == "document_coverage_gap")
    assert "allotment_results_announcement" in coverage.sql


def test_event_truth_audit_reports_review_backlog_without_blocking() -> None:
    responses = iter(
        [
            [{"review_reason": "low_confidence", "term_count": "3"}],
            [{"issue_count": 0}],
            [{"issue_count": 0}],
            [{"issue_count": 0}],
            [{"issue_count": 2}],
        ],
    )

    report = run_event_truth_audit(
        task_id="audit-1",
        project="project",
        dataset="dataset",
        query_runner=lambda sql: next(responses),
    )

    assert report.status is ResearchTaskStatus.REVIEW_REQUIRED
    assert report.blocking_issue_count == 0
    assert report.review_issue_count == 5
    assert {issue.code for issue in report.issues} == {
        "review_backlog",
        "document_coverage_gap",
    }


def test_event_truth_audit_fails_closed_on_query_error() -> None:
    def fail(sql: str):
        raise RuntimeError("query unavailable")

    report = run_event_truth_audit(
        task_id="audit-failed",
        project="project",
        dataset="dataset",
        query_runner=fail,
    )

    assert report.status is ResearchTaskStatus.FAILED
    assert report.blocking_issue_count == 5
    assert all(issue.code.endswith("_query_error") for issue in report.issues)


def test_event_truth_audit_fails_closed_on_malformed_count() -> None:
    responses = iter(
        [
            [{"term_count": {"unexpected": "object"}}],
            [{"issue_count": 0}],
            [{"issue_count": 0}],
            [{"issue_count": 0}],
            [{"issue_count": 0}],
        ],
    )

    report = run_event_truth_audit(
        task_id="audit-malformed",
        project="project",
        dataset="dataset",
        query_runner=lambda sql: next(responses),
    )

    assert report.status is ResearchTaskStatus.FAILED
    assert report.issues[0].code == "review_backlog_query_error"


def test_event_truth_cli_writes_typed_artifact(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    responses = iter(
        [
            [{"review_reason": "needs_review", "term_count": 4}],
            [{"issue_count": 0}],
            [{"issue_count": 0}],
            [{"issue_count": 0}],
            [{"issue_count": 0}],
        ],
    )
    monkeypatch.setattr(
        audit_script,
        "_bigquery_runner",
        lambda **kwargs: lambda sql: next(responses),
    )

    rc = audit_script.main(
        [
            "--task-id",
            "task-cli",
            "--artifact-dir",
            str(tmp_path),
            "--json",
        ],
    )

    payload = json.loads((tmp_path / "task-cli.json").read_text(encoding="utf-8"))
    assert rc == 0
    assert payload["executor"] == "event_truth_audit"
    assert payload["status"] == "review_required"
    assert read_index(tmp_path)[0]["task_id"] == "task-cli"


def test_event_truth_cli_rejects_unsafe_identifier(tmp_path) -> None:
    rc = audit_script.main(
        [
            "--task-id",
            "unsafe",
            "--artifact-dir",
            str(tmp_path),
            "--dataset",
            "dataset`; DROP TABLE x; --",
        ],
    )

    payload = json.loads((tmp_path / "unsafe.json").read_text(encoding="utf-8"))
    assert rc == 3
    assert payload["status"] == "failed"
