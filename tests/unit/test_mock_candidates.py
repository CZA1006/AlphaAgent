"""Round 4A.10 — fast unit guard on the mock-LLM candidate fixtures.

The autonomous-cycle smoke test depends on every entry in
``scripts.autonomous_cycle._MOCK_CANDIDATES`` compiling cleanly through
the factor DSL.  A typo there would only surface during the slow
integration smoke run; this unit test catches it in milliseconds.
"""

from __future__ import annotations

from alpha_harness.factors.compiler import FactorDslCompiler
from alpha_harness.proposer.prompts import build_system_prompt
from alpha_harness.proposer.schemas import RawProposalBatch
from alpha_harness.schemas.hypothesis import Hypothesis
from scripts.autonomous_cycle import _MOCK_CANDIDATES
from scripts.validate_strict import _mock_candidates_for_preset


def test_mock_candidates_are_non_empty() -> None:
    assert len(_MOCK_CANDIDATES) >= 3


def test_mock_candidates_form_valid_batch() -> None:
    """The handler returns the batch JSON; it must be Pydantic-valid."""
    batch = RawProposalBatch(proposals=_MOCK_CANDIDATES)
    assert len(batch.proposals) == len(_MOCK_CANDIDATES)


def test_every_mock_candidate_compiles() -> None:
    compiler = FactorDslCompiler()
    for cand in _MOCK_CANDIDATES:
        spec = compiler.compile(
            Hypothesis(text=cand.expression, rationale=cand.rationale),
        )
        assert spec.expression == cand.expression
        assert spec.operator_tree is not None


def test_hk_ipo_event_mock_candidates_compile() -> None:
    compiler = FactorDslCompiler()
    candidates = _mock_candidates_for_preset("hk_ipo_events")
    assert len(candidates) >= 5
    for cand in candidates:
        spec = compiler.compile(
            Hypothesis(text=cand.expression, rationale=cand.rationale),
        )
        assert spec.expression == cand.expression
        assert spec.operator_tree is not None


def test_proposer_prompt_documents_hk_ipo_event_fields() -> None:
    prompt = build_system_prompt()
    assert "days_to_next_cornerstone_lockup" in prompt
    assert "is_pre_greenshoe_expiry_5d" in prompt
    assert "is_stabilization_window_active" in prompt


# ── Doctor probe ────────────────────────────────────────────────────────────


def test_doctor_smoke_probe_passes_on_clean_tree() -> None:
    from scripts.doctor import _check_smoke_can_run

    result = _check_smoke_can_run()
    assert result.passed is True
    assert "compile" in result.detail
