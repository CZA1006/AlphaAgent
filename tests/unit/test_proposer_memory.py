"""Unit tests for Round 4A.4 proposer memory digest."""

from __future__ import annotations

from alpha_harness.hermes_boundary.contracts import ThemeCycleRequest
from alpha_harness.proposer.memory import build_memory_digest
from alpha_harness.proposer.prompts import build_user_prompt
from alpha_harness.proposer.schemas import ProposalRequest
from alpha_harness.schemas.evaluation import EvaluationBundle
from alpha_harness.schemas.experiment import (
    ExperimentDecision,
    ExperimentRecord,
    FailureCategory,
    FailureRecord,
)
from alpha_harness.schemas.factor import FactorSpec
from alpha_harness.schemas.hypothesis import Hypothesis


def _record(
    *,
    expression: str,
    decision: ExperimentDecision,
    ic: float | None = 0.03,
    failure_category: FailureCategory | None = None,
) -> ExperimentRecord:
    return ExperimentRecord(
        hypothesis=Hypothesis(text=expression),
        factor=FactorSpec(name="f", expression=expression),
        evaluation=EvaluationBundle(ic=ic),
        decision=decision,
        failure=(
            FailureRecord(category=failure_category)
            if failure_category is not None
            else None
        ),
    )


# ── Summarizer ──────────────────────────────────────────────────────────────


def test_empty_records_return_empty_string() -> None:
    assert build_memory_digest([]) == ""


def test_digest_includes_header_counts() -> None:
    records = [
        _record(expression="rank(close)", decision=ExperimentDecision.PROMOTE_CANDIDATE),
        _record(expression="zscore(close)", decision=ExperimentDecision.REJECT,
                failure_category=FailureCategory.WEAK_SIGNAL),
        _record(expression="ts_mean(close, 5)", decision=ExperimentDecision.REFINE),
    ]
    digest = build_memory_digest(records)
    assert "Recent experiments (last 3)" in digest
    assert "promoted=1" in digest
    assert "refined=1" in digest
    assert "rejected=1" in digest


def test_digest_lists_promoted_expressions() -> None:
    records = [
        _record(expression="rank(close)", decision=ExperimentDecision.PROMOTE_CANDIDATE, ic=0.07),
    ]
    digest = build_memory_digest(records)
    assert "Already promoted" in digest
    assert "`rank(close)`" in digest
    assert "ic=0.070" in digest


def test_digest_lists_rejection_categories() -> None:
    records = [
        _record(expression=f"expr_{i}", decision=ExperimentDecision.REJECT,
                failure_category=FailureCategory.WEAK_SIGNAL)
        for i in range(3)
    ] + [
        _record(expression="bad", decision=ExperimentDecision.REJECT,
                failure_category=FailureCategory.DUPLICATE),
    ]
    digest = build_memory_digest(records)
    assert "Recent rejection modes" in digest
    assert "weak_signal: 3" in digest
    assert "duplicate: 1" in digest


def test_digest_respects_depth() -> None:
    records = [
        _record(expression=f"expr_{i}", decision=ExperimentDecision.PROMOTE_CANDIDATE)
        for i in range(25)
    ]
    digest = build_memory_digest(records, depth=5)
    assert "last 5" in digest
    assert "expr_0" in digest
    # expr_6..24 should not leak into the top-promoted list.
    assert "expr_20" not in digest


def test_digest_truncates_to_max_chars() -> None:
    records = [
        _record(expression=f"long_expression_{i}_" + "x" * 50,
                decision=ExperimentDecision.PROMOTE_CANDIDATE)
        for i in range(20)
    ]
    digest = build_memory_digest(records, max_chars=300)
    assert len(digest) <= 300
    assert digest.endswith("…[truncated]")


def test_digest_dedupes_repeated_expressions() -> None:
    records = [
        _record(expression="rank(close)", decision=ExperimentDecision.PROMOTE_CANDIDATE)
        for _ in range(5)
    ]
    digest = build_memory_digest(records)
    # The same expression should appear at most once in the promoted list
    # and once in the fingerprints section.
    assert digest.count("`rank(close)`") <= 2


# ── Prompt assembly integration ─────────────────────────────────────────────


def test_user_prompt_includes_memory_section() -> None:
    req = ProposalRequest(
        theme="momentum",
        n_candidates=2,
        prior_memory="Recent experiments (last 3): promoted=1 refined=0 rejected=2",
    )
    prompt = build_user_prompt(req)
    assert "What has already been tried" in prompt
    assert "promoted=1 refined=0 rejected=2" in prompt


def test_user_prompt_omits_memory_when_empty() -> None:
    req = ProposalRequest(theme="momentum", n_candidates=2, prior_memory="")
    prompt = build_user_prompt(req)
    assert "What has already been tried" not in prompt


def test_user_prompt_shows_both_extra_guidance_and_memory() -> None:
    req = ProposalRequest(
        theme="momentum",
        n_candidates=2,
        prior_memory="Recent experiments (last 1): promoted=0",
        extra_guidance="focus on volatility regimes",
    )
    prompt = build_user_prompt(req)
    assert "What has already been tried" in prompt
    assert "Extra guidance" in prompt
    assert "focus on volatility regimes" in prompt


# ── Contract plumbing ───────────────────────────────────────────────────────


def test_theme_cycle_request_accepts_prior_memory() -> None:
    req = ThemeCycleRequest(theme="t", prior_memory="hello")
    assert req.prior_memory == "hello"


def test_theme_cycle_request_prior_memory_default_empty() -> None:
    req = ThemeCycleRequest(theme="t")
    assert req.prior_memory == ""
