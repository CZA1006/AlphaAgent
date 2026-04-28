"""Round 4A.10 — end-to-end smoke covering 4A.5-4A.9 wiring.

Drives ``scripts.autonomous_cycle.main`` with ``--mock-llm`` against
fully tmp-scoped artifact directories so the real ``artifacts/`` tree
is never touched.  Asserts that the cycle report and (when any factor
gets promoted) the promoted-factor index land where the script claims
they will, and that ``list_cycles`` / ``list_factors`` round-trip those
on-disk artifacts.

Marked ``integration`` — opt in via ``pytest -m integration`` or
``make smoke``; ``make check`` (unit-only) does not run it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alpha_harness.artifacts import PROMOTED_INDEX_NAME
from alpha_harness.reports import REPORT_INDEX_NAME
from alpha_harness.reports import read_index as read_report_index
from scripts.autonomous_cycle import main as autonomous_main
from scripts.list_cycles import main as list_cycles_main
from scripts.list_factors import main as list_factors_main


@pytest.mark.integration
def test_autonomous_smoke_writes_reports_and_factor_zoo(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    promoted_dir = tmp_path / "promoted"
    report_dir = tmp_path / "reports"
    log_dir = tmp_path / "llm_calls"
    cycle_id = "smoke-cycle-001"

    rc = autonomous_main(
        [
            "--mock-llm",
            "--n-candidates",
            "2",
            "--n-days",
            "120",
            "--n-symbols",
            "8",
            "--seed",
            "7",
            "--max-depth",
            "1",
            "--max-variants-per-step",
            "2",
            "--max-total-children",
            "3",
            "--cycle-id",
            cycle_id,
            "--promoted-dir",
            str(promoted_dir),
            "--report-dir",
            str(report_dir),
            "--llm-log-dir",
            str(log_dir),
            "--json",
        ],
    )
    assert rc == 0

    # ── Cycle report — always written when --no-report is absent ─────────
    report_index = report_dir / REPORT_INDEX_NAME
    assert report_index.is_file(), "cycle report index missing"
    rows = read_report_index(report_dir)
    assert len(rows) == 1, f"expected one cycle report row, got {len(rows)}"
    row = rows[0]
    assert row["cycle_id"] == cycle_id

    # The per-cycle JSON file sits next to the index and matches the row.
    payload_path = report_dir / f"{cycle_id}.json"
    assert payload_path.is_file()
    payload = json.loads(payload_path.read_text())
    assert payload["cycle_id"] == cycle_id
    assert payload["n_experiments"] == row["n_experiments"]
    # 4A.7 lineage histogram should at least track the roots.
    assert "0" in payload["refinement_rounds_seen"]

    # ── Promoted artifacts — only present when something was promoted ────
    promoted_index = promoted_dir / PROMOTED_INDEX_NAME
    if payload["n_promoted"] > 0:
        assert promoted_index.is_file(), "n_promoted > 0 but promoted-index missing"

    # ── list_cycles round-trips ──────────────────────────────────────────
    capsys.readouterr()  # drain prior stdout
    rc = list_cycles_main(["--report-dir", str(report_dir)])
    assert rc == 0
    out = capsys.readouterr().out
    assert cycle_id in out

    # ── list_factors round-trips (graceful when zoo is empty) ────────────
    rc = list_factors_main(["--promoted-dir", str(promoted_dir)])
    assert rc == 0
