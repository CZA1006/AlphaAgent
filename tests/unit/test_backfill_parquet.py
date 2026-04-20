"""Unit tests for :mod:`scripts.backfill_parquet`.

These tests exercise the backfill orchestration with an injected fake
loader — no network, no Polygon key required.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from alpha_harness.data.equities_loader import EquitiesLoader
from alpha_harness.data.models import (
    AdjustmentType,
    BarFrequency,
    DataRequest,
    DataResult,
)
from scripts.backfill_parquet import (
    backfill,
    read_universe,
    select_missing,
)

# ── Universe parsing ────────────────────────────────────────────────────────


def test_read_universe_strips_comments_and_dedups(tmp_path: Path) -> None:
    f = tmp_path / "u.txt"
    f.write_text(
        "# header comment\n"
        "AAPL\n"
        "\n"
        "MSFT  # inline comment\n"
        "  nvda  \n"
        "AAPL\n"  # duplicate
        "# trailing\n",
    )
    assert read_universe(f) == ["AAPL", "MSFT", "NVDA"]


def test_read_universe_rejects_empty(tmp_path: Path) -> None:
    f = tmp_path / "empty.txt"
    f.write_text("# only comments\n\n")
    with pytest.raises(ValueError):
        read_universe(f)


def test_read_universe_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_universe(tmp_path / "nope.txt")


def test_ships_with_sp50_universe() -> None:
    """The committed sp50.txt must always parse and contain ≥50 names."""
    path = Path("configs/universes/sp50.txt")
    syms = read_universe(path)
    assert len(syms) >= 50
    assert len(syms) == len(set(syms))  # unique
    assert all(s.isupper() and s.isalpha() for s in syms)


# ── Cache inspection ────────────────────────────────────────────────────────


def _write_parquet(path: Path, start: date, end: date) -> None:
    """Helper: write a minimal per-symbol Parquet file covering [start, end]."""
    path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame({
        "symbol": ["AAPL", "AAPL"],
        "timestamp": [
            datetime.combine(start, datetime.min.time()).replace(tzinfo=UTC),
            datetime.combine(end, datetime.min.time()).replace(tzinfo=UTC),
        ],
        "open": [1.0, 2.0],
        "high": [1.5, 2.5],
        "low": [0.5, 1.5],
        "close": [1.2, 2.2],
        "volume": [100.0, 200.0],
    })
    df.to_parquet(path, index=False)


def test_select_missing_skips_fully_cached(tmp_path: Path) -> None:
    _write_parquet(tmp_path / "AAPL.parquet", date(2023, 1, 1), date(2024, 6, 1))
    _write_parquet(tmp_path / "MSFT.parquet", date(2024, 1, 1), date(2024, 6, 1))

    # AAPL covers window; MSFT does not (start too late).
    missing = select_missing(
        ["AAPL", "MSFT", "NVDA"],
        output_dir=tmp_path,
        start=date(2023, 6, 1),
        end=date(2024, 5, 1),
    )
    # MSFT: start=2024-01-01 > requested 2023-06-01 -> missing.
    # NVDA: no file -> missing.
    assert missing == ["MSFT", "NVDA"]


def test_select_missing_handles_corrupt_file(tmp_path: Path) -> None:
    bad = tmp_path / "BAD.parquet"
    bad.write_bytes(b"not a parquet file")
    missing = select_missing(
        ["BAD"], output_dir=tmp_path,
        start=date(2023, 1, 1), end=date(2023, 6, 1),
    )
    assert missing == ["BAD"]


# ── Orchestration with an injected fake loader ─────────────────────────────


class FakeLoader:
    """In-memory stand-in for :class:`PolygonEquitiesLoader`.

    Returns a small, deterministic bar frame per requested symbol unless
    the symbol is in ``empty_symbols``, in which case it returns nothing.
    """

    def __init__(self, empty_symbols: set[str] | None = None) -> None:
        self.empty_symbols = empty_symbols or set()
        self.calls: list[str] = []

    def load_bars(
        self,
        request: DataRequest,
        adjustment: AdjustmentType = AdjustmentType.SPLIT_AND_DIVIDEND,
    ) -> tuple[pd.DataFrame, DataResult]:
        assert len(request.symbols) == 1, "backfill must request one symbol at a time"
        sym = request.symbols[0]
        self.calls.append(sym)
        if sym in self.empty_symbols:
            df = pd.DataFrame(columns=[
                "symbol", "timestamp", "open", "high", "low", "close",
                "volume", "vwap", "adjustment", "source", "frequency",
            ])
            return df, DataResult(
                symbols_requested=1, symbols_returned=0, bars_returned=0,
                start=request.start, end=request.end, source="fake",
            )

        days = [request.start + timedelta(days=i) for i in range(3)]
        df = pd.DataFrame({
            "symbol": [sym] * 3,
            "timestamp": [
                datetime.combine(d, datetime.min.time()).replace(tzinfo=UTC)
                for d in days
            ],
            "open": [1.0, 1.1, 1.2],
            "high": [1.3, 1.4, 1.5],
            "low": [0.9, 1.0, 1.1],
            "close": [1.2, 1.3, 1.4],
            "volume": [1e6, 1.1e6, 1.2e6],
            "vwap": [1.1, 1.2, 1.3],
            "adjustment": [adjustment.value] * 3,
            "source": ["fake"] * 3,
            "frequency": [BarFrequency.DAILY.value] * 3,
        })
        return df, DataResult(
            symbols_requested=1, symbols_returned=1, bars_returned=3,
            start=request.start, end=request.end, source="fake",
        )


def _loader_as_protocol(loader: FakeLoader) -> EquitiesLoader:
    # Help mypy see the duck-typed protocol match without forcing FakeLoader
    # to explicitly inherit from it.
    return loader  # type: ignore[return-value]


def test_backfill_writes_files_and_reports_empty(tmp_path: Path) -> None:
    loader = FakeLoader(empty_symbols={"ZZZZ"})
    written, empty = backfill(
        symbols=["AAPL", "MSFT", "ZZZZ"],
        start=date(2024, 1, 1),
        end=date(2024, 1, 5),
        output_dir=tmp_path,
        loader=_loader_as_protocol(loader),
    )

    assert written == 2
    assert empty == ["ZZZZ"]
    assert loader.calls == ["AAPL", "MSFT", "ZZZZ"]
    assert (tmp_path / "AAPL.parquet").is_file()
    assert (tmp_path / "MSFT.parquet").is_file()
    assert not (tmp_path / "ZZZZ.parquet").exists()


def test_backfill_output_is_readable_by_local_loader(tmp_path: Path) -> None:
    """End-to-end: backfill → LocalEquitiesLoader reads it back correctly.

    This is the invariant that keeps ``--data-source parquet`` working
    after the backfill.  If the Parquet layout drifts, this test breaks
    loudly instead of silently producing empty cycles.
    """
    from alpha_harness.data.equities_loader import LocalEquitiesLoader

    loader = FakeLoader()
    backfill(
        symbols=["AAPL", "MSFT"],
        start=date(2024, 1, 1),
        end=date(2024, 1, 5),
        output_dir=tmp_path,
        loader=_loader_as_protocol(loader),
    )

    local = LocalEquitiesLoader(base_path=str(tmp_path))
    df, meta = local.load_bars(DataRequest(
        symbols=["AAPL", "MSFT"],
        start=date(2024, 1, 1),
        end=date(2024, 1, 5),
        frequency=BarFrequency.DAILY,
    ))
    assert meta.symbols_returned == 2
    assert meta.bars_returned == 6  # 3 bars per symbol, 2 symbols
    assert set(df["symbol"].unique()) == {"AAPL", "MSFT"}


def test_backfill_empty_input_is_noop(tmp_path: Path) -> None:
    loader = FakeLoader()
    written, empty = backfill(
        symbols=[],
        start=date(2024, 1, 1),
        end=date(2024, 1, 5),
        output_dir=tmp_path,
        loader=_loader_as_protocol(loader),
    )
    assert written == 0
    assert empty == []
    assert loader.calls == []
