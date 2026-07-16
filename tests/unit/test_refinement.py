"""Tests for mutation templates and the RefinementRunner."""

from __future__ import annotations

from datetime import date

import pytest

from alpha_harness.evaluators.promotion_judge import PromotionJudge
from alpha_harness.factors.compiler import FactorDslCompiler
from alpha_harness.factors.dsl_parser import parse_expression
from alpha_harness.orchestrator.mutations import propose_mutations, render
from alpha_harness.orchestrator.refinement import (
    RefinementConfig,
    RefinementRunner,
)
from alpha_harness.orchestrator.research_loop import ResearchOrchestrator
from alpha_harness.registries.experiment import ExperimentRegistry
from alpha_harness.registries.hypothesis import HypothesisRegistry
from alpha_harness.registries.memory import MemoryRegistry
from alpha_harness.schemas.evaluation import EvaluationBundle, EvaluationRequest
from alpha_harness.schemas.experiment import ExperimentDecision
from alpha_harness.schemas.factor import FactorSpec
from alpha_harness.schemas.hypothesis import Hypothesis
from alpha_harness.schemas.memory import MemoryCategory
from alpha_harness.service import AlphaHarnessService

DEFAULT_EVAL_REQUEST = EvaluationRequest(
    factor_id="placeholder",
    universe_id="test_universe",
    eval_start=date(2020, 1, 1),
    eval_end=date(2023, 12, 31),
)


# ── render() round-trip ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "expr",
    [
        "close",
        "rank(close)",
        "ts_mean(close, 20)",
        "rank(ts_mean(close, 20))",
        "ts_delta(close, 5) / ts_std(close, 20)",
        "rank(close) + zscore(volume)",
    ],
)
def test_render_roundtrip_is_parseable(expr: str):
    ast = parse_expression(expr)
    rendered = render(ast)
    # Must parse and canonicalize to the same structural form.
    reparsed = parse_expression(rendered)
    from alpha_harness.factors.canonical import canonicalize

    assert canonicalize(ast) == canonicalize(reparsed)


# ── propose_mutations() ─────────────────────────────────────────────────────


def test_mutations_empty_for_unparseable():
    assert propose_mutations("not a valid dsl @@@") == []


def test_mutations_window_scaling():
    variants = propose_mutations("ts_mean(close, 20)")
    labels = [label for _, label in variants]
    assert any(label.startswith("window_halve") for label in labels)
    assert any(label.startswith("window_double") for label in labels)

    # Check the produced expressions actually contain 10 and 40.
    exprs = [e for e, _ in variants]
    assert any("10" in e for e in exprs)
    assert any("40" in e for e in exprs)


def test_mutations_wrap_rank_and_zscore():
    variants = propose_mutations("ts_mean(close, 20)")
    labels = [label for _, label in variants]
    assert "wrap_rank" in labels
    assert "wrap_zscore" in labels


def test_mutations_unwrap_outer_rank():
    variants = propose_mutations("rank(ts_mean(close, 20))")
    labels = [label for _, label in variants]
    assert "unwrap_outer" in labels
    # rank → wrap_zscore should still be proposed, wrap_rank should not.
    assert "wrap_zscore" in labels
    assert "wrap_rank" not in labels


def test_mutations_window_window_of_1_does_not_halve_further():
    # window 1 cannot be halved (would go below 1)
    variants = propose_mutations("ts_mean(close, 1)")
    labels = [label for _, label in variants]
    assert not any(label.startswith("window_halve") for label in labels)
    # Doubling to 2 is still allowed.
    assert any(label.startswith("window_double") for label in labels)


def test_mutations_are_deduplicated():
    # Applying unwrap then window-scale can't produce the same string twice.
    variants = propose_mutations("rank(ts_mean(close, 20))")
    exprs = [e for e, _ in variants]
    assert len(exprs) == len(set(exprs))


# ── RefinementRunner — test helpers ─────────────────────────────────────────


class _ScriptedEvaluator:
    """Evaluator whose output depends on the expression it receives.

    Maps an expression to an (ic, rank_ic, quantile_spread) triple; missing
    expressions fall back to ``default``.
    """

    def __init__(
        self,
        mapping: dict[str, tuple[float, float, float]],
        default: tuple[float, float, float] = (0.0, 0.0, 0.0),
    ) -> None:
        self._mapping = mapping
        self._default = default
        self.calls: list[str] = []

    def evaluate(self, factor: FactorSpec, request: EvaluationRequest) -> EvaluationBundle:
        self.calls.append(factor.expression)
        ic, rank_ic, spread = self._mapping.get(
            factor.expression,
            self._default,
        )
        return EvaluationBundle(
            ic=ic,
            rank_ic=rank_ic,
            quantile_spread=spread,
        )


