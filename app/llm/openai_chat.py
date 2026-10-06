"""Streaming client for Azure OpenAI and any OpenAI-compatible chat-completions endpoint.

Implemented directly over HTTP (httpx2, already a dependency of the MCP SDK)
to keep the dependency surface small and the wire behaviour explicit:

* azure_openai:      POST {endpoint}/openai/deployments/{deployment}/chat/completions?api-version=...
                     header  api-key: <key>
* openai_compatible: POST {base_url}/chat/completions   (e.g. Azure AI Foundry /openai/v1)
                     header  Authorization: Bearer <key>   or   api-key: <key>
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from collections.abc import AsyncIterator
from typing import Any

import httpx2

from app.config import Settings, describe_proxy
from app.llm.base import LlmError, LlmEvent, TextDelta, ToolCall, TurnComplete

log = logging.getLogger(__name__)

RETRYABLE = {408, 409, 429, 500, 502, 503, 504}


class OpenAIChatClient:
    def __init__(self, settings: Settings, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self._transport = transport
        # Only LLM traffic goes through LLM_PROXY_URL, so live MCP and internal calls are unaffected.
        # HTTPS through a forward proxy is tunnelled (CONNECT): TLS still ends at the Azure host.
        self._proxy = settings.llm_proxy_url or None
        self._verify: ssl.SSLContext | bool = (
            ssl.create_default_context(cafile=settings.llm_ca_bundle) if settings.llm_ca_bundle else True
        )
        self.route = f"via proxy {describe_proxy(settings.llm_proxy_url)}" if settings.llm_proxy_url else "direct"
        if settings.llm_provider == "azure_openai":
            self.name = f"azure_openai:{settings.azure_openai_deployment}"
            self.url = (
                f"{settings.azure_openai_endpoint}/openai/deployments/"
                f"{settings.azure_openai_deployment}/chat/completions"
            )
            self.params = {"api-version": settings.azure_openai_api_version}
            self.headers = {"api-key": settings.azure_openai_api_key}
            self.model: str | None = None
        else:
            self.name = f"openai_compatible:{settings.openai_compat_model}"
            self.url = f"{settings.openai_compat_base_url}/chat/completions"
            self.params = {}
            if settings.openai_compat_key_header == "api-key":
                self.headers = {"api-key": settings.openai_compat_api_key}
            else:
                self.headers = {"Authorization": f"Bearer {settings.openai_compat_api_key}"}
            self.model = settings.openai_compat_model

    def _body(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        body: dict[str, Any] = {
            "messages": messages,
            "stream": True,
            "temperature": self.settings.llm_temperature,
            self.settings.llm_max_tokens_param: self.settings.llm_max_tokens,
        }
        if self.model:
            body["model"] = self.model
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
        async with httpx2.AsyncClient(
            timeout=timeout, transport=self._transport, proxy=self._proxy, verify=self._verify
        ) as client:
            try:
                async with client.stream(
                    "POST",
                    self.url,
                    params=self.params,
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
                    async for event in _parse_sse(response.aiter_lines()):
                        yield event
            except httpx2.TimeoutException as exc:
                raise LlmError("LLM request timed out", retryable=True) from exc
            except httpx2.ProxyError as exc:
                raise LlmError(f"LLM proxy error ({self.route}): {type(exc).__name__}", retryable=True) from exc
            except httpx2.TransportError as exc:
                if "CERTIFICATE_VERIFY_FAILED" in str(exc):
                    raise LlmError(
                        f"LLM TLS certificate not trusted ({self.route}); set LLM_CA_BUNDLE to your CA file",
                        retryable=False,
                    ) from exc
                raise LlmError(f"LLM transport error ({self.route}): {type(exc).__name__}", retryable=True) from exc


def _safe_error(detail: str) -> str:
    try:
        payload = json.loads(detail)
        err = payload.get("error", payload)
        if isinstance(err, dict):
            return f"{err.get('code', '')} {err.get('message', '')}".strip()[:300]
    except (json.JSONDecodeError, AttributeError):
        pass
    return detail[:200]


async def _parse_sse(lines: AsyncIterator[str]) -> AsyncIterator[LlmEvent]:
    calls: dict[int, dict[str, str]] = {}
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    async for line in lines:
        line = line.strip()
        if not line.startswith("data:"):
            continue
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
    tool_calls = [
        ToolCall(id=slot["id"] or f"call_{i}", name=slot["name"], arguments=slot["arguments"] or "{}")
        for i, slot in sorted(calls.items())
        if slot["name"]
    ]
    if finish_reason == "content_filter":
        raise LlmError("Response blocked by the provider content filter", status=400)
    yield TurnComplete(tool_calls=tool_calls, finish_reason=finish_reason, usage=usage)
