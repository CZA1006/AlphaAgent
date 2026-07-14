"""Strict-validation report (Round 5).

After a ``validate_strict`` run, this module persists a single JSON
summary describing how the strict regime gated each candidate:

* the regime ``trail_id`` (the immutable hash of every evaluator+judge
  knob — see Round 4F)
* counts: how many proposals tried, how many promoted, how many
  refined, rejected, and **per-gate** rejection counts parsed from
  each ``record.failure.detail`` line
* the ``factor_id`` of every promoted candidate

Same on-disk shape as :class:`alpha_harness.reports.cycle_report` —
``{cycle_id}.json`` plus an append-only ``_index.jsonl`` keyed on
cycle_id.  The writer is best-effort: a disk failure logs and
returns ``None``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from alpha_harness.reports.cycle_report import BudgetSnapshot, snapshot_budget
from alpha_harness.schemas.experiment import ExperimentDecision, ExperimentRecord

logger = logging.getLogger(__name__)

DEFAULT_VALIDATION_DIR = Path("artifacts/validations")
VALIDATION_INDEX_NAME = "_index.jsonl"
SCHEMA_VERSION = 4


# ── Schema ──────────────────────────────────────────────────────────────────


class FactorThumbnail(BaseModel):
    """Per-factor record persisted into the validation report.

    Captures the full DSL expression plus headline metrics so downstream
    tooling (notably :mod:`scripts.combine_factors`) can re-load the
    full factor set without re-running the whole cycle.  When the
    factor was rejected, ``gate`` is the canonical gate name from
    :func:`classify_failure`.
    """

    factor_id: str
    expression: str
    decision: str
    ic: float | None = None
    rank_ic: float | None = None
    quantile_spread: float | None = None
    net_quantile_spread: float | None = None
    sharpe: float | None = None
    turnover: float | None = None
    gate: str | None = None  # set when decision == reject
    failure_detail: str = ""
    # Round 9.1 — holdout metrics surfaced from bundle.metadata["holdout"]
    # so a reader of the report can see whether the in-sample edge
    # survived out-of-sample.  All three are ``None`` when the
    # evaluator wasn't run with a TAIL holdout policy.
    holdout_ic: float | None = None
    holdout_rank_ic: float | None = None
    holdout_decay_ratio: float | None = None


class StrictValidationReport(BaseModel):
    """Single-file summary of one strict-regime validation cycle."""

    schema_version: int = SCHEMA_VERSION
    cycle_id: str
    regime_trail_id: str
    universe_id: str = ""
    # Hash of the full evaluation request plus the actual input panel. Unlike
    # PromotionTrail, this changes when dates, thresholds, universe, or data
    # contents change, so proposer feedback cannot cross research samples.
    memory_scope_id: str = ""
    data_fingerprint: str = ""
    candidate_source: str = "propose"
    source_cycle_ids: list[str] = Field(default_factory=list)
    cost_bps: float = 0.0
    started_at: datetime
    finished_at: datetime
    n_proposals: int
    n_promoted: int
    n_refined: int
    n_rejected: int
    n_rejected_by_gate: dict[str, int] = Field(default_factory=dict)
    promoted_factor_ids: list[str] = Field(default_factory=list)
    promoted_trail_ids: list[str] = Field(default_factory=list)
    # Round 7 — per-factor thumbnails so reports are self-contained:
    # every factor (promoted + rejected) survives with its expression +
    # headline metrics, no need to keep the in-memory registry around.
    factors: list[FactorThumbnail] = Field(default_factory=list)
    budget: BudgetSnapshot | None = None
    notes: str = ""


# ── Gate classifier ─────────────────────────────────────────────────────────


# Each pattern is (gate_name, compiled regex).  Order matters — the first
# match wins so more specific patterns precede more general ones.  The
# strings come from the failure details emitted by PromotionJudge across
# Rounds 4A.3 / 4B / 4C / 4E.
_GATE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("data_insufficient", re.compile(r"n_(periods|assets)=")),
    ("threshold_ic", re.compile(r"\bic=.*<\s*threshold")),
    ("threshold_rank_ic", re.compile(r"\brank_ic=.*<\s*threshold")),
    ("threshold_quantile_spread", re.compile(r"\bquantile_spread=.*<\s*threshold")),
    ("missing_metric", re.compile(r"Required metric .* is missing")),
    ("sign_consistency", re.compile(r"ic_sign_consistent_horizons=")),
    ("walk_forward_stability", re.compile(r"fraction_positive_rank_ic=")),
    ("tail_concentration", re.compile(r"tail_concentration=")),
    ("holdout_sign_flip", re.compile(r"holdout rank_ic=.*disagrees in sign")),
    ("holdout_decay", re.compile(r"holdout/in-sample rank_ic ratio=")),
    ("duplicate", re.compile(r"Too similar to factor")),
]


def classify_failure(detail: str) -> str:
    """Return the gate name that best explains ``detail``.

    Falls back to ``'other'`` so the dict always sums to total rejections
    even when the judge's failure text drifts.
    """
    if not detail:
        return "other"
    for name, pattern in _GATE_PATTERNS:
        if pattern.search(detail):
            return name
    return "other"


# ── Builder ─────────────────────────────────────────────────────────────────


def build_validation_report(
    *,
    cycle_id: str,
    regime_trail_id: str,
    universe_id: str,
    memory_scope_id: str = "",
    data_fingerprint: str = "",
    candidate_source: str = "propose",
    source_cycle_ids: list[str] | None = None,
    cost_bps: float = 0.0,
    started_at: datetime,
    records: list[ExperimentRecord],
    finished_at: datetime | None = None,
    notes: str = "",
    budget: Any | None = None,
) -> StrictValidationReport:
    """Aggregate ``records`` into a :class:`StrictValidationReport`."""
    finished = finished_at or datetime.now(UTC)
    counts = {"promoted": 0, "refined": 0, "rejected": 0, "archived": 0}
    by_gate: dict[str, int] = {}
    promoted_ids: list[str] = []
    promoted_trail_ids: list[str] = []
    thumbnails: list[FactorThumbnail] = []
    for r in records:
        gate: str | None = None
        detail = ""
        if r.decision == ExperimentDecision.PROMOTE_CANDIDATE:
            counts["promoted"] += 1
            promoted_ids.append(r.factor.id)
            if r.promotion_trail is not None:
                promoted_trail_ids.append(r.promotion_trail.trail_id)
        elif r.decision == ExperimentDecision.REFINE:
            counts["refined"] += 1
        elif r.decision == ExperimentDecision.REJECT:
            counts["rejected"] += 1
            detail = r.failure.detail if r.failure is not None else ""
            gate = classify_failure(detail)
            by_gate[gate] = by_gate.get(gate, 0) + 1
        else:
            counts["archived"] += 1

        ev = r.evaluation
        # Round 9.1 — pull holdout fields out of metadata so the
        # report is self-contained.  All three default to ``None``
        # when the evaluator ran without a TAIL holdout.
        holdout_meta = ev.metadata.get("holdout") if isinstance(ev.metadata, dict) else None
        holdout_ic = holdout_rank_ic = holdout_decay_ratio = None
        if isinstance(holdout_meta, dict):
            holdout_ic = holdout_meta.get("ic")
            holdout_rank_ic = holdout_meta.get("rank_ic")
            holdout_decay_ratio = holdout_meta.get("decay_ratio")
        thumbnails.append(
            FactorThumbnail(
                factor_id=r.factor.id,
                expression=r.factor.expression,
                decision=r.decision.value,
                ic=ev.ic,
                rank_ic=ev.rank_ic,
                quantile_spread=ev.quantile_spread,
                net_quantile_spread=ev.net_quantile_spread,
                sharpe=ev.sharpe,
                turnover=ev.turnover,
                gate=gate,
                failure_detail=detail,
                holdout_ic=holdout_ic,
                holdout_rank_ic=holdout_rank_ic,
                holdout_decay_ratio=holdout_decay_ratio,
            ),
        )

    return StrictValidationReport(
        cycle_id=cycle_id,
        regime_trail_id=regime_trail_id,
        universe_id=universe_id,
        memory_scope_id=memory_scope_id,
        data_fingerprint=data_fingerprint,
        candidate_source=candidate_source,
        source_cycle_ids=list(source_cycle_ids or []),
        cost_bps=cost_bps,
        started_at=started_at,
        finished_at=finished,
        n_proposals=len(records),
        n_promoted=counts["promoted"],
        n_refined=counts["refined"],
        n_rejected=counts["rejected"],
        n_rejected_by_gate=dict(sorted(by_gate.items())),
        promoted_factor_ids=promoted_ids,
        promoted_trail_ids=promoted_trail_ids,
        factors=thumbnails,
        budget=snapshot_budget(budget),
        notes=notes,
    )


# ── Index helpers ───────────────────────────────────────────────────────────


def index_path(base_dir: Path | str = DEFAULT_VALIDATION_DIR) -> Path:
    return Path(base_dir) / VALIDATION_INDEX_NAME


def read_index(
    base_dir: Path | str = DEFAULT_VALIDATION_DIR,
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
                    "Skipping corrupt validation index line %d in %s: %s",
                    i,
                    path,
                    exc,
                )
    return rows


def read_reports(
    base_dir: Path | str = DEFAULT_VALIDATION_DIR,
    *,
    regime_trail_id: str | None = None,
    memory_scope_id: str | None = None,
    exclude_cycle_prefix: str | None = None,
    limit: int | None = None,
) -> list[StrictValidationReport]:
    """Load full validation reports newest first.

    ``memory_scope_id`` is the point-in-time boundary for reusable feedback;
    callers should normally provide it. ``regime_trail_id`` remains available
    for narrower tooling queries. ``exclude_cycle_prefix`` prevents a rerun
    from learning from an older artifact with the same cycle id before that
    artifact is replaced.
    """
    if limit is not None and limit < 0:
        raise ValueError("limit must be >= 0")
    if limit == 0:
        return []

    root = Path(base_dir)
    reports: list[StrictValidationReport] = []
    for row in read_index(root):
        cycle_id = str(row.get("cycle_id", ""))
        if not cycle_id:
            continue
        if exclude_cycle_prefix and (
            cycle_id == exclude_cycle_prefix or cycle_id.startswith(f"{exclude_cycle_prefix}-c")
        ):
            continue
        path = root / f"{cycle_id}.json"
        try:
            report = StrictValidationReport.model_validate_json(
                path.read_text(encoding="utf-8"),
            )
        except (OSError, ValueError) as exc:
            logger.warning("Skipping unreadable validation report %s: %s", path, exc)
            continue
        if regime_trail_id is not None and report.regime_trail_id != regime_trail_id:
            continue
        if memory_scope_id is not None and report.memory_scope_id != memory_scope_id:
            continue
        reports.append(report)

    reports.sort(key=lambda report: report.finished_at, reverse=True)
    return reports if limit is None else reports[:limit]


def read_report(
    base_dir: Path | str,
    cycle_id: str,
) -> StrictValidationReport | None:
    """Load one full validation report by its exact cycle id."""
    path = Path(base_dir) / f"{cycle_id}.json"
    try:
        return StrictValidationReport.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Unable to load validation report %s: %s", path, exc)
        return None


# ── Writer (mirrors PromotedArtifactWriter's atomic shape) ──────────────────


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


class StrictValidationReportWriter:
    """Persist :class:`StrictValidationReport` instances to disk."""

    def __init__(
        self,
        base_dir: Path | str = DEFAULT_VALIDATION_DIR,
    ) -> None:
        self._base_dir = Path(base_dir)

    def write(self, report: StrictValidationReport) -> Path | None:
        try:
            return self._write(report)
        except OSError as exc:  # pragma: no cover — defensive
            logger.warning(
                "Failed to write validation report for %s: %s",
                report.cycle_id,
                exc,
            )
            return None

    def _write(self, report: StrictValidationReport) -> Path:
        path = self._base_dir / f"{report.cycle_id}.json"
        payload = json.loads(report.model_dump_json())
        _atomic_write_json(path, payload)
        self._upsert_index(report)
        logger.info("validation report written: %s", path)
        return path

    def _upsert_index(self, report: StrictValidationReport) -> None:
        idx = index_path(self._base_dir)
        rows = [r for r in _iter_index_entries(idx) if r.get("cycle_id") != report.cycle_id]
        rows.append(
            {
                "cycle_id": report.cycle_id,
                "regime_trail_id": report.regime_trail_id,
                "universe_id": report.universe_id,
                "memory_scope_id": report.memory_scope_id,
                "data_fingerprint": report.data_fingerprint,
                "candidate_source": report.candidate_source,
                "source_cycle_ids": list(report.source_cycle_ids),
                "cost_bps": report.cost_bps,
                "started_at": report.started_at.isoformat(),
                "finished_at": report.finished_at.isoformat(),
                "n_proposals": report.n_proposals,
                "n_promoted": report.n_promoted,
                "n_rejected": report.n_rejected,
                "promoted_factor_ids": list(report.promoted_factor_ids),
            }
        )
        self._base_dir.mkdir(parents=True, exist_ok=True)
        _rewrite_index(idx, rows)
