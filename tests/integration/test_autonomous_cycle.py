"""End-to-end test for the autonomous cycle script.

Drives :func:`scripts.autonomous_cycle.main` with ``--mock-llm`` so no
API key is required, and asserts that the script exits cleanly and the
JSON payload it prints contains a non-empty :class:`ThemeCycleResponse`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.autonomous_cycle as autonomous_module
from alpha_harness.combination import CombinationMethod, CombinationRecipe
from alpha_harness.proposer.schemas import RawProposal

autonomous_main = autonomous_module.main


def test_autonomous_cycle_mock_llm_end_to_end(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = autonomous_main(
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
            "--json",
        ]
    )
    assert exit_code == 0

    out = capsys.readouterr().out.strip()
    # The script emits other log lines too; the JSON payload is the last
    # well-formed JSON object on stdout.
    payload = json.loads(out)
    assert payload["proposals_requested"] == 2
    assert payload["proposals_accepted"] >= 1
    assert isinstance(payload["roots"], list)
    assert payload["roots"], "expected at least one root cycle"

    outcomes = {r["outcome"] for r in payload["roots"]}
    assert outcomes.issubset({"promoted", "refined", "rejected", "error"})


def test_autonomous_cycle_live_without_api_key_exits_cleanly(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live mode without OPENROUTER_API_KEY must fail fast with a hint."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    exit_code = autonomous_main(
        [
            "--n-candidates",
            "1",
            "--n-days",
            "90",
            "--n-symbols",
            "4",
            "--seed",
            "1",
            "--max-depth",
            "0",
            "--max-variants-per-step",
            "1",
            "--max-total-children",
            "0",
        ]
    )

    assert exit_code == 2
    err = capsys.readouterr().err
    assert "OPENROUTER_API_KEY" in err
    assert "--mock-llm" in err


def test_autonomous_cycle_surfaces_refine_branch(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """At least one cycle should land in REFINE (or produce a refinement child).

    The mock LLM always emits the same borderline proposals, and the default
    ``ic_threshold`` of 0.02 with the built-in ``refine_margin`` of 0.20 makes
    a REFINE verdict the most likely outcome for the scripted expressions.
    The assertion is deliberately lenient: we just need to see the refinement
    branch exercised somewhere in the response so the integration surface is
    not silently broken.
    """
    # The default ic_threshold=0.02 is too permissive: strong signals clear it
    # outright and weak ones fall below it, leaving no REFINE verdicts. We
    # deliberately lift the base bar so its N=3 session-adjusted value lands
    # just below one mock candidate's typical IC. The candidate passes but stays
    # inside the default 20% refine margin, so the refinement runner fires.
    exit_code = autonomous_main(
        [
            "--mock-llm",
            "--n-candidates",
            "3",
            "--n-days",
            "180",
            "--n-symbols",
            "8",
            "--seed",
            "11",
            "--ic-threshold",
            "0.065",
            "--max-depth",
            "1",
            "--max-variants-per-step",
            "2",
            "--max-total-children",
            "4",
            "--json",
        ]
    )
    assert exit_code == 0

    payload = json.loads(capsys.readouterr().out.strip())
    refined_roots = [r for r in payload["roots"] if r["outcome"] == "refined"]
    assert refined_roots or payload["refinements"], (
        "Expected at least one refined root or refinement child; got "
        f"roots={payload['roots']!r} refinements={payload['refinements']!r}"
    )


def test_autonomous_cycle_complement_mode_builds_augmented_basket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    promoted_dir = tmp_path / "promoted"
    promoted_dir.mkdir()
    base = CombinationRecipe.build(
        method=CombinationMethod.RANK_AGGREGATE,
        components=["rank(close)", "rank(volume)"],
    )
    factor_id = "composite_base"
    (promoted_dir / f"{factor_id}.json").write_text(
        json.dumps(
            {
                "factor_id": factor_id,
                "composite_recipe": base.model_dump(mode="json"),
            }
        )
    )
    (promoted_dir / "_index.jsonl").write_text(
        json.dumps(
            {
                "factor_id": factor_id,
                "ic": 0.03,
                "rank_ic": 0.04,
                "promoted_at": "2026-07-15T00:00:00+00:00",
            }
        )
        + "\n"
    )
    monkeypatch.setattr(
        autonomous_module,
        "_MOCK_CANDIDATES",
        [
            RawProposal(
                expression="rank(high - low)",
                rationale="range signal diversifies level and volume",
                base_recipe_id=base.recipe_id,
            )
        ],
    )

    exit_code = autonomous_main(
        [
            "--mock-llm",
            "--composite-complements",
            "--promoted-dir",
            str(promoted_dir),
            "--no-promoted-artifacts",
            "--n-candidates",
            "1",
            "--n-days",
            "160",
            "--n-symbols",
            "8",
            "--seed",
            "7",
            "--max-depth",
            "0",
            "--max-total-children",
            "0",
            "--json",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["proposals_accepted"] == 1
    assert payload["roots"][0]["factor_name"].startswith("complement_")


def test_autonomous_cycle_complement_mode_fails_without_anchor(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = autonomous_main(
        [
            "--mock-llm",
            "--composite-complements",
            "--promoted-dir",
            str(tmp_path / "missing"),
            "--n-candidates",
            "1",
            "--n-days",
            "40",
            "--n-symbols",
            "4",
        ]
    )
    assert exit_code == 2
    assert "requires at least one valid promoted composite anchor" in capsys.readouterr().err
