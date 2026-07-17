"""Per-promotion JSON artifact writer.

Every ``PROMOTE_CANDIDATE`` decision that reaches the registry also writes
a stable, human-readable record to disk:

* ``{dir}/{factor_id}.json`` — the full artifact.  Atomic write via
  ``os.replace`` on a sibling ``.tmp`` file so a crash mid-flight leaves
  either the old file or the new file, never a partial one.
* ``{dir}/_index.jsonl`` — append-only index, one line per *unique*
  factor id.  Re-promoting the same factor id rewrites the ``.json`` but
  does *not* duplicate the index line.

The writer is best-effort: a disk failure logs a warning but never
breaks the research loop.  Promotion still lives in the registry; the
artifact is a convenience mirror.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alpha_harness.artifacts.store import LocalArtifactStore
from alpha_harness.schemas.evaluation import EvaluationBundle
from alpha_harness.schemas.experiment import (
    ExperimentDecision,
    ExperimentRecord,
    PromotionTrail,
)
from alpha_harness.schemas.factor import FactorSpec
from alpha_harness.schemas.hypothesis import Hypothesis

logger = logging.getLogger(__name__)

DEFAULT_PROMOTED_DIR = Path("artifacts/promoted")
PROMOTED_INDEX_NAME = "_index.jsonl"


def index_path(base_dir: Path | str = DEFAULT_PROMOTED_DIR) -> Path:
    """Return the path to the append-only index inside ``base_dir``."""
    return Path(base_dir) / PROMOTED_INDEX_NAME


def read_index(base_dir: Path | str = DEFAULT_PROMOTED_DIR) -> list[dict[str, Any]]:
    """Load all index entries.  Returns empty list when the file is absent."""
    return LocalArtifactStore.for_directory("promoted", base_dir).list_index("promoted")


def read_artifact(
    factor_id: str,
    base_dir: Path | str = DEFAULT_PROMOTED_DIR,
) -> dict[str, Any] | None:
    """Load the per-factor JSON for ``factor_id``.

    Returns the parsed dict, or ``None`` when the file is missing or
    unreadable.  The caller decides how to react (CLIs typically exit
    non-zero with a clear message).
    """
    return LocalArtifactStore.for_directory("promoted", base_dir).read("promoted", factor_id)


def record_from_payload(payload: dict[str, Any]) -> ExperimentRecord:
    """Rehydrate an :class:`ExperimentRecord` from a v3 artifact payload.

    Reconstructs just the fields the writer captured — this is enough
    for downstream tools (notably the Round 4G refinement guard) to
    reason about the promotion.  The original full registry record may
    carry more (notes, tags, lineage memory hooks); those aren't part
    of the artifact contract.

    Older payloads (v1/v2) work too; they simply yield records without
    a ``promotion_trail``.
    """
    ev_block = payload.get("evaluation") or {}
    bundle = EvaluationBundle(
        ic=ev_block.get("ic"),
        rank_ic=ev_block.get("rank_ic"),
        quantile_spread=ev_block.get("quantile_spread"),
        net_quantile_spread=ev_block.get("net_quantile_spread"),
        turnover=ev_block.get("turnover"),
        n_periods=ev_block.get("n_periods"),
        n_assets=ev_block.get("n_assets"),
        forecast_horizon_bars=ev_block.get("forecast_horizon_bars"),
        metadata=ev_block.get("metadata") or {},
    )
    factor = FactorSpec(
        id=str(payload.get("factor_id", "")),
        name=str(payload.get("factor_name", "")),
        expression=str(payload.get("expression", "")),
        operator_tree=payload.get("operator_tree"),
        parent_factor_id=payload.get("parent_factor_id"),
        refinement_round=int(payload.get("refinement_round", 0) or 0),
    )
    hypothesis = Hypothesis(
        id=str(payload.get("hypothesis_id") or "rehydrated"),
        text=str(payload.get("hypothesis_text") or factor.expression),
        rationale=str(payload.get("hypothesis_rationale") or ""),
    )
    trail_block = payload.get("promotion_trail")
    promotion_trail: PromotionTrail | None = None
    if isinstance(trail_block, dict) and trail_block.get("trail_id"):
        promotion_trail = PromotionTrail.model_validate(trail_block)
    return ExperimentRecord(
        id=str(payload.get("experiment_id") or "rehydrated"),
        hypothesis=hypothesis,
        factor=factor,
        evaluation=bundle,
        decision=ExperimentDecision.PROMOTE_CANDIDATE,
        notes=str(payload.get("notes") or ""),
        promotion_trail=promotion_trail,
    )


def _git_head() -> str:
    """Return the short git SHA, or ``""`` if git is unavailable."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if out.returncode != 0:
        return ""
    return out.stdout.strip()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` to ``path`` atomically via a temp-file + rename."""
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
    """Rewrite the index atomically from a sequence of entries."""
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, sort_keys=True, default=str))
            fh.write("\n")
    os.replace(tmp, path)


class PromotedArtifactWriter:
    """Persist PROMOTE_CANDIDATE experiments to disk.

    One writer is safe to share across a cycle; all methods are pure
    functions of the record + directory state.  Callers typically invoke
    :meth:`maybe_write` from the orchestrator's post-save hook so any
    record — not just promotions — can be fed in without branching.
    """

    def __init__(
        self,
        base_dir: Path | str = DEFAULT_PROMOTED_DIR,
        *,
        cycle_id: str | None = None,
        trail_registry: Any | None = None,
    ) -> None:
        self._base_dir = Path(base_dir)
        self._cycle_id = cycle_id
        # Optional :class:`TrailRegistryWriter` — when supplied, every
        # promote also lands in the trail-registry mirror.  Typed as
        # ``Any`` to avoid a circular import; the writer is duck-typed
        # against ``record(trail, factor_id) -> bool``.
        self._trail_registry = trail_registry

    # ── Public API ───────────────────────────────────────────────────────

    def maybe_write(self, record: ExperimentRecord) -> Path | None:
        """Write the artifact iff ``record`` is a promotion.

        Returns the artifact path on success, ``None`` when skipped, and
        logs a warning (returns ``None``) if writing raises.  Never
        propagates disk exceptions — the research loop owns correctness.
        """
        if record.decision != ExperimentDecision.PROMOTE_CANDIDATE:
            return None
        try:
            return self._write(record)
        except OSError as exc:  # pragma: no cover — defensive
            logger.warning(
                "Failed to write promotion artifact for %s: %s",
                record.id,
                exc,
            )
            return None

    # ── Internals ────────────────────────────────────────────────────────

    def _write(self, record: ExperimentRecord) -> Path:
        factor_id = record.factor.id
        payload = self._build_payload(record)
        artifact_path = LocalArtifactStore.for_directory("promoted", self._base_dir).write(
            "promoted", factor_id, payload
        )
        self._upsert_index(factor_id, self._build_index_entry(record, payload))

        # Round 4J — also record the trail in the standalone registry
        # when one was supplied.  Best-effort; legacy promotions without
        # a trail (record.promotion_trail is None) are silently ignored.
        if self._trail_registry is not None:
            try:
                self._trail_registry.record(record.promotion_trail, factor_id)
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning(
                    "Trail registry write failed for %s: %s",
                    factor_id,
                    exc,
                )
        logger.info(
            "promotion artifact written: %s (factor_id=%s)",
            artifact_path,
            factor_id,
        )
        return artifact_path

    def _build_payload(self, record: ExperimentRecord) -> dict[str, Any]:
        ev = record.evaluation
        trail = record.promotion_trail
        # Round 8: serialize composite_recipe when the promoted factor is a
        # basket — the recipe is the canonical content the audit story
        # references, so it has to land in the artifact alongside the
        # placeholder expression.
        composite = (
            record.factor.composite_recipe.model_dump(mode="json")
            if record.factor.composite_recipe is not None
            else None
        )
        return {
            "schema_version": 3,
            "experiment_id": record.id,
            "factor_id": record.factor.id,
            "factor_name": record.factor.name,
            "expression": record.factor.expression,
            "operator_tree": record.factor.operator_tree,
            "composite_recipe": composite,
            "parent_factor_id": record.factor.parent_factor_id,
            "refinement_round": record.factor.refinement_round,
            "promotion_trail": (trail.model_dump(mode="json") if trail is not None else None),
            "hypothesis_id": record.hypothesis.id,
            "hypothesis_text": record.hypothesis.text,
            "hypothesis_rationale": record.hypothesis.rationale,
            "decision": record.decision.value,
            "notes": record.notes,
            "evaluation": {
                "ic": ev.ic,
                "rank_ic": ev.rank_ic,
                "quantile_spread": ev.quantile_spread,
                "net_quantile_spread": ev.net_quantile_spread,
                "turnover": ev.turnover,
                "n_periods": ev.n_periods,
                "n_assets": ev.n_assets,
                "forecast_horizon_bars": ev.forecast_horizon_bars,
                "eval_start": ev.eval_start,
                "eval_end": ev.eval_end,
                "metadata": ev.metadata,
            },
            "reproducibility": {
                "code_version": (record.reproducibility.code_version or _git_head()),
                "dataset_snapshot_id": record.reproducibility.dataset_snapshot_id,
                "universe_snapshot_id": record.reproducibility.universe_snapshot_id,
                "config_snapshot": record.reproducibility.config_snapshot,
            },
            "cycle_id": self._cycle_id or "",
            "tags": list(record.tags),
            "promoted_at": datetime.now(UTC).isoformat(),
        }

    def _build_index_entry(
        self,
        record: ExperimentRecord,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        ev = record.evaluation
        trail = record.promotion_trail
        return {
            "factor_id": record.factor.id,
            "factor_name": record.factor.name,
            "expression": record.factor.expression,
            "parent_factor_id": record.factor.parent_factor_id,
            "refinement_round": record.factor.refinement_round,
            "trail_id": trail.trail_id if trail is not None else None,
            "ic": ev.ic,
            "rank_ic": ev.rank_ic,
            "net_quantile_spread": ev.net_quantile_spread,
            "turnover": ev.turnover,
            "n_periods": ev.n_periods,
            "n_assets": ev.n_assets,
            "promoted_at": payload["promoted_at"],
            "cycle_id": payload["cycle_id"],
            "experiment_id": record.id,
        }

    def _upsert_index(self, factor_id: str, entry: dict[str, Any]) -> None:
        """Append ``entry`` to the index, replacing any prior line with the
        same ``factor_id``.  Keeps the file bounded in the re-promotion
        case without losing the latest values.
        """
        path = index_path(self._base_dir)
        existing = [e for e in _iter_index_entries(path) if e.get("factor_id") != factor_id]
        existing.append(entry)
        path.parent.mkdir(parents=True, exist_ok=True)
        _rewrite_index(path, existing)
