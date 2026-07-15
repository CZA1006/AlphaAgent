"""Research orchestrator — drives the compile / evaluate / judge / persist loop.

Wires AlphaHarnessService, PromotionJudge, and registries into a single
``run_cycle`` entry point. Contains no Hermes runtime logic.
"""

from __future__ import annotations

import logging

from alpha_harness.artifacts import PromotedArtifactWriter
from alpha_harness.memory.lineage import build_lineage_entry
from alpha_harness.registries.protocols import (
    ExperimentRegistryProtocol,
    HypothesisRegistryProtocol,
    MemoryRegistryProtocol,
)
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
        The service's judge now returns a :class:`JudgmentDetail` directly,
        so the orchestrator no longer needs a judge handle of its own.
    experiment_registry:
        Persists completed ExperimentRecords.
    hypothesis_registry:
        Persists hypothesis status updates.
    memory_registry:
        Optional. When supplied, every completed cycle writes a compact
        EXPERIMENT_LINEAGE entry so parent/child relationships and headline
        metrics can be reconstructed without re-reading full experiment
        records.  Entries are factual and deterministic — no LLM summaries.
    write_lineage:
        Guard flag (default ``True``) allowing callers to suppress lineage
        writes even when a memory registry is configured.
    """

    def __init__(
        self,
        service: AlphaHarnessService,
        experiment_registry: ExperimentRegistryProtocol,
        hypothesis_registry: HypothesisRegistryProtocol,
        memory_registry: MemoryRegistryProtocol | None = None,
        *,
        write_lineage: bool = True,
        artifact_writer: PromotedArtifactWriter | None = None,
    ) -> None:
        self._service = service
        self._experiments = experiment_registry
        self._hypotheses = hypothesis_registry
        self._memory = memory_registry
        self._write_lineage = write_lineage
        # When supplied, every PROMOTE_CANDIDATE record also lands on disk
        # as a diff-friendly JSON plus an append-only index entry.  The
        # registry remains the source of truth; the artifact is a mirror.
        self._artifact_writer = artifact_writer

    def run_cycle(
        self,
        hypothesis: Hypothesis,
        eval_request: EvaluationRequest,
        *,
        parent_factor_id: str | None = None,
        refinement_round: int = 0,
        precompiled_factor: FactorSpec | None = None,
    ) -> ExperimentRecord:
        """Execute one complete research cycle.

        Steps:
            1. Mark hypothesis as TESTING.
            2. Run compile → evaluate → judge via the service; the service
               already attaches failure/notes from the judge detail.
            3. Update hypothesis status based on the decision.
            4. Save hypothesis and experiment to registries.
            5. Return the completed ExperimentRecord.
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
            record = self._service.run_research_cycle(
                hypothesis,
                eval_request,
                precompiled_factor=precompiled_factor,
            )
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

        # ── 2b. Stamp refinement lineage onto the factor ───────────────
        # Done before persistence so every downstream reader (registry,
        # artifact writer, lineage memory) sees the same object.
        if parent_factor_id is not None or refinement_round > 0:
            record = record.model_copy(
                update={
                    "factor": record.factor.model_copy(
                        update={
                            "parent_factor_id": parent_factor_id,
                            "refinement_round": refinement_round,
                        },
                    ),
                },
            )

        # ── 3. Update hypothesis status ────────────────────────────────
        new_status = _decision_to_status(record.decision)
        hypothesis = hypothesis.model_copy(
            update={"status": new_status},
        )
        self._hypotheses.save(hypothesis)

        # ── 4. Persist experiment ──────────────────────────────────────
        self._experiments.save(record)

        # ── 4b. Optional promotion artifact ────────────────────────────
        if self._artifact_writer is not None:
            self._artifact_writer.maybe_write(record)

        # ── 5. Optional lineage memory write ───────────────────────────
        if self._memory is not None and self._write_lineage:
            try:
                self._memory.save(build_lineage_entry(record))
            except Exception as exc:  # pragma: no cover — best-effort
                logger.warning(
                    "Failed to write lineage memory for experiment %s: %s",
                    record.id,
                    exc,
                )

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
        session_request = eval_request.model_copy(
            update={
                "n_proposals_in_session": max(
                    eval_request.n_proposals_in_session,
                    len(hypotheses),
                ),
            },
        )
        results: list[ExperimentRecord] = []
        for i, hypothesis in enumerate(hypotheses):
            logger.info(
                "Batch progress: %d/%d — hypothesis %s",
                i + 1,
                len(hypotheses),
                hypothesis.id,
            )
            record = self.run_cycle(hypothesis, session_request)
            results.append(record)
        return results

    def summary(self) -> dict[str, int | dict[int, int]]:
        """Return a count of experiments by decision category.

        Also includes ``refinement_rounds_seen`` — a histogram
        ``{round: count}``.  Roots contribute to ``round=0`` so the
        histogram sum equals the total experiment count; downstream
        reports can answer "how much of this run was refinement churn
        vs. fresh ideas?" without re-querying the registry.
        """
        all_experiments = self._experiments.list_all()
        counts: dict[str, int | dict[int, int]] = {}
        rounds: dict[int, int] = {}
        for record in all_experiments:
            key = record.decision.value
            prev = counts.get(key, 0)
            assert isinstance(prev, int)
            counts[key] = prev + 1
            r = record.factor.refinement_round
            rounds[r] = rounds.get(r, 0) + 1
        counts["refinement_rounds_seen"] = dict(sorted(rounds.items()))
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
