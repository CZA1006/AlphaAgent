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

import os
from collections.abc import Mapping
from typing import TYPE_CHECKING

from alpha_harness.data.crypto_loader import CryptoLoader, LocalCryptoLoader
from alpha_harness.data.equities_loader import EquitiesLoader, LocalEquitiesLoader
from alpha_harness.markets import MarketPack, list_market_packs, load_market_pack

if TYPE_CHECKING:
    from alpha_harness.data.bigquery_loader import BigQueryTickLoader


def create_equities_loader(
    source: str | None = None,
    *,
    base_path: str | None = None,
    api_key: str | None = None,
    market_pack: MarketPack | None = None,
    market_id: str | None = None,
    with_intraday_features: bool | None = None,
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
    if market_pack is not None and market_id is not None:
        raise ValueError("pass market_pack or market_id, not both")
    pack = market_pack or (load_market_pack(market_id) if market_id else None)
    selected_source = source or (pack.data.loader if pack is not None else "local")

    # "parquet" is the user-facing name (matches the CLI flag and the
    # ALPHA_AGENT_DATA_SOURCE env var); "local" is kept as a historical
    # alias.  Both route to the same Parquet-backed loader.
    if selected_source in ("local", "parquet"):
        resolved_path = base_path or (pack.data.base_path if pack is not None else None)
        return LocalEquitiesLoader(base_path=resolved_path or "data/silver/equities")

    if selected_source == "polygon":
        from alpha_harness.data.polygon_equities import PolygonEquitiesLoader

        return PolygonEquitiesLoader(api_key=api_key)

    if selected_source == "bigquery":
        pack = pack or _unique_pack_for_loader("bigquery")
        from alpha_harness.data.bigquery_loader import BigQueryEquitiesLoader

        data = pack.data
        return BigQueryEquitiesLoader(
            project=_required_location(data.project_env, data.project),
            dataset=_required_location(data.dataset_env, data.dataset),
            prices_table=_required_table(data.tables, "prices"),
            micro_table=_required_table(data.tables, "micro"),
            event_features_table=_required_table(data.tables, "event_features"),
            intraday_table=_required_table(data.tables, "intraday"),
            micro_columns=data.join_columns.get("micro", ()),
            event_feature_columns=data.join_columns.get("event_features", ()),
            intraday_columns=data.join_columns.get("intraday", ()),
            with_micro_features=_bool_kwarg(data.loader_kwargs, "with_micro_features", True),
            with_event_features=_bool_kwarg(data.loader_kwargs, "with_event_features", True),
            with_intraday_features=(
                with_intraday_features
                if with_intraday_features is not None
                else _bool_kwarg(data.loader_kwargs, "with_intraday_features", False)
            ),
            max_bytes_billed=_int_env_or_kwarg(
                "BQ_MAX_BYTES_BILLED", data.loader_kwargs, "max_bytes_billed"
            ),
        )

    msg = (
        f"Unknown equities source: {selected_source!r}. "
        "Use 'parquet' (or 'local') for the local Parquet store, "
        "'polygon' for live US API calls, "
        "or 'bigquery' for a configured BigQuery panel."
    )
    raise ValueError(msg)


def create_bigquery_tick_loader(
    market_pack: MarketPack,
    *,
    scope: str = "target",
) -> BigQueryTickLoader:
    """Build a tick loader from an explicit BigQuery market pack."""
    if market_pack.data.loader != "bigquery":
        raise ValueError(f"market pack {market_pack.market_id!r} is not a BigQuery pack")
    from alpha_harness.data.bigquery_loader import BigQueryTickLoader

    data = market_pack.data
    return BigQueryTickLoader(
        project=_required_location(data.project_env, data.project),
        dataset=_required_location(data.dataset_env, data.dataset),
        tick_table=_required_table(data.tables, "ticks"),
        max_bytes_billed=_int_env_or_kwarg(
            "BQ_TICK_MAX_BYTES_BILLED", data.loader_kwargs, "tick_max_bytes_billed"
        ),
        scope=scope,
    )


def resolve_market_data_location(
    market_pack: MarketPack,
) -> tuple[str, str]:
    """Resolve a pack's project and dataset with its declared env overrides."""
    return (
        _required_location(market_pack.data.project_env, market_pack.data.project),
        _required_location(market_pack.data.dataset_env, market_pack.data.dataset),
    )


def _unique_pack_for_loader(loader: str) -> MarketPack:
    matches = [
        pack
        for market_id in list_market_packs()
        if (pack := load_market_pack(market_id)).data.loader == loader
    ]
    if len(matches) != 1:
        ids = [pack.market_id for pack in matches]
        raise ValueError(f"source {loader!r} requires an explicit market pack; matches={ids}")
    return matches[0]


def _required_location(env_name: str | None, configured: str | None) -> str:
    value = os.environ.get(env_name, "").strip() if env_name else ""
    resolved = value or (configured or "").strip()
    if not resolved:
        raise ValueError(f"market data location is unset (env={env_name!r})")
    return resolved


def _required_table(tables: Mapping[str, str], key: str) -> str:
    value = tables.get(key, "").strip()
    if not value:
        raise ValueError(f"market pack is missing required table {key!r}")
    return value


def _bool_kwarg(kwargs: Mapping[str, str | int | float | bool], key: str, default: bool) -> bool:
    value = kwargs.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"market loader setting {key!r} must be boolean")
    return value


def _int_env_or_kwarg(
    env_name: str,
    kwargs: Mapping[str, str | int | float | bool],
    key: str,
) -> int:
    value: object = os.environ.get(env_name, kwargs.get(key))
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"market loader setting {key!r} must be an integer")
    try:
        result = int(value)
    except ValueError as exc:
        raise ValueError(f"market loader setting {key!r} must be an integer") from exc
    if result <= 0:
        raise ValueError(f"market loader setting {key!r} must be positive")
    return result


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
