"""Skill schema — a reusable research pattern distilled from experiments."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, Field


class Skill(BaseModel):
    """A research skill extracted from successful experiment patterns.

    Skills represent reusable knowledge the harness can apply in future cycles.
    """

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str
    description: str
    source_experiment_ids: list[str] = Field(default_factory=list)
    code_ref: str = ""  # pointer to implementation if applicable
    tags: list[str] = Field(default_factory=list)
    promoted: bool = False
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
    )
