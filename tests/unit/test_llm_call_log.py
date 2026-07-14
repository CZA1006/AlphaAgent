"""Unit tests for :mod:`alpha_harness.llm.call_log`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alpha_harness.llm import (
    LLMCallLogger,
    LLMError,
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LoggingLLMClient,
    MockLLMClient,
    default_log_path,
)


def _req(secret: str = "top-secret-prompt") -> LLMRequest:
    return LLMRequest(
        messages=[
            LLMMessage(role="system", content="you are a test system"),
            LLMMessage(role="user", content=secret),
        ],
        temperature=0.1,
        max_tokens=128,
    )


def _read_records(path: Path) -> list[dict]:
    lines = path.read_text().splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def test_logger_creates_file_and_writes_jsonl(tmp_path: Path) -> None:
    log_path = tmp_path / "cycle1.jsonl"
    inner = MockLLMClient(
        handler=lambda _r: LLMResponse(
            content="hello world",
            model="mock/model",
            finish_reason="stop",
            usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        )
    )
    call_logger = LLMCallLogger(path=log_path, cycle_id="cycle1")
    client = LoggingLLMClient(inner, call_logger, purpose="unit-test")

    client.complete(_req())
    client.complete(_req())

    records = _read_records(log_path)
    assert len(records) == 2
    for rec in records:
        assert rec["cycle_id"] == "cycle1"
        assert rec["purpose"] == "unit-test"
        assert rec["response"]["content_length"] == len("hello world")
        assert rec["response"]["total_tokens"] == 7
        assert "latency_ms" in rec
        assert rec["error"] is None


def test_logger_records_provider_reported_cost(tmp_path: Path) -> None:
    log_path = tmp_path / "cost.jsonl"
    inner = MockLLMClient(
        handler=lambda _r: LLMResponse(
            content="ok",
            model="mock/model",
            usage={"prompt_tokens": 5, "completion_tokens": 2, "cost": 0.0001},
        )
    )

    LoggingLLMClient(inner, LLMCallLogger(log_path, "cost")).complete(_req())

    [record] = _read_records(log_path)
    assert record["response"]["cost_usd"] == pytest.approx(0.0001)


def test_logger_redacts_full_prompt_content(tmp_path: Path) -> None:
    log_path = tmp_path / "cycle2.jsonl"
    secret = "A" * 500
    inner = MockLLMClient(responses=["ok"])
    call_logger = LLMCallLogger(path=log_path, cycle_id="cycle2")
    client = LoggingLLMClient(inner, call_logger)

    client.complete(_req(secret=secret))

    [rec] = _read_records(log_path)
    msgs = rec["request"]["messages"]
    user_msg = next(m for m in msgs if m["role"] == "user")

    # The full secret must NOT be written; only a preview + fingerprint.
    assert secret not in json.dumps(rec)
    assert user_msg["content_length"] == 500
    assert len(user_msg["content_preview"]) <= 81  # 80 + ellipsis
    assert "fingerprint_sha256" in rec["request"]
    assert len(rec["request"]["fingerprint_sha256"]) == 64


def test_logger_does_not_leak_api_key_lookalikes(tmp_path: Path) -> None:
    """Keys are carried in HTTP headers, never in LLMRequest content; the
    logger only touches request/response content, so no ``sk-`` / bearer
    token should ever appear in the artifact.  This test pins that
    invariant against future regressions (e.g. someone adding a
    ``headers`` field to the log record).
    """
    log_path = tmp_path / "cycle-safety.jsonl"
    fake_key = "sk-or-v1-" + "F" * 48  # OpenRouter-style lookalike
    inner = MockLLMClient(
        handler=lambda _r: LLMResponse(
            content="benign response",
            model="mock/model",
            finish_reason="stop",
            usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        )
    )
    call_logger = LLMCallLogger(path=log_path, cycle_id="cycle-safety")
    client = LoggingLLMClient(inner, call_logger, purpose="safety-probe")

    # A benign prompt that does not itself contain the key.
    client.complete(_req(secret="please summarise"))

    raw = log_path.read_text()
    assert fake_key not in raw
    assert "sk-or-" not in raw
    assert "Authorization" not in raw
    assert "Bearer " not in raw


def test_logger_records_error_on_failure(tmp_path: Path) -> None:
    log_path = tmp_path / "cycle3.jsonl"

    def boom(_req: LLMRequest) -> LLMResponse:
        raise LLMError("kaboom")

    inner = MockLLMClient(handler=boom)
    call_logger = LLMCallLogger(path=log_path, cycle_id="cycle3")
    client = LoggingLLMClient(inner, call_logger)

    with pytest.raises(LLMError):
        client.complete(_req())

    [rec] = _read_records(log_path)
    assert rec["error"] is not None
    assert "kaboom" in rec["error"]
    # Response block is empty on failure.
    assert rec["response"] == {}


def test_default_log_path_honors_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ALPHA_AGENT_LLM_LOG_DIR", str(tmp_path / "custom"))
    p = default_log_path("abcd")
    assert p == tmp_path / "custom" / "abcd.jsonl"


def test_default_log_path_explicit_override(tmp_path: Path) -> None:
    p = default_log_path("xyz", base_dir=tmp_path)
    assert p == tmp_path / "xyz.jsonl"


def test_logger_is_append_only(tmp_path: Path) -> None:
    log_path = tmp_path / "cycle4.jsonl"
    call_logger = LLMCallLogger(path=log_path, cycle_id="cycle4")
    call_logger.record({"a": 1})

    # Build a second logger on the same path; should not truncate.
    another = LLMCallLogger(path=log_path, cycle_id="cycle4")
    another.record({"b": 2})

    records = _read_records(log_path)
    assert records == [{"a": 1}, {"b": 2}]
