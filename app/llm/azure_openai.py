"""Azure OpenAI chat client, using the official SDK (`openai` package, AsyncAzureOpenAI).

Same code and settings locally and on Cloud Foundry: the SDK calls AZURE_OPENAI_ENDPOINT directly.
The SDK requests  {endpoint}/openai/deployments/{deployment}/chat/completions?api-version=...
with the `api-key` header. Streamed chunks are turned into the provider-neutral events
the orchestrator uses (text deltas, then one TurnComplete with any tool calls).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from app.config import ConfigError, Settings
from app.llm.base import LlmError, LlmEvent, TextDelta, ToolCall, TurnComplete

log = logging.getLogger(__name__)


class AzureOpenAIClient:
    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self.settings = settings
        self.name = f"azure_openai:{settings.azure_openai_deployment}"
        if client is None:
            try:
                from openai import AsyncAzureOpenAI  # imported lazily: the mock model doesn't need the SDK
            except ModuleNotFoundError as exc:
                raise ConfigError(
                    "AZURE_OPENAI_ENDPOINT is set but the 'openai' package isn't installed. "
                    "Run: pip install -r requirements.txt (or make venv)"
                ) from exc

            client = AsyncAzureOpenAI(
                azure_endpoint=settings.azure_openai_endpoint,
                api_key=settings.azure_openai_api_key,
                api_version=settings.azure_openai_api_version,
                timeout=settings.llm_timeout_s,
                max_retries=2,  # the SDK retries 408/429/5xx and connection errors before any output
            )
        self._client = client

    def _request(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": self.settings.azure_openai_deployment,  # Azure: the deployment name
            "messages": messages,
            "stream": True,
            self.settings.llm_max_tokens_param: self.settings.llm_max_tokens,
        }
        if self.settings.llm_temperature is not None:  # reasoning models reject anything but the default
            request["temperature"] = self.settings.llm_temperature
        if self.settings.llm_reasoning_effort:
            request["reasoning_effort"] = self.settings.llm_reasoning_effort
        if tools:
            request["tools"] = tools
            request["tool_choice"] = "auto"
        return request

    async def stream(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> AsyncIterator[LlmEvent]:
        acc = _Accumulator()
        try:
            response = await self._client.chat.completions.create(**self._request(messages, tools))
            async for chunk in response:
                for event in acc.add(_as_dict(chunk)):
                    yield event
        except LlmError:
            raise
        except Exception as exc:  # noqa: BLE001 - SDK errors are mapped to safe messages below
            raise _to_llm_error(exc) from exc
        yield acc.finish()


def _as_dict(chunk: Any) -> dict[str, Any]:
    if isinstance(chunk, dict):
        return chunk
    return chunk.model_dump(exclude_none=True)


class _Accumulator:
    """Collects streamed deltas: text is passed through, tool-call fragments are joined."""

    def __init__(self) -> None:
        self.calls: dict[int, dict[str, str]] = {}
        self.text_seen = False
        self.finish_reason: str | None = None
        self.usage: dict[str, Any] | None = None

    def add(self, chunk: dict[str, Any]) -> list[LlmEvent]:
        events: list[LlmEvent] = []
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        for choice in chunk.get("choices") or []:  # Azure sends filter-only chunks with no choices
            delta = choice.get("delta") or {}
            if delta.get("content"):
                self.text_seen = True
                events.append(TextDelta(delta["content"]))
            for tc in delta.get("tool_calls") or []:
                slot = self.calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "arguments": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]
        return events

    def finish(self) -> TurnComplete:
        if self.finish_reason == "content_filter":
            raise LlmError("Response blocked by the provider content filter", status=400)
        tool_calls = [
            ToolCall(id=slot["id"] or f"call_{i}", name=slot["name"], arguments=slot["arguments"] or "{}")
            for i, slot in sorted(self.calls.items())
            if slot["name"]
        ]
        if self.finish_reason == "length" and not self.text_seen and not tool_calls:
            # Reasoning models spend hidden tokens first; a low limit can leave nothing for the answer.
            raise LlmError("Model hit LLM_MAX_TOKENS before answering; raise LLM_MAX_TOKENS", status=400)
        return TurnComplete(tool_calls=tool_calls, finish_reason=self.finish_reason, usage=self.usage)


def _to_llm_error(exc: Exception) -> LlmError:
    """Map SDK exceptions to messages that are safe to log (never the key)."""
    name = type(exc).__name__
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        detail = _safe_detail(getattr(exc, "body", None)) or name
        return LlmError(f"LLM HTTP {status}: {detail}", status=status, retryable=status in (408, 429, 500, 502, 503))
    if "Timeout" in name:
        return LlmError("LLM request timed out", retryable=True)
    if "Connection" in name:
        cause = exc.__cause__ or exc.__context__
        text = str(cause or exc)
        if "CERTIFICATE_VERIFY_FAILED" in text:
            return LlmError("LLM TLS certificate not trusted (set SSL_CERT_FILE to your CA bundle)")
        return LlmError(f"LLM connection error: {type(cause).__name__ if cause else name}", retryable=True)
    return LlmError(f"LLM error: {name}")


def _safe_detail(body: Any) -> str:
    err = body.get("error", body) if isinstance(body, dict) else None
    if isinstance(err, dict):
        return f"{err.get('code', '')} {err.get('message', '')}".strip()[:300]
    return ""
