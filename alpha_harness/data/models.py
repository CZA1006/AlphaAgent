"""Shared data models for the data layer.

These Pydantic models define the canonical shape of market data flowing
through AlphaAgent. They serve as the typed contract between data loaders
(equities, crypto, fundamentals) and downstream consumers (factor DSL,
evaluators).

Point-in-time rules:
    - Every bar carries an explicit timestamp representing the END of the bar.
    - Adjustment status must be explicit (split/dividend adjusted vs. raw).
    - Fundamental records carry both the period end date AND the publication
      date — only the publication date is safe for PIT joins.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

# ── Enums ────────────────────────────────────────────────────────────────────


class BarFrequency(StrEnum):
    """Supported bar frequencies."""

    TICK = "tick"
    DAILY = "1d"
    HOURLY = "1h"
    MINUTE_1 = "1m"
    MINUTE_5 = "5m"
    MINUTE_15 = "15m"


class AdjustmentType(StrEnum):
    """How prices are adjusted."""

    RAW = "raw"  # no adjustments
    SPLIT_ADJUSTED = "split"  # stock-split adjusted
    SPLIT_AND_DIVIDEND = "split_div"  # split + dividend adjusted


# ── Bar models ───────────────────────────────────────────────────────────────


class Bar(BaseModel):
    """A single OHLCV bar — the atomic unit of price data.

    Shared across equities and crypto. The `source` field distinguishes
    origin for audit/provenance.
    """

    symbol: str
    timestamp: datetime  # bar close time, always UTC-aware
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float | None = None  # volume-weighted average price (if available)
    trade_count: int | None = None
    source: str = ""  # e.g. "polygon", "ccxt:binance", "local_csv"
    frequency: BarFrequency = BarFrequency.DAILY


class EquityBar(Bar):
    """Equity-specific bar with adjustment metadata.

    Survivorship bias note:
        Bars for delisted tickers must still be loadable if the universe
        spec includes them. The loader must NOT silently drop symbols
        that were delisted after the requested date range.
    """

    adjustment: AdjustmentType = AdjustmentType.SPLIT_AND_DIVIDEND


class CryptoBar(Bar):
    """Crypto-specific bar with exchange scope.

    Exchange scoping note:
        Crypto prices are exchange-specific. Bars from different exchanges
        must NOT be mixed unless explicitly normalized. The `exchange` field
        is mandatory for production use; it defaults to empty only for tests.
    """

    exchange: str = ""  # e.g. "binance", "coinbase", "bybit"
    quote_currency: str = "USDT"


class TickEventType(StrEnum):
    """Supported tick event types in the HK IPO tick lake."""

    TRADE = "TRADE"
    BID = "BID"
    ASK = "ASK"


class TickEvent(BaseModel):
    """A single market-data tick.

    HK IPO tick data stores trades and quote updates as separate events.
    ``price`` is the trade price for TRADE rows and quoted bid/ask price
    for BID/ASK rows.
    """

    symbol: str
    timestamp: datetime
    event_type: TickEventType
    price: float
    size: float | None = None
    trading_date: date | None = None
    source: str = ""
    condition_codes: str | None = None
    exchange_code: str | None = None


# ── Fundamental models ───────────────────────────────────────────────────────


class FundamentalRecord(BaseModel):
    """A single fundamental data point for a company.

    Point-in-time critical fields:
        - period_end: when the fiscal period ended (e.g. 2023-12-31)
        - published_at: when the filing became publicly available
        - Only `published_at` is safe for point-in-time factor construction.
          Using `period_end` directly causes lookahead bias because the data
          is not available to market participants until `published_at`.
    """

    symbol: str
    field_name: str  # e.g. "revenue", "eps", "book_value"
    value: float
    period_end: date  # fiscal period end date
    published_at: datetime  # when this data became publicly available
    fiscal_quarter: str = ""  # e.g. "Q4", "FY"
    source: str = ""  # e.g. "sec_edgar", "polygon_fundamentals"


# ── Data request/response ────────────────────────────────────────────────────


class DataRequest(BaseModel):
    """A request for market data — shared input type for all loaders.

    This makes the data loading interface explicit: the caller always
    specifies symbols, date range, and frequency up front.
    """

    symbols: list[str]
    start: date
    end: date
    frequency: BarFrequency = BarFrequency.DAILY


class DataResult(BaseModel):
    """Metadata about a data load result.

    Returned alongside the actual data (DataFrame or list of bars) to
    provide provenance information for reproducibility.
    """

    symbols_requested: int
    symbols_returned: int
    bars_returned: int
    start: date
    end: date
    source: str
    loaded_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
    )

    @property
    def missing_symbols(self) -> int:
        return self.symbols_requested - self.symbols_returned
