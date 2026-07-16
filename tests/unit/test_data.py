"""Tests for data layer models, local loaders, API adapters, and persistence.

Sections:
    - Data model tests
    - Local loader tests (empty data directory)
    - PolygonEquitiesLoader (mocked HTTP)
    - CcxtCryptoLoader (mocked exchange)
    - ParquetStore (round-trip through temp files)
    - Loader factory
"""

from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

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


def test_bar_defaults() -> None:
    b = Bar(
        symbol="AAPL",
        timestamp=datetime(2023, 6, 15, 20, 0, tzinfo=UTC),
        open=180.0,
        high=182.0,
        low=179.0,
        close=181.5,
        volume=50_000_000.0,
    )
    assert b.frequency == BarFrequency.DAILY
    assert b.vwap is None
    assert b.source == ""


def test_equity_bar_adjustment() -> None:
    b = EquityBar(
        symbol="MSFT",
        timestamp=datetime(2023, 6, 15, 20, 0, tzinfo=UTC),
        open=330.0,
        high=335.0,
        low=329.0,
        close=334.0,
        volume=25_000_000.0,
        adjustment=AdjustmentType.RAW,
    )
    assert b.adjustment == AdjustmentType.RAW


def test_crypto_bar_exchange() -> None:
    b = CryptoBar(
        symbol="BTC/USDT",
        timestamp=datetime(2023, 6, 15, 0, 0, tzinfo=UTC),
        open=25000.0,
        high=25500.0,
        low=24800.0,
        close=25200.0,
        volume=1200.0,
        exchange="binance",
    )
    assert b.exchange == "binance"
    assert b.quote_currency == "USDT"


def test_fundamental_record_pit_fields() -> None:
    f = FundamentalRecord(
        symbol="AAPL",
        field_name="revenue",
        value=94_836_000_000.0,
        period_end=date(2023, 12, 31),
        published_at=datetime(2024, 2, 1, 16, 0, tzinfo=UTC),
        fiscal_quarter="Q1",
        source="sec_edgar",
    )
    assert f.published_at.date() > f.period_end
    assert f.field_name == "revenue"


def test_data_request() -> None:
    req = DataRequest(
        symbols=["AAPL", "MSFT", "GOOG"],
        start=date(2020, 1, 1),
        end=date(2023, 12, 31),
    )
    assert len(req.symbols) == 3
    assert req.frequency == BarFrequency.DAILY


def test_data_result_missing_symbols() -> None:
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


def test_local_equities_loader_empty() -> None:
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


def test_local_crypto_loader_empty() -> None:
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


def test_local_fundamentals_loader_empty() -> None:
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


# ── PolygonEquitiesLoader (mocked HTTP) ──────────────────────────────────────


def _polygon_response(symbol: str, n_bars: int = 3) -> dict[str, object]:
    """Build a realistic Polygon /v2/aggs response dict."""
    base_ms = 1672531200000  # 2023-01-01 00:00:00 UTC
    results: list[dict[str, object]] = []
    for i in range(n_bars):
        results.append(
            {
                "t": base_ms + i * 86_400_000,
                "o": 100.0 + i,
                "h": 101.0 + i,
                "l": 99.0 + i,
                "c": 100.5 + i,
                "v": 1_000_000.0 + i * 100_000,
                "vw": 100.3 + i,
            }
        )
    return {
        "ticker": symbol,
        "queryCount": n_bars,
        "resultsCount": n_bars,
        "adjusted": True,
        "results": results,
        "status": "OK",
        "request_id": "test",
    }


