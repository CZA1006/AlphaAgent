"""Loader factory — configurable data source selection.

Provides a single function to instantiate the correct loader based on a
source name string. This keeps source selection out of business logic.

Usage::

    from alpha_harness.data.loader_factory import create_equities_loader, create_crypto_loader

    equities = create_equities_loader("polygon")          # requires POLYGON_API_KEY
    equities = create_equities_loader("local")             # reads from Parquet
    crypto   = create_crypto_loader("ccxt", exchange="binance")
    crypto   = create_crypto_loader("local")               # reads from Parquet
"""

from __future__ import annotations

from alpha_harness.data.crypto_loader import CryptoLoader, LocalCryptoLoader
from alpha_harness.data.equities_loader import EquitiesLoader, LocalEquitiesLoader


def create_equities_loader(
    source: str = "local",
    *,
    base_path: str = "data/silver/equities",
    api_key: str | None = None,
) -> EquitiesLoader:
    """Create an equities loader for the given source.

    Parameters
    ----------
    source:
        ``"local"`` for Parquet files, ``"polygon"`` for Polygon.io API.
    base_path:
        Directory for local Parquet files (ignored for ``"polygon"``).
    api_key:
        Polygon API key.  Falls back to ``POLYGON_API_KEY`` env var.

    Raises
    ------
    ValueError:
        If ``source`` is not a recognised loader name.
    """
    # "parquet" is the user-facing name (matches the CLI flag and the
    # ALPHA_AGENT_DATA_SOURCE env var); "local" is kept as a historical
    # alias.  Both route to the same Parquet-backed loader.
    if source in ("local", "parquet"):
        return LocalEquitiesLoader(base_path=base_path)

    if source == "polygon":
        from alpha_harness.data.polygon_equities import PolygonEquitiesLoader

        return PolygonEquitiesLoader(api_key=api_key)

    if source == "bigquery":
        # HK IPO daily panel from BigQuery (project bloomberg-database-0629,
        # table ipo_daily_prices).  Imported lazily so the GCP SDK is only
        # required when this source is actually selected.
        from alpha_harness.data.bigquery_loader import BigQueryEquitiesLoader

        return BigQueryEquitiesLoader()

    msg = (
        f"Unknown equities source: {source!r}. "
        "Use 'parquet' (or 'local') for the local Parquet store, "
        "'polygon' for live US API calls, "
        "or 'bigquery' for the HK IPO daily panel."
    )
    raise ValueError(msg)


def create_crypto_loader(
    source: str = "local",
    *,
    base_path: str = "data/silver/crypto",
    exchange: str = "binance",
    exchange_config: dict[str, object] | None = None,
) -> CryptoLoader:
    """Create a crypto loader for the given source.

    Parameters
    ----------
    source:
        ``"local"`` for Parquet files, ``"ccxt"`` for live exchange data.
    base_path:
        Directory for local Parquet files (ignored for ``"ccxt"``).
    exchange:
        Exchange identifier (e.g. ``"binance"``, ``"coinbase"``).
    exchange_config:
        Optional dict passed to the ccxt exchange constructor.

    Raises
    ------
    ValueError:
        If ``source`` is not a recognised loader name.
    """
    if source == "local":
        return LocalCryptoLoader(base_path=base_path, default_exchange=exchange)

    if source == "ccxt":
        from alpha_harness.data.ccxt_crypto import CcxtCryptoLoader

        return CcxtCryptoLoader(
            exchange_id=exchange,
            exchange_config=exchange_config,
        )

    msg = f"Unknown crypto source: {source!r}. Use 'local' or 'ccxt'."
    raise ValueError(msg)
