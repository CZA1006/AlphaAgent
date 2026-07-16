"""Round 9 Phase C — inspect_composite CLI (read-only auditor)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.inspect_composite import (
    main,
    render_detail,
    render_list,
)


def _write_artifact(
    promoted_dir: Path,
    *,
    factor_id: str,
    recipe_id: str,
    method: str = "equal_weight",
    components: list[str] | None = None,
    ic: float = 0.030,
    rank_ic: float = 0.040,
    promoted_at: str = "2026-05-21T00:00:00+00:00",
    parent_factor_id: str | None = None,
    refinement_round: int = 0,
    is_composite: bool = True,
) -> None:
    promoted_dir.mkdir(parents=True, exist_ok=True)
    components = components or ["rank(close)", "rank(volume)"]
    artifact: dict = {
        "schema_version": 3,
        "factor_id": factor_id,
        "factor_name": f"composite_{recipe_id}",
        "expression": f"combine.{method}([{', '.join(components)}])",
        "composite_recipe": (
            {
                "method": method,
                "components": components,
                "component_factor_ids": [],
                "recipe_id": recipe_id,
            }
            if is_composite
            else None
        ),
        "parent_factor_id": parent_factor_id,
        "refinement_round": refinement_round,
        "evaluation": {
            "ic": ic,
            "rank_ic": rank_ic,
            "quantile_spread": 0.01,
            "net_quantile_spread": 0.005,
            "turnover": 0.2,
            "n_periods": 200,
            "n_assets": 50,
        },
        "promotion_trail": {
            "trail_id": "abc123",
            "neutralize": "sector",
            "cost_bps": 5.0,
            "forecast_horizon_bars": 5,
            "holdout_strategy": "tail",
        },
        "promoted_at": promoted_at,
        "cycle_id": "cycle-1",
    }
    (promoted_dir / f"{factor_id}.json").write_text(json.dumps(artifact))
    row = {
        "factor_id": factor_id,
        "expression": artifact["expression"],
        "ic": ic,
        "rank_ic": rank_ic,
        "promoted_at": promoted_at,
    }
    with (promoted_dir / "_index.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


# ── render_list ─────────────────────────────────────────────────────────────


def test_render_list_empty_dir(tmp_path: Path) -> None:
    out = render_list(tmp_path)
    assert "No promoted composites" in out


def test_render_list_shows_recipe_rows(tmp_path: Path) -> None:
    _write_artifact(
        tmp_path,
        factor_id="composite_aaa_111111",
        recipe_id="aaa",
        ic=0.03,
        rank_ic=0.04,
    )
    _write_artifact(
        tmp_path,
        factor_id="composite_bbb_222222",
        recipe_id="bbb",
        method="zscore_average",
        ic=0.05,
        rank_ic=0.06,
        promoted_at="2026-05-22T00:00:00+00:00",
    )
    out = render_list(tmp_path)
    assert "aaa" in out
    assert "bbb" in out
    assert "zscore_average" in out
    # Newest first → bbb appears before aaa
    assert out.index("bbb") < out.index("aaa")
    assert "total: 2 composite(s)" in out


def test_render_list_skips_scalar_artifacts(tmp_path: Path) -> None:
    _write_artifact(
        tmp_path,
        factor_id="scalar_xyz",
        recipe_id="ignored",
        is_composite=False,
    )
    out = render_list(tmp_path)
    assert "No promoted composites" in out


# ── render_detail ──────────────────────────────────────────────────────────


def test_render_detail_finds_composite(tmp_path: Path) -> None:
    _write_artifact(tmp_path, factor_id="composite_aaa_111111", recipe_id="aaaa")
    out = render_detail(tmp_path, "aaaa")
    assert "recipe_id=aaaa" in out
    assert "equal_weight" in out
    assert "rank(close)" in out
    assert "trail_id" in out
    assert "Regime trail" in out


def test_render_detail_missing_recipe(tmp_path: Path) -> None:
    _write_artifact(tmp_path, factor_id="composite_aaa_111111", recipe_id="aaaa")
    out = render_detail(tmp_path, "nonexistent")
    assert "No composite" in out


def test_render_detail_walks_ancestry(tmp_path: Path) -> None:
    """Refined composite → ancestry chain shows parent recipe."""
    _write_artifact(
        tmp_path,
        factor_id="parent_id",
        recipe_id="parent_rec",
        promoted_at="2026-05-20T00:00:00+00:00",
    )
    _write_artifact(
        tmp_path,
        factor_id="child_id",
        recipe_id="child_rec",
        parent_factor_id="parent_id",
        refinement_round=1,
        promoted_at="2026-05-21T00:00:00+00:00",
    )
    out = render_detail(tmp_path, "child_rec")
    assert "Refinement ancestry (2 step(s)" in out
    assert "parent_rec" in out
    assert "child_rec" in out


# ── main() entry ────────────────────────────────────────────────────────────


def test_main_list_mode(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_artifact(tmp_path, factor_id="composite_aaa_111111", recipe_id="aaaa")
    rc = main(["--list", "--promoted-dir", str(tmp_path)])
    assert rc == 0
    captured = capsys.readouterr()
    assert "aaaa" in captured.out


def test_main_recipe_id_mode(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_artifact(tmp_path, factor_id="composite_aaa_111111", recipe_id="aaaa")
    rc = main(["--recipe-id", "aaaa", "--promoted-dir", str(tmp_path)])
    assert rc == 0
    captured = capsys.readouterr()
    assert "recipe_id=aaaa" in captured.out


def test_main_missing_dir_errors(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["--list", "--promoted-dir", str(tmp_path / "no_such")])
    assert rc == 2
    captured = capsys.readouterr()
    assert "not found" in captured.err
