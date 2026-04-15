"""Universe specification — typed definition of asset membership for evaluation."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class MembershipSource(StrEnum):
    """Where universe membership comes from."""

    STATIC_LIST = "static_list"          # fixed ticker list (toy / testing)
    INDEX_CONSTITUENT = "index_const"    # e.g. S&P 500 as-of date
    LIQUIDITY_FILTER = "liquidity"       # top-N by volume / market cap
    EXCHANGE_LISTED = "exchange_listed"  # all symbols on an exchange


class UniverseSpec(BaseModel):
    """Typed universe definition that prevents survivorship bias.

    Every evaluation must reference a universe spec so that asset membership
    is explicit, timestamped, and auditable.
    """

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str
    asset_class: str  # "us_equity", "crypto"
    membership_source: MembershipSource = MembershipSource.STATIC_LIST

    # For STATIC_LIST: explicit ticker list
    symbols: list[str] = Field(default_factory=list)

    # For INDEX_CONSTITUENT / LIQUIDITY_FILTER: as-of date for membership
    as_of_date: date | None = None

    # Exchange scope (critical for crypto — prevents cross-exchange mixing)
    exchange: str | None = None

    # Delisting / survivorship
    include_delisted: bool = False

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
    )
