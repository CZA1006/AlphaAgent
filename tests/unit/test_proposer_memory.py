"""Unit tests for Round 4A.4 proposer memory digest."""

from __future__ import annotations

from datetime import UTC, datetime

from alpha_harness.hermes_boundary.contracts import ThemeCycleRequest
from alpha_harness.proposer.memory import build_memory_digest
from alpha_harness.proposer.prompts import build_user_prompt
from alpha_harness.proposer.schemas import ProposalRequest
from alpha_harness.reports.validation import FactorThumbnail, StrictValidationReport
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
            FailureRecord(category=failure_category) if failure_category is not None else None
        ),
    )


# ── Summarizer ──────────────────────────────────────────────────────────────


def test_empty_records_return_empty_string() -> None:
    assert build_memory_digest([]) == ""


def test_digest_includes_header_counts() -> None:
    records = [
        _record(expression="rank(close)", decision=ExperimentDecision.PROMOTE_CANDIDATE),
        _record(
            expression="zscore(close)",
            decision=ExperimentDecision.REJECT,
            failure_category=FailureCategory.WEAK_SIGNAL,
        ),
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
        _record(
            expression=f"expr_{i}",
            decision=ExperimentDecision.REJECT,
            failure_category=FailureCategory.WEAK_SIGNAL,
        )
        for i in range(3)
    ] + [
        _record(
            expression="bad",
            decision=ExperimentDecision.REJECT,
            failure_category=FailureCategory.DUPLICATE,
        ),
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
        _record(
            expression=f"long_expression_{i}_" + "x" * 50,
            decision=ExperimentDecision.PROMOTE_CANDIDATE,
        )
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


def test_digest_consumes_durable_validation_reports() -> None:
    now = datetime.now(UTC)
    report = StrictValidationReport(
        cycle_id="prior-cycle",
        regime_trail_id="trail-1",
        started_at=now,
        finished_at=now,
        n_proposals=2,
        n_promoted=1,
        n_refined=0,
        n_rejected=1,
        factors=[
            FactorThumbnail(
                factor_id="promoted-1",
                expression="rank(ts_mean(close, 20))",
                decision=ExperimentDecision.PROMOTE_CANDIDATE.value,
                ic=0.04,
            ),
            FactorThumbnail(
                factor_id="rejected-1",
                expression="rank(ts_std(close, 5))",
                decision=ExperimentDecision.REJECT.value,
                gate="tail_concentration",
            ),
        ],
    )

    digest = build_memory_digest([], validation_reports=[report])

    assert "promoted=1" in digest
    assert "rejected=1" in digest
    assert "`rank(ts_mean(close, 20))` ic=0.040" in digest
    assert "tail_concentration: 1" in digest
    assert "`rank(ts_std(close, 5))`" in digest


def test_live_records_precede_durable_history_at_depth_limit() -> None:
    now = datetime.now(UTC)
    report = StrictValidationReport(
        cycle_id="prior-cycle",
        regime_trail_id="trail-1",
        started_at=now,
        finished_at=now,
        n_proposals=1,
        n_promoted=1,
        n_refined=0,
        n_rejected=0,
        factors=[
            FactorThumbnail(
                factor_id="old",
                expression="rank(volume)",
                decision=ExperimentDecision.PROMOTE_CANDIDATE.value,
            ),
        ],
    )
    live = _record(
        expression="rank(close)",
        decision=ExperimentDecision.PROMOTE_CANDIDATE,
    )

    digest = build_memory_digest([live], depth=1, validation_reports=[report])

    assert "`rank(close)`" in digest
    assert "`rank(volume)`" not in digest


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


# ── Round 9 A.1: promoted composites from artifact index ───────────────────


def _write_promoted_index(
    tmp_path,
    *,
    factor_id: str,
    method: str = "equal_weight",
    components: list[str] | None = None,
    ic: float = 0.03,
    rank_ic: float = 0.04,
    promoted_at: str = "2026-05-21T00:00:00+00:00",
    include_recipe: bool = True,
) -> None:
    """Write a {factor_id}.json + append an _index.jsonl row.

    Mirrors the on-disk shape PromotedArtifactWriter produces so the
    digest helper has something realistic to read.
    """
    import hashlib as _hashlib
    import json as _json

    components = components or ["rank(close)", "rank(volume)"]
    # Derive a stable per-(method, components) recipe_id so dedupe
    # tests can distinguish "same recipe" from "different recipe".
    recipe_id = _hashlib.sha256(
        (method + "|" + "|".join(components)).encode("utf-8"),
    ).hexdigest()[:16]
    artifact = {
        "schema_version": 3,
        "factor_id": factor_id,
        "factor_name": factor_id,
        "expression": f"combine.{method}([{', '.join(components)}])",
        "composite_recipe": (
            {
                "method": method,
                "components": components,
                "component_factor_ids": [],
                "recipe_id": recipe_id,
            }
            if include_recipe
            else None
        ),
    }
    (tmp_path / f"{factor_id}.json").write_text(_json.dumps(artifact))
    idx = tmp_path / "_index.jsonl"
    row = {
        "factor_id": factor_id,
        "expression": artifact["expression"],
        "ic": ic,
        "rank_ic": rank_ic,
        "promoted_at": promoted_at,
    }
    with idx.open("a", encoding="utf-8") as fh:
        fh.write(_json.dumps(row) + "\n")


def test_composites_section_emitted_when_index_present(tmp_path) -> None:
    _write_promoted_index(
        tmp_path,
        factor_id="composite_aaa_111111",
        ic=0.030,
        rank_ic=0.040,
    )
    records = [
        _record(expression="rank(close)", decision=ExperimentDecision.PROMOTE_CANDIDATE),
    ]
    digest = build_memory_digest(
        records,
        promoted_index_path=tmp_path / "_index.jsonl",
    )
    assert "Recently promoted composites" in digest
    assert "combine.equal_weight" in digest
    assert "recipe_id=" in digest
    assert "ic=+0.030" in digest
    assert "rank_ic=+0.040" in digest


def test_composites_section_skipped_when_path_unset() -> None:
    records = [
        _record(expression="rank(close)", decision=ExperimentDecision.PROMOTE_CANDIDATE),
    ]
    digest = build_memory_digest(records)  # no promoted_index_path
    assert "Recently promoted composites" not in digest


def test_composites_section_skipped_when_index_missing() -> None:
    """Missing index file is silent — not an error."""
    records = [
        _record(expression="rank(close)", decision=ExperimentDecision.PROMOTE_CANDIDATE),
    ]
    digest = build_memory_digest(
        records,
        promoted_index_path="/nonexistent/path/_index.jsonl",
    )
    assert "Recently promoted composites" not in digest


def test_composites_section_ignores_non_composite_artifacts(tmp_path) -> None:
    """Scalar promotions (no composite_recipe field) must not appear."""
    _write_promoted_index(
        tmp_path,
        factor_id="scalar_factor_xyz",
        include_recipe=False,
    )
    digest = build_memory_digest(
        [_record(expression="rank(close)", decision=ExperimentDecision.PROMOTE_CANDIDATE)],
        promoted_index_path=tmp_path / "_index.jsonl",
    )
    assert "Recently promoted composites" not in digest


def test_composites_section_dedupes_by_recipe_id(tmp_path) -> None:
    """Same recipe_id promoted twice should appear once in the digest."""
    _write_promoted_index(
        tmp_path,
        factor_id="composite_aaa_111111",
        promoted_at="2026-05-20T00:00:00+00:00",
    )
    _write_promoted_index(
        tmp_path,
        factor_id="composite_aaa_222222",
        promoted_at="2026-05-21T00:00:00+00:00",
    )
    digest = build_memory_digest(
        [_record(expression="rank(close)", decision=ExperimentDecision.PROMOTE_CANDIDATE)],
        promoted_index_path=tmp_path / "_index.jsonl",
        top_composites=5,
    )
    # Same recipe_id appears in both rows → dedupe → exactly 1 bullet.
    assert digest.count("combine.equal_weight") == 1


def test_composites_section_caps_at_top_composites(tmp_path) -> None:
    for i in range(5):
        _write_promoted_index(
            tmp_path,
            factor_id=f"composite_aaa_{i:06d}",
            components=[f"rank(close + {i})"],  # distinct recipes
            promoted_at=f"2026-05-{20 + i:02d}T00:00:00+00:00",
        )
    digest = build_memory_digest(
        [_record(expression="x", decision=ExperimentDecision.PROMOTE_CANDIDATE)],
        promoted_index_path=tmp_path / "_index.jsonl",
        top_composites=2,
    )
    # Each entry starts with "  - combine."
    assert digest.count("  - combine.") == 2


def test_composites_only_when_no_records(tmp_path) -> None:
    """Records empty but composite index non-empty → digest is just composites."""
    _write_promoted_index(tmp_path, factor_id="composite_aaa_111111")
    digest = build_memory_digest(
        [],
        promoted_index_path=tmp_path / "_index.jsonl",
    )
    assert "Recently promoted composites" in digest
    assert "Recent experiments" not in digest  # scalar header skipped
