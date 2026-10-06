"""The Azure/OpenAI-compatible streaming client, tested against a fake HTTP transport."""

from __future__ import annotations

import json

import httpx2
import pytest

from app.llm.base import LlmError, TextDelta, TurnComplete
from app.llm.openai_chat import OpenAIChatClient
from tests.conftest import make_settings

AZURE = dict(
    LLM_PROVIDER="azure_openai",
    AZURE_OPENAI_ENDPOINT="https://demo.openai.azure.com",
    AZURE_OPENAI_API_KEY="secret-key-123456",
    AZURE_OPENAI_DEPLOYMENT="gpt-41",
)


def sse(*chunks: dict) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return body.encode()


STREAM = sse(
    {"choices": [], "prompt_filter_results": []},  # Azure filter-only chunk
    {"choices": [{"index": 0, "delta": {"content": "Checking "}}]},
    {"choices": [{"index": 0, "delta": {"content": "now."}}]},
    {
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {"index": 0, "id": "call_a", "function": {"name": "run_line_diagnostics", "arguments": ""}}
                    ]
                },
            }
        ]
    },
    {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{}"}}]}}]},
    {
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": 1,
                            "id": "call_b",
                            "function": {"name": "get_eligible_offers", "arguments": '{"need":'},
                        }
                    ]
                },
            }
        ]
    },
    {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 1, "function": {"arguments": '"speed"}'}}]}}]},
    {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
)


async def collect(client: OpenAIChatClient):
    return [e async for e in client.stream([{"role": "user", "content": "hi"}], [])]


@pytest.mark.anyio
async def test_azure_request_shape_and_stream_parsing() -> None:
    seen: dict = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, content=STREAM, headers={"content-type": "text/event-stream"})

    client = OpenAIChatClient(make_settings(**AZURE), transport=httpx2.MockTransport(handler))
    events = await collect(client)

    assert seen["url"].startswith("https://demo.openai.azure.com/openai/deployments/gpt-41/chat/completions")
    assert "api-version=" in seen["url"]
    assert seen["headers"]["api-key"] == "secret-key-123456"
    assert seen["body"]["stream"] is True and "model" not in seen["body"]
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert text == "Checking now."
    done = events[-1]
    assert isinstance(done, TurnComplete)
    assert [(c.name, c.arguments) for c in done.tool_calls] == [
        ("run_line_diagnostics", "{}"),
        ("get_eligible_offers", '{"need":"speed"}'),
    ]


@pytest.mark.anyio
async def test_openai_compatible_uses_bearer_and_model() -> None:
    seen: dict = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        seen["url"] = str(request.url)
        return httpx2.Response(
            200, content=sse({"choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]})
        )

    settings = make_settings(
        LLM_PROVIDER="openai_compatible",
        OPENAI_COMPAT_BASE_URL="https://x.services.ai.azure.com/openai/v1",
        OPENAI_COMPAT_API_KEY="k-123",
        OPENAI_COMPAT_MODEL="gpt-4.1",
    )
    await collect(OpenAIChatClient(settings, transport=httpx2.MockTransport(handler)))
    assert seen["url"] == "https://x.services.ai.azure.com/openai/v1/chat/completions"
    assert seen["headers"]["authorization"] == "Bearer k-123"
    assert seen["body"]["model"] == "gpt-4.1"


@pytest.mark.anyio
async def test_retry_once_on_429_then_succeed() -> None:
    attempts = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx2.Response(429, json={"error": {"code": "429", "message": "Rate limit"}})
        return httpx2.Response(200, content=sse({"choices": [{"index": 0, "delta": {"content": "hi"}}]}))

    client = OpenAIChatClient(make_settings(**AZURE), transport=httpx2.MockTransport(handler))
    events = await collect(client)
    assert attempts["n"] == 2 and isinstance(events[-1], TurnComplete)


@pytest.mark.anyio
async def test_errors_do_not_leak_key() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401, json={"error": {"code": "401", "message": "Access denied due to invalid key"}})

    client = OpenAIChatClient(make_settings(**AZURE), transport=httpx2.MockTransport(handler))
    with pytest.raises(LlmError) as info:
        await collect(client)
    assert info.value.status == 401 and "secret-key" not in str(info.value)


@pytest.mark.anyio
async def test_content_filter_is_reported() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, content=sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "content_filter"}]})
        )

    client = OpenAIChatClient(make_settings(**AZURE), transport=httpx2.MockTransport(handler))
    with pytest.raises(LlmError):
        await collect(client)
