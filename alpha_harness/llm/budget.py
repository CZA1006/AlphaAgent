"""Per-cycle token / cost budget for LLM calls.

Round 4A.1 infrastructure — wraps any :class:`LLMClient` so that a cycle
cannot silently over-spend.  Two orthogonal caps are supported:

* ``max_total_tokens`` — hard cap on cumulative ``total_tokens`` reported
  by the provider.
* ``max_cost_usd`` — hard cap in USD, computed from the optional
  ``prompt_cost_per_1k`` / ``completion_cost_per_1k`` rates.

The pre-check is a *soft fence* (the call is not issued if the budget is
already exceeded).  The post-debit is the real enforcement — if a call
pushes the ledger over either cap, a :class:`BudgetExceededError` is
raised *after* the record is logged, so the call that tripped the limit
is still observable.

Design notes
------------
* The wrapper itself implements :class:`LLMClient`, so downstream code
  (proposer, refiner, structured helper) is unaware of its presence.
* Budget state is mutable and lives on the :class:`TokenBudget` instance;
  callers should create a fresh budget per cycle.
* Typed error — ``BudgetExceededError`` inherits :class:`LLMError` so
  existing ``except LLMError`` paths in the cycle drivers continue to
  work.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from alpha_harness.llm.protocol import (
    LLMClient,
    LLMError,
    LLMRequest,
    LLMResponse,
)


class BudgetExceededError(LLMError):
    """Raised when an LLM call pushes the cycle ledger past its cap."""


@dataclass
class TokenBudget:
    """Mutable per-cycle ledger of token / cost consumption.

    At least one of ``max_total_tokens`` / ``max_cost_usd`` should be set;
    leaving both ``None`` disables enforcement entirely (the wrapper then
    behaves as a pure pass-through, which is still useful because the
    :class:`BudgetedLLMClient` is also the point where a missing ``usage``
    dict would be detected).
    """

    max_total_tokens: int | None = None
    max_cost_usd: float | None = None
    prompt_cost_per_1k: float = 0.0
    completion_cost_per_1k: float = 0.0

    # Running totals.  Do not mutate from outside; use :meth:`debit`.
    total_tokens_spent: int = field(default=0, init=False)
    cost_usd_spent: float = field(default=0.0, init=False)
    calls: int = field(default=0, init=False)

    # ── Queries ──────────────────────────────────────────────────────────

    def remaining_tokens(self) -> int | None:
        if self.max_total_tokens is None:
            return None
        return max(0, self.max_total_tokens - self.total_tokens_spent)

    def remaining_cost_usd(self) -> float | None:
        if self.max_cost_usd is None:
            return None
        return max(0.0, self.max_cost_usd - self.cost_usd_spent)

    def is_exhausted(self) -> bool:
        if (
            self.max_total_tokens is not None
            and self.total_tokens_spent >= self.max_total_tokens
        ):
            return True
        return (
            self.max_cost_usd is not None
            and self.cost_usd_spent >= self.max_cost_usd
        )

    # ── Mutation ─────────────────────────────────────────────────────────

    def debit(
        self,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
    ) -> None:
        """Record one call's usage and raise if it pushes over a cap."""
        self.calls += 1
        self.total_tokens_spent += max(0, total_tokens)
        call_cost = (
            (prompt_tokens / 1000.0) * self.prompt_cost_per_1k
            + (completion_tokens / 1000.0) * self.completion_cost_per_1k
        )
        self.cost_usd_spent += call_cost

        reasons: list[str] = []
        if (
            self.max_total_tokens is not None
            and self.total_tokens_spent > self.max_total_tokens
        ):
            reasons.append(
                f"token budget exceeded: {self.total_tokens_spent} "
                f"> {self.max_total_tokens}"
            )
        if (
            self.max_cost_usd is not None
            and self.cost_usd_spent > self.max_cost_usd
        ):
            reasons.append(
                f"cost budget exceeded: "
                f"${self.cost_usd_spent:.4f} > ${self.max_cost_usd:.4f}"
            )
        if reasons:
            raise BudgetExceededError("; ".join(reasons))


class BudgetedLLMClient:
    """:class:`LLMClient` wrapper that enforces a :class:`TokenBudget`.

    Protocol note
    -------------
    Duck-typed against :class:`~alpha_harness.llm.protocol.LLMClient`;
    structural typing means downstream code can't distinguish this from
    the wrapped provider.
    """

    def __init__(self, inner: LLMClient, budget: TokenBudget) -> None:
        self._inner = inner
        self._budget = budget

    @property
    def budget(self) -> TokenBudget:
        return self._budget

    def complete(self, request: LLMRequest) -> LLMResponse:
        if self._budget.is_exhausted():
            raise BudgetExceededError(
                "Cycle budget already exhausted before this call — refusing "
                f"to issue another request (tokens_spent="
                f"{self._budget.total_tokens_spent}, "
                f"cost_usd={self._budget.cost_usd_spent:.4f})."
            )

        response = self._inner.complete(request)

        usage = response.usage or {}
        prompt = int(usage.get("prompt_tokens", 0))
        completion = int(usage.get("completion_tokens", 0))
        total = int(usage.get("total_tokens", prompt + completion))

        # debit() raises BudgetExceededError if this call overshoots.
        self._budget.debit(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
        )
        return response
