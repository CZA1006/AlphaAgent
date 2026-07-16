"""Unit tests for :mod:`alpha_harness.data.rate_limit`."""

from __future__ import annotations

import httpx
import pytest

from alpha_harness.data.rate_limit import (
    RateLimiter,
    polygon_rate_limiter_from_env,
    request_with_retry,
)

# ── RateLimiter ─────────────────────────────────────────────────────────────


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_rate_limiter_does_not_sleep_under_cap() -> None:
    clock = FakeClock()
    rl = RateLimiter(
        max_requests=5,
        window_seconds=60.0,
        clock=clock.time,
        sleep=clock.sleep,
    )
    for _ in range(5):
        rl.acquire()
    assert clock.slept == []


def test_rate_limiter_sleeps_when_over_cap() -> None:
    clock = FakeClock()
    rl = RateLimiter(
        max_requests=2,
        window_seconds=60.0,
        clock=clock.time,
        sleep=clock.sleep,
    )
    rl.acquire()  # t=0
    clock.now = 10.0
    rl.acquire()  # t=10 — 2/2
    rl.acquire()  # should sleep until t=60 (oldest + 60)

    assert len(clock.slept) == 1
    assert clock.slept[0] == pytest.approx(50.0)


def test_rate_limiter_rejects_bad_config() -> None:
    with pytest.raises(ValueError):
        RateLimiter(max_requests=0)
    with pytest.raises(ValueError):
        RateLimiter(max_requests=1, window_seconds=0)


def test_polygon_rate_limiter_default_is_five(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POLYGON_RPM", raising=False)
    rl = polygon_rate_limiter_from_env()
    assert rl.max_requests == 5


def test_polygon_rate_limiter_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLYGON_RPM", "20")
    rl = polygon_rate_limiter_from_env()
    assert rl.max_requests == 20


def test_polygon_rate_limiter_bad_env_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POLYGON_RPM", "not-a-number")
    rl = polygon_rate_limiter_from_env()
    assert rl.max_requests == 5


# ── 429 retry helper ────────────────────────────────────────────────────────


def _mock_client(
    responses: list[httpx.Response],
) -> httpx.Client:
    """Build a Client that returns the given responses in order."""
    it = iter(responses)

    def handler(_request: httpx.Request) -> httpx.Response:
        return next(it)

    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport)


def test_request_with_retry_succeeds_on_first_try() -> None:
    client = _mock_client([httpx.Response(200, json={"ok": True})])
    sleeps: list[float] = []

    resp = request_with_retry(
        client,
        url="https://api.polygon.io/test",
        params={"apiKey": "x"},
        sleep=sleeps.append,
    )
    assert resp.status_code == 200
    assert sleeps == []


def test_request_with_retry_recovers_after_429() -> None:
    client = _mock_client(
        [
            httpx.Response(429, headers={"Retry-After": "1"}),
            httpx.Response(429),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    sleeps: list[float] = []

    resp = request_with_retry(
        client,
        url="https://api.polygon.io/test",
        params=None,
        max_retries=4,
        backoff_base_seconds=2.0,
        sleep=sleeps.append,
    )

    assert resp.status_code == 200
    # First retry honors Retry-After (1s); second falls back to exponential (base*2).
    assert sleeps == [1.0, 4.0]


def test_request_with_retry_returns_last_429_when_exhausted() -> None:
    client = _mock_client(
        [
            httpx.Response(429),
            httpx.Response(429),
        ]
    )
    sleeps: list[float] = []

    resp = request_with_retry(
        client,
        url="https://api.polygon.io/test",
        params=None,
        max_retries=1,
        backoff_base_seconds=1.0,
        sleep=sleeps.append,
    )

    assert resp.status_code == 429
    # One retry gap -> one sleep.
    assert sleeps == [1.0]


def test_request_with_retry_respects_rate_limiter() -> None:
    # Rate limiter pacing is separate from 429 handling; a successful request
    # with a rate limiter attached should still call acquire().
    client = _mock_client([httpx.Response(200)])

    clock = FakeClock()
    rl = RateLimiter(
        max_requests=1,
        window_seconds=60.0,
        clock=clock.time,
        sleep=clock.sleep,
    )

    request_with_retry(
        client,
        url="https://api.polygon.io/test",
        params=None,
        rate_limiter=rl,
        sleep=lambda _s: None,
    )
    # One request in the limiter's window.
    assert len(rl._timestamps) == 1
