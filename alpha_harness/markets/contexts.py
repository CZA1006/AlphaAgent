"""Compatibility context builders for market-specific callers."""

from __future__ import annotations

from pathlib import Path

from alpha_harness.director.research_director import (
    DEFAULT_VALIDATION_DIR,
    ResearchDirectorContext,
    build_market_context,
)
from alpha_harness.markets.registry import load_market_pack


def build_hk_ipo_context(
    *,
    validation_dir: Path | str = DEFAULT_VALIDATION_DIR,
) -> ResearchDirectorContext:
    """Build the legacy HK IPO context through its registered market pack."""
    return build_market_context(
        load_market_pack("hk_ipo"),
        validation_dir=validation_dir,
    )
