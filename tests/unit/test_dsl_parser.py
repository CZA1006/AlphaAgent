"""Tests for the Factor DSL parser — tokenizer, parser, and validator."""

import pytest

from alpha_harness.factors.dsl_parser import (
    DslParseError,
    parse_expression,
    tokenize,
    validate_expression,
)

# ── Tokenizer ────────────────────────────────────────────────────────────────


class TestTokenizer:
    def test_simple_field(self) -> None:
        tokens = tokenize("close")
        assert len(tokens) == 2  # IDENTIFIER + EOF
        assert tokens[0].value == "close"

    def test_function_call(self) -> None:
        tokens = tokenize("ts_mean(close, 20)")
        names = [t.value for t in tokens if t.value]
        assert names == ["ts_mean", "(", "close", ",", "20", ")"]

    def test_arithmetic(self) -> None:
        tokens = tokenize("close / volume")
        names = [t.value for t in tokens if t.value]
        assert names == ["close", "/", "volume"]

    def test_whitespace_ignored(self) -> None:
        tokens_a = tokenize("close+volume")
        tokens_b = tokenize("  close  +  volume  ")
        # Same tokens (ignoring position)
        values_a = [t.value for t in tokens_a]
        values_b = [t.value for t in tokens_b]
        assert values_a == values_b

    def test_invalid_character(self) -> None:
        with pytest.raises(DslParseError, match="Unexpected character"):
            tokenize("close @ volume")

    def test_float_number(self) -> None:
        tokens = tokenize("1.5")
        assert tokens[0].value == "1.5"


# ── Parser — valid expressions ───────────────────────────────────────────────


class TestParserValid:
    def test_field_reference(self) -> None:
        ast = parse_expression("close")
        assert ast == {"type": "field", "name": "close"}

    def test_number_literal(self) -> None:
        ast = parse_expression("42")
        assert ast == {"type": "number", "value": 42.0}

    def test_simple_function(self) -> None:
        ast = parse_expression("rank(close)")
        assert ast == {
            "type": "function",
            "name": "rank",
            "args": [{"type": "field", "name": "close"}],
        }

    def test_function_with_window(self) -> None:
        ast = parse_expression("ts_mean(close, 20)")
        assert ast == {
            "type": "function",
            "name": "ts_mean",
            "args": [
                {"type": "field", "name": "close"},
                {"type": "number", "value": 20.0},
            ],
        }

    def test_nested_function(self) -> None:
        ast = parse_expression("rank(ts_mean(close, 20))")
        assert ast["type"] == "function"
        assert ast["name"] == "rank"
        inner = ast["args"][0]
        assert inner["type"] == "function"
        assert inner["name"] == "ts_mean"
        assert inner["args"][0] == {"type": "field", "name": "close"}

    def test_arithmetic_add(self) -> None:
        ast = parse_expression("close + volume")
        assert ast == {
            "type": "binop",
            "op": "+",
            "left": {"type": "field", "name": "close"},
            "right": {"type": "field", "name": "volume"},
        }

    def test_arithmetic_precedence(self) -> None:
        # close + volume * 2 should parse as close + (volume * 2)
        ast = parse_expression("close + volume * 2")
        assert ast["type"] == "binop"
        assert ast["op"] == "+"
        assert ast["right"]["type"] == "binop"
        assert ast["right"]["op"] == "*"

    def test_parenthesized_expression(self) -> None:
        # (close + volume) * 2 should group addition first
        ast = parse_expression("(close + volume) * 2")
        assert ast["type"] == "binop"
        assert ast["op"] == "*"
        assert ast["left"]["type"] == "binop"
        assert ast["left"]["op"] == "+"

    def test_division_in_function(self) -> None:
        ast = parse_expression("zscore(close / ts_mean(close, 20))")
        assert ast["type"] == "function"
        assert ast["name"] == "zscore"
        inner = ast["args"][0]
        assert inner["type"] == "binop"
        assert inner["op"] == "/"

    def test_unary_minus(self) -> None:
        ast = parse_expression("-close")
        assert ast == {
            "type": "unary",
            "op": "-",
            "operand": {"type": "field", "name": "close"},
        }

    def test_complex_expression(self) -> None:
        # rank(volume / ts_mean(volume, 20))
        ast = parse_expression("rank(volume / ts_mean(volume, 20))")
        assert ast["type"] == "function"
        assert ast["name"] == "rank"
        div = ast["args"][0]
        assert div["type"] == "binop"
        assert div["op"] == "/"
        assert div["left"] == {"type": "field", "name": "volume"}
        assert div["right"]["type"] == "function"
        assert div["right"]["name"] == "ts_mean"

    def test_all_fields(self) -> None:
        for field in ("open", "high", "low", "close", "volume", "vwap"):
            ast = parse_expression(field)
            assert ast == {"type": "field", "name": field}

    def test_all_functions_parse(self) -> None:
        # Every allowed function should parse
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
            ast = parse_expression(f"{func}(close, 10)")
            assert ast["type"] == "function"
            assert ast["name"] == func

        for func in ("rank", "zscore"):
            ast = parse_expression(f"{func}(close)")
            assert ast["type"] == "function"
            assert ast["name"] == func

    def test_multiple_nested_levels(self) -> None:
        expr = "rank(ts_mean(close - ts_lag(close, 1), 20))"
        ast = parse_expression(expr)
        assert ast["type"] == "function"
        assert ast["name"] == "rank"


