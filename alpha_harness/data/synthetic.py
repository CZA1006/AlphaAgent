"""Synthetic price panel generator for testing and demonstration.

Creates realistic-looking multi-symbol OHLCV panels with configurable
drift, volatility, and cross-sectional dispersion.  The generated data
is deterministic given a fixed seed, making it suitable for reproducible
tests and demo scripts.

Usage::

    from alpha_harness.data.synthetic import generate_price_panel

    df = generate_price_panel(
        symbols=["AAPL", "MSFT", "GOOG", "AMZN", "META"],
        n_days=120,
        seed=42,
    )
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd


def generate_price_panel(
    symbols: list[str] | None = None,
    n_days: int = 120,
    seed: int = 42,
    start_date: datetime | None = None,
    base_price: float = 100.0,
    daily_vol: float = 0.02,
) -> pd.DataFrame:
    """Generate a synthetic multi-symbol OHLCV panel.

    Each symbol gets a different drift so that cross-sectional signals
    (rank, zscore) have meaningful variation.  Prices follow a geometric
    random walk: ``close[t] = close[t-1] * exp(drift + vol * Z)``.

    Parameters
    ----------
    symbols:
        List of ticker symbols.  Defaults to a 10-stock universe.
    n_days:
        Number of trading days to generate.
    seed:
        Random seed for reproducibility.
    start_date:
        First timestamp.  Defaults to 2024-01-02 UTC.
    base_price:
        Starting close price for each symbol (before drift adjustment).
    daily_vol:
        Daily return volatility (standard deviation of log returns).

    Returns
    -------
    DataFrame with columns: symbol, timestamp, open, high, low, close, volume.
    Sorted by (symbol, timestamp).
    """
    if symbols is None:
        symbols = [
            "AAPL",
            "MSFT",
            "GOOG",
            "AMZN",
            "META",
            "NVDA",
            "TSLA",
            "JPM",
            "V",
            "UNH",
        ]
    if start_date is None:
        start_date = datetime(2024, 1, 2, tzinfo=UTC)

    rng = np.random.default_rng(seed)

    # Give each symbol a different daily drift so cross-sectional
    # dispersion is meaningful
    n_symbols = len(symbols)
    drifts = np.linspace(-0.001, 0.002, n_symbols)

    rows: list[dict[str, object]] = []
    for sym_idx, symbol in enumerate(symbols):
        drift = drifts[sym_idx]
        price = base_price + rng.uniform(-10, 10)  # slightly different start

        for day in range(n_days):
            ts = start_date + timedelta(days=day)
            # Skip weekends for realism
            if ts.weekday() >= 5:
                continue

            log_ret = drift + daily_vol * rng.standard_normal()
            close = price * np.exp(log_ret)

            # Synthesise OHLV from close
            intraday_range = abs(log_ret) + daily_vol * 0.5
            high = close * (1 + rng.uniform(0, intraday_range))
            low = close * (1 - rng.uniform(0, intraday_range))
            open_price = price  # open = previous close

            volume = float(rng.integers(100_000, 10_000_000))

            rows.append(
                {
                    "symbol": symbol,
                    "timestamp": ts,
                    "open": round(float(open_price), 4),
                    "high": round(float(high), 4),
                    "low": round(float(low), 4),
                    "close": round(float(close), 4),
                    "volume": volume,
                }
            )
            price = close

    df = pd.DataFrame(rows)
    df = df.sort_values(["symbol", "timestamp"]).reset_index(drop=True)
    return df
