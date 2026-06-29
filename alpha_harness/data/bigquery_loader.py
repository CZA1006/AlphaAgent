"""BigQuery-backed equities loader for the HK IPO dataset.

Reads the ``ipo_daily_prices`` table (project ``bloomberg-database-0629``,
dataset ``hk_ipo_research``) and maps it onto the harness's canonical
OHLCV panel so the existing factor engine can run unchanged on HK IPO
names.

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
import os
from typing import TYPE_CHECKING, Any

import pandas as pd

from alpha_harness.data.models import AdjustmentType, DataRequest, DataResult

if TYPE_CHECKING:  # pragma: no cover — typing only
    from google.cloud import bigquery

logger = logging.getLogger(__name__)

# Defaults for the HK IPO research dataset.  Overridable via env / ctor so
# the loader isn't hard-wired to one project.
DEFAULT_PROJECT = os.environ.get("GCP_PROJECT", "bloomberg-database-0629")
DEFAULT_DATASET = os.environ.get("HK_IPO_DATASET", "hk_ipo_research")
DEFAULT_PRICES_TABLE = "ipo_daily_prices"
# 1 GiB scan cap — ipo_daily_prices is far smaller; this is a runaway guard.
DEFAULT_MAX_BYTES_BILLED = int(os.environ.get("BQ_MAX_BYTES_BILLED", 1_073_741_824))

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


class BigQueryEquitiesLoader:
    """Load HK IPO daily bars from BigQuery ``ipo_daily_prices``.

    Conforms to the :class:`~alpha_harness.data.equities_loader.EquitiesLoader`
    protocol: ``load_bars(request, adjustment) -> (DataFrame, DataResult)``.
    """

    def __init__(
        self,
        *,
        project: str = DEFAULT_PROJECT,
        dataset: str = DEFAULT_DATASET,
        prices_table: str = DEFAULT_PRICES_TABLE,
        max_bytes_billed: int = DEFAULT_MAX_BYTES_BILLED,
        client: Any | None = None,
    ) -> None:
        self._project = project
        self._dataset = dataset
        self._table = prices_table
        self._max_bytes_billed = max_bytes_billed
        # Injectable for tests; lazily constructed in production so importing
        # this module never requires credentials.
        self._client = client

    # ── client ───────────────────────────────────────────────────────────

    def _get_client(self) -> bigquery.Client:
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
        """Load HK IPO daily OHLCV for the requested symbols + date range."""
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
        from google.cloud import bigquery

        client = self._get_client()
        fq_table = f"`{self._project}.{self._dataset}.{self._table}`"
        select_cols = ", ".join(_COLUMN_MAP.keys())
        # Parameterized — symbols + dates bound, never string-interpolated.
        sql = (
            f"SELECT date, {select_cols} FROM {fq_table} "
            "WHERE date BETWEEN @start AND @end "
            "AND stock_code IN UNNEST(@symbols) "
            "ORDER BY stock_code, date"
        )
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("start", "DATE", request.start),
                bigquery.ScalarQueryParameter("end", "DATE", request.end),
                bigquery.ArrayQueryParameter("symbols", "STRING", list(request.symbols)),
            ],
            maximum_bytes_billed=self._max_bytes_billed,
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
        """Rename to canonical columns + attach timestamp/provenance."""
        cols = ["symbol", "timestamp", "open", "high", "low", "close",
                "volume", "vwap", "adjustment", "source", "frequency"]
        if raw.empty:
            return pd.DataFrame(columns=cols)

        df = raw.rename(columns=_COLUMN_MAP)
        # ``date`` (a BQ DATE) → bar-close UTC timestamp, matching the
        # parquet loader's convention (timestamp = end of bar).
        df["timestamp"] = pd.to_datetime(df["date"], utc=True)
        df = df.drop(columns=["date"])
        # Numeric hygiene: BQ NUMERIC/FLOAT come back as object/Decimal at
        # times; coerce the price/volume columns to float.
        for c in ("open", "high", "low", "close", "volume", "vwap"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["symbol"] = df["symbol"].astype(str)
        df["adjustment"] = adjustment.value
        df["source"] = "bigquery"
        df["frequency"] = request.frequency.value
        return df[cols].reset_index(drop=True)
