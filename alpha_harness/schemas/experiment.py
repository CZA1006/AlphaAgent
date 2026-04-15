"""Experiment record schema — the central artifact of a research cycle."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

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

    WEAK_SIGNAL = "weak_signal"               # IC/RankIC below threshold
    NO_MONOTONICITY = "no_monotonicity"       # quantile spread not monotonic
    HIGH_TURNOVER = "high_turnover"           # impractical rebalance cost
    DATA_INSUFFICIENT = "data_insufficient"   # not enough periods or assets
    COMPILATION_ERROR = "compilation_error"   # factor DSL failed to compile
    EVALUATION_ERROR = "evaluation_error"     # evaluator raised an exception
    DUPLICATE = "duplicate"                   # too similar to existing factor
    OTHER = "other"


class FailureRecord(BaseModel):
    """Structured failure information for rejected experiments."""

    category: FailureCategory
    detail: str = ""  # free-form elaboration


# ── Reproducibility snapshot ─────────────────────────────────────────────────


class ReproducibilityInfo(BaseModel):
    """Fields needed to reproduce or audit an experiment result."""

    code_version: str = ""            # git commit hash or tag
    config_snapshot: dict[str, str] = Field(default_factory=dict)
    dataset_snapshot_id: str = ""     # identifies data version used
    universe_snapshot_id: str = ""    # identifies universe membership used
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
    tags: list[str] = Field(default_factory=list)
    reproducibility: ReproducibilityInfo = Field(default_factory=ReproducibilityInfo)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
    )
