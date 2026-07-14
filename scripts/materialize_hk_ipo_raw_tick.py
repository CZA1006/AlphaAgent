#!/usr/bin/env python3
"""Execute an exact, previously planned HK IPO raw-tick candidate materialization."""

from __future__ import annotations

import argparse
import re
import sys
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from alpha_harness.data.tick_materialization import (
    MaterializationExecutionMetadata,
    MaterializationPreconditionError,
    execute_raw_tick_materialization,
    render_sql_template,
    validate_bigquery_identifier,
    validate_execution_approval,
)
from alpha_harness.reports.research_task import (
    DEFAULT_RESEARCH_TASK_DIR,
    ResearchTaskReport,
    ResearchTaskReportWriter,
    ResearchTaskStatus,
    failed_task_report,
)

DEFAULT_PROJECT = "bloomberg-database-0629"
DEFAULT_DATASET = "hk_ipo_research"
DEFAULT_END_DATE = date(2026, 6, 26)
DEFAULT_SQL_PATH = Path("scripts/sql/micro_features_intraday_v1.sql")
DEFAULT_POST_QA_SQL_PATH = Path("scripts/sql/micro_features_intraday_v1_post_qa.sql")


def _bigquery_runners(
    *, project: str, dataset: str, max_bytes_billed: int
) -> tuple[Any, Any, Any]:
    from google.api_core.exceptions import NotFound
    from google.cloud import bigquery

    client = bigquery.Client(project=project)
    target = f"{project}.{dataset}.micro_features_intraday_v1_candidate"

    def statement(sql: str) -> None:
        try:
            client.get_table(target)
        except NotFound:
            pass
        else:
            raise MaterializationPreconditionError(f"target table already exists: {target}")
        config = bigquery.QueryJobConfig(maximum_bytes_billed=max_bytes_billed)
        client.query(sql, job_config=config).result()

    def metadata() -> MaterializationExecutionMetadata:
        table = client.get_table(target)
        return MaterializationExecutionMetadata(
            table_id=table.full_table_id.replace(":", "."),
            expires_at=table.expires,
        )

    def query(sql: str) -> list[dict[str, Any]]:
        config = bigquery.QueryJobConfig(maximum_bytes_billed=max_bytes_billed)
        return [dict(row.items()) for row in client.query(sql, job_config=config).result()]

    return statement, metadata, query


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--plan-artifact", type=Path, required=True)
    parser.add_argument("--approve-sql-sha256", required=True)
    parser.add_argument("--acknowledge-external-scan-cost-unknown", action="store_true")
    parser.add_argument("--max-bytes-billed", type=int, required=True)
    parser.add_argument("--task-id", default=None)
    parser.add_argument("--artifact-dir", default=str(DEFAULT_RESEARCH_TASK_DIR))
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--end-date", type=date.fromisoformat, default=DEFAULT_END_DATE)
    parser.add_argument("--sql-path", type=Path, default=DEFAULT_SQL_PATH)
    parser.add_argument("--post-qa-sql-path", type=Path, default=DEFAULT_POST_QA_SQL_PATH)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    task_id = args.task_id or f"raw-tick-write-{uuid.uuid4().hex[:12]}"
    started_at = datetime.now(UTC)
    try:
        if not args.execute:
            raise ValueError("--execute is required for materialization")
        if args.max_bytes_billed <= 0:
            raise ValueError("--max-bytes-billed must be > 0")
        if re.fullmatch(r"[0-9a-f]{64}", args.approve_sql_sha256) is None:
            raise ValueError("--approve-sql-sha256 must be a lowercase SHA-256 digest")
        project = validate_bigquery_identifier(args.project, project=True)
        dataset = validate_bigquery_identifier(args.dataset)
        rendered_sql = render_sql_template(
            args.sql_path.read_text(encoding="utf-8"),
            project=project,
            dataset=dataset,
            end_date=args.end_date,
        )
        post_qa_sql = render_sql_template(
            args.post_qa_sql_path.read_text(encoding="utf-8"),
            project=project,
            dataset=dataset,
            end_date=args.end_date,
        )
        plan_report = ResearchTaskReport.model_validate_json(
            args.plan_artifact.read_text(encoding="utf-8")
        )
        validate_execution_approval(
            plan_report=plan_report,
            rendered_sql=rendered_sql,
            approved_sql_sha256=args.approve_sql_sha256,
            acknowledge_external_scan_cost_unknown=(
                args.acknowledge_external_scan_cost_unknown
            ),
            project=project,
            dataset=dataset,
            end_date=args.end_date,
        )
        statement, metadata, query = _bigquery_runners(
            project=project,
            dataset=dataset,
            max_bytes_billed=args.max_bytes_billed,
        )
        report = execute_raw_tick_materialization(
            task_id=task_id,
            project=project,
            dataset=dataset,
            end_date=args.end_date,
            rendered_sql=rendered_sql,
            post_qa_sql=post_qa_sql,
            statement_runner=statement,
            metadata_runner=metadata,
            query_runner=query,
            max_bytes_billed=args.max_bytes_billed,
            started_at=started_at,
        )
    except Exception as exc:
        report = failed_task_report(
            task_id=task_id,
            executor="raw_tick_materialization_execute",
            started_at=started_at,
            detail=str(exc),
        )

    path = ResearchTaskReportWriter(Path(args.artifact_dir)).write(report)
    if args.json:
        print(report.model_dump_json(indent=2))
    else:
        print(f"raw tick materialization: {report.status.value}")
        print(f"artifact: {path}")
        print(f"write executed: {report.summary.get('write_executed', False)}")
        print(f"blocking issues: {report.blocking_issue_count}")
    return 0 if report.status is ResearchTaskStatus.PASSED else 3


if __name__ == "__main__":
    sys.exit(main())
