from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

import scripts.plan_hk_ipo_raw_tick_materialization as plan_script
from alpha_harness.data.tick_materialization import (
    BigQueryDryRunEstimate,
    build_raw_tick_materialization_report,
    materialization_query_body,
    render_sql_template,
    validate_materialization_sql,
)

PROJECT = "project"
DATASET = "dataset"
END_DATE = date(2026, 6, 26)
SQL_TEMPLATE = Path("scripts/sql/micro_features_intraday_v1.sql").read_text(encoding="utf-8")
QA_TEMPLATE = Path("scripts/sql/raw_tick_nonpositive_qa.sql").read_text(encoding="utf-8")


def _rendered_sql() -> str:
    return render_sql_template(
        SQL_TEMPLATE,
        project=PROJECT,
        dataset=DATASET,
        end_date=END_DATE,
    )


def test_intraday_sql_contract_is_point_in_time_and_staging_only() -> None:
    sql = _rendered_sql()

    validate_materialization_sql(sql, project=PROJECT, dataset=DATASET)

    assert "micro_features_intraday_v1_candidate" in sql
    assert "micro_features_daily`" not in sql
    assert "DATE '2026-06-26'" in sql
    assert "ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING" in sql
    assert 'TIME(time, "Asia/Hong_Kong")' in sql


def test_sql_contract_rejects_production_target_or_missing_tick_filter() -> None:
    production_target = _rendered_sql().replace(
        "micro_features_intraday_v1_candidate",
        "micro_features_daily",
    )
    missing_filter = _rendered_sql().replace("AND value > 0", "")

    with pytest.raises(ValueError, match="must target"):
        validate_materialization_sql(production_target, project=PROJECT, dataset=DATASET)
    with pytest.raises(ValueError, match="positive tick values"):
        validate_materialization_sql(missing_filter, project=PROJECT, dataset=DATASET)


def test_materialization_query_body_removes_only_staging_ddl() -> None:
    query = materialization_query_body(_rendered_sql(), project=PROJECT, dataset=DATASET)

    assert query.lstrip().startswith("WITH calendar AS")
    assert "CREATE OR REPLACE" not in query
    assert "tick_events_ext" in query


def test_plan_dry_runs_write_sql_and_executes_only_read_only_qa() -> None:
    observed: dict[str, str] = {}

    def dry_run(sql: str) -> BigQueryDryRunEstimate:
        observed["dry_run"] = sql
        return BigQueryDryRunEstimate(
            total_bytes_processed=123456,
            total_bytes_billed=0,
        )

    def query(sql: str) -> list[dict[str, object]]:
        observed["query"] = sql
        return [
            {
                "stock_code": "00001",
                "trading_date": "2026-01-02",
                "event_type": "BID",
                "nonpositive_value_rows": 3,
            },
            {
                "stock_code": "00002",
                "trading_date": "2026-01-03",
                "event_type": "TRADE",
                "nonpositive_value_rows": 2,
            },
        ]

    report = build_raw_tick_materialization_report(
        task_id="tick-plan",
        project=PROJECT,
        dataset=DATASET,
        end_date=END_DATE,
        materialization_template=SQL_TEMPLATE,
        qa_template=QA_TEMPLATE,
        dry_run_runner=dry_run,
        query_runner=query,
    )

    assert "CREATE OR REPLACE" not in observed["dry_run"]
    assert observed["dry_run"].lstrip().startswith("WITH calendar AS")
    assert "CREATE" not in observed["query"].upper()
    assert report.status.value == "review_required"
    assert report.summary["write_executed"] is False
    assert report.summary["bigquery_dry_run_bytes_processed"] == 123456
    assert report.summary["cost_estimate_complete"] is False
    assert report.summary["qa_nonpositive_rows"] == 5
    assert report.summary["qa_affected_groups"] == 2
    assert {issue.code for issue in report.issues} == {
        "external_scan_cost_not_estimated",
        "unsupported_opening_auction_order_imbalance",
        "unsupported_quote_recovery_speed",
        "nonpositive_tick_values",
    }


def test_raw_tick_plan_cli_writes_typed_artifact(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        plan_script,
        "_bigquery_runners",
        lambda **kwargs: (
            lambda sql: BigQueryDryRunEstimate(total_bytes_processed=42),
            lambda sql: [],
        ),
    )

    rc = plan_script.main(
        [
            "--task-id",
            "tick-cli",
            "--artifact-dir",
            str(tmp_path),
            "--project",
            PROJECT,
            "--dataset",
            DATASET,
            "--json",
        ],
    )

    payload = json.loads((tmp_path / "tick-cli.json").read_text(encoding="utf-8"))
    assert rc == 0
    assert payload["executor"] == "raw_tick_materialization_plan"
    assert payload["summary"]["write_executed"] is False
    assert payload["summary"]["end_date"] == "2026-06-26"


def test_raw_tick_plan_cli_rejects_unsafe_identifier(tmp_path) -> None:
    rc = plan_script.main(
        [
            "--task-id",
            "unsafe-tick",
            "--artifact-dir",
            str(tmp_path),
            "--dataset",
            "dataset`; DROP TABLE x; --",
        ],
    )

    payload = json.loads((tmp_path / "unsafe-tick.json").read_text(encoding="utf-8"))
    assert rc == 3
    assert payload["status"] == "failed"
