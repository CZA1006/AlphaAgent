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

from collections import Counter

from alpha_harness.schemas.experiment import ExperimentDecision, ExperimentRecord

# ── Defaults ────────────────────────────────────────────────────────────────

DEFAULT_MEMORY_DEPTH = 20
DEFAULT_MAX_CHARS = 1_200
DEFAULT_TOP_PROMOTED = 5
DEFAULT_TOP_REJECTED = 3


def build_memory_digest(
    records: list[ExperimentRecord],
    *,
    depth: int = DEFAULT_MEMORY_DEPTH,
    max_chars: int = DEFAULT_MAX_CHARS,
    top_promoted: int = DEFAULT_TOP_PROMOTED,
    top_rejected: int = DEFAULT_TOP_REJECTED,
) -> str:
    """Return a short prose digest of the most recent experiments.

    Ordering contract: callers should pass records in recency order (newest
    first) — the canonical way is ``registry.list_recent(limit=depth)``.
    When ``records`` is empty, returns the empty string (caller can use
    this to decide whether to emit the memory section at all).

    The digest contains up to four sections:
        1. Rolling counts (n, promoted, refined, rejected).
        2. Promoted expressions (deduped, newest first).
        3. Top rejection categories with counts.
        4. Recently-proposed expression fingerprints to avoid repeating.

    The combined output is truncated to ``max_chars`` with an explicit
    ``…[truncated]`` marker; the truncation keeps the prompt bounded even
    when the registry is huge.
    """
    if not records:
        return ""

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

    digest = "\n\n".join(sections)
    if len(digest) > max_chars:
        digest = digest[: max_chars - len(" …[truncated]")] + " …[truncated]"
    return digest
