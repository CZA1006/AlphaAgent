from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Sequence

from alpha_harness.director import ResearchTopicPlan
from scripts.autonomous_researcher import (
    AutonomousRunnerConfig,
    build_validation_argv,
    run_autonomous_research,
)


def test_build_validation_argv_overrides_director_defaults(tmp_path) -> None:
    record = run_autonomous_research(
        AutonomousRunnerConfig(
            execute=False,
            run_id="dry-run",
            validation_dir=tmp_path,
            no_artifact=True,
        ),
        python_executable=sys.executable,
    )
    selected = record.iterations[0]

    assert selected.status == "planned"
    assert "--cycle-id" in selected.command
    assert selected.command[selected.command.index("--cycle-id") + 1] == "dry-run-i01"
    assert "--validation-dir" in selected.command
    assert selected.command[selected.command.index("--validation-dir") + 1] == str(tmp_path)
    assert "--json" in selected.command


def test_build_validation_argv_applies_budget_and_no_write(tmp_path) -> None:
    record = run_autonomous_research(
        AutonomousRunnerConfig(
            execute=False,
            run_id="budgeted",
            validation_dir=tmp_path,
            no_artifact=True,
        )
    )
    topic = ResearchTopicPlan.model_validate(record.plan["topics"][0])

    argv = build_validation_argv(
        topic,
        python_executable="python",
        validation_dir=tmp_path,
        cycle_id="cycle-1",
        llm="mock",
        n_candidates=3,
        n_cycles=2,
        token_budget=1000,
        cost_budget_usd=0.25,
        no_write=True,
    )

    assert argv[0:3] == ["python", "-m", "scripts.validate_strict"]
    assert argv[argv.index("--llm") + 1] == "mock"
    assert argv[argv.index("--n-candidates") + 1] == "3"
    assert argv[argv.index("--n-cycles") + 1] == "2"
    assert argv[argv.index("--candidate-source") + 1] == "propose"
    assert argv[argv.index("--token-budget") + 1] == "1000"
    assert argv[argv.index("--cost-budget-usd") + 1] == "0.25"
    assert "--no-write" in argv
    assert "--json" in argv


def test_dry_run_writes_structured_artifact(tmp_path) -> None:
    artifact_dir = tmp_path / "runs"
    record = run_autonomous_research(
        AutonomousRunnerConfig(
            execute=False,
            run_id="dry-run-artifact",
            validation_dir=tmp_path / "validations",
            artifact_dir=artifact_dir,
        )
    )

    artifact_path = artifact_dir / "dry-run-artifact.json"
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))

    assert record.status == "planned"
    assert record.dry_run is True
    assert record.next_decision["action"] == "continue_topic"
    assert payload["schema_version"] == 2
    assert payload["run_id"] == "dry-run-artifact"
    assert payload["next_decision"]["action"] == "continue_topic"
    assert payload["iterations"][0]["status"] == "planned"
    assert payload["iterations"][0]["stop_reason"] == "dry-run: pass --execute to run validation"


def test_execute_records_new_validation_rows(tmp_path) -> None:
    validation_dir = tmp_path / "validations"
    index_path = validation_dir / "_index.jsonl"

    def runner(argv: Sequence[str], timeout_seconds: int) -> subprocess.CompletedProcess[str]:
        assert timeout_seconds == 1800
        validation_dir.mkdir(parents=True, exist_ok=True)
        cycle_id = argv[argv.index("--cycle-id") + 1]
        index_path.write_text(
            json.dumps({"cycle_id": cycle_id, "n_promoted": 1, "n_rejected": 4}) + "\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(list(argv), 0, stdout="ok", stderr="")

    record = run_autonomous_research(
        AutonomousRunnerConfig(
            execute=True,
            iterations=1,
            run_id="execute-once",
            validation_dir=validation_dir,
            no_artifact=True,
        ),
        command_runner=runner,
    )

    assert record.status == "completed"
    assert record.next_decision["action"] == "switch_topic"
    assert record.next_decision["next_topic_id"] == "hk_ipo_cost_realism_oos"
    assert record.iterations[0].status == "executed"
    assert record.iterations[0].validation_reports == [
        {"cycle_id": "execute-once-i01", "n_promoted": 1, "n_rejected": 4}
    ]


def test_execute_uses_post_run_policy_to_select_next_iteration_topic(tmp_path) -> None:
    validation_dir = tmp_path / "validations"
    index_path = validation_dir / "_index.jsonl"
    commands: list[list[str]] = []

    def runner(argv: Sequence[str], timeout_seconds: int) -> subprocess.CompletedProcess[str]:
        commands.append(list(argv))
        validation_dir.mkdir(parents=True, exist_ok=True)
        cycle_id = argv[argv.index("--cycle-id") + 1]
        with index_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"cycle_id": cycle_id, "n_promoted": 1, "n_rejected": 4}) + "\n")
        return subprocess.CompletedProcess(list(argv), 0, stdout="ok", stderr="")

    record = run_autonomous_research(
        AutonomousRunnerConfig(
            execute=True,
            iterations=2,
            run_id="execute-switch",
            validation_dir=validation_dir,
            no_artifact=True,
        ),
        command_runner=runner,
    )

    first_theme = commands[0][commands[0].index("--theme") + 1]
    second_theme = commands[1][commands[1].index("--theme") + 1]

    assert record.status == "completed"
    assert record.iterations[0].selected_topic_id == "hk_ipo_event_conditioned_microstructure"
    assert record.iterations[0].next_decision["action"] == "switch_topic"
    assert record.iterations[0].next_decision["next_topic_id"] == "hk_ipo_cost_realism_oos"
    assert record.iterations[1].selected_topic_id == "hk_ipo_cost_realism_oos"
    assert record.next_decision["action"] == "stop_completed"
    assert first_theme == "HK IPO continuous event-decay microstructure signals"
    assert second_theme == "HK IPO implementability and cost realism"
    assert commands[1][commands[1].index("--candidate-source") + 1] == "replay_promoted"
    assert commands[1][commands[1].index("--n-cycles") + 1] == "1"
    assert commands[1][commands[1].index("--source-cycle-id") + 1] == ("execute-switch-i01")
    assert commands[1][commands[1].index("--cost-bps") + 1] == "15.0"


