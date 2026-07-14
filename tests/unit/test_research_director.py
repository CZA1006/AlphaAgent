from __future__ import annotations

import json

import pytest

from alpha_harness.director import (
    ResearchDirector,
    ResearchDirectorContext,
    ResearchExecutorKind,
    build_hk_ipo_context,
)


def test_hk_ipo_context_reads_recent_validation_counts(tmp_path) -> None:
    index = tmp_path / "_index.jsonl"
    rows = [
        {"cycle_id": "old-1", "n_promoted": 1, "n_rejected": 4},
        {"cycle_id": "old-2", "n_promoted": 2, "n_rejected": 3},
    ]
    index.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    context = build_hk_ipo_context(validation_dir=tmp_path)

    assert context.promoted_factor_count == 3
    assert context.rejected_factor_count == 7
    assert context.recent_validation_notes == [
        "old-1: promoted=1, rejected=4",
        "old-2: promoted=2, rejected=3",
    ]
    event_dates = next(ds for ds in context.dataset_status if ds.name == "ipo_event_dates_curated")
    document_registry = next(
        ds for ds in context.dataset_status if ds.name == "hkex_document_registry_curated"
    )
    review_gap = next(gap for gap in context.data_gaps if gap.name == "event_terms_needs_review")
    assert event_dates.rows == 593
    assert document_registry.stocks == 77
    assert "280 rows" in review_gap.evidence


def test_hk_ipo_director_selects_event_microstructure_topic(tmp_path) -> None:
    context = build_hk_ipo_context(validation_dir=tmp_path)
    plan = ResearchDirector().plan(context)

    assert plan.selected_topic_id == "hk_ipo_event_conditioned_microstructure"
    assert plan.selected_topic.validation_command == (
        'make validate-hk-ipo-events ARGS="--llm openrouter --n-candidates 12 --n-cycles 3"'
    )
    assert "--data-source" in plan.selected_topic.validation_args
    assert "bigquery" in plan.selected_topic.validation_args
    assert "ipo_event_features_daily" in plan.selected_topic.data_requirements
    assert [topic.priority for topic in plan.topics] == sorted(
        [topic.priority for topic in plan.topics],
        reverse=True,
    )
    cost_topic = next(topic for topic in plan.topics if topic.topic_id == "hk_ipo_cost_realism_oos")
    assert cost_topic.executor is ResearchExecutorKind.REPLAY_PROMOTED
    assert cost_topic.validation_args[cost_topic.validation_args.index("--cost-bps") + 1] == "15.0"
    event_review = next(
        topic for topic in plan.topics if topic.topic_id == "hk_ipo_event_truth_review"
    )
    assert event_review.executor is ResearchExecutorKind.EVENT_TRUTH_AUDIT
    assert event_review.validation_args == []
    assert "scripts.audit_hk_ipo_event_truth" in event_review.validation_command
    raw_tick = next(
        topic for topic in plan.topics if topic.topic_id == "hk_ipo_raw_tick_intraday_features"
    )
    assert raw_tick.executor is ResearchExecutorKind.RAW_TICK_MATERIALIZATION_PLAN
    assert raw_tick.validation_args == []
    assert "scripts.plan_hk_ipo_raw_tick_materialization" in raw_tick.validation_command


def test_hk_ipo_director_surfaces_known_data_gaps(tmp_path) -> None:
    plan = ResearchDirector().plan(build_hk_ipo_context(validation_dir=tmp_path))
    gap_names = {gap.name for gap in plan.data_gaps}

    assert "nonpositive_tick_values" in gap_names
    assert "event_terms_needs_review" in gap_names
    assert "prospectus_allotment_coverage" not in gap_names
    assert "bloomberg_lockup_anomalies" in gap_names
    assert "raw_tick_intraday_loop_gap" in gap_names


def test_director_rejects_unknown_market() -> None:
    with pytest.raises(ValueError, match="unsupported research market"):
        ResearchDirector().plan(ResearchDirectorContext(market="crypto"))
