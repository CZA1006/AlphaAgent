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
from pathlib import Path
from typing import Any

from alpha_harness.schemas.experiment import ExperimentDecision, ExperimentRecord

logger = logging.getLogger(__name__)

# ── Defaults ────────────────────────────────────────────────────────────────

DEFAULT_MEMORY_DEPTH = 20
DEFAULT_MAX_CHARS = 1_200
DEFAULT_TOP_PROMOTED = 5
DEFAULT_TOP_REJECTED = 3
DEFAULT_TOP_COMPOSITES = 2


def build_memory_digest(
    records: list[ExperimentRecord],
    *,
    depth: int = DEFAULT_MEMORY_DEPTH,
    max_chars: int = DEFAULT_MAX_CHARS,
    top_promoted: int = DEFAULT_TOP_PROMOTED,
    top_rejected: int = DEFAULT_TOP_REJECTED,
    promoted_index_path: Path | str | None = None,
    top_composites: int = DEFAULT_TOP_COMPOSITES,
) -> str:
    """Return a short prose digest of the most recent experiments.

    Ordering contract: callers should pass records in recency order (newest
    first) — the canonical way is ``registry.list_recent(limit=depth)``.
    When ``records`` is empty, returns the empty string (caller can use
    this to decide whether to emit the memory section at all).

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
        promoted_index_path, top_composites,
    )
    if not records:
        return composites_section  # may itself be empty

    trimmed = records[:depth]

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
    promoted = [
        r for r in trimmed
        if r.decision == ExperimentDecision.PROMOTE_CANDIDATE
    ]
    if promoted:
        lines = ["Already promoted (do not re-propose near-duplicates):"]
        seen: set[str] = set()
        for r in promoted[:top_promoted]:
            expr = r.factor.expression
            if expr in seen:
                continue
            seen.add(expr)
            ic = r.evaluation.ic
            ic_str = f" ic={ic:.3f}" if ic is not None else ""
            lines.append(f"  - `{expr}`{ic_str}")
        sections.append("\n".join(lines))

    # ── 3. Rejection categories ──────────────────────────────────────
    rejected_categories: Counter[str] = Counter()
    for r in trimmed:
        if r.decision == ExperimentDecision.REJECT and r.failure is not None:
            rejected_categories[r.failure.category.value] += 1
    if rejected_categories:
        lines = ["Recent rejection modes (counts):"]
        for cat, n in rejected_categories.most_common(top_rejected):
            lines.append(f"  - {cat}: {n}")
        sections.append("\n".join(lines))

    # ── 4. Recently-proposed fingerprints ────────────────────────────
    recent_exprs: list[str] = []
    seen2: set[str] = set()
    for r in trimmed:
        expr = r.factor.expression
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
    if promoted_index_path is None or top_composites <= 0:
        return ""
    idx_path = Path(promoted_index_path)
    if not idx_path.is_file():
        return ""

    rows = _read_index_rows(idx_path)
    if not rows:
        return ""

    # Sort newest first.  ``promoted_at`` is an ISO 8601 string written
    # by PromotedArtifactWriter, so lexicographic order == chronological.
    rows.sort(key=lambda r: r.get("promoted_at", ""), reverse=True)

    artifact_dir = idx_path.parent
    lines = [
        "Recently promoted composites (use these as building blocks, "
        "don't re-propose the same recipe):",
    ]
    emitted = 0
    seen_recipes: set[str] = set()
    for row in rows:
        if emitted >= top_composites:
            break
        factor_id = row.get("factor_id")
        if not factor_id:
            continue
        recipe = _load_composite_recipe(artifact_dir / f"{factor_id}.json")
        if recipe is None:
            continue
        recipe_id = recipe.get("recipe_id", "")
        if recipe_id in seen_recipes:
            continue
        seen_recipes.add(recipe_id)
        method = recipe.get("method", "?")
        components = recipe.get("components", [])
        ic = row.get("ic")
        ric = row.get("rank_ic")
        metrics = []
        if isinstance(ic, int | float):
            metrics.append(f"ic={ic:+.3f}")
        if isinstance(ric, int | float):
            metrics.append(f"rank_ic={ric:+.3f}")
        metrics_str = f"  ({', '.join(metrics)})" if metrics else ""
        components_str = ", ".join(components) if components else ""
        lines.append(
            f"  - combine.{method}([{components_str}])  "
            f"recipe_id={recipe_id}{metrics_str}",
        )
        emitted += 1
    if emitted == 0:
        return ""
    return "\n".join(lines)


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
