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
    assert payload["schema_version"] == 1
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
    assert first_theme == "HK IPO event-conditioned microstructure signals"
    assert second_theme == "HK IPO implementability and cost realism"


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
        "validate_strict succeeded but wrote no new validation reports"
    )


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
