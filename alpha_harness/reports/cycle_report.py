"""Cycle reports — durable, JSON-serialisable audit of one autonomous run.

Each autonomous cycle persists exactly one :class:`CycleReport` to disk
plus an append-only ``_index.jsonl`` row keyed by ``cycle_id``.  The
write path mirrors :class:`alpha_harness.artifacts.PromotedArtifactWriter`:
atomic file replace, idempotent index upsert, best-effort error handling.
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

from pydantic import BaseModel, Field

from alpha_harness.registries.protocols import ExperimentRegistryProtocol
from alpha_harness.schemas.experiment import ExperimentDecision, ExperimentRecord

logger = logging.getLogger(__name__)

DEFAULT_REPORT_DIR = Path("artifacts/reports")
REPORT_INDEX_NAME = "_index.jsonl"
SCHEMA_VERSION = 1


# ── Schemas ─────────────────────────────────────────────────────────────────


class BudgetSnapshot(BaseModel):
    """LLM token + cost spend recorded at end-of-cycle."""

    total_tokens_spent: int = 0
    cost_usd_spent: float = 0.0
    max_total_tokens: int | None = None
    max_cost_usd: float | None = None
    calls: int = 0
    prompt_cost_per_1k: float = 0.0
    completion_cost_per_1k: float = 0.0


class ExperimentThumbnail(BaseModel):
    """Minimal per-experiment record sufficient for an audit trail.

    Carries the lineage fields introduced in 4A.6/4A.7 so a report
    alone can answer "which factor came from which refinement chain?".
    """

    experiment_id: str
    factor_id: str
    factor_name: str
    expression: str
    decision: str
    refinement_round: int = 0
    parent_factor_id: str | None = None
    ic: float | None = None
    rank_ic: float | None = None
    quantile_spread: float | None = None
    net_quantile_spread: float | None = None
    turnover: float | None = None
    # Risk-aware portfolio metrics (Round 4C).  Pulled from
    # ``metadata.portfolio`` when the evaluator surfaced them; absent on
    # bundles produced by older evaluators.
    sharpe: float | None = None
    max_drawdown: float | None = None
    hit_rate: float | None = None
    # Walk-forward audit (Round 4D).  When the evaluator wrapped folds,
    # the per-experiment payload includes ``n_folds``, ``embargo_days``,
    # ``purged_folds``, and stability fractions; otherwise None.
    walk_forward: dict[str, Any] | None = None
    # Holdout audit (Round 4E).  Carries ``rank_ic`` and ``decay_ratio``
    # when the evaluator reserved a tail slice; otherwise None.  We keep
    # this slim — the full block bloats reports — and the registry holds
    # the unabridged ``metadata.holdout`` for deeper audits.
    holdout: dict[str, Any] | None = None
    failure_category: str | None = None


class CycleReport(BaseModel):
    """The whole cycle in a single document."""

    schema_version: int = SCHEMA_VERSION
    cycle_id: str
    theme: str = ""
    started_at: datetime
    finished_at: datetime
    duration_s: float
    n_experiments: int
    n_promoted: int
    n_refined: int
    n_rejected: int
    n_archived: int = 0
    refinement_rounds_seen: dict[str, int] = Field(default_factory=dict)
    promoted_factor_ids: list[str] = Field(default_factory=list)
    experiments: list[ExperimentThumbnail] = Field(default_factory=list)
    budget: BudgetSnapshot | None = None
    llm_log_path: str | None = None
    notes: str = ""


# ── Builder ─────────────────────────────────────────────────────────────────


def build_cycle_report(
    *,
    cycle_id: str,
    theme: str,
    started_at: datetime,
    experiment_registry: ExperimentRegistryProtocol,
    experiment_ids: list[str],
    finished_at: datetime | None = None,
    budget: BudgetSnapshot | None = None,
    llm_log_path: str | None = None,
    notes: str = "",
) -> CycleReport:
    """Assemble a :class:`CycleReport` from registry lookups + timing.

    ``experiment_ids`` should hold the ids of records produced by *this*
    cycle (root + refinement); the registry is queried to hydrate each
    one.  Missing ids are skipped with a warning rather than aborting —
    a partial report is more useful than none.
    """
    finished = finished_at or datetime.now(UTC)
    duration = max(0.0, (finished - started_at).total_seconds())

    thumbnails: list[ExperimentThumbnail] = []
    rounds: dict[int, int] = {}
    counts: dict[str, int] = {}
    promoted_factor_ids: list[str] = []

    for eid in experiment_ids:
        record = experiment_registry.get(eid)
        if record is None:
            logger.warning("cycle report: experiment %s not in registry", eid)
            continue
        thumbnails.append(_thumbnail(record))
        r = record.factor.refinement_round
        rounds[r] = rounds.get(r, 0) + 1
        key = record.decision.value
        counts[key] = counts.get(key, 0) + 1
        if record.decision == ExperimentDecision.PROMOTE_CANDIDATE:
            promoted_factor_ids.append(record.factor.id)

    return CycleReport(
        cycle_id=cycle_id,
        theme=theme,
        started_at=started_at,
        finished_at=finished,
        duration_s=duration,
        n_experiments=len(thumbnails),
        n_promoted=counts.get(ExperimentDecision.PROMOTE_CANDIDATE.value, 0),
        n_refined=counts.get(ExperimentDecision.REFINE.value, 0),
        n_rejected=counts.get(ExperimentDecision.REJECT.value, 0),
        n_archived=counts.get(ExperimentDecision.ARCHIVE_ONLY.value, 0),
        refinement_rounds_seen={str(k): v for k, v in sorted(rounds.items())},
        promoted_factor_ids=promoted_factor_ids,
        experiments=thumbnails,
        budget=budget,
        llm_log_path=llm_log_path,
        notes=notes,
    )


def _holdout_summary(holdout: Any) -> dict[str, Any] | None:
    """Slim ``metadata.holdout`` to ``rank_ic`` + ``decay_ratio`` for the report."""
    if not isinstance(holdout, dict):
        return None
    summary: dict[str, Any] = {}
    for key in ("rank_ic", "decay_ratio", "holdout_start", "holdout_end"):
        if key in holdout:
            summary[key] = holdout[key]
    return summary or None


def _thumbnail(record: ExperimentRecord) -> ExperimentThumbnail:
    ev = record.evaluation
    portfolio = ev.metadata.get("portfolio") or {}
    if not isinstance(portfolio, dict):
        portfolio = {}

    def _metric(name: str) -> float | None:
        v = portfolio.get(name)
        return float(v) if isinstance(v, int | float) else None

    return ExperimentThumbnail(
        experiment_id=record.id,
        factor_id=record.factor.id,
        factor_name=record.factor.name,
        expression=record.factor.expression,
        decision=record.decision.value,
        refinement_round=record.factor.refinement_round,
        parent_factor_id=record.factor.parent_factor_id,
        ic=ev.ic,
        rank_ic=ev.rank_ic,
        quantile_spread=ev.quantile_spread,
        net_quantile_spread=ev.net_quantile_spread,
        turnover=ev.turnover,
        sharpe=ev.sharpe if ev.sharpe is not None else _metric("sharpe"),
        max_drawdown=_metric("max_drawdown"),
        hit_rate=_metric("hit_rate"),
        walk_forward=(
            dict(ev.metadata["walk_forward"])
            if isinstance(ev.metadata.get("walk_forward"), dict)
            else None
        ),
        holdout=_holdout_summary(ev.metadata.get("holdout")),
        failure_category=(record.failure.category.value if record.failure is not None else None),
    )


# ── Index helpers ───────────────────────────────────────────────────────────


def index_path(base_dir: Path | str = DEFAULT_REPORT_DIR) -> Path:
    return Path(base_dir) / REPORT_INDEX_NAME


def read_index(base_dir: Path | str = DEFAULT_REPORT_DIR) -> list[dict[str, Any]]:
    """Load the report index.  Returns empty list when the file is absent."""
    path = index_path(base_dir)
    if not path.is_file():
        return []
    entries: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                entries.append(json.loads(stripped))
            except json.JSONDecodeError as exc:
                logger.warning(
                    "Skipping corrupt report index line %d in %s: %s",
                    i,
                    path,
                    exc,
                )
    return entries


# ── Writer ──────────────────────────────────────────────────────────────────


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


class CycleReportWriter:
    """Persist :class:`CycleReport` instances to ``artifacts/reports/``.

    Best-effort: a disk failure logs a warning and returns ``None``;
    cycle correctness lives in the registry, the report is a mirror.
    """

    def __init__(self, base_dir: Path | str = DEFAULT_REPORT_DIR) -> None:
        self._base_dir = Path(base_dir)

    def write(self, report: CycleReport) -> Path | None:
        try:
            return self._write(report)
        except OSError as exc:  # pragma: no cover — defensive
            logger.warning(
                "Failed to write cycle report for %s: %s",
                report.cycle_id,
                exc,
            )
            return None

    # ── Internals ────────────────────────────────────────────────────────

    def _write(self, report: CycleReport) -> Path:
        path = self._base_dir / f"{report.cycle_id}.json"
        payload = json.loads(report.model_dump_json())
        _atomic_write_json(path, payload)
        self._upsert_index(report.cycle_id, self._index_entry(report))
        logger.info("cycle report written: %s", path)
        return path

    def _index_entry(self, report: CycleReport) -> dict[str, Any]:
        return {
            "cycle_id": report.cycle_id,
            "theme": report.theme,
            "started_at": report.started_at.isoformat(),
            "finished_at": report.finished_at.isoformat(),
            "duration_s": report.duration_s,
            "n_experiments": report.n_experiments,
            "n_promoted": report.n_promoted,
            "n_refined": report.n_refined,
            "n_rejected": report.n_rejected,
            "promoted_factor_ids": list(report.promoted_factor_ids),
        }

    def _upsert_index(self, cycle_id: str, entry: dict[str, Any]) -> None:
        path = index_path(self._base_dir)
        existing = [e for e in _iter_index_entries(path) if e.get("cycle_id") != cycle_id]
        existing.append(entry)
        path.parent.mkdir(parents=True, exist_ok=True)
        _rewrite_index(path, existing)


# ── Convenience: snapshot a TokenBudget ─────────────────────────────────────


def snapshot_budget(budget: Any | None) -> BudgetSnapshot | None:
    """Capture spend from a :class:`alpha_harness.llm.TokenBudget`-shaped object.

    Accepts duck-typed inputs so tests don't need to import the LLM
    package.  Returns ``None`` when ``budget`` is ``None``.
    """
    if budget is None:
        return None
    return BudgetSnapshot(
        total_tokens_spent=int(getattr(budget, "total_tokens_spent", 0)),
        cost_usd_spent=float(getattr(budget, "cost_usd_spent", 0.0)),
        max_total_tokens=getattr(budget, "max_total_tokens", None),
        max_cost_usd=getattr(budget, "max_cost_usd", None),
        calls=int(getattr(budget, "calls", 0)),
        prompt_cost_per_1k=float(getattr(budget, "prompt_cost_per_1k", 0.0)),
        completion_cost_per_1k=float(getattr(budget, "completion_cost_per_1k", 0.0)),
    )
