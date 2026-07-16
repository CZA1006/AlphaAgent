"""Round 9 Phase B — composite refinement: mutator + runner branch."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

from alpha_harness.combination import CombinationMethod, CombinationRecipe
from alpha_harness.orchestrator.mutations import propose_composite_mutations
from alpha_harness.orchestrator.refinement import (
    RefinementConfig,
    RefinementResult,
    RefinementRunner,
)
from alpha_harness.schemas.evaluation import EvaluationBundle, EvaluationRequest
from alpha_harness.schemas.experiment import ExperimentDecision, ExperimentRecord
from alpha_harness.schemas.factor import FactorSpec
from alpha_harness.schemas.hypothesis import Hypothesis

# ── propose_composite_mutations ────────────────────────────────────────────


def test_composite_mutator_returns_candidates_with_distinct_recipe_ids() -> None:
    recipe = CombinationRecipe.build(
        method=CombinationMethod.EQUAL_WEIGHT,
        components=["rank(ts_mean(close, 20))", "rank(ts_std(volume, 10))"],
    )
    out = propose_composite_mutations(recipe)
    assert len(out) > 0
    # Every child must be a distinct recipe, no-ops filtered, parent
    # itself never present.
    parent_id = recipe.recipe_id
    seen: set[str] = set()
    for child, label in out:
        assert child.recipe_id != parent_id
        assert child.recipe_id not in seen
        seen.add(child.recipe_id)
        # Same method, same number of components, only one differs.
        assert child.method == recipe.method
        assert len(child.components) == len(recipe.components)
        n_diff = sum(1 for a, b in zip(recipe.components, child.components, strict=True) if a != b)
        assert n_diff == 1, f"label={label} changed {n_diff} components"
        assert label.startswith("component_")


def test_composite_mutator_empty_recipe_returns_empty_list() -> None:
    empty = CombinationRecipe(
        method=CombinationMethod.EQUAL_WEIGHT,
        components=[],
        recipe_id="deadbeef",
    )
    assert propose_composite_mutations(empty) == []


def test_composite_mutator_label_carries_component_index() -> None:
    recipe = CombinationRecipe.build(
        method=CombinationMethod.EQUAL_WEIGHT,
        components=["rank(ts_mean(close, 20))", "rank(ts_std(volume, 10))"],
    )
    out = propose_composite_mutations(recipe)
    # At least one child should target component 0 and one component 1.
    indices = {label.split(":")[0] for _, label in out}
    assert "component_0" in indices
    assert "component_1" in indices


# ── RefinementRunner composite branch ──────────────────────────────────────


def _composite_record(recipe: CombinationRecipe) -> ExperimentRecord:
    return ExperimentRecord(
        hypothesis=Hypothesis(text="seed"),
        factor=FactorSpec(
            name="parent",
            expression=f"combine.{recipe.method.value}([...])",
            composite_recipe=recipe,
        ),
        evaluation=EvaluationBundle(ic=0.01, rank_ic=0.015, n_periods=200, n_assets=50),
        decision=ExperimentDecision.REFINE,
    )


def _eval_request() -> EvaluationRequest:
    return EvaluationRequest(
        factor_id="t",
        universe_id="t",
        eval_start=date(2024, 1, 1),
        eval_end=date(2024, 6, 30),
    )


def test_runner_dispatches_composite_branch_when_recipe_present() -> None:
    """Composite parent ⇒ orchestrator.run_cycle receives precompiled_factor."""
    recipe = CombinationRecipe.build(
        method=CombinationMethod.EQUAL_WEIGHT,
        components=["rank(ts_mean(close, 20))", "rank(ts_std(volume, 10))"],
    )
    parent = _composite_record(recipe)

    # Mock orchestrator returns a REJECT for every child so recursion
    # halts after one level and we can count outer calls cleanly.
    orchestrator = MagicMock()
    orchestrator.run_cycle.return_value = ExperimentRecord(
        hypothesis=parent.hypothesis,
        factor=parent.factor,
        evaluation=EvaluationBundle(),
        decision=ExperimentDecision.REJECT,
    )
    runner = RefinementRunner(
        orchestrator=orchestrator,
        config=RefinementConfig(
            max_depth=2,
            max_variants_per_step=2,
            max_total_children=4,
        ),
    )

    result = RefinementResult(root=parent)
    runner._expand(
        parent_record=parent,
        parent_hypothesis=parent.hypothesis,
        root_expression=parent.factor.expression,
        depth=0,
        eval_request=_eval_request(),
        result=result,
    )

    # At least one child must have been attempted.
    assert orchestrator.run_cycle.called
    # Every call must have carried a precompiled_factor with a recipe.
    for call in orchestrator.run_cycle.call_args_list:
        kwargs = call.kwargs
        assert kwargs.get("precompiled_factor") is not None
        assert kwargs["precompiled_factor"].composite_recipe is not None
        assert kwargs["refinement_round"] == 1
    # Cap honoured.
    assert len(result.children) <= 2


def test_runner_scalar_path_unaffected_by_composite_branch() -> None:
    """Regression guard: scalar parent still uses the legacy mutator."""
    parent = ExperimentRecord(
        hypothesis=Hypothesis(text="rank(close)"),
        factor=FactorSpec(name="parent", expression="rank(close)"),
        evaluation=EvaluationBundle(ic=0.01, rank_ic=0.015, n_periods=200, n_assets=50),
        decision=ExperimentDecision.REFINE,
    )
    orchestrator = MagicMock()
    orchestrator.run_cycle.return_value = ExperimentRecord(
        hypothesis=parent.hypothesis,
        factor=parent.factor,
        evaluation=EvaluationBundle(),
        decision=ExperimentDecision.REJECT,
    )
    runner = RefinementRunner(
        orchestrator=orchestrator,
        config=RefinementConfig(max_depth=1, max_variants_per_step=2, max_total_children=3),
    )
    result = RefinementResult(root=parent)
    runner._expand(
        parent_record=parent,
        parent_hypothesis=parent.hypothesis,
        root_expression=parent.factor.expression,
        depth=0,
        eval_request=_eval_request(),
        result=result,
    )
    # Scalar calls don't pass precompiled_factor.
    for call in orchestrator.run_cycle.call_args_list:
        assert call.kwargs.get("precompiled_factor") is None
