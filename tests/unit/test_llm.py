"""Tests for the LLM provider layer.

No live API calls — OpenRouter is exercised via ``httpx.MockTransport``.
Everything else goes through the in-process mock client.
"""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import BaseModel, Field

from alpha_harness.llm import (
    LLMClient,
    LLMConfigError,
    LLMMessage,
    LLMRequest,
    MockLLMClient,
    OpenRouterClient,
    OpenRouterConfig,
    OpenRouterError,
    StructuredLLMError,
    request_structured,
)

# ── Protocol conformance ─────────────────────────────────────────────────────


class TestProtocol:
    def test_mock_client_satisfies_protocol(self) -> None:
        mock = MockLLMClient(responses=["hello"])
        assert isinstance(mock, LLMClient)

    def test_openrouter_client_satisfies_protocol(self) -> None:
        transport = httpx.MockTransport(lambda r: httpx.Response(200, json={}))
        client = OpenRouterClient(
            OpenRouterConfig(api_key="sk-test"),
            http_client=httpx.Client(transport=transport),
        )
        assert isinstance(client, LLMClient)
        client.close()


# ── Config ───────────────────────────────────────────────────────────────────


class TestOpenRouterConfig:
    def test_from_env_happy_path(self) -> None:
        env = {
            "OPENROUTER_API_KEY": "sk-xxx",
            "OPENROUTER_MODEL": "anthropic/claude-3.5-sonnet",
            "OPENROUTER_TEMPERATURE": "0.5",
        }
        cfg = OpenRouterConfig.from_env(env)
        assert cfg.api_key == "sk-xxx"
        assert cfg.model == "anthropic/claude-3.5-sonnet"
        assert cfg.temperature == 0.5
        assert cfg.base_url == "https://openrouter.ai/api/v1"

    def test_from_env_defaults(self) -> None:
        from alpha_harness.llm.config import DEFAULT_MODEL

        cfg = OpenRouterConfig.from_env({"OPENROUTER_API_KEY": "sk-xxx"})
        assert cfg.model == DEFAULT_MODEL
        assert cfg.temperature == 0.2
        assert cfg.timeout_seconds == 60.0

    def test_missing_api_key_raises(self) -> None:
        with pytest.raises(LLMConfigError, match="OPENROUTER_API_KEY"):
            OpenRouterConfig.from_env({})

    def test_empty_api_key_raises(self) -> None:
        with pytest.raises(LLMConfigError, match="OPENROUTER_API_KEY"):
            OpenRouterConfig.from_env({"OPENROUTER_API_KEY": "   "})

    def test_bad_temperature_raises(self) -> None:
        with pytest.raises(LLMConfigError, match="OPENROUTER_TEMPERATURE"):
            OpenRouterConfig.from_env({"OPENROUTER_API_KEY": "sk", "OPENROUTER_TEMPERATURE": "hot"})

    def test_base_url_trailing_slash_stripped(self) -> None:
        cfg = OpenRouterConfig.from_env(
            {
                "OPENROUTER_API_KEY": "sk",
                "OPENROUTER_BASE_URL": "https://example.com/v1/",
            }
        )
        assert cfg.base_url == "https://example.com/v1"


# ── OpenRouter HTTP transport ────────────────────────────────────────────────


