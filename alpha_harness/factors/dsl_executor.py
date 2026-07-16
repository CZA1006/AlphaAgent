"""Factor DSL executor — walks the AST and computes a signal from a DataFrame.

Execution semantics
-------------------
The executor takes a parsed AST (from dsl_parser) and a pandas DataFrame,
then recursively evaluates each node to produce a pd.Series aligned to the
DataFrame's index.

**Time-series operators** (``lag``, ``ts_mean``, ``ts_std``, ``ts_sum``,
``ts_min``, ``ts_max``, ``ts_delta``, ``ts_lag``) operate *per-symbol over
time*.  When the DataFrame contains a ``symbol`` column, each operator is
applied independently within each symbol group using ``groupby("symbol")``.
When there is no ``symbol`` column (single-symbol case), operators apply to
the entire Series.  Rolling windows use ``min_periods=1`` so they produce
values from the first row onward.

**Cross-sectional operators** (``rank``, ``zscore``) operate *across symbols
within a single timestamp*.  They require a ``symbol`` column to be
meaningful; without one the executor falls back to a time-series expanding
variant (expanding percentile-rank or expanding z-score) — this is useful
for single-symbol backtests but is *not* true cross-sectional ranking.

**Event operators** (``event_decay``) turn a calendar-distance series into a
continuous weight. Missing events receive zero weight, avoiding the sparse
all-zero cross-sections created by hard event-window flags.

**Arithmetic** (``+``, ``-``, ``*``, ``/``, unary ``-``) follows standard
pandas broadcast rules.  Series-on-series division replaces ``inf`` / ``-inf``
with ``NaN`` so downstream metrics see missing data rather than unbounded
values.

Safety: no eval, no exec, no dynamic attribute access.  Every operator is
a pure function in a static dispatch dict.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

# ── Operator implementations ────────────────────────────────────────────────
# Each operator takes (series_or_df, *args) and returns a pd.Series.
# Time-series ops work per-symbol (grouped by 'symbol' if present).
# Cross-sectional ops work per-timestamp (grouped by 'timestamp').


def _ensure_series(value: pd.Series | float) -> pd.Series:
    """Coerce a scalar to a constant series if needed."""
    if isinstance(value, int | float):
        raise DslExecutionError("Cannot apply time-series operator to a scalar")
    return value


def _ts_grouped_apply(
    df: pd.DataFrame,
    signal: pd.Series,
    func: Any,
    window: int,
) -> pd.Series:
    """Apply a rolling window function grouped by symbol."""
    if "symbol" in df.columns:
        groups = df["symbol"]
        result: pd.Series = signal.groupby(groups).transform(func, window)
        return result
    result_single: pd.Series = func(signal, window)
    return result_single


def _rolling_mean(s: pd.Series, window: int) -> pd.Series:
    return s.rolling(window, min_periods=1).mean()


def _rolling_std(s: pd.Series, window: int) -> pd.Series:
    return s.rolling(window, min_periods=1).std()


def _rolling_sum(s: pd.Series, window: int) -> pd.Series:
    return s.rolling(window, min_periods=1).sum()


def _rolling_min(s: pd.Series, window: int) -> pd.Series:
    return s.rolling(window, min_periods=1).min()


def _rolling_max(s: pd.Series, window: int) -> pd.Series:
    return s.rolling(window, min_periods=1).max()


def _ts_delta(s: pd.Series, window: int) -> pd.Series:
    return s - s.shift(window)


def _ts_lag(s: pd.Series, window: int) -> pd.Series:
    return s.shift(window)


# ── Executor ─────────────────────────────────────────────────────────────────


class DslExecutionError(Exception):
    """Raised when factor execution fails at runtime."""


class DslExecutor:
    """Walk a parsed AST and compute a signal Series from a DataFrame.

    Parameters
    ----------
    df:
        Price DataFrame with columns: timestamp, open, high, low, close, volume.
        Optionally: vwap, symbol. Must be sorted by (symbol, timestamp).
    """

    def __init__(self, df: pd.DataFrame) -> None:
        self._df = df
        self._validate_df()

    def _validate_df(self) -> None:
        """Check that the DataFrame has the minimum required columns."""
        required = {"timestamp", "close"}
        missing = required - set(self._df.columns)
        if missing:
            raise DslExecutionError(f"DataFrame missing required columns: {sorted(missing)}")

    def execute(self, ast: dict[str, Any]) -> pd.Series:
        """Execute the AST and return the computed signal Series.

        The returned Series has the same index as the input DataFrame.
        """
        result = self._eval_node(ast)
        if isinstance(result, int | float):
            return pd.Series(result, index=self._df.index)
        return result

    def _eval_node(self, node: dict[str, Any]) -> pd.Series | float:
        """Recursively evaluate an AST node."""
        node_type = node.get("type")

        if node_type == "number":
            return float(node["value"])

        if node_type == "field":
            return self._eval_field(node["name"])

        if node_type == "function":
            return self._eval_function(node["name"], node["args"])

        if node_type == "binop":
            return self._eval_binop(node["op"], node["left"], node["right"])

        if node_type == "unary":
            return self._eval_unary(node["op"], node["operand"])

        raise DslExecutionError(f"Unknown AST node type: {node_type!r}")

    def _eval_field(self, name: str) -> pd.Series:
        """Look up a price field column in the DataFrame."""
        if name not in self._df.columns:
            raise DslExecutionError(
                f"Field {name!r} not found in DataFrame columns: {list(self._df.columns)}"
            )
        return self._df[name].astype(float)

    def _eval_function(self, name: str, args: list[dict[str, Any]]) -> pd.Series:
        """Evaluate a function call node."""
        # Time-series functions: f(signal, window)
        if name in ("ts_mean", "ts_std", "ts_sum", "ts_min", "ts_max", "ts_delta", "ts_lag", "lag"):
            return self._eval_ts_function(name, args)

        # Cross-sectional functions: f(signal)
        if name == "rank":
            return self._eval_rank(args)
        if name == "zscore":
            return self._eval_zscore(args)
        if name == "event_decay":
            return self._eval_event_decay(args)

        raise DslExecutionError(f"Unknown function: {name!r}")

    def _eval_event_decay(self, args: list[dict[str, Any]]) -> pd.Series:
        """Map absolute event distance to an exponential half-life weight."""
        if len(args) != 2:
            raise DslExecutionError(f"Function 'event_decay' requires 2 arguments, got {len(args)}")
        distance = _ensure_series(self._eval_node(args[0]))
        half_life_value = self._eval_node(args[1])
        if not isinstance(half_life_value, int | float) or half_life_value <= 0:
            raise DslExecutionError("Function 'event_decay' half-life must be a positive number")
        weight = pd.Series(
            np.exp(-np.log(2.0) * distance.abs() / float(half_life_value)),
            index=distance.index,
        )
        return weight.fillna(0.0)

    def _eval_ts_function(self, name: str, args: list[dict[str, Any]]) -> pd.Series:
        """Evaluate a time-series windowed function."""
        if len(args) != 2:
            raise DslExecutionError(f"Function {name!r} requires 2 arguments, got {len(args)}")

        signal_val = self._eval_node(args[0])
        window_val = self._eval_node(args[1])

        signal = _ensure_series(signal_val)
        if not isinstance(window_val, int | float):
            raise DslExecutionError(f"Function {name!r} window must be a number")
        window = int(window_val)
        if window <= 0:
            raise DslExecutionError(f"Function {name!r} window must be positive, got {window}")

        ts_ops: dict[str, Any] = {
            "ts_mean": _rolling_mean,
            "ts_std": _rolling_std,
            "ts_sum": _rolling_sum,
            "ts_min": _rolling_min,
            "ts_max": _rolling_max,
            "ts_delta": _ts_delta,
            "ts_lag": _ts_lag,
            "lag": _ts_lag,
        }

        func = ts_ops[name]
        return _ts_grouped_apply(self._df, signal, func, window)

    def _eval_rank(self, args: list[dict[str, Any]]) -> pd.Series:
        """Cross-sectional percentile rank per timestamp."""
        if len(args) != 1:
            raise DslExecutionError(f"Function 'rank' requires 1 argument, got {len(args)}")
        signal_val = self._eval_node(args[0])
        signal = _ensure_series(signal_val)

        if "symbol" in self._df.columns:
            # Cross-sectional: rank within each timestamp across symbols
            return signal.groupby(self._df["timestamp"]).rank(pct=True)
        # Single-symbol: time-series expanding rank
        return signal.rank(pct=True)

    def _eval_zscore(self, args: list[dict[str, Any]]) -> pd.Series:
        """Cross-sectional z-score per timestamp."""
        if len(args) != 1:
            raise DslExecutionError(f"Function 'zscore' requires 1 argument, got {len(args)}")
        signal_val = self._eval_node(args[0])
        signal = _ensure_series(signal_val)

        if "symbol" in self._df.columns:
            grouped = signal.groupby(self._df["timestamp"])
            mean = grouped.transform("mean")
            std = grouped.transform("std")
            # Avoid division by zero
            std = std.replace(0, float("nan"))
            return (signal - mean) / std
        # Single-symbol fallback: expanding zscore
        mean = signal.expanding().mean()
        std = signal.expanding().std()
        std = std.replace(0, float("nan"))
        return (signal - mean) / std

    def _eval_binop(
        self, op: str, left_node: dict[str, Any], right_node: dict[str, Any]
    ) -> pd.Series | float:
        """Evaluate a binary arithmetic operator."""
        left = self._eval_node(left_node)
        right = self._eval_node(right_node)

        if op == "+":
            return left + right
        if op == "-":
            return left - right
        if op == "*":
            return left * right
        if op == "/":
            if isinstance(right, int | float) and right == 0:
                raise DslExecutionError("Division by zero")
            result = left / right
            # Replace inf/-inf with NaN so downstream metrics see missing data
            if isinstance(result, pd.Series):
                result = result.replace([np.inf, -np.inf], np.nan)
            return result

        raise DslExecutionError(f"Unknown operator: {op!r}")

    def _eval_unary(self, op: str, operand_node: dict[str, Any]) -> pd.Series | float:
        """Evaluate a unary operator."""
        operand = self._eval_node(operand_node)
        if op == "-":
            return -operand
        raise DslExecutionError(f"Unknown unary operator: {op!r}")
