"""Unit tests for :mod:`alpha_harness.llm.budget`."""

from __future__ import annotations

import pytest

from alpha_harness.llm import (
    BudgetedLLMClient,
    BudgetExceededError,
    LLMError,
    LLMMessage,
    LLMRequest,
    LLMResponse,
    MockLLMClient,
    TokenBudget,
    token_budget_from_env,
)


def _mock_with_usage(*usages: dict[str, int]) -> MockLLMClient:
    """Build a MockLLMClient that returns LLMResponses with the given usage dicts."""
    queue = list(usages)

    def handler(_req: LLMRequest) -> LLMResponse:
        usage = queue.pop(0)
        return LLMResponse(
            content="ok",
            model="mock/model",
            finish_reason="stop",
            usage=usage,
        )

    return MockLLMClient(handler=handler)


def _req() -> LLMRequest:
    return LLMRequest(messages=[LLMMessage(role="user", content="hi")])


def test_budget_passthrough_when_under_cap() -> None:
    budget = TokenBudget(max_total_tokens=1000)
    inner = _mock_with_usage({"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30})
    client = BudgetedLLMClient(inner, budget)

    resp = client.complete(_req())

    assert resp.content == "ok"
    assert budget.total_tokens_spent == 30
    assert budget.calls == 1
    assert not budget.is_exhausted()


def test_budget_raises_when_single_call_exceeds() -> None:
    budget = TokenBudget(max_total_tokens=25)
    inner = _mock_with_usage({"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30})
    client = BudgetedLLMClient(inner, budget)

    with pytest.raises(BudgetExceededError, match="token budget exceeded"):
        client.complete(_req())

    # The call *was* issued — ledger records it.
    assert budget.total_tokens_spent == 30
    assert budget.calls == 1


def test_budget_raises_on_second_call_when_cumulative_overshoots() -> None:
    budget = TokenBudget(max_total_tokens=50)
    inner = _mock_with_usage(
        {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    )
    client = BudgetedLLMClient(inner, budget)

    client.complete(_req())  # 30 so far — under 50
    with pytest.raises(BudgetExceededError):
        client.complete(_req())  # would push to 60 — over 50


def test_budget_refuses_further_calls_once_exhausted() -> None:
    budget = TokenBudget(max_total_tokens=30)
    inner = _mock_with_usage(
        {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    )
    client = BudgetedLLMClient(inner, budget)

    client.complete(_req())
    assert budget.is_exhausted()

    # Further calls must *not* be issued; a typed error is raised pre-flight.
    with pytest.raises(BudgetExceededError, match="already exhausted"):
        client.complete(_req())


def test_cost_budget_enforced_with_rates() -> None:
    # $0.01 / 1k prompt tokens, $0.02 / 1k completion tokens.
    budget = TokenBudget(
        max_cost_usd=0.0005,
        prompt_cost_per_1k=0.01,
        completion_cost_per_1k=0.02,
    )
    # 100 prompt + 100 completion = 0.001 + 0.002 = $0.003 — over cap.
    inner = _mock_with_usage({"prompt_tokens": 100, "completion_tokens": 100, "total_tokens": 200})
    client = BudgetedLLMClient(inner, budget)

    with pytest.raises(BudgetExceededError, match="cost budget exceeded"):
        client.complete(_req())


def test_no_caps_set_is_passthrough() -> None:
    budget = TokenBudget()
    inner = _mock_with_usage({"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30})
    client = BudgetedLLMClient(inner, budget)

    for _ in range(3):
        inner._queue = []  # not used, handler mode
    # issue one call; no cap → never raises
    client.complete(_req())
    assert budget.total_tokens_spent == 30


def test_budget_exceeded_error_is_llm_error() -> None:
    # Downstream code already catches LLMError — make sure the typed
    # subclass is picked up.
    assert issubclass(BudgetExceededError, LLMError)


def test_missing_usage_defaults_to_zero() -> None:
    """If the provider doesn't return usage, we don't spuriously debit."""
    budget = TokenBudget(max_total_tokens=10)
    inner = _mock_with_usage({})  # no usage fields at all
    client = BudgetedLLMClient(inner, budget)

    client.complete(_req())

    assert budget.total_tokens_spent == 0
    assert budget.calls == 1


def test_cost_cap_requires_explicit_pricing_rates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALPHA_AGENT_PROMPT_COST_PER_1K", raising=False)
    monkeypatch.delenv("ALPHA_AGENT_COMPLETION_COST_PER_1K", raising=False)

    with pytest.raises(ValueError, match="USD cost budget requires"):
        token_budget_from_env(max_total_tokens=None, max_cost_usd=2.0)


def test_cost_cap_uses_explicit_pricing_rates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPHA_AGENT_PROMPT_COST_PER_1K", "0.001")
    monkeypatch.setenv("ALPHA_AGENT_COMPLETION_COST_PER_1K", "0.002")

    budget = token_budget_from_env(max_total_tokens=10_000, max_cost_usd=2.0)

    assert budget is not None
    assert budget.prompt_cost_per_1k == pytest.approx(0.001)
    assert budget.completion_cost_per_1k == pytest.approx(0.002)
