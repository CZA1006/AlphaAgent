"""Schema-constrained completion helper.

:func:`request_structured` is the only entry point the research loop should
use to get JSON back from an LLM: it asks for JSON-mode output, parses the
body, validates against a Pydantic model, and retries a bounded number of
times with the actual parse/validation error fed back to the model.

Free-form model text therefore never reaches the proposer/refiner — every
downstream consumer sees a validated Pydantic instance or an explicit
:class:`StructuredLLMError`.
"""

from __future__ import annotations

import json
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from alpha_harness.llm.protocol import (
    LLMClient,
    LLMError,
    LLMMessage,
    LLMRequest,
)

T = TypeVar("T", bound=BaseModel)


class StructuredLLMError(LLMError):
    """Raised when a model cannot produce schema-valid output within the retry budget.

    Carries the attempt history so callers / logs can inspect every raw
    response and its corresponding error.
    """

    def __init__(
        self,
        message: str,
        attempts: list[tuple[str, str]],
    ) -> None:
        super().__init__(message)
        # List of (raw_content, error_description) per attempt.
        self.attempts = attempts


JSON_RESPONSE_FORMAT: dict[str, str] = {"type": "json_object"}


def request_structured(
    client: LLMClient,
    messages: list[LLMMessage],
    schema: type[T],
    *,
    max_attempts: int = 3,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> T:
    """Request a JSON object matching ``schema`` with bounded, explicit retries.

    Parameters
    ----------
    client:
        Any ``LLMClient`` — real or mock.
    messages:
        Seed conversation.  The caller is responsible for including a system
        prompt that describes the expected JSON shape; we additionally pass
        ``response_format={"type": "json_object"}`` to providers that honor
        it (OpenRouter + OpenAI-compatible models do).
    schema:
        Pydantic model class.  Its JSON schema is not serialized into the
        prompt — instructing the model is the caller's responsibility.
    max_attempts:
        Hard cap on total completion calls.  Must be ``>= 1``.  On each
        failure, the raw model output and the concrete parse/validation
        error are appended to the conversation as an ``assistant`` +
        ``user`` turn so the next attempt sees exactly what went wrong.

    Raises
    ------
    StructuredLLMError
        When every attempt yields malformed JSON or schema-invalid data.
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")

    conversation: list[LLMMessage] = list(messages)
    attempts: list[tuple[str, str]] = []

    for attempt_index in range(max_attempts):
        request = LLMRequest(
            messages=list(conversation),
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=JSON_RESPONSE_FORMAT,
        )
        response = client.complete(request)
        raw = response.content

        try:
            parsed = _parse_json_object(raw)
        except _StructuredParseError as exc:
            attempts.append((raw, str(exc)))
            if attempt_index == max_attempts - 1:
                break
            conversation.extend(_repair_turns(raw, str(exc)))
            continue

        try:
            return schema.model_validate(parsed)
        except ValidationError as exc:
            err_str = _format_validation_error(exc)
            attempts.append((raw, err_str))
            if attempt_index == max_attempts - 1:
                break
            conversation.extend(_repair_turns(raw, err_str))

    raise StructuredLLMError(
        f"Model failed to produce schema-valid JSON after {max_attempts} attempt(s).",
        attempts=attempts,
    )


# ── Internals ────────────────────────────────────────────────────────────────


class _StructuredParseError(Exception):
    """Internal marker for JSON-parse failures (not exported)."""


def _parse_json_object(raw: str) -> object:
    """Parse a JSON object out of the model output.

    Tolerant of a single fenced ```json ... ``` block, which some models
    emit even in JSON mode.  Anything else must already be valid JSON.
    """
    stripped = raw.strip()
    if stripped.startswith("```"):
        # Drop the first fence line and any trailing fence.
        inner = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        if inner.endswith("```"):
            inner = inner[: -len("```")]
        stripped = inner.strip()

    if not stripped:
        raise _StructuredParseError("Model returned empty content.")

    try:
        return json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise _StructuredParseError(
            f"Content is not valid JSON: {exc.msg} at line {exc.lineno} col {exc.colno}."
        ) from exc


def _format_validation_error(exc: ValidationError) -> str:
    """Turn a Pydantic ``ValidationError`` into a compact single-line message."""
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()))
        msg = err.get("msg", "invalid")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "Schema validation failed: " + "; ".join(parts)


def _repair_turns(raw_output: str, error_description: str) -> list[LLMMessage]:
    """Build the (assistant, user) follow-up pair that surfaces the error."""
    return [
        LLMMessage(role="assistant", content=raw_output),
        LLMMessage(
            role="user",
            content=(
                "The previous response was not valid JSON matching the required "
                f"schema.  Error: {error_description}\n\n"
                "Reply with a single JSON object that exactly satisfies the "
                "schema.  Do not include any prose, explanation, or markdown "
                "fences."
            ),
        ),
    ]
