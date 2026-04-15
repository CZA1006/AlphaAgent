"""Tests for data layer models and local loader stubs."""

from datetime import UTC, date, datetime

import pandas as pd

from alpha_harness.data.models import (
    AdjustmentType,
    Bar,
    BarFrequency,
    CryptoBar,
    DataRequest,
    DataResult,
    EquityBar,
    FundamentalRecord,
)

# ── Data model tests ─────────────────────────────────────────────────────────


def test_bar_defaults():
    b = Bar(
        symbol="AAPL",
        timestamp=datetime(2023, 6, 15, 20, 0, tzinfo=UTC),
        open=180.0, high=182.0, low=179.0, close=181.5, volume=50_000_000.0,
    )
    assert b.frequency == BarFrequency.DAILY
    assert b.vwap is None
    assert b.source == ""


def test_equity_bar_adjustment():
    b = EquityBar(
        symbol="MSFT",
        timestamp=datetime(2023, 6, 15, 20, 0, tzinfo=UTC),
        open=330.0, high=335.0, low=329.0, close=334.0, volume=25_000_000.0,
        adjustment=AdjustmentType.RAW,
    )
    assert b.adjustment == AdjustmentType.RAW


def test_crypto_bar_exchange():
    b = CryptoBar(
        symbol="BTC/USDT",
        timestamp=datetime(2023, 6, 15, 0, 0, tzinfo=UTC),
        open=25000.0, high=25500.0, low=24800.0, close=25200.0,
        volume=1200.0,
        exchange="binance",
    )
    assert b.exchange == "binance"
    assert b.quote_currency == "USDT"


def test_fundamental_record_pit_fields():
    f = FundamentalRecord(
        symbol="AAPL",
        field_name="revenue",
        value=94_836_000_000.0,
        period_end=date(2023, 12, 31),
        published_at=datetime(2024, 2, 1, 16, 0, tzinfo=UTC),
        fiscal_quarter="Q1",
        source="sec_edgar",
    )
    # The critical PIT check: published_at is AFTER period_end
    assert f.published_at.date() > f.period_end
    assert f.field_name == "revenue"


def test_data_request():
    req = DataRequest(
        symbols=["AAPL", "MSFT", "GOOG"],
        start=date(2020, 1, 1),
        end=date(2023, 12, 31),
    )
    assert len(req.symbols) == 3
    assert req.frequency == BarFrequency.DAILY


def test_data_result_missing_symbols():
    res = DataResult(
        symbols_requested=5,
        symbols_returned=3,
        bars_returned=750,
        start=date(2020, 1, 1),
        end=date(2023, 12, 31),
        source="polygon",
    )
    assert res.missing_symbols == 2
    assert res.loaded_at.tzinfo is not None


# ── Local loader tests (empty data directory) ────────────────────────────────


def test_local_equities_loader_empty():
    from alpha_harness.data.equities_loader import LocalEquitiesLoader

    loader = LocalEquitiesLoader(base_path="/tmp/nonexistent_equities_path")
    req = DataRequest(
        symbols=["AAPL", "MSFT"],
        start=date(2020, 1, 1),
        end=date(2023, 12, 31),
    )
    df, meta = loader.load_bars(req)
    assert isinstance(df, pd.DataFrame)
    assert len(df) == 0
    assert meta.symbols_requested == 2
    assert meta.symbols_returned == 0
    assert meta.bars_returned == 0


def test_local_crypto_loader_empty():
    from alpha_harness.data.crypto_loader import LocalCryptoLoader

    loader = LocalCryptoLoader(base_path="/tmp/nonexistent_crypto_path")
    req = DataRequest(
        symbols=["BTC/USDT", "ETH/USDT"],
        start=date(2020, 1, 1),
        end=date(2023, 12, 31),
    )
    df, meta = loader.load_bars(req, exchange="binance")
    assert isinstance(df, pd.DataFrame)
    assert len(df) == 0
    assert meta.source == "local_parquet:binance"


def test_local_fundamentals_loader_empty():
    from alpha_harness.data.fundamentals_loader import LocalFundamentalsLoader

    loader = LocalFundamentalsLoader(base_path="/tmp/nonexistent_fundamentals_path")
    records, meta = loader.load_fundamentals(
        symbols=["AAPL"],
        fields=["revenue"],
        start=date(2020, 1, 1),
        end=date(2023, 12, 31),
    )
    assert records == []
    assert meta.symbols_returned == 0
