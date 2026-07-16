"""Unit tests for Round 4A.5 promotion artifacts + factor zoo."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alpha_harness.artifacts import (
    PROMOTED_INDEX_NAME,
    PromotedArtifactWriter,
    index_path,
    read_index,
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
from scripts.list_factors import main as list_main


def _record(
    *,
    factor_id: str = "fct_001",
    expression: str = "rank(close)",
    decision: ExperimentDecision = ExperimentDecision.PROMOTE_CANDIDATE,
    ic: float | None = 0.05,
    rank_ic: float | None = 0.06,
    failure: FailureCategory | None = None,
) -> ExperimentRecord:
    return ExperimentRecord(
        hypothesis=Hypothesis(text=expression, rationale="r"),
        factor=FactorSpec(id=factor_id, name="f", expression=expression),
        evaluation=EvaluationBundle(
            ic=ic,
            rank_ic=rank_ic,
            quantile_spread=0.008,
            net_quantile_spread=0.007,
            turnover=0.42,
            n_periods=200,
            n_assets=20,
        ),
        decision=decision,
        failure=(FailureRecord(category=failure) if failure is not None else None),
    )


# ── Basic write path ────────────────────────────────────────────────────────


def test_writes_artifact_and_index_on_promotion(tmp_path: Path) -> None:
    writer = PromotedArtifactWriter(tmp_path, cycle_id="c-abc")
    path = writer.maybe_write(_record(factor_id="fct_a"))
    assert path is not None
    assert path.is_file()
    payload = json.loads(path.read_text())
    assert payload["factor_id"] == "fct_a"
    assert payload["expression"] == "rank(close)"
    assert payload["cycle_id"] == "c-abc"
    assert payload["evaluation"]["ic"] == 0.05

    index = read_index(tmp_path)
    assert len(index) == 1
    assert index[0]["factor_id"] == "fct_a"
    assert index[0]["rank_ic"] == 0.06


def test_skips_non_promoted_decisions(tmp_path: Path) -> None:
    writer = PromotedArtifactWriter(tmp_path)
    for decision in (
        ExperimentDecision.REJECT,
        ExperimentDecision.REFINE,
        ExperimentDecision.ARCHIVE_ONLY,
    ):
        out = writer.maybe_write(
            _record(
                decision=decision,
                failure=FailureCategory.WEAK_SIGNAL
                if decision == ExperimentDecision.REJECT
                else None,
            )
        )
        assert out is None
    assert not index_path(tmp_path).exists()
    assert list(tmp_path.iterdir()) == []


# ── Idempotency / re-promotion ─────────────────────────────────────────────


def test_repromotion_does_not_duplicate_index_line(tmp_path: Path) -> None:
    writer = PromotedArtifactWriter(tmp_path)
    writer.maybe_write(_record(factor_id="fct_dup", ic=0.05))
    writer.maybe_write(_record(factor_id="fct_dup", ic=0.07))

    index = read_index(tmp_path)
    assert len(index) == 1
    # The latest metric values should have replaced the earlier ones.
    assert index[0]["ic"] == 0.07


def test_three_distinct_factors_three_index_lines(tmp_path: Path) -> None:
    writer = PromotedArtifactWriter(tmp_path)
    for i in range(3):
        writer.maybe_write(_record(factor_id=f"fct_{i}"))
    entries = read_index(tmp_path)
    assert {e["factor_id"] for e in entries} == {"fct_0", "fct_1", "fct_2"}


# ── Robustness ──────────────────────────────────────────────────────────────


def test_corrupt_index_lines_are_skipped(tmp_path: Path) -> None:
    path = index_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"factor_id": "good", "ic": 0.01}\nthis is not json\n{"factor_id": "good2", "ic": 0.02}\n'
    )
    entries = read_index(tmp_path)
    assert [e["factor_id"] for e in entries] == ["good", "good2"]


def test_atomic_write_leaves_no_tmp_behind(tmp_path: Path) -> None:
    writer = PromotedArtifactWriter(tmp_path)
    writer.maybe_write(_record(factor_id="fct_atomic"))
    stray = [
        p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp") or p.name.startswith(".")
    ]
    assert stray == []


def test_artifact_payload_includes_reproducibility_keys(tmp_path: Path) -> None:
    writer = PromotedArtifactWriter(tmp_path, cycle_id="c-1")
    path = writer.maybe_write(_record(factor_id="fct_repro"))
    assert path is not None
    payload = json.loads(path.read_text())
    assert "reproducibility" in payload
    for key in ("code_version", "dataset_snapshot_id", "universe_snapshot_id"):
        assert key in payload["reproducibility"]


# ── list_factors CLI ────────────────────────────────────────────────────────


def test_list_factors_cli_prints_table(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    writer = PromotedArtifactWriter(tmp_path)
    writer.maybe_write(_record(factor_id="fct_low", ic=0.01, rank_ic=0.01))
    writer.maybe_write(_record(factor_id="fct_high", ic=0.09, rank_ic=0.10))

    rc = list_main(["--promoted-dir", str(tmp_path), "--sort-by", "rank_ic"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "fct_high" in out
    assert "fct_low" in out
    # higher rank_ic comes first
    assert out.index("fct_high") < out.index("fct_low")


def test_list_factors_cli_json_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    writer = PromotedArtifactWriter(tmp_path)
    writer.maybe_write(_record(factor_id="fct_j"))
    rc = list_main(
        [
            "--promoted-dir",
            str(tmp_path),
            "--json",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload[0]["factor_id"] == "fct_j"


def test_list_factors_empty_zoo_returns_zero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = list_main(["--promoted-dir", str(tmp_path)])
    assert rc == 0
    err = capsys.readouterr().err
    assert "no promoted factors" in err


def test_list_factors_since_filters_older_entries(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Hand-craft index file with explicit old + new promoted_at timestamps.
    idx = index_path(tmp_path)
    idx.parent.mkdir(parents=True, exist_ok=True)
    idx.write_text(
        '{"factor_id":"old","factor_name":"o","rank_ic":0.1,'
        '"promoted_at":"2020-01-01T00:00:00+00:00"}\n'
        '{"factor_id":"new","factor_name":"n","rank_ic":0.2,'
        '"promoted_at":"2030-01-01T00:00:00+00:00"}\n'
    )
    rc = list_main(
        [
            "--promoted-dir",
            str(tmp_path),
            "--since",
            "2025-01-01",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "new" in out
    assert "old" not in out


# ── Index helper ────────────────────────────────────────────────────────────


def test_index_path_name_is_stable() -> None:
    # Freeze the filename so external tools (jq pipelines, etc.) don't
    # break on a silent rename.
    assert PROMOTED_INDEX_NAME == "_index.jsonl"
