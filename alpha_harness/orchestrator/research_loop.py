"""Research orchestrator — drives the compile / evaluate / judge / persist loop.

Wires AlphaHarnessService, PromotionJudge, and registries into a single
``run_cycle`` entry point. Contains no Hermes runtime logic.
"""

from __future__ import annotations

import logging

from alpha_harness.evaluators.promotion_judge import PromotionJudge
from alpha_harness.registries.experiment import ExperimentRegistry
from alpha_harness.registries.hypothesis import HypothesisRegistry
from alpha_harness.schemas.evaluation import EvaluationBundle, EvaluationRequest
from alpha_harness.schemas.experiment import (
    ExperimentDecision,
    ExperimentRecord,
    FailureCategory,
    FailureRecord,
)
from alpha_harness.schemas.factor import FactorSpec
from alpha_harness.schemas.hypothesis import Hypothesis, HypothesisStatus
from alpha_harness.service import AlphaHarnessService

logger = logging.getLogger(__name__)


class ResearchOrchestrator:
    """Drives one or more research cycles and persists results.

    Parameters
    ----------
    service:
        The Alpha Harness domain service (compiler + evaluator + judge).
    judge:
        The promotion judge. Must be the same instance used inside ``service``
        so that ``last_detail`` is available after ``service.run_research_cycle``.
    experiment_registry:
        Persists completed ExperimentRecords.
    hypothesis_registry:
        Persists hypothesis status updates.
    """

    def __init__(
        self,
        service: AlphaHarnessService,
        judge: PromotionJudge,
        experiment_registry: ExperimentRegistry,
        hypothesis_registry: HypothesisRegistry,
    ) -> None:
        self._service = service
        self._judge = judge
        self._experiments = experiment_registry
        self._hypotheses = hypothesis_registry

    def run_cycle(
        self,
        hypothesis: Hypothesis,
        eval_request: EvaluationRequest,
    ) -> ExperimentRecord:
        """Execute one complete research cycle.

        Steps:
            1. Mark hypothesis as TESTING.
            2. Run compile → evaluate → judge via the service.
            3. Attach rich failure/notes from the judge detail.
            4. Update hypothesis status based on the decision.
            5. Save hypothesis and experiment to registries.
            6. Return the completed ExperimentRecord.
        """
        # ── 1. Mark hypothesis as testing ──────────────────────────────
        hypothesis = hypothesis.model_copy(
            update={"status": HypothesisStatus.TESTING},
        )
        self._hypotheses.save(hypothesis)

        # ── 2. Run the research cycle ──────────────────────────────────
        logger.info(
            "Running research cycle for hypothesis %s: %s",
            hypothesis.id,
            hypothesis.text[:80],
        )
        try:
            record = self._service.run_research_cycle(hypothesis, eval_request)
        except Exception as exc:
            # Compilation or evaluation failure → record as REJECT
            logger.warning(
                "Cycle failed for hypothesis %s: %s",
                hypothesis.id,
                exc,
            )
            record = ExperimentRecord(
                hypothesis=hypothesis,
                factor=FactorSpec(name="failed", expression=""),
                evaluation=EvaluationBundle(),
                eval_request=eval_request,
                decision=ExperimentDecision.REJECT,
                failure=FailureRecord(
                    category=FailureCategory.COMPILATION_ERROR,
                    detail=str(exc),
                ),
                notes=f"Cycle failed: {exc}",
            )

        # ── 3. Attach rich detail from judge ───────────────────────────
        if record.failure is None:
            detail = self._judge.last_detail
            if detail is not None:
                record = record.model_copy(
                    update={
                        "failure": detail.failure,
                        "notes": detail.notes,
                    },
                )

        # ── 4. Update hypothesis status ────────────────────────────────
        new_status = _decision_to_status(record.decision)
        hypothesis = hypothesis.model_copy(
            update={"status": new_status},
        )
        self._hypotheses.save(hypothesis)

        # ── 5. Persist experiment ──────────────────────────────────────
        self._experiments.save(record)

        logger.info(
            "Cycle complete for hypothesis %s → decision=%s",
            hypothesis.id,
            record.decision.value,
        )
        return record

    def run_batch(
        self,
        hypotheses: list[Hypothesis],
        eval_request: EvaluationRequest,
    ) -> list[ExperimentRecord]:
        """Run research cycles for a batch of hypotheses.

        Processes sequentially — parallelism will be added in a later round
        once the factor DSL supports concurrent evaluation safely.

        Parameters
        ----------
        hypotheses:
            List of hypotheses to evaluate.
        eval_request:
            Shared evaluation context (universe, dates, profile).

        Returns
        -------
        List of ExperimentRecords, one per hypothesis.
        """
        results: list[ExperimentRecord] = []
        for i, hypothesis in enumerate(hypotheses):
            logger.info(
                "Batch progress: %d/%d — hypothesis %s",
                i + 1,
                len(hypotheses),
                hypothesis.id,
            )
            record = self.run_cycle(hypothesis, eval_request)
            results.append(record)
        return results

    def summary(self) -> dict[str, int]:
        """Return a count of experiments by decision category."""
        all_experiments = self._experiments.list_all()
        counts: dict[str, int] = {}
        for record in all_experiments:
            key = record.decision.value
            counts[key] = counts.get(key, 0) + 1
        return counts


def _decision_to_status(decision: ExperimentDecision) -> HypothesisStatus:
    """Map an experiment decision to the corresponding hypothesis status."""
    mapping: dict[ExperimentDecision, HypothesisStatus] = {
        ExperimentDecision.PROMOTE_CANDIDATE: HypothesisStatus.PROMISING,
        ExperimentDecision.REFINE: HypothesisStatus.TESTING,
        ExperimentDecision.REJECT: HypothesisStatus.REJECTED,
        ExperimentDecision.ARCHIVE_ONLY: HypothesisStatus.ARCHIVED,
    }
    return mapping.get(decision, HypothesisStatus.ARCHIVED)
