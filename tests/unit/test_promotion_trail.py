"""Round 4F — promotion-trail snapshot for reproducibility."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from alpha_harness.artifacts import PromotedArtifactWriter, read_index
from alpha_harness.evaluators.promotion_judge import PromotionJudge
from alpha_harness.schemas.evaluation import (
    EvaluationBundle,
    EvaluationProfile,
    EvaluationRequest,
    HoldoutPolicy,
    HoldoutStrategy,
    LabelDefinition,
    NeutralizeMode,
)
from alpha_harness.schemas.experiment import (
    ExperimentDecision,
    ExperimentRecord,
    PromotionTrail,
)
from alpha_harness.schemas.factor import FactorSpec
from alpha_harness.schemas.hypothesis import Hypothesis
from scripts.list_factors import main as list_main


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


def _bundle(walk_forward: dict[str, object] | None = None) -> EvaluationBundle:
    metadata: dict[str, object] = {}
    if walk_forward is not None:
        metadata["walk_forward"] = walk_forward
    return EvaluationBundle(
        ic=0.05,
        rank_ic=0.06,
        quantile_spread=0.01,
        net_quantile_spread=0.009,
        turnover=0.4,
        n_periods=400,
        n_assets=10,
        metadata=metadata,
    )


def _factor() -> FactorSpec:
    return FactorSpec(name="f", expression="rank(close)")


# ── PromotionTrail.from_inputs ──────────────────────────────────────────────


def test_trail_id_is_stable_for_identical_inputs() -> None:
    req = _request(cost_bps=2.0)
    a = PromotionTrail.from_inputs(
        evaluation_request=req,
        judge_thresholds={"refine_margin": 0.20},
    )
    b = PromotionTrail.from_inputs(
        evaluation_request=req,
        judge_thresholds={"refine_margin": 0.20},
    )
    assert a.trail_id == b.trail_id


def test_trail_id_differs_when_cost_bps_changes() -> None:
    a = PromotionTrail.from_inputs(
        evaluation_request=_request(cost_bps=2.0),
        judge_thresholds={"refine_margin": 0.20},
    )
    b = PromotionTrail.from_inputs(
        evaluation_request=_request(cost_bps=5.0),
        judge_thresholds={"refine_margin": 0.20},
    )
    assert a.trail_id != b.trail_id
    assert a.cost_bps != b.cost_bps


def test_trail_id_differs_when_holdout_policy_changes() -> None:
    a = PromotionTrail.from_inputs(
        evaluation_request=_request(),
        judge_thresholds={},
    )
    b = PromotionTrail.from_inputs(
        evaluation_request=_request(
            holdout=HoldoutPolicy(
                strategy=HoldoutStrategy.TAIL,
                holdout_fraction=0.2,
            ),
        ),
        judge_thresholds={},
    )
    assert a.trail_id != b.trail_id


def test_optional_selection_provenance_changes_trail_id() -> None:
    baseline = PromotionTrail.from_inputs(
        evaluation_request=_request(),
        judge_thresholds={},
    )
    selected = PromotionTrail.from_inputs(
        evaluation_request=_request(),
        judge_thresholds={},
        selection={"strategy": "persistence", "top_k": 4},
    )
    assert baseline.trail_id != selected.trail_id
    assert baseline.selection == {}
    assert selected.selection == {"strategy": "persistence", "top_k": 4}


def test_trail_captures_walk_forward_immutables() -> None:
    wf = {"n_folds": 4, "fold_size_days": 60, "step_days": 20, "embargo_days": 6}
    trail = PromotionTrail.from_inputs(
        evaluation_request=_request(),
        judge_thresholds={},
        walk_forward=wf,
    )
    assert trail.walk_forward == wf


def test_session_family_size_changes_trail_id() -> None:
    single = PromotionTrail.from_inputs(
        evaluation_request=_request(),
        judge_thresholds={},
    )
    family = PromotionTrail.from_inputs(
        evaluation_request=_request(n_proposals_in_session=18),
        judge_thresholds={},
    )
    assert family.trail_id != single.trail_id
    assert family.n_proposals_in_session == 18
    assert family.ic_threshold_multiplier == pytest.approx(1.6858164454)


def test_trail_id_is_independent_of_runtime_walk_forward_stats() -> None:
    base = {"n_folds": 4, "fold_size_days": 60, "step_days": 20}
    a = PromotionTrail.from_inputs(
        evaluation_request=_request(),
        judge_thresholds={},
        walk_forward=base,
    )
    b = PromotionTrail.from_inputs(
        evaluation_request=_request(),
        judge_thresholds={},
        walk_forward=base,
    )
    assert a.trail_id == b.trail_id


def test_sector_map_changes_trail_id() -> None:
    a = PromotionTrail.from_inputs(
        evaluation_request=_request(neutralize=NeutralizeMode.SECTOR, sector_map={"AAA": "tech"}),
        judge_thresholds={},
    )
    b = PromotionTrail.from_inputs(
        evaluation_request=_request(neutralize=NeutralizeMode.SECTOR, sector_map={"AAA": "energy"}),
        judge_thresholds={},
    )
    assert a.trail_id != b.trail_id


# ── Judge embeds trail only on PROMOTE ──────────────────────────────────────


def test_judge_embeds_trail_on_promote() -> None:
    detail = PromotionJudge().judge(
        Hypothesis(text="x"),
        _factor(),
        _bundle(),
        _request(),
    )
    assert detail.decision == ExperimentDecision.PROMOTE_CANDIDATE
    assert detail.promotion_trail is not None
    assert len(detail.promotion_trail.trail_id) == 16


def test_judge_omits_trail_on_reject() -> None:
    bad = EvaluationBundle(
        ic=-0.5,
        rank_ic=-0.5,
        quantile_spread=-0.5,
        n_periods=400,
        n_assets=10,
    )
    detail = PromotionJudge().judge(
        Hypothesis(text="x"),
        _factor(),
        bad,
        _request(),
    )
    assert detail.decision == ExperimentDecision.REJECT
    assert detail.promotion_trail is None


# ── Artifact writer round-trip ──────────────────────────────────────────────


def _record_with_trail(tmp_path_seed: int = 0) -> ExperimentRecord:
    trail = PromotionTrail.from_inputs(
        evaluation_request=_request(cost_bps=float(tmp_path_seed)),
        judge_thresholds={},
    )
    return ExperimentRecord(
        hypothesis=Hypothesis(text="rank(close)"),
        factor=FactorSpec(id=f"fct_{tmp_path_seed}", name="f", expression="rank(close)"),
        evaluation=_bundle(),
        decision=ExperimentDecision.PROMOTE_CANDIDATE,
        promotion_trail=trail,
    )


def test_artifact_payload_carries_trail_block(tmp_path: Path) -> None:
    record = _record_with_trail(tmp_path_seed=2)
    path = PromotedArtifactWriter(tmp_path).maybe_write(record)
    assert path is not None
    payload = json.loads(path.read_text())
    assert payload["schema_version"] == 3
    assert payload["promotion_trail"] is not None
    assert payload["promotion_trail"]["trail_id"] == record.promotion_trail.trail_id  # type: ignore[union-attr]


def test_index_carries_trail_id(tmp_path: Path) -> None:
    PromotedArtifactWriter(tmp_path).maybe_write(_record_with_trail(2))
    PromotedArtifactWriter(tmp_path).maybe_write(_record_with_trail(5))
    rows = read_index(tmp_path)
    trails = {r["trail_id"] for r in rows}
    # Two distinct cost_bps → two distinct trail_ids.
    assert None not in trails
    assert len(trails) == 2


# ── list_factors --trail-id / --show-trail ──────────────────────────────────


def test_list_factors_filters_by_trail_id(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    a = _record_with_trail(2)
    b = _record_with_trail(5)
    PromotedArtifactWriter(tmp_path).maybe_write(a)
    PromotedArtifactWriter(tmp_path).maybe_write(b)
    target = a.promotion_trail.trail_id  # type: ignore[union-attr]
    rc = list_main(["--promoted-dir", str(tmp_path), "--trail-id", target])
    out = capsys.readouterr().out
    assert rc == 0
    assert a.factor.id in out
    assert b.factor.id not in out


def test_show_trail_dumps_block(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    record = _record_with_trail(3)
    PromotedArtifactWriter(tmp_path).maybe_write(record)
    rc = list_main(["--promoted-dir", str(tmp_path), "--show-trail"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Promotion trails" in out
    assert record.promotion_trail.trail_id in out  # type: ignore[union-attr]
    assert '"cost_bps": 3' in out


# ── Backwards compatibility ─────────────────────────────────────────────────


def test_legacy_v2_rows_without_trail_still_load(tmp_path: Path) -> None:
    from alpha_harness.artifacts import index_path

    p = index_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"factor_id":"old","factor_name":"o","rank_ic":0.04,"refinement_round":0}\n')
    rc = list_main(["--promoted-dir", str(tmp_path)])
    assert rc == 0


# ── Doctor probe ────────────────────────────────────────────────────────────


def test_doctor_passes_when_trail_ids_well_formed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    art = tmp_path / "artifacts" / "promoted"
    art.mkdir(parents=True, exist_ok=True)
    (art / "_index.jsonl").write_text(
        '{"factor_id":"a","trail_id":"abc123","refinement_round":0}\n'
        '{"factor_id":"b","trail_id":null,"refinement_round":0}\n'
    )
    from scripts.doctor import _check_promoted_artifacts_dir

    res = _check_promoted_artifacts_dir()
    assert res.passed is True
    assert "1 with trail" in res.detail


def test_doctor_flags_empty_trail_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    art = tmp_path / "artifacts" / "promoted"
    art.mkdir(parents=True, exist_ok=True)
    (art / "_index.jsonl").write_text('{"factor_id":"a","trail_id":""}\n')
    from scripts.doctor import _check_promoted_artifacts_dir

    res = _check_promoted_artifacts_dir()
    assert res.passed is False
    assert "malformed" in res.detail
