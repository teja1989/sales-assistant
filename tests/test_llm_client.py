"""The model-proxy streaming client (LLM_PROXY_URL), tested against a fake HTTP transport."""

from __future__ import annotations

import json

import httpx2
import pytest

from app.llm.base import LlmError, TextDelta, TurnComplete
from app.llm.openai_chat import OpenAIChatClient
from tests.conftest import make_settings

PROXY = dict(LLM_PROXY_URL="https://proxy.example.com/completions/api?tenant=demo")
KEYED = dict(PROXY, LLM_PROXY_KEY="secret-key-123456")


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
async def test_posts_to_proxy_url_as_given_and_parses_stream() -> None:
    seen: dict = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["url"] = str(request.url)
        seen["headers"] = {k.lower() for k in request.headers}
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, content=STREAM, headers={"content-type": "text/event-stream"})

    client = OpenAIChatClient(make_settings(**PROXY), transport=httpx2.MockTransport(handler))
    events = await collect(client)

    assert seen["url"] == "https://proxy.example.com/completions/api?tenant=demo"
    assert "api-key" not in seen["headers"] and "authorization" not in seen["headers"]
    assert seen["body"]["stream"] is True and "model" not in seen["body"]
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert text == "Checking now."
    done = events[-1]
    assert isinstance(done, TurnComplete)
    assert [(c.name, c.arguments) for c in done.tool_calls] == [
        ("run_line_diagnostics", "{}"),
        ("get_eligible_offers", '{"need":"speed"}'),
    ]
    assert client.last_mode == "stream"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("key", "header", "value"),
    [("secret-key-123456", "api-key", "secret-key-123456"), ("Bearer tok-1", "authorization", "Bearer tok-1")],
)
async def test_proxy_key_header_convention(key: str, header: str, value: str) -> None:
    seen: dict = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["headers"] = dict(request.headers)
        return httpx2.Response(200, content=STREAM, headers={"content-type": "text/event-stream"})

    await collect(OpenAIChatClient(make_settings(**PROXY, LLM_PROXY_KEY=key), transport=httpx2.MockTransport(handler)))
    assert seen["headers"][header] == value


@pytest.mark.anyio
async def test_retry_once_on_429_then_succeed() -> None:
    attempts = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx2.Response(429, json={"error": {"code": "429", "message": "Rate limit"}})
        return httpx2.Response(200, content=sse({"choices": [{"index": 0, "delta": {"content": "hi"}}]}))

    client = OpenAIChatClient(make_settings(**PROXY), transport=httpx2.MockTransport(handler))
    events = await collect(client)
    assert attempts["n"] == 2 and isinstance(events[-1], TurnComplete)


@pytest.mark.anyio
async def test_errors_do_not_leak_key() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401, json={"error": {"code": "401", "message": "Access denied due to invalid key"}})

    client = OpenAIChatClient(make_settings(**KEYED), transport=httpx2.MockTransport(handler))
    with pytest.raises(LlmError) as info:
        await collect(client)
    assert info.value.status == 401 and "secret-key" not in str(info.value)


@pytest.mark.anyio
async def test_content_filter_is_reported() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, content=sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "content_filter"}]})
        )

    client = OpenAIChatClient(make_settings(**PROXY), transport=httpx2.MockTransport(handler))
    with pytest.raises(LlmError):
        await collect(client)


COMPLETION = {
    "choices": [
        {
            "index": 0,
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": "Let me check.",
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "check_area_outage", "arguments": "{}"}}
                ],
            },
        }
    ],
    "usage": {"total_tokens": 42},
}


@pytest.mark.anyio
@pytest.mark.parametrize("content_type", ["application/json", "text/plain"])
async def test_non_streamed_json_reply_is_accepted(content_type: str) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=json.dumps(COMPLETION).encode(), headers={"content-type": content_type})

    client = OpenAIChatClient(make_settings(**PROXY), transport=httpx2.MockTransport(handler))
    events = await collect(client)
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["Let me check."]
    done = events[-1]
    assert isinstance(done, TurnComplete)
    assert [(c.id, c.name) for c in done.tool_calls] == [("c1", "check_area_outage")]
    assert done.usage == {"total_tokens": 42}


def test_config_rules() -> None:
    from app.config import ConfigError

    assert make_settings(LLM_PROXY_URL="host.example.com/completions/api").llm_proxy_url == (
        "https://host.example.com/completions/api"
    )
    assert make_settings(LLM_PROXY_URL="").llm_provider == "mock"
    assert make_settings(**PROXY).llm_provider == "proxy"
    with pytest.raises(ConfigError, match="LLM_PROXY_URL"):
        make_settings(LLM_PROXY_URL="https:///no-host")
