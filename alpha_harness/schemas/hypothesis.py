"""Hypothesis schema — a research idea to be tested."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class HypothesisStatus(StrEnum):
    DRAFT = "draft"
    TESTING = "testing"
    REJECTED = "rejected"
    PROMISING = "promising"
    ARCHIVED = "archived"


class AssetClass(StrEnum):
    US_EQUITY = "us_equity"
    CRYPTO = "crypto"


class Hypothesis(BaseModel):
    """A research hypothesis to be compiled into a factor and evaluated."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    text: str
    rationale: str = ""
    source: str = ""  # e.g. "llm_proposal", "manual", "mutation"
    asset_class: AssetClass = AssetClass.US_EQUITY
    status: HypothesisStatus = HypothesisStatus.DRAFT
    tags: list[str] = Field(default_factory=list)
    parent_id: str | None = None  # lineage: which hypothesis this was derived from
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
    )
