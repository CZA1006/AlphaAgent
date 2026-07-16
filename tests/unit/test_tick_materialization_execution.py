from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

import scripts.materialize_hk_ipo_raw_tick as execute_script
from alpha_harness.data.tick_materialization import (
    BigQueryDryRunEstimate,
    MaterializationExecutionMetadata,
    MaterializationPreconditionError,
    build_raw_tick_materialization_report,
    execute_raw_tick_materialization,
    materialization_sql_sha256,
    render_sql_template,
    validate_execution_approval,
)

PROJECT = "project"
DATASET = "dataset"
END_DATE = date(2026, 6, 26)
STARTED = datetime(2026, 7, 14, tzinfo=UTC)
SQL_TEMPLATE = Path("scripts/sql/micro_features_intraday_v1.sql").read_text(encoding="utf-8")
SOURCE_QA_TEMPLATE = Path("scripts/sql/raw_tick_nonpositive_qa.sql").read_text(encoding="utf-8")
POST_QA_TEMPLATE = Path("scripts/sql/micro_features_intraday_v1_post_qa.sql").read_text(
    encoding="utf-8"
)


def _render(template: str) -> str:
    return render_sql_template(
        template,
        project=PROJECT,
        dataset=DATASET,
        end_date=END_DATE,
    )


def _plan_report():
    return build_raw_tick_materialization_report(
        task_id="plan",
        project=PROJECT,
        dataset=DATASET,
        end_date=END_DATE,
        materialization_template=SQL_TEMPLATE,
        qa_template=SOURCE_QA_TEMPLATE,
        dry_run_runner=lambda sql: BigQueryDryRunEstimate(total_bytes_processed=42),
        query_runner=lambda sql: [],
        started_at=STARTED,
    )


def _qa_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "row_count": 7118,
        "stock_count": 77,
        "min_date": "2025-12-12",
        "max_date": "2026-06-26",
        "post_end_date_rows": 0,
        "duplicate_key_rows": 0,
        "no_first_hour_feature_rows": 0,
    }
    row.update(overrides)
    return row


def _metadata(**overrides: object) -> MaterializationExecutionMetadata:
    values: dict[str, object] = {
        "table_id": f"{PROJECT}.{DATASET}.micro_features_intraday_v1_candidate",
        "expires_at": STARTED + timedelta(days=7),
    }
    values.update(overrides)
    return MaterializationExecutionMetadata.model_validate(values)


def test_execution_approval_binds_exact_sql_hash_and_risk_acknowledgement() -> None:
    sql = _render(SQL_TEMPLATE)
    sql_hash = materialization_sql_sha256(sql)
    plan = _plan_report()

    validate_execution_approval(
        plan_report=plan,
        rendered_sql=sql,
        approved_sql_sha256=sql_hash,
        acknowledge_external_scan_cost_unknown=True,
        project=PROJECT,
        dataset=DATASET,
        end_date=END_DATE,
    )

    with pytest.raises(ValueError, match="does not match"):
        validate_execution_approval(
            plan_report=plan,
            rendered_sql=sql,
            approved_sql_sha256="0" * 64,
            acknowledge_external_scan_cost_unknown=True,
            project=PROJECT,
            dataset=DATASET,
            end_date=END_DATE,
        )
    with pytest.raises(ValueError, match="acknowledged explicitly"):
        validate_execution_approval(
            plan_report=plan,
            rendered_sql=sql,
            approved_sql_sha256=sql_hash,
            acknowledge_external_scan_cost_unknown=False,
            project=PROJECT,
            dataset=DATASET,
            end_date=END_DATE,
        )


def test_execute_materialization_passes_exact_post_write_contract() -> None:
    observed: dict[str, str] = {}

    report = execute_raw_tick_materialization(
        task_id="write",
        project=PROJECT,
        dataset=DATASET,
        end_date=END_DATE,
        rendered_sql=_render(SQL_TEMPLATE),
        post_qa_sql=_render(POST_QA_TEMPLATE),
        statement_runner=lambda sql: observed.update(statement=sql),
        metadata_runner=_metadata,
        query_runner=lambda sql: observed.update(query=sql) or [_qa_row()],
        max_bytes_billed=10_000,
        started_at=STARTED,
    )

    assert "CREATE TABLE" in observed["statement"]
    assert "CREATE" not in observed["query"].upper()
    assert report.status.value == "passed"
    assert report.summary["write_executed"] is True
    assert report.summary["row_count"] == 7118
    assert report.summary["max_bytes_billed"] == 10_000


