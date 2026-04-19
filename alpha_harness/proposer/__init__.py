"""Hypothesis proposer — LLM-driven generation of DSL-valid research ideas.

The proposer is intentionally kept *outside* the deterministic service
core.  It produces candidate hypotheses; the rest of Alpha Harness
(compile → evaluate → judge) still decides truth.  Every candidate that
leaves this module has already been machine-validated against the factor
DSL, so free-form LLM text never reaches the research loop.
"""

from alpha_harness.proposer.hypothesis_proposer import HypothesisProposer
from alpha_harness.proposer.schemas import (
    DroppedProposal,
    ProposalCandidate,
    ProposalRequest,
    ProposalResult,
    RawProposal,
    RawProposalBatch,
)

__all__ = [
    "DroppedProposal",
    "HypothesisProposer",
    "ProposalCandidate",
    "ProposalRequest",
    "ProposalResult",
    "RawProposal",
    "RawProposalBatch",
]
