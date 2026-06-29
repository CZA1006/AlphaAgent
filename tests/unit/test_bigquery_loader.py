"""Unit tests for the HK IPO BigQuery loader (mocked client — no network)."""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from alpha_harness.data.bigquery_loader import BigQueryEquitiesLoader
from alpha_harness.data.models import BarFrequency, DataRequest


class _FakeQueryJob:
    def __init__(self, df: pd.DataFrame) -> None:
        self._df = df

    def to_dataframe(self) -> pd.DataFrame:
        return self._df


class _FakeBQClient:
    """Captures the query + job_config and returns a canned frame."""

    def __init__(self, df: pd.DataFrame) -> None:
        self._df = df
        self.last_sql: str | None = None
        self.last_job_config: Any = None

    def query(self, sql: str, job_config: Any = None) -> _FakeQueryJob:
        self.last_sql = sql
        self.last_job_config = job_config
        return _FakeQueryJob(self._df)


def _raw_rows() -> pd.DataFrame:
    """Mimic the shape `SELECT date, stock_code, px_open, ... ` returns."""
    return pd.DataFrame(
        {
            "date": [date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 2)],
            "stock_code": ["00068", "00068", "00100"],
            "px_open": [20.7, 19.46, 199.0],
            "px_high": [21.86, 39.5, 205.0],
            "px_low": [16.5, 18.66, 180.0],
            "px_last": [18.6, 37.44, 195.0],
            "volume": [134488211.0, 134303615.0, 5_000_000.0],
            "weighted_avg_px": [19.37, 29.6, None],
        },
    )


def _request() -> DataRequest:
    return DataRequest(
        symbols=["00068", "00100"],
        start=date(2026, 3, 1),
        end=date(2026, 3, 31),
        frequency=BarFrequency.DAILY,
    )


def test_load_bars_maps_columns_to_canonical_panel() -> None:
    client = _FakeBQClient(_raw_rows())
    loader = BigQueryEquitiesLoader(client=client)
    df, _meta = loader.load_bars(_request())

    # Canonical harness columns, in order.
    assert list(df.columns) == [
        "symbol", "timestamp", "open", "high", "low", "close",
        "volume", "vwap", "adjustment", "source", "frequency",
    ]
    # ipo_daily_prices column mapping.
    row = df[(df["symbol"] == "00068") & (df["timestamp"].dt.date == date(2026, 3, 2))].iloc[0]
    assert row["open"] == 20.7
    assert row["high"] == 21.86
    assert row["low"] == 16.5
    assert row["close"] == 18.6          # px_last → close
    assert row["volume"] == 134488211.0
    assert row["vwap"] == 19.37          # weighted_avg_px → vwap
    assert row["source"] == "bigquery"
    assert row["frequency"] == "1d"
    # timestamp is UTC-aware.
    assert str(df["timestamp"].dt.tz) == "UTC"


def test_load_bars_metadata_counts() -> None:
    client = _FakeBQClient(_raw_rows())
    loader = BigQueryEquitiesLoader(client=client)
    _df, meta = loader.load_bars(_request())
    assert meta.symbols_requested == 2
    assert meta.symbols_returned == 2     # 00068 + 00100
    assert meta.bars_returned == 3
    assert meta.source == "bigquery"


def test_query_is_parameterized_and_cost_capped() -> None:
    client = _FakeBQClient(_raw_rows())
    loader = BigQueryEquitiesLoader(client=client, max_bytes_billed=12345)
    loader.load_bars(_request())
    # No raw symbol/date interpolation in the SQL — bound via parameters.
    assert "@symbols" in client.last_sql
    assert "@start" in client.last_sql and "@end" in client.last_sql
    assert "00068" not in client.last_sql
    # Cost guard threaded through.
    assert client.last_job_config.maximum_bytes_billed == 12345


def test_empty_result_returns_well_formed_empty_panel() -> None:
    empty = _raw_rows().iloc[0:0]
    client = _FakeBQClient(empty)
    loader = BigQueryEquitiesLoader(client=client)
    df, meta = loader.load_bars(_request())
    assert df.empty
    assert list(df.columns) == [
        "symbol", "timestamp", "open", "high", "low", "close",
        "volume", "vwap", "adjustment", "source", "frequency",
    ]
    assert meta.symbols_returned == 0
    assert meta.bars_returned == 0


def test_nullable_vwap_survives_as_nan() -> None:
    client = _FakeBQClient(_raw_rows())
    loader = BigQueryEquitiesLoader(client=client)
    df, _meta = loader.load_bars(_request())
    vwap_00100 = df[df["symbol"] == "00100"]["vwap"].iloc[0]
    assert pd.isna(vwap_00100)


def test_factory_returns_bigquery_loader() -> None:
    from alpha_harness.data.loader_factory import create_equities_loader

    loader = create_equities_loader(source="bigquery")
    assert isinstance(loader, BigQueryEquitiesLoader)


# ── microstructure feature join (Track B) ──────────────────────────────────


def _raw_rows_with_micro() -> pd.DataFrame:
    df = _raw_rows()
    # Micro columns as the LEFT JOIN would return them (one row has no
    # tick coverage → NaN, exercising the nullable path).
    df["ofi"] = [0.05, -0.02, None]
    df["rel_spread"] = [0.002, 0.003, None]
    df["realized_vol"] = [0.08, 0.06, None]
    df["n_trades"] = [114674, 90000, None]
    df["tick_volume"] = [36_762_009, 20_000_000, None]
    df["avg_trade_size"] = [321.0, 222.0, None]
    df["n_quotes"] = [60000, 40000, None]
    return df


def test_micro_features_pass_through_as_dsl_fields() -> None:
    client = _FakeBQClient(_raw_rows_with_micro())
    loader = BigQueryEquitiesLoader(client=client, with_micro_features=True)
    df, _meta = loader.load_bars(_request())

    # OHLCV columns first, micro columns appended.
    for col in ("ofi", "rel_spread", "realized_vol", "n_trades",
                "tick_volume", "avg_trade_size", "n_quotes"):
        assert col in df.columns
        assert pd.api.types.is_float_dtype(df[col])
    row = df[df["symbol"] == "00068"].iloc[0]
    assert row["ofi"] == 0.05
    assert row["realized_vol"] == 0.08
    # The no-coverage row keeps NaN (evaluator will skip it).
    assert pd.isna(df[df["symbol"] == "00100"]["ofi"].iloc[0])


def test_with_micro_features_false_uses_price_only_query() -> None:
    client = _FakeBQClient(_raw_rows())
    loader = BigQueryEquitiesLoader(client=client, with_micro_features=False)
    loader.load_bars(_request())
    # No join to the micro table in the SQL.
    assert "micro_features_daily" not in client.last_sql
    assert "LEFT JOIN" not in client.last_sql


def test_micro_query_joins_micro_table_when_enabled() -> None:
    client = _FakeBQClient(_raw_rows_with_micro())
    loader = BigQueryEquitiesLoader(client=client, with_micro_features=True)
    loader.load_bars(_request())
    assert "micro_features_daily" in client.last_sql
    assert "LEFT JOIN" in client.last_sql


def test_micro_fields_compile_in_dsl() -> None:
    """The microstructure field names are whitelisted in the DSL."""
    from alpha_harness.factors.dsl_parser import ALLOWED_FIELDS, parse_expression

    for f in ("ofi", "rel_spread", "realized_vol", "n_trades",
              "tick_volume", "avg_trade_size", "n_quotes"):
        assert f in ALLOWED_FIELDS
    # A real microstructure factor must parse.
    parse_expression("rank(ofi) * rank(-realized_vol)")
