"""Alpha Harness service interface.

This module defines the typed domain interface that Alpha Harness exposes.
External callers (CLI scripts, Hermes runtime adapters, notebooks) drive
research through this interface. Alpha Harness never reaches outward to
call runtime primitives — the dependency arrow points inward.

Boundary rule (AGENTS.md #8):
    Hermes runtime adapts INTO these services.
    Alpha Harness does NOT call assemble_prompt / run_agent_step / invoke_tool.
"""

from __future__ import annotations

from typing import Protocol

from alpha_harness.schemas.evaluation import EvaluationBundle, EvaluationRequest
from alpha_harness.schemas.experiment import (
    ExperimentRecord,
    JudgmentDetail,
)
from alpha_harness.schemas.factor import FactorSpec
from alpha_harness.schemas.hypothesis import Hypothesis


class FactorCompiler(Protocol):
    """Compile a hypothesis into a safe, executable factor specification."""

    def compile(self, hypothesis: Hypothesis) -> FactorSpec: ...


class FactorEvaluator(Protocol):
    """Run deterministic evaluation on a compiled factor.

    The EvaluationRequest carries all inputs (universe, time window, label
    definition, dataset snapshot) so the evaluator never reaches into
    ambient state. This prevents lookahead leakage by construction.
    """

    def evaluate(self, factor: FactorSpec, request: EvaluationRequest) -> EvaluationBundle: ...


class ExperimentJudge(Protocol):
    """Decide whether an evaluated factor should be promoted, refined, or rejected.

    The verdict is returned as a :class:`JudgmentDetail` — the decision
    together with any structured failure and human-readable notes.
    Returning a self-contained value (instead of the previous mutable
    ``last_detail`` side channel) keeps the judge stateless and makes
    cycle results safe under concurrency and retries.
    """

    def judge(
        self,
        hypothesis: Hypothesis,
        factor: FactorSpec,
        evaluation: EvaluationBundle,
        request: EvaluationRequest,
    ) -> JudgmentDetail: ...


class AlphaHarnessService:
    """Top-level domain service for the Alpha Harness research loop.

    This is the entry point that external callers use. It composes the
    compiler, evaluator, and judge into a single research cycle.

    For Milestone 1 this is a synchronous, single-threaded service.
    Each dependency is injected so the service remains testable without
    any infrastructure.
    """

    def __init__(
        self,
        compiler: FactorCompiler,
        evaluator: FactorEvaluator,
        judge: ExperimentJudge,
    ) -> None:
        self._compiler = compiler
        self._evaluator = evaluator
        self._judge = judge

    def compile_factor(self, hypothesis: Hypothesis) -> FactorSpec:
        """Compile a hypothesis into a factor spec via the factor DSL."""
        return self._compiler.compile(hypothesis)

    def evaluate_factor(self, factor: FactorSpec, request: EvaluationRequest) -> EvaluationBundle:
        """Run deterministic evaluation on a compiled factor."""
        return self._evaluator.evaluate(factor, request)

    def run_research_cycle(
        self,
        hypothesis: Hypothesis,
        eval_request: EvaluationRequest,
        *,
        precompiled_factor: FactorSpec | None = None,
    ) -> ExperimentRecord:
        """Execute one full research cycle: compile → evaluate → judge → record.

        ``precompiled_factor`` (Round 9 Phase B) lets the caller supply a
        FactorSpec directly, bypassing the DSL compile step.  This is the
        path composite factors take — they aren't DSL strings, so there's
        nothing for the compiler to parse.  Scalar callers leave the
        kwarg unset and behave exactly as before.
        """
        if precompiled_factor is not None:
            factor = precompiled_factor
        else:
            factor = self.compile_factor(hypothesis)
        evaluation = self.evaluate_factor(factor, eval_request)
        detail = self._judge.judge(hypothesis, factor, evaluation, eval_request)

        return ExperimentRecord(
            hypothesis=hypothesis,
            factor=factor,
            evaluation=evaluation,
            eval_request=eval_request,
            decision=detail.decision,
            failure=detail.failure,
            notes=detail.notes,
            promotion_trail=detail.promotion_trail,
        )
