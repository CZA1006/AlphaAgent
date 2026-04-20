"""Bounded refinement of REFINE-verdict experiments.

When the promotion judge returns :class:`ExperimentDecision.REFINE`, the
idea is promising but not yet good enough.  The :class:`RefinementRunner`
wraps an existing :class:`ResearchOrchestrator` and, for each REFINE
verdict, generates a bounded set of mutated child hypotheses, evaluates
each one through the same orchestrator (so normal persistence, judging,
and lineage writes all still happen), and recurses up to a small
configurable depth.

Design choices
--------------
* The runner **never rewrites** ``ResearchOrchestrator.run_cycle`` — it
  only calls it.  Single-cycle semantics are preserved exactly.
* Every child hypothesis carries ``parent_id`` pointing at its immediate
  parent, so the experiment graph can be reconstructed from the registry
  alone (no extra bookkeeping required).
* Mutations come from :mod:`alpha_harness.orchestrator.mutations` —
  purely deterministic syntactic edits.  A future extension could ask an
  LLM for suggestions, but the output must still pass the DSL compiler
  and sibling-novelty checks.
* Novelty is enforced against the *root* expression of the refinement
  tree plus all siblings already accepted at the current level.  This
  prevents trivial duplicates and keeps the search diverse without
  needing a global registry scan.
* Three hard budgets cap the search:

      max_depth               depth of mutation (root = depth 0)
      max_variants_per_step   children generated per parent
      max_total_children      absolute cap across the whole tree

  When any cap trips, the runner returns cleanly — no partial state is
  left behind because every cycle was persisted atomically by the
  orchestrator itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from alpha_harness.evaluators.novelty import NoveltyEvaluator
from alpha_harness.factors.compiler import DslCompilationError, FactorDslCompiler
from alpha_harness.orchestrator.mutations import propose_mutations
from alpha_harness.orchestrator.research_loop import ResearchOrchestrator
from alpha_harness.refiner import build_brief
from alpha_harness.schemas.evaluation import EvaluationRequest
from alpha_harness.schemas.experiment import ExperimentDecision, ExperimentRecord
from alpha_harness.schemas.factor import FactorSpec
from alpha_harness.schemas.hypothesis import Hypothesis

logger = logging.getLogger(__name__)


# ── Configuration ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RefinementConfig:
    """Hard budgets for one :meth:`RefinementRunner.run` invocation."""

    max_depth: int = 2
    max_variants_per_step: int = 3
    max_total_children: int = 6
    novelty_threshold: float = 0.95  # strict — only block near-exact dupes
    refine_tag: str = "refine"

    def __post_init__(self) -> None:
        if self.max_depth < 0:
            raise ValueError("max_depth must be >= 0")
        if self.max_variants_per_step < 1:
            raise ValueError("max_variants_per_step must be >= 1")
        if self.max_total_children < 0:
            raise ValueError("max_total_children must be >= 0")


# ── Result type ──────────────────────────────────────────────────────────────


@dataclass
class RefinementResult:
    """Flat record of what happened during one refinement run."""

    root: ExperimentRecord
    children: list[ExperimentRecord] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    # ``skipped`` is ``(expression, reason)`` for candidates that were not
    # submitted to the orchestrator (compile error, duplicate, or cap hit).

    @property
    def all_records(self) -> list[ExperimentRecord]:
        """Root followed by every accepted child, in the order they ran."""
        return [self.root, *self.children]


# ── Runner ───────────────────────────────────────────────────────────────────


class RefinementRunner:
    """Drive bounded refinement of REFINE-verdict experiments."""

    def __init__(
        self,
        orchestrator: ResearchOrchestrator,
        novelty_evaluator: NoveltyEvaluator | None = None,
        *,
        compiler: FactorDslCompiler | None = None,
        config: RefinementConfig | None = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._novelty = novelty_evaluator
        self._compiler = compiler or FactorDslCompiler()
        self._config = config or RefinementConfig()

    # ── Public API ───────────────────────────────────────────────────────

    def run(
        self,
        root_hypothesis: Hypothesis,
        eval_request: EvaluationRequest,
    ) -> RefinementResult:
        """Evaluate ``root_hypothesis`` and refine it if the judge says so.

        Always runs the root cycle first so the caller gets at least one
        record back, even when no refinement is warranted.
        """
        root_record = self._orchestrator.run_cycle(root_hypothesis, eval_request)
        result = RefinementResult(root=root_record)

        if root_record.decision != ExperimentDecision.REFINE:
            return result

        self._expand(
            parent_record=root_record,
            parent_hypothesis=root_hypothesis,
            root_expression=root_hypothesis.text,
            depth=0,
            eval_request=eval_request,
            result=result,
        )
        return result

    # ── Internals ────────────────────────────────────────────────────────

    def _expand(
        self,
        *,
        parent_record: ExperimentRecord,
        parent_hypothesis: Hypothesis,
        root_expression: str,
        depth: int,
        eval_request: EvaluationRequest,
        result: RefinementResult,
    ) -> None:
        cfg = self._config
        if depth >= cfg.max_depth:
            return
        if len(result.children) >= cfg.max_total_children:
            return

        # Build a diagnostic brief from the parent record so mutation
        # ordering targets the specific weakness that earned REFINE.  The
        # brief is advisory only — it reorders, never filters.
        brief = build_brief(parent_record, eval_request.profile)
        mutations = propose_mutations(
            parent_record.factor.expression,
            brief=brief,
        )
        if not mutations:
            return
        if not brief.is_empty:
            logger.info(
                "Refinement brief for %s: %s",
                parent_record.factor.id,
                brief.describe(),
            )

        sibling_expressions: list[str] = []
        children_this_level = 0

        for expression, label in mutations:
            if children_this_level >= cfg.max_variants_per_step:
                break
            if len(result.children) >= cfg.max_total_children:
                break

            accepted_record = self._try_cycle(
                expression=expression,
                label=label,
                parent_hypothesis=parent_hypothesis,
                parent_factor_id=parent_record.factor.id,
                refinement_round=depth + 1,
                root_expression=root_expression,
                sibling_expressions=sibling_expressions,
                eval_request=eval_request,
                result=result,
            )
            if accepted_record is None:
                continue

            sibling_expressions.append(expression)
            children_this_level += 1
            result.children.append(accepted_record)

            # Recurse only when the child itself earns a REFINE verdict.
            if accepted_record.decision == ExperimentDecision.REFINE:
                self._expand(
                    parent_record=accepted_record,
                    parent_hypothesis=accepted_record.hypothesis,
                    root_expression=root_expression,
                    depth=depth + 1,
                    eval_request=eval_request,
                    result=result,
                )

    def _try_cycle(
        self,
        *,
        expression: str,
        label: str,
        parent_hypothesis: Hypothesis,
        parent_factor_id: str,
        refinement_round: int,
        root_expression: str,
        sibling_expressions: list[str],
        eval_request: EvaluationRequest,
        result: RefinementResult,
    ) -> ExperimentRecord | None:
        """Validate + novelty-check + run one mutation; return its record or ``None``."""
        # 1. DSL compilation — the authoritative validator.
        probe = Hypothesis(text=expression, rationale=f"mutation:{label}")
        try:
            factor = self._compiler.compile(probe)
        except DslCompilationError as exc:
            result.skipped.append((expression, f"compile_error:{exc}"))
            return None

        # 2. Structural novelty vs. the root + earlier siblings.
        if not self._is_novel(factor, root_expression, sibling_expressions):
            result.skipped.append((expression, "duplicate_of_root_or_sibling"))
            return None

        # 3. Build the child hypothesis with proper lineage + tags.
        child = Hypothesis(
            text=expression,
            rationale=(f"Refined from {parent_hypothesis.id} via {label}"),
            source=parent_hypothesis.source or "refinement",
            asset_class=parent_hypothesis.asset_class,
            tags=list(dict.fromkeys([*parent_hypothesis.tags, self._config.refine_tag])),
            parent_id=parent_hypothesis.id,
        )

        logger.info(
            "Refinement: running child %s (mutation=%s) of parent %s",
            child.id,
            label,
            parent_hypothesis.id,
        )
        return self._orchestrator.run_cycle(
            child,
            eval_request,
            parent_factor_id=parent_factor_id,
            refinement_round=refinement_round,
        )

    def _is_novel(
        self,
        factor: FactorSpec,
        root_expression: str,
        sibling_expressions: list[str],
    ) -> bool:
        """Compare the mutation against the root and its siblings.

        When an external novelty evaluator is injected at construction time,
        it is consulted *first* so the runner respects whatever global
        corpus and threshold the caller configured (e.g. the same evaluator
        the orchestrator judge uses).  The strict intra-refinement
        comparison against the root + already-accepted siblings is then
        applied on top using the configured ``novelty_threshold``.
        """
        # 1. Global / injected novelty (optional).
        if self._novelty is not None:
            verdict = self._novelty.check_novelty(factor)
            if not verdict.is_novel:
                logger.debug(
                    "Refinement: dropping %s — %s",
                    factor.expression,
                    verdict.detail,
                )
                return False

        # 2. Strict refinement-scoped check vs. root + siblings.
        comparisons: list[tuple[str, str]] = [("__root__", root_expression)]
        comparisons.extend((f"__sibling_{i}__", expr) for i, expr in enumerate(sibling_expressions))
        evaluator = NoveltyEvaluator(
            existing_expressions=comparisons,
            similarity_threshold=self._config.novelty_threshold,
        )
        verdict = evaluator.check_novelty(factor)
        if not verdict.is_novel:
            logger.debug(
                "Refinement: dropping %s — %s",
                factor.expression,
                verdict.detail,
            )
        return verdict.is_novel
