"""Tests for the Factor DSL executor — runs parsed ASTs on DataFrames.

Split into sections:
  - Hand-crafted deterministic tests (exact expected values)
  - Field references
  - Arithmetic
  - Time-series functions (single-symbol + multi-symbol grouping)
  - Cross-sectional functions
  - Composition / nesting
  - End-to-end expression-to-signal tests
  - Error / edge cases
"""

import numpy as np
import pandas as pd
import pytest

from alpha_harness.factors.dsl_executor import DslExecutionError, DslExecutor
from alpha_harness.factors.dsl_parser import parse_expression

# ── Fixtures ─────────────────────────────────────────────────────────────────


def _make_single_symbol_df(n: int = 50) -> pd.DataFrame:
    """Create a simple single-symbol price DataFrame."""
    rng = np.random.default_rng(42)
    prices = 100.0 + np.cumsum(rng.standard_normal(n) * 0.5)
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2023-01-01", periods=n, freq="D", tz="UTC"),
            "open": prices + rng.uniform(-0.5, 0.5, n),
            "high": prices + rng.uniform(0, 1, n),
            "low": prices - rng.uniform(0, 1, n),
            "close": prices,
            "volume": rng.uniform(1e6, 5e6, n),
        }
    )


def _make_multi_symbol_df() -> pd.DataFrame:
    """Create a multi-symbol panel DataFrame."""
    rng = np.random.default_rng(42)
    frames: list[pd.DataFrame] = []
    for symbol in ("AAPL", "MSFT", "GOOGL"):
        n = 30
        prices = 100.0 + np.cumsum(rng.standard_normal(n) * 0.5)
        df = pd.DataFrame(
            {
                "timestamp": pd.date_range("2023-01-01", periods=n, freq="D", tz="UTC"),
                "symbol": symbol,
                "open": prices + rng.uniform(-0.5, 0.5, n),
                "high": prices + rng.uniform(0, 1, n),
                "low": prices - rng.uniform(0, 1, n),
                "close": prices,
                "volume": rng.uniform(1e6, 5e6, n),
            }
        )
        frames.append(df)
    return pd.concat(frames, ignore_index=True)


def _make_tiny_df() -> pd.DataFrame:
    """Tiny hand-crafted DataFrame for exact-value tests (no randomness)."""
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                ["2023-01-01", "2023-01-02", "2023-01-03", "2023-01-04", "2023-01-05"], utc=True
            ),
            "close": [10.0, 12.0, 11.0, 14.0, 13.0],
            "open": [9.5, 11.5, 10.5, 13.5, 12.5],
            "high": [10.5, 12.5, 11.5, 14.5, 13.5],
            "low": [9.0, 11.0, 10.0, 13.0, 12.0],
            "volume": [100.0, 200.0, 150.0, 300.0, 0.0],
        }
    )


def _make_tiny_multi_df() -> pd.DataFrame:
    """Tiny multi-symbol DataFrame for exact cross-sectional tests."""
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                [
                    "2023-01-01",
                    "2023-01-01",
                    "2023-01-01",
                    "2023-01-02",
                    "2023-01-02",
                    "2023-01-02",
                ],
                utc=True,
            ),
            "symbol": ["A", "B", "C", "A", "B", "C"],
            "close": [10.0, 20.0, 30.0, 15.0, 25.0, 5.0],
            "open": [9.0, 19.0, 29.0, 14.0, 24.0, 4.0],
            "high": [11.0, 21.0, 31.0, 16.0, 26.0, 6.0],
            "low": [8.0, 18.0, 28.0, 13.0, 23.0, 3.0],
            "volume": [100.0, 200.0, 300.0, 150.0, 250.0, 50.0],
        }
    )


# ── Hand-crafted deterministic tests ────────────────────────────────────────


