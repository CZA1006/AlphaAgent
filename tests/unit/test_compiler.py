"""Tests for the Factor DSL compiler."""

import pytest

from alpha_harness.factors.compiler import DslCompilationError, FactorDslCompiler
from alpha_harness.schemas.hypothesis import Hypothesis


class TestFactorDslCompiler:
    def test_compiles_simple_expression(self) -> None:
        compiler = FactorDslCompiler()
        h = Hypothesis(text="rank(ts_mean(close, 20))")
        factor = compiler.compile(h)

        assert factor.expression == "rank(ts_mean(close, 20))"
        assert factor.operator_tree is not None
        assert factor.operator_tree["type"] == "function"
        assert factor.operator_tree["name"] == "rank"
        assert factor.hypothesis_id == h.id

    def test_compiles_complex_expression(self) -> None:
        compiler = FactorDslCompiler()
        h = Hypothesis(text="zscore(close / ts_mean(close, 20))")
        factor = compiler.compile(h)

        assert factor.operator_tree is not None
        assert factor.operator_tree["type"] == "function"
        assert factor.operator_tree["name"] == "zscore"
        inner = factor.operator_tree["args"][0]
        assert inner["type"] == "binop"
        assert inner["op"] == "/"

    def test_extracts_from_expr_marker(self) -> None:
        compiler = FactorDslCompiler()
        h = Hypothesis(text="Momentum signal for large caps expr: rank(ts_mean(close, 20))")
        factor = compiler.compile(h)

        assert factor.expression == "rank(ts_mean(close, 20))"
        assert factor.operator_tree is not None

    def test_extracts_from_expression_marker(self) -> None:
        compiler = FactorDslCompiler()
        h = Hypothesis(text="Test idea expression: zscore(volume)")
        factor = compiler.compile(h)

        assert factor.expression == "zscore(volume)"

    def test_generates_name_from_expression(self) -> None:
        compiler = FactorDslCompiler()
        h = Hypothesis(text="rank(close)")
        factor = compiler.compile(h)

        assert factor.name == "rank_close"

    def test_rejects_invalid_expression(self) -> None:
        compiler = FactorDslCompiler()
        h = Hypothesis(text="eval(os.system('rm -rf /'))")
        with pytest.raises(DslCompilationError):
            compiler.compile(h)

    def test_rejects_unknown_function(self) -> None:
        compiler = FactorDslCompiler()
        h = Hypothesis(text="exec(close)")
        with pytest.raises(DslCompilationError, match="Unknown function"):
            compiler.compile(h)

    def test_rejects_empty_text(self) -> None:
        compiler = FactorDslCompiler()
        h = Hypothesis(text="")
        with pytest.raises(DslCompilationError):
            compiler.compile(h)

    def test_rejects_wrong_arity(self) -> None:
        compiler = FactorDslCompiler()
        h = Hypothesis(text="ts_mean(close)")
        with pytest.raises(DslCompilationError, match="expects 2"):
            compiler.compile(h)

    def test_compiles_event_decay_with_positive_half_life(self) -> None:
        factor = FactorDslCompiler().compile(
            Hypothesis(text="event_decay(days_to_next_greenshoe_expiry, 10)")
        )

        assert factor.operator_tree is not None
        assert factor.operator_tree["name"] == "event_decay"

    @pytest.mark.parametrize("half_life", ["0", "-5", "close"])
    def test_rejects_invalid_event_decay_half_life(self, half_life: str) -> None:
        with pytest.raises(DslCompilationError, match="half-life"):
            FactorDslCompiler().compile(
                Hypothesis(text=f"event_decay(days_to_next_greenshoe_expiry, {half_life})")
            )

    def test_preserves_hypothesis_id(self) -> None:
        compiler = FactorDslCompiler()
        h = Hypothesis(text="rank(close)")
        factor = compiler.compile(h)
        assert factor.hypothesis_id == h.id

    def test_implements_protocol(self) -> None:
        """FactorDslCompiler satisfies the FactorCompiler protocol."""
        compiler = FactorDslCompiler()
        # Protocol is not @runtime_checkable, so verify structurally
        assert hasattr(compiler, "compile")
        assert callable(compiler.compile)

    def test_all_fields_compile(self) -> None:
        compiler = FactorDslCompiler()
        for field in ("open", "high", "low", "close", "volume"):
            h = Hypothesis(text=f"rank({field})")
            factor = compiler.compile(h)
            assert factor.operator_tree is not None

    def test_all_ts_functions_compile(self) -> None:
        compiler = FactorDslCompiler()
        for func in (
            "ts_mean",
            "ts_std",
            "ts_sum",
            "ts_min",
            "ts_max",
            "ts_delta",
            "ts_lag",
            "lag",
        ):
            h = Hypothesis(text=f"rank({func}(close, 10))")
            factor = compiler.compile(h)
            assert factor.operator_tree is not None
