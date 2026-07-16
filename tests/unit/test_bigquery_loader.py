"""Unit tests for the HK IPO BigQuery loader (mocked client — no network)."""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from alpha_harness.data.bigquery_loader import BigQueryEquitiesLoader, BigQueryTickLoader
from alpha_harness.data.models import BarFrequency, DataRequest


class _FakeQueryJob:
    def __init__(self, df: pd.DataFrame) -> None:
        self._df = df

    def to_dataframe(self) -> pd.DataFrame:
        return self._df


class _FakeBQClient:
    """Captures the query + job_config and returns a canned frame."""

    use_lightweight_query_config = True

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
        "frequency",
    ]
    # ipo_daily_prices column mapping.
    row = df[(df["symbol"] == "00068") & (df["timestamp"].dt.date == date(2026, 3, 2))].iloc[0]
    assert row["open"] == 20.7
    assert row["high"] == 21.86
    assert row["low"] == 16.5
    assert row["close"] == 18.6  # px_last → close
    assert row["volume"] == 134488211.0
    assert row["vwap"] == 19.37  # weighted_avg_px → vwap
    assert row["source"] == "bigquery"
    assert row["frequency"] == "1d"
    # timestamp is UTC-aware.
    assert str(df["timestamp"].dt.tz) == "UTC"


def test_load_bars_metadata_counts() -> None:
    client = _FakeBQClient(_raw_rows())
    loader = BigQueryEquitiesLoader(client=client)
    _df, meta = loader.load_bars(_request())
    assert meta.symbols_requested == 2
    assert meta.symbols_returned == 2  # 00068 + 00100
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
        "frequency",
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
    for col in (
        "ofi",
        "rel_spread",
        "realized_vol",
        "n_trades",
        "tick_volume",
        "avg_trade_size",
        "n_quotes",
    ):
        assert col in df.columns
        assert pd.api.types.is_float_dtype(df[col])
    row = df[df["symbol"] == "00068"].iloc[0]
    assert row["ofi"] == 0.05
    assert row["realized_vol"] == 0.08
    # The no-coverage row keeps NaN (evaluator will skip it).
    assert pd.isna(df[df["symbol"] == "00100"]["ofi"].iloc[0])


def test_with_micro_features_false_uses_price_only_query() -> None:
    client = _FakeBQClient(_raw_rows())
    loader = BigQueryEquitiesLoader(
        client=client,
        with_micro_features=False,
        with_event_features=False,
    )
    loader.load_bars(_request())
    # No join to the micro table in the SQL.
    assert "micro_features_daily" not in client.last_sql
    assert "LEFT JOIN" not in client.last_sql


def test_micro_query_joins_micro_table_when_enabled() -> None:
    client = _FakeBQClient(_raw_rows_with_micro())
    loader = BigQueryEquitiesLoader(
        client=client,
        with_micro_features=True,
        with_event_features=False,
    )
    loader.load_bars(_request())
    assert "micro_features_daily" in client.last_sql
    assert "LEFT JOIN" in client.last_sql


def _raw_rows_with_event_features() -> pd.DataFrame:
    df = _raw_rows()
    df["days_since_listing"] = [1, 2, 1]
    df["days_since_pricing"] = [5, 6, 4]
    df["days_to_next_cornerstone_lockup"] = [179, 178, None]
    df["next_cornerstone_unlock_pct_offer"] = [0.21, 0.21, None]
    df["days_to_next_greenshoe_expiry"] = [28, 27, 20]
    df["days_to_next_stabilization_end"] = [27, 26, 19]
    df["is_pre_greenshoe_expiry_5d"] = [0, 0, 0]
    df["is_pre_cornerstone_lockup_5d"] = [0, 0, 0]
    df["is_stabilization_window_active"] = [1, 1, 1]
    return df


def test_event_features_pass_through_as_dsl_fields() -> None:
    client = _FakeBQClient(_raw_rows_with_event_features())
    loader = BigQueryEquitiesLoader(
        client=client,
        with_micro_features=False,
        with_event_features=True,
    )
    df, _meta = loader.load_bars(_request())

    for col in (
        "days_since_listing",
        "days_since_pricing",
        "days_to_next_cornerstone_lockup",
        "next_cornerstone_unlock_pct_offer",
        "days_to_next_greenshoe_expiry",
        "days_to_next_stabilization_end",
        "is_pre_greenshoe_expiry_5d",
        "is_pre_cornerstone_lockup_5d",
        "is_stabilization_window_active",
    ):
        assert col in df.columns
        assert pd.api.types.is_numeric_dtype(df[col])
    assert "ipo_event_features_daily" in client.last_sql


def test_micro_fields_compile_in_dsl() -> None:
    """The microstructure field names are whitelisted in the DSL."""
    from alpha_harness.factors.dsl_parser import ALLOWED_FIELDS, parse_expression

    for f in (
        "ofi",
        "rel_spread",
        "realized_vol",
        "n_trades",
        "tick_volume",
        "avg_trade_size",
        "n_quotes",
    ):
        assert f in ALLOWED_FIELDS
    # A real microstructure factor must parse.
    parse_expression("rank(ofi) * rank(-realized_vol)")