def _mock_transport(
    *,
    status: int = 200,
    body: dict | None = None,
    text: str | None = None,
    captured: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(request)
        if text is not None:
            return httpx.Response(status, text=text)
        return httpx.Response(status, json=body or {})

    return httpx.MockTransport(handler)


def _ok_body(content: str = "hi there", model: str = "anthropic/claude-3.5-sonnet") -> dict:
    return {
        "id": "gen-1",
        "model": model,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    }


class TestOpenRouterClient:
    def test_complete_parses_response(self) -> None:
        http = httpx.Client(transport=_mock_transport(body=_ok_body("hello world")))
        client = OpenRouterClient(OpenRouterConfig(api_key="sk"), http_client=http)

        response = client.complete(LLMRequest(messages=[LLMMessage(role="user", content="hi")]))

        assert response.content == "hello world"
        assert response.model == "anthropic/claude-3.5-sonnet"
        assert response.finish_reason == "stop"
        assert response.usage == {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}

    def test_complete_preserves_provider_reported_cost(self) -> None:
        body = _ok_body()
        body["usage"]["cost"] = 0.000321
        http = httpx.Client(transport=_mock_transport(body=body))

        response = OpenRouterClient(OpenRouterConfig(api_key="sk"), http_client=http).complete(
            LLMRequest(messages=[LLMMessage(role="user", content="hi")])
        )

        assert response.usage["cost"] == pytest.approx(0.000321)

    def test_sends_auth_and_attribution_headers(self) -> None:
        captured: list[httpx.Request] = []
        http = httpx.Client(transport=_mock_transport(body=_ok_body(), captured=captured))
        cfg = OpenRouterConfig(
            api_key="sk-123",
            http_referer="https://alpha.test",
            app_title="alpha-agent-tests",
        )
        OpenRouterClient(cfg, http_client=http).complete(
            LLMRequest(messages=[LLMMessage(role="user", content="hi")])
        )

        assert len(captured) == 1
        req = captured[0]
        assert req.headers["authorization"] == "Bearer sk-123"
        assert req.headers["http-referer"] == "https://alpha.test"
        assert req.headers["x-title"] == "alpha-agent-tests"

    def test_payload_uses_request_overrides(self) -> None:
        captured: list[httpx.Request] = []
        http = httpx.Client(transport=_mock_transport(body=_ok_body(), captured=captured))
        client = OpenRouterClient(
            OpenRouterConfig(api_key="sk", model="default/model", temperature=0.2),
            http_client=http,
        )
        client.complete(
            LLMRequest(
                messages=[LLMMessage(role="user", content="hi")],
                model="override/model",
                temperature=0.9,
                max_tokens=256,
                response_format={"type": "json_object"},
            )
        )

        body = json.loads(captured[0].content)
        assert body["model"] == "override/model"
        assert body["temperature"] == 0.9
        assert body["max_tokens"] == 256
        assert body["response_format"] == {"type": "json_object"}

    def test_http_error_wrapped(self) -> None:
        http = httpx.Client(transport=_mock_transport(status=500, text="boom"))
        client = OpenRouterClient(OpenRouterConfig(api_key="sk"), http_client=http)

        with pytest.raises(OpenRouterError, match="500"):
            client.complete(LLMRequest(messages=[LLMMessage(role="user", content="hi")]))

    def test_malformed_body_raises(self) -> None:
        http = httpx.Client(transport=_mock_transport(status=200, text="not json"))
        client = OpenRouterClient(OpenRouterConfig(api_key="sk"), http_client=http)

        with pytest.raises(OpenRouterError, match="non-JSON"):
            client.complete(LLMRequest(messages=[LLMMessage(role="user", content="hi")]))

    def test_empty_choices_raises(self) -> None:
        http = httpx.Client(transport=_mock_transport(body={"choices": []}))
        client = OpenRouterClient(OpenRouterConfig(api_key="sk"), http_client=http)

        with pytest.raises(OpenRouterError, match="no choices"):
            client.complete(LLMRequest(messages=[LLMMessage(role="user", content="hi")]))

    def test_context_manager_closes_owned_client(self) -> None:
        with OpenRouterClient(OpenRouterConfig(api_key="sk")) as client:
            assert client is not None
        # Nothing to assert — just verifying the dunder methods are wired.


# ── MockLLMClient ────────────────────────────────────────────────────────────


class TestMockLLMClient:
    def test_queue_pops_in_order(self) -> None:
        mock = MockLLMClient(responses=["one", "two"])
        req = LLMRequest(messages=[LLMMessage(role="user", content="x")])
        assert mock.complete(req).content == "one"
        assert mock.complete(req).content == "two"

    def test_empty_queue_raises(self) -> None:
        mock = MockLLMClient(responses=["only"])
        req = LLMRequest(messages=[LLMMessage(role="user", content="x")])
        mock.complete(req)
        with pytest.raises(Exception, match="queue is empty"):
            mock.complete(req)

    def test_handler_sees_request(self) -> None:
        mock = MockLLMClient(handler=lambda req: f"echo:{req.messages[-1].content}")
        req = LLMRequest(messages=[LLMMessage(role="user", content="ping")])
        assert mock.complete(req).content == "echo:ping"

    def test_records_calls(self) -> None:
        mock = MockLLMClient(responses=["a"])
        req = LLMRequest(messages=[LLMMessage(role="user", content="hello")])
        mock.complete(req)
        assert len(mock.calls) == 1
        assert mock.calls[0].messages[0].content == "hello"

    def test_rejects_both_modes(self) -> None:
        with pytest.raises(ValueError):
            MockLLMClient(responses=["x"], handler=lambda r: "y")

    def test_rejects_neither_mode(self) -> None:
        with pytest.raises(ValueError):
            MockLLMClient()


# ── Structured output ────────────────────────────────────────────────────────


class ProposalSchema(BaseModel):
    hypothesis: str
    rationale: str = ""
    confidence: float = Field(ge=0.0, le=1.0)


class TestRequestStructured:
    def test_happy_path_parses_and_validates(self) -> None:
        mock = MockLLMClient(
            responses=[
                json.dumps(
                    {
                        "hypothesis": "ts_mean(close, 20)",
                        "rationale": "momentum",
                        "confidence": 0.7,
                    }
                ),
            ]
        )
        result = request_structured(
            mock,
            [LLMMessage(role="user", content="propose")],
            ProposalSchema,
        )
        assert isinstance(result, ProposalSchema)
        assert result.hypothesis == "ts_mean(close, 20)"
        assert result.confidence == 0.7

    def test_sets_json_response_format(self) -> None:
        mock = MockLLMClient(responses=[json.dumps({"hypothesis": "x", "confidence": 0.5})])
        request_structured(
            mock,
            [LLMMessage(role="user", content="q")],
            ProposalSchema,
        )
        assert mock.calls[0].response_format == {"type": "json_object"}

    def test_retries_on_invalid_json(self) -> None:
        mock = MockLLMClient(
            responses=[
                "not json at all",
                json.dumps({"hypothesis": "x", "confidence": 0.5}),
            ]
        )
        result = request_structured(
            mock,
            [LLMMessage(role="user", content="q")],
            ProposalSchema,
            max_attempts=3,
        )
        assert result.hypothesis == "x"
        # Second call must include a repair turn carrying the error.
        assert len(mock.calls) == 2
        second_msgs = mock.calls[1].messages
        assert second_msgs[-1].role == "user"
        assert "not valid JSON" in second_msgs[-1].content

    def test_retries_on_schema_violation(self) -> None:
        mock = MockLLMClient(
            responses=[
                json.dumps({"hypothesis": "x", "confidence": 5.0}),  # > 1.0
                json.dumps({"hypothesis": "x", "confidence": 0.5}),
            ]
        )
        result = request_structured(
            mock,
            [LLMMessage(role="user", content="q")],
            ProposalSchema,
            max_attempts=3,
        )
        assert result.confidence == 0.5
        second_msgs = mock.calls[1].messages
        assert "Schema validation failed" in second_msgs[-1].content

    def test_gives_up_after_max_attempts(self) -> None:
        mock = MockLLMClient(responses=["nope", "still nope"])
        with pytest.raises(StructuredLLMError) as excinfo:
            request_structured(
                mock,
                [LLMMessage(role="user", content="q")],
                ProposalSchema,
                max_attempts=2,
            )
        assert len(excinfo.value.attempts) == 2
        assert len(mock.calls) == 2

    def test_strips_markdown_fences(self) -> None:
        mock = MockLLMClient(
            responses=[
                "```json\n" + json.dumps({"hypothesis": "x", "confidence": 0.5}) + "\n```",
            ]
        )
        result = request_structured(
            mock,
            [LLMMessage(role="user", content="q")],
            ProposalSchema,
        )
        assert result.hypothesis == "x"

    def test_zero_attempts_rejected(self) -> None:
        mock = MockLLMClient(responses=["x"])
        with pytest.raises(ValueError, match="max_attempts"):
            request_structured(
                mock,
                [LLMMessage(role="user", content="q")],
                ProposalSchema,
                max_attempts=0,
            )