class TestHandCraftedDeterministic:
    """Tests on tiny DataFrames where expected values are computed by hand."""

    def test_lag_exact(self) -> None:
        """lag(close, 1) shifts by 1: [NaN, 10, 12, 11, 14]."""
        df = _make_tiny_df()
        result = DslExecutor(df).execute(parse_expression("lag(close, 1)"))
        expected = pd.Series([np.nan, 10.0, 12.0, 11.0, 14.0])
        pd.testing.assert_series_equal(result, expected, check_names=False)

    def test_ts_mean_window_3_exact(self) -> None:
        """ts_mean(close, 3) with min_periods=1:
        [10, (10+12)/2, (10+12+11)/3, (12+11+14)/3, (11+14+13)/3]
        = [10, 11, 11, 12.333..., 12.666...]
        """
        df = _make_tiny_df()
        result = DslExecutor(df).execute(parse_expression("ts_mean(close, 3)"))
        expected = pd.Series([10.0, 11.0, 11.0, 37.0 / 3, 38.0 / 3])
        pd.testing.assert_series_equal(result, expected, check_names=False)

    def test_ts_std_window_3_exact(self) -> None:
        """ts_std(close, 3) first value is NaN (std of single value)."""
        df = _make_tiny_df()
        result = DslExecutor(df).execute(parse_expression("ts_std(close, 3)"))
        # First value: std([10]) = NaN, second: std([10,12]), third: std([10,12,11])
        assert np.isnan(result.iloc[0])
        # std([10, 12]) = 1.4142...
        assert abs(result.iloc[1] - np.std([10, 12], ddof=1)) < 1e-10

    def test_ts_delta_exact(self) -> None:
        """ts_delta(close, 1) = close - lag(close, 1): [NaN, 2, -1, 3, -1]."""
        df = _make_tiny_df()
        result = DslExecutor(df).execute(parse_expression("ts_delta(close, 1)"))
        expected = pd.Series([np.nan, 2.0, -1.0, 3.0, -1.0])
        pd.testing.assert_series_equal(result, expected, check_names=False)

    def test_ts_sum_exact(self) -> None:
        """ts_sum(volume, 3) with min_periods=1:
        [100, 300, 450, 650, 450]
        """
        df = _make_tiny_df()
        result = DslExecutor(df).execute(parse_expression("ts_sum(volume, 3)"))
        expected = pd.Series([100.0, 300.0, 450.0, 650.0, 450.0])
        pd.testing.assert_series_equal(result, expected, check_names=False)

    def test_event_decay_uses_half_life_and_fills_missing_events(self) -> None:
        df = _make_tiny_df()
        df["days_to_next_greenshoe_expiry"] = [0.0, 5.0, 10.0, np.nan, -5.0]

        result = DslExecutor(df).execute(
            parse_expression("event_decay(days_to_next_greenshoe_expiry, 5)")
        )

        expected = pd.Series([1.0, 0.5, 0.25, 0.0, 0.5])
        pd.testing.assert_series_equal(result, expected, check_names=False)

    def test_ts_min_exact(self) -> None:
        """ts_min(close, 3) with min_periods=1:
        [10, 10, 10, 11, 11]
        """
        df = _make_tiny_df()
        result = DslExecutor(df).execute(parse_expression("ts_min(close, 3)"))
        expected = pd.Series([10.0, 10.0, 10.0, 11.0, 11.0])
        pd.testing.assert_series_equal(result, expected, check_names=False)

    def test_ts_max_exact(self) -> None:
        """ts_max(close, 3) with min_periods=1:
        [10, 12, 12, 14, 14]
        """
        df = _make_tiny_df()
        result = DslExecutor(df).execute(parse_expression("ts_max(close, 3)"))
        expected = pd.Series([10.0, 12.0, 12.0, 14.0, 14.0])
        pd.testing.assert_series_equal(result, expected, check_names=False)

    def test_arithmetic_exact(self) -> None:
        """(high - low) * 2: [3, 3, 3, 3, 3]."""
        df = _make_tiny_df()
        result = DslExecutor(df).execute(parse_expression("(high - low) * 2"))
        expected = pd.Series([3.0, 3.0, 3.0, 3.0, 3.0])
        pd.testing.assert_series_equal(result, expected, check_names=False)

    def test_cross_sectional_rank_exact(self) -> None:
        """rank(close) on 3 symbols at 2 timestamps.

        t1: A=10, B=20, C=30 -> ranks 1/3, 2/3, 3/3
        t2: A=15, B=25, C=5  -> ranks 2/3, 3/3, 1/3
        """
        df = _make_tiny_multi_df()
        result = DslExecutor(df).execute(parse_expression("rank(close)"))
        expected = pd.Series(
            [
                1.0 / 3,
                2.0 / 3,
                3.0 / 3,
                2.0 / 3,
                3.0 / 3,
                1.0 / 3,
            ]
        )
        pd.testing.assert_series_equal(result, expected, check_names=False)

    def test_cross_sectional_zscore_exact(self) -> None:
        """zscore(close) on 3 symbols at 2 timestamps.

        t1: A=10, B=20, C=30 -> mean=20, std=10 -> [-1, 0, 1]
        t2: A=15, B=25, C=5  -> mean=15, std=10 -> [0, 1, -1]
        """
        df = _make_tiny_multi_df()
        result = DslExecutor(df).execute(parse_expression("zscore(close)"))
        expected = pd.Series([-1.0, 0.0, 1.0, 0.0, 1.0, -1.0])
        pd.testing.assert_series_equal(result, expected, check_names=False)


