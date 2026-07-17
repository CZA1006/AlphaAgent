"""Read-only registry for versioned market-pack configuration files."""

from __future__ import annotations

import json
from pathlib import Path

from alpha_harness.markets.models import MarketPack

DEFAULT_MARKET_CONFIG_DIR = Path(__file__).resolve().parents[2] / "configs" / "markets"


class MarketPackNotFoundError(LookupError):
    """Raised when a requested market pack is not registered."""


def load_market_pack(
    market_id: str,
    *,
    config_dir: Path | str = DEFAULT_MARKET_CONFIG_DIR,
) -> MarketPack:
    """Load and validate one market pack without mutating global state."""
    path = Path(config_dir) / f"{market_id}.json"
    if not path.is_file():
        raise MarketPackNotFoundError(f"market pack not found: {market_id!r} ({path})")
    pack = MarketPack.model_validate_json(path.read_text(encoding="utf-8"))
    if pack.market_id != market_id:
        raise ValueError(
            f"market pack id mismatch: requested {market_id!r}, file declares {pack.market_id!r}"
        )
    return pack


def list_market_packs(*, config_dir: Path | str = DEFAULT_MARKET_CONFIG_DIR) -> tuple[str, ...]:
    """Return registered market ids in deterministic order."""
    root = Path(config_dir)
    if not root.is_dir():
        return ()
    market_ids: list[str] = []
    for path in sorted(root.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        market_id = payload.get("market_id")
        if isinstance(market_id, str):
            market_ids.append(market_id)
    return tuple(market_ids)