class TestPolygonEquitiesLoader:
    def test_load_bars_single_symbol(self) -> None:
        """Mock HTTP and verify DataFrame shape and provenance."""
        from alpha_harness.data.polygon_equities import PolygonEquitiesLoader

        mock_response = MagicMock()
        mock_response.json.return_value = _polygon_response("AAPL", n_bars=5)
        mock_response.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.get.return_value = mock_response

        loader = PolygonEquitiesLoader(api_key="test_key", client=mock_client)
        req = DataRequest(
            symbols=["AAPL"],
            start=date(2023, 1, 1),
            end=date(2023, 1, 31),
        )
        df, meta = loader.load_bars(req)

        assert len(df) == 5
        assert meta.symbols_returned == 1
        assert meta.source == "polygon"
        assert set(df.columns) >= {
            "symbol",
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "vwap",
            "adjustment",
            "source",
        }
        assert df["symbol"].iloc[0] == "AAPL"
        assert df["source"].iloc[0] == "polygon"

    def test_load_bars_multiple_symbols(self) -> None:
        """Multiple symbols generate one HTTP call per symbol."""
        from alpha_harness.data.polygon_equities import PolygonEquitiesLoader

        call_count = 0

        def _side_effect(*_args: object, **_kwargs: object) -> MagicMock:
            nonlocal call_count
            call_count += 1
            resp = MagicMock()
            symbols = ["AAPL", "MSFT"]
            sym = symbols[min(call_count - 1, 1)]
            resp.json.return_value = _polygon_response(sym, n_bars=2)
            resp.raise_for_status = MagicMock()
            return resp

        mock_client = MagicMock()
        mock_client.get.side_effect = _side_effect

        loader = PolygonEquitiesLoader(api_key="key", client=mock_client)
        req = DataRequest(
            symbols=["AAPL", "MSFT"],
            start=date(2023, 1, 1),
            end=date(2023, 1, 31),
        )
        _df, meta = loader.load_bars(req)

        assert meta.symbols_requested == 2
        assert meta.symbols_returned == 2
        assert meta.bars_returned == 4
        assert call_count == 2

    def test_empty_response(self) -> None:
        """Symbol with no data returns empty."""
        from alpha_harness.data.polygon_equities import PolygonEquitiesLoader

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "results": [],
            "status": "OK",
            "queryCount": 0,
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.get.return_value = mock_response

        loader = PolygonEquitiesLoader(api_key="key", client=mock_client)
        req = DataRequest(
            symbols=["DELISTED"],
            start=date(2023, 1, 1),
            end=date(2023, 1, 31),
        )
        df, meta = loader.load_bars(req)

        assert len(df) == 0
        assert meta.symbols_returned == 0

    def test_raw_adjustment(self) -> None:
        """Requesting RAW adjustment records it in the DataFrame."""
        from alpha_harness.data.polygon_equities import PolygonEquitiesLoader

        mock_response = MagicMock()
        mock_response.json.return_value = _polygon_response("AAPL", n_bars=1)
        mock_response.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.get.return_value = mock_response

        loader = PolygonEquitiesLoader(api_key="key", client=mock_client)
        req = DataRequest(
            symbols=["AAPL"],
            start=date(2023, 1, 1),
            end=date(2023, 1, 31),
        )
        df, _ = loader.load_bars(req, adjustment=AdjustmentType.RAW)

        assert df["adjustment"].iloc[0] == "raw"
        # Verify the adjusted=false was sent in the request
        call_args = mock_client.get.call_args
        params = call_args.kwargs.get("params") or call_args[1].get("params", {})
        assert params.get("adjusted") == "false"

    def test_pagination_follows_next_url(self) -> None:
        """Loader follows next_url for paginated responses."""
        from alpha_harness.data.polygon_equities import PolygonEquitiesLoader

        page1 = _polygon_response("AAPL", n_bars=2)
        page1["next_url"] = "https://api.polygon.io/v2/aggs/next_page"

        page2 = _polygon_response("AAPL", n_bars=1)
        # No next_url on page 2

        call_idx = 0

        def _side_effect(*_args: object, **_kwargs: object) -> MagicMock:
            nonlocal call_idx
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = page1 if call_idx == 0 else page2
            call_idx += 1
            return resp

        mock_client = MagicMock()
        mock_client.get.side_effect = _side_effect

        loader = PolygonEquitiesLoader(api_key="key", client=mock_client)
        req = DataRequest(
            symbols=["AAPL"],
            start=date(2023, 1, 1),
            end=date(2023, 1, 31),
        )
        _df, meta = loader.load_bars(req)

        assert meta.bars_returned == 3  # 2 from page 1 + 1 from page 2
        assert mock_client.get.call_count == 2


