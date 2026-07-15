"""Hypothesis proposer — theme → DSL-validated candidate expressions.

The proposer runs at most two LLM rounds:

    Round 1 — initial proposal generation (always)
    Round 2 — repair round (only if fewer than the requested number of
              candidates survived DSL validation, and ``max_rounds >= 2``)

Both rounds use :func:`alpha_harness.llm.request_structured`, so every LLM
reply is Pydantic-validated before we touch it, and every *expression*
inside that reply is run through :class:`FactorDslCompiler` before it can
reach the research loop.  Invalid expressions are dropped with the exact
compilation error stored on the result object.
"""

from __future__ import annotations

from dataclasses import dataclass

from alpha_harness.evaluators.novelty import NoveltyEvaluator
from alpha_harness.factors.compiler import DslCompilationError, FactorDslCompiler
from alpha_harness.llm import LLMClient, LLMMessage, StructuredLLMError, request_structured
from alpha_harness.proposer.prompts import (
    build_repair_prompt,
    build_system_prompt,
    build_user_prompt,
)
from alpha_harness.proposer.schemas import (
    CompositeAnchor,
    DroppedProposal,
    ProposalCandidate,
    ProposalRequest,
    ProposalResult,
    RawProposal,
    RawProposalBatch,
)
from alpha_harness.schemas.hypothesis import Hypothesis


@dataclass
class _ValidationOutcome:
    candidates: list[ProposalCandidate]
    dropped: list[DroppedProposal]


