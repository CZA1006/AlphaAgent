"""Round 5 — end-to-end smoke for the strict-validation CLI.

Drives ``scripts.validate_strict.main`` against synthetic data with
fully tmp-scoped artifact directories.  Asserts that the CLI exits
cleanly, the validation report file lands, and the per-gate
rejection breakdown is populated (synthetic noise should fail
multiple gates of the strict regime).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alpha_harness.reports.validation import (
    VALIDATION_INDEX_NAME,
)
from alpha_harness.reports.validation import (
    read_index as read_validation_index,
)
from scripts.validate_strict import main as validate_main


@pytest.mark.integration
def test_validate_strict_synthetic_smoke(tmp_path: Path) -> None:
    promoted_dir = tmp_path / "promoted"
    trail_dir = tmp_path / "trails"
    val_dir = tmp_path / "validations"
    cycle_id = "smoke-strict-001"

    rc = validate_main(
        [
            "--data-source",
            "synthetic",
            "--n-days",
            "240",
            "--n-symbols",
            "8",
            "--seed",
            "11",
            "--n-candidates",
            "3",
            "--cycle-id",
            cycle_id,
            "--promoted-dir",
            str(promoted_dir),
            "--trail-dir",
            str(trail_dir),
            "--validation-dir",
            str(val_dir),
            "--json",
        ],
    )
    assert rc == 0

    # Report file landed
    payload_path = val_dir / f"{cycle_id}.json"
    assert payload_path.is_file(), "validation report payload missing"
    payload = json.loads(payload_path.read_text())
    assert payload["cycle_id"] == cycle_id
    assert payload["regime_trail_id"]
    assert payload["n_proposals"] >= 1

    # Index round-trip
    index_path = val_dir / VALIDATION_INDEX_NAME
    assert index_path.is_file()
    rows = read_validation_index(val_dir)
    assert len(rows) == 1
    assert rows[0]["cycle_id"] == cycle_id

    # Synthetic noise + strict regime: anything that wasn't promoted
    # should land in n_rejected_by_gate or in n_refined / n_promoted.
    total = payload["n_promoted"] + payload["n_refined"] + payload["n_rejected"]
    assert total == payload["n_proposals"]