def _build_runner(
    evaluator,
    config: RefinementConfig | None = None,
    memory_registry: MemoryRegistry | None = None,
) -> tuple[RefinementRunner, ExperimentRegistry]:
    judge = PromotionJudge()
    service = AlphaHarnessService(
        compiler=FactorDslCompiler(),
        evaluator=evaluator,
        judge=judge,
    )
    experiments = ExperimentRegistry()
    orch = ResearchOrchestrator(
        service=service,
        experiment_registry=experiments,
        hypothesis_registry=HypothesisRegistry(),
        memory_registry=memory_registry,
    )
    runner = RefinementRunner(orch, config=config)
    return runner, experiments


# ── RefinementRunner — behaviour ────────────────────────────────────────────


def test_runner_no_refinement_if_root_not_refine():
    # PROMOTE_CANDIDATE verdict at root → no children.
    evaluator = _ScriptedEvaluator(
        {"rank(ts_mean(close, 20))": (0.10, 0.10, 0.02)},
    )
    runner, experiments = _build_runner(evaluator)
    root = Hypothesis(text="rank(ts_mean(close, 20))")
    result = runner.run(root, DEFAULT_EVAL_REQUEST)

    assert result.root.decision == ExperimentDecision.PROMOTE_CANDIDATE
    assert result.children == []
    # Only the root experiment was persisted.
    assert len(experiments.list_all()) == 1


def test_runner_expands_on_refine_verdict():
    # Borderline pass → REFINE. Any child expression uses the default (reject).
    borderline = (0.023, 0.035, 0.006)  # just above ic/rank_ic thresholds
    evaluator = _ScriptedEvaluator(
        {"rank(ts_mean(close, 20))": borderline},
        default=(0.0, 0.0, 0.0),
    )
    runner, experiments = _build_runner(evaluator)
    root = Hypothesis(text="rank(ts_mean(close, 20))")
    result = runner.run(root, DEFAULT_EVAL_REQUEST)

    assert result.root.decision == ExperimentDecision.REFINE
    assert len(result.children) >= 1
    # Bounded by default max_variants_per_step=3 at the root level, but the
    # top-level budget is max_total_children=6.
    assert len(result.children) <= 6
    # Every child has parent_id pointing at root.
    for child in result.children:
        assert child.hypothesis.parent_id == root.id
    # Experiment registry contains root + all children.
    assert len(experiments.list_all()) == 1 + len(result.children)


def test_runner_respects_max_variants_per_step():
    borderline = (0.023, 0.035, 0.006)
    evaluator = _ScriptedEvaluator(
        {"rank(ts_mean(close, 20))": borderline},
        default=(0.0, 0.0, 0.0),
    )
    cfg = RefinementConfig(
        max_depth=1,
        max_variants_per_step=2,
        max_total_children=10,
    )
    runner, _ = _build_runner(evaluator, config=cfg)
    result = runner.run(
        Hypothesis(text="rank(ts_mean(close, 20))"),
        DEFAULT_EVAL_REQUEST,
    )
    assert len(result.children) <= 2


def test_runner_respects_max_total_children():
    # Make every child REFINE too, so the runner wants to keep going.
    everyone_borderline = (0.023, 0.035, 0.006)
    evaluator = _ScriptedEvaluator(
        mapping={},
        default=everyone_borderline,
    )
    cfg = RefinementConfig(
        max_depth=5,
        max_variants_per_step=3,
        max_total_children=4,
    )
    runner, _ = _build_runner(evaluator, config=cfg)
    result = runner.run(
        Hypothesis(text="rank(ts_mean(close, 20))"),
        DEFAULT_EVAL_REQUEST,
    )
    assert len(result.children) <= 4


def test_runner_records_lineage_chain_through_depth():
    everyone_borderline = (0.023, 0.035, 0.006)
    evaluator = _ScriptedEvaluator(mapping={}, default=everyone_borderline)
    cfg = RefinementConfig(
        max_depth=2,
        max_variants_per_step=1,
        max_total_children=3,
    )
    runner, _ = _build_runner(evaluator, config=cfg)
    root = Hypothesis(text="rank(ts_mean(close, 20))")
    result = runner.run(root, DEFAULT_EVAL_REQUEST)

    # Walk the parent_id chain — every non-root hypothesis has a parent that
    # appears somewhere in {root, earlier children}.
    known_ids = {root.id} | {c.hypothesis.id for c in result.children}
    for child in result.children:
        assert child.hypothesis.parent_id in known_ids


