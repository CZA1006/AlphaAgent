#!/usr/bin/env python
"""Sample ingestion script — fetch a small slice of market data and save to Parquet.

This script demonstrates how to use the real data adapters (Polygon, ccxt) to
fetch a small sample of data and persist it locally for offline research.

Usage
-----
Equities (requires POLYGON_API_KEY env var)::

    export POLYGON_API_KEY="your_key_here"
    python scripts/sample_ingest.py equities --symbols AAPL,MSFT --days 30

Crypto (no API key needed for public OHLCV)::

    python scripts/sample_ingest.py crypto --symbols BTC/USDT,ETH/USDT --exchange binance --days 30

Both commands save Parquet files under ``data/silver/`` in the layout expected
by ``LocalEquitiesLoader`` and ``LocalCryptoLoader``.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta

from alpha_harness.data.loader_factory import create_crypto_loader, create_equities_loader
from alpha_harness.data.models import DataRequest
from alpha_harness.data.parquet_store import ParquetStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _ingest_equities(symbols: list[str], days: int, out_dir: str) -> None:
    """Fetch equity bars from Polygon and save to Parquet."""
    loader = create_equities_loader("polygon")
    end = date.today()
    start = end - timedelta(days=days)

    request = DataRequest(symbols=symbols, start=start, end=end)
    logger.info(
        "Fetching equities: symbols=%s, range=%s to %s",
        symbols,
        start,
        end,
    )

    df, meta = loader.load_bars(request)
    logger.info(
        "Received %d bars for %d/%d symbols from %s",
        meta.bars_returned,
        meta.symbols_returned,
        meta.symbols_requested,
        meta.source,
    )

    if df.empty:
        logger.warning("No data returned — check your API key and symbols.")
        return

    store = ParquetStore(out_dir)
    n = store.save_equities(df)
    logger.info("Saved %d symbol files to %s", n, out_dir)


def _ingest_crypto(symbols: list[str], exchange: str, days: int, out_dir: str) -> None:
    """Fetch crypto bars from an exchange via ccxt and save to Parquet."""
    loader = create_crypto_loader("ccxt", exchange=exchange)
    end = date.today()
    start = end - timedelta(days=days)

    request = DataRequest(symbols=symbols, start=start, end=end)
    logger.info(
        "Fetching crypto: symbols=%s, exchange=%s, range=%s to %s",
        symbols,
        exchange,
        start,
        end,
    )

    df, meta = loader.load_bars(request)
    logger.info(
        "Received %d bars for %d/%d symbols from %s",
        meta.bars_returned,
        meta.symbols_returned,
        meta.symbols_requested,
        meta.source,
    )

    if df.empty:
        logger.warning("No data returned — check symbol format and exchange.")
        return

    store = ParquetStore(out_dir)
    n = store.save_crypto(df, exchange=exchange)
    logger.info("Saved %d symbol files to %s/%s", n, out_dir, exchange)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch sample market data and save to local Parquet."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # -- equities subcommand --
    eq = sub.add_parser("equities", help="Fetch equity bars from Polygon")
    eq.add_argument(
        "--symbols",
        required=True,
        help="Comma-separated ticker symbols (e.g. AAPL,MSFT)",
    )
    eq.add_argument("--days", type=int, default=30, help="Number of days to fetch")
    eq.add_argument(
        "--out",
        default="data/silver/equities",
        help="Output directory for Parquet files",
    )

    # -- crypto subcommand --
    cr = sub.add_parser("crypto", help="Fetch crypto bars via ccxt")
    cr.add_argument(
        "--symbols",
        required=True,
        help="Comma-separated symbols (e.g. BTC/USDT,ETH/USDT)",
    )
    cr.add_argument("--exchange", default="binance", help="Exchange to fetch from")
    cr.add_argument("--days", type=int, default=30, help="Number of days to fetch")
    cr.add_argument(
        "--out",
        default="data/silver/crypto",
        help="Output directory for Parquet files",
    )

    args = parser.parse_args()

    if args.command == "equities":
        syms = [s.strip() for s in args.symbols.split(",")]
        _ingest_equities(syms, args.days, args.out)
    elif args.command == "crypto":
        syms = [s.strip() for s in args.symbols.split(",")]
        _ingest_crypto(syms, args.exchange, args.days, args.out)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
