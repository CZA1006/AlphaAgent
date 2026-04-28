"""Round 4A.7 — promoted-artifact lineage + list_factors lineage views."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alpha_harness.artifacts import PromotedArtifactWriter, index_path, read_index
from alpha_harness.schemas.evaluation import EvaluationBundle
from alpha_harness.schemas.experiment import (
    ExperimentDecision,
    ExperimentRecord,
)
from alpha_harness.schemas.factor import FactorSpec
from alpha_harness.schemas.hypothesis import Hypothesis
from scripts.list_factors import main as list_main


def _record(
    *,
    factor_id: str = "fct_x",
    parent_factor_id: str | None = None,
    refinement_round: int = 0,
    rank_ic: float = 0.06,
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
        decision=ExperimentDecision.PROMOTE_CANDIDATE,
    )


# ── Payload + index lineage ─────────────────────────────────────────────────


def test_payload_includes_lineage_fields(tmp_path: Path) -> None:
    writer = PromotedArtifactWriter(tmp_path)
    path = writer.maybe_write(
        _record(factor_id="fct_root"),
    )
    assert path is not None
    payload = json.loads(path.read_text())
    assert payload["parent_factor_id"] is None
    assert payload["refinement_round"] == 0
    assert payload["schema_version"] == 2


def test_payload_records_refined_lineage(tmp_path: Path) -> None:
    writer = PromotedArtifactWriter(tmp_path)
    path = writer.maybe_write(
        _record(
            factor_id="fct_child",
            parent_factor_id="fct_root",
            refinement_round=1,
        ),
    )
    assert path is not None
    payload = json.loads(path.read_text())
    assert payload["parent_factor_id"] == "fct_root"
    assert payload["refinement_round"] == 1


def test_index_includes_lineage_fields(tmp_path: Path) -> None:
    writer = PromotedArtifactWriter(tmp_path)
    writer.maybe_write(_record(factor_id="fct_a"))
    writer.maybe_write(
        _record(factor_id="fct_b", parent_factor_id="fct_a", refinement_round=1),
    )
    index = read_index(tmp_path)
    by_id = {e["factor_id"]: e for e in index}
    assert by_id["fct_a"]["parent_factor_id"] is None
    assert by_id["fct_a"]["refinement_round"] == 0
    assert by_id["fct_b"]["parent_factor_id"] == "fct_a"
    assert by_id["fct_b"]["refinement_round"] == 1


# ── Backwards compatibility: old rows still parse ──────────────────────────


def test_legacy_index_rows_without_lineage_still_load(tmp_path: Path) -> None:
    idx = index_path(tmp_path)
    idx.parent.mkdir(parents=True, exist_ok=True)
    # Pre-4A.7 row: no parent_factor_id / refinement_round keys.
    idx.write_text(
        '{"factor_id":"old","factor_name":"o","rank_ic":0.04,'
        '"promoted_at":"2025-06-01T00:00:00+00:00"}\n'
    )
    rc = list_main(["--promoted-dir", str(tmp_path)])
    assert rc == 0


# ── list_factors filters ────────────────────────────────────────────────────


def test_list_factors_min_refinement_round_filter(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    writer = PromotedArtifactWriter(tmp_path)
    writer.maybe_write(_record(factor_id="root1"))
    writer.maybe_write(
        _record(factor_id="kid1", parent_factor_id="root1", refinement_round=1),
    )
    rc = list_main(
        [
            "--promoted-dir",
            str(tmp_path),
            "--min-refinement-round",
            "1",
        ],
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "kid1" in out
    assert "root1" not in out


def test_list_factors_max_refinement_round_filter(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    writer = PromotedArtifactWriter(tmp_path)
    writer.maybe_write(_record(factor_id="root1"))
    writer.maybe_write(
        _record(factor_id="kid1", parent_factor_id="root1", refinement_round=2),
    )
    rc = list_main(
        [
            "--promoted-dir",
            str(tmp_path),
            "--max-refinement-round",
            "0",
        ],
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "root1" in out
    assert "kid1" not in out


def test_list_factors_lineage_flag_renders_tree(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    writer = PromotedArtifactWriter(tmp_path)
    writer.maybe_write(_record(factor_id="root1", rank_ic=0.05))
    writer.maybe_write(
        _record(
            factor_id="mid1",
            parent_factor_id="root1",
            refinement_round=1,
            rank_ic=0.06,
        ),
    )
    writer.maybe_write(
        _record(
            factor_id="leaf1",
            parent_factor_id="mid1",
            refinement_round=2,
            rank_ic=0.07,
        ),
    )
    rc = list_main(["--promoted-dir", str(tmp_path), "--lineage"])
    out = capsys.readouterr().out
    assert rc == 0
    # The inline columns should expose r= and parent= for each row.
    assert "r=0" in out
    assert "r=1" in out
    assert "r=2" in out
    assert "parent=root1" in out
    assert "parent=mid1" in out
    # The lineage tree section should put root above leaf in document order.
    assert "Lineage trees" in out
    tree = out[out.index("Lineage trees") :]
    assert tree.index("root1") < tree.index("mid1") < tree.index("leaf1")


def test_list_factors_lineage_disconnected_parent_treated_as_root(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Child references a parent that is NOT in the visible set —
    # tree renderer should still produce output and not crash.
    writer = PromotedArtifactWriter(tmp_path)
    writer.maybe_write(
        _record(
            factor_id="orphan",
            parent_factor_id="vanished_root",
            refinement_round=1,
        ),
    )
    rc = list_main(["--promoted-dir", str(tmp_path), "--lineage"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "orphan" in out
    assert "Lineage trees" in out


# ── Orchestrator summary ────────────────────────────────────────────────────


def test_orchestrator_summary_reports_refinement_rounds_seen() -> None:
    from datetime import date

    from alpha_harness.evaluators.promotion_judge import PromotionJudge
    from alpha_harness.factors.compiler import FactorDslCompiler
    from alpha_harness.orchestrator.refinement import (
        RefinementConfig,
        RefinementRunner,
    )
    from alpha_harness.orchestrator.research_loop import ResearchOrchestrator
    from alpha_harness.registries.experiment import ExperimentRegistry
    from alpha_harness.registries.hypothesis import HypothesisRegistry
    from alpha_harness.schemas.evaluation import EvaluationRequest
    from alpha_harness.service import AlphaHarnessService

    class _Eval:
        def evaluate(
            self,
            factor: FactorSpec,
            request: EvaluationRequest,
        ) -> EvaluationBundle:
            # Borderline → REFINE for the root expression, default
            # below-threshold → REJECT for everything else.
            if factor.expression == "rank(ts_mean(close, 20))":
                return EvaluationBundle(
                    ic=0.023,
                    rank_ic=0.035,
                    quantile_spread=0.006,
                )
            return EvaluationBundle(ic=0.0, rank_ic=0.0, quantile_spread=0.0)

    service = AlphaHarnessService(
        compiler=FactorDslCompiler(),
        evaluator=_Eval(),
        judge=PromotionJudge(),
    )
    experiments = ExperimentRegistry()
    orch = ResearchOrchestrator(
        service=service,
        experiment_registry=experiments,
        hypothesis_registry=HypothesisRegistry(),
    )
    runner = RefinementRunner(orch, config=RefinementConfig(max_depth=1))
    runner.run(
        Hypothesis(text="rank(ts_mean(close, 20))"),
        EvaluationRequest(
            factor_id="p",
            universe_id="u",
            eval_start=date(2020, 1, 1),
            eval_end=date(2023, 12, 31),
        ),
    )
    summary = orch.summary()
    rounds = summary["refinement_rounds_seen"]
    assert isinstance(rounds, dict)
    assert rounds.get(0) == 1  # root
    assert rounds.get(1, 0) >= 1  # at least one child


# ── Doctor probe — schema integrity ────────────────────────────────────────


def test_doctor_promoted_artifacts_rejects_malformed_round(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The schema probe should flag a non-integer refinement_round."""
    monkeypatch.chdir(tmp_path)
    art = tmp_path / "artifacts" / "promoted"
    art.mkdir(parents=True, exist_ok=True)
    (art / "_index.jsonl").write_text(
        '{"factor_id":"good","refinement_round":0}\n'
        '{"factor_id":"bad","refinement_round":"oops"}\n',
    )
    from scripts.doctor import _check_promoted_artifacts_dir

    result = _check_promoted_artifacts_dir()
    assert result.passed is False
    assert "malformed" in result.detail


def test_doctor_promoted_artifacts_passes_clean_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    art = tmp_path / "artifacts" / "promoted"
    art.mkdir(parents=True, exist_ok=True)
    (art / "_index.jsonl").write_text(
        '{"factor_id":"a","refinement_round":0}\n'
        '{"factor_id":"b","refinement_round":1,"parent_factor_id":"a"}\n',
    )
    from scripts.doctor import _check_promoted_artifacts_dir

    result = _check_promoted_artifacts_dir()
    assert result.passed is True
    assert "1 from refinement" in result.detail
