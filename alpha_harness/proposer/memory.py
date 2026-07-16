"""Proposer memory digest — compress recent experiments into a short string.

The hypothesis proposer already supports a ``related`` field populated by
the retrieval layer (semantic top-K per theme).  This module is the
complement: a globally-scoped, *recency*-ordered summary of what the loop
has tried lately, regardless of theme.

Intent
------
* Give the LLM a compact "here's what's been tried and how it landed"
  paragraph so it avoids re-proposing near-duplicates and avoids the same
  failure modes.
* Stay small — hard char cap keeps this from eating the prompt budget.
* Zero LLM calls: pure code, deterministic given a fixed registry state.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alpha_harness.combination import CombinationMethod, CombinationRecipe
from alpha_harness.proposer.schemas import CompositeAnchor
from alpha_harness.reports.validation import StrictValidationReport
from alpha_harness.schemas.experiment import ExperimentDecision, ExperimentRecord

logger = logging.getLogger(__name__)

# ── Defaults ────────────────────────────────────────────────────────────────

DEFAULT_MEMORY_DEPTH = 20
DEFAULT_MAX_CHARS = 1_200
DEFAULT_TOP_PROMOTED = 5
DEFAULT_TOP_REJECTED = 3
DEFAULT_TOP_COMPOSITES = 2


@dataclass(frozen=True)
class _MemoryRecord:
    """Backend-neutral experiment facts used to build proposer memory."""

    expression: str
    decision: ExperimentDecision
    ic: float | None = None
    failure_category: str | None = None


def build_memory_digest(
    records: list[ExperimentRecord],
    *,
    depth: int = DEFAULT_MEMORY_DEPTH,
    max_chars: int = DEFAULT_MAX_CHARS,
    top_promoted: int = DEFAULT_TOP_PROMOTED,
    top_rejected: int = DEFAULT_TOP_REJECTED,
    promoted_index_path: Path | str | None = None,
    top_composites: int = DEFAULT_TOP_COMPOSITES,
    validation_reports: Sequence[StrictValidationReport] = (),
) -> str:
    """Return a short prose digest of the most recent experiments.

    Ordering contract: callers should pass records in recency order (newest
    first) — the canonical way is ``registry.list_recent(limit=depth)``.
    When both ``records`` and ``validation_reports`` are empty, returns the
    empty string unless a promoted-composite index contributes context.

    ``validation_reports`` supplies durable facts from earlier processes.
    Callers must scope those reports to the current evaluation trail before
    passing them here; current in-process records always take precedence.

    The digest contains up to five sections:
        1. Rolling counts (n, promoted, refined, rejected).
        2. Promoted expressions (deduped, newest first).
        3. Top rejection categories with counts.
        4. Recently-proposed expression fingerprints to avoid repeating.
        5. (Round 9) Promoted composites — basket recipes registered via
           ``combine_factors --promote``.  Sourced from the durable
           ``artifacts/promoted/_index.jsonl`` mirror so composites
           survive across sessions even when the in-memory registry is
           fresh.  Only emitted when ``promoted_index_path`` is set and
           the index contains at least one composite.

    The combined output is truncated to ``max_chars`` with an explicit
    ``…[truncated]`` marker; the truncation keeps the prompt bounded even
    when the registry is huge.
    """
    composites_section = _build_composites_section(
        promoted_index_path,
        top_composites,
    )
    memory_records = _to_memory_records(records, validation_reports)
    if not memory_records:
        return composites_section  # may itself be empty

    trimmed = memory_records[:depth]

    # ── 1. Rolling counts ────────────────────────────────────────────
    counts: Counter[ExperimentDecision] = Counter(r.decision for r in trimmed)
    total = len(trimmed)
    header = (
        f"Recent experiments (last {total}): "
        f"promoted={counts.get(ExperimentDecision.PROMOTE_CANDIDATE, 0)} "
        f"refined={counts.get(ExperimentDecision.REFINE, 0)} "
        f"rejected={counts.get(ExperimentDecision.REJECT, 0)}"
    )

    sections: list[str] = [header]

    # ── 2. Promoted expressions ──────────────────────────────────────
    promoted = [r for r in trimmed if r.decision == ExperimentDecision.PROMOTE_CANDIDATE]
    if promoted:
        lines = ["Already promoted (do not re-propose near-duplicates):"]
        seen: set[str] = set()
        for r in promoted[:top_promoted]:
            expr = r.expression
            if expr in seen:
                continue
            seen.add(expr)
            ic = r.ic
            ic_str = f" ic={ic:.3f}" if ic is not None else ""
            lines.append(f"  - `{expr}`{ic_str}")
        sections.append("\n".join(lines))

    # ── 3. Rejection categories ──────────────────────────────────────
    rejected_categories: Counter[str] = Counter()
    for r in trimmed:
        if r.decision == ExperimentDecision.REJECT and r.failure_category is not None:
            rejected_categories[r.failure_category] += 1
    if rejected_categories:
        lines = ["Recent rejection modes (counts):"]
        for cat, n in rejected_categories.most_common(top_rejected):
            lines.append(f"  - {cat}: {n}")
        sections.append("\n".join(lines))

    # ── 4. Recently-proposed fingerprints ────────────────────────────
    recent_exprs: list[str] = []
    seen2: set[str] = set()
    for r in trimmed:
        expr = r.expression
        if expr in seen2:
            continue
        seen2.add(expr)
        recent_exprs.append(expr)
    if recent_exprs:
        joined = ", ".join(f"`{e}`" for e in recent_exprs[:10])
        sections.append(f"Recently tried expressions: {joined}")

    # ── 5. Promoted composites (Round 9) ─────────────────────────────
    if composites_section:
        sections.append(composites_section)

    digest = "\n\n".join(sections)
    if len(digest) > max_chars:
        digest = digest[: max_chars - len(" …[truncated]")] + " …[truncated]"
    return digest


def _to_memory_records(
    records: Sequence[ExperimentRecord],
    validation_reports: Sequence[StrictValidationReport],
) -> list[_MemoryRecord]:
    """Combine live records with durable report thumbnails in recency order."""
    combined = [
        _MemoryRecord(
            expression=record.factor.expression,
            decision=record.decision,
            ic=record.evaluation.ic,
            failure_category=(
                record.failure.category.value if record.failure is not None else None
            ),
        )
        for record in records
    ]
    for report in validation_reports:
        for factor in report.factors:
            try:
                decision = ExperimentDecision(factor.decision)
            except ValueError:
                logger.warning(
                    "Skipping validation-memory factor %s with unknown decision %r",
                    factor.factor_id,
                    factor.decision,
                )
                continue
            combined.append(
                _MemoryRecord(
                    expression=factor.expression,
                    decision=decision,
                    ic=factor.ic,
                    failure_category=factor.gate,
                ),
            )
    return combined


# ── Composite section (Round 9 A.1) ─────────────────────────────────────────


def _build_composites_section(
    promoted_index_path: Path | str | None,
    top_composites: int,
) -> str:
    """Render the "Promoted composites" digest section, or "" when none.

    Reads the durable PromotedArtifact index (``artifacts/promoted/
    _index.jsonl``), filters to entries whose persisted artifact carries
    a ``composite_recipe``, and emits a recency-ordered list of the most
    recent ``top_composites`` baskets.  Defensive: missing path, corrupt
    lines, and missing artifact files all degrade to "no section",
    never to a raised exception, so this can be wired into any proposer
    flow without a try/except in the caller.
    """
    anchors = load_composite_anchors(promoted_index_path, limit=top_composites)
    if not anchors:
        return ""
    lines = [
        "Recently promoted composites (use these as building blocks, "
        "don't re-propose the same recipe):",
    ]
    for anchor in anchors:
        recipe = anchor.recipe
        metrics = []
        if anchor.ic is not None:
            metrics.append(f"ic={anchor.ic:+.3f}")
        if anchor.rank_ic is not None:
            metrics.append(f"rank_ic={anchor.rank_ic:+.3f}")
        metrics_str = f"  ({', '.join(metrics)})" if metrics else ""
        components_str = ", ".join(recipe.components)
        lines.append(
            f"  - combine.{recipe.method.value}([{components_str}])  "
            f"recipe_id={recipe.recipe_id}{metrics_str}",
        )
    return "\n".join(lines)


def load_composite_anchors(
    promoted_index_path: Path | str | None,
    *,
    limit: int = DEFAULT_TOP_COMPOSITES,
) -> list[CompositeAnchor]:
    """Load recent, valid promoted composites as typed Round 10 anchors."""
    if promoted_index_path is None or limit <= 0:
        return []
    idx_path = Path(promoted_index_path)
    if not idx_path.is_file():
        return []
    rows = _read_index_rows(idx_path)
    rows.sort(key=lambda row: str(row.get("promoted_at", "")), reverse=True)
    anchors: list[CompositeAnchor] = []
    seen_recipes: set[str] = set()
    for row in rows:
        factor_id = row.get("factor_id")
        if not isinstance(factor_id, str) or not factor_id:
            continue
        raw = _load_composite_recipe(idx_path.parent / f"{factor_id}.json")
        if raw is None:
            continue
        try:
            method = CombinationMethod(str(raw.get("method", "")))
            components_raw = raw.get("components")
            if not isinstance(components_raw, list) or not all(
                isinstance(component, str) for component in components_raw
            ):
                continue
            component_ids_raw = raw.get("component_factor_ids", [])
            component_ids = (
                [str(item) for item in component_ids_raw]
                if isinstance(component_ids_raw, list)
                else []
            )
            recipe = CombinationRecipe.build(
                method=method,
                components=components_raw,
                component_factor_ids=component_ids,
            )
        except (TypeError, ValueError):
            continue
        persisted_recipe_id = raw.get("recipe_id")
        if persisted_recipe_id != recipe.recipe_id:
            logger.warning(
                "Skipping composite %s with inconsistent recipe_id %r (expected %s)",
                factor_id,
                persisted_recipe_id,
                recipe.recipe_id,
            )
            continue
        if recipe.recipe_id in seen_recipes:
            continue
        seen_recipes.add(recipe.recipe_id)
        anchors.append(
            CompositeAnchor(
                factor_id=factor_id,
                recipe=recipe,
                ic=_optional_float(row.get("ic")),
                rank_ic=_optional_float(row.get("rank_ic")),
                promoted_at=str(row.get("promoted_at", "")),
            )
        )
        if len(anchors) >= limit:
            break
    return anchors


def _optional_float(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def _read_index_rows(idx_path: Path) -> list[dict[str, Any]]:
    """Read promoted-artifact index lines defensively (skip + log on corruption)."""
    rows: list[dict[str, Any]] = []
    try:
        with idx_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    rows.append(json.loads(stripped))
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "Skipping corrupt promoted index line in %s: %s",
                        idx_path,
                        exc,
                    )
    except OSError as exc:  # pragma: no cover — defensive
        logger.warning("Failed to read promoted index %s: %s", idx_path, exc)
    return rows


def _load_composite_recipe(artifact_path: Path) -> dict[str, Any] | None:
    """Return the composite_recipe block of one PromotedArtifact, or ``None``."""
    if not artifact_path.is_file():
        return None
    try:
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Skipping unreadable artifact %s: %s", artifact_path, exc)
        return None
    recipe = payload.get("composite_recipe")
    if not isinstance(recipe, dict):
        return None
    return recipe
