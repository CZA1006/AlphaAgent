"""LLM provider protocol and typed request/response envelopes.

Everything downstream (proposer, refiner, Hermes adapter) depends only on
:class:`LLMClient` — never on a concrete provider.  This keeps tests
honest: a :class:`~alpha_harness.llm.mock.MockLLMClient` can stand in for
the real provider with zero code changes.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class LLMError(Exception):
    """Base class for all LLM-layer failures (transport, config, validation)."""


# ── Typed envelopes ──────────────────────────────────────────────────────────


Role = Literal["system", "user", "assistant"]


class LLMMessage(BaseModel):
    """A single chat turn."""

    role: Role
    content: str


class LLMRequest(BaseModel):
    """Inputs for a single completion call.

    Fields default to ``None`` so the concrete provider can fall back to its
    configured defaults (model, temperature) when the caller leaves them
    unspecified.
    """

    messages: list[LLMMessage]
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    # OpenAI-compatible response_format payload, e.g. ``{"type": "json_object"}``.
    response_format: dict[str, Any] | None = None
    # Provider-specific knobs callers may need to plumb through without
    # enlarging this schema (stop sequences, top_p, …).
    extra: dict[str, Any] = Field(default_factory=dict)


class LLMResponse(BaseModel):
    """Normalized completion output.

    ``content`` is always a string — structured JSON parsing is handled by
    :func:`~alpha_harness.llm.structured.request_structured` on top.
    """

    content: str
    model: str
    finish_reason: str | None = None
    usage: dict[str, int | float | bool] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)


# ── Protocol ─────────────────────────────────────────────────────────────────


@runtime_checkable
class LLMClient(Protocol):
    """Provider-agnostic chat-completion interface.

    Implementations must be synchronous and deterministic in their error
    taxonomy: any failure should surface as :class:`LLMError` or a subclass,
    never as a bare ``httpx`` / vendor exception.
    """

    def complete(self, request: LLMRequest) -> LLMResponse:
        """Submit a single completion request and return the normalized response."""
        ...