# ── Field references ─────────────────────────────────────────────────────────


class TestFieldReferences:
    def test_close(self) -> None:
        df = _make_single_symbol_df()
        executor = DslExecutor(df)
        result = executor.execute(parse_expression("close"))
        pd.testing.assert_series_equal(result, df["close"].astype(float), check_names=False)

    def test_volume(self) -> None:
        df = _make_single_symbol_df()
        executor = DslExecutor(df)
        result = executor.execute(parse_expression("volume"))
        pd.testing.assert_series_equal(result, df["volume"].astype(float), check_names=False)

    def test_missing_field(self) -> None:
        df = _make_single_symbol_df()
        executor = DslExecutor(df)
        with pytest.raises(DslExecutionError, match="not found"):
            executor.execute(parse_expression("vwap"))


# ── Arithmetic ───────────────────────────────────────────────────────────────


class TestArithmetic:
    def test_add(self) -> None:
        df = _make_single_symbol_df()
        executor = DslExecutor(df)
        result = executor.execute(parse_expression("close + volume"))
        expected = df["close"] + df["volume"]
        pd.testing.assert_series_equal(result, expected, check_names=False)

    def test_subtract(self) -> None:
        df = _make_single_symbol_df()
        executor = DslExecutor(df)
        result = executor.execute(parse_expression("high - low"))
        expected = df["high"] - df["low"]
        pd.testing.assert_series_equal(result, expected, check_names=False)

    def test_multiply(self) -> None:
        df = _make_single_symbol_df()
        executor = DslExecutor(df)
        result = executor.execute(parse_expression("close * 2"))
        expected = df["close"] * 2
        pd.testing.assert_series_equal(result, expected, check_names=False)

    def test_divide(self) -> None:
        df = _make_single_symbol_df()
        executor = DslExecutor(df)
        result = executor.execute(parse_expression("close / volume"))
        expected = df["close"] / df["volume"]
        pd.testing.assert_series_equal(result, expected, check_names=False)

    def test_unary_minus(self) -> None:
        df = _make_single_symbol_df()
        executor = DslExecutor(df)
        result = executor.execute(parse_expression("-close"))
        expected = -df["close"]
        pd.testing.assert_series_equal(result, expected, check_names=False)

    def test_precedence(self) -> None:
        df = _make_single_symbol_df()
        executor = DslExecutor(df)
        # close + volume * 2 == close + (volume * 2)
        result = executor.execute(parse_expression("close + volume * 2"))
        expected = df["close"] + df["volume"] * 2
        pd.testing.assert_series_equal(result, expected, check_names=False)

    def test_division_by_zero_series(self) -> None:
        """Dividing by a series with zeros produces NaN, not inf."""
        df = _make_tiny_df()  # volume has a 0.0 at index 4
        result = DslExecutor(df).execute(parse_expression("close / volume"))
        # Index 4: close=13, volume=0 -> should be NaN, not inf
        assert np.isnan(result.iloc[4])
        # Other values should be finite
        assert np.isfinite(result.iloc[0])


# ── Time-series functions ────────────────────────────────────────────────────


