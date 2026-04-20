"""Backfill a local Parquet equity store from Polygon.io.

Round 4A.2 — give the harness a real research universe so cycles can
run on ≥50 symbols without hammering Polygon on every iteration.

Usage
-----
::

    uv run python -m scripts.backfill_parquet \
        --universe configs/universes/sp50.txt \
        --start-date 2023-01-01 \
        --end-date   2025-04-01

Or the Makefile shortcut::

    make backfill-sp50

Behavior
--------
* Reads a plain-text universe file (one ticker per line, ``#`` comments
  and blank lines ignored).
* For each symbol, checks whether a Parquet file already covers the
  requested date range.  If so, the symbol is **skipped** — backfills
  are idempotent and resumable after interruption.
* Fetches missing symbols one at a time via
  :class:`PolygonEquitiesLoader`, which enforces the ``POLYGON_RPM``
  rate limit and handles 429 retries (shipped in Round 4A.1).
* Writes results via :class:`ParquetStore` to
  ``data/silver/equities/{symbol}.parquet`` — the exact layout
  :class:`LocalEquitiesLoader` expects.

Exit codes
----------
``0``  all requested symbols are present after the run (cached or fetched).
``1``  argparse / I/O error before any fetch.
``2``  at least one symbol returned zero bars from Polygon.

This script is deliberately thin: it is orchestration only and delegates
every piece of real work (rate limiting, retries, Parquet layout) to
existing modules.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from alpha_harness.data.equities_loader import EquitiesLoader
from alpha_harness.data.loader_factory import create_equities_loader
from alpha_harness.data.models import BarFrequency, DataRequest
from alpha_harness.data.parquet_store import ParquetStore

logger = logging.getLogger("alpha_harness.backfill")

DEFAULT_UNIVERSE = Path("configs/universes/sp50.txt")
DEFAULT_OUTPUT = Path("data/silver/equities")
DEFAULT_START = date(2023, 1, 1)
# Default end is "yesterday" — Polygon does not always serve same-day bars.
DEFAULT_END_OFFSET = timedelta(days=1)


# ── Universe parsing ────────────────────────────────────────────────────────


def read_universe(path: Path) -> list[str]:
    """Parse a plain-text ticker list.

    Blank lines and ``#``-prefixed comments are ignored.  Order is
    preserved; duplicates are silently deduplicated while keeping the
    first occurrence.
    """
    if not path.is_file():
        raise FileNotFoundError(f"Universe file not found: {path}")

    symbols: list[str] = []
    seen: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        sym = line.upper()
        if sym in seen:
            continue
        seen.add(sym)
        symbols.append(sym)

    if not symbols:
        raise ValueError(f"Universe file {path} contained zero tickers.")
    return symbols


# ── Cache inspection ────────────────────────────────────────────────────────


def _parquet_covers_range(path: Path, start: date, end: date) -> bool:
    """Return True iff ``path`` already covers ``[start, end]``.

    A file "covers" the range when the min timestamp is ``<= start`` and
    the max timestamp is ``>= end``.  We read only the ``timestamp``
    column so this stays cheap for thousands of rows.
    """
    if not path.is_file():
        return False
    try:
        df = pd.read_parquet(path, columns=["timestamp"])
    except (OSError, ValueError) as exc:
        logger.warning("Could not read %s for cache check: %s", path, exc)
        return False
    if df.empty:
        return False
    ts = pd.to_datetime(df["timestamp"], utc=True)
    file_start = ts.min().date()
    file_end = ts.max().date()
    return file_start <= start and file_end >= end


def select_missing(
    symbols: Iterable[str],
    *,
    output_dir: Path,
    start: date,
    end: date,
) -> list[str]:
    """Return the subset of ``symbols`` whose Parquet file does not
    already cover ``[start, end]``."""
    missing: list[str] = []
    for sym in symbols:
        path = output_dir / f"{sym}.parquet"
        if _parquet_covers_range(path, start, end):
            logger.info(
                "cache-hit %s — %s already covers %s..%s",
                sym, path, start, end,
            )
            continue
        missing.append(sym)
    return missing


# ── Orchestration ───────────────────────────────────────────────────────────


def backfill(
    *,
    symbols: list[str],
    start: date,
    end: date,
    output_dir: Path,
    loader: EquitiesLoader | None = None,
) -> tuple[int, list[str]]:
    """Fetch and persist ``symbols`` for ``[start, end]``.

    Parameters
    ----------
    loader:
        Injectable for tests.  Defaults to
        ``create_equities_loader("polygon")`` which honours
        ``POLYGON_API_KEY`` / ``POLYGON_RPM``.

    Returns
    -------
    ``(written, empty_symbols)``.  ``written`` is the number of symbol
    files successfully persisted this run; ``empty_symbols`` lists
    symbols for which Polygon returned zero bars.
    """
    if not symbols:
        logger.info("Nothing to backfill — every symbol is already cached.")
        return 0, []

    loader = loader or create_equities_loader("polygon")
    store = ParquetStore(str(output_dir))

    written = 0
    empty: list[str] = []

    # One symbol at a time: keeps the per-symbol Parquet file atomic,
    # preserves resumability, and plays nicely with the rate limiter.
    for sym in symbols:
        logger.info("fetching %s  %s..%s", sym, start, end)
        request = DataRequest(
            symbols=[sym],
            start=start,
            end=end,
            frequency=BarFrequency.DAILY,
        )
        df, meta = loader.load_bars(request)
        if df.empty or meta.bars_returned == 0:
            logger.warning("no data returned for %s — skipping", sym)
            empty.append(sym)
            continue
        saved = store.save_equities(df)
        if saved == 0:
            logger.warning("ParquetStore wrote zero files for %s", sym)
            empty.append(sym)
            continue
        written += 1
        logger.info("wrote %s  (%d bars)", sym, meta.bars_returned)

    return written, empty


def _configure_logging(verbosity: int) -> None:
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _parse_date(raw: str) -> date:
    return datetime.strptime(raw, "%Y-%m-%d").date()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--universe",
        type=Path,
        default=DEFAULT_UNIVERSE,
        help=f"Path to a newline-delimited ticker file (default: {DEFAULT_UNIVERSE}).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Parquet output directory (default: {DEFAULT_OUTPUT}).",
    )
    parser.add_argument(
        "--start-date",
        type=_parse_date,
        default=DEFAULT_START,
        help="Start date YYYY-MM-DD (default: 2023-01-01).",
    )
    parser.add_argument(
        "--end-date",
        type=_parse_date,
        default=None,
        help="End date YYYY-MM-DD (default: yesterday).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Refetch every symbol even if its Parquet file already covers the range.",
    )
    parser.add_argument(
        "-v", "--verbose", action="count", default=1,
        help="Increase log verbosity (-v=INFO, -vv=DEBUG).",
    )
    args = parser.parse_args(argv)

    _configure_logging(args.verbose)

    try:
        symbols = read_universe(args.universe)
    except (FileNotFoundError, ValueError) as exc:
        logger.error("%s", exc)
        return 1

    end = args.end_date or (date.today() - DEFAULT_END_OFFSET)
    start = args.start_date
    if end <= start:
        logger.error("end-date (%s) must be after start-date (%s)", end, start)
        return 1

    args.output_dir.mkdir(parents=True, exist_ok=True)

    targets = symbols if args.force else select_missing(
        symbols, output_dir=args.output_dir, start=start, end=end,
    )
    logger.info(
        "universe=%d  targets=%d  cached=%d  window=%s..%s",
        len(symbols), len(targets), len(symbols) - len(targets), start, end,
    )

    written, empty = backfill(
        symbols=targets,
        start=start,
        end=end,
        output_dir=args.output_dir,
    )

    logger.info("done — wrote %d symbol files; %d empty", written, len(empty))
    if empty:
        logger.warning("symbols with no data: %s", ",".join(empty))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
