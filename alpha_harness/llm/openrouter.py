"""OpenRouter concrete client — synchronous HTTPS over ``httpx``.

OpenRouter speaks the OpenAI chat-completions wire format, so this client
simply POSTs to ``{base_url}/chat/completions`` with a bearer token and
parses ``choices[0].message.content`` out of the response.

Transport-level failures (timeouts, non-2xx, malformed JSON body) are
wrapped in :class:`OpenRouterError` so callers only have to catch
:class:`~alpha_harness.llm.protocol.LLMError`.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from alpha_harness.llm.config import OpenRouterConfig
from alpha_harness.llm.protocol import (
    LLMError,
    LLMRequest,
    LLMResponse,
)


class OpenRouterError(LLMError):
    """Any failure from the OpenRouter provider (transport, 4xx/5xx, bad body)."""


class OpenRouterClient:
    """Concrete :class:`~alpha_harness.llm.protocol.LLMClient` for OpenRouter.

    Parameters
    ----------
    config:
        Connection settings.  Normally built via ``OpenRouterConfig.from_env()``.
    http_client:
        Injectable ``httpx.Client``.  Tests can pass a client with a
        ``MockTransport`` attached; leaving this ``None`` creates a fresh
        client bound to the config's timeout.
    """

    def __init__(
        self,
        config: OpenRouterConfig,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._config = config
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client(timeout=config.timeout_seconds)

    # ── Lifecycle ────────────────────────────────────────────────────────

    def close(self) -> None:
        """Close the underlying HTTP client if we own it."""
        if self._owns_client:
            self._http.close()

    def __enter__(self) -> OpenRouterClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ── Public API ───────────────────────────────────────────────────────

    def complete(self, request: LLMRequest) -> LLMResponse:
        """Submit one chat completion to OpenRouter."""
        payload = self._build_payload(request)
        headers = self._build_headers()

        url = f"{self._config.base_url}/chat/completions"
        try:
            response = self._http.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise OpenRouterError(f"HTTP transport error: {exc}") from exc

        if response.status_code >= 400:
            raise OpenRouterError(
                f"OpenRouter returned {response.status_code}: "
                f"{_truncate(response.text, 500)}"
            )

        try:
            body = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise OpenRouterError(
                f"OpenRouter returned non-JSON body: {_truncate(response.text, 500)}"
            ) from exc

        return _parse_response(body)

    # ── Internals ────────────────────────────────────────────────────────

    def _build_payload(self, request: LLMRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model or self._config.model,
            "messages": [m.model_dump() for m in request.messages],
            "temperature": (
                request.temperature
                if request.temperature is not None
                else self._config.temperature
            ),
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.response_format is not None:
            payload["response_format"] = request.response_format
        # Pass through provider-specific extras without schema changes.
        for key, value in request.extra.items():
            payload.setdefault(key, value)
        return payload

    def _build_headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }
        if self._config.http_referer:
            headers["HTTP-Referer"] = self._config.http_referer
        if self._config.app_title:
            headers["X-Title"] = self._config.app_title
        return headers


# ── Response parsing ─────────────────────────────────────────────────────────


def _parse_response(body: dict[str, Any]) -> LLMResponse:
    """Extract content/metadata from an OpenAI-compatible response body."""
    choices = body.get("choices") or []
    if not choices:
        raise OpenRouterError(f"Response contained no choices: {body!r}")

    first = choices[0]
    message = first.get("message") or {}
    content = message.get("content")
    if not isinstance(content, str):
        raise OpenRouterError(
            f"Response choice had no string content: {first!r}"
        )

    usage_raw = body.get("usage") or {}
    usage: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage_raw.get(key)
        if isinstance(value, int):
            usage[key] = value

    return LLMResponse(
        content=content,
        model=str(body.get("model", "")),
        finish_reason=first.get("finish_reason"),
        usage=usage,
        raw=body,
    )


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"
