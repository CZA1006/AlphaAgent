"""Rate-limit + retry helpers for HTTP market-data loaders.

Scope
-----
Deliberately minimal — a single-process sliding-window rate limiter and
an exponential-backoff helper for 429 responses.  This is not a generic
scheduler; it exists to make :class:`PolygonEquitiesLoader` safe under
free-tier constraints (~5 requests / minute).

Determinism
-----------
Both the rate limiter and the backoff helper accept injectable
``clock`` and ``sleep`` callables so tests can exercise their behavior
without actually waiting.
"""

from __future__ import annotations

import logging
import os
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

import httpx

# httpx's get(params=) accepts a mapping of primitive values.
ParamValue = str | int | float | bool | None
ParamsMapping = Mapping[str, ParamValue]

logger = logging.getLogger(__name__)


DEFAULT_POLYGON_RPM = 5
DEFAULT_MAX_RETRIES = 4
DEFAULT_BACKOFF_BASE_SECONDS = 2.0


# ── Rate limiter ────────────────────────────────────────────────────────────


@dataclass
class RateLimiter:
    """Sliding-window rate limiter (no more than ``max_requests`` per window).

    Parameters
    ----------
    max_requests:
        Ceiling per rolling window.  Must be ``>= 1``.
    window_seconds:
        Rolling window length in seconds.  Default 60s (minute rate).
    clock, sleep:
        Injectable for testing.  Default to ``time.monotonic`` and
        ``time.sleep``.
    """

    max_requests: int
    window_seconds: float = 60.0
    clock: Callable[[], float] = field(default=time.monotonic)
    sleep: Callable[[float], None] = field(default=time.sleep)
    _timestamps: deque[float] = field(default_factory=deque, init=False)

    def __post_init__(self) -> None:
        if self.max_requests < 1:
            raise ValueError(
                f"max_requests must be >= 1, got {self.max_requests}"
            )
        if self.window_seconds <= 0:
            raise ValueError(
                f"window_seconds must be > 0, got {self.window_seconds}"
            )

    def acquire(self) -> None:
        """Block until issuing a request would not violate the rate limit."""
        now = self.clock()
        cutoff = now - self.window_seconds
        while self._timestamps and self._timestamps[0] <= cutoff:
            self._timestamps.popleft()

        if len(self._timestamps) >= self.max_requests:
            wait = self._timestamps[0] + self.window_seconds - now
            if wait > 0:
                logger.info(
                    "RateLimiter: %d/%d in window, sleeping %.2fs",
                    len(self._timestamps),
                    self.max_requests,
                    wait,
                )
                self.sleep(wait)
            # Re-evict anything that aged out during the sleep.
            now = self.clock()
            cutoff = now - self.window_seconds
            while self._timestamps and self._timestamps[0] <= cutoff:
                self._timestamps.popleft()

        self._timestamps.append(self.clock())


def polygon_rate_limiter_from_env() -> RateLimiter:
    """Build a rate limiter honoring ``POLYGON_RPM`` (default: 5)."""
    raw = os.environ.get("POLYGON_RPM", "").strip()
    try:
        rpm = int(raw) if raw else DEFAULT_POLYGON_RPM
    except ValueError:
        logger.warning(
            "POLYGON_RPM=%r is not an integer; falling back to %d rpm.",
            raw,
            DEFAULT_POLYGON_RPM,
        )
        rpm = DEFAULT_POLYGON_RPM
    return RateLimiter(max_requests=max(1, rpm))


# ── 429 retry ──────────────────────────────────────────────────────────────


def request_with_retry(
    client: httpx.Client,
    *,
    url: str,
    params: ParamsMapping | None,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    rate_limiter: RateLimiter | None = None,
) -> httpx.Response:
    """GET ``url`` with exponential backoff on HTTP 429.

    If ``rate_limiter`` is provided, it is acquired before every attempt
    (including retries) so the pacing contract is preserved even when
    the server asks us to slow down.

    Parameters
    ----------
    max_retries:
        Number of *additional* attempts after the first one.  Total calls
        is at most ``max_retries + 1``.
    backoff_base_seconds:
        Base of the exponential schedule — attempt ``n`` waits
        ``base * 2**n`` seconds.  If the server sends ``Retry-After``, we
        honor that instead (clamped to ``base * 2**max_retries``).
    """
    if max_retries < 0:
        raise ValueError(f"max_retries must be >= 0, got {max_retries}")

    last_429: httpx.Response | None = None
    max_wait = backoff_base_seconds * (2 ** max_retries)

    for attempt in range(max_retries + 1):
        if rate_limiter is not None:
            rate_limiter.acquire()

        response = client.get(url, params=dict(params) if params else None)
        if response.status_code != 429:
            return response

        last_429 = response
        if attempt == max_retries:
            break

        wait = _compute_backoff(
            attempt=attempt,
            base=backoff_base_seconds,
            retry_after_header=response.headers.get("Retry-After"),
            max_wait=max_wait,
        )
        logger.warning(
            "HTTP 429 on %s (attempt %d/%d); sleeping %.2fs before retry",
            url,
            attempt + 1,
            max_retries + 1,
            wait,
        )
        sleep(wait)

    # Exhausted retries — return the last 429 so the caller decides.
    assert last_429 is not None
    return last_429


def _compute_backoff(
    *,
    attempt: int,
    base: float,
    retry_after_header: str | None,
    max_wait: float,
) -> float:
    if retry_after_header:
        try:
            return float(min(max_wait, float(retry_after_header)))
        except ValueError:
            pass  # fall through to exponential
    return float(min(max_wait, base * (2 ** attempt)))