class TestTimeSeriesFunctions:
    def test_ts_mean(self) -> None:
        df = _make_single_symbol_df()
        executor = DslExecutor(df)
        result = executor.execute(parse_expression("ts_mean(close, 5)"))
        expected = df["close"].rolling(5, min_periods=1).mean()
        pd.testing.assert_series_equal(result, expected, check_names=False)

    def test_ts_std(self) -> None:
        df = _make_single_symbol_df()
        executor = DslExecutor(df)
        result = executor.execute(parse_expression("ts_std(close, 10)"))
        expected = df["close"].rolling(10, min_periods=1).std()
        pd.testing.assert_series_equal(result, expected, check_names=False)

    def test_ts_lag(self) -> None:
        df = _make_single_symbol_df()
        executor = DslExecutor(df)
        result = executor.execute(parse_expression("ts_lag(close, 1)"))
        expected = df["close"].shift(1)
        pd.testing.assert_series_equal(result, expected, check_names=False)

    def test_lag_alias(self) -> None:
        df = _make_single_symbol_df()
        executor = DslExecutor(df)
        result = executor.execute(parse_expression("lag(close, 1)"))
        expected = df["close"].shift(1)
        pd.testing.assert_series_equal(result, expected, check_names=False)

    def test_ts_delta(self) -> None:
        df = _make_single_symbol_df()
        executor = DslExecutor(df)
        result = executor.execute(parse_expression("ts_delta(close, 1)"))
        expected = df["close"] - df["close"].shift(1)
        pd.testing.assert_series_equal(result, expected, check_names=False)

    def test_ts_sum(self) -> None:
        df = _make_single_symbol_df()
        executor = DslExecutor(df)
        result = executor.execute(parse_expression("ts_sum(volume, 5)"))
        expected = df["volume"].rolling(5, min_periods=1).sum()
        pd.testing.assert_series_equal(result, expected, check_names=False)

    def test_ts_min(self) -> None:
        df = _make_single_symbol_df()
        executor = DslExecutor(df)
        result = executor.execute(parse_expression("ts_min(close, 5)"))
        expected = df["close"].rolling(5, min_periods=1).min()
        pd.testing.assert_series_equal(result, expected, check_names=False)

    def test_ts_max(self) -> None:
        df = _make_single_symbol_df()
        executor = DslExecutor(df)
        result = executor.execute(parse_expression("ts_max(close, 5)"))
        expected = df["close"].rolling(5, min_periods=1).max()
        pd.testing.assert_series_equal(result, expected, check_names=False)


# ── Multi-symbol time-series grouping ────────────────────────────────────────


class TestMultiSymbolTimeSeries:
    """Verify that time-series ops apply *per-symbol*, not across all rows."""

    def test_ts_mean_per_symbol(self) -> None:
        """ts_mean should not bleed across symbols."""
        df = _make_tiny_multi_df()
        result = DslExecutor(df).execute(parse_expression("ts_mean(close, 2)"))

        # For symbol A (rows 0, 3): close=[10, 15]
        #   row 0: mean([10]) = 10.0 (min_periods=1)
        #   row 3: mean([10, 15]) = 12.5
        assert result.iloc[0] == pytest.approx(10.0)
        assert result.iloc[3] == pytest.approx(12.5)

        # For symbol B (rows 1, 4): close=[20, 25]
        #   row 1: mean([20]) = 20.0
        #   row 4: mean([20, 25]) = 22.5
        assert result.iloc[1] == pytest.approx(20.0)
        assert result.iloc[4] == pytest.approx(22.5)

        # For symbol C (rows 2, 5): close=[30, 5]
        #   row 2: mean([30]) = 30.0
        #   row 5: mean([30, 5]) = 17.5
        assert result.iloc[2] == pytest.approx(30.0)
        assert result.iloc[5] == pytest.approx(17.5)

    def test_lag_per_symbol(self) -> None:
        """lag should shift within each symbol independently."""
        df = _make_tiny_multi_df()
        result = DslExecutor(df).execute(parse_expression("lag(close, 1)"))

        # First row per symbol has NaN (no prior value)
        assert np.isnan(result.iloc[0])  # A first
        assert np.isnan(result.iloc[1])  # B first
        assert np.isnan(result.iloc[2])  # C first

        # Second row per symbol has the first value
        assert result.iloc[3] == pytest.approx(10.0)  # A: lag of 15 is 10
        assert result.iloc[4] == pytest.approx(20.0)  # B: lag of 25 is 20
        assert result.iloc[5] == pytest.approx(30.0)  # C: lag of 5 is 30

    def test_ts_delta_per_symbol(self) -> None:
        """ts_delta should compute differences within each symbol."""
        df = _make_tiny_multi_df()
        result = DslExecutor(df).execute(parse_expression("ts_delta(close, 1)"))

        # First row per symbol: NaN
        assert np.isnan(result.iloc[0])
        # Second row: close[t] - close[t-1] within symbol
        assert result.iloc[3] == pytest.approx(5.0)  # A: 15 - 10
        assert result.iloc[4] == pytest.approx(5.0)  # B: 25 - 20
        assert result.iloc[5] == pytest.approx(-25.0)  # C: 5 - 30