def test_execute_stops_when_validation_writes_no_new_rows(tmp_path) -> None:
    def runner(argv: Sequence[str], timeout_seconds: int) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(argv), 0, stdout="", stderr="")

    record = run_autonomous_research(
        AutonomousRunnerConfig(
            execute=True,
            run_id="no-progress",
            validation_dir=tmp_path / "validations",
            no_artifact=True,
        ),
        command_runner=runner,
    )

    assert record.status == "no_progress"
    assert record.iterations[0].status == "no_progress"
    assert record.iterations[0].stop_reason == (
        "research command succeeded but wrote no new typed reports"
    )


def test_execute_dispatches_event_truth_task_and_stops(tmp_path) -> None:
    validation_dir = tmp_path / "validations"
    task_dir = tmp_path / "tasks"
    validation_index = validation_dir / "_index.jsonl"
    task_index = task_dir / "_index.jsonl"
    commands: list[list[str]] = []

    def runner(argv: Sequence[str], timeout_seconds: int) -> subprocess.CompletedProcess[str]:
        commands.append(list(argv))
        if "scripts.validate_strict" in argv:
            validation_dir.mkdir(parents=True, exist_ok=True)
            cycle_id = argv[argv.index("--cycle-id") + 1]
            validation_index.write_text(
                json.dumps(
                    {
                        "cycle_id": cycle_id,
                        "n_promoted": 0,
                        "n_rejected": 3,
                        "n_rejected_by_gate": {"missing_metric": 3},
                    },
                )
                + "\n",
                encoding="utf-8",
            )
        else:
            task_dir.mkdir(parents=True, exist_ok=True)
            task_id = argv[argv.index("--task-id") + 1]
            task_index.write_text(
                json.dumps(
                    {
                        "task_id": task_id,
                        "executor": "event_truth_audit",
                        "status": "review_required",
                        "blocking_issue_count": 0,
                        "review_issue_count": 12,
                    },
                )
                + "\n",
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(list(argv), 0, stdout="ok", stderr="")

    record = run_autonomous_research(
        AutonomousRunnerConfig(
            execute=True,
            iterations=2,
            run_id="event-truth-switch",
            validation_dir=validation_dir,
            task_dir=task_dir,
            stop_after_no_promote=3,
            no_artifact=True,
        ),
        command_runner=runner,
    )

    assert record.status == "completed"
    assert record.iterations[0].next_decision["action"] == "open_data_review"
    assert record.iterations[1].selected_topic_id == "hk_ipo_event_truth_review"
    assert record.iterations[1].task_reports[0]["review_issue_count"] == 12
    assert record.next_decision["action"] == "stop_completed"
    assert commands[1][0:3] == [sys.executable, "-m", "scripts.audit_hk_ipo_event_truth"]
    assert commands[1][commands[1].index("--artifact-dir") + 1] == str(task_dir)


def test_execute_dispatches_raw_tick_plan_and_stops(tmp_path) -> None:
    task_dir = tmp_path / "tasks"
    task_index = task_dir / "_index.jsonl"
    commands: list[list[str]] = []

    def runner(argv: Sequence[str], timeout_seconds: int) -> subprocess.CompletedProcess[str]:
        commands.append(list(argv))
        task_dir.mkdir(parents=True, exist_ok=True)
        task_id = argv[argv.index("--task-id") + 1]
        task_index.write_text(
            json.dumps(
                {
                    "task_id": task_id,
                    "executor": "raw_tick_materialization_plan",
                    "status": "review_required",
                    "blocking_issue_count": 0,
                    "review_issue_count": 4,
                },
            )
            + "\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(list(argv), 0, stdout="ok", stderr="")

    record = run_autonomous_research(
        AutonomousRunnerConfig(
            execute=True,
            topic_id="hk_ipo_raw_tick_intraday_features",
            run_id="raw-tick-plan",
            task_dir=task_dir,
            validation_dir=tmp_path / "validations",
            no_artifact=True,
        ),
        command_runner=runner,
    )

    assert record.status == "completed"
    assert record.selected_topic_id == "hk_ipo_raw_tick_intraday_features"
    assert record.iterations[0].task_reports[0]["review_issue_count"] == 4
    assert record.next_decision["action"] == "stop_completed"
    assert commands[0][0:3] == [
        sys.executable,
        "-m",
        "scripts.plan_hk_ipo_raw_tick_materialization",
    ]


def test_execute_records_failed_validation(tmp_path) -> None:
    def runner(argv: Sequence[str], timeout_seconds: int) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(argv), 7, stdout="partial", stderr="boom")

    record = run_autonomous_research(
        AutonomousRunnerConfig(
            execute=True,
            run_id="failed",
            validation_dir=tmp_path / "validations",
            no_artifact=True,
        ),
        command_runner=runner,
    )

    assert record.status == "failed"
    assert record.iterations[0].status == "failed"
    assert record.iterations[0].returncode == 7
    assert record.iterations[0].stdout_tail == "partial"
    assert record.iterations[0].stderr_tail == "boom"
