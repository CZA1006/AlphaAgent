"""Parquet persistence helper — save loader output to local Parquet files.

Bridges API loaders (Polygon, ccxt) and local loaders by writing fetched
DataFrames to the same directory layout that ``LocalEquitiesLoader`` and
``LocalCryptoLoader`` expect.

Layout
------
Equities::

    {base_path}/{symbol}.parquet

Crypto::

    {base_path}/{exchange}/{symbol_filename}.parquet

Each file contains all bars for one symbol, sorted by timestamp.
Existing files are overwritten (append mode is a future enhancement).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)


class ParquetStore:
    """Write DataFrames to local Parquet files for offline use.

    Parameters
    ----------
    base_path:
        Root directory for Parquet files.  Created if it does not exist.
    """

    def __init__(self, base_path: str) -> None:
        self._base_path = Path(base_path)

    def save_equities(self, df: pd.DataFrame) -> int:
        """Save equity bars grouped by symbol.

        Parameters
        ----------
        df:
            DataFrame with at least ``symbol`` and ``timestamp`` columns.
            Typically the output of ``PolygonEquitiesLoader.load_bars()``.

        Returns
        -------
        Number of symbol files written.
        """
        if df.empty or "symbol" not in df.columns:
            return 0

        self._base_path.mkdir(parents=True, exist_ok=True)
        count = 0
        for symbol, group in df.groupby("symbol"):
            path = self._base_path / f"{symbol}.parquet"
            sorted_group = group.sort_values("timestamp").reset_index(drop=True)
            sorted_group.to_parquet(path, index=False)
            logger.info("Wrote %d bars to %s", len(sorted_group), path)
            count += 1
        return count

    def save_crypto(self, df: pd.DataFrame, exchange: str = "") -> int:
        """Save crypto bars grouped by symbol under an exchange directory.

        Parameters
        ----------
        df:
            DataFrame with at least ``symbol``, ``timestamp``, and
            optionally ``exchange`` columns.
        exchange:
            Exchange subdirectory.  If empty, reads from the ``exchange``
            column of the first row, defaulting to ``"unknown"``.

        Returns
        -------
        Number of symbol files written.
        """
        if df.empty or "symbol" not in df.columns:
            return 0

        if not exchange:
            exchange = str(
                df["exchange"].iloc[0] if "exchange" in df.columns else "unknown"
            )

        exchange_dir = self._base_path / exchange
        exchange_dir.mkdir(parents=True, exist_ok=True)
        count = 0
        for symbol, group in df.groupby("symbol"):
            filename = str(symbol).replace("/", "_")
            path = exchange_dir / f"{filename}.parquet"
            sorted_group = group.sort_values("timestamp").reset_index(drop=True)
            sorted_group.to_parquet(path, index=False)
            logger.info("Wrote %d bars to %s", len(sorted_group), path)
            count += 1
        return count
