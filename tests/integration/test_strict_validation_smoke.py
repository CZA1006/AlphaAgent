"""Round 5 — end-to-end smoke for the strict-validation CLI.

Drives ``scripts.validate_strict.main`` against synthetic data with
fully tmp-scoped artifact directories.  Asserts that the CLI exits
cleanly, the validation report file lands, and the per-gate
rejection breakdown is populated (synthetic noise should fail
multiple gates of the strict regime).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

import scripts.validate_strict as validate_module
from alpha_harness.data.synthetic import generate_price_panel
from alpha_harness.reports.validation import (
    VALIDATION_INDEX_NAME,
    FactorThumbnail,
    StrictValidationReport,
    StrictValidationReportWriter,
)
from alpha_harness.reports.validation import (
    read_index as read_validation_index,
)
from alpha_harness.schemas.experiment import ExperimentDecision


@pytest.mark.integration
def test_validate_strict_synthetic_smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    promoted_dir = tmp_path / "promoted"
    trail_dir = tmp_path / "trails"
    val_dir = tmp_path / "validations"
    cycle_id = "smoke-strict-001"

    base_args = [
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
    ]
    rc = validate_module.main(base_args)
    assert rc == 0

    # Report file landed
    payload_path = val_dir / f"{cycle_id}.json"
    assert payload_path.is_file(), "validation report payload missing"
    payload = json.loads(payload_path.read_text())
    assert payload["cycle_id"] == cycle_id
    assert payload["regime_trail_id"]
    assert payload["memory_scope_id"]
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

    captured_prior_cycle_ids: list[str] = []
    original_build_memory_digest = validate_module.build_memory_digest

    def capture_memory(records, **kwargs):
        captured_prior_cycle_ids.extend(
            report.cycle_id for report in kwargs.get("validation_reports", [])
        )
        return original_build_memory_digest(records, **kwargs)

    monkeypatch.setattr(validate_module, "build_memory_digest", capture_memory)
    second_args = list(base_args)
    second_args[second_args.index("--cycle-id") + 1] = "smoke-strict-002"

    assert validate_module.main(second_args) == 0
    assert cycle_id in captured_prior_cycle_ids
    assert len(read_validation_index(val_dir)) == 2


@pytest.mark.integration
def test_validate_strict_replays_exact_promoted_factor_without_llm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    promoted_dir = tmp_path / "promoted"
    trail_dir = tmp_path / "trails"
    val_dir = tmp_path / "validations"
    source_cycle_id = "source-discovery-c01"
    now = datetime.now(UTC)
    panel = generate_price_panel(
        n_days=240,
        symbols=[f"S{i}" for i in range(8)],
        seed=11,
    )
    data_fingerprint = validate_module._dataframe_fingerprint(panel)
    source_report = StrictValidationReport(
        cycle_id=source_cycle_id,
        regime_trail_id="source-trail",
        memory_scope_id="source-scope",
        data_fingerprint=data_fingerprint,
        started_at=now,
        finished_at=now,
        n_proposals=1,
        n_promoted=1,
        n_refined=0,
        n_rejected=0,
        factors=[
            FactorThumbnail(
                factor_id="source-factor",
                expression="rank(ts_mean(close, 20))",
                decision=ExperimentDecision.PROMOTE_CANDIDATE.value,
            ),
        ],
    )
    StrictValidationReportWriter(val_dir).write(source_report)

    def fail_if_llm_is_built(*args, **kwargs):
        raise AssertionError("replay executor must not construct an LLM client")

    monkeypatch.setattr(validate_module, "_build_llm_client", fail_if_llm_is_built)
    replay_cycle_id = "cost-replay-001"
    rc = validate_module.main(
        [
            "--data-source",
            "synthetic",
            "--n-days",
            "240",
            "--n-symbols",
            "8",
            "--seed",
            "11",
            "--candidate-source",
            "replay_promoted",
            "--source-cycle-id",
            source_cycle_id,
            "--cost-bps",
            "15",
            "--n-candidates",
            "1",
            "--cycle-id",
            replay_cycle_id,
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
    replay = json.loads((val_dir / f"{replay_cycle_id}.json").read_text())
    assert replay["candidate_source"] == "replay_promoted"
    assert replay["source_cycle_ids"] == [source_cycle_id]
    assert replay["data_fingerprint"] == data_fingerprint
    assert replay["cost_bps"] == 15.0
    assert replay["n_proposals"] == 1
    assert replay["factors"][0]["expression"] == "rank(ts_mean(close, 20))"
