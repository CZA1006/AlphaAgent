"""Regression tests for the event-study sanity guards.

Curated HKEX extraction produced event dates *before* listing (e.g. 03378's
cornerstone expiry, 02706/01989/01609's greenshoe expiry).  The study script
snaps an event to the first trading day at/after the event date, so a
pre-listing date lands on the IPO's first trading day and injects the day-1
pop/crash into tau=0.  These tests pin the guard that drops such events.
"""

from __future__ import annotations

import pandas as pd

from scripts.analysis.lockup_event_study import (
    MIN_DAYS_FROM_LISTING,
    _drop_implausible_events,
    _sign_test,
)


def _events(rows: list[tuple[str, str, str]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["stock_code", "event_date", "listing_date"])


def test_drops_event_dated_before_listing() -> None:
    events = _events(
        [
            ("03378", "2025-12-15", "2025-12-23"),  # pre-listing: impossible
            ("00001", "2026-06-15", "2025-12-23"),  # ~6 months out: fine
        ]
    )
    kept = _drop_implausible_events(events, "cornerstone_lockup_expiry")
    assert list(kept["stock_code"]) == ["00001"]


def test_day30_event_types_enforce_20_day_floor() -> None:
    assert MIN_DAYS_FROM_LISTING["greenshoe_expiry"] == 20
    assert MIN_DAYS_FROM_LISTING["stabilization_end"] == 20
    events = _events(
        [
            ("00068", "2026-04-20", "2026-04-17"),  # day 3: implausible "end"
            ("00002", "2026-05-17", "2026-04-17"),  # day 30: the normal case
        ]
    )
    kept = _drop_implausible_events(events, "stabilization_end")
    assert list(kept["stock_code"]) == ["00002"]


def test_missing_listing_date_is_kept() -> None:
    events = _events([("00003", "2026-05-17", None)])
    kept = _drop_implausible_events(events, "greenshoe_expiry")
    assert len(kept) == 1


def test_sign_test_balanced_sample_is_insignificant() -> None:
    pos, n, p = _sign_test(pd.Series([1.0, -1.0, 2.0, -2.0, 3.0, -3.0]))
    assert (pos, n) == (3, 6)
    assert p == 1.0


def test_sign_test_one_sided_sample_is_significant() -> None:
    pos, n, p = _sign_test(pd.Series([1.0] * 10))
    assert (pos, n) == (10, 10)
    assert p < 0.01


def test_sign_test_robust_to_fat_tail() -> None:
    # One +244% outlier cannot rescue a coin-flip sample — unlike the mean.
    x = pd.Series([-1.0, -0.5, -0.2, 0.1, 0.2, 244.0])
    _, _, p = _sign_test(x)
    assert p == 1.0