class HypothesisProposer:
    """Turn a research theme into a bounded list of validated candidates.

    Parameters
    ----------
    llm_client:
        Any :class:`~alpha_harness.llm.LLMClient` — mock in tests, OpenRouter
        in live runs.
    compiler:
        DSL compiler used to validate candidate expressions.  Injected so
        tests can swap it if needed; defaults to the standard compiler.
    max_rounds:
        Upper bound on LLM calls per ``propose()`` invocation.  ``1`` means
        no repair round; ``2`` (the default) allows one repair.  Values
        above ``2`` are not recommended — if the model can't produce valid
        DSL in two tries the theme is likely too vague.
    max_schema_attempts:
        Forwarded to :func:`request_structured` — how many times we retry
        on malformed JSON / schema-invalid LLM replies within a single
        proposal round.
    temperature:
        Optional temperature override applied to every LLM call.
    """

    def __init__(
        self,
        llm_client: LLMClient,
        compiler: FactorDslCompiler | None = None,
        *,
        max_rounds: int = 2,
        max_schema_attempts: int = 3,
        temperature: float | None = None,
    ) -> None:
        if max_rounds < 1:
            raise ValueError(f"max_rounds must be >= 1, got {max_rounds}")
        self._llm = llm_client
        self._compiler = compiler or FactorDslCompiler()
        self._max_rounds = max_rounds
        self._max_schema_attempts = max_schema_attempts
        self._temperature = temperature

    # ── Public API ───────────────────────────────────────────────────────

    def propose(self, request: ProposalRequest) -> ProposalResult:
        """Return a bounded, DSL-validated set of candidate hypotheses."""
        if request.n_candidates < 1:
            raise ValueError(
                f"n_candidates must be >= 1, got {request.n_candidates}"
            )

        # Round 1 — initial generation.
        system = LLMMessage(role="system", content=build_system_prompt())
        user = LLMMessage(role="user", content=build_user_prompt(request))
        messages: list[LLMMessage] = [system, user]

        try:
            batch = self._call_llm(messages)
        except StructuredLLMError as exc:
            # The LLM couldn't even emit schema-valid JSON — return an empty
            # result and surface the reason so callers/logs can see it.
            return ProposalResult(
                candidates=[],
                dropped=[
                    DroppedProposal(
                        expression="",
                        reason=f"LLM schema failure: {exc}",
                    )
                ],
                attempts=1,
            )

        anchors = {anchor.recipe.recipe_id: anchor for anchor in request.composite_anchors}
        outcome = self._validate(batch.proposals, anchors=anchors)
        attempts = 1

        # Round 2 — bounded repair pass, only if we came up short.
        if (
            len(outcome.candidates) < request.n_candidates
            and self._max_rounds >= 2
            and outcome.dropped
        ):
            repair_messages: list[LLMMessage] = [
                *messages,
                LLMMessage(
                    role="assistant",
                    content=batch.model_dump_json(),
                ),
                LLMMessage(
                    role="user",
                    content=build_repair_prompt(
                        [(d.expression, d.reason) for d in outcome.dropped],
                        n_needed=request.n_candidates - len(outcome.candidates),
                    ),
                ),
            ]
            try:
                repair_batch = self._call_llm(repair_messages)
            except StructuredLLMError:
                # Repair LLM call failed — keep round-1 results as-is.
                pass
            else:
                attempts += 1
                repair_outcome = self._validate(
                    repair_batch.proposals,
                    already_seen={c.expression for c in outcome.candidates},
                    anchors=anchors,
                )
                outcome.candidates.extend(repair_outcome.candidates)
                outcome.dropped.extend(repair_outcome.dropped)

        # Cap to n_candidates so the caller's budget is respected.
        trimmed = outcome.candidates[: request.n_candidates]

        return ProposalResult(
            candidates=trimmed,
            dropped=outcome.dropped,
            attempts=attempts,
        )

    # ── Internals ────────────────────────────────────────────────────────

    def _call_llm(self, messages: list[LLMMessage]) -> RawProposalBatch:
        """Run a single schema-constrained completion round."""
        return request_structured(
            self._llm,
            messages,
            RawProposalBatch,
            max_attempts=self._max_schema_attempts,
            temperature=self._temperature,
        )

    def _validate(
        self,
        raw_proposals: list[RawProposal],
        *,
        already_seen: set[str] | None = None,
        anchors: dict[str, CompositeAnchor] | None = None,
    ) -> _ValidationOutcome:
        """Run every raw proposal through the DSL compiler.

        Drops:
            * expressions that fail to parse or compile
            * empty expressions
            * exact duplicates of an expression we already accepted

        The compiler is invoked via :class:`Hypothesis` so we share the
        exact validation path used by the research loop itself — there is
        no separate "proposer-only" validator that could drift.
        """
        seen: set[str] = set(already_seen or set())
        candidates: list[ProposalCandidate] = []
        dropped: list[DroppedProposal] = []
        anchor_map = anchors or {}

        for raw in raw_proposals:
            expression = (raw.expression or "").strip()
            if not expression:
                dropped.append(DroppedProposal(
                    expression="",
                    rationale=raw.rationale,
                    reason="Empty expression.",
                ))
                continue

            if expression in seen:
                dropped.append(DroppedProposal(
                    expression=expression,
                    rationale=raw.rationale,
                    reason="Duplicate of an earlier accepted candidate.",
                ))
                continue

            probe = Hypothesis(text=expression, rationale=raw.rationale)
            try:
                factor = self._compiler.compile(probe)
            except DslCompilationError as exc:
                dropped.append(DroppedProposal(
                    expression=expression,
                    rationale=raw.rationale,
                    reason=str(exc),
                ))
                continue

            base_recipe_id = (raw.base_recipe_id or "").strip() or None
            if anchor_map and base_recipe_id is None:
                dropped.append(DroppedProposal(
                    expression=expression,
                    rationale=raw.rationale,
                    reason="Complement mode requires a base_recipe_id.",
                ))
                continue
            if base_recipe_id is not None and base_recipe_id not in anchor_map:
                dropped.append(DroppedProposal(
                    expression=expression,
                    rationale=raw.rationale,
                    reason=f"Unknown base_recipe_id {base_recipe_id!r}.",
                ))
                continue
            if base_recipe_id is not None:
                anchor = anchor_map[base_recipe_id]
                recipe = anchor.recipe
                novelty = NoveltyEvaluator(
                    existing_expressions=[
                        (f"base_component_{i}", component)
                        for i, component in enumerate(recipe.components)
                    ],
                    similarity_threshold=0.85,
                ).check_novelty(factor)
                if not novelty.is_novel:
                    dropped.append(DroppedProposal(
                        expression=expression,
                        rationale=raw.rationale,
                        reason=(
                            "Candidate is not structurally novel versus its base "
                            f"basket: {novelty.detail}"
                        ),
                    ))
                    continue

            seen.add(expression)
            tags = list(raw.tags)
            if base_recipe_id is not None:
                tags.extend(["complement", f"base_recipe:{base_recipe_id}"])
            candidates.append(ProposalCandidate(
                expression=expression,
                rationale=raw.rationale,
                name=(raw.name or factor.name).strip() or factor.name,
                tags=list(dict.fromkeys(tags)),
                base_recipe_id=base_recipe_id,
            ))

        return _ValidationOutcome(candidates=candidates, dropped=dropped)
