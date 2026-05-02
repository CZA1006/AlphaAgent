"""Round 4I — promotion-trail field-level diff."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from alpha_harness.artifacts import PromotedArtifactWriter
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
from scripts.list_factors import main as list_main
from scripts.refine_factor import main as refine_main

# ── Helpers ─────────────────────────────────────────────────────────────────


def _request(**overrides: object) -> EvaluationRequest:
    base = {
        "factor_id": "f",
        "universe_id": "u",
        "eval_start": date(2024, 1, 1),
        "eval_end": date(2024, 12, 31),
        "label": LabelDefinition(forecast_horizon_bars=5, lag_bars=1),
        "profile": EvaluationProfile(min_periods=10),
    }
    base.update(overrides)
    return EvaluationRequest(**base)  # type: ignore[arg-type]


def _trail(**overrides: object) -> PromotionTrail:
    return PromotionTrail.from_inputs(
        evaluation_request=_request(**overrides),
        judge_thresholds={"refine_margin": 0.20},
    )


# ── PromotionTrail.diff ─────────────────────────────────────────────────────


def test_diff_identical_trails_is_empty() -> None:
    a = _trail(cost_bps=2.0)
    b = _trail(cost_bps=2.0)
    assert a.diff(b) == {}


def test_diff_excludes_trail_id() -> None:
    a = _trail(cost_bps=2.0)
    b = _trail(cost_bps=5.0)
    diff = a.diff(b)
    assert "trail_id" not in diff
    assert diff["cost_bps"] == (2.0, 5.0)


def test_diff_is_symmetric_under_tuple_swap() -> None:
    a = _trail(cost_bps=2.0)
    b = _trail(cost_bps=5.0)
    forward = a.diff(b)
    reverse = b.diff(a)
    assert set(forward) == set(reverse)
    for key in forward:
        assert forward[key] == (reverse[key][1], reverse[key][0])


def test_diff_handles_list_field() -> None:
    a = _trail(label=LabelDefinition(extra_horizons=[1, 5]))
    b = _trail(label=LabelDefinition(extra_horizons=[1, 5, 20]))
    diff = a.diff(b)
    assert "extra_horizons" in diff
    assert diff["extra_horizons"] == ([1, 5], [1, 5, 20])


def test_diff_picks_up_judge_threshold_change() -> None:
    a = PromotionTrail.from_inputs(
        evaluation_request=_request(),
        judge_thresholds={"refine_margin": 0.20},
    )
    b = PromotionTrail.from_inputs(
        evaluation_request=_request(),
        judge_thresholds={"refine_margin": 0.30},
    )
    assert a.diff(b)["refine_margin"] == (0.20, 0.30)


# ── refine_factor CLI surfaces trail_diff ───────────────────────────────────


def _seeded_record(*, factor_id: str, cost_bps: float) -> ExperimentRecord:
    trail = _trail(cost_bps=cost_bps)
    return ExperimentRecord(
        hypothesis=Hypothesis(text="rank(ts_mean(close, 20))"),
        factor=FactorSpec(
            id=factor_id,
            name=f"f_{factor_id}",
            expression="rank(ts_mean(close, 20))",
        ),
        evaluation=EvaluationBundle(
            ic=0.05,
            rank_ic=0.06,
            quantile_spread=0.01,
            n_periods=200,
            n_assets=10,
        ),
        decision=ExperimentDecision.PROMOTE_CANDIDATE,
        promotion_trail=trail,
    )


def test_refine_factor_json_includes_trail_diff_on_mismatch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    PromotedArtifactWriter(tmp_path).maybe_write(
        _seeded_record(factor_id="diff1", cost_bps=2.0),
    )
    rc = refine_main(
        [
            "--factor-id",
            "diff1",
            "--promoted-dir",
            str(tmp_path),
            "--cost-bps",
            "5.0",
            "--n-days",
            "60",
            "--n-symbols",
            "5",
            "--seed",
            "1",
            "--max-refinement-rounds",
            "1",
            "--max-variants-per-step",
            "1",
            "--max-total-children",
            "1",
            "--json",
        ],
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["trail_status"] == "mismatch"
    diff = payload["trail_diff"]
    assert "cost_bps" in diff
    assert diff["cost_bps"] == [2.0, 5.0]


def test_refine_factor_text_summary_renders_diff_block(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    PromotedArtifactWriter(tmp_path).maybe_write(
        _seeded_record(factor_id="diff2", cost_bps=2.0),
    )
    rc = refine_main(
        [
            "--factor-id",
            "diff2",
            "--promoted-dir",
            str(tmp_path),
            "--cost-bps",
            "5.0",
            "--n-days",
            "60",
            "--n-symbols",
            "5",
            "--seed",
            "1",
            "--max-refinement-rounds",
            "0",
            "--max-variants-per-step",
            "1",
            "--max-total-children",
            "0",
        ],
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Trail diff" in out
    assert "cost_bps" in out
    assert "2.0 -> 5.0" in out


# ── list_factors --diff-trails ──────────────────────────────────────────────


def test_diff_trails_prints_text(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    a = _seeded_record(factor_id="A", cost_bps=2.0)
    b = _seeded_record(factor_id="B", cost_bps=5.0)
    PromotedArtifactWriter(tmp_path).maybe_write(a)
    PromotedArtifactWriter(tmp_path).maybe_write(b)
    rc = list_main(
        [
            "--promoted-dir",
            str(tmp_path),
            "--diff-trails",
            "A",
            "B",
        ],
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "trail_id_a" in out
    assert "differences" in out
    assert "cost_bps" in out


def test_diff_trails_json_mode(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    a = _seeded_record(factor_id="A", cost_bps=2.0)
    b = _seeded_record(factor_id="B", cost_bps=5.0)
    PromotedArtifactWriter(tmp_path).maybe_write(a)
    PromotedArtifactWriter(tmp_path).maybe_write(b)
    rc = list_main(
        [
            "--promoted-dir",
            str(tmp_path),
            "--diff-trails",
            "A",
            "B",
            "--json",
        ],
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["diff"]["cost_bps"] == [2.0, 5.0]


def test_diff_trails_exits_2_on_missing_artifact(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    PromotedArtifactWriter(tmp_path).maybe_write(
        _seeded_record(factor_id="A", cost_bps=2.0),
    )
    rc = list_main(
        [
            "--promoted-dir",
            str(tmp_path),
            "--diff-trails",
            "A",
            "NOPE",
        ],
    )
    err = capsys.readouterr().err
    assert rc == 2
    assert "NOPE" in err


def test_diff_trails_identical_is_zero_with_message(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    a = _seeded_record(factor_id="A", cost_bps=2.0)
    b = _seeded_record(factor_id="B", cost_bps=2.0)
    PromotedArtifactWriter(tmp_path).maybe_write(a)
    PromotedArtifactWriter(tmp_path).maybe_write(b)
    rc = list_main(
        [
            "--promoted-dir",
            str(tmp_path),
            "--diff-trails",
            "A",
            "B",
        ],
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "trails are identical" in out
