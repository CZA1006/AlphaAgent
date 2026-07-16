"""Promotion artifact writer — durable, diff-friendly record of every promote."""

from alpha_harness.artifacts.promoted import (
    DEFAULT_PROMOTED_DIR,
    PROMOTED_INDEX_NAME,
    PromotedArtifactWriter,
    index_path,
    read_artifact,
    read_index,
    record_from_payload,
)
from alpha_harness.artifacts.store import ArtifactKind, ArtifactStore, LocalArtifactStore
from alpha_harness.artifacts.trail_registry import (
    DEFAULT_TRAIL_DIR,
    TRAIL_INDEX_NAME,
    TrailRegistryWriter,
    read_trail,
    read_trails,
)

__all__ = [
    "DEFAULT_PROMOTED_DIR",
    "DEFAULT_TRAIL_DIR",
    "PROMOTED_INDEX_NAME",
    "TRAIL_INDEX_NAME",
    "ArtifactKind",
    "ArtifactStore",
    "LocalArtifactStore",
    "PromotedArtifactWriter",
    "TrailRegistryWriter",
    "index_path",
    "read_artifact",
    "read_index",
    "read_trail",
    "read_trails",
    "record_from_payload",
]
