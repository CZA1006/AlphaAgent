"""End-to-end test for the autonomous cycle script.

Drives :func:`scripts.autonomous_cycle.main` with ``--mock-llm`` so no
API key is required, and asserts that the script exits cleanly and the
JSON payload it prints contains a non-empty :class:`ThemeCycleResponse`.
"""

from __future__ import annotations

import json

import pytest

from scripts.autonomous_cycle import main as autonomous_main


def test_autonomous_cycle_mock_llm_end_to_end(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = autonomous_main([
        "--mock-llm",
        "--n-candidates", "2",
        "--n-days", "120",
        "--n-symbols", "8",
        "--seed", "7",
        "--max-depth", "1",
        "--max-variants-per-step", "2",
        "--max-total-children", "3",
        "--json",
    ])
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

    exit_code = autonomous_main([
        "--n-candidates", "1",
        "--n-days", "90",
        "--n-symbols", "4",
        "--seed", "1",
        "--max-depth", "0",
        "--max-variants-per-step", "1",
        "--max-total-children", "0",
    ])

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
    exit_code = autonomous_main([
        "--mock-llm",
        "--n-candidates", "3",
        "--n-days", "180",
        "--n-symbols", "8",
        "--seed", "11",
        "--ic-threshold", "0.065",
        "--max-depth", "1",
        "--max-variants-per-step", "2",
        "--max-total-children", "4",
        "--json",
    ])
    assert exit_code == 0

    payload = json.loads(capsys.readouterr().out.strip())
    refined_roots = [r for r in payload["roots"] if r["outcome"] == "refined"]
    assert refined_roots or payload["refinements"], (
        "Expected at least one refined root or refinement child; got "
        f"roots={payload['roots']!r} refinements={payload['refinements']!r}"
    )
