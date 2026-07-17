"""BigQuery-backed equities and tick loaders.

Reads pack-configured tables and maps them onto the harness's canonical
panels so the existing factor engine can run unchanged.

Column mapping (``ipo_daily_prices`` → harness):

    stock_code        → symbol
    date              → timestamp (UTC, bar-close convention)
    px_open           → open
    px_high           → high
    px_low            → low
    px_last           → close
    volume            → volume
    weighted_avg_px   → vwap

Cost discipline:
    BigQuery bills on bytes scanned.  Every query is parameterized
    (symbols + date range) and capped with ``maximum_bytes_billed`` so a
    runaway scan fails loudly instead of billing.  ``ipo_daily_prices``
    is small (one row per IPO per trading day), so a filtered pull is
    a few MB at most.

Auth:
    Uses Application Default Credentials (``gcloud auth
    application-default login``) or ``GOOGLE_APPLICATION_CREDENTIALS``.
    No credentials are embedded here.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, Protocol

import pandas as pd

from alpha_harness.data.models import AdjustmentType, BarFrequency, DataRequest, DataResult


class _QueryResult(Protocol):
    def to_dataframe(self) -> pd.DataFrame: ...


class _BigQueryClient(Protocol):
    def query(self, query: str, *, job_config: Any) -> _QueryResult: ...


logger = logging.getLogger(__name__)

# ipo_daily_prices → canonical panel column names.
_COLUMN_MAP = {
    "stock_code": "symbol",
    "px_open": "open",
    "px_high": "high",
    "px_low": "low",
    "px_last": "close",
    "volume": "volume",
    "weighted_avg_px": "vwap",
}


def _make_job_config(
    client: Any,
    specs: list[tuple[str, str, Any, str]],
    *,
    max_bytes_billed: int,
) -> Any:
    """Build a BigQuery QueryJobConfig, with a lightweight test fallback."""
    if getattr(client, "use_lightweight_query_config", False):
        return SimpleNamespace(
            query_parameters=[
                SimpleNamespace(name=name, type_=type_, value=value, mode=mode)
                for name, type_, value, mode in specs
            ],
            maximum_bytes_billed=max_bytes_billed,
        )

    from google.cloud import bigquery

    params: list[bigquery.ArrayQueryParameter | bigquery.ScalarQueryParameter] = []
    for name, type_, value, mode in specs:
        if mode == "array":
            params.append(bigquery.ArrayQueryParameter(name, type_, value))
        else:
            params.append(bigquery.ScalarQueryParameter(name, type_, value))
    return bigquery.QueryJobConfig(
        query_parameters=params,
        maximum_bytes_billed=max_bytes_billed,
    )


class BigQueryEquitiesLoader:
    """Load daily bars from pack-configured BigQuery tables.

    Conforms to the :class:`~alpha_harness.data.equities_loader.EquitiesLoader`
    protocol: ``load_bars(request, adjustment) -> (DataFrame, DataResult)``.
    """

    def __init__(
        self,
        *,
        project: str,
        dataset: str,
        prices_table: str,
        micro_table: str,
        event_features_table: str,
        intraday_table: str,
        micro_columns: tuple[str, ...],
        event_feature_columns: tuple[str, ...],
        intraday_columns: tuple[str, ...],
        max_bytes_billed: int,
        with_micro_features: bool = True,
        with_event_features: bool = True,
        with_intraday_features: bool = False,
        client: _BigQueryClient | None = None,
    ) -> None:
        self._project = project
        self._dataset = dataset
        self._table = prices_table
        self._micro_table = micro_table
        self._event_features_table = event_features_table
        self._intraday_table = intraday_table
        self._micro_columns = micro_columns
        self._event_feature_columns = event_feature_columns
        self._intraday_columns = intraday_columns
        self._with_micro = with_micro_features
        self._with_event_features = with_event_features
        self._with_intraday = with_intraday_features
        self._max_bytes_billed = max_bytes_billed
        # Injectable for tests; lazily constructed in production so importing
        # this module never requires credentials.
        self._client = client

    # ── client ───────────────────────────────────────────────────────────

    def _get_client(self) -> _BigQueryClient:
        if self._client is None:
            try:
                from google.cloud import bigquery
            except ImportError as exc:  # pragma: no cover — env guard
                raise RuntimeError(
                    "google-cloud-bigquery is not installed. "
                    "Install the GCP extra: uv sync --extra gcp",
                ) from exc
            self._client = bigquery.Client(project=self._project)
        return self._client

    # ── public API ───────────────────────────────────────────────────────

    def load_bars(
        self,
        request: DataRequest,
        adjustment: AdjustmentType = AdjustmentType.SPLIT_AND_DIVIDEND,
    ) -> tuple[pd.DataFrame, DataResult]:
        """Load daily OHLCV for the requested symbols and date range."""
        df = self._query(request)
        df = self._to_panel(df, request, adjustment)

        symbols_returned = int(df["symbol"].nunique()) if len(df) else 0
        metadata = DataResult(
            symbols_requested=len(request.symbols),
            symbols_returned=symbols_returned,
            bars_returned=len(df),
            start=request.start,
            end=request.end,
            source="bigquery",
        )
        return df, metadata

    # ── internals ────────────────────────────────────────────────────────

    def _query(self, request: DataRequest) -> pd.DataFrame:
        client = self._get_client()
        fq_prices = f"`{self._project}.{self._dataset}.{self._table}`"
        price_cols = ", ".join(f"p.{c}" for c in _COLUMN_MAP)
        select_cols = [f"p.date, {price_cols}"]
        joins: list[str] = []
        # Parameterized — symbols + dates bound, never string-interpolated.
        if self._with_micro:
            fq_micro = f"`{self._project}.{self._dataset}.{self._micro_table}`"
            micro_cols = ", ".join(f"m.{c}" for c in self._micro_columns)
            select_cols.append(micro_cols)
            joins.append(
                f"LEFT JOIN {fq_micro} m "
                "ON p.stock_code = m.stock_code AND p.date = m.trading_date",
            )
        if self._with_event_features:
            fq_events = f"`{self._project}.{self._dataset}.{self._event_features_table}`"
            event_cols = ", ".join(f"ef.{c}" for c in self._event_feature_columns)
            select_cols.append(event_cols)
            joins.append(
                f"LEFT JOIN {fq_events} ef ON p.stock_code = ef.stock_code AND p.date = ef.date",
            )
        if self._with_intraday:
            fq_intraday = f"`{self._project}.{self._dataset}.{self._intraday_table}`"
            intraday_cols = ", ".join(f"i.{c}" for c in self._intraday_columns)
            select_cols.append(intraday_cols)
            joins.append(
                f"LEFT JOIN {fq_intraday} i "
                "ON p.stock_code = i.stock_code AND p.date = i.trading_date",
            )
        sql = (
            f"SELECT {', '.join(select_cols)} "
            f"FROM {fq_prices} p "
            f"{' '.join(joins)} "
            "WHERE p.date BETWEEN @start AND @end "
            "AND p.stock_code IN UNNEST(@symbols) "
            "ORDER BY p.stock_code, p.date"
        )
        job_config = _make_job_config(
            client,
            [
                ("start", "DATE", request.start, "scalar"),
                ("end", "DATE", request.end, "scalar"),
                ("symbols", "STRING", list(request.symbols), "array"),
            ],
            max_bytes_billed=self._max_bytes_billed,
        )
        logger.info(
            "BigQuery load: %d symbols, %s..%s from %s",
            len(request.symbols),
            request.start,
            request.end,
            self._table,
        )
        return client.query(sql, job_config=job_config).to_dataframe()

    def _to_panel(
        self,
        raw: pd.DataFrame,
        request: DataRequest,
        adjustment: AdjustmentType,
    ) -> pd.DataFrame:
        """Rename to canonical columns + attach timestamp/provenance.

        When micro features were joined in, they ride through as extra
        DSL-addressable columns appended after ``frequency``.
        """
        base_cols = [
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
        micro_present = [c for c in self._micro_columns if c in raw.columns]
        event_present = [c for c in self._event_feature_columns if c in raw.columns]
        intraday_present = [c for c in self._intraday_columns if c in raw.columns]
        cols = base_cols + micro_present + event_present + intraday_present
        if raw.empty:
            return pd.DataFrame(columns=cols)

        df = raw.rename(columns=_COLUMN_MAP)
        # ``date`` (a BQ DATE) → bar-close UTC timestamp, matching the
        # parquet loader's convention (timestamp = end of bar).
        df["timestamp"] = pd.to_datetime(df["date"], utc=True)
        df = df.drop(columns=["date"])
        # Numeric hygiene: BQ NUMERIC/FLOAT come back as object/Decimal at
        # times; coerce the price/volume + micro columns to float.
        for c in (
            "open",
            "high",
            "low",
            "close",
            "volume",
            "vwap",
            *micro_present,
            *event_present,
            *intraday_present,
        ):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["symbol"] = df["symbol"].astype(str)
        df["adjustment"] = adjustment.value
        df["source"] = "bigquery"
        df["frequency"] = request.frequency.value
        return df[cols].reset_index(drop=True)


class BigQueryTickLoader:
    """Load TRADE/BID/ASK tick events from a pack-configured BigQuery table."""

    def __init__(
        self,
        *,
        project: str,
        dataset: str,
        tick_table: str,
        max_bytes_billed: int,
        scope: str = "target",
        client: _BigQueryClient | None = None,
    ) -> None:
        self._project = project
        self._dataset = dataset
        self._tick_table = tick_table
        self._scope = scope
        self._max_bytes_billed = max_bytes_billed
        self._client = client

    def _get_client(self) -> _BigQueryClient:
        if self._client is None:
            try:
                from google.cloud import bigquery
            except ImportError as exc:  # pragma: no cover — env guard
                raise RuntimeError(
                    "google-cloud-bigquery is not installed. "
                    "Install the GCP extra: uv sync --extra gcp",
                ) from exc
            self._client = bigquery.Client(project=self._project)
        return self._client

    def load_ticks(
        self,
        request: DataRequest,
        *,
        event_types: tuple[str, ...] = ("TRADE", "BID", "ASK"),
        limit: int | None = None,
    ) -> tuple[pd.DataFrame, DataResult]:
        if request.frequency != BarFrequency.TICK:
            raise ValueError("BigQueryTickLoader requires DataRequest.frequency='tick'")
        if not event_types:
            raise ValueError("event_types must be non-empty")
        if limit is not None and limit <= 0:
            raise ValueError("limit must be positive when provided")

        raw = self._query(request, event_types=event_types, limit=limit)
        df = self._to_ticks(raw)
        symbols_returned = int(df["symbol"].nunique()) if len(df) else 0
        metadata = DataResult(
            symbols_requested=len(request.symbols),
            symbols_returned=symbols_returned,
            bars_returned=len(df),
            start=request.start,
            end=request.end,
            source=f"bigquery:{self._tick_table}",
        )
        return df, metadata

    def _query(
        self,
        request: DataRequest,
        *,
        event_types: tuple[str, ...],
        limit: int | None,
    ) -> pd.DataFrame:
        client = self._get_client()
        fq_ticks = f"`{self._project}.{self._dataset}.{self._tick_table}`"
        limit_sql = f" LIMIT {int(limit)}" if limit is not None else ""
        sql = (
            "SELECT "
            "stock_code AS symbol, "
            "time AS timestamp, "
            "event_type, "
            "value AS price, "
            "size, "
            "conditionCodes AS condition_codes, "
            "exchangeCode AS exchange_code, "
            "tradeTime AS trade_time, "
            "hk_time, "
            "trading_date, "
            "scope "
            f"FROM {fq_ticks} "
            "WHERE scope = @scope "
            "AND trading_date BETWEEN @start AND @end "
            "AND stock_code IN UNNEST(@symbols) "
            "AND event_type IN UNNEST(@event_types) "
            "ORDER BY stock_code, time"
            f"{limit_sql}"
        )
        job_config = _make_job_config(
            client,
            [
                ("scope", "STRING", self._scope, "scalar"),
                ("start", "DATE", request.start, "scalar"),
                ("end", "DATE", request.end, "scalar"),
                ("symbols", "STRING", list(request.symbols), "array"),
                ("event_types", "STRING", list(event_types), "array"),
            ],
            max_bytes_billed=self._max_bytes_billed,
        )
        logger.info(
            "BigQuery tick load: %d symbols, %s..%s from %s",
            len(request.symbols),
            request.start,
            request.end,
            self._tick_table,
        )
        return client.query(sql, job_config=job_config).to_dataframe()

    def _to_ticks(self, raw: pd.DataFrame) -> pd.DataFrame:
        cols = [
            "symbol",
            "timestamp",
            "event_type",
            "price",
            "size",
            "condition_codes",
            "exchange_code",
            "trade_time",
            "hk_time",
            "trading_date",
            "scope",
            "source",
        ]
        if raw.empty:
            return pd.DataFrame(columns=cols)
        df = raw.copy()
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        for c in ("price", "size"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["symbol"] = df["symbol"].astype(str)
        df["event_type"] = df["event_type"].astype(str)
        df["source"] = f"bigquery:{self._tick_table}"
        return df[cols].reset_index(drop=True)