def test_event_fields_compile_in_dsl() -> None:
    """The curated IPO event feature names are whitelisted in the DSL."""
    from alpha_harness.factors.dsl_parser import ALLOWED_FIELDS, parse_expression

    for f in (
        "days_since_listing",
        "days_since_pricing",
        "days_to_next_cornerstone_lockup",
        "next_cornerstone_unlock_pct_offer",
        "days_to_next_greenshoe_expiry",
        "days_to_next_stabilization_end",
        "is_pre_greenshoe_expiry_5d",
        "is_pre_cornerstone_lockup_5d",
        "is_stabilization_window_active",
    ):
        assert f in ALLOWED_FIELDS
    parse_expression("rank(ofi) * is_pre_greenshoe_expiry_5d")


# ── tick loader ────────────────────────────────────────────────────────────


def _tick_request() -> DataRequest:
    return DataRequest(
        symbols=["00068"],
        start=date(2026, 3, 2),
        end=date(2026, 3, 2),
        frequency=BarFrequency.TICK,
    )


def _raw_tick_rows() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": ["00068", "00068", "00068"],
            "timestamp": [
                "2026-03-02T01:30:00Z",
                "2026-03-02T01:30:01Z",
                "2026-03-02T01:30:02Z",
            ],
            "event_type": ["BID", "ASK", "TRADE"],
            "price": ["18.50", "18.54", "18.52"],
            "size": [1000, 1000, 500],
            "condition_codes": [None, None, "XT"],
            "exchange_code": ["HK", "HK", "HK"],
            "trade_time": [None, None, "2026-03-02T09:30:02+08:00"],
            "hk_time": [
                "2026-03-02T09:30:00+08:00",
                "2026-03-02T09:30:01+08:00",
                "2026-03-02T09:30:02+08:00",
            ],
            "trading_date": [date(2026, 3, 2)] * 3,
            "scope": ["target"] * 3,
        },
    )


def test_tick_loader_maps_raw_bid_ask_trade_events() -> None:
    client = _FakeBQClient(_raw_tick_rows())
    loader = BigQueryTickLoader(client=client)
    df, meta = loader.load_ticks(_tick_request())

    assert list(df["event_type"]) == ["BID", "ASK", "TRADE"]
    assert pd.api.types.is_float_dtype(df["price"])
    assert pd.api.types.is_numeric_dtype(df["size"])
    assert str(df["timestamp"].dt.tz) == "UTC"
    assert df["source"].iloc[0] == "bigquery:tick_events_ext"
    assert meta.symbols_returned == 1
    assert meta.bars_returned == 3


def test_tick_query_is_parameterized_scope_filtered_and_cost_capped() -> None:
    client = _FakeBQClient(_raw_tick_rows())
    loader = BigQueryTickLoader(client=client, max_bytes_billed=123456)
    loader.load_ticks(_tick_request(), event_types=("TRADE",), limit=10)

    assert "@scope" in client.last_sql
    assert "@symbols" in client.last_sql
    assert "@event_types" in client.last_sql
    assert "scope = @scope" in client.last_sql
    assert "00068" not in client.last_sql
    assert "LIMIT 10" in client.last_sql
    assert client.last_job_config.maximum_bytes_billed == 123456


def test_tick_loader_requires_tick_frequency() -> None:
    client = _FakeBQClient(_raw_tick_rows())
    loader = BigQueryTickLoader(client=client)
    daily_request = DataRequest(
        symbols=["00068"],
        start=date(2026, 3, 2),
        end=date(2026, 3, 2),
        frequency=BarFrequency.DAILY,
    )

    try:
        loader.load_ticks(daily_request)
    except ValueError as exc:
        assert "frequency='tick'" in str(exc)
    else:
        raise AssertionError("expected ValueError for non-tick request")


# ── Intraday v1 candidate features (opt-in) ─────────────────────────────────


def _raw_rows_with_intraday() -> pd.DataFrame:
    df = _raw_rows()
    df["first_hour_ofi"] = [0.12, -0.30, None]
    df["first_hour_rel_spread"] = [0.004, 0.006, None]
    df["opening_auction_trade_share"] = [0.35, 0.10, None]
    return df


def test_intraday_features_off_by_default() -> None:
    client = _FakeBQClient(_raw_rows())
    loader = BigQueryEquitiesLoader(client=client)
    df, _ = loader.load_bars(_request())
    assert client.last_sql is not None
    assert "micro_features_intraday_v1_candidate" not in client.last_sql
    assert "first_hour_ofi" not in df.columns


def test_intraday_query_joins_candidate_table_when_enabled() -> None:
    client = _FakeBQClient(_raw_rows_with_intraday())
    loader = BigQueryEquitiesLoader(
        client=client,
        with_micro_features=False,
        with_event_features=False,
        with_intraday_features=True,
    )
    loader.load_bars(_request())
    assert client.last_sql is not None
    assert "micro_features_intraday_v1_candidate" in client.last_sql
    assert "i.first_hour_ofi" in client.last_sql
    assert "p.date = i.trading_date" in client.last_sql


def test_intraday_features_pass_through_as_dsl_fields() -> None:
    client = _FakeBQClient(_raw_rows_with_intraday())
    loader = BigQueryEquitiesLoader(
        client=client,
        with_micro_features=False,
        with_event_features=False,
        with_intraday_features=True,
    )
    df, _ = loader.load_bars(_request())
    for col in ("first_hour_ofi", "first_hour_rel_spread", "opening_auction_trade_share"):
        assert col in df.columns
        assert df[col].dtype.kind == "f"  # numeric, NULL -> NaN
    assert df["first_hour_ofi"].isna().sum() == 1
