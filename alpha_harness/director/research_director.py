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


def _hk_ipo_validation_args(
    *,
    theme: str,
    extra_guidance: str,
    cost_bps: float | None = None,
) -> list[str]:
    args = [
        "--data-source",
        "bigquery",
        "--universe",
        "configs/universes/hk_ipo.txt",
        "--start-date",
        "2025-12-12",
        "--end-date",
        "2026-06-26",
        "--regime",
        "lenient",
        "--llm",
        "openrouter",
        "--mock-preset",
        "hk_ipo_events",
        "--n-candidates",
        "12",
        "--n-cycles",
        "3",
        "--theme",
        theme,
        "--extra-guidance",
        extra_guidance,
    ]
    if cost_bps is not None:
        args.extend(["--cost-bps", str(cost_bps)])
    return args


def _hk_ipo_make_command(args: list[str]) -> str:
    # Keep the human-facing command on the Makefile target so the baseline
    # BigQuery/universe/date contract stays centralized there.
    overrides = [
        "--llm openrouter",
        "--n-candidates 12",
        "--n-cycles 3",
    ]
    return f'make validate-hk-ipo-events ARGS="{" ".join(overrides)}"'


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


def build_hk_ipo_context(
    *,
    validation_dir: Path | str = DEFAULT_VALIDATION_DIR,
) -> ResearchDirectorContext:
    """Build the current HK IPO research context from committed contracts.

    The doctor scripts remain the live source of truth.  This context captures
    the last reviewed GCP snapshot so the director can run offline and still
    produce a useful next-step plan.
    """
    promoted, rejected, notes = _validation_counts(Path(validation_dir))
    return ResearchDirectorContext(
        market="hk_ipo",
        dataset_status=[
            DatasetStatus(
                name="ipo_daily_prices",
                rows=7118,
                stocks=77,
                aligned_to_daily=True,
                notes="Daily HK IPO panel used as the evaluation spine.",
            ),
            DatasetStatus(
                name="micro_features_daily",
                rows=7118,
                stocks=77,
                aligned_to_daily=True,
                notes="Daily tick-derived OFI, spread, volatility, and liquidity features.",
            ),
            DatasetStatus(
                name="tick_manifest_target",
                rows=86103563,
                stocks=77,
                aligned_to_daily=None,
                notes="TRADE/BID/ASK tick manifest; no zero trade/bid/ask stocks in latest doctor.",
            ),
            DatasetStatus(
                name="ipo_event_features_daily",
                rows=7118,
                stocks=77,
                aligned_to_daily=True,
                notes="Daily event distances and flags aligned to ipo_daily_prices.",
            ),
            DatasetStatus(
                name="ipo_event_dates_curated",
                rows=593,
                stocks=75,
                aligned_to_daily=None,
                notes="HKEX/prospectus-derived event dates with source evidence.",
            ),
            DatasetStatus(
                name="hkex_document_registry_curated",
                rows=846,
                stocks=77,
                aligned_to_daily=None,
                notes=(
                    "Prospectus and allotment-results announcement coverage is 77/77 "
                    "in the 2026-07-14 event-truth audit."
                ),
            ),
        ],
        data_gaps=[
            DataGap(
                name="nonpositive_tick_values",
                severity=DataGapSeverity.WARNING,
                evidence=(
                    "Latest doctor found 364,768 nonpositive tick value rows across 77 stocks."
                ),
                recommended_action=(
                    "Keep value > 0 filters in derived SQL; add a source-level QA extract by "
                    "stock/date/event_type before building intraday features."
                ),
            ),
            DataGap(
                name="event_terms_needs_review",
                severity=DataGapSeverity.WARNING,
                evidence="ipo_event_terms_needs_review has 280 rows across 75 stocks.",
                recommended_action=(
                    "Prioritize rows that affect greenshoe, stabilization, cornerstone, "
                    "lockup, and tranche unlock dates; promote only source-backed terms."
                ),
            ),
            DataGap(
                name="bloomberg_lockup_anomalies",
                severity=DataGapSeverity.WARNING,
                evidence=(
                    "Bloomberg still reports anomalous lockup fields for 06051 and 03636; "
                    "HKEX/prospectus terms should be treated as truth."
                ),
                recommended_action=(
                    "Use Bloomberg lockup fields only as review candidates unless confirmed "
                    "by HKEX/prospectus source text."
                ),
            ),
            DataGap(
                name="raw_tick_intraday_loop_gap",
                severity=DataGapSeverity.INFO,
                evidence=(
                    "The validation loop consumes daily bars plus tick-derived daily features; "
                    "raw tick loading exists but the agent does not yet generate "
                    "intraday features itself."
                ),
                recommended_action=(
                    "Add an intraday feature-build step that materializes "
                    "opening-auction, first-hour, "
                    "and event-window features from TRADE/BID/ASK ticks."
                ),
            ),
        ],
        promoted_factor_count=promoted,
        rejected_factor_count=rejected,
        recent_validation_notes=notes,
        operator_constraints=[
            "Use /Users/caizhuoang/AlphaAgent as the official working tree.",
            "Prefer HKEX/prospectus-derived event dates over Bloomberg lockup fields.",
            "Do not treat promoted lenient-regime factors as production alpha "
            "without stricter follow-up.",
        ],
    )


