from __future__ import annotations

import math

import pandas as pd

from scripts.analysis.hk_ipo_ofi_attribution import (
    _event_proximity_bucket,
    _listing_age_bucket,
)


def test_listing_age_buckets_are_predeclared() -> None:
    assert _listing_age_bucket(math.nan) == "unknown"
    assert _listing_age_bucket(30) == "0_30"
    assert _listing_age_bucket(31) == "31_90"
    assert _listing_age_bucket(90) == "31_90"
    assert _listing_age_bucket(91) == "91_plus"


def test_event_proximity_uses_nearest_available_event() -> None:
    row = pd.Series(
        {
            "days_to_next_cornerstone_lockup": 40.0,
            "days_since_prev_greenshoe_expiry": 4.0,
        },
    )
    assert _event_proximity_bucket(row) == "0_5"
    assert _event_proximity_bucket(pd.Series({"days_to_next_cornerstone_lockup": 12.0})) == "6_30"
    assert (
        _event_proximity_bucket(pd.Series({"days_to_next_cornerstone_lockup": 45.0}))
        == "31_plus"
    )
    assert _event_proximity_bucket(pd.Series(dtype=float)) == "no_event"