# ── CcxtCryptoLoader (mocked exchange) ───────────────────────────────────────


def _mock_ohlcv(n: int = 5, base_ms: int = 1672531200000) -> list[list[object]]:
    """Build fake ccxt OHLCV candles: [timestamp, o, h, l, c, v]."""
    return [
        [
            base_ms + i * 86_400_000,
            30000.0 + i * 100,
            30500.0 + i * 100,
            29500.0 + i * 100,
            30200.0 + i * 100,
            500.0 + i * 50,
        ]
        for i in range(n)
    ]


class TestCcxtCryptoLoader:
    def test_load_bars_single_symbol(self) -> None:
        """Mock exchange and verify DataFrame shape."""
        from alpha_harness.data.ccxt_crypto import CcxtCryptoLoader

        mock_exchange = MagicMock()
        mock_exchange.fetch_ohlcv.side_effect = [
            _mock_ohlcv(5),
            [],  # second call returns empty -> stop pagination
        ]

        loader = CcxtCryptoLoader(
            exchange_id="binance",
            exchange_instance=mock_exchange,
        )
        req = DataRequest(
            symbols=["BTC/USDT"],
            start=date(2023, 1, 1),
            end=date(2023, 1, 31),
        )
        df, meta = loader.load_bars(req)

        assert len(df) == 5
        assert meta.symbols_returned == 1
        assert meta.source == "ccxt:binance"
        assert set(df.columns) >= {
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
        }
        assert df["symbol"].iloc[0] == "BTC/USDT"
        assert df["exchange"].iloc[0] == "binance"
        assert df["quote_currency"].iloc[0] == "USDT"

    def test_load_bars_multiple_symbols(self) -> None:
        """Multiple symbols fetch independently."""
        from alpha_harness.data.ccxt_crypto import CcxtCryptoLoader

        mock_exchange = MagicMock()
        mock_exchange.fetch_ohlcv.side_effect = [
            _mock_ohlcv(3),
            [],  # BTC/USDT
            _mock_ohlcv(2),
            [],  # ETH/USDT
        ]

        loader = CcxtCryptoLoader(
            exchange_id="binance",
            exchange_instance=mock_exchange,
        )
        req = DataRequest(
            symbols=["BTC/USDT", "ETH/USDT"],
            start=date(2023, 1, 1),
            end=date(2023, 1, 31),
        )
        _df, meta = loader.load_bars(req)

        assert meta.symbols_returned == 2
        assert meta.bars_returned == 5

    def test_empty_exchange_response(self) -> None:
        """Exchange returns no data -> empty DataFrame."""
        from alpha_harness.data.ccxt_crypto import CcxtCryptoLoader

        mock_exchange = MagicMock()
        mock_exchange.fetch_ohlcv.return_value = []

        loader = CcxtCryptoLoader(
            exchange_id="coinbase",
            exchange_instance=mock_exchange,
        )
        req = DataRequest(
            symbols=["UNKNOWN/USDT"],
            start=date(2023, 1, 1),
            end=date(2023, 1, 31),
        )
        df, meta = loader.load_bars(req)

        assert len(df) == 0
        assert meta.symbols_returned == 0

    def test_exchange_error_handled(self) -> None:
        """Exchange API error is caught and symbol is skipped."""
        from alpha_harness.data.ccxt_crypto import CcxtCryptoLoader

        mock_exchange = MagicMock()
        mock_exchange.fetch_ohlcv.side_effect = RuntimeError("rate limit")

        loader = CcxtCryptoLoader(
            exchange_id="binance",
            exchange_instance=mock_exchange,
        )
        req = DataRequest(
            symbols=["BTC/USDT"],
            start=date(2023, 1, 1),
            end=date(2023, 1, 31),
        )
        df, meta = loader.load_bars(req)

        assert len(df) == 0
        assert meta.symbols_returned == 0

    def test_quote_currency_parsed(self) -> None:
        """Quote currency extracted from symbol string."""
        from alpha_harness.data.ccxt_crypto import CcxtCryptoLoader

        mock_exchange = MagicMock()
        mock_exchange.fetch_ohlcv.side_effect = [_mock_ohlcv(1), []]

        loader = CcxtCryptoLoader(
            exchange_id="binance",
            exchange_instance=mock_exchange,
        )
        req = DataRequest(
            symbols=["ETH/BTC"],
            start=date(2023, 1, 1),
            end=date(2023, 1, 31),
        )
        df, _ = loader.load_bars(req)
        assert df["quote_currency"].iloc[0] == "BTC"


