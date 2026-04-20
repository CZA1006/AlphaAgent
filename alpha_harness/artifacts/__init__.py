"""Promotion artifact writer — durable, diff-friendly record of every promote."""

from alpha_harness.artifacts.promoted import (
    DEFAULT_PROMOTED_DIR,
    PROMOTED_INDEX_NAME,
    PromotedArtifactWriter,
    index_path,
    read_index,
)

__all__ = [
    "DEFAULT_PROMOTED_DIR",
    "PROMOTED_INDEX_NAME",
    "PromotedArtifactWriter",
    "index_path",
    "read_index",
]
