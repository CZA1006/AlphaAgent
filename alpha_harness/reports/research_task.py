"""Typed artifacts for deterministic research tasks outside factor validation."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from alpha_harness.artifacts.store import LocalArtifactStore

logger = logging.getLogger(__name__)

DEFAULT_RESEARCH_TASK_DIR = Path("artifacts/research_tasks")
RESEARCH_TASK_INDEX_NAME = "_index.jsonl"
SCHEMA_VERSION = 1


class ResearchTaskStatus(StrEnum):
    PASSED = "passed"
    REVIEW_REQUIRED = "review_required"
    BLOCKED = "blocked"
    FAILED = "failed"


class ResearchTaskIssueSeverity(StrEnum):
    WARNING = "warning"
    BLOCKING = "blocking"


class ResearchTaskIssue(BaseModel):
    code: str
    severity: ResearchTaskIssueSeverity
    count: int = 1
    detail: str = ""
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class ResearchTaskReport(BaseModel):
    schema_version: int = SCHEMA_VERSION
    task_id: str
    executor: str
    status: ResearchTaskStatus
    started_at: datetime
    finished_at: datetime
    issues: list[ResearchTaskIssue] = Field(default_factory=list)
    summary: dict[str, str | int | float | bool] = Field(default_factory=dict)
    notes: str = ""

    @property
    def blocking_issue_count(self) -> int:
        return sum(
            issue.count
            for issue in self.issues
            if issue.severity is ResearchTaskIssueSeverity.BLOCKING
        )

    @property
    def review_issue_count(self) -> int:
        return sum(
            issue.count
            for issue in self.issues
            if issue.severity is ResearchTaskIssueSeverity.WARNING
        )


def read_index(
    base_dir: Path | str = DEFAULT_RESEARCH_TASK_DIR,
) -> list[dict[str, Any]]:
    return LocalArtifactStore.for_directory("research_tasks", base_dir).list_index("research_tasks")


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True, default=str)
            fh.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


class ResearchTaskReportWriter:
    def __init__(self, base_dir: Path | str = DEFAULT_RESEARCH_TASK_DIR) -> None:
        self._base_dir = Path(base_dir)

    def write(self, report: ResearchTaskReport) -> Path:
        path = LocalArtifactStore.for_directory("research_tasks", self._base_dir).write(
            "research_tasks",
            report.task_id,
            json.loads(report.model_dump_json()),
        )
        self._upsert_index(report)
        return path

    def _upsert_index(self, report: ResearchTaskReport) -> None:
        index_path = self._base_dir / RESEARCH_TASK_INDEX_NAME
        rows = [row for row in read_index(self._base_dir) if row.get("task_id") != report.task_id]
        rows.append(
            {
                "task_id": report.task_id,
                "executor": report.executor,
                "status": report.status.value,
                "started_at": report.started_at.isoformat(),
                "finished_at": report.finished_at.isoformat(),
                "blocking_issue_count": report.blocking_issue_count,
                "review_issue_count": report.review_issue_count,
            },
        )
        self._base_dir.mkdir(parents=True, exist_ok=True)
        tmp = index_path.with_name(index_path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, sort_keys=True))
                fh.write("\n")
        os.replace(tmp, index_path)


def failed_task_report(
    *,
    task_id: str,
    executor: str,
    started_at: datetime,
    detail: str,
) -> ResearchTaskReport:
    return ResearchTaskReport(
        task_id=task_id,
        executor=executor,
        status=ResearchTaskStatus.FAILED,
        started_at=started_at,
        finished_at=datetime.now(UTC),
        issues=[
            ResearchTaskIssue(
                code="execution_error",
                severity=ResearchTaskIssueSeverity.BLOCKING,
                detail=detail,
            ),
        ],
    )
