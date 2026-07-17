"""LLM configuration loader.

Env vars (see ``OpenRouterConfig.from_env``):

    * ``OPENROUTER_API_KEY``       — required for the real provider
    * ``OPENROUTER_BASE_URL``      — default ``https://openrouter.ai/api/v1``
    * ``OPENROUTER_MODEL``         — required model identifier
    * ``OPENROUTER_TEMPERATURE``   — default ``0.2``
    * ``OPENROUTER_TIMEOUT``       — HTTP timeout in seconds, default ``60``
    * ``OPENROUTER_HTTP_REFERER``  — optional attribution header
    * ``OPENROUTER_APP_TITLE``     — optional attribution header
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from alpha_harness.llm.protocol import LLMError

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_TEMPERATURE = 0.2
DEFAULT_TIMEOUT = 60.0


class LLMConfigError(LLMError):
    """Raised when required LLM configuration is missing or malformed."""


@dataclass(frozen=True)
class OpenRouterConfig:
    """Immutable OpenRouter connection settings.

    Construct directly for tests / custom setups, or via
    :meth:`from_env` for the default local-development path.
    """

    api_key: str
    model: str
    base_url: str = DEFAULT_BASE_URL
    temperature: float = DEFAULT_TEMPERATURE
    timeout_seconds: float = DEFAULT_TIMEOUT
    http_referer: str | None = None
    app_title: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> OpenRouterConfig:
        """Build a config from environment variables.

        Raises :class:`LLMConfigError` if ``OPENROUTER_API_KEY`` is unset or
        if a numeric field fails to parse — this ensures the real provider
        path fails loudly rather than sending unauthenticated requests.
        """
        source: Mapping[str, str] = env if env is not None else os.environ

        api_key = source.get("OPENROUTER_API_KEY", "").strip()
        if not api_key:
            raise LLMConfigError(
                "OPENROUTER_API_KEY is not set — cannot construct a real "
                "OpenRouter client.  Set the env var or use MockLLMClient "
                "for offline runs."
            )

        model = source.get("OPENROUTER_MODEL", "").strip()
        if not model:
            raise LLMConfigError(
                "OPENROUTER_MODEL is not set — choose an OpenRouter model "
                "available in this region and set the env var explicitly."
            )

        base_url = source.get("OPENROUTER_BASE_URL", DEFAULT_BASE_URL).strip()

        try:
            temperature = float(source.get("OPENROUTER_TEMPERATURE", DEFAULT_TEMPERATURE))
        except ValueError as exc:
            raise LLMConfigError(
                f"OPENROUTER_TEMPERATURE must be a float, got "
                f"{source.get('OPENROUTER_TEMPERATURE')!r}"
            ) from exc

        try:
            timeout = float(source.get("OPENROUTER_TIMEOUT", DEFAULT_TIMEOUT))
        except ValueError as exc:
            raise LLMConfigError(
                f"OPENROUTER_TIMEOUT must be a float, got {source.get('OPENROUTER_TIMEOUT')!r}"
            ) from exc

        referer = source.get("OPENROUTER_HTTP_REFERER") or None
        title = source.get("OPENROUTER_APP_TITLE") or None

        return cls(
            api_key=api_key,
            base_url=base_url.rstrip("/"),
            model=model,
            temperature=temperature,
            timeout_seconds=timeout,
            http_referer=referer,
            app_title=title,
        )