# ── ParquetStore ─────────────────────────────────────────────────────────────


class TestParquetStore:
    def test_save_equities_round_trip(self, tmp_path: Path) -> None:
        """Save equities DataFrame, verify files on disk."""
        from alpha_harness.data.parquet_store import ParquetStore

        df = pd.DataFrame(
            {
                "symbol": ["AAPL", "AAPL", "MSFT"],
                "timestamp": pd.to_datetime(
                    ["2023-01-01", "2023-01-02", "2023-01-01"],
                    utc=True,
                ),
                "open": [100.0, 101.0, 200.0],
                "high": [102.0, 103.0, 202.0],
                "low": [99.0, 100.0, 199.0],
                "close": [101.0, 102.0, 201.0],
                "volume": [1e6, 1.1e6, 2e6],
            }
        )

        store = ParquetStore(str(tmp_path / "equities"))
        n = store.save_equities(df)

        assert n == 2
        assert (tmp_path / "equities" / "AAPL.parquet").exists()
        assert (tmp_path / "equities" / "MSFT.parquet").exists()

        # Read back and verify
        aapl = pd.read_parquet(tmp_path / "equities" / "AAPL.parquet")
        assert len(aapl) == 2
        assert list(aapl["close"]) == [101.0, 102.0]  # sorted by timestamp

    def test_save_crypto_round_trip(self, tmp_path: Path) -> None:
        """Save crypto DataFrame, verify exchange subdirectory."""
        from alpha_harness.data.parquet_store import ParquetStore

        df = pd.DataFrame(
            {
                "symbol": ["BTC/USDT", "BTC/USDT"],
                "timestamp": pd.to_datetime(
                    ["2023-01-01", "2023-01-02"],
                    utc=True,
                ),
                "open": [30000.0, 30100.0],
                "high": [30500.0, 30600.0],
                "low": [29500.0, 29600.0],
                "close": [30200.0, 30300.0],
                "volume": [500.0, 600.0],
                "exchange": ["binance", "binance"],
            }
        )

        store = ParquetStore(str(tmp_path / "crypto"))
        n = store.save_crypto(df, exchange="binance")

        assert n == 1
        expected_path = tmp_path / "crypto" / "binance" / "BTC_USDT.parquet"
        assert expected_path.exists()

        btc = pd.read_parquet(expected_path)
        assert len(btc) == 2

    def test_save_empty_returns_zero(self, tmp_path: Path) -> None:
        """Empty DataFrame produces no files."""
        from alpha_harness.data.parquet_store import ParquetStore

        store = ParquetStore(str(tmp_path / "empty"))
        n = store.save_equities(pd.DataFrame())
        assert n == 0

    def test_equities_to_local_loader_round_trip(self, tmp_path: Path) -> None:
        """Full round trip: save via ParquetStore, load via LocalEquitiesLoader."""
        from alpha_harness.data.equities_loader import LocalEquitiesLoader
        from alpha_harness.data.parquet_store import ParquetStore

        # Create and save
        df = pd.DataFrame(
            {
                "symbol": ["AAPL"] * 3,
                "timestamp": pd.to_datetime(
                    ["2023-01-02", "2023-01-03", "2023-01-04"],
                    utc=True,
                ),
                "open": [100.0, 101.0, 102.0],
                "high": [102.0, 103.0, 104.0],
                "low": [99.0, 100.0, 101.0],
                "close": [101.0, 102.0, 103.0],
                "volume": [1e6, 1.1e6, 1.2e6],
                "vwap": [100.5, 101.5, 102.5],
            }
        )
        outdir = str(tmp_path / "equities")
        ParquetStore(outdir).save_equities(df)

        # Load back
        loader = LocalEquitiesLoader(base_path=outdir)
        req = DataRequest(
            symbols=["AAPL"],
            start=date(2023, 1, 1),
            end=date(2023, 1, 31),
        )
        loaded_df, meta = loader.load_bars(req)

        assert meta.symbols_returned == 1
        assert meta.bars_returned == 3
        assert list(loaded_df["close"]) == [101.0, 102.0, 103.0]


