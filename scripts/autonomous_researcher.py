#!/usr/bin/env python3
"""Run the research-director plan through a controlled validation loop."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from alpha_harness.director import (
    DEFAULT_VALIDATION_DIR,
    NextResearchAction,
    PostRunDecision,
    ResearchDirector,
    ResearchDirectorPlan,
    ResearchExecutorKind,
    ResearchPostRunPolicy,
    ResearchRunSummary,
    ResearchTopicPlan,
    build_hk_ipo_context,
    research_task_report_summary_from_payload,
    validation_report_summary_from_payload,
)
from alpha_harness.reports import DEFAULT_RESEARCH_TASK_DIR, read_research_task_index

DEFAULT_RUN_DIR = Path("artifacts/autonomous_runs")
RUN_SCHEMA_VERSION = 2

CommandRunner = Callable[[Sequence[str], int], subprocess.CompletedProcess[str]]


class AutonomousRunnerConfig(BaseModel):
    """Operator guardrails for the autonomous researcher executor."""

    market: str = "hk_ipo"
    topic_id: str | None = None
    execute: bool = False
    iterations: int = 1
    validation_dir: Path = DEFAULT_VALIDATION_DIR
    artifact_dir: Path = DEFAULT_RUN_DIR
    task_dir: Path = DEFAULT_RESEARCH_TASK_DIR
    run_id: str | None = None
    llm: Literal["mock", "openrouter"] = "mock"
    n_candidates: int = 12
    n_cycles: int = 3
    timeout_seconds: int = 1800
    token_budget: int | None = None
    cost_budget_usd: float | None = None
    stop_after_no_promote: int = 2
    no_artifact: bool = False
    validation_no_write: bool = False


class AutonomousIterationRecord(BaseModel):
    """One director-selected validation attempt."""

    iteration: int
    cycle_id: str
    selected_topic_id: str
    theme: str
    status: Literal["planned", "executed", "failed", "no_progress", "stopped"]
    command: list[str]
    started_at: datetime
    finished_at: datetime
    returncode: int | None = None
    validation_reports: list[dict[str, Any]] = Field(default_factory=list)
    task_reports: list[dict[str, Any]] = Field(default_factory=list)
    stdout_tail: str = ""
    stderr_tail: str = ""
    stop_reason: str = ""
    next_decision: dict[str, Any] = Field(default_factory=dict)


class AutonomousRunRecord(BaseModel):
    """Machine-readable artifact for one autonomous researcher invocation."""

    schema_version: int = RUN_SCHEMA_VERSION
    run_id: str
    market: str
    dry_run: bool
    status: Literal["planned", "completed", "failed", "no_progress", "stopped"]
    started_at: datetime
    finished_at: datetime
    selected_topic_id: str = ""
    plan: dict[str, Any] = Field(default_factory=dict)
    data_gaps: list[dict[str, Any]] = Field(default_factory=list)
    iterations: list[AutonomousIterationRecord] = Field(default_factory=list)
    next_decision: dict[str, Any] = Field(default_factory=dict)
    artifact_path: str | None = None


def _replace_option(argv: list[str], option: str, value: str) -> list[str]:
    result = list(argv)
    if option in result:
        idx = result.index(option)
        if idx + 1 >= len(result):
            raise ValueError(f"{option} requires a value")
        result[idx + 1] = value
    else:
        result.extend([option, value])
    return result


def _ensure_flag(argv: list[str], flag: str) -> list[str]:
    result = list(argv)
    if flag not in result:
        result.append(flag)
    return result


def _append_options(argv: list[str], option: str, values: Sequence[str]) -> list[str]:
    result = list(argv)
    for value in values:
        result.extend([option, value])
    return result


def build_validation_argv(
    topic: ResearchTopicPlan,
    *,
    python_executable: str,
    validation_dir: Path,
    task_dir: Path = DEFAULT_RESEARCH_TASK_DIR,
    cycle_id: str,
    llm: Literal["mock", "openrouter"],
    n_candidates: int,
    n_cycles: int,
    source_cycle_ids: Sequence[str] = (),
    token_budget: int | None = None,
    cost_budget_usd: float | None = None,
    no_write: bool = False,
) -> list[str]:
    """Return the exact ``validate_strict`` argv for a selected topic."""
    if topic.executor is ResearchExecutorKind.EVENT_TRUTH_AUDIT:
        return [
            python_executable,
            "-m",
            "scripts.audit_hk_ipo_event_truth",
            "--task-id",
            cycle_id,
            "--artifact-dir",
            str(task_dir),
            "--json",
        ]
    if topic.executor is ResearchExecutorKind.RAW_TICK_MATERIALIZATION_PLAN:
        return [
            python_executable,
            "-m",
            "scripts.plan_hk_ipo_raw_tick_materialization",
            "--task-id",
            cycle_id,
            "--artifact-dir",
            str(task_dir),
            "--json",
        ]
    args = list(topic.validation_args)
    args = _replace_option(args, "--candidate-source", topic.executor.value)
    args = _replace_option(args, "--llm", llm)
    args = _replace_option(args, "--n-candidates", str(n_candidates))
    effective_cycles = 1 if topic.executor is ResearchExecutorKind.REPLAY_PROMOTED else n_cycles
    args = _replace_option(args, "--n-cycles", str(effective_cycles))
    args = _replace_option(args, "--cycle-id", cycle_id)
    args = _replace_option(args, "--validation-dir", str(validation_dir))
    if token_budget is not None:
        args = _replace_option(args, "--token-budget", str(token_budget))
    if cost_budget_usd is not None:
        args = _replace_option(args, "--cost-budget-usd", str(cost_budget_usd))
    if topic.executor is ResearchExecutorKind.REPLAY_PROMOTED:
        args = _append_options(args, "--source-cycle-id", source_cycle_ids)
    if no_write:
        args = _ensure_flag(args, "--no-write")
    args = _ensure_flag(args, "--json")
    return [python_executable, "-m", "scripts.validate_strict", *args]


def _run_command(argv: Sequence[str], timeout_seconds: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )


def _run_id(market: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    suffix = uuid.uuid4().hex[:8]
    return f"autonomous-{market}-{stamp}-{suffix}"


def _tail(value: str, *, max_chars: int = 4000) -> str:
    if len(value) <= max_chars:
        return value
    return value[-max_chars:]


def _read_validation_index(validation_dir: Path) -> list[dict[str, Any]]:
    path = validation_dir / "_index.jsonl"
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                rows.append(payload)
    return rows


def _new_validation_rows(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    *,
    id_key: str = "cycle_id",
) -> list[dict[str, Any]]:
    seen = {str(row.get(id_key)) for row in before if row.get(id_key)}
    return [row for row in after if row.get(id_key) and str(row.get(id_key)) not in seen]


def _read_validation_report(validation_dir: Path, cycle_id: str) -> dict[str, Any] | None:
    path = validation_dir / f"{cycle_id}.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _build_research_run_summary(
    record: AutonomousRunRecord,
    validation_dir: Path,
) -> ResearchRunSummary:
    report_payloads: list[dict[str, Any]] = []
    task_payloads: list[dict[str, Any]] = []
    for iteration in record.iterations:
        for row in iteration.validation_reports:
            cycle_id = str(row.get("cycle_id", ""))
            full_report = _read_validation_report(validation_dir, cycle_id) if cycle_id else None
            report_payloads.append(full_report or row)
        task_payloads.extend(iteration.task_reports)
    return ResearchRunSummary(
        market=record.market,
        selected_topic_id=(
            record.iterations[-1].selected_topic_id
            if record.iterations
            else record.selected_topic_id
        ),
        status=record.status,
        validation_reports=[
            validation_report_summary_from_payload(payload) for payload in report_payloads
        ],
        task_reports=[
            research_task_report_summary_from_payload(payload) for payload in task_payloads
        ],
        data_gap_names=[str(gap.get("name")) for gap in record.data_gaps if gap.get("name")],
    )


def _decide_next_step(
    record: AutonomousRunRecord,
    validation_dir: Path,
) -> PostRunDecision:
    summary = _build_research_run_summary(record, validation_dir)
    return ResearchPostRunPolicy().decide(summary)


def _select_topic(plan: ResearchDirectorPlan, topic_id: str) -> ResearchTopicPlan:
    for topic in plan.topics:
        if topic.topic_id == topic_id:
            return topic
    raise ValueError(f"policy selected unknown topic: {topic_id}")


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
        with suppress(OSError):
            os.unlink(tmp_name)
        raise


def write_run_record(record: AutonomousRunRecord, artifact_dir: Path) -> Path:
    path = artifact_dir / f"{record.run_id}.json"
    record.artifact_path = str(path)
    payload = json.loads(record.model_dump_json())
    _atomic_write_json(path, payload)
    return path


def run_autonomous_research(
    config: AutonomousRunnerConfig,
    *,
    command_runner: CommandRunner = _run_command,
    python_executable: str = sys.executable,
) -> AutonomousRunRecord:
    """Plan and optionally execute autonomous research iterations."""
    if config.market != "hk_ipo":
        raise ValueError(f"unsupported market: {config.market}")
    if config.iterations < 1:
        raise ValueError("iterations must be >= 1")

    run_id = config.run_id or _run_id(config.market)
    run_started = datetime.now(UTC)
    context = build_hk_ipo_context(validation_dir=config.validation_dir)
    plan = ResearchDirector().plan(context)
    selected = _select_topic(plan, config.topic_id) if config.topic_id else plan.selected_topic
    record = AutonomousRunRecord(
        run_id=run_id,
        market=config.market,
        dry_run=not config.execute,
        status="planned",
        started_at=run_started,
        finished_at=run_started,
        selected_topic_id=selected.topic_id,
        plan=json.loads(plan.model_dump_json()),
        data_gaps=[json.loads(gap.model_dump_json()) for gap in plan.data_gaps],
    )

    consecutive_no_promote = 0
    for iteration in range(1, config.iterations + 1):
        cycle_id = f"{run_id}-i{iteration:02d}"
        source_cycle_ids = [
            str(row["cycle_id"])
            for prior_iteration in record.iterations[-1:]
            for row in prior_iteration.validation_reports
            if row.get("cycle_id") and int(row.get("n_promoted") or 0) > 0
        ]
        command = build_validation_argv(
            selected,
            python_executable=python_executable,
            validation_dir=config.validation_dir,
            task_dir=config.task_dir,
            cycle_id=cycle_id,
            llm=config.llm,
            n_candidates=config.n_candidates,
            n_cycles=config.n_cycles,
            source_cycle_ids=source_cycle_ids,
            token_budget=config.token_budget,
            cost_budget_usd=config.cost_budget_usd,
            no_write=config.validation_no_write,
        )
        started = datetime.now(UTC)
        if not config.execute:
            finished = datetime.now(UTC)
            record.iterations.append(
                AutonomousIterationRecord(
                    iteration=iteration,
                    cycle_id=cycle_id,
                    selected_topic_id=selected.topic_id,
                    theme=selected.theme,
                    status="planned",
                    command=command,
                    started_at=started,
                    finished_at=finished,
                    stop_reason="dry-run: pass --execute to run validation",
                )
            )
            break

        before = _read_validation_index(config.validation_dir)
        before_tasks = read_research_task_index(config.task_dir)
        try:
            completed = command_runner(command, config.timeout_seconds)
            returncode = completed.returncode
            stdout_tail = _tail(completed.stdout or "")
            stderr_tail = _tail(completed.stderr or "")
        except subprocess.TimeoutExpired as exc:
            finished = datetime.now(UTC)
            record.iterations.append(
                AutonomousIterationRecord(
                    iteration=iteration,
                    cycle_id=cycle_id,
                    selected_topic_id=selected.topic_id,
                    theme=selected.theme,
                    status="failed",
                    command=command,
                    started_at=started,
                    finished_at=finished,
                    stderr_tail=(
                        f"research command timed out after {config.timeout_seconds}s: {exc}"
                    ),
                    stop_reason="timeout",
                )
            )
            record.status = "failed"
            break

        after = _read_validation_index(config.validation_dir)
        after_tasks = read_research_task_index(config.task_dir)
        new_rows = _new_validation_rows(before, after)
        new_task_rows = _new_validation_rows(
            before_tasks,
            after_tasks,
            id_key="task_id",
        )
        if selected.executor in {
            ResearchExecutorKind.EVENT_TRUTH_AUDIT,
            ResearchExecutorKind.RAW_TICK_MATERIALIZATION_PLAN,
        }:
            new_rows = []
        else:
            new_task_rows = []
        finished = datetime.now(UTC)
        if returncode != 0:
            status: Literal["planned", "executed", "failed", "no_progress", "stopped"] = "failed"
            stop_reason = f"research command exited with {returncode}"
        elif config.validation_no_write and selected.executor not in {
            ResearchExecutorKind.EVENT_TRUTH_AUDIT,
            ResearchExecutorKind.RAW_TICK_MATERIALIZATION_PLAN,
        }:
            status = "executed"
            stop_reason = "validation_no_write enabled; validation index was not updated"
        elif not new_rows and not new_task_rows:
            status = "no_progress"
            stop_reason = "research command succeeded but wrote no new typed reports"
        else:
            status = "executed"
            stop_reason = ""

        record.iterations.append(
            AutonomousIterationRecord(
                iteration=iteration,
                cycle_id=cycle_id,
                selected_topic_id=selected.topic_id,
                theme=selected.theme,
                status=status,
                command=command,
                started_at=started,
                finished_at=finished,
                returncode=returncode,
                validation_reports=new_rows,
                task_reports=new_task_rows,
                stdout_tail=stdout_tail,
                stderr_tail=stderr_tail,
                stop_reason=stop_reason,
            )
        )

        if status == "failed":
            record.status = "failed"
            break
        if status == "no_progress":
            record.status = "no_progress"
            break

        promoted = sum(int(row.get("n_promoted") or 0) for row in new_rows)
        if selected.executor not in {
            ResearchExecutorKind.EVENT_TRUTH_AUDIT,
            ResearchExecutorKind.RAW_TICK_MATERIALIZATION_PLAN,
        }:
            consecutive_no_promote = 0 if promoted else consecutive_no_promote + 1
        if (
            selected.executor
            not in {
                ResearchExecutorKind.EVENT_TRUTH_AUDIT,
                ResearchExecutorKind.RAW_TICK_MATERIALIZATION_PLAN,
            }
            and consecutive_no_promote >= config.stop_after_no_promote
        ):
            latest_iteration = record.iterations[-1]
            record.status = "stopped"
            latest_iteration.status = "stopped"
            latest_iteration.stop_reason = (
                f"no promoted factors for {consecutive_no_promote} consecutive iteration(s)"
            )
            break
        record.status = "completed"

        context = build_hk_ipo_context(validation_dir=config.validation_dir)
        plan = ResearchDirector().plan(context)
        decision = _decide_next_step(record, config.validation_dir)
        record.iterations[-1].next_decision = json.loads(decision.model_dump_json())
        if decision.action in {
            NextResearchAction.STOP_COMPLETED,
            NextResearchAction.STOP_FAILED,
            NextResearchAction.STOP_NO_PROGRESS,
        }:
            break
        if decision.next_topic_id:
            selected = _select_topic(plan, decision.next_topic_id)
        else:
            selected = plan.selected_topic

    record.finished_at = datetime.now(UTC)
    decision = _decide_next_step(record, config.validation_dir)
    record.next_decision = json.loads(decision.model_dump_json())
    if not config.no_artifact:
        write_run_record(record, config.artifact_dir)
    return record


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", choices=["hk_ipo"], default="hk_ipo")
    parser.add_argument("--topic-id", default=None)
    parser.add_argument("--execute", action="store_true", help="Run the selected validation loop.")
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--validation-dir", default=str(DEFAULT_VALIDATION_DIR))
    parser.add_argument("--artifact-dir", default=str(DEFAULT_RUN_DIR))
    parser.add_argument("--task-dir", default=str(DEFAULT_RESEARCH_TASK_DIR))
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--llm", choices=["mock", "openrouter"], default="mock")
    parser.add_argument("--n-candidates", type=int, default=12)
    parser.add_argument("--n-cycles", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--token-budget", type=int, default=None)
    parser.add_argument("--cost-budget-usd", type=float, default=None)
    parser.add_argument("--stop-after-no-promote", type=int, default=2)
    parser.add_argument("--validation-no-write", action="store_true")
    parser.add_argument("--no-artifact", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def _print_text(record: AutonomousRunRecord) -> None:
    print("\nAUTONOMOUS RESEARCHER RUN")
    print("=" * 72)
    print(f"run_id           : {record.run_id}")
    print(f"market           : {record.market}")
    print(f"status           : {record.status}")
    print(f"dry_run          : {record.dry_run}")
    print(f"selected topic   : {record.selected_topic_id}")
    if record.artifact_path:
        print(f"artifact         : {record.artifact_path}")
    if record.iterations:
        latest = record.iterations[-1]
        print(f"latest iteration : {latest.status}")
        if latest.stop_reason:
            print(f"stop reason      : {latest.stop_reason}")
        print("command          :")
        print(f"  {shlex.join(latest.command)}")
        if latest.validation_reports:
            print("validation reports:")
            for row in latest.validation_reports:
                cycle_id = row.get("cycle_id", "unknown")
                promoted = row.get("n_promoted", 0)
                rejected = row.get("n_rejected", 0)
                print(f"  - {cycle_id}: promoted={promoted}, rejected={rejected}")
        if latest.task_reports:
            print("task reports:")
            for row in latest.task_reports:
                print(
                    f"  - {row.get('task_id', 'unknown')}: "
                    f"status={row.get('status', 'unknown')} "
                    f"blocking={row.get('blocking_issue_count', 0)} "
                    f"review={row.get('review_issue_count', 0)}",
                )
    if record.next_decision:
        print("next decision    :")
        print(f"  action         : {record.next_decision.get('action')}")
        if record.next_decision.get("next_topic_id"):
            print(f"  next topic     : {record.next_decision.get('next_topic_id')}")
        print(f"  rationale      : {record.next_decision.get('rationale')}")
    print("=" * 72)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    config = AutonomousRunnerConfig(
        market=args.market,
        topic_id=args.topic_id,
        execute=args.execute,
        iterations=args.iterations,
        validation_dir=Path(args.validation_dir),
        artifact_dir=Path(args.artifact_dir),
        task_dir=Path(args.task_dir),
        run_id=args.run_id,
        llm=args.llm,
        n_candidates=args.n_candidates,
        n_cycles=args.n_cycles,
        timeout_seconds=args.timeout_seconds,
        token_budget=args.token_budget,
        cost_budget_usd=args.cost_budget_usd,
        stop_after_no_promote=args.stop_after_no_promote,
        no_artifact=args.no_artifact,
        validation_no_write=args.validation_no_write,
    )
    try:
        record = run_autonomous_research(config)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(json.loads(record.model_dump_json()), indent=2))
    else:
        _print_text(record)
    if record.status == "failed":
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