def test_runner_terminates_when_mutations_have_no_novelty():
    # Force root to REFINE but make the compiler reject everything except the
    # root. Easier: use a 0-window mutation target where mutations collapse
    # to the same expression (e.g. plain `close` has no windows).
    borderline = (0.023, 0.035, 0.006)
    evaluator = _ScriptedEvaluator(
        {"rank(close)": borderline},
        default=(0.0, 0.0, 0.0),
    )
    runner, _ = _build_runner(evaluator)
    result = runner.run(Hypothesis(text="rank(close)"), DEFAULT_EVAL_REQUEST)
    # At least the root ran. Children depend on available mutations; the test
    # just verifies we don't crash and all children have the right parent.
    for child in result.children:
        assert child.hypothesis.parent_id == result.root.hypothesis.id


def test_end_to_end_refine_flow_with_lineage_memory():
    """Drive a REFINE parent through refinement with memory enabled."""
    borderline = (0.023, 0.035, 0.006)
    evaluator = _ScriptedEvaluator(
        {"rank(ts_mean(close, 20))": borderline},
        default=(0.0, 0.0, 0.0),
    )
    memory = MemoryRegistry()
    cfg = RefinementConfig(max_depth=1, max_variants_per_step=3)
    runner, _experiments = _build_runner(
        evaluator,
        config=cfg,
        memory_registry=memory,
    )
    root = Hypothesis(text="rank(ts_mean(close, 20))")
    result = runner.run(root, DEFAULT_EVAL_REQUEST)

    # Root was REFINE and produced children.
    assert result.root.decision == ExperimentDecision.REFINE
    assert len(result.children) >= 1

    # Every run_cycle call wrote a lineage entry, so we have root + children.
    entries = memory.list_by_category(MemoryCategory.EXPERIMENT_LINEAGE)
    assert len(entries) == 1 + len(result.children)

    # Root entry has "root" tag; children have "child".
    root_entries = memory.list_by_experiment(result.root.id)
    assert len(root_entries) == 1
    assert "root" in root_entries[0].tags

    for child in result.children:
        child_entries = memory.list_by_experiment(child.id)
        assert len(child_entries) == 1
        assert "child" in child_entries[0].tags
        assert f"parent={root.id}" in child_entries[0].content


def test_runner_uses_injected_novelty_evaluator():
    """Children whose canonical form matches a globally-known factor are dropped."""
    from alpha_harness.evaluators.novelty import NoveltyEvaluator

    borderline = (0.023, 0.035, 0.006)
    evaluator = _ScriptedEvaluator(
        {"rank(ts_mean(close, 20))": borderline},
        default=borderline,
    )
    # Pre-existing factor corpus that collides with the most obvious window
    # mutations, so the injected novelty evaluator prunes them.
    novelty = NoveltyEvaluator(
        existing_expressions=[
            ("existing_10", "rank(ts_mean(close, 10))"),
            ("existing_40", "rank(ts_mean(close, 40))"),
        ],
        similarity_threshold=0.85,
    )

    judge = PromotionJudge()
    service = AlphaHarnessService(
        compiler=FactorDslCompiler(),
        evaluator=evaluator,
        judge=judge,
    )
    experiments = ExperimentRegistry()
    orch = ResearchOrchestrator(
        service=service,
        experiment_registry=experiments,
        hypothesis_registry=HypothesisRegistry(),
    )
    runner = RefinementRunner(orch, novelty_evaluator=novelty)

    root = Hypothesis(text="rank(ts_mean(close, 20))")
    result = runner.run(root, DEFAULT_EVAL_REQUEST)

    # At least one mutation should have been rejected by the injected novelty
    # evaluator with a duplicate-style reason.
    assert any(
        "duplicate" in reason or "Duplicate" in reason for _expr, reason in result.skipped
    ), f"Expected some skip with duplicate reason; got {result.skipped!r}"


def test_refine_tag_preserves_parent_tag_order():
    """Refine tag merge must be deterministic and preserve parent ordering."""
    from alpha_harness.orchestrator.refinement import RefinementRunner

    borderline = (0.023, 0.035, 0.006)
    evaluator = _ScriptedEvaluator(
        {"rank(ts_mean(close, 20))": borderline},
        default=borderline,
    )
    cfg = RefinementConfig(max_depth=1, max_variants_per_step=1)
    runner, _ = _build_runner(evaluator, config=cfg)
    # Force a known parent tag order.
    root = Hypothesis(
        text="rank(ts_mean(close, 20))",
        tags=["alpha", "beta", "gamma"],
    )
    result = runner.run(root, DEFAULT_EVAL_REQUEST)
    assert len(result.children) >= 1
    for child in result.children:
        # Parent tags must appear in original order, followed by "refine".
        assert child.hypothesis.tags[:3] == ["alpha", "beta", "gamma"]
        assert child.hypothesis.tags[-1] == "refine"
        assert len(child.hypothesis.tags) == 4

    # Runner is used below; keep the lint happy.
    assert isinstance(runner, RefinementRunner)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