def test_execute_materialization_records_pre_and_post_write_failures() -> None:
    def fail_write(sql: str) -> None:
        raise MaterializationPreconditionError("target already exists")

    write_failure = execute_raw_tick_materialization(
        task_id="write-failed",
        project=PROJECT,
        dataset=DATASET,
        end_date=END_DATE,
        rendered_sql=_render(SQL_TEMPLATE),
        post_qa_sql=_render(POST_QA_TEMPLATE),
        statement_runner=fail_write,
        metadata_runner=_metadata,
        query_runner=lambda sql: [],
        max_bytes_billed=10_000,
        started_at=STARTED,
    )

    def ambiguous_write(sql: str) -> None:
        raise RuntimeError("connection lost while awaiting DDL")

    ambiguous = execute_raw_tick_materialization(
        task_id="write-unknown",
        project=PROJECT,
        dataset=DATASET,
        end_date=END_DATE,
        rendered_sql=_render(SQL_TEMPLATE),
        post_qa_sql=_render(POST_QA_TEMPLATE),
        statement_runner=ambiguous_write,
        metadata_runner=_metadata,
        query_runner=lambda sql: [],
        max_bytes_billed=10_000,
        started_at=STARTED,
    )

    def fail_qa(sql: str) -> list[dict[str, object]]:
        raise RuntimeError("QA unavailable")

    qa_failure = execute_raw_tick_materialization(
        task_id="qa-failed",
        project=PROJECT,
        dataset=DATASET,
        end_date=END_DATE,
        rendered_sql=_render(SQL_TEMPLATE),
        post_qa_sql=_render(POST_QA_TEMPLATE),
        statement_runner=lambda sql: None,
        metadata_runner=_metadata,
        query_runner=fail_qa,
        max_bytes_billed=10_000,
        started_at=STARTED,
    )

    assert write_failure.status.value == "failed"
    assert write_failure.summary["write_executed"] is False
    assert write_failure.issues[0].code == "materialization_precondition_failed"
    assert ambiguous.status.value == "failed"
    assert ambiguous.summary["write_executed"] == "unknown"
    assert ambiguous.issues[0].code == "materialization_write_state_unknown"
    assert qa_failure.status.value == "blocked"
    assert qa_failure.summary["write_executed"] is True
    assert qa_failure.issues[0].code == "post_write_qa_error"


def test_metadata_failure_keeps_write_state_true_and_runs_qa() -> None:
    qa_called = False

    def fail_metadata() -> MaterializationExecutionMetadata:
        raise RuntimeError("metadata unavailable")

    def query(sql: str) -> list[dict[str, object]]:
        nonlocal qa_called
        qa_called = True
        return [_qa_row()]

    report = execute_raw_tick_materialization(
        task_id="metadata-failed",
        project=PROJECT,
        dataset=DATASET,
        end_date=END_DATE,
        rendered_sql=_render(SQL_TEMPLATE),
        post_qa_sql=_render(POST_QA_TEMPLATE),
        statement_runner=lambda sql: None,
        metadata_runner=fail_metadata,
        query_runner=query,
        max_bytes_billed=10_000,
        started_at=STARTED,
    )

    assert qa_called is True
    assert report.status.value == "blocked"
    assert report.summary["write_executed"] is True
    assert report.issues[0].code == "materialization_metadata_error"


def test_execute_materialization_blocks_bad_acceptance_and_warns_missing_features() -> None:
    blocked = execute_raw_tick_materialization(
        task_id="blocked",
        project=PROJECT,
        dataset=DATASET,
        end_date=END_DATE,
        rendered_sql=_render(SQL_TEMPLATE),
        post_qa_sql=_render(POST_QA_TEMPLATE),
        statement_runner=lambda sql: None,
        metadata_runner=lambda: _metadata(expires_at=None),
        query_runner=lambda sql: [
            _qa_row(row_count=7117, duplicate_key_rows=2, no_first_hour_feature_rows=3)
        ],
        max_bytes_billed=10_000,
        started_at=STARTED,
    )

    assert blocked.status.value == "blocked"
    assert {issue.code for issue in blocked.issues} == {
        "missing_table_expiration",
        "unexpected_row_count",
        "duplicate_stock_dates",
        "missing_first_hour_features",
    }


def test_execute_rejects_mutating_post_qa_before_write() -> None:
    called = False

    def statement(sql: str) -> None:
        nonlocal called
        called = True

    with pytest.raises(ValueError, match="forbidden statement: DROP"):
        execute_raw_tick_materialization(
            task_id="unsafe-qa",
            project=PROJECT,
            dataset=DATASET,
            end_date=END_DATE,
            rendered_sql=_render(SQL_TEMPLATE),
            post_qa_sql=(
                f"SELECT * FROM `{PROJECT}.{DATASET}.micro_features_intraday_v1_candidate`; "
                "DROP TABLE x"
            ),
            statement_runner=statement,
            metadata_runner=_metadata,
            query_runner=lambda sql: [],
            max_bytes_billed=10_000,
            started_at=STARTED,
        )

    assert called is False


def test_execution_cli_requires_all_approval_factors_and_writes_report(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sql_hash = materialization_sql_sha256(_render(SQL_TEMPLATE))
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(_plan_report().model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(
        execute_script,
        "_bigquery_runners",
        lambda **kwargs: (
            lambda sql: None,
            lambda: MaterializationExecutionMetadata(
                table_id=f"{PROJECT}.{DATASET}.micro_features_intraday_v1_candidate",
                expires_at=datetime.now(UTC) + timedelta(days=7),
            ),
            lambda sql: [_qa_row()],
        ),
    )
    common = [
        "--plan-artifact",
        str(plan_path),
        "--approve-sql-sha256",
        sql_hash,
        "--acknowledge-external-scan-cost-unknown",
        "--max-bytes-billed",
        "10000",
        "--artifact-dir",
        str(tmp_path),
        "--project",
        PROJECT,
        "--dataset",
        DATASET,
    ]

    rejected = execute_script.main([*common, "--task-id", "rejected"])
    accepted = execute_script.main(["--execute", *common, "--task-id", "accepted"])

    rejected_payload = json.loads((tmp_path / "rejected.json").read_text(encoding="utf-8"))
    accepted_payload = json.loads((tmp_path / "accepted.json").read_text(encoding="utf-8"))
    assert rejected == 3
    assert rejected_payload["summary"] == {}
    assert accepted == 0
    assert accepted_payload["status"] == "passed"
    assert accepted_payload["summary"]["write_executed"] is True
