"""Typed request / response schemas for the hypothesis proposer.

Two layers of types:

    * ``RawProposal`` / ``RawProposalBatch`` — what the LLM is asked to
      produce.  These are *untrusted*: DSL validity has not been checked.
    * ``ProposalCandidate`` / ``ProposalResult`` — what the proposer
      returns to callers.  These have passed DSL compilation and are safe
      to hand to the research loop.

Keeping the two separate makes the trust boundary explicit.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from alpha_harness.combination import CombinationRecipe
from alpha_harness.retrieval import RelatedExperiment
from alpha_harness.schemas.hypothesis import AssetClass, Hypothesis

# ── Raw (pre-validation) schemas — what the LLM must return ─────────────────


class RawProposal(BaseModel):
    """A single proposal as emitted by the LLM, before DSL validation."""

    expression: str
    rationale: str = ""
    name: str | None = None
    tags: list[str] = Field(default_factory=list)
    base_recipe_id: str | None = None


class RawProposalBatch(BaseModel):
    """Envelope used with ``request_structured`` — the whole LLM reply."""

    proposals: list[RawProposal]


# ── Post-validation schemas — what callers receive ───────────────────────────


class ProposalCandidate(BaseModel):
    """A DSL-validated proposal, safe to convert into a :class:`Hypothesis`."""

    expression: str
    rationale: str = ""
    name: str = ""
    tags: list[str] = Field(default_factory=list)
    base_recipe_id: str | None = None


class CompositeAnchor(BaseModel):
    """Promoted basket available as a deterministic complement target."""

    factor_id: str
    recipe: CombinationRecipe
    ic: float | None = None
    rank_ic: float | None = None
    promoted_at: str = ""


class DroppedProposal(BaseModel):
    """A raw proposal that failed validation, together with the reason."""

    expression: str
    rationale: str = ""
    reason: str  # human-readable — typically the ``DslCompilationError`` message


class ProposalRequest(BaseModel):
    """Inputs to a single proposal call."""

    theme: str
    asset_class: AssetClass = AssetClass.US_EQUITY
    n_candidates: int = 5
    related: list[RelatedExperiment] = Field(default_factory=list)
    extra_guidance: str = ""  # optional operator-supplied hints

    # Compact, globally-scoped recency digest built by
    # :func:`alpha_harness.proposer.memory.build_memory_digest`.  Independent
    # of ``related`` (which is theme/AST-scored).  Empty string disables.
    prior_memory: str = ""

    # When non-empty, Round 10 complement mode is active: every proposal must
    # name one of these promoted recipes as its base.
    composite_anchors: list[CompositeAnchor] = Field(default_factory=list)

    model_config = {"arbitrary_types_allowed": True}


class ProposalResult(BaseModel):
    """The bounded, machine-checked output of one proposal round."""

    candidates: list[ProposalCandidate]
    dropped: list[DroppedProposal] = Field(default_factory=list)
    attempts: int = 1  # number of LLM rounds actually executed

    def to_hypotheses(
        self,
        *,
        asset_class: AssetClass = AssetClass.US_EQUITY,
        source: str = "llm_proposer",
        extra_tags: tuple[str, ...] = (),
    ) -> list[Hypothesis]:
        """Convert validated candidates into :class:`Hypothesis` objects.

        This is the single bridge from proposer output into the research
        loop — keeping it on the result type means callers cannot skip the
        DSL-validation step.
        """
        extras = tuple(extra_tags)
        return [
            Hypothesis(
                text=candidate.expression,
                rationale=candidate.rationale,
                source=source,
                asset_class=asset_class,
                tags=list({*candidate.tags, *extras}),
            )
            for candidate in self.candidates
        ]
