"""Factor DSL parser — recursive-descent parser for safe factor expressions.

Grammar (EBNF):

    expression  = term (('+' | '-') term)*
    term        = unary (('*' | '/') unary)*
    unary       = '-' unary | atom
    atom        = NUMBER | FIELD | function_call | '(' expression ')'
    function_call = IDENTIFIER '(' arg_list ')'
    arg_list    = expression (',' expression)*

Tokens:
    NUMBER      = integer or float literal (no scientific notation)
    FIELD       = 'open' | 'high' | 'low' | 'close' | 'volume' | 'vwap'
    IDENTIFIER  = function name from the allowed set

Safety:
    - No eval(), no exec(), no arbitrary code.
    - Only whitelisted function names and field names are accepted.
    - The parser produces a dict-based AST; execution is a separate step.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum, auto
from typing import Any

# ── Whitelists ───────────────────────────────────────────────────────────────

ALLOWED_FIELDS = frozenset({"open", "high", "low", "close", "volume", "vwap"})

ALLOWED_FUNCTIONS = frozenset({
    "lag",
    "ts_mean",
    "ts_std",
    "ts_sum",
    "ts_min",
    "ts_max",
    "ts_delta",
    "ts_lag",
    "rank",
    "zscore",
})

# ── Token types ──────────────────────────────────────────────────────────────


class TokenType(StrEnum):
    NUMBER = auto()
    IDENTIFIER = auto()
    PLUS = auto()
    MINUS = auto()
    STAR = auto()
    SLASH = auto()
    LPAREN = auto()
    RPAREN = auto()
    COMMA = auto()
    EOF = auto()


@dataclass(frozen=True)
class Token:
    type: TokenType
    value: str
    pos: int  # character position in source for error messages


# ── AST node types ───────────────────────────────────────────────────────────
# The AST is a plain dict so it serialises directly into FactorSpec.operator_tree.
#
# Node shapes:
#   {"type": "field", "name": "close"}
#   {"type": "number", "value": 20.0}
#   {"type": "function", "name": "ts_mean", "args": [<node>, <node>]}
#   {"type": "binop", "op": "+", "left": <node>, "right": <node>}
#   {"type": "unary", "op": "-", "operand": <node>}


class DslParseError(Exception):
    """Raised when the DSL expression is syntactically or semantically invalid."""


# ── Tokenizer ────────────────────────────────────────────────────────────────

_TOKEN_PATTERN = re.compile(
    r"""
    (?P<NUMBER>   \d+(?:\.\d+)? )  |
    (?P<IDENT>    [a-zA-Z_]\w*  )  |
    (?P<PLUS>     \+            )  |
    (?P<MINUS>    -             )  |
    (?P<STAR>     \*            )  |
    (?P<SLASH>    /             )  |
    (?P<LPAREN>   \(            )  |
    (?P<RPAREN>   \)            )  |
    (?P<COMMA>    ,             )  |
    (?P<WS>       \s+           )  |
    (?P<INVALID>  .             )
    """,
    re.VERBOSE,
)


def tokenize(source: str) -> list[Token]:
    """Tokenize a DSL expression string into a list of Tokens."""
    tokens: list[Token] = []
    for m in _TOKEN_PATTERN.finditer(source):
        kind = m.lastgroup
        value = m.group()
        pos = m.start()

        if kind == "WS":
            continue
        if kind == "INVALID":
            raise DslParseError(f"Unexpected character {value!r} at position {pos}")
        if kind == "NUMBER":
            tokens.append(Token(TokenType.NUMBER, value, pos))
        elif kind == "IDENT":
            tokens.append(Token(TokenType.IDENTIFIER, value, pos))
        elif kind == "PLUS":
            tokens.append(Token(TokenType.PLUS, value, pos))
        elif kind == "MINUS":
            tokens.append(Token(TokenType.MINUS, value, pos))
        elif kind == "STAR":
            tokens.append(Token(TokenType.STAR, value, pos))
        elif kind == "SLASH":
            tokens.append(Token(TokenType.SLASH, value, pos))
        elif kind == "LPAREN":
            tokens.append(Token(TokenType.LPAREN, value, pos))
        elif kind == "RPAREN":
            tokens.append(Token(TokenType.RPAREN, value, pos))
        elif kind == "COMMA":
            tokens.append(Token(TokenType.COMMA, value, pos))

    tokens.append(Token(TokenType.EOF, "", len(source)))
    return tokens


# ── Parser ───────────────────────────────────────────────────────────────────


class Parser:
    """Recursive-descent parser that produces a dict-based AST."""

    def __init__(self, tokens: list[Token]) -> None:
        self._tokens = tokens
        self._pos = 0

    def parse(self) -> dict[str, Any]:
        """Parse the full expression and return the AST root node."""
        node = self._expression()
        if self._current().type != TokenType.EOF:
            tok = self._current()
            raise DslParseError(
                f"Unexpected token {tok.value!r} at position {tok.pos} "
                f"(expected end of expression)"
            )
        return node

    # ── Helpers ──────────────────────────────────────────────────────

    def _current(self) -> Token:
        return self._tokens[self._pos]

    def _advance(self) -> Token:
        tok = self._tokens[self._pos]
        self._pos += 1
        return tok

    def _expect(self, token_type: TokenType) -> Token:
        tok = self._current()
        if tok.type != token_type:
            raise DslParseError(
                f"Expected {token_type.value} at position {tok.pos}, "
                f"got {tok.type.value} ({tok.value!r})"
            )
        return self._advance()

    # ── Grammar rules ────────────────────────────────────────────────

    def _expression(self) -> dict[str, Any]:
        """expression = term (('+' | '-') term)*"""
        left = self._term()
        while self._current().type in (TokenType.PLUS, TokenType.MINUS):
            op_tok = self._advance()
            right = self._term()
            left = {"type": "binop", "op": op_tok.value, "left": left, "right": right}
        return left

    def _term(self) -> dict[str, Any]:
        """term = unary (('*' | '/') unary)*"""
        left = self._unary()
        while self._current().type in (TokenType.STAR, TokenType.SLASH):
            op_tok = self._advance()
            right = self._unary()
            left = {"type": "binop", "op": op_tok.value, "left": left, "right": right}
        return left

    def _unary(self) -> dict[str, Any]:
        """unary = '-' unary | atom"""
        if self._current().type == TokenType.MINUS:
            self._advance()
            operand = self._unary()
            return {"type": "unary", "op": "-", "operand": operand}
        return self._atom()

    def _atom(self) -> dict[str, Any]:
        """atom = NUMBER | FIELD | function_call | '(' expression ')'"""
        tok = self._current()

        # Number literal
        if tok.type == TokenType.NUMBER:
            self._advance()
            return {"type": "number", "value": float(tok.value)}

        # Identifier: either a field name or a function call
        if tok.type == TokenType.IDENTIFIER:
            name = tok.value
            self._advance()

            # Function call: name '(' args ')'
            if self._current().type == TokenType.LPAREN:
                return self._function_call(name, tok.pos)

            # Field reference
            if name in ALLOWED_FIELDS:
                return {"type": "field", "name": name}

            raise DslParseError(
                f"Unknown identifier {name!r} at position {tok.pos}. "
                f"Allowed fields: {sorted(ALLOWED_FIELDS)}. "
                f"Allowed functions: {sorted(ALLOWED_FUNCTIONS)}."
            )

        # Parenthesized expression
        if tok.type == TokenType.LPAREN:
            self._advance()
            node = self._expression()
            self._expect(TokenType.RPAREN)
            return node

        raise DslParseError(
            f"Unexpected token {tok.value!r} at position {tok.pos}"
        )

    def _function_call(self, name: str, name_pos: int) -> dict[str, Any]:
        """Parse function_call = name '(' arg_list ')'"""
        if name not in ALLOWED_FUNCTIONS:
            raise DslParseError(
                f"Unknown function {name!r} at position {name_pos}. "
                f"Allowed functions: {sorted(ALLOWED_FUNCTIONS)}."
            )

        self._expect(TokenType.LPAREN)
        args: list[dict[str, Any]] = []

        # Handle empty arg list (shouldn't happen for our functions, but be safe)
        if self._current().type != TokenType.RPAREN:
            args.append(self._expression())
            while self._current().type == TokenType.COMMA:
                self._advance()
                args.append(self._expression())

        self._expect(TokenType.RPAREN)
        return {"type": "function", "name": name, "args": args}


# ── Public API ───────────────────────────────────────────────────────────────


def parse_expression(source: str) -> dict[str, Any]:
    """Parse a DSL expression string into a dict-based AST.

    Raises DslParseError for syntactically invalid or disallowed expressions.

    Examples:
        >>> parse_expression("rank(ts_mean(close, 20))")
        {'type': 'function', 'name': 'rank', 'args': [
            {'type': 'function', 'name': 'ts_mean', 'args': [
                {'type': 'field', 'name': 'close'},
                {'type': 'number', 'value': 20.0}
            ]}
        ]}
    """
    if not source or not source.strip():
        raise DslParseError("Empty expression")
    tokens = tokenize(source)
    parser = Parser(tokens)
    return parser.parse()


def validate_expression(source: str) -> list[str]:
    """Validate a DSL expression and return a list of errors (empty = valid).

    Does not raise — returns errors as strings for display.
    """
    errors: list[str] = []
    try:
        ast = parse_expression(source)
        errors.extend(validate_ast(ast))
    except DslParseError as e:
        errors.append(str(e))
    return errors


def validate_ast(node: dict[str, Any]) -> list[str]:
    """Walk the AST and check semantic constraints."""
    errors: list[str] = []
    node_type = node.get("type")

    if node_type == "function":
        name = node["name"]
        args = node["args"]
        expected = _function_arity(name)
        if expected is not None and len(args) != expected:
            errors.append(
                f"Function {name!r} expects {expected} argument(s), got {len(args)}"
            )
        # Check that window arguments (2nd arg for ts_* and lag) are positive integers
        if name in ("ts_mean", "ts_std", "ts_sum", "ts_min", "ts_max",
                     "ts_delta", "ts_lag", "lag") and len(args) >= 2:
            window_arg = args[1]
            if window_arg.get("type") != "number":
                errors.append(
                    f"Function {name!r} requires a numeric window argument"
                )
            elif window_arg.get("value", 0) <= 0:
                errors.append(
                    f"Function {name!r} window must be positive, got {window_arg['value']}"
                )
        for arg in args:
            errors.extend(validate_ast(arg))

    elif node_type == "binop":
        errors.extend(validate_ast(node["left"]))
        errors.extend(validate_ast(node["right"]))

    elif node_type == "unary":
        errors.extend(validate_ast(node["operand"]))

    elif node_type in ("field", "number"):
        pass  # leaf nodes, always valid

    else:
        errors.append(f"Unknown AST node type: {node_type!r}")

    return errors


def _function_arity(name: str) -> int | None:
    """Return the expected argument count for a function, or None if variable."""
    arities: dict[str, int] = {
        "lag": 2,
        "ts_mean": 2,
        "ts_std": 2,
        "ts_sum": 2,
        "ts_min": 2,
        "ts_max": 2,
        "ts_delta": 2,
        "ts_lag": 2,
        "rank": 1,
        "zscore": 1,
    }
    return arities.get(name)
