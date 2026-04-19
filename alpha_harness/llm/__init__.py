"""LLM provider layer — provider-agnostic client interface + OpenRouter default.

Public surface:
    * ``LLMClient`` protocol (what proposer/refiner modules depend on)
    * ``LLMMessage``, ``LLMRequest``, ``LLMResponse`` typed envelopes
    * ``OpenRouterClient`` concrete implementation of the protocol
    * ``MockLLMClient`` for tests and offline runs
    * ``OpenRouterConfig.from_env`` env-based configuration loader
    * ``request_structured`` helper for schema-constrained JSON completions

Design goals:
    * Provider abstraction via ``Protocol`` (swap implementations without
      changing callers).
    * Raw model text never flows straight into the research loop — structured
      outputs are parsed and Pydantic-validated first.
    * Retries are bounded and explicit; malformed JSON is surfaced back to
      the model with the actual error string.
    * Unit tests must never require a live API key — the default is the
      mock client.
"""

from alpha_harness.llm.budget import (
    BudgetedLLMClient,
    BudgetExceededError,
    TokenBudget,
)
from alpha_harness.llm.call_log import (
    DEFAULT_CALL_LOG_DIR,
    LLMCallLogger,
    LoggingLLMClient,
    default_log_path,
)
from alpha_harness.llm.config import LLMConfigError, OpenRouterConfig
from alpha_harness.llm.mock import MockLLMClient
from alpha_harness.llm.openrouter import OpenRouterClient, OpenRouterError
from alpha_harness.llm.protocol import (
    LLMClient,
    LLMError,
    LLMMessage,
    LLMRequest,
    LLMResponse,
)
from alpha_harness.llm.structured import (
    StructuredLLMError,
    request_structured,
)

__all__ = [
    "DEFAULT_CALL_LOG_DIR",
    "BudgetExceededError",
    "BudgetedLLMClient",
    "LLMCallLogger",
    "LLMClient",
    "LLMConfigError",
    "LLMError",
    "LLMMessage",
    "LLMRequest",
    "LLMResponse",
    "LoggingLLMClient",
    "MockLLMClient",
    "OpenRouterClient",
    "OpenRouterConfig",
    "OpenRouterError",
    "StructuredLLMError",
    "TokenBudget",
    "default_log_path",
    "request_structured",
]
