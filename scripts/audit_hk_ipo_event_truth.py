#!/usr/bin/env python3
"""Run the read-only HK IPO event-truth audit and persist a task report."""

from __future__ import annotations

import argparse
import re
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alpha_harness.data.event_truth import run_event_truth_audit
from alpha_harness.data.loader_factory import resolve_market_data_location
from alpha_harness.markets import load_market_pack
from alpha_harness.reports.research_task import (
    DEFAULT_RESEARCH_TASK_DIR,
    ResearchTaskReportWriter,
    ResearchTaskStatus,
    failed_task_report,
)

DEFAULT_PROJECT, DEFAULT_DATASET = resolve_market_data_location(load_market_pack("hk_ipo"))
DEFAULT_MAX_BYTES_BILLED = 1_073_741_824


def _validate_identifier(value: str, *, project: bool = False) -> str:
    pattern = r"[A-Za-z0-9_-]+" if project else r"[A-Za-z0-9_]+"
    if re.fullmatch(pattern, value) is None:
        raise ValueError(f"invalid BigQuery identifier: {value!r}")
    return value


def _bigquery_runner(
    *,
    project: str,
    max_bytes_billed: int,
) -> Any:
    from google.cloud import bigquery

    client = bigquery.Client(project=project)

    def run(sql: str) -> list[dict[str, Any]]:
        config = bigquery.QueryJobConfig(maximum_bytes_billed=max_bytes_billed)
        result = client.query(sql, job_config=config).result()
        return [dict(row.items()) for row in result]

    return run


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", default=None)
    parser.add_argument("--artifact-dir", default=str(DEFAULT_RESEARCH_TASK_DIR))
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--max-bytes-billed", type=int, default=DEFAULT_MAX_BYTES_BILLED)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    task_id = args.task_id or f"event-truth-{uuid.uuid4().hex[:12]}"
    started_at = datetime.now(UTC)
    try:
        project = _validate_identifier(args.project, project=True)
        dataset = _validate_identifier(args.dataset)
        if args.max_bytes_billed <= 0:
            raise ValueError("--max-bytes-billed must be > 0")
        report = run_event_truth_audit(
            task_id=task_id,
            project=project,
            dataset=dataset,
            query_runner=_bigquery_runner(
                project=project,
                max_bytes_billed=args.max_bytes_billed,
            ),
            started_at=started_at,
        )
    except Exception as exc:
        report = failed_task_report(
            task_id=task_id,
            executor="event_truth_audit",
            started_at=started_at,
            detail=str(exc),
        )

    path = ResearchTaskReportWriter(Path(args.artifact_dir)).write(report)
    if args.json:
        print(report.model_dump_json(indent=2))
    else:
        print(f"event truth audit: {report.status.value}")
        print(f"artifact: {path}")
        print(f"blocking issues: {report.blocking_issue_count}")
        print(f"review issues: {report.review_issue_count}")
    return 3 if report.status is ResearchTaskStatus.FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
