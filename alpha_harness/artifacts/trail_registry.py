"""First-class on-disk store for :class:`PromotionTrail` instances.

Trails (Round 4F) live inside per-factor JSON artifacts.  Reading them
from there means scanning every promoted file even to answer "which
trails have we ever used?".  This module provides a small append-only
registry — one full ``PromotionTrail`` JSON per unique ``trail_id``
plus an index row keyed on ``trail_id`` recording when the trail was
first seen and which factors have been promoted under it.

Mirrors the contract of
:class:`alpha_harness.artifacts.PromotedArtifactWriter`:

* atomic JSON write via ``os.replace`` on a sibling ``.tmp`` file
* idempotent: re-writing the same trail_id is a no-op
* best-effort: a disk failure logs a warning and is swallowed; the
  research loop's correctness sits on the registry, not on this mirror
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alpha_harness.artifacts.store import LocalArtifactStore
from alpha_harness.schemas.experiment import PromotionTrail

logger = logging.getLogger(__name__)

DEFAULT_TRAIL_DIR = Path("artifacts/trails")
TRAIL_INDEX_NAME = "_index.jsonl"


# ── Index helpers ───────────────────────────────────────────────────────────


def index_path(base_dir: Path | str = DEFAULT_TRAIL_DIR) -> Path:
    """Return the path to the append-only index inside ``base_dir``."""
    return Path(base_dir) / TRAIL_INDEX_NAME


def read_trails(base_dir: Path | str = DEFAULT_TRAIL_DIR) -> list[dict[str, Any]]:
    """Load every index row.  Returns ``[]`` when the file is absent."""
    return LocalArtifactStore.for_directory("trails", base_dir).list_index("trails")


def read_trail(
    trail_id: str,
    base_dir: Path | str = DEFAULT_TRAIL_DIR,
) -> PromotionTrail | None:
    """Load the full :class:`PromotionTrail` for ``trail_id``, or ``None``."""
    payload = LocalArtifactStore.for_directory("trails", base_dir).read("trails", trail_id)
    if not isinstance(payload, dict) or not payload.get("trail_id"):
        return None
    return PromotionTrail.model_validate(payload)


# ── Atomic write helpers (mirror the promoted-artifact pattern) ─────────────


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True, default=str)
            fh.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _iter_index_entries(path: Path) -> Iterator[dict[str, Any]]:
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                yield json.loads(stripped)
            except json.JSONDecodeError:
                continue


def _rewrite_index(path: Path, entries: list[dict[str, Any]]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, sort_keys=True, default=str))
            fh.write("\n")
    os.replace(tmp, path)


# ── Writer ──────────────────────────────────────────────────────────────────


class TrailRegistryWriter:
    """Append a row to the trail index whenever a new ``trail_id`` appears.

    A single instance is safe across a cycle; ``record(trail, factor_id)``
    is idempotent and best-effort.
    """

    def __init__(self, base_dir: Path | str = DEFAULT_TRAIL_DIR) -> None:
        self._base_dir = Path(base_dir)

    def record(
        self,
        trail: PromotionTrail | None,
        factor_id: str,
    ) -> bool:
        """Record a promotion under ``trail`` for ``factor_id``.

        Returns ``True`` when the registry was updated (either a new
        trail file was written or the index row's factor list grew),
        ``False`` when there was nothing to do (legacy promotion with
        no trail, or the factor_id was already linked to this trail).
        """
        if trail is None:
            return False
        try:
            return self._record(trail, factor_id)
        except OSError as exc:  # pragma: no cover — defensive
            logger.warning(
                "Failed to record trail %s: %s",
                trail.trail_id,
                exc,
            )
            return False

    # ── Internals ────────────────────────────────────────────────────────

    def _record(self, trail: PromotionTrail, factor_id: str) -> bool:
        trail_path = self._base_dir / f"{trail.trail_id}.json"
        wrote_full = False
        if not trail_path.is_file():
            payload = json.loads(trail.model_dump_json())
            LocalArtifactStore.for_directory("trails", self._base_dir).write(
                "trails", trail.trail_id, payload
            )
            wrote_full = True

        idx_path = index_path(self._base_dir)
        existing = list(_iter_index_entries(idx_path))
        now = datetime.now(UTC).isoformat()

        for row in existing:
            if row.get("trail_id") == trail.trail_id:
                factors = list(row.get("factor_ids") or [])
                if factor_id and factor_id not in factors:
                    factors.append(factor_id)
                    row["factor_ids"] = factors
                    row["last_seen_at"] = now
                    self._base_dir.mkdir(parents=True, exist_ok=True)
                    _rewrite_index(idx_path, existing)
                    return True
                return wrote_full

        new_row = {
            "trail_id": trail.trail_id,
            "first_seen_at": now,
            "last_seen_at": now,
            "factor_ids": [factor_id] if factor_id else [],
        }
        existing.append(new_row)
        self._base_dir.mkdir(parents=True, exist_ok=True)
        _rewrite_index(idx_path, existing)
        return True
