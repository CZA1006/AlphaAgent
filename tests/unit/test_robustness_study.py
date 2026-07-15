"""Unit tests for the multi-window robustness study orchestrator."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from scripts.robustness_study import (
    LLMSpec,
    generate_splits,
    parse_llm_specs,
    sign_test,
    summarise,
)

# ── Split generation ─────────────────────────────────────────────────────────


def test_splits_are_rolling_and_fit_history() -> None:
    splits = generate_splits(
        date(2024, 7, 1),
        date(2026, 6, 30),
        y1_months=12,
        y2_months=6,
        step_months=3,
        gap_days=7,
    )
    assert len(splits) == 2
    first = splits[0]
    assert first.y1_start == date(2024, 7, 1)
    assert first.y1_end == date(2025, 7, 1)
    assert first.y2_start == first.y1_end + timedelta(days=7)
    assert all(s.y2_end <= date(2026, 6, 30) for s in splits)
    # rolling: consecutive Y1 starts are step_months apart
    assert splits[1].y1_start == date(2024, 10, 1)


def test_y1_never_overlaps_y2_within_a_split() -> None:
    for s in generate_splits(
        date(2024, 1, 1),
        date(2026, 12, 31),
        y1_months=9,
        y2_months=6,
        step_months=3,
        gap_days=7,
    ):
        assert s.y2_start > s.y1_end


def test_no_splits_when_history_too_short() -> None:
    assert (
        generate_splits(
            date(2025, 1, 1),
            date(2025, 12, 31),
            y1_months=12,
            y2_months=6,
            step_months=3,
            gap_days=7,
        )
        == []
    )


def test_month_end_dates_do_not_raise() -> None:
    splits = generate_splits(
        date(2024, 1, 31),
        date(2026, 12, 31),
        y1_months=1,
        y2_months=1,
        step_months=1,
        gap_days=0,
    )
    assert splits  # Jan 31 + 1 month clamps to Feb 29 (2024 is a leap year)
    assert splits[0].y1_end == date(2024, 2, 29)


def test_invalid_parameters_raise() -> None:
    with pytest.raises(ValueError):
        generate_splits(
            date(2024, 1, 1), date(2026, 1, 1), y1_months=0, y2_months=6, step_months=3, gap_days=7
        )


# ── LLM specs ────────────────────────────────────────────────────────────────


def test_parse_llm_specs() -> None:
    specs = parse_llm_specs("mock,openrouter:deepseek/deepseek-chat-v3.1")
    assert specs == [
        LLMSpec("mock", None),
        LLMSpec("openrouter", "deepseek/deepseek-chat-v3.1"),
    ]
    assert specs[1].label == "openrouter:deepseek/deepseek-chat-v3.1"


def test_parse_llm_specs_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        parse_llm_specs("gpt4-direct")


# ── Statistics ───────────────────────────────────────────────────────────────


def test_sign_test_balanced_is_one() -> None:
    pos, n, p = sign_test([0.02, -0.02, 0.01, -0.01])
    assert (pos, n) == (2, 4)
    assert p == 1.0


def test_sign_test_all_positive_small_n_is_not_significant() -> None:
    # 4/4 positive: p = 2 * (1/16) = 0.125 — the study must not claim
    # significance from a handful of windows.
    _, _, p = sign_test([0.01, 0.02, 0.03, 0.04])
    assert p == pytest.approx(0.125)


# ── Tally ────────────────────────────────────────────────────────────────────


def _cell(llm: str, sel: str, status: str, ric: float | None = None, strict: bool = False) -> dict:
    c = {"split": "s", "llm": llm, "selection": sel, "status": status}
    if status == "executed":
        c.update(y2_rank_ic=ric, passes_strict=strict)
    return c


def test_summarise_counts_by_arm_and_keeps_failures_in_denominator() -> None:
    cells = [
        _cell("mock", "input_order", "executed", +0.02, strict=True),
        _cell("mock", "input_order", "executed", -0.01),
        _cell("mock", "input_order", "no_basket"),
        _cell("mock", "persistence", "executed", +0.03),
        _cell("mock", "persistence", "failed"),
    ]
    s = summarise(cells)
    pooled = s["pooled"]
    assert pooled["cells_total"] == 5
    assert pooled["cells_executed"] == 3
    assert pooled["cells_no_basket"] == 1
    assert pooled["cells_failed"] == 1
    assert pooled["y2_rank_ic_positive"] == 2
    assert pooled["strict_clears"] == 1
    arm = s["arms"]["mock|input_order"]
    assert arm["cells_total"] == 3
    assert arm["y2_rank_ic_n"] == 2
