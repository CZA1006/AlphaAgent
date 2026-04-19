"""Structured, append-only LLM call log.

Every real-mode cycle writes one JSONL file under
``artifacts/llm_calls/{cycle_id}.jsonl``.  Each line is a self-contained
JSON object — safe to grep, tail, or load with ``pandas.read_json(...,
lines=True)``.

Redaction contract
------------------
* No API keys, headers, or bearer tokens are ever written.
* Full prompt text is **not** written — only a SHA-256 fingerprint of
  the concatenated message contents plus a short preview of each
  message's first 80 chars.
* Full response text is truncated to 200 chars plus a SHA-256 of the
  full content.

This lets us analyze call patterns, diff runs, and prove budget
adherence without risking prompt-level secret leakage.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alpha_harness.llm.protocol import (
    LLMClient,
    LLMRequest,
    LLMResponse,
)

_PREVIEW_CHARS_PER_MESSAGE = 80
_PREVIEW_CHARS_RESPONSE = 200


@dataclass
class LLMCallLogger:
    """Append-only JSONL writer for LLM call metadata.

    Single-writer: callers are expected to own exactly one logger per
    ``cycle_id`` / file.  Multiple loggers writing to the same path
    concurrently is unsupported.
    """

    path: Path
    cycle_id: str

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Touch the file so downstream tooling can rely on it existing
        # even before the first call.
        self.path.touch(exist_ok=True)

    def record(self, record: dict[str, Any]) -> None:
        """Append one JSON object as a line."""
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.write("\n")


class LoggingLLMClient:
    """:class:`LLMClient` wrapper that records every call to an ``LLMCallLogger``.

    Parameters
    ----------
    inner:
        The underlying client (real or mock).
    logger:
        The :class:`LLMCallLogger` to append records to.
    purpose:
        Free-form tag describing *why* this call is being made
        (e.g. ``"proposer"``, ``"refinement"``).  Passed through to the
        log record so analyses can bucket by intent.
    """

    def __init__(
        self,
        inner: LLMClient,
        logger: LLMCallLogger,
        *,
        purpose: str = "unspecified",
    ) -> None:
        self._inner = inner
        self._logger = logger
        self._purpose = purpose

    def complete(self, request: LLMRequest) -> LLMResponse:
        start = time.perf_counter()
        error: str | None = None
        response: LLMResponse | None = None
        try:
            response = self._inner.complete(request)
            return response
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            latency_ms = int((time.perf_counter() - start) * 1000)
            record = _build_record(
                cycle_id=self._logger.cycle_id,
                purpose=self._purpose,
                request=request,
                response=response,
                latency_ms=latency_ms,
                error=error,
            )
            # Swallow logging errors — never let observability break a cycle.
            with contextlib.suppress(OSError):
                self._logger.record(record)


# ── Record building ─────────────────────────────────────────────────────────


def _build_record(
    *,
    cycle_id: str,
    purpose: str,
    request: LLMRequest,
    response: LLMResponse | None,
    latency_ms: int,
    error: str | None,
) -> dict[str, Any]:
    usage = (response.usage if response else None) or {}
    resp_record: dict[str, Any] = {}
    if response is not None:
        content = response.content or ""
        resp_record = {
            "model": response.model,
            "finish_reason": response.finish_reason,
            "content_length": len(content),
            "content_preview": _truncate(content, _PREVIEW_CHARS_RESPONSE),
            "content_sha256": _sha256(content),
            "prompt_tokens": int(usage.get("prompt_tokens", 0)),
            "completion_tokens": int(usage.get("completion_tokens", 0)),
            "total_tokens": int(usage.get(
                "total_tokens",
                int(usage.get("prompt_tokens", 0))
                + int(usage.get("completion_tokens", 0)),
            )),
        }

    return {
        "ts": datetime.now(tz=UTC).isoformat(timespec="milliseconds"),
        "cycle_id": cycle_id,
        "purpose": purpose,
        "latency_ms": latency_ms,
        "request": _request_summary(request),
        "response": resp_record,
        "error": error,
    }


def _request_summary(request: LLMRequest) -> dict[str, Any]:
    joined = "\n".join(f"[{m.role}] {m.content}" for m in request.messages)
    messages_meta = [
        {
            "role": m.role,
            "content_length": len(m.content),
            "content_preview": _truncate(m.content, _PREVIEW_CHARS_PER_MESSAGE),
        }
        for m in request.messages
    ]
    return {
        "model": request.model,
        "temperature": request.temperature,
        "max_tokens": request.max_tokens,
        "response_format": request.response_format,
        "n_messages": len(request.messages),
        "messages": messages_meta,
        "fingerprint_sha256": _sha256(joined),
    }


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


# ── Path helpers ────────────────────────────────────────────────────────────


DEFAULT_CALL_LOG_DIR = Path("artifacts/llm_calls")


def default_log_path(cycle_id: str, base_dir: Path | str | None = None) -> Path:
    """Return the canonical JSONL path for a given cycle id."""
    root = Path(base_dir) if base_dir else Path(
        os.environ.get("ALPHA_AGENT_LLM_LOG_DIR", str(DEFAULT_CALL_LOG_DIR))
    )
    return root / f"{cycle_id}.jsonl"
