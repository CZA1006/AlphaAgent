"""Mock LLM client for tests and offline runs.

Two construction modes:

    * ``MockLLMClient(responses=[...])`` — pre-loaded queue of string
      responses; each :meth:`complete` call pops the next one.
    * ``MockLLMClient(handler=lambda req: "…")`` — callable that sees the
      full request and decides what to return.  Useful for simulating
      invalid-then-valid schema retries.

All calls are recorded on :attr:`calls` so tests can assert on prompts.
"""

from __future__ import annotations

from collections.abc import Callable

from alpha_harness.llm.protocol import (
    LLMError,
    LLMRequest,
    LLMResponse,
)

# A handler either returns a plain string (treated as content) or a full
# ``LLMResponse`` when the test wants to exercise finish_reason / usage paths.
MockHandler = Callable[[LLMRequest], str | LLMResponse]


class MockLLMClient:
    """In-process stand-in for any :class:`~alpha_harness.llm.protocol.LLMClient`."""

    def __init__(
        self,
        responses: list[str] | None = None,
        handler: MockHandler | None = None,
        model: str = "mock/model",
    ) -> None:
        if responses is None and handler is None:
            raise ValueError("MockLLMClient requires either `responses` or `handler`.")
        if responses is not None and handler is not None:
            raise ValueError("MockLLMClient takes `responses` *or* `handler`, not both.")
        self._queue: list[str] = list(responses) if responses else []
        self._handler = handler
        self._model = model
        self.calls: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)

        if self._handler is not None:
            result = self._handler(request)
            if isinstance(result, LLMResponse):
                return result
            return LLMResponse(
                content=result,
                model=request.model or self._model,
                finish_reason="stop",
            )

        if not self._queue:
            raise LLMError(
                "MockLLMClient response queue is empty — no more responses "
                "queued but `complete` was called."
            )
        content = self._queue.pop(0)
        return LLMResponse(
            content=content,
            model=request.model or self._model,
            finish_reason="stop",
        )
