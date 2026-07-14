"""Round 4A.8 — cycle audit reports + list_cycles CLI."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from alpha_harness.registries.experiment import ExperimentRegistry
from alpha_harness.reports import (
    CycleReport,
    CycleReportWriter,
    build_cycle_report,
    index_path,
    read_index,
)
from alpha_harness.reports.cycle_report import (
    SCHEMA_VERSION,
    BudgetSnapshot,
    snapshot_budget,
)
from alpha_harness.schemas.evaluation import EvaluationBundle
from alpha_harness.schemas.experiment import (
    ExperimentDecision,
    ExperimentRecord,
    FailureCategory,
    FailureRecord,
)
from alpha_harness.schemas.factor import FactorSpec
from alpha_harness.schemas.hypothesis import Hypothesis
from scripts.list_cycles import main as list_cycles_main


def _record(
    *,
    factor_id: str,
    decision: ExperimentDecision = ExperimentDecision.PROMOTE_CANDIDATE,
    parent_factor_id: str | None = None,
    refinement_round: int = 0,
    rank_ic: float = 0.06,
    failure: FailureCategory | None = None,
) -> ExperimentRecord:
    return ExperimentRecord(
        hypothesis=Hypothesis(text="rank(close)", rationale="r"),
        factor=FactorSpec(
            id=factor_id,
            name=f"f_{factor_id}",
            expression="rank(close)",
            parent_factor_id=parent_factor_id,
            refinement_round=refinement_round,
        ),
        evaluation=EvaluationBundle(
            ic=0.05,
            rank_ic=rank_ic,
            quantile_spread=0.008,
            net_quantile_spread=0.007,
            turnover=0.42,
            n_periods=200,
            n_assets=20,
        ),
        decision=decision,
        failure=FailureRecord(category=failure) if failure is not None else None,
    )


def _seed_registry(records: list[ExperimentRecord]) -> ExperimentRegistry:
    reg = ExperimentRegistry()
    for r in records:
        reg.save(r)
    return reg


# ── build_cycle_report ──────────────────────────────────────────────────────


def test_build_report_aggregates_decisions_and_lineage() -> None:
    records = [
        _record(factor_id="root1", decision=ExperimentDecision.PROMOTE_CANDIDATE),
        _record(
            factor_id="kid1",
            decision=ExperimentDecision.PROMOTE_CANDIDATE,
            parent_factor_id="root1",
            refinement_round=1,
        ),
        _record(
            factor_id="kid2",
            decision=ExperimentDecision.REJECT,
            parent_factor_id="root1",
            refinement_round=1,
            failure=FailureCategory.WEAK_SIGNAL,
        ),
    ]
    reg = _seed_registry(records)
    started = datetime.now(UTC) - timedelta(seconds=12)
    report = build_cycle_report(
        cycle_id="cyc-1",
        theme="momentum",
        started_at=started,
        experiment_registry=reg,
        experiment_ids=[r.id for r in records],
    )
    assert report.cycle_id == "cyc-1"
    assert report.theme == "momentum"
    assert report.n_experiments == 3
    assert report.n_promoted == 2
    assert report.n_rejected == 1
    assert report.duration_s >= 0
    assert report.refinement_rounds_seen == {"0": 1, "1": 2}
    assert "root1" in report.promoted_factor_ids
    assert "kid1" in report.promoted_factor_ids
    rej = next(t for t in report.experiments if t.decision == "reject")
    assert rej.failure_category == FailureCategory.WEAK_SIGNAL.value


def test_build_report_skips_missing_experiment_ids() -> None:
    records = [_record(factor_id="present")]
    reg = _seed_registry(records)
    report = build_cycle_report(
        cycle_id="cyc-x",
        theme="t",
        started_at=datetime.now(UTC),
        experiment_registry=reg,
        experiment_ids=[records[0].id, "nonexistent_id"],
    )
    assert report.n_experiments == 1


# ── CycleReportWriter ──────────────────────────────────────────────────────


def _minimal_report(cycle_id: str = "cyc-w") -> CycleReport:
    started = datetime.now(UTC)
    return CycleReport(
        cycle_id=cycle_id,
        theme="t",
        started_at=started,
        finished_at=started + timedelta(seconds=1),
        duration_s=1.0,
        n_experiments=0,
        n_promoted=0,
        n_refined=0,
        n_rejected=0,
    )


def test_writer_writes_payload_and_index(tmp_path: Path) -> None:
    writer = CycleReportWriter(tmp_path)
    path = writer.write(_minimal_report("cyc-a"))
    assert path is not None and path.is_file()
    payload = json.loads(path.read_text())
    assert payload["cycle_id"] == "cyc-a"
    assert payload["schema_version"] == SCHEMA_VERSION
    index = read_index(tmp_path)
    assert len(index) == 1
    assert index[0]["cycle_id"] == "cyc-a"


def test_writer_idempotent_on_same_cycle_id(tmp_path: Path) -> None:
    writer = CycleReportWriter(tmp_path)
    writer.write(_minimal_report("cyc-dup"))
    writer.write(_minimal_report("cyc-dup"))
    assert len(read_index(tmp_path)) == 1


def test_writer_appends_distinct_cycles(tmp_path: Path) -> None:
    writer = CycleReportWriter(tmp_path)
    writer.write(_minimal_report("cyc-1"))
    writer.write(_minimal_report("cyc-2"))
    ids = {e["cycle_id"] for e in read_index(tmp_path)}
    assert ids == {"cyc-1", "cyc-2"}


def test_writer_atomic_no_tmp_residue(tmp_path: Path) -> None:
    writer = CycleReportWriter(tmp_path)
    writer.write(_minimal_report("cyc-atomic"))
    leftover = [
        p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp") or p.name.startswith(".")
    ]
    assert leftover == []


def test_corrupt_index_rows_are_skipped(tmp_path: Path) -> None:
    p = index_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        '{"cycle_id":"good"}\nthis is not json\n{"cycle_id":"good2"}\n',
    )
    rows = read_index(tmp_path)
    assert [r["cycle_id"] for r in rows] == ["good", "good2"]


# ── BudgetSnapshot ──────────────────────────────────────────────────────────


def test_snapshot_budget_handles_none() -> None:
    assert snapshot_budget(None) is None


def test_snapshot_budget_reads_duck_typed_object() -> None:
    class _B:
        total_tokens_spent = 1234
        cost_usd_spent = 0.0567
        max_total_tokens = 5000
        max_cost_usd = 0.5
        calls = 3
        prompt_cost_per_1k = 0.001
        completion_cost_per_1k = 0.002
        actual_cost_calls = 2
        estimated_cost_calls = 1

    snap = snapshot_budget(_B())
    assert isinstance(snap, BudgetSnapshot)
    assert snap.total_tokens_spent == 1234
    assert snap.max_cost_usd == pytest.approx(0.5)
    assert snap.calls == 3
    assert snap.prompt_cost_per_1k == pytest.approx(0.001)
    assert snap.completion_cost_per_1k == pytest.approx(0.002)
    assert snap.actual_cost_calls == 2
    assert snap.estimated_cost_calls == 1


# ── list_cycles CLI ─────────────────────────────────────────────────────────


def test_list_cycles_table_and_sort_newest_first(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    writer = CycleReportWriter(tmp_path)
    old = _minimal_report("old").model_copy(
        update={
            "started_at": datetime(2025, 1, 1, tzinfo=UTC),
            "finished_at": datetime(2025, 1, 1, 0, 0, 1, tzinfo=UTC),
        },
    )
    new = _minimal_report("new").model_copy(
        update={
            "started_at": datetime(2026, 1, 1, tzinfo=UTC),
            "finished_at": datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
        },
    )
    writer.write(old)
    writer.write(new)
    rc = list_cycles_main(["--report-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.index("new") < out.index("old")


def test_list_cycles_since_filter(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    p = index_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        '{"cycle_id":"old","started_at":"2020-01-01T00:00:00+00:00"}\n'
        '{"cycle_id":"new","started_at":"2030-01-01T00:00:00+00:00"}\n',
    )
    rc = list_cycles_main(["--report-dir", str(tmp_path), "--since", "2025-01-01"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "new" in out
    assert "old" not in out


def test_list_cycles_json_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    writer = CycleReportWriter(tmp_path)
    writer.write(_minimal_report("cyc-j"))
    rc = list_cycles_main(["--report-dir", str(tmp_path), "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload[0]["cycle_id"] == "cyc-j"


def test_list_cycles_empty_returns_zero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = list_cycles_main(["--report-dir", str(tmp_path)])
    assert rc == 0
    err = capsys.readouterr().err
    assert "no cycle reports" in err


# ── Doctor probe ────────────────────────────────────────────────────────────


def test_doctor_cycle_reports_passes_clean_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    art = tmp_path / "artifacts" / "reports"
    art.mkdir(parents=True, exist_ok=True)
    (art / "_index.jsonl").write_text(
        '{"cycle_id":"a","n_experiments":3,"n_promoted":1,"n_refined":1,"n_rejected":1}\n',
    )
    from scripts.doctor import _check_cycle_reports_dir

    res = _check_cycle_reports_dir()
    assert res.passed is True
    assert "1 cycle report" in res.detail


def test_doctor_cycle_reports_flags_malformed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    art = tmp_path / "artifacts" / "reports"
    art.mkdir(parents=True, exist_ok=True)
    (art / "_index.jsonl").write_text(
        '{"cycle_id":"a","n_experiments":3}\n{"cycle_id":"b","n_promoted":-1}\n',
    )
    from scripts.doctor import _check_cycle_reports_dir

    res = _check_cycle_reports_dir()
    assert res.passed is False
    assert "malformed" in res.detail
