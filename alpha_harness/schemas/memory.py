"""Memory entry schema — structured research memory for the harness.

Research memory is NOT chat history. It stores typed patterns:
success/failure observations, experiment lineage insights, and
meta-policy notes that the harness uses to improve over time.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class MemoryCategory(StrEnum):
    """Categories of research memory per ARCHITECTURE.md."""

    SUCCESS_PATTERN = "success_pattern"
    FAILURE_PATTERN = "failure_pattern"
    EXPERIMENT_LINEAGE = "experiment_lineage"
    PROMOTION_HISTORY = "promotion_history"
    META_POLICY = "meta_policy"


class MemoryEntry(BaseModel):
    """A single research memory entry.

    Each entry captures a typed observation from the research process
    that the harness can retrieve and use in future cycles.
    """

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    category: MemoryCategory
    content: str
    source_experiment_ids: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
    )
