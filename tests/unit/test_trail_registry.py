"""Round 4J — promotion-trail registry."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from alpha_harness.artifacts import (
    PromotedArtifactWriter,
    TrailRegistryWriter,
    read_trail,
    read_trails,
)
from alpha_harness.schemas.evaluation import (
    EvaluationBundle,
    EvaluationProfile,
    EvaluationRequest,
    LabelDefinition,
)
from alpha_harness.schemas.experiment import (
    ExperimentDecision,
    ExperimentRecord,
    PromotionTrail,
)
from alpha_harness.schemas.factor import FactorSpec
from alpha_harness.schemas.hypothesis import Hypothesis
from scripts.list_trails import main as list_trails_main


def _trail(cost_bps: float) -> PromotionTrail:
    return PromotionTrail.from_inputs(
        evaluation_request=EvaluationRequest(
            factor_id="f",
            universe_id="u",
            eval_start=date(2024, 1, 1),
            eval_end=date(2024, 12, 31),
            label=LabelDefinition(),
            profile=EvaluationProfile(),
            cost_bps=cost_bps,
        ),
        judge_thresholds={"refine_margin": 0.20},
    )


def _record(*, factor_id: str, cost_bps: float) -> ExperimentRecord:
    return ExperimentRecord(
        hypothesis=Hypothesis(text="rank(close)"),
        factor=FactorSpec(id=factor_id, name="f", expression="rank(close)"),
        evaluation=EvaluationBundle(
            ic=0.05,
            rank_ic=0.06,
            quantile_spread=0.01,
            n_periods=200,
            n_assets=10,
        ),
        decision=ExperimentDecision.PROMOTE_CANDIDATE,
        promotion_trail=_trail(cost_bps),
    )


# ── TrailRegistryWriter ─────────────────────────────────────────────────────


def test_first_write_creates_full_trail_json(tmp_path: Path) -> None:
    writer = TrailRegistryWriter(tmp_path)
    trail = _trail(cost_bps=2.0)
    assert writer.record(trail, "fct1") is True
    file = tmp_path / f"{trail.trail_id}.json"
    assert file.is_file()
    payload = json.loads(file.read_text())
    assert payload["trail_id"] == trail.trail_id
    assert payload["cost_bps"] == 2.0


def test_index_carries_first_seen_and_factor_ids(tmp_path: Path) -> None:
    writer = TrailRegistryWriter(tmp_path)
    trail = _trail(cost_bps=2.0)
    writer.record(trail, "fct1")
    rows = read_trails(tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["trail_id"] == trail.trail_id
    assert row["factor_ids"] == ["fct1"]
    assert "first_seen_at" in row


def test_repeated_record_same_factor_is_no_op(tmp_path: Path) -> None:
    writer = TrailRegistryWriter(tmp_path)
    trail = _trail(cost_bps=2.0)
    writer.record(trail, "fct1")
    assert writer.record(trail, "fct1") is False
    rows = read_trails(tmp_path)
    assert rows[0]["factor_ids"] == ["fct1"]


def test_new_factor_appends_to_existing_row(tmp_path: Path) -> None:
    writer = TrailRegistryWriter(tmp_path)
    trail = _trail(cost_bps=2.0)
    writer.record(trail, "fct1")
    assert writer.record(trail, "fct2") is True
    rows = read_trails(tmp_path)
    assert rows[0]["factor_ids"] == ["fct1", "fct2"]


def test_distinct_trails_distinct_rows(tmp_path: Path) -> None:
    writer = TrailRegistryWriter(tmp_path)
    writer.record(_trail(2.0), "a")
    writer.record(_trail(5.0), "b")
    rows = read_trails(tmp_path)
    assert len({r["trail_id"] for r in rows}) == 2


def test_record_none_trail_is_no_op(tmp_path: Path) -> None:
    writer = TrailRegistryWriter(tmp_path)
    assert writer.record(None, "legacy") is False
    assert read_trails(tmp_path) == []


def test_read_trail_round_trips(tmp_path: Path) -> None:
    writer = TrailRegistryWriter(tmp_path)
    trail = _trail(cost_bps=2.0)
    writer.record(trail, "fct1")
    loaded = read_trail(trail.trail_id, tmp_path)
    assert loaded is not None
    assert loaded.cost_bps == 2.0
    assert loaded.trail_id == trail.trail_id


def test_read_trail_returns_none_when_absent(tmp_path: Path) -> None:
    assert read_trail("does_not_exist", tmp_path) is None


# ── PromotedArtifactWriter integration ──────────────────────────────────────


def test_promoted_writer_records_trail_when_registry_supplied(
    tmp_path: Path,
) -> None:
    promoted_dir = tmp_path / "promoted"
    trail_dir = tmp_path / "trails"
    registry = TrailRegistryWriter(trail_dir)
    writer = PromotedArtifactWriter(promoted_dir, trail_registry=registry)
    record = _record(factor_id="fct_int", cost_bps=2.0)
    writer.maybe_write(record)
    rows = read_trails(trail_dir)
    assert len(rows) == 1
    assert "fct_int" in rows[0]["factor_ids"]


def test_promoted_writer_legacy_promotion_skips_trail_registry(
    tmp_path: Path,
) -> None:
    promoted_dir = tmp_path / "promoted"
    trail_dir = tmp_path / "trails"
    registry = TrailRegistryWriter(trail_dir)
    writer = PromotedArtifactWriter(promoted_dir, trail_registry=registry)
    legacy = ExperimentRecord(
        hypothesis=Hypothesis(text="rank(close)"),
        factor=FactorSpec(id="legacy", name="f", expression="rank(close)"),
        evaluation=EvaluationBundle(
            ic=0.05,
            rank_ic=0.06,
            quantile_spread=0.01,
            n_periods=200,
            n_assets=10,
        ),
        decision=ExperimentDecision.PROMOTE_CANDIDATE,
        promotion_trail=None,
    )
    writer.maybe_write(legacy)
    assert read_trails(trail_dir) == []


# ── list_trails CLI ─────────────────────────────────────────────────────────


def test_list_trails_table_renders(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    writer = TrailRegistryWriter(tmp_path)
    writer.record(_trail(2.0), "a")
    writer.record(_trail(5.0), "b")
    rc = list_trails_main(["--trail-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "trail_id" in out
    # Both trail_ids should appear
    rows = read_trails(tmp_path)
    for row in rows:
        assert row["trail_id"][:8] in out


def test_list_trails_diff_works_on_known_ids(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    writer = TrailRegistryWriter(tmp_path)
    a = _trail(2.0)
    b = _trail(5.0)
    writer.record(a, "fa")
    writer.record(b, "fb")
    rc = list_trails_main(
        ["--trail-dir", str(tmp_path), "--diff", a.trail_id, b.trail_id],
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "differences" in out
    assert "cost_bps" in out
    assert "2.0 -> 5.0" in out


def test_list_trails_diff_exits_2_on_unknown_id(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    writer = TrailRegistryWriter(tmp_path)
    a = _trail(2.0)
    writer.record(a, "fa")
    rc = list_trails_main(
        ["--trail-dir", str(tmp_path), "--diff", a.trail_id, "no_such_trail"],
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert "no_such_trail" in err


def test_list_trails_empty_returns_zero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = list_trails_main(["--trail-dir", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 0
    assert "no trails recorded" in err


# ── Doctor probe ────────────────────────────────────────────────────────────


def test_doctor_passes_on_clean_trail_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    art = tmp_path / "artifacts" / "trails"
    art.mkdir(parents=True, exist_ok=True)
    (art / "_index.jsonl").write_text(
        '{"trail_id":"abc123","first_seen_at":"2026-01-01T00:00:00+00:00","factor_ids":["x"]}\n',
    )
    from scripts.doctor import _check_trail_registry_dir

    res = _check_trail_registry_dir()
    assert res.passed is True
    assert "1 unique trail" in res.detail


def test_doctor_flags_malformed_trail_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    art = tmp_path / "artifacts" / "trails"
    art.mkdir(parents=True, exist_ok=True)
    (art / "_index.jsonl").write_text(
        '{"trail_id":"","first_seen_at":"2026-01-01T00:00:00+00:00"}\n'
    )
    from scripts.doctor import _check_trail_registry_dir

    res = _check_trail_registry_dir()
    assert res.passed is False
    assert "malformed" in res.detail
