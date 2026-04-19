"""Deterministic experiment-lineage memory entries.

These entries are *factual and compact* snapshots of what happened in a
research cycle — no LLM summaries, no free-form prose.  They exist so that
later cycles (and human operators) can reconstruct the parent-child graph
of hypotheses and see at-a-glance metrics without re-loading the full
``ExperimentRecord``.

The content format is a single line of ``key=value`` pairs:

    exp=<id> factor=<name> decision=<decision> hypothesis=<id>
    parent=<id-or-"-"> ic=<ic-or-"-"> rank_ic=<ric-or-"-"> failure=<cat-or-"-">

Keeping it stable and grep-friendly means tests can assert on exact
strings, and future tooling can parse it without a schema migration.
"""

from __future__ import annotations

from alpha_harness.schemas.experiment import ExperimentRecord
from alpha_harness.schemas.memory import MemoryCategory, MemoryEntry


def build_lineage_entry(record: ExperimentRecord) -> MemoryEntry:
    """Build a compact, factual lineage entry for one completed experiment."""
    hypothesis = record.hypothesis
    evaluation = record.evaluation

    parent = hypothesis.parent_id or "-"
    ic = _fmt(evaluation.ic)
    rank_ic = _fmt(evaluation.rank_ic)
    failure = record.failure.category.value if record.failure is not None else "-"

    content = (
        f"exp={record.id} "
        f"factor={record.factor.name} "
        f"decision={record.decision.value} "
        f"hypothesis={hypothesis.id} "
        f"parent={parent} "
        f"ic={ic} "
        f"rank_ic={rank_ic} "
        f"failure={failure}"
    )

    tags = ["lineage", record.decision.value]
    if hypothesis.parent_id is not None:
        tags.append("child")
    else:
        tags.append("root")

    return MemoryEntry(
        category=MemoryCategory.EXPERIMENT_LINEAGE,
        content=content,
        source_experiment_ids=[record.id],
        tags=tags,
    )


def _fmt(value: float | None) -> str:
    """Format a metric value compactly, or ``-`` when missing."""
    if value is None:
        return "-"
    return f"{value:.4f}"
