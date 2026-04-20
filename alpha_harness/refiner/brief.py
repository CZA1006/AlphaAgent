"""Construct a :class:`RefinementBrief` from an experiment record.

A brief is a small, opinionated summary of *which* diagnostics pushed a
factor toward REFINE.  It inspects the evaluation bundle, the failure
record, and the multi-horizon metadata, then exposes a handful of
boolean flags the mutation prioritiser can act on.

The classifier intentionally errs on the side of flagging *something* —
an empty brief means the mutation engine has no hint, so it falls back
to its default ordering.  When in doubt, flag the most informative
weakness; downstream ordering is stable, so false positives at most
shuffle mutation priority, never skip candidates.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from alpha_harness.schemas.evaluation import EvaluationBundle, EvaluationProfile
from alpha_harness.schemas.experiment import ExperimentRecord

# ── Small value types ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class FailingGate:
    """One metric that failed or is borderline against its profile threshold.

    ``margin_pct`` is ``(value - threshold) / threshold`` when both sides
    are available; negative means below threshold, small-positive means
    borderline.  ``None`` when the metric is missing or the threshold is
    zero/absent.
    """

    name: str
    value: float | None
    threshold: float | None
    margin_pct: float | None


@dataclass(frozen=True)
class RefinementBrief:
    """Structured diagnostic of a REFINE-verdict experiment."""

    failing_gates: tuple[FailingGate, ...] = field(default_factory=tuple)
    borderline_gates: tuple[FailingGate, ...] = field(default_factory=tuple)
    sign_inconsistent: bool = False
    turnover_high: bool = False
    cost_drag_large: bool = False
    weak_cross_sectional: bool = False

    @property
    def is_empty(self) -> bool:
        """True when nothing meaningful can be said about the weakness."""
        return (
            not self.failing_gates
            and not self.borderline_gates
            and not self.sign_inconsistent
            and not self.turnover_high
            and not self.cost_drag_large
            and not self.weak_cross_sectional
        )

    def describe(self) -> str:
        """One-line human-readable summary (for logs / debug dumps)."""
        parts: list[str] = []
        if self.failing_gates:
            parts.append("failing=" + ",".join(g.name for g in self.failing_gates))
        if self.borderline_gates:
            parts.append("borderline=" + ",".join(g.name for g in self.borderline_gates))
        for label, flag in (
            ("sign_inconsistent", self.sign_inconsistent),
            ("turnover_high", self.turnover_high),
            ("cost_drag_large", self.cost_drag_large),
            ("weak_cross_sectional", self.weak_cross_sectional),
        ):
            if flag:
                parts.append(label)
        return "; ".join(parts) or "(no diagnostic)"


# ── Builder ─────────────────────────────────────────────────────────────────


# Turnover above this is treated as "high churn"; the mutation prioritiser
# will prefer window-doubling edits.  Deliberately loose — factors flagged
# here are still passing the judge's hard turnover threshold; we're just
# tilting ordering, not gating anything.
_TURNOVER_HIGH = 1.0

# Cost drag is considered "large" when the cost adjustment eats at least
# half of the gross quantile spread.
_COST_DRAG_FRACTION = 0.5


def build_brief(
    record: ExperimentRecord,
    profile: EvaluationProfile,
    *,
    refine_margin: float = 0.20,
) -> RefinementBrief:
    """Distil ``record`` into a :class:`RefinementBrief`.

    ``refine_margin`` mirrors the value used by
    :class:`alpha_harness.evaluators.promotion_judge.PromotionJudge`;
    metrics whose relative margin over their threshold is below this
    fraction are classed as *borderline*.
    """
    ev = record.evaluation
    failing: list[FailingGate] = []
    borderline: list[FailingGate] = []
    weak_cross_sectional = False

    for metric in profile.required_metrics:
        name = metric.value
        value = getattr(ev, name, None)
        threshold = profile.thresholds.get(name)
        margin = _margin(value, threshold)
        gate = FailingGate(name=name, value=value, threshold=threshold, margin_pct=margin)
        if value is None:
            failing.append(gate)
            continue
        if threshold is not None and value < threshold:
            failing.append(gate)
            if name in ("ic", "rank_ic"):
                weak_cross_sectional = True
            continue
        if margin is not None and margin < refine_margin:
            borderline.append(gate)
            if name in ("ic", "rank_ic"):
                weak_cross_sectional = True

    return RefinementBrief(
        failing_gates=tuple(failing),
        borderline_gates=tuple(borderline),
        sign_inconsistent=_is_sign_inconsistent(ev),
        turnover_high=_is_turnover_high(ev),
        cost_drag_large=_is_cost_drag_large(ev),
        weak_cross_sectional=weak_cross_sectional,
    )


# ── Internal helpers ────────────────────────────────────────────────────────


def _margin(value: float | None, threshold: float | None) -> float | None:
    if value is None or threshold is None or threshold == 0:
        return None
    return (value - threshold) / threshold


def _is_sign_inconsistent(ev: EvaluationBundle) -> bool:
    horizons = ev.metadata.get("ic_by_horizon")
    if not isinstance(horizons, dict) or len(horizons) < 2:
        return False
    same_sign = ev.metadata.get("ic_sign_consistent_horizons")
    if not isinstance(same_sign, int):
        return False
    # Judge rejects outright at < 2; "barely consistent" (==2 across many
    # horizons) is still a signal the refiner should try to stabilise.
    return same_sign <= max(2, len(horizons) // 2)


def _is_turnover_high(ev: EvaluationBundle) -> bool:
    return ev.turnover is not None and ev.turnover >= _TURNOVER_HIGH


def _is_cost_drag_large(ev: EvaluationBundle) -> bool:
    gross = ev.quantile_spread
    net = ev.net_quantile_spread
    if gross is None or net is None or gross <= 0:
        return False
    drag = gross - net
    return drag >= _COST_DRAG_FRACTION * gross