# ── Loader factory ───────────────────────────────────────────────────────────


class TestLoaderFactory:
    def test_create_local_equities(self) -> None:
        from alpha_harness.data.equities_loader import LocalEquitiesLoader
        from alpha_harness.data.loader_factory import create_equities_loader

        loader = create_equities_loader("local", base_path="/tmp/test")
        assert isinstance(loader, LocalEquitiesLoader)

    def test_create_parquet_is_alias_for_local(self) -> None:
        """The CLI / env var spell it ``parquet``; the factory must accept
        both spellings so ``--data-source parquet`` doesn't crash."""
        from alpha_harness.data.equities_loader import LocalEquitiesLoader
        from alpha_harness.data.loader_factory import create_equities_loader

        loader = create_equities_loader("parquet", base_path="/tmp/test")
        assert isinstance(loader, LocalEquitiesLoader)

    def test_create_equities_loader_from_market_pack(self) -> None:
        from alpha_harness.data.equities_loader import LocalEquitiesLoader
        from alpha_harness.data.loader_factory import create_equities_loader
        from alpha_harness.markets import load_market_pack

        loader = create_equities_loader(market_pack=load_market_pack("us_equities_daily"))
        assert isinstance(loader, LocalEquitiesLoader)
        assert loader._base_path == Path("data/silver/equities")

    def test_create_local_crypto(self) -> None:
        from alpha_harness.data.crypto_loader import LocalCryptoLoader
        from alpha_harness.data.loader_factory import create_crypto_loader

        loader = create_crypto_loader("local", base_path="/tmp/test")
        assert isinstance(loader, LocalCryptoLoader)

    def test_create_polygon_equities(self) -> None:
        from alpha_harness.data.loader_factory import create_equities_loader
        from alpha_harness.data.polygon_equities import PolygonEquitiesLoader

        loader = create_equities_loader("polygon", api_key="test_key")
        assert isinstance(loader, PolygonEquitiesLoader)

    def test_create_ccxt_crypto(self) -> None:
        from alpha_harness.data.ccxt_crypto import CcxtCryptoLoader
        from alpha_harness.data.loader_factory import create_crypto_loader

        with patch("alpha_harness.data.ccxt_crypto._create_exchange") as mock:
            mock.return_value = MagicMock()
            loader = create_crypto_loader("ccxt", exchange="binance")
            assert isinstance(loader, CcxtCryptoLoader)

    def test_unknown_source_raises(self) -> None:
        from alpha_harness.data.loader_factory import (
            create_crypto_loader,
            create_equities_loader,
        )

        with pytest.raises(ValueError, match="Unknown equities source"):
            create_equities_loader("bloomberg")

        with pytest.raises(ValueError, match="Unknown crypto source"):
            create_crypto_loader("reuters")