# ── Cross-sectional functions ────────────────────────────────────────────────


class TestCrossSectionalFunctions:
    def test_rank_single_symbol(self) -> None:
        df = _make_single_symbol_df()
        executor = DslExecutor(df)
        result = executor.execute(parse_expression("rank(close)"))
        expected = df["close"].rank(pct=True)
        pd.testing.assert_series_equal(result, expected, check_names=False)

    def test_rank_multi_symbol(self) -> None:
        df = _make_multi_symbol_df()
        executor = DslExecutor(df)
        result = executor.execute(parse_expression("rank(close)"))
        # Cross-sectional rank per timestamp
        expected = df["close"].groupby(df["timestamp"]).rank(pct=True)
        pd.testing.assert_series_equal(result, expected, check_names=False)

    def test_zscore_single_symbol(self) -> None:
        df = _make_single_symbol_df()
        executor = DslExecutor(df)
        result = executor.execute(parse_expression("zscore(close)"))
        # Expanding zscore
        mean = df["close"].expanding().mean()
        std = df["close"].expanding().std().replace(0, float("nan"))
        expected = (df["close"] - mean) / std
        pd.testing.assert_series_equal(result, expected, check_names=False)

    def test_zscore_multi_symbol(self) -> None:
        df = _make_multi_symbol_df()
        executor = DslExecutor(df)
        result = executor.execute(parse_expression("zscore(close)"))
        # Cross-sectional zscore per timestamp
        grouped = df["close"].groupby(df["timestamp"])
        mean = grouped.transform("mean")
        std = grouped.transform("std").replace(0, float("nan"))
        expected = (df["close"] - mean) / std
        pd.testing.assert_series_equal(result, expected, check_names=False)


# ── Composition ──────────────────────────────────────────────────────────────


class TestComposition:
    def test_rank_of_ts_mean(self) -> None:
        df = _make_multi_symbol_df()
        executor = DslExecutor(df)
        result = executor.execute(parse_expression("rank(ts_mean(close, 5))"))
        # Should produce values between 0 and 1 (percentile ranks)
        assert result.min() >= 0.0
        assert result.max() <= 1.0

    def test_zscore_of_ratio(self) -> None:
        df = _make_multi_symbol_df()
        executor = DslExecutor(df)
        result = executor.execute(parse_expression("zscore(close / ts_mean(close, 10))"))
        # zscore should have mean near 0 per timestamp
        assert not result.isna().all()

    def test_rank_of_volume_ratio(self) -> None:
        df = _make_multi_symbol_df()
        executor = DslExecutor(df)
        result = executor.execute(parse_expression("rank(volume / ts_mean(volume, 10))"))
        assert result.min() >= 0.0
        assert result.max() <= 1.0

    def test_momentum_minus_mean(self) -> None:
        df = _make_single_symbol_df()
        executor = DslExecutor(df)
        result = executor.execute(parse_expression("ts_mean(close, 20) - ts_mean(close, 5)"))
        expected = (
            df["close"].rolling(20, min_periods=1).mean()
            - df["close"].rolling(5, min_periods=1).mean()
        )
        pd.testing.assert_series_equal(result, expected, check_names=False)

    def test_deterministic(self) -> None:
        """Same input produces same output."""
        df = _make_single_symbol_df()
        expr = "rank(ts_mean(close, 10) - ts_mean(close, 3))"
        ast = parse_expression(expr)
        r1 = DslExecutor(df).execute(ast)
        r2 = DslExecutor(df).execute(ast)
        pd.testing.assert_series_equal(r1, r2)


