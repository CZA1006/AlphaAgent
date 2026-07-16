from __future__ import annotations

import json
from datetime import UTC, datetime

from alpha_harness.artifacts import LocalArtifactStore
from alpha_harness.reports.validation import (
    StrictValidationReport,
    StrictValidationReportWriter,
    read_report,
)


def _report() -> StrictValidationReport:
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    return StrictValidationReport(
        cycle_id="store-round-trip",
        regime_trail_id="trail-1",
        memory_scope_id="scope-1",
        data_fingerprint="data-1",
        started_at=now,
        finished_at=now,
        n_proposals=1,
        n_promoted=0,
        n_refined=0,
        n_rejected=1,
    )


def test_local_store_and_raw_validation_paths_are_byte_compatible(tmp_path) -> None:
    report = _report()
    payload = json.loads(report.model_dump_json())
    store_dir = tmp_path / "store" / "validations"
    raw_dir = tmp_path / "raw" / "validations"

    store = LocalArtifactStore.for_directory("validations", store_dir)
    store_path = store.write("validations", report.cycle_id, payload)
    assert read_report(store_dir, report.cycle_id) == report

    raw_path = StrictValidationReportWriter(raw_dir).write(report)
    assert raw_path is not None
    raw_store = LocalArtifactStore.for_directory("validations", raw_dir)
    assert raw_store.read("validations", report.cycle_id) == payload
    assert store_path.read_bytes() == raw_path.read_bytes()
