"""Regime state schema — market regime classification at a point in time."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, Field


class RegimeState(BaseModel):
    """A snapshot of market regime classification.

    Used to condition research strategies on current market conditions.
    """

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    label: str  # e.g. "risk_on", "risk_off", "high_vol", "low_vol"
    features: dict[str, float] = Field(default_factory=dict)
    asset_class: str = "all"
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
    )
