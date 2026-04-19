"""Polygon.io equities loader — fetches historical OHLCV bars via REST API.

Implements the EquitiesLoader protocol using Polygon.io's ``/v2/aggs/ticker``
endpoint for historical daily bars via httpx.

Environment variables
---------------------
POLYGON_API_KEY : str
    Required. Obtain from https://polygon.io/dashboard/keys

Adjustment handling
-------------------
Polygon returns adjusted prices by default (``adjusted=true``).  When
``AdjustmentType.RAW`` is requested the loader sets ``adjusted=false``.
Split-only adjustment is not natively supported by Polygon; the loader
falls back to split-and-dividend in that case and logs a warning.

Point-in-time / survivorship notes
-----------------------------------
- Polygon serves data for delisted tickers if they existed during the
  requested date range, satisfying the survivorship-bias rule.
- Polygon's adjustment is applied retroactively to the full history on
  each corporate action.  For strict PIT research, store raw bars and
  apply adjustments using a separate corporate-actions table.
- This loader does NOT solve PIT adjustment — it documents the caveat
  and provides raw-price access for callers that need it.

Persistence
-----------
This loader returns a DataFrame in memory.  Use ``ParquetStore`` (in
``parquet_store.py``) to persist the result to local Parquet files for
subsequent reads via ``LocalEquitiesLoader``.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

import httpx
import pandas as pd

from alpha_harness.data.models import (
    AdjustmentType,
    BarFrequency,
    DataRequest,
    DataResult,
)
from alpha_harness.data.rate_limit import (
    RateLimiter,
    polygon_rate_limiter_from_env,
    request_with_retry,
)

logger = logging.getLogger(__name__)

_POLYGON_BASE_URL = "https://api.polygon.io"

# Map our BarFrequency to Polygon's multiplier/timespan pair
_FREQUENCY_MAP: dict[BarFrequency, tuple[int, str]] = {
    BarFrequency.DAILY: (1, "day"),
    BarFrequency.HOURLY: (1, "hour"),
    BarFrequency.MINUTE_1: (1, "minute"),
    BarFrequency.MINUTE_5: (5, "minute"),
    BarFrequency.MINUTE_15: (15, "minute"),
}


class PolygonEquitiesLoader:
    """Fetch US equity OHLCV bars from Polygon.io.

    Conforms to the ``EquitiesLoader`` protocol.

    Parameters
    ----------
    api_key:
        Polygon API key.  If ``None``, reads from ``POLYGON_API_KEY`` env var.
    base_url:
        Override for testing (e.g. point at a mock server).
    client:
        Optional pre-configured ``httpx.Client``.  If ``None``, a new client
        is created per ``load_bars`` call.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = _POLYGON_BASE_URL,
        client: httpx.Client | None = None,
        rate_limiter: RateLimiter | None = None,
        max_retries: int = 4,
    ) -> None:
        self._api_key = api_key or os.environ.get("POLYGON_API_KEY", "")
        if not self._api_key:
            logger.warning(
                "POLYGON_API_KEY not set — Polygon requests will fail. "
                "Set the env var or pass api_key= to the constructor."
            )
        self._base_url = base_url.rstrip("/")
        self._client = client
        # Respect free-tier limits by default.  Callers (tests, paid-tier)
        # can inject a permissive limiter or one with a custom rpm ceiling.
        self._rate_limiter = rate_limiter or polygon_rate_limiter_from_env()
        self._max_retries = max_retries

    def load_bars(
        self,
        request: DataRequest,
        adjustment: AdjustmentType = AdjustmentType.SPLIT_AND_DIVIDEND,
    ) -> tuple[pd.DataFrame, DataResult]:
        """Fetch bars from Polygon for each symbol sequentially.

        One HTTP request per symbol.  Pagination (``next_url``) is followed
        automatically so multi-year ranges work without caller intervention.
        """
        adjusted = adjustment != AdjustmentType.RAW
        if adjustment == AdjustmentType.SPLIT_ADJUSTED:
            logger.warning(
                "Polygon does not support split-only adjustment. "
                "Using split-and-dividend adjusted prices."
            )

        multiplier, timespan = _FREQUENCY_MAP.get(
            request.frequency, (1, "day")
        )

        frames: list[pd.DataFrame] = []
        symbols_found = 0
        client = self._client or httpx.Client(timeout=30.0)
        close_client = self._client is None  # only close if we created it

        try:
            for symbol in request.symbols:
                df = self._fetch_symbol(
                    client,
                    symbol=symbol,
                    start=request.start.isoformat(),
                    end=request.end.isoformat(),
                    multiplier=multiplier,
                    timespan=timespan,
                    adjusted=adjusted,
                    adjustment=adjustment,
                    frequency=request.frequency,
                )
                if len(df) > 0:
                    frames.append(df)
                    symbols_found += 1
        finally:
            if close_client:
                client.close()

        if frames:
            result_df = pd.concat(frames, ignore_index=True)
        else:
            result_df = pd.DataFrame(
                columns=[
                    "symbol", "timestamp", "open", "high", "low", "close",
                    "volume", "vwap", "adjustment", "source", "frequency",
                ],
            )

        metadata = DataResult(
            symbols_requested=len(request.symbols),
            symbols_returned=symbols_found,
            bars_returned=len(result_df),
            start=request.start,
            end=request.end,
            source="polygon",
        )
        return result_df, metadata

    def _fetch_symbol(
        self,
        client: httpx.Client,
        *,
        symbol: str,
        start: str,
        end: str,
        multiplier: int,
        timespan: str,
        adjusted: bool,
        adjustment: AdjustmentType,
        frequency: BarFrequency,
    ) -> pd.DataFrame:
        """Fetch all pages of bars for one symbol."""
        url = (
            f"{self._base_url}/v2/aggs/ticker/{symbol}"
            f"/range/{multiplier}/{timespan}/{start}/{end}"
        )
        params: dict[str, str | int | bool] = {
            "adjusted": str(adjusted).lower(),
            "sort": "asc",
            "limit": 50_000,
            "apiKey": self._api_key,
        }

        all_rows: list[dict[str, object]] = []

        while url:
            resp = request_with_retry(
                client,
                url=url,
                params=params,
                max_retries=self._max_retries,
                rate_limiter=self._rate_limiter,
            )
            resp.raise_for_status()
            body = resp.json()

            results = body.get("results", [])
            for r in results:
                all_rows.append({
                    "symbol": symbol,
                    "timestamp": _ms_to_utc(int(r["t"])),
                    "open": float(r["o"]),
                    "high": float(r["h"]),
                    "low": float(r["l"]),
                    "close": float(r["c"]),
                    "volume": float(r["v"]),
                    "vwap": float(r["vw"]) if "vw" in r else None,
                    "adjustment": adjustment.value,
                    "source": "polygon",
                    "frequency": frequency.value,
                })

            # Follow pagination
            next_url: str | None = body.get("next_url")
            if next_url:
                url = next_url
                # next_url is a full URL; clear params so we don't double-add
                params = {"apiKey": self._api_key}
            else:
                url = ""  # exit loop

        return pd.DataFrame(all_rows)


def _ms_to_utc(ms: int) -> datetime:
    """Convert Unix milliseconds to a timezone-aware UTC datetime."""
    return datetime.fromtimestamp(ms / 1000, tz=UTC)
