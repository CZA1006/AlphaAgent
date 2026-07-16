"""Experiment record schema — the central artifact of a research cycle."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from alpha_harness.schemas.evaluation import EvaluationBundle, EvaluationRequest
from alpha_harness.schemas.factor import FactorSpec
from alpha_harness.schemas.hypothesis import Hypothesis


class ExperimentDecision(StrEnum):
    REJECT = "reject"
    REFINE = "refine"
    ARCHIVE_ONLY = "archive_only"
    PROMOTE_CANDIDATE = "promote_candidate"


# ── Failure taxonomy ─────────────────────────────────────────────────────────


class FailureCategory(StrEnum):
    """Typed failure categories per AGENTS.md rule #6.

    Every rejected experiment must classify its failure so that patterns
    can be analyzed across runs (data issues vs. factor invalidity vs. ...).
    """

    WEAK_SIGNAL = "weak_signal"  # IC/RankIC below threshold
    NO_MONOTONICITY = "no_monotonicity"  # quantile spread not monotonic
    HIGH_TURNOVER = "high_turnover"  # impractical rebalance cost
    DATA_INSUFFICIENT = "data_insufficient"  # not enough periods or assets
    COMPILATION_ERROR = "compilation_error"  # factor DSL failed to compile
    EVALUATION_ERROR = "evaluation_error"  # evaluator raised an exception
    DUPLICATE = "duplicate"  # too similar to existing factor
    OTHER = "other"


class FailureRecord(BaseModel):
    """Structured failure information for rejected experiments."""

    category: FailureCategory
    detail: str = ""  # free-form elaboration


class PromotionTrail(BaseModel):
    """Immutable snapshot of the evaluator + judge knobs that flowed into
    a ``PROMOTE_CANDIDATE`` decision.

    Each unique (evaluator config, judge config, label config) tuple
    collapses to the same ``trail_id`` so the on-disk factor zoo can
    answer "was this factor promoted under the same regime as that
    other one?" without comparing dicts field-by-field.

    Build via :meth:`PromotionTrail.from_inputs`; the constructor is
    public mainly for round-trip deserialisation.
    """

    trail_id: str
    # Evaluator-side
    neutralize: str = "none"
    beta_estimation_method: str = "rolling_ols_lagged_1"
    beta_lookback_bars: int = Field(default=60, ge=2)
    beta_min_periods: int = Field(default=20, ge=2)
    sector_map_hash: str = ""  # sha256 of sorted (symbol,sector) pairs
    cost_bps: float = 0.0
    extra_horizons: list[int] = Field(default_factory=list)
    forecast_horizon_bars: int = 5
    lag_bars: int = 1
    return_type: str = "simple"
    # Holdout (Round 4E)
    holdout_strategy: str = "none"
    holdout_fraction: float = 0.0
    # Walk-forward (Round 4B/4D) — optional; populated when wrapped
    walk_forward: dict[str, int | float | str] = Field(default_factory=dict)
    # Judge thresholds (Rounds 4A.3, 4B, 4C, 4E)
    refine_margin: float = 0.20
    min_fraction_positive_folds: float = 0.6
    max_tail_concentration: float = 0.5
    min_holdout_decay_ratio: float = 0.5
    n_proposals_in_session: int = Field(default=1, ge=1)
    multiple_testing_familywise_alpha: float = Field(default=0.05, gt=0.0, lt=0.5)
    ic_threshold_multiplier: float = Field(default=1.0, ge=1.0)
    # Optional upstream candidate-selection provenance. Empty for normal
    # single-factor promotion.
    selection: dict[str, str | int | float] = Field(default_factory=dict)

    @classmethod
    def from_inputs(
        cls,
        *,
        evaluation_request: Any,
        judge_thresholds: dict[str, float],
        walk_forward: dict[str, int | float | str] | None = None,
        selection: dict[str, str | int | float] | None = None,
    ) -> PromotionTrail:
        """Construct a trail and compute its ``trail_id`` hash.

        ``evaluation_request`` is duck-typed so tests can pass anything
        with the right attributes; production callers pass an
        :class:`EvaluationRequest`.
        """
        import hashlib
        import json as _json

        sector_map = getattr(evaluation_request, "sector_map", {}) or {}
        sector_pairs = sorted((str(k), str(v)) for k, v in sector_map.items())
        sector_hash = hashlib.sha256(
            _json.dumps(sector_pairs, sort_keys=True).encode("utf-8"),
        ).hexdigest()[:16]

        label = getattr(evaluation_request, "label", None)
        holdout = getattr(evaluation_request, "holdout", None)
        wf = dict(walk_forward or {})
        n_proposals = int(getattr(evaluation_request, "n_proposals_in_session", 1))
        multiple_testing_alpha = float(
            judge_thresholds.get("multiple_testing_familywise_alpha", 0.05),
        )
        from alpha_harness.multiple_testing import bonferroni_z_threshold_multiplier

        threshold_multiplier = bonferroni_z_threshold_multiplier(
            n_proposals,
            familywise_alpha=multiple_testing_alpha,
        )

        neutralize = str(getattr(evaluation_request, "neutralize", "none"))
        beta_enabled = neutralize in {"beta", "both"}
        body = {
            "neutralize": neutralize,
            "sector_map_hash": sector_hash,
            "cost_bps": float(getattr(evaluation_request, "cost_bps", 0.0)),
            "extra_horizons": (list(label.extra_horizons) if label is not None else []),
            "forecast_horizon_bars": (int(label.forecast_horizon_bars) if label is not None else 5),
            "lag_bars": int(label.lag_bars) if label is not None else 1,
            "return_type": (str(label.return_type) if label is not None else "simple"),
            "holdout_strategy": (str(holdout.strategy) if holdout is not None else "none"),
            "holdout_fraction": (float(holdout.holdout_fraction) if holdout is not None else 0.0),
            "walk_forward": wf,
            "refine_margin": float(judge_thresholds.get("refine_margin", 0.20)),
            "min_fraction_positive_folds": float(
                judge_thresholds.get("min_fraction_positive_folds", 0.6),
            ),
            "max_tail_concentration": float(
                judge_thresholds.get("max_tail_concentration", 0.5),
            ),
            "min_holdout_decay_ratio": float(
                judge_thresholds.get("min_holdout_decay_ratio", 0.5),
            ),
            "n_proposals_in_session": n_proposals,
            "multiple_testing_familywise_alpha": multiple_testing_alpha,
            "ic_threshold_multiplier": threshold_multiplier,
        }
        if beta_enabled:
            body.update(
                {
                    "beta_estimation_method": "rolling_ols_lagged_1",
                    "beta_lookback_bars": int(
                        getattr(evaluation_request, "beta_lookback_bars", 60)
                    ),
                    "beta_min_periods": int(getattr(evaluation_request, "beta_min_periods", 20)),
                }
            )
        if selection:
            body["selection"] = dict(selection)
        canonical = _json.dumps(body, sort_keys=True, default=str)
        trail_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        return cls.model_validate({"trail_id": trail_id, **body})

    def diff(self, other: PromotionTrail) -> dict[str, tuple[Any, Any]]:
        """Return ``{field: (self_value, other_value)}`` for fields that differ.

        ``trail_id`` is excluded — it's a function of the other fields,
        so listing it adds noise without information.  The result is
        symmetric in the sense that ``a.diff(b)`` is the
        tuple-swap of ``b.diff(a)``.
        """
        self_dump = self.model_dump()
        other_dump = other.model_dump()
        out: dict[str, tuple[Any, Any]] = {}
        for key in self_dump:
            if key == "trail_id":
                continue
            mine = self_dump[key]
            theirs = other_dump.get(key)
            if mine != theirs:
                out[key] = (mine, theirs)
        return out


class JudgmentDetail(BaseModel):
    """Rich output of one ``ExperimentJudge.judge()`` call.

    Replaces the previous ``PromotionJudge.last_detail`` side channel —
    the judge now returns this object directly so callers do not need
    to share an instance to recover failure context.

    ``promotion_trail`` is populated only when ``decision`` is
    ``PROMOTE_CANDIDATE``; it captures the evaluator + judge knobs that
    drove the decision so the on-disk factor zoo stays reproducible.
    """

    decision: ExperimentDecision
    failure: FailureRecord | None = None
    notes: str = ""
    promotion_trail: PromotionTrail | None = None


# ── Reproducibility snapshot ─────────────────────────────────────────────────


class ReproducibilityInfo(BaseModel):
    """Fields needed to reproduce or audit an experiment result."""

    code_version: str = ""  # git commit hash or tag
    config_snapshot: dict[str, str] = Field(default_factory=dict)
    dataset_snapshot_id: str = ""  # identifies data version used
    universe_snapshot_id: str = ""  # identifies universe membership used
    artifact_paths: list[str] = Field(default_factory=list)


# ── Experiment record ────────────────────────────────────────────────────────


class ExperimentRecord(BaseModel):
    """A complete record of one research cycle iteration."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    hypothesis: Hypothesis
    factor: FactorSpec
    evaluation: EvaluationBundle
    eval_request: EvaluationRequest | None = None
    decision: ExperimentDecision = ExperimentDecision.ARCHIVE_ONLY
    failure: FailureRecord | None = None  # populated when decision is REJECT
    notes: str = ""
    # Round 4F — frozen evaluator + judge config that produced a
    # PROMOTE_CANDIDATE decision.  ``None`` for non-promote outcomes.
    promotion_trail: PromotionTrail | None = None
    tags: list[str] = Field(default_factory=list)
    reproducibility: ReproducibilityInfo = Field(default_factory=ReproducibilityInfo)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
    )
