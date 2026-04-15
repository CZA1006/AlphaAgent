"""Fundamentals data loader — interface and stub for SEC-derived financial data.

Integration plan:
    Phase 1: LocalFundamentalsLoader reads from local Parquet/CSV files
    Phase 2: PolygonFundamentalsLoader fetches from Polygon.io financials API
    Phase 3: SEC EDGAR direct adapter for raw filings

Point-in-time rules (CRITICAL):
    This is the most dangerous data layer for lookahead bias. Financial
    statements have TWO dates that matter:

    1. period_end   — when the fiscal quarter/year ended (e.g. 2023-12-31)
    2. published_at — when the filing was made public (e.g. 2024-02-15)

    For factor construction, ONLY published_at determines when the data
    was available to the market. Using period_end directly causes lookahead
    bias because the data is not known to participants until publication.

    The loader MUST:
    - Preserve both dates on every record
    - Never silently backfill: if a record has no published_at, it MUST be
      excluded from PIT queries, not assumed to be available at period_end
    - Support as-of queries: "give me the latest known fundamentals as of date X"
      where X is compared against published_at, NOT period_end

Backfill detection:
    Many data vendors silently update historical fundamentals when restatements
    occur. The loader should log when a (symbol, field, period_end) tuple has
    multiple published_at values, as this indicates a restatement chain.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Protocol

import pandas as pd

from alpha_harness.data.models import DataResult, FundamentalRecord


class FundamentalsLoader(Protocol):
    """Protocol for fundamentals data loaders."""

    def load_fundamentals(
        self,
        symbols: list[str],
        fields: list[str],
        start: date,
        end: date,
    ) -> tuple[list[FundamentalRecord], DataResult]:
        """Load fundamental data points for the given symbols and fields.

        Args:
            symbols: Ticker symbols to load.
            fields: Fundamental field names (e.g. ["revenue", "eps", "book_value"]).
            start: Earliest period_end date to include.
            end: Latest period_end date to include.

        Returns:
            A tuple of:
            - List of FundamentalRecord objects with explicit published_at dates.
            - DataResult with provenance metadata.

        Point-in-time note:
            The returned records include ALL publications within the date range,
            including restatements. Consumers must filter by published_at for
            PIT correctness — the loader does not do this automatically.
        """
        ...

    def load_as_of(
        self,
        symbols: list[str],
        fields: list[str],
        as_of: date,
    ) -> tuple[list[FundamentalRecord], DataResult]:
        """Load the latest known fundamentals as of a specific date.

        This is the point-in-time safe query: returns only records where
        published_at <= as_of. For each (symbol, field), returns only the
        most recent period_end whose publication was available by as_of.

        This is the method that factor construction should use.
        """
        ...


class LocalFundamentalsLoader:
    """Loads fundamentals from local Parquet/CSV files.

    Expected file layout:
        {base_path}/fundamentals.parquet
        Columns: symbol, field_name, value, period_end, published_at,
                 fiscal_quarter, source

    This is a stub for Milestone 1. Real implementations will handle
    restatement chains and multi-source reconciliation.
    """

    def __init__(self, base_path: str = "data/silver/fundamentals") -> None:
        self._base_path = Path(base_path)

    def load_fundamentals(
        self,
        symbols: list[str],
        fields: list[str],
        start: date,
        end: date,
    ) -> tuple[list[FundamentalRecord], DataResult]:
        path = self._base_path / "fundamentals.parquet"
        if not path.exists():
            return [], DataResult(
                symbols_requested=len(symbols),
                symbols_returned=0,
                bars_returned=0,
                start=start,
                end=end,
                source="local_parquet",
            )

        df = pd.read_parquet(path)
        df = df[df["symbol"].isin(symbols) & df["field_name"].isin(fields)]

        if "period_end" in df.columns:
            df["period_end"] = pd.to_datetime(df["period_end"]).dt.date
            df = df[(df["period_end"] >= start) & (df["period_end"] <= end)]

        records = [
            FundamentalRecord(
                symbol=row["symbol"],
                field_name=row["field_name"],
                value=float(row["value"]),
                period_end=row["period_end"],
                published_at=pd.Timestamp(row["published_at"], tz="UTC").to_pydatetime(),
                fiscal_quarter=row.get("fiscal_quarter", ""),
                source=row.get("source", "local_parquet"),
            )
            for _, row in df.iterrows()
        ]

        symbols_found = len(set(r.symbol for r in records))
        return records, DataResult(
            symbols_requested=len(symbols),
            symbols_returned=symbols_found,
            bars_returned=len(records),
            start=start,
            end=end,
            source="local_parquet",
        )

    def load_as_of(
        self,
        symbols: list[str],
        fields: list[str],
        as_of: date,
    ) -> tuple[list[FundamentalRecord], DataResult]:
        # Load all available data up to as_of
        all_records, _meta = self.load_fundamentals(
            symbols=symbols,
            fields=fields,
            start=date(1900, 1, 1),
            end=as_of,
        )

        # Filter to only records published by as_of
        pit_records = [
            r for r in all_records
            if r.published_at.date() <= as_of
        ]

        # Keep only the latest period_end per (symbol, field)
        latest: dict[tuple[str, str], FundamentalRecord] = {}
        for r in pit_records:
            key = (r.symbol, r.field_name)
            if key not in latest or r.period_end > latest[key].period_end:
                latest[key] = r

        result = list(latest.values())
        symbols_found = len(set(r.symbol for r in result))
        return result, DataResult(
            symbols_requested=len(symbols),
            symbols_returned=symbols_found,
            bars_returned=len(result),
            start=date(1900, 1, 1),
            end=as_of,
            source="local_parquet:pit",
        )
