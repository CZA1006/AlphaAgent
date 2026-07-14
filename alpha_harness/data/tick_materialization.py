"""Deterministic planning and QA for HK IPO raw-tick materialization."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from typing import Any

from pydantic import BaseModel

from alpha_harness.reports.research_task import (
    ResearchTaskIssue,
    ResearchTaskIssueSeverity,
    ResearchTaskReport,
    ResearchTaskStatus,
)

EXECUTOR_NAME = "raw_tick_materialization_plan"
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
_TEMPLATE_FIELDS = ("PROJECT", "DATASET", "END_DATE")
_FORBIDDEN_SQL = ("INSERT", "UPDATE", "DELETE", "MERGE", "DROP", "ALTER", "TRUNCATE")


class BigQueryDryRunEstimate(BaseModel):
    """BigQuery planner output without executing a write job."""

    total_bytes_processed: int
    total_bytes_billed: int = 0


DryRunRunner = Callable[[str], BigQueryDryRunEstimate]
QueryRunner = Callable[[str], Sequence[dict[str, Any]]]


def render_sql_template(
    template: str,
    *,
    project: str,
    dataset: str,
    end_date: date,
) -> str:
    """Render only the three validated fields supported by committed SQL templates."""
    values = {
        "PROJECT": project,
        "DATASET": dataset,
        "END_DATE": end_date.isoformat(),
    }
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
    if f"CREATE OR REPLACE TABLE {target}" not in sql:
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
    missing = [feature for feature in EXPECTED_FEATURES if feature not in sql]
    if missing:
        raise ValueError(f"materialization is missing contracted features: {missing}")


def materialization_query_body(sql: str, *, project: str, dataset: str) -> str:
    """Return the SELECT body so BigQuery estimates source scans, not DDL metadata."""
    target = re.escape(f"`{project}.{dataset}.{TARGET_TABLE}`")
    pattern = rf"\bCREATE\s+OR\s+REPLACE\s+TABLE\s+{target}\s+AS\s+"
    match = re.search(pattern, sql, flags=re.IGNORECASE)
    if match is None:
        raise ValueError("unable to isolate materialization SELECT body")
    return sql[match.end() :]


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
    if re.search(r"\b(CREATE|INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|TRUNCATE)\b", qa_sql.upper()):
        raise ValueError("raw-tick QA SQL must be read-only")

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

    sql_hash = hashlib.sha256(materialization_sql.encode("utf-8")).hexdigest()
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
