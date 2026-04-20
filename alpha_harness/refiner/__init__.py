"""Refinement diagnostics — structured briefs that steer mutation choice.

The deterministic mutation engine in
:mod:`alpha_harness.orchestrator.mutations` is blind to *why* the judge
returned REFINE.  A :class:`RefinementBrief` summarises the specific
weakness (borderline IC, high turnover, sign-inconsistent horizons,
cost drag eating the spread) so mutations can be ordered to attack
that weakness first.  The brief is ephemeral — it never lands in the
registry, it just shapes the next cycle's candidate list.
"""

from alpha_harness.refiner.brief import (
    FailingGate,
    RefinementBrief,
    build_brief,
)

__all__ = [
    "FailingGate",
    "RefinementBrief",
    "build_brief",
]
