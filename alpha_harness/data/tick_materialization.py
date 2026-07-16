"""Deterministic planning and QA for HK IPO raw-tick materialization."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from pydantic import BaseModel

from alpha_harness.reports.research_task import (
    ResearchTaskIssue,
    ResearchTaskIssueSeverity,
    ResearchTaskReport,
    ResearchTaskStatus,
)

EXECUTOR_NAME = "raw_tick_materialization_plan"
EXECUTION_EXECUTOR_NAME = "raw_tick_materialization_execute"
TARGET_TABLE = "micro_features_intraday_v1_candidate"
SOURCE_TABLE = "tick_events_ext"
EXPECTED_FEATURES = (
    "first_hour_n_trades",
    "first_hour_tick_volume",
    "first_hour_ofi",
    "first_hour_rel_spread",
    "first_hour_realized_vol",
    "first_hour_n_quotes",
    "opening_auction_trade_share",
    "first_hour_spread_shock",
    "first_hour_liquidity_withdrawal",
)
EXPECTED_ROW_COUNT = 7118
EXPECTED_STOCK_COUNT = 77
_TEMPLATE_FIELDS = ("PROJECT", "DATASET", "END_DATE")
_FORBIDDEN_SQL = ("INSERT", "UPDATE", "DELETE", "MERGE", "DROP", "ALTER", "TRUNCATE")
_MUTATING_SQL = ("CREATE", *_FORBIDDEN_SQL)


class BigQueryDryRunEstimate(BaseModel):
    """BigQuery planner output without executing a write job."""

    total_bytes_processed: int
    total_bytes_billed: int = 0


class MaterializationExecutionMetadata(BaseModel):
    """Metadata returned after BigQuery has completed the guarded DDL."""

    table_id: str
    expires_at: datetime | None = None


DryRunRunner = Callable[[str], BigQueryDryRunEstimate]
QueryRunner = Callable[[str], Sequence[dict[str, Any]]]
StatementRunner = Callable[[str], None]
MetadataRunner = Callable[[], MaterializationExecutionMetadata]


class MaterializationPreconditionError(RuntimeError):
    """Raised before DDL submission when the target cannot be created safely."""


def validate_bigquery_identifier(value: str, *, project: bool = False) -> str:
    """Validate identifiers before inserting them into committed SQL templates."""
    pattern = r"[A-Za-z0-9_-]+" if project else r"[A-Za-z0-9_]+"
    if re.fullmatch(pattern, value) is None:
        raise ValueError(f"invalid BigQuery identifier: {value!r}")
    return value


def render_sql_template(
    template: str,
    *,
    project: str,
    dataset: str,
    end_date: date | None = None,
) -> str:
    """Render validated fields supported by committed SQL templates."""
    values = {
        "PROJECT": project,
        "DATASET": dataset,
    }
    if end_date is not None:
        values["END_DATE"] = end_date.isoformat()
    rendered = template
    for field, value in values.items():
        rendered = rendered.replace(f"{{{{{field}}}}}", value)
    unresolved = re.findall(r"\{\{([A-Z_]+)\}\}", rendered)
    if unresolved:
        raise ValueError(f"unresolved SQL template fields: {sorted(set(unresolved))}")
    return rendered


def validate_materialization_sql(sql: str, *, project: str, dataset: str) -> None:
    """Fail closed unless SQL matches the committed staging-table contract."""
    upper = sql.upper()
    target = f"`{project}.{dataset}.{TARGET_TABLE}`"
    source = f"`{project}.{dataset}.{SOURCE_TABLE}`"
    if f"CREATE TABLE {target}" not in sql:
        raise ValueError(f"materialization must target {target}")
    if source not in sql:
        raise ValueError(f"materialization must read {source}")
    for statement in _FORBIDDEN_SQL:
        if re.search(rf"\b{statement}\b", upper):
            raise ValueError(f"forbidden SQL statement: {statement}")
    if "scope = 'target'" not in sql or "value > 0" not in sql:
        raise ValueError("materialization must enforce target scope and positive tick values")
    if 'TIME(time, "Asia/Hong_Kong")' not in sql:
        raise ValueError("materialization must derive sessions in Asia/Hong_Kong")
    if "ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING" not in sql:
        raise ValueError("rolling baselines must exclude the current row")
    if "expiration_timestamp = TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)" not in sql:
        raise ValueError("candidate materialization must expire after 7 days")
    missing = [feature for feature in EXPECTED_FEATURES if feature not in sql]
    if missing:
        raise ValueError(f"materialization is missing contracted features: {missing}")


def materialization_query_body(sql: str, *, project: str, dataset: str) -> str:
    """Return the SELECT body so BigQuery estimates source scans, not DDL metadata."""
    target = re.escape(f"`{project}.{dataset}.{TARGET_TABLE}`")
    pattern = rf"\bCREATE\s+TABLE\s+{target}\s+OPTIONS\s*\(.*?\)\s+AS\s+"
    match = re.search(pattern, sql, flags=re.IGNORECASE | re.DOTALL)
    if match is None:
        raise ValueError("unable to isolate materialization SELECT body")
    return sql[match.end() :]


def materialization_sql_sha256(sql: str) -> str:
    """Return the approval identity for a fully rendered materialization statement."""
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def validate_read_only_sql(sql: str, *, required_table: str) -> None:
    """Reject mutation tokens and require the expected fully qualified table."""
    upper = sql.upper()
    for statement in _MUTATING_SQL:
        if re.search(rf"\b{statement}\b", upper):
            raise ValueError(f"read-only SQL contains forbidden statement: {statement}")
    if required_table not in sql:
        raise ValueError(f"read-only SQL must reference {required_table}")


def validate_execution_approval(
    *,
    plan_report: ResearchTaskReport,
    rendered_sql: str,
    approved_sql_sha256: str,
    acknowledge_external_scan_cost_unknown: bool,
    project: str,
    dataset: str,
    end_date: date,
) -> None:
    """Bind execution to an exact prior plan artifact and explicit risk acknowledgement."""
    validate_materialization_sql(rendered_sql, project=project, dataset=dataset)
    sql_hash = materialization_sql_sha256(rendered_sql)
    expected_target = f"{project}.{dataset}.{TARGET_TABLE}"
    if plan_report.executor != EXECUTOR_NAME:
        raise ValueError("approval artifact is not a raw-tick materialization plan")
    if plan_report.status not in {
        ResearchTaskStatus.PASSED,
        ResearchTaskStatus.REVIEW_REQUIRED,
    }:
        raise ValueError("blocked or failed materialization plans cannot authorize execution")
    if plan_report.summary.get("write_executed") is not False:
        raise ValueError("approval artifact must record write_executed=false")
    if plan_report.summary.get("target_table") != expected_target:
        raise ValueError("approval artifact target does not match the execution target")
    if plan_report.summary.get("end_date") != end_date.isoformat():
        raise ValueError("approval artifact end date does not match the execution end date")
    if plan_report.summary.get("sql_sha256") != sql_hash:
        raise ValueError("committed SQL no longer matches the approval artifact")
    if plan_report.summary.get("cost_estimate_complete") is not False:
        raise ValueError("approval artifact must record incomplete external scan cost")
    if approved_sql_sha256 != sql_hash:
        raise ValueError("--approve-sql-sha256 does not match the rendered SQL")
    if not acknowledge_external_scan_cost_unknown:
        raise ValueError("external scan cost uncertainty must be acknowledged explicitly")


def execute_raw_tick_materialization(
    *,
    task_id: str,
    project: str,
    dataset: str,
    end_date: date,
    rendered_sql: str,
    post_qa_sql: str,
    statement_runner: StatementRunner,
    metadata_runner: MetadataRunner,
    query_runner: QueryRunner,
    max_bytes_billed: int,
    started_at: datetime | None = None,
) -> ResearchTaskReport:
    """Execute an already-approved DDL once and fail closed on post-write QA."""
    if max_bytes_billed <= 0:
        raise ValueError("max_bytes_billed must be > 0")
    validate_materialization_sql(rendered_sql, project=project, dataset=dataset)
    validate_read_only_sql(
        post_qa_sql,
        required_table=f"`{project}.{dataset}.{TARGET_TABLE}`",
    )
    started = started_at or datetime.now(UTC)
    sql_hash = materialization_sql_sha256(rendered_sql)
    target = f"{project}.{dataset}.{TARGET_TABLE}"
    base_summary: dict[str, str | int | float | bool] = {
        "sql_sha256": sql_hash,
        "target_table": target,
        "end_date": end_date.isoformat(),
        "max_bytes_billed": max_bytes_billed,
        "write_executed": False,
    }
    try:
        statement_runner(rendered_sql)
    except MaterializationPreconditionError as exc:
        return ResearchTaskReport(
            task_id=task_id,
            executor=EXECUTION_EXECUTOR_NAME,
            status=ResearchTaskStatus.FAILED,
            started_at=started,
            finished_at=datetime.now(UTC),
            issues=[
                ResearchTaskIssue(
                    code="materialization_precondition_failed",
                    severity=ResearchTaskIssueSeverity.BLOCKING,
                    detail=str(exc),
                )
            ],
            summary=base_summary,
        )
    except Exception as exc:
        base_summary["write_executed"] = "unknown"
        return ResearchTaskReport(
            task_id=task_id,
            executor=EXECUTION_EXECUTOR_NAME,
            status=ResearchTaskStatus.FAILED,
            started_at=started,
            finished_at=datetime.now(UTC),
            issues=[
                ResearchTaskIssue(
                    code="materialization_write_state_unknown",
                    severity=ResearchTaskIssueSeverity.BLOCKING,
                    detail=str(exc),
                )
            ],
            summary=base_summary,
            notes=(
                "DDL submission did not complete cleanly. Inspect the exact target and job "
                "state before retrying; CREATE TABLE makes blind retry fail closed."
            ),
        )

    base_summary["write_executed"] = True
    issues: list[ResearchTaskIssue] = []
    metadata: MaterializationExecutionMetadata | None = None
    try:
        metadata = metadata_runner()
        base_summary["table_id"] = metadata.table_id
        if metadata.expires_at is not None:
            base_summary["expires_at"] = metadata.expires_at.isoformat()
    except Exception as exc:
        issues.append(
            ResearchTaskIssue(
                code="materialization_metadata_error",
                severity=ResearchTaskIssueSeverity.BLOCKING,
                detail=str(exc),
            )
        )
    try:
        rows = list(query_runner(post_qa_sql))
        if len(rows) != 1:
            raise ValueError(f"post-write QA returned {len(rows)} rows; expected exactly one")
        row = rows[0]
        row_count = int(row.get("row_count") or 0)
        stock_count = int(row.get("stock_count") or 0)
        duplicate_key_rows = int(row.get("duplicate_key_rows") or 0)
        post_end_date_rows = int(row.get("post_end_date_rows") or 0)
        no_feature_rows = int(row.get("no_first_hour_feature_rows") or 0)
    except Exception as exc:
        return ResearchTaskReport(
            task_id=task_id,
            executor=EXECUTION_EXECUTOR_NAME,
            status=ResearchTaskStatus.BLOCKED,
            started_at=started,
            finished_at=datetime.now(UTC),
            issues=[
                *issues,
                ResearchTaskIssue(
                    code="post_write_qa_error",
                    severity=ResearchTaskIssueSeverity.BLOCKING,
                    detail=str(exc),
                ),
            ],
            summary=base_summary,
            notes="The candidate table exists, but acceptance failed closed.",
        )

    base_summary.update(
        {
            "row_count": row_count,
            "stock_count": stock_count,
            "duplicate_key_rows": duplicate_key_rows,
            "post_end_date_rows": post_end_date_rows,
            "no_first_hour_feature_rows": no_feature_rows,
            "min_date": str(row.get("min_date") or ""),
            "max_date": str(row.get("max_date") or ""),
        }
    )
    if metadata is not None and metadata.table_id != target:
        issues.append(
            ResearchTaskIssue(
                code="unexpected_materialization_target",
                severity=ResearchTaskIssueSeverity.BLOCKING,
                detail=f"expected {target}, observed {metadata.table_id}",
            )
        )
    if metadata is not None and metadata.expires_at is None:
        issues.append(
            ResearchTaskIssue(
                code="missing_table_expiration",
                severity=ResearchTaskIssueSeverity.BLOCKING,
                detail="BigQuery did not report an expiration timestamp for the candidate table.",
            )
        )
    elif (
        metadata is not None
        and metadata.expires_at is not None
        and (metadata.expires_at.tzinfo is None or metadata.expires_at.utcoffset() is None)
    ):
        issues.append(
            ResearchTaskIssue(
                code="invalid_table_expiration",
                severity=ResearchTaskIssueSeverity.BLOCKING,
                detail="candidate expiration timestamp must be timezone-aware",
            )
        )
    elif (
        metadata is not None
        and metadata.expires_at is not None
        and not started < metadata.expires_at <= started + timedelta(days=8)
    ):
        issues.append(
            ResearchTaskIssue(
                code="invalid_table_expiration",
                severity=ResearchTaskIssueSeverity.BLOCKING,
                detail=f"unexpected expiration timestamp: {metadata.expires_at.isoformat()}",
            )
        )
    if row_count != EXPECTED_ROW_COUNT:
        issues.append(
            ResearchTaskIssue(
                code="unexpected_row_count",
                severity=ResearchTaskIssueSeverity.BLOCKING,
                count=abs(EXPECTED_ROW_COUNT - row_count),
                detail=f"expected {EXPECTED_ROW_COUNT} rows, observed {row_count}",
            )
        )
    if stock_count != EXPECTED_STOCK_COUNT:
        issues.append(
            ResearchTaskIssue(
                code="unexpected_stock_coverage",
                severity=ResearchTaskIssueSeverity.BLOCKING,
                count=abs(EXPECTED_STOCK_COUNT - stock_count),
                detail=f"expected {EXPECTED_STOCK_COUNT} stocks, observed {stock_count}",
            )
        )
    if duplicate_key_rows:
        issues.append(
            ResearchTaskIssue(
                code="duplicate_stock_dates",
                severity=ResearchTaskIssueSeverity.BLOCKING,
                count=duplicate_key_rows,
            )
        )
    if post_end_date_rows:
        issues.append(
            ResearchTaskIssue(
                code="post_end_date_rows",
                severity=ResearchTaskIssueSeverity.BLOCKING,
                count=post_end_date_rows,
            )
        )
    if no_feature_rows:
        issues.append(
            ResearchTaskIssue(
                code="missing_first_hour_features",
                severity=ResearchTaskIssueSeverity.WARNING,
                count=no_feature_rows,
                detail="Calendar rows without any first-hour tick-derived feature need review.",
            )
        )
    has_blocking = any(issue.severity is ResearchTaskIssueSeverity.BLOCKING for issue in issues)
    return ResearchTaskReport(
        task_id=task_id,
        executor=EXECUTION_EXECUTOR_NAME,
        status=(
            ResearchTaskStatus.BLOCKED
            if has_blocking
            else ResearchTaskStatus.REVIEW_REQUIRED
            if issues
            else ResearchTaskStatus.PASSED
        ),
        started_at=started,
        finished_at=datetime.now(UTC),
        issues=issues,
        summary=base_summary,
        notes="Candidate materialization is temporary and must not be treated as production data.",
    )


def build_raw_tick_materialization_report(
    *,
    task_id: str,
    project: str,
    dataset: str,
    end_date: date,
    materialization_template: str,
    qa_template: str,
    dry_run_runner: DryRunRunner,
    query_runner: QueryRunner,
    started_at: datetime | None = None,
) -> ResearchTaskReport:
    """Validate and dry-run a write plan, then execute only its read-only QA query."""
    started = started_at or datetime.now(UTC)
    materialization_sql = render_sql_template(
        materialization_template,
        project=project,
        dataset=dataset,
        end_date=end_date,
    )
    qa_sql = render_sql_template(
        qa_template,
        project=project,
        dataset=dataset,
        end_date=end_date,
    )
    validate_materialization_sql(materialization_sql, project=project, dataset=dataset)
    validate_read_only_sql(
        qa_sql,
        required_table=f"`{project}.{dataset}.{SOURCE_TABLE}`",
    )

    estimate = dry_run_runner(
        materialization_query_body(materialization_sql, project=project, dataset=dataset)
    )
    qa_rows = list(query_runner(qa_sql))
    invalid_rows = 0
    evidence: list[dict[str, Any]] = []
    for row in qa_rows:
        count = int(row.get("nonpositive_value_rows") or 0)
        if count < 0:
            raise ValueError("nonpositive_value_rows cannot be negative")
        invalid_rows += count
        if len(evidence) < 25:
            evidence.append(dict(row))

    issues = [
        ResearchTaskIssue(
            code="external_scan_cost_not_estimated",
            severity=ResearchTaskIssueSeverity.WARNING,
            detail=(
                "BigQuery dry-run does not provide a complete scan estimate for the external "
                "tick table. The planner byte count is recorded but must not be used as a "
                "write budget."
            ),
        ),
        ResearchTaskIssue(
            code="unsupported_opening_auction_order_imbalance",
            severity=ResearchTaskIssueSeverity.WARNING,
            detail=(
                "TRADE/BID/ASK best-quote events do not expose auction order-book depth; "
                "v1 reports auction trade share instead of inventing an imbalance measure."
            ),
        ),
        ResearchTaskIssue(
            code="unsupported_quote_recovery_speed",
            severity=ResearchTaskIssueSeverity.WARNING,
            detail=(
                "A recovery-speed definition needs a separately reviewed shock/recovery event "
                "contract; v1 materializes point-in-time spread shock only."
            ),
        ),
    ]
    if invalid_rows:
        issues.append(
            ResearchTaskIssue(
                code="nonpositive_tick_values",
                severity=ResearchTaskIssueSeverity.WARNING,
                count=invalid_rows,
                detail=(
                    "Rows are excluded by value > 0. Evidence is capped at the 25 largest "
                    "stock/date/event-type groups."
                ),
                evidence=evidence,
            )
        )

    sql_hash = materialization_sql_sha256(materialization_sql)
    return ResearchTaskReport(
        task_id=task_id,
        executor=EXECUTOR_NAME,
        status=ResearchTaskStatus.REVIEW_REQUIRED if issues else ResearchTaskStatus.PASSED,
        started_at=started,
        finished_at=datetime.now(UTC),
        issues=issues,
        summary={
            "sql_sha256": sql_hash,
            "target_table": f"{project}.{dataset}.{TARGET_TABLE}",
            "source_table": f"{project}.{dataset}.{SOURCE_TABLE}",
            "end_date": end_date.isoformat(),
            "feature_count": len(EXPECTED_FEATURES),
            "bigquery_dry_run_bytes_processed": estimate.total_bytes_processed,
            "bigquery_dry_run_bytes_billed": estimate.total_bytes_billed,
            "cost_estimate_complete": False,
            "qa_nonpositive_rows": invalid_rows,
            "qa_affected_groups": len(qa_rows),
            "write_executed": False,
            "point_in_time_baseline": True,
        },
        notes=(
            "The materialization statement was validated and dry-run only. No BigQuery table "
            "was created or replaced. Any write requires a separate operator-approved path."
        ),
    )


def template_fields() -> tuple[str, ...]:
    """Expose the intentionally small template surface for audits and tests."""
    return _TEMPLATE_FIELDS
