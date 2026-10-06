"""Streaming chat-completions client for the model proxy (LLM_PROXY_URL).

POST {LLM_PROXY_URL} exactly as given (its query string is kept) with the standard
OpenAI chat-completions body (messages, tools, stream). The proxy in front of Azure
OpenAI picks the deployment, so no model name is sent. Optional LLM_PROXY_KEY:
"Bearer <token>" goes in Authorization, anything else in the api-key header.
Streamed (SSE) and non-streamed JSON replies are both accepted.

Implemented directly over HTTP (httpx2, already a dependency of the MCP SDK)
to keep the dependency surface small and the wire behaviour explicit.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from collections.abc import AsyncIterator
from typing import Any

import httpx2

from app.config import Settings, proxy_auth_header
from app.llm.base import LlmError, LlmEvent, TextDelta, ToolCall, TurnComplete

log = logging.getLogger(__name__)

RETRYABLE = {408, 409, 429, 500, 502, 503, 504}


class OpenAIChatClient:
    def __init__(self, settings: Settings, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self._transport = transport
        self._verify: ssl.SSLContext | bool = (
            ssl.create_default_context(cafile=settings.llm_ca_bundle) if settings.llm_ca_bundle else True
        )
        self.name = "proxy"
        self.url = settings.llm_proxy_url
        self.headers = proxy_auth_header(settings.llm_proxy_key)
        self.last_mode = ""  # "stream" or "json" after a call (shown by make llm-check)

    def _body(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        body: dict[str, Any] = {
            "messages": messages,
            "stream": True,
            "temperature": self.settings.llm_temperature,
            self.settings.llm_max_tokens_param: self.settings.llm_max_tokens,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        return body

    async def stream(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> AsyncIterator[LlmEvent]:
        body = self._body(messages, tools)
        attempts = 2
        for attempt in range(1, attempts + 1):
            emitted = False
            try:
                async for event in self._stream_once(body):
                    emitted = True
                    yield event
                return
            except LlmError as exc:
                # Only retry when nothing has been streamed yet, so the user never sees duplicates.
                if exc.retryable and not emitted and attempt < attempts:
                    log.warning("LLM call failed (%s); retrying once", exc.status or exc)
                    await asyncio.sleep(1.5)
                    continue
                raise

    async def _stream_once(self, body: dict[str, Any]) -> AsyncIterator[LlmEvent]:
        timeout = httpx2.Timeout(self.settings.llm_timeout_s, connect=10.0)
        async with httpx2.AsyncClient(timeout=timeout, transport=self._transport, verify=self._verify) as client:
            try:
                async with client.stream(
                    "POST",
                    self.url,
                    headers={**self.headers, "Content-Type": "application/json"},
                    json=body,
                ) as response:
                    if response.status_code >= 400:
                        detail = (await response.aread())[:500].decode("utf-8", "replace")
                        raise LlmError(
                            f"LLM HTTP {response.status_code}: {_safe_error(detail)}",
                            status=response.status_code,
                            retryable=response.status_code in RETRYABLE,
                        )
                    content_type = response.headers.get("content-type", "")
                    if "json" in content_type and "event-stream" not in content_type:
                        # Some gateways ignore "stream": true and return one JSON completion.
                        self.last_mode = "json"
                        for event in _parse_completion(await response.aread()):
                            yield event
                        return
                    self.last_mode = "stream"
                    async for event in _parse_sse(response.aiter_lines()):
                        yield event
            except httpx2.TimeoutException as exc:
                raise LlmError("LLM request timed out", retryable=True) from exc
            except httpx2.TransportError as exc:
                if "CERTIFICATE_VERIFY_FAILED" in str(exc):
                    raise LlmError(
                        "LLM TLS certificate not trusted; set LLM_CA_BUNDLE to your CA file",
                        retryable=False,
                    ) from exc
                raise LlmError(f"LLM transport error: {type(exc).__name__}", retryable=True) from exc


def _safe_error(detail: str) -> str:
    try:
        payload = json.loads(detail)
        err = payload.get("error", payload)
        if isinstance(err, dict):
            return f"{err.get('code', '')} {err.get('message', '')}".strip()[:300]
    except (json.JSONDecodeError, AttributeError):
        pass
    return detail[:200]


def _parse_completion(raw: bytes) -> list[LlmEvent]:
    """Turn a non-streamed chat completion into the same events the stream produces."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LlmError("LLM returned JSON that could not be parsed", retryable=False) from exc
    choices = payload.get("choices") or []
    if not choices:
        raise LlmError("LLM response had no choices", retryable=False)
    choice = choices[0]
    message = choice.get("message") or {}
    events: list[LlmEvent] = []
    if message.get("content"):
        events.append(TextDelta(message["content"]))
    tool_calls = [
        ToolCall(
            id=tc.get("id") or f"call_{i}",
            name=(tc.get("function") or {}).get("name", ""),
            arguments=(tc.get("function") or {}).get("arguments") or "{}",
        )
        for i, tc in enumerate(message.get("tool_calls") or [])
        if (tc.get("function") or {}).get("name")
    ]
    finish_reason = choice.get("finish_reason")
    if finish_reason == "content_filter":
        raise LlmError("Response blocked by the provider content filter", status=400)
    events.append(TurnComplete(tool_calls=tool_calls, finish_reason=finish_reason, usage=payload.get("usage")))
    return events


async def _parse_sse(lines: AsyncIterator[str]) -> AsyncIterator[LlmEvent]:
    calls: dict[int, dict[str, str]] = {}
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    saw_data = False
    other: list[str] = []  # in case a gateway sends plain JSON with a non-JSON content type
    async for line in lines:
        line = line.strip()
        if not line.startswith("data:"):
            if not saw_data and len(other) < 2000:
                other.append(line)
            continue
        saw_data = True
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:  # Azure sends filter-only chunks with no choices
            delta = choice.get("delta") or {}
            content = delta.get("content")
            if content:
                yield TextDelta(content)
            for tc in delta.get("tool_calls") or []:
                slot = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "arguments": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
    if not saw_data and "".join(other).lstrip().startswith("{"):
        for event in _parse_completion("".join(other).encode()):
            yield event
        return
    tool_calls = [
        ToolCall(id=slot["id"] or f"call_{i}", name=slot["name"], arguments=slot["arguments"] or "{}")
        for i, slot in sorted(calls.items())
        if slot["name"]
    ]
    if finish_reason == "content_filter":
        raise LlmError("Response blocked by the provider content filter", status=400)
    yield TurnComplete(tool_calls=tool_calls, finish_reason=finish_reason, usage=usage)