# ── Parser — invalid expressions ─────────────────────────────────────────────


class TestParserInvalid:
    def test_empty_expression(self) -> None:
        with pytest.raises(DslParseError, match="Empty expression"):
            parse_expression("")

    def test_whitespace_only(self) -> None:
        with pytest.raises(DslParseError, match="Empty expression"):
            parse_expression("   ")

    def test_unknown_function(self) -> None:
        with pytest.raises(DslParseError, match="Unknown function"):
            parse_expression("eval(close)")

    def test_unknown_identifier(self) -> None:
        with pytest.raises(DslParseError, match="Unknown identifier"):
            parse_expression("price")

    def test_unclosed_paren(self) -> None:
        with pytest.raises(DslParseError):
            parse_expression("rank(close")

    def test_extra_paren(self) -> None:
        with pytest.raises(DslParseError):
            parse_expression("rank(close))")

    def test_missing_argument(self) -> None:
        with pytest.raises(DslParseError):
            parse_expression("ts_mean(close,)")

    def test_dangling_operator(self) -> None:
        with pytest.raises(DslParseError):
            parse_expression("close +")

    def test_double_operator(self) -> None:
        # "close ++ volume" — second + is unary, then 'volume' is valid
        # Actually "close + + volume" parses as close + (+volume) which
        # would fail because +unary is not in grammar. Let's test properly:
        with pytest.raises(DslParseError):
            parse_expression("close * /  volume")

    def test_exec_blocked(self) -> None:
        with pytest.raises(DslParseError, match="Unknown function"):
            parse_expression("exec(close)")

    def test_import_blocked(self) -> None:
        # 'import' is not in allowed functions or fields
        with pytest.raises(DslParseError):
            parse_expression("import(os)")


# ── Validator ────────────────────────────────────────────────────────────────


class TestValidator:
    def test_valid_expression(self) -> None:
        errors = validate_expression("rank(ts_mean(close, 20))")
        assert errors == []

    def test_wrong_arity(self) -> None:
        errors = validate_expression("rank(close, 10)")
        assert any("expects 1 argument" in e for e in errors)

    def test_wrong_arity_ts(self) -> None:
        errors = validate_expression("ts_mean(close)")
        assert any("expects 2 argument" in e for e in errors)

    def test_negative_window(self) -> None:
        # Parser accepts -1 as unary minus on 1, but validator should catch it
        # Actually "ts_mean(close, -1)" parses the window as unary(-1) which
        # is not a "number" node but a "unary" node, so validation catches it
        errors = validate_expression("ts_mean(close, -1)")
        assert any("numeric window" in e for e in errors)

    def test_zero_window(self) -> None:
        errors = validate_expression("ts_mean(close, 0)")
        assert any("positive" in e for e in errors)

    def test_complex_valid(self) -> None:
        errors = validate_expression("zscore(close / ts_mean(close, 20))")
        assert errors == []

    def test_parse_error_in_validate(self) -> None:
        errors = validate_expression("eval(close)")
        assert len(errors) > 0


class TestIntradayCandidateFields:
    """Intraday v1 candidate fields are DSL-addressable (opt-in loader join)."""

    def test_intraday_fields_parse(self) -> None:
        for field in (
            "first_hour_ofi",
            "first_hour_rel_spread",
            "opening_auction_trade_share",
            "first_hour_spread_shock",
            "first_hour_liquidity_withdrawal",
            "prior_20d_first_hour_tick_volume",
        ):
            ast = parse_expression(f"rank({field})")
            assert ast["args"][0] == {"type": "field", "name": field}
