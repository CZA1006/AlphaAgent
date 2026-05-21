"""Round 8 Phase B-1 — composite_recipe field + executor + evaluator dispatch."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from alpha_harness.combination import CombinationMethod, CombinationRecipe
from alpha_harness.evaluators.signal_quality import SignalQualityEvaluator
from alpha_harness.factors.composite_executor import execute_composite
from alpha_harness.schemas.evaluation import EvaluationRequest
from alpha_harness.schemas.factor import FactorSpec


def _make_panel(seed: int = 0) -> pd.DataFrame:
    """Small deterministic panel: 5 symbols x 30 days, rng-driven close."""
    rng = np.random.default_rng(seed)
    n_dates = 30
    timestamps = pd.date_range("2023-01-01", periods=n_dates, freq="D", tz="UTC")
    symbols = ["A", "B", "C", "D", "E"]
    frames = []
    for sym in symbols:
        close = 100.0 + rng.standard_normal(n_dates).cumsum()
        frames.append(
            pd.DataFrame(
                {
                    "timestamp": timestamps,
                    "symbol": sym,
                    "close": close,
                    "open": close,
                    "high": close * 1.01,
                    "low": close * 0.99,
                    "volume": (1e6 + rng.standard_normal(n_dates) * 1e5).clip(min=1.0),
                },
            ),
        )
    return pd.concat(frames, ignore_index=True)


# ── execute_composite ──────────────────────────────────────────────────────


def test_execute_composite_returns_aligned_series() -> None:
    df = _make_panel()
    recipe = CombinationRecipe.build(
        method=CombinationMethod.EQUAL_WEIGHT,
        components=["rank(close)", "rank(volume)"],
    )
    sig = execute_composite(recipe, df)
    assert len(sig) == len(df)


def test_execute_composite_rejects_empty_components() -> None:
    df = _make_panel()
    # build() would barf on empty list earlier — bypass via direct construction
    bad = CombinationRecipe(
        method=CombinationMethod.EQUAL_WEIGHT,
        components=[],
        recipe_id="deadbeef",
    )
    with pytest.raises(ValueError, match="no components"):
        execute_composite(bad, df)


def test_execute_composite_wraps_bad_component_as_value_error() -> None:
    df = _make_panel()
    # An expression that parses but executes against a missing column
    bad = CombinationRecipe(
        method=CombinationMethod.EQUAL_WEIGHT,
        components=["rank(this_column_does_not_exist)"],
        recipe_id="deadbeef",
    )
    with pytest.raises(ValueError, match="failed to execute"):
        execute_composite(bad, df)


# ── SignalQualityEvaluator dispatch ────────────────────────────────────────


def test_composite_factor_dispatches_through_recipe() -> None:
    """A FactorSpec with composite_recipe must score identically to running
    the recipe through execute_composite and feeding it to evaluate_precomputed_signal.

    Indirect verification: build a *scalar* factor whose DSL is the basket
    we'd get from equal_weight([rank(close)]) — a 1-component basket is
    just rank(close) under any combination method — and confirm the
    composite path gives the same IC.  Same signal in, same metrics out.
    """
    df = _make_panel()
    evaluator = SignalQualityEvaluator(df)

    scalar = FactorSpec(name="rank_close", expression="rank(close)")
    composite = FactorSpec(
        name="basket",
        expression="<composite:placeholder>",
        composite_recipe=CombinationRecipe.build(
            method=CombinationMethod.EQUAL_WEIGHT,
            components=["rank(close)"],
        ),
    )
    request = EvaluationRequest(
        factor_id="t",
        universe_id="t",
        eval_start=date(2023, 1, 1),
        eval_end=date(2023, 1, 30),
    )

    a = evaluator.evaluate(scalar, request)
    b = evaluator.evaluate(composite, request)

    # 1-component equal_weight basket of rank(close) IS rank(close).
    assert a.ic == pytest.approx(b.ic, nan_ok=True)
    assert a.rank_ic == pytest.approx(b.rank_ic, nan_ok=True)
    assert a.quantile_spread == pytest.approx(b.quantile_spread, nan_ok=True)


def test_scalar_factor_path_unchanged_when_composite_is_none() -> None:
    """Regression guard: existing scalar factors must keep working with
    composite_recipe defaulting to None — i.e. the dispatch doesn't
    accidentally swallow the DSL path.
    """
    df = _make_panel()
    factor = FactorSpec(name="x", expression="rank(close)")
    assert factor.composite_recipe is None  # default
    bundle = SignalQualityEvaluator(df).evaluate(
        factor,
        EvaluationRequest(
            factor_id="t",
            universe_id="t",
            eval_start=date(2023, 1, 1),
            eval_end=date(2023, 1, 30),
        ),
    )
    assert bundle.n_periods > 0