# ── End-to-end factor execution ──────────────────────────────────────────────


class TestEndToEnd:
    """Full pipeline: expression string -> parse -> execute -> verify values."""

    def test_mean_reversion_signal(self) -> None:
        """Classic mean-reversion: close / ts_mean(close, 3) - 1.

        On tiny data close=[10, 12, 11, 14, 13]:
          ts_mean(close,3) = [10, 11, 11, 37/3, 38/3]
          ratio - 1       = [0, 1/11, 0, 14*3/37-1, 13*3/38-1]
                          = [0, 0.0909..., 0, 0.1351..., 0.0263...]
        """
        df = _make_tiny_df()
        result = DslExecutor(df).execute(parse_expression("close / ts_mean(close, 3) - 1"))
        assert len(result) == 5
        assert result.iloc[0] == pytest.approx(0.0)
        assert result.iloc[1] == pytest.approx(1.0 / 11.0)
        assert result.iloc[2] == pytest.approx(0.0)
        assert result.iloc[3] == pytest.approx(14.0 / (37.0 / 3) - 1)
        assert result.iloc[4] == pytest.approx(13.0 / (38.0 / 3) - 1)

    def test_cross_sectional_momentum_rank(self) -> None:
        """rank(ts_delta(close, 1)) on multi-symbol tiny data.

        After ts_delta per symbol: A=[NaN,5], B=[NaN,5], C=[NaN,-25]
        At t2: rank across [5, 5, -25]:
          -25 -> 1/3, 5 -> 2.5/3 (tie), 5 -> 2.5/3 (tie)
        """
        df = _make_tiny_multi_df()
        result = DslExecutor(df).execute(parse_expression("rank(ts_delta(close, 1))"))
        # t1: all NaN (no prior data) -> rank of NaN = NaN
        assert np.isnan(result.iloc[0])
        assert np.isnan(result.iloc[1])
        assert np.isnan(result.iloc[2])
        # t2: C=-25 gets rank 1/3, A and B tied at 5 get (2+3)/(2*3) = 5/6
        assert result.iloc[5] == pytest.approx(1.0 / 3)  # C
        assert result.iloc[3] == pytest.approx(5.0 / 6)  # A (tied)
        assert result.iloc[4] == pytest.approx(5.0 / 6)  # B (tied)


# ── Error cases ──────────────────────────────────────────────────────────────


class TestExecutorErrors:
    def test_missing_required_column(self) -> None:
        df = pd.DataFrame({"price": [1, 2, 3]})
        with pytest.raises(DslExecutionError, match="missing required columns"):
            DslExecutor(df)

    def test_division_by_zero_scalar(self) -> None:
        df = _make_single_symbol_df()
        executor = DslExecutor(df)
        with pytest.raises(DslExecutionError, match="Division by zero"):
            executor.execute(parse_expression("close / 0"))

    def test_ts_function_on_scalar(self) -> None:
        """Applying a time-series function to a scalar raises."""
        df = _make_tiny_df()
        with pytest.raises(DslExecutionError, match="scalar"):
            DslExecutor(df).execute(parse_expression("ts_mean(42, 3)"))

    def test_wrong_arg_count(self) -> None:
        """Runtime arity check for ts functions."""
        df = _make_tiny_df()
        # Manually build a bad AST (parser would catch this, but executor
        # validates too)
        bad_ast = {
            "type": "function",
            "name": "ts_mean",
            "args": [{"type": "field", "name": "close"}],
        }
        with pytest.raises(DslExecutionError, match="requires 2"):
            DslExecutor(df).execute(bad_ast)

    def test_unknown_ast_node(self) -> None:
        """Unknown node type raises."""
        df = _make_tiny_df()
        bad_ast = {"type": "unknown_type"}
        with pytest.raises(DslExecutionError, match="Unknown AST node"):
            DslExecutor(df).execute(bad_ast)

    def test_negative_window(self) -> None:
        """Negative window raises at execution time."""
        df = _make_tiny_df()
        bad_ast = {
            "type": "function",
            "name": "ts_mean",
            "args": [
                {"type": "field", "name": "close"},
                {"type": "number", "value": -3},
            ],
        }
        with pytest.raises(DslExecutionError, match="positive"):
            DslExecutor(df).execute(bad_ast)
