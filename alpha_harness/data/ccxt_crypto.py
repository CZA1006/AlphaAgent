"""CCXT crypto loader — fetches historical OHLCV bars from crypto exchanges.

Implements the CryptoLoader protocol using the ``ccxt`` library for
unified exchange connectivity.

Environment variables
---------------------
No API key is required for public OHLCV data on most exchanges.
For rate-limited or private endpoints, set exchange-specific keys:

    BINANCE_API_KEY / BINANCE_SECRET
    COINBASE_API_KEY / COINBASE_SECRET

Exchange scoping
----------------
Each ``CcxtCryptoLoader`` instance is bound to a single exchange.
Cross-exchange aggregation is NOT done at the loader level — that is
a downstream normalization decision.

Symbol normalization
--------------------
Uses ccxt-style symbols: ``"BTC/USDT"``, ``"ETH/USDT"`` (base/quote
with slash).  The loader passes these directly to the ccxt exchange
instance.

Persistence
-----------
This loader returns a DataFrame in memory.  Use ``ParquetStore`` (in
``parquet_store.py``) to persist the result to local Parquet files for
subsequent reads via ``LocalCryptoLoader``.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Any

import pandas as pd

from alpha_harness.data.models import (
    BarFrequency,
    DataRequest,
    DataResult,
)

logger = logging.getLogger(__name__)

# Map our BarFrequency enum to ccxt timeframe strings
_TIMEFRAME_MAP: dict[BarFrequency, str] = {
    BarFrequency.DAILY: "1d",
    BarFrequency.HOURLY: "1h",
    BarFrequency.MINUTE_1: "1m",
    BarFrequency.MINUTE_5: "5m",
    BarFrequency.MINUTE_15: "15m",
}


class CcxtCryptoLoader:
    """Fetch crypto OHLCV bars from a single exchange via ccxt.

    Conforms to the ``CryptoLoader`` protocol.

    Parameters
    ----------
    exchange_id:
        The ccxt exchange identifier (e.g. ``"binance"``, ``"coinbase"``).
    exchange_config:
        Optional dict passed to the ccxt exchange constructor (api keys,
        rate-limit overrides, sandbox mode, etc.).
    exchange_instance:
        Optional pre-configured ccxt exchange object for testing.
        If provided, ``exchange_id`` and ``exchange_config`` are ignored.
    """

    def __init__(
        self,
        exchange_id: str = "binance",
        exchange_config: dict[str, object] | None = None,
        exchange_instance: object | None = None,
    ) -> None:
        self._exchange_id = exchange_id
        if exchange_instance is not None:
            self._exchange: object = exchange_instance
        else:
            self._exchange = _create_exchange(exchange_id, exchange_config)

    def load_bars(
        self,
        request: DataRequest,
        exchange: str = "",
    ) -> tuple[pd.DataFrame, DataResult]:
        """Fetch bars from the configured exchange for each symbol.

        The ``exchange`` parameter is accepted for protocol compatibility
        but ignored — the exchange is fixed at construction time.  Pass
        it as empty string or omit it.
        """
        effective_exchange = exchange or self._exchange_id
        timeframe = _TIMEFRAME_MAP.get(request.frequency, "1d")
        since_ms = _date_to_ms(request.start)
        end_ms = _date_to_ms(request.end) + 86_400_000  # include end date

        frames: list[pd.DataFrame] = []
        symbols_found = 0

        for symbol in request.symbols:
            df = self._fetch_symbol(
                symbol=symbol,
                timeframe=timeframe,
                since_ms=since_ms,
                end_ms=end_ms,
                exchange_name=effective_exchange,
                frequency=request.frequency,
            )
            if len(df) > 0:
                frames.append(df)
                symbols_found += 1

        if frames:
            result_df = pd.concat(frames, ignore_index=True)
        else:
            result_df = pd.DataFrame(
                columns=[
                    "symbol",
                    "timestamp",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "exchange",
                    "quote_currency",
                    "source",
                    "frequency",
                ],
            )

        metadata = DataResult(
            symbols_requested=len(request.symbols),
            symbols_returned=symbols_found,
            bars_returned=len(result_df),
            start=request.start,
            end=request.end,
            source=f"ccxt:{effective_exchange}",
        )
        return result_df, metadata

    def _fetch_symbol(
        self,
        *,
        symbol: str,
        timeframe: str,
        since_ms: int,
        end_ms: int,
        exchange_name: str,
        frequency: BarFrequency,
    ) -> pd.DataFrame:
        """Fetch all pages of OHLCV candles for one symbol.

        CCXT returns at most ``limit`` candles per call, so we loop
        forward by advancing ``since`` until we pass ``end_ms``.
        """
        all_rows: list[dict[str, Any]] = []
        current_since = since_ms
        limit = 1000  # default ccxt page size

        quote_currency = symbol.split("/")[-1] if "/" in symbol else "USDT"

        while current_since < end_ms:
            try:
                candles: list[list[Any]] = self._exchange.fetch_ohlcv(  # type: ignore[attr-defined]
                    symbol,
                    timeframe=timeframe,
                    since=current_since,
                    limit=limit,
                )
            except Exception:
                logger.warning(
                    "Failed to fetch %s from %s (since=%s)",
                    symbol,
                    exchange_name,
                    current_since,
                    exc_info=True,
                )
                break

            if not candles:
                break

            for candle in candles:
                ts_ms = int(candle[0])
                if ts_ms >= end_ms:
                    break
                all_rows.append(
                    {
                        "symbol": symbol,
                        "timestamp": datetime.fromtimestamp(ts_ms / 1000, tz=UTC),
                        "open": float(candle[1]),
                        "high": float(candle[2]),
                        "low": float(candle[3]),
                        "close": float(candle[4]),
                        "volume": float(candle[5]),
                        "exchange": exchange_name,
                        "quote_currency": quote_currency,
                        "source": f"ccxt:{exchange_name}",
                        "frequency": frequency.value,
                    }
                )

            # Advance past the last candle to avoid duplicates
            last_ts = int(candles[-1][0])
            if last_ts <= current_since:
                break  # no progress — avoid infinite loop
            current_since = last_ts + 1

        return pd.DataFrame(all_rows)


def _create_exchange(exchange_id: str, config: dict[str, object] | None) -> object:
    """Create a ccxt exchange instance.

    Defers the ccxt import so the module can be imported without ccxt
    installed (for type checking / CI environments that mock the loader).
    """
    import ccxt

    exchange_class = getattr(ccxt, exchange_id, None)
    if exchange_class is None:
        msg = f"Unknown ccxt exchange: {exchange_id!r}"
        raise ValueError(msg)
    result: object = exchange_class(config or {})
    return result


def _date_to_ms(d: date) -> int:
    """Convert a date to Unix milliseconds at midnight UTC."""
    dt = datetime(d.year, d.month, d.day, tzinfo=UTC)
    return int(dt.timestamp() * 1000)
