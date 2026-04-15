"""Crypto data loader — interface and local stub for crypto OHLCV.

Integration plan:
    Phase 1: LocalCryptoLoader reads from local Parquet files in data/silver/crypto/
    Phase 2: CcxtCryptoLoader fetches from exchanges via the ccxt library
    Phase 3: Multiple exchanges can be loaded and kept exchange-scoped

Exchange scoping rules:
    - Crypto prices are exchange-specific. BTC/USDT on Binance is NOT the same
      price as BTC/USDT on Coinbase.
    - The loader MUST tag every bar with its source exchange.
    - Cross-exchange aggregation is NOT done at the loader level — that is a
      downstream normalization decision.
    - For local Parquet files, the exchange is encoded in the directory structure:
      data/silver/crypto/{exchange}/{symbol}.parquet

Symbol normalization:
    - Use ccxt-style symbols: "BTC/USDT", "ETH/USDT" (base/quote with slash).
    - Filenames use underscore: "BTC_USDT.parquet" (filesystem-safe).
    - The loader handles this conversion internally.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

import pandas as pd

from alpha_harness.data.models import (
    DataRequest,
    DataResult,
)


class CryptoLoader(Protocol):
    """Protocol for crypto data loaders.

    All implementations — local Parquet, ccxt, etc. — conform to this.
    """

    def load_bars(
        self,
        request: DataRequest,
        exchange: str = "",
    ) -> tuple[pd.DataFrame, DataResult]:
        """Load crypto OHLCV bars for the given symbols and date range.

        Args:
            request: Symbols (in "BTC/USDT" format), date range, frequency.
            exchange: Exchange scope. Required for production; empty for tests.

        Returns:
            A tuple of:
            - DataFrame with columns: symbol, timestamp, open, high, low, close,
              volume, exchange, quote_currency, source, frequency.
            - DataResult with provenance metadata.
        """
        ...


def _symbol_to_filename(symbol: str) -> str:
    """Convert ccxt-style symbol to filesystem-safe filename.

    "BTC/USDT" -> "BTC_USDT"
    """
    return symbol.replace("/", "_")


class LocalCryptoLoader:
    """Loads crypto bars from local Parquet files.

    Expected file layout:
        {base_path}/{exchange}/{symbol}.parquet
        e.g. data/silver/crypto/binance/BTC_USDT.parquet

    Each file has columns: timestamp, open, high, low, close, volume
    """

    def __init__(
        self,
        base_path: str = "data/silver/crypto",
        default_exchange: str = "binance",
    ) -> None:
        self._base_path = Path(base_path)
        self._default_exchange = default_exchange

    def load_bars(
        self,
        request: DataRequest,
        exchange: str = "",
    ) -> tuple[pd.DataFrame, DataResult]:
        exchange = exchange or self._default_exchange
        exchange_path = self._base_path / exchange
        frames: list[pd.DataFrame] = []
        symbols_found = 0

        for symbol in request.symbols:
            filename = _symbol_to_filename(symbol)
            path = exchange_path / f"{filename}.parquet"
            if not path.exists():
                continue

            df = pd.read_parquet(path)
            df["symbol"] = symbol
            df["exchange"] = exchange
            df["quote_currency"] = symbol.split("/")[-1] if "/" in symbol else "USDT"
            df["source"] = f"local_parquet:{exchange}"
            df["frequency"] = request.frequency.value

            if "timestamp" in df.columns:
                df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
                mask = (df["timestamp"].dt.date >= request.start) & (
                    df["timestamp"].dt.date <= request.end
                )
                df = df.loc[mask]

            if len(df) > 0:
                frames.append(df)
                symbols_found += 1

        if frames:
            result_df = pd.concat(frames, ignore_index=True)
        else:
            result_df = pd.DataFrame(
                columns=[
                    "symbol", "timestamp", "open", "high", "low", "close",
                    "volume", "exchange", "quote_currency", "source", "frequency",
                ],
            )

        metadata = DataResult(
            symbols_requested=len(request.symbols),
            symbols_returned=symbols_found,
            bars_returned=len(result_df),
            start=request.start,
            end=request.end,
            source=f"local_parquet:{exchange}",
        )
        return result_df, metadata
