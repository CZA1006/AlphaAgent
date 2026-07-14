#!/usr/bin/env python3
"""Dry-run the HK IPO intraday feature plan and execute read-only tick QA."""

from __future__ import annotations

import argparse
import re
import sys
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from alpha_harness.data.tick_materialization import (
    BigQueryDryRunEstimate,
    build_raw_tick_materialization_report,
)
from alpha_harness.reports.research_task import (
    DEFAULT_RESEARCH_TASK_DIR,
    ResearchTaskReportWriter,
    ResearchTaskStatus,
    failed_task_report,
)

DEFAULT_PROJECT = "bloomberg-database-0629"
DEFAULT_DATASET = "hk_ipo_research"
DEFAULT_END_DATE = date(2026, 6, 26)
DEFAULT_MAX_BYTES_BILLED = 20 * 1024**3
DEFAULT_SQL_PATH = Path("scripts/sql/micro_features_intraday_v1.sql")
DEFAULT_QA_SQL_PATH = Path("scripts/sql/raw_tick_nonpositive_qa.sql")


def _validate_identifier(value: str, *, project: bool = False) -> str:
    pattern = r"[A-Za-z0-9_-]+" if project else r"[A-Za-z0-9_]+"
    if re.fullmatch(pattern, value) is None:
        raise ValueError(f"invalid BigQuery identifier: {value!r}")
    return value


def _bigquery_runners(*, project: str, max_bytes_billed: int) -> tuple[Any, Any]:
    from google.cloud import bigquery

    client = bigquery.Client(project=project)

    def dry_run(sql: str) -> BigQueryDryRunEstimate:
        config = bigquery.QueryJobConfig(
            dry_run=True,
            use_query_cache=False,
            maximum_bytes_billed=max_bytes_billed,
        )
        job = client.query(sql, job_config=config)
        return BigQueryDryRunEstimate(
            total_bytes_processed=int(job.total_bytes_processed or 0),
            total_bytes_billed=int(job.total_bytes_billed or 0),
        )

    def query(sql: str) -> list[dict[str, Any]]:
        config = bigquery.QueryJobConfig(maximum_bytes_billed=max_bytes_billed)
        return [dict(row.items()) for row in client.query(sql, job_config=config).result()]

    return dry_run, query


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", default=None)
    parser.add_argument("--artifact-dir", default=str(DEFAULT_RESEARCH_TASK_DIR))
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--end-date", type=date.fromisoformat, default=DEFAULT_END_DATE)
    parser.add_argument("--sql-path", type=Path, default=DEFAULT_SQL_PATH)
    parser.add_argument("--qa-sql-path", type=Path, default=DEFAULT_QA_SQL_PATH)
    parser.add_argument("--max-bytes-billed", type=int, default=DEFAULT_MAX_BYTES_BILLED)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    task_id = args.task_id or f"raw-tick-plan-{uuid.uuid4().hex[:12]}"
    started_at = datetime.now(UTC)
    try:
        project = _validate_identifier(args.project, project=True)
        dataset = _validate_identifier(args.dataset)
        if args.max_bytes_billed <= 0:
            raise ValueError("--max-bytes-billed must be > 0")
        dry_run, query = _bigquery_runners(
            project=project,
            max_bytes_billed=args.max_bytes_billed,
        )
        report = build_raw_tick_materialization_report(
            task_id=task_id,
            project=project,
            dataset=dataset,
            end_date=args.end_date,
            materialization_template=args.sql_path.read_text(encoding="utf-8"),
            qa_template=args.qa_sql_path.read_text(encoding="utf-8"),
            dry_run_runner=dry_run,
            query_runner=query,
            started_at=started_at,
        )
    except Exception as exc:
        report = failed_task_report(
            task_id=task_id,
            executor="raw_tick_materialization_plan",
            started_at=started_at,
            detail=str(exc),
        )

    path = ResearchTaskReportWriter(Path(args.artifact_dir)).write(report)
    if args.json:
        print(report.model_dump_json(indent=2))
    else:
        print(f"raw tick materialization plan: {report.status.value}")
        print(f"artifact: {path}")
        print(f"blocking issues: {report.blocking_issue_count}")
        print(f"review issues: {report.review_issue_count}")
        if report.summary:
            print(
                "BigQuery dry-run bytes: "
                f"{report.summary.get('bigquery_dry_run_bytes_processed', 0)} "
                "(external scan estimate incomplete)"
            )
    return 3 if report.status is ResearchTaskStatus.FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
