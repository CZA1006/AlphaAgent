"""Deterministic HK IPO event-truth audit rules."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from alpha_harness.reports.research_task import (
    ResearchTaskIssue,
    ResearchTaskIssueSeverity,
    ResearchTaskReport,
    ResearchTaskStatus,
)

QueryRunner = Callable[[str], list[dict[str, Any]]]


@dataclass(frozen=True)
class EventTruthCheck:
    code: str
    sql: str
    severity: ResearchTaskIssueSeverity
    count_field: str = "issue_count"


def build_event_truth_checks(project: str, dataset: str) -> list[EventTruthCheck]:
    prefix = f"`{project}.{dataset}"
    return [
        EventTruthCheck(
            code="review_backlog",
            severity=ResearchTaskIssueSeverity.WARNING,
            count_field="term_count",
            sql=f"""
                SELECT review_reason, event_type, COUNT(*) AS term_count
                FROM {prefix}.ipo_event_terms_needs_review`
                GROUP BY review_reason, event_type
                ORDER BY term_count DESC, review_reason, event_type
            """,
        ),
        EventTruthCheck(
            code="missing_curated_source_evidence",
            severity=ResearchTaskIssueSeverity.BLOCKING,
            sql=f"""
                SELECT COUNT(*) AS issue_count
                FROM {prefix}.ipo_event_dates_curated`
                WHERE primary_source_doc_id IS NULL
                   OR primary_source_url IS NULL
                   OR primary_source_text IS NULL
                   OR primary_source_text = ''
            """,
        ),
        EventTruthCheck(
            code="implausible_curated_event_date",
            severity=ResearchTaskIssueSeverity.BLOCKING,
            sql=f"""
                SELECT COUNT(*) AS issue_count
                FROM {prefix}.ipo_event_dates_curated`
                WHERE (
                    event_type IN (
                        'stabilization_start', 'stabilization_trade',
                        'stabilization_end', 'greenshoe_expiry',
                        'greenshoe_full_exercise', 'greenshoe_partial_exercise',
                        'greenshoe_lapse', 'cornerstone_lockup_expiry',
                        'pre_ipo_investor_unlock'
                    )
                    AND event_date < listing_date
                ) OR (
                    event_type IN ('stabilization_end', 'greenshoe_expiry')
                    AND event_date < DATE_ADD(listing_date, INTERVAL 20 DAY)
                )
            """,
        ),
        EventTruthCheck(
            code="event_feature_alignment",
            severity=ResearchTaskIssueSeverity.BLOCKING,
            sql=f"""
                WITH p AS (
                    SELECT stock_code, date AS trading_date
                    FROM {prefix}.ipo_daily_prices`
                ), f AS (
                    SELECT stock_code, date AS trading_date
                    FROM {prefix}.ipo_event_features_daily`
                )
                SELECT COUNT(*) AS issue_count
                FROM (
                    SELECT p.stock_code, p.trading_date
                    FROM p LEFT JOIN f USING (stock_code, trading_date)
                    WHERE f.stock_code IS NULL
                    UNION ALL
                    SELECT f.stock_code, f.trading_date
                    FROM f LEFT JOIN p USING (stock_code, trading_date)
                    WHERE p.stock_code IS NULL
                )
            """,
        ),
        EventTruthCheck(
            code="document_coverage_gap",
            severity=ResearchTaskIssueSeverity.WARNING,
            sql=f"""
                WITH panel AS (
                    SELECT DISTINCT stock_code FROM {prefix}.ipo_daily_prices`
                ), coverage AS (
                    SELECT
                        stock_code,
                        COUNTIF(document_type = 'prospectus') > 0 AS has_prospectus,
                        COUNTIF(
                            document_type = 'allotment_results_announcement'
                        ) > 0 AS has_allotment
                    FROM {prefix}.hkex_document_registry_curated`
                    GROUP BY stock_code
                )
                SELECT 'prospectus' AS document_type, COUNT(*) AS issue_count
                FROM panel LEFT JOIN coverage USING (stock_code)
                WHERE NOT COALESCE(has_prospectus, FALSE)
                UNION ALL
                SELECT 'allotment_results_announcement', COUNT(*)
                FROM panel LEFT JOIN coverage USING (stock_code)
                WHERE NOT COALESCE(has_allotment, FALSE)
            """,
        ),
    ]


def run_event_truth_audit(
    *,
    task_id: str,
    project: str,
    dataset: str,
    query_runner: QueryRunner,
    started_at: datetime | None = None,
) -> ResearchTaskReport:
    started = started_at or datetime.now(UTC)
    issues: list[ResearchTaskIssue] = []
    checks_run = 0
    for check in build_event_truth_checks(project, dataset):
        checks_run += 1
        try:
            rows = query_runner(check.sql)
            count = sum(_coerce_count(row.get(check.count_field)) for row in rows)
        except Exception as exc:
            issues.append(
                ResearchTaskIssue(
                    code=f"{check.code}_query_error",
                    severity=ResearchTaskIssueSeverity.BLOCKING,
                    detail=str(exc),
                ),
            )
            continue
        if count > 0:
            issues.append(
                ResearchTaskIssue(
                    code=check.code,
                    severity=check.severity,
                    count=count,
                    evidence=rows[:20],
                ),
            )

    query_failed = any(issue.code.endswith("_query_error") for issue in issues)
    blocking = any(issue.severity is ResearchTaskIssueSeverity.BLOCKING for issue in issues)
    warning = any(issue.severity is ResearchTaskIssueSeverity.WARNING for issue in issues)
    if query_failed:
        status = ResearchTaskStatus.FAILED
    elif blocking:
        status = ResearchTaskStatus.BLOCKED
    elif warning:
        status = ResearchTaskStatus.REVIEW_REQUIRED
    else:
        status = ResearchTaskStatus.PASSED
    return ResearchTaskReport(
        task_id=task_id,
        executor="event_truth_audit",
        status=status,
        started_at=started,
        finished_at=datetime.now(UTC),
        issues=issues,
        summary={
            "checks_run": checks_run,
            "blocking_issue_count": sum(
                issue.count
                for issue in issues
                if issue.severity is ResearchTaskIssueSeverity.BLOCKING
            ),
            "review_issue_count": sum(
                issue.count
                for issue in issues
                if issue.severity is ResearchTaskIssueSeverity.WARNING
            ),
        },
    )


def _coerce_count(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float | str):
        return int(value)
    raise ValueError(f"unsupported count value: {value!r}")