class ResearchDirector:
    """Choose autonomous research topics and data follow-ups."""

    def plan(self, context: ResearchDirectorContext) -> ResearchDirectorPlan:
        if context.market != "hk_ipo":
            raise ValueError(f"unsupported research market: {context.market}")
        return self._plan_hk_ipo(context)

    def _plan_hk_ipo(self, context: ResearchDirectorContext) -> ResearchDirectorPlan:
        topics = self._hk_ipo_topics(context)
        topics.sort(key=lambda topic: topic.priority, reverse=True)
        selected = self._select_topic(topics, context)
        notes = (
            "Director selected an event-conditioned microstructure run because the core "
            "daily, microstructure, tick-manifest, and curated-event datasets are aligned. "
            "Data-review topics remain in the queue because they affect truth labeling and "
            "future intraday feature work."
        )
        return ResearchDirectorPlan(
            market=context.market,
            selected_topic_id=selected.topic_id,
            topics=topics,
            data_gaps=context.data_gaps,
            notes=notes,
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

    def _hk_ipo_topics(
        self,
        context: ResearchDirectorContext,
    ) -> list[ResearchTopicPlan]:
        event_theme = "HK IPO continuous event-decay microstructure signals"
        event_guidance = (
            "The previous hard-window search produced 18/18 rejections because Boolean "
            "event gates collapsed the cross-section. Every proposal must retain a daily "
            "base microstructure signal and add a continuous interaction using "
            "event_decay(days_to_next_*, half_life), with half_life in {5, 10, 20}. "
            "Use OFI, relative spread, realized volatility, trade count, quote count, or "
            "average trade size. Do not multiply by is_pre_*, is_near_*, or "
            "is_stabilization_window_active as the sole signal. Do not use first_hour_* "
            "fields because this loader is daily-only. Avoid Bloomberg-only anomalous "
            "lockup dates. Prefer forms such as base_rank + event_decay(distance, 10) * "
            "interaction_rank so non-event stocks remain in the cross-section."
        )
        event_args = _hk_ipo_validation_args(theme=event_theme, extra_guidance=event_guidance)

        tick_theme = "HK IPO raw-tick intraday pressure features"
        tick_guidance = (
            "Design feature families that should be materialized from TRADE/BID/ASK ticks: "
            "first-hour OFI, opening-auction imbalance, quote-recovery speed, spread shock "
            "persistence, and event-window liquidity withdrawal."
        )
        cost_theme = "HK IPO implementability and cost realism"
        cost_guidance = (
            "Stress previously promoted event-conditioned factors for turnover, spread, "
            "liquidity, holdout decay, and post-cost quantile spread. Prefer lower-turnover "
            "variants that remain stable outside the first listing week."
        )
        cost_args = _hk_ipo_validation_args(
            theme=cost_theme,
            extra_guidance=cost_guidance,
            cost_bps=15.0,
        )

        data_review_theme = "HK IPO event truth and document review"
        data_review_guidance = (
            "Use the data gaps to decide which HKEX/prospectus terms to review next. "
            "Prioritize missing or ambiguous greenshoe, stabilization, cornerstone, and "
            "lockup terms that affect the validation window."
        )
        history_penalty = 5 if context.promoted_factor_count + context.rejected_factor_count else 0
        return [
            ResearchTopicPlan(
                topic_id="hk_ipo_event_conditioned_microstructure",
                theme=event_theme,
                priority=100 - history_penalty,
                rationale=(
                    "The aligned daily, microstructure, and curated event tables can already "
                    "support continuous event-distance interactions without collapsing "
                    "the daily cross-section to a narrow Boolean event window."
                ),
                extra_guidance=event_guidance,
                validation_command=_hk_ipo_make_command(event_args),
                validation_args=event_args,
                data_requirements=[
                    "ipo_daily_prices",
                    "micro_features_daily",
                    "ipo_event_features_daily",
                    "ipo_event_dates_curated",
                ],
                success_criteria=[
                    "At least one promoted factor survives lenient validation and holdout checks.",
                    "Promoted factors contain a continuous event-decay interaction while "
                    "retaining a non-event daily base signal.",
                    "Failure breakdown identifies whether the next bottleneck is IC, "
                    "rank IC, holdout, or turnover.",
                ],
                stop_conditions=[
                    "Stop repeating this topic if promoted factors are generic or "
                    "fail holdout repeatedly.",
                    "Switch to event data review if new failures trace to missing or "
                    "ambiguous event labels.",
                ],
            ),
            ResearchTopicPlan(
                topic_id="hk_ipo_event_truth_review",
                executor=ResearchExecutorKind.EVENT_TRUTH_AUDIT,
                theme=data_review_theme,
                priority=85,
                rationale=(
                    "The event tables are usable and document coverage is complete, but 280 "
                    "needs-review rows still require a deterministic backlog audit."
                ),
                extra_guidance=data_review_guidance,
                validation_command=(
                    "uv run --extra gcp python -m scripts.audit_hk_ipo_event_truth"
                ),
                validation_args=[],
                data_requirements=[
                    "event_terms_needs_review",
                    "bloomberg_lockup_anomalies",
                ],
                success_criteria=[
                    "Needs-review rows that affect validation are resolved or marked unavailable.",
                    "Bloomberg anomalies remain excluded from truth tables unless "
                    "source-confirmed.",
                ],
                stop_conditions=[
                    "Pause factor validation only if event truth gaps become blocking "
                    "for selected dates.",
                ],
            ),
            ResearchTopicPlan(
                topic_id="hk_ipo_cost_realism_oos",
                executor=ResearchExecutorKind.REPLAY_PROMOTED,
                theme=cost_theme,
                priority=75,
                rationale=(
                    "Early lenient promotions are useful for exploration, but production-quality "
                    "research needs post-cost, turnover, and holdout stress before any alpha claim."
                ),
                extra_guidance=cost_guidance,
                validation_command=_hk_ipo_make_command(cost_args),
                validation_args=cost_args,
                data_requirements=[
                    "ipo_daily_prices",
                    "micro_features_daily",
                    "ipo_event_features_daily",
                ],
                success_criteria=[
                    "Promoted candidates retain positive post-cost quantile spread.",
                    "Holdout rank IC does not flip sign.",
                    "Turnover is explainable by event timing rather than noisy daily churn.",
                ],
                stop_conditions=[
                    "If all candidates fail cost or holdout gates, return to feature engineering.",
                ],
            ),
            ResearchTopicPlan(
                topic_id="hk_ipo_raw_tick_intraday_features",
                executor=ResearchExecutorKind.RAW_TICK_MATERIALIZATION_PLAN,
                theme=tick_theme,
                priority=65,
                rationale=(
                    "Tick-level TRADE/BID/ASK data is available, but the current validation "
                    "loop mainly consumes daily materialized features. Intraday feature "
                    "materialization is the next data-extension topic."
                ),
                extra_guidance=tick_guidance,
                validation_command=(
                    "uv run --extra gcp python -m scripts.plan_hk_ipo_raw_tick_materialization"
                ),
                validation_args=[],
                data_requirements=[
                    "tick_manifest_target",
                    "raw_tick_intraday_loop_gap",
                    "nonpositive_tick_values",
                ],
                success_criteria=[
                    "A reproducible SQL/materialization plan exists for first-hour "
                    "and event-window features.",
                    "Nonpositive tick rows are quantified by symbol/date/event_type "
                    "before feature build.",
                ],
                stop_conditions=[
                    "Do not use raw tick-derived intraday signals in validation until "
                    "QA tables pass doctor checks.",
                ],
            ),
        ]
