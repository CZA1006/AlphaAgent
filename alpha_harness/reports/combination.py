"""Combination report (Round 8 Phase A).

When :mod:`scripts.combine_factors` builds a basket out of N component
expressions and scores it under a regime, this module persists a
self-contained audit record: the recipe, the regime trail hash, the
basket's headline metrics, every component's metrics, and whether the
basket cleared the regime's profile gates.

Same on-disk shape as :mod:`alpha_harness.reports.validation` —
``{cycle_id}.json`` plus an append-only ``_index.jsonl`` keyed on
``cycle_id``.  The report is the **canonical persisted form of a
basket**: Round 8 Phase B's promotion path writes a registry pointer
that references the recipe captured here.

The ``recipe_id`` is a SHA-256 over ``(method, sorted canonical-AST
hashes of every component)`` — collisions between two equivalent
recipes (``equal_weight(A, B, C)`` and ``equal_weight(C, B, A)``)
are intentional so the novelty check never re-promotes the same
basket twice.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from alpha_harness.combination import CombinationMethod
from alpha_harness.factors.canonical import canonicalize
from alpha_harness.factors.dsl_parser import DslParseError, parse_expression
from alpha_harness.reports.validation import FactorThumbnail

logger = logging.getLogger(__name__)

DEFAULT_COMBINATION_DIR = Path("artifacts/combinations")
COMBINATION_INDEX_NAME = "_index.jsonl"
SCHEMA_VERSION = 1


# ── Recipe id ───────────────────────────────────────────────────────────────


def _canonical_hash(expression: str) -> str:
    """Return a stable 16-hex-char digest for a single DSL expression.

    Uses the same canonicalizer the novelty check uses so two expressions
    that differ only in commutative-operand order collapse to the same
    component hash.  Raises ``ValueError`` if the expression won't parse —
    re-wrapping ``DslParseError`` keeps callers from having to depend on
    DSL internals.
    """
    try:
        ast = parse_expression(expression)
    except DslParseError as exc:
        raise ValueError(f"unparseable component expression {expression!r}: {exc}") from exc
    canon_repr = repr(canonicalize(ast))
    return hashlib.sha256(canon_repr.encode("utf-8")).hexdigest()[:16]


def recipe_id_for(method: CombinationMethod, components: list[str]) -> str:
    """SHA-256 over ``(method, sorted component hashes)``.

    Sorting is the load-bearing step: ``equal_weight(A, B, C)`` and
    ``equal_weight(C, B, A)`` describe the same basket and must hash
    identically, otherwise the novelty check would let the proposer
    re-promote the same recipe under a permuted order.
    """
    component_hashes = sorted(_canonical_hash(e) for e in components)
    payload = method.value + "|" + "|".join(component_hashes)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ── Schema ──────────────────────────────────────────────────────────────────


class CombinationRecipe(BaseModel):
    """Hashable description of one basket.

    ``component_factor_ids`` is populated when the components were loaded
    from a validation report (so promotion can cite their lineage);
    otherwise it's an empty list and the components are anonymous.
    """

    method: CombinationMethod
    components: list[str]
    component_factor_ids: list[str] = Field(default_factory=list)
    recipe_id: str


class CombinationReport(BaseModel):
    """Single-file summary of one combination cycle."""

    schema_version: int = SCHEMA_VERSION
    cycle_id: str
    regime_trail_id: str
    universe_id: str = ""
    started_at: datetime
    finished_at: datetime
    recipe: CombinationRecipe
    basket_metrics: FactorThumbnail
    component_metrics: list[FactorThumbnail] = Field(default_factory=list)
    avg_pairwise_rank_corr: float | None = None
    passes_regime: bool = False
    notes: str = ""


# ── Builder ─────────────────────────────────────────────────────────────────


def build_combination_report(
    *,
    cycle_id: str,
    regime_trail_id: str,
    universe_id: str,
    started_at: datetime,
    method: CombinationMethod,
    components: list[str],
    component_factor_ids: list[str] | None,
    basket_metrics: FactorThumbnail,
    component_metrics: list[FactorThumbnail],
    avg_pairwise_rank_corr: float | None,
    passes_regime: bool,
    finished_at: datetime | None = None,
    notes: str = "",
) -> CombinationReport:
    """Assemble a :class:`CombinationReport` for one basket evaluation."""
    finished = finished_at or datetime.now(UTC)
    recipe = CombinationRecipe(
        method=method,
        components=list(components),
        component_factor_ids=list(component_factor_ids or []),
        recipe_id=recipe_id_for(method, components),
    )
    return CombinationReport(
        cycle_id=cycle_id,
        regime_trail_id=regime_trail_id,
        universe_id=universe_id,
        started_at=started_at,
        finished_at=finished,
        recipe=recipe,
        basket_metrics=basket_metrics,
        component_metrics=list(component_metrics),
        avg_pairwise_rank_corr=avg_pairwise_rank_corr,
        passes_regime=passes_regime,
        notes=notes,
    )


# ── Index helpers ───────────────────────────────────────────────────────────


def index_path(base_dir: Path | str = DEFAULT_COMBINATION_DIR) -> Path:
    return Path(base_dir) / COMBINATION_INDEX_NAME


def read_index(
    base_dir: Path | str = DEFAULT_COMBINATION_DIR,
) -> list[dict[str, Any]]:
    path = index_path(base_dir)
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                rows.append(json.loads(stripped))
            except json.JSONDecodeError as exc:
                logger.warning(
                    "Skipping corrupt combination index line %d in %s: %s",
                    i,
                    path,
                    exc,
                )
    return rows


# ── Writer (mirrors StrictValidationReportWriter) ───────────────────────────


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


class CombinationReportWriter:
    """Persist :class:`CombinationReport` instances to disk."""

    def __init__(
        self,
        base_dir: Path | str = DEFAULT_COMBINATION_DIR,
    ) -> None:
        self._base_dir = Path(base_dir)

    def write(self, report: CombinationReport) -> Path | None:
        try:
            return self._write(report)
        except OSError as exc:  # pragma: no cover — defensive
            logger.warning(
                "Failed to write combination report for %s: %s",
                report.cycle_id,
                exc,
            )
            return None

    def _write(self, report: CombinationReport) -> Path:
        path = self._base_dir / f"{report.cycle_id}.json"
        payload = json.loads(report.model_dump_json())
        _atomic_write_json(path, payload)
        self._upsert_index(report)
        logger.info("combination report written: %s", path)
        return path

    def _upsert_index(self, report: CombinationReport) -> None:
        idx = index_path(self._base_dir)
        rows = [
            r for r in _iter_index_entries(idx) if r.get("cycle_id") != report.cycle_id
        ]
        rows.append(
            {
                "cycle_id": report.cycle_id,
                "regime_trail_id": report.regime_trail_id,
                "universe_id": report.universe_id,
                "started_at": report.started_at.isoformat(),
                "finished_at": report.finished_at.isoformat(),
                "method": report.recipe.method.value,
                "recipe_id": report.recipe.recipe_id,
                "n_components": len(report.recipe.components),
                "basket_ic": report.basket_metrics.ic,
                "basket_rank_ic": report.basket_metrics.rank_ic,
                "passes_regime": report.passes_regime,
            }
        )
        self._base_dir.mkdir(parents=True, exist_ok=True)
        _rewrite_index(idx, rows)
