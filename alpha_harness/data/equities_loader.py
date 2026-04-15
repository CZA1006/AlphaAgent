"""Equities data loader — interface and local stub for US equity OHLCV.

Integration plan:
    Phase 1: LocalEquitiesLoader reads from local Parquet files in data/silver/equities/
    Phase 2: PolygonEquitiesLoader fetches from Polygon.io REST API via httpx
    Phase 3: Additional providers can implement the same protocol

Survivorship bias rules:
    - The loader MUST accept symbols that were valid during [start, end] even if
      they have since been delisted, acquired, or renamed.
    - The loader MUST NOT silently filter the symbol list to only currently-listed names.
    - If a requested symbol has no data for the given range, it should be absent
      from the output with an entry in DataResult.symbols_returned reflecting the gap.

Adjustment rules:
    - Default to split-and-dividend adjusted prices for factor research.
    - Raw prices should be available via the adjustment parameter for special cases.
    - The adjustment type MUST be recorded on every returned EquityBar.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

import pandas as pd

from alpha_harness.data.models import (
    AdjustmentType,
    DataRequest,
    DataResult,
)


class EquitiesLoader(Protocol):
    """Protocol for equity data loaders.

    All implementations — local Parquet, Polygon API, etc. — conform to this.
    """

    def load_bars(
        self,
        request: DataRequest,
        adjustment: AdjustmentType = AdjustmentType.SPLIT_AND_DIVIDEND,
    ) -> tuple[pd.DataFrame, DataResult]:
        """Load equity OHLCV bars for the given symbols and date range.

        Returns:
            A tuple of:
            - DataFrame with columns: symbol, timestamp, open, high, low, close,
              volume, vwap (nullable), adjustment. Index is not set — callers
              decide their own indexing.
            - DataResult with provenance metadata.
        """
        ...


class LocalEquitiesLoader:
    """Loads equity bars from local Parquet files.

    Expected file layout:
        {base_path}/{symbol}.parquet
        Each file has columns: timestamp, open, high, low, close, volume, vwap

    This is the Milestone 1 default — no API keys required.
    """

    def __init__(self, base_path: str = "data/silver/equities") -> None:
        self._base_path = Path(base_path)

    def load_bars(
        self,
        request: DataRequest,
        adjustment: AdjustmentType = AdjustmentType.SPLIT_AND_DIVIDEND,
    ) -> tuple[pd.DataFrame, DataResult]:
        frames: list[pd.DataFrame] = []
        symbols_found = 0

        for symbol in request.symbols:
            path = self._base_path / f"{symbol}.parquet"
            if not path.exists():
                continue

            df = pd.read_parquet(path)
            df["symbol"] = symbol
            df["adjustment"] = adjustment.value
            df["source"] = "local_parquet"
            df["frequency"] = request.frequency.value

            # Filter to requested date range
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
                    "volume", "vwap", "adjustment", "source", "frequency",
                ],
            )

        metadata = DataResult(
            symbols_requested=len(request.symbols),
            symbols_returned=symbols_found,
            bars_returned=len(result_df),
            start=request.start,
            end=request.end,
            source="local_parquet",
        )
        return result_df, metadata
