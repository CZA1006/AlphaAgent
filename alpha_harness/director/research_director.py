"""Research director layer for autonomous quant research.

The proposer/refinement loop already knows how to explore candidates for a
given theme.  This module sits one level above that loop: it chooses the next
research topic, records data gaps, and emits executable validation arguments.
"""

from __future__ import annotations

import json
import logging
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from alpha_harness.markets.models import MarketPack
from alpha_harness.markets.registry import load_market_pack

DEFAULT_VALIDATION_DIR = Path("artifacts/validations")
VALIDATION_INDEX_NAME = "_index.jsonl"

logger = logging.getLogger(__name__)


class DataGapSeverity(StrEnum):
    """Severity of a data gap from the director's point of view."""

    INFO = "info"
    WARNING = "warning"
    BLOCKING = "blocking"


class ResearchExecutorKind(StrEnum):
    """Deterministic execution path attached to a research topic."""

    PROPOSE = "propose"
    REPLAY_PROMOTED = "replay_promoted"
    EVENT_TRUTH_AUDIT = "event_truth_audit"
    RAW_TICK_MATERIALIZATION_PLAN = "raw_tick_materialization_plan"


class DatasetStatus(BaseModel):
    """Compact health snapshot for one dataset used by a research track."""

    name: str
    rows: int | None = None
    stocks: int | None = None
    aligned_to_daily: bool | None = None
    notes: str = ""


class DataGap(BaseModel):
    """A missing, suspicious, or incomplete data item the agent should plan around."""

    name: str
    severity: DataGapSeverity
    evidence: str
    recommended_action: str
    blocking: bool = False


class ResearchTopicPlan(BaseModel):
    """A candidate research topic plus the command needed to test it."""

    topic_id: str
    executor: ResearchExecutorKind = ResearchExecutorKind.PROPOSE
    theme: str
    priority: int
    rationale: str
    extra_guidance: str
    validation_command: str
    runner_module: str = "scripts.validate_strict"
    validation_args: list[str] = Field(default_factory=list)
    data_requirements: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    stop_conditions: list[str] = Field(default_factory=list)


class ResearchDirectorContext(BaseModel):
    """Inputs the director uses to choose topics and data work."""

    market: str
    dataset_status: list[DatasetStatus] = Field(default_factory=list)
    data_gaps: list[DataGap] = Field(default_factory=list)
    promoted_factor_count: int = 0
    rejected_factor_count: int = 0
    recent_validation_notes: list[str] = Field(default_factory=list)
    operator_constraints: list[str] = Field(default_factory=list)


class ResearchDirectorPlan(BaseModel):
    """Structured output consumed by humans, CLIs, or future agent loops."""

    market: str
    selected_topic_id: str
    topics: list[ResearchTopicPlan]
    data_gaps: list[DataGap]
    notes: str = ""

    @property
    def selected_topic(self) -> ResearchTopicPlan:
        """Return the topic selected for the next autonomous validation run."""
        for topic in self.topics:
            if topic.topic_id == self.selected_topic_id:
                return topic
        raise ValueError(f"selected topic not present: {self.selected_topic_id}")


def _validation_note(row: dict[str, Any]) -> str:
    cycle = str(row.get("cycle_id", "unknown"))
    promoted = int(row.get("n_promoted") or 0)
    rejected = int(row.get("n_rejected") or 0)
    return f"{cycle}: promoted={promoted}, rejected={rejected}"


def _validation_counts(validation_dir: Path) -> tuple[int, int, list[str]]:
    rows = _read_validation_index(validation_dir)
    promoted = 0
    rejected = 0
    notes: list[str] = []
    for row in rows[-5:]:
        promoted += int(row.get("n_promoted") or 0)
        rejected += int(row.get("n_rejected") or 0)
        notes.append(_validation_note(row))
    return promoted, rejected, notes


def _read_validation_index(validation_dir: Path) -> list[dict[str, Any]]:
    path = validation_dir / VALIDATION_INDEX_NAME
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError as exc:
                logger.warning(
                    "Skipping corrupt research-director validation index line %d in %s: %s",
                    line_no,
                    path,
                    exc,
                )
                continue
            if isinstance(payload, dict):
                rows.append(payload)
    return rows


def build_market_context(
    pack: MarketPack,
    *,
    validation_dir: Path | str = DEFAULT_VALIDATION_DIR,
) -> ResearchDirectorContext:
    """Combine pack-owned market context with recent validation history."""
    promoted, rejected, notes = _validation_counts(Path(validation_dir))
    return ResearchDirectorContext(
        market=pack.market_id,
        dataset_status=[
            DatasetStatus(
                name=item.name,
                rows=item.rows,
                stocks=item.stocks,
                aligned_to_daily=item.aligned_to_daily,
                notes=item.notes,
            )
            for item in pack.director_context.dataset_status
        ],
        data_gaps=[
            DataGap(
                name=item.name,
                severity=DataGapSeverity(item.severity),
                evidence=item.evidence,
                recommended_action=item.recommended_action,
                blocking=item.blocking,
            )
            for item in pack.director_context.data_gaps
        ],
        promoted_factor_count=promoted,
        rejected_factor_count=rejected,
        recent_validation_notes=notes,
        operator_constraints=list(pack.director_context.operator_constraints),
    )


class ResearchDirector:
    """Choose autonomous research topics and data follow-ups."""

    def plan(
        self,
        pack: MarketPack | ResearchDirectorContext,
        context: ResearchDirectorContext | None = None,
    ) -> ResearchDirectorPlan:
        if isinstance(pack, ResearchDirectorContext):
            context = pack
            try:
                pack = load_market_pack(context.market)
            except LookupError as exc:
                raise ValueError(f"unsupported research market: {context.market}") from exc
        if context is None:
            raise TypeError("context is required when a market pack is provided")
        if context.market != pack.market_id:
            raise ValueError(
                f"market context mismatch: pack={pack.market_id!r}, context={context.market!r}"
            )
        topics = self._topics(pack, context)
        if not topics:
            raise ValueError(f"market pack has no director topics: {pack.market_id}")
        topics.sort(key=lambda topic: topic.priority, reverse=True)
        selected = self._select_topic(topics, context)
        return ResearchDirectorPlan(
            market=context.market,
            selected_topic_id=selected.topic_id,
            topics=topics,
            data_gaps=context.data_gaps,
            notes=pack.director_context.plan_notes,
        )

    def _select_topic(
        self,
        topics: list[ResearchTopicPlan],
        context: ResearchDirectorContext,
    ) -> ResearchTopicPlan:
        blocking_gaps = {gap.name for gap in context.data_gaps if gap.blocking}
        for topic in topics:
            if not blocking_gaps.intersection(topic.data_requirements):
                return topic
        return topics[0]

    def _topics(
        self,
        pack: MarketPack,
        context: ResearchDirectorContext,
    ) -> list[ResearchTopicPlan]:
        has_history = bool(context.promoted_factor_count + context.rejected_factor_count)
        return [
            ResearchTopicPlan(
                topic_id=item.topic_id,
                executor=ResearchExecutorKind(item.executor),
                theme=item.theme,
                priority=item.priority - (item.history_penalty if has_history else 0),
                rationale=item.rationale,
                extra_guidance=item.extra_guidance,
                validation_command=item.validation_command,
                runner_module=item.runner_module,
                validation_args=list(item.validation_args),
                data_requirements=list(item.data_requirements),
                success_criteria=list(item.success_criteria),
                stop_conditions=list(item.stop_conditions),
            )
            for item in pack.director_topics
        ]
