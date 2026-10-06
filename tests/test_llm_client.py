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


# ------------------------------------------------------------ forward proxy
class _RecordingProxy:
    """Minimal forward proxy on localhost: records request lines; tunnels nothing.

    HTTPS targets arrive as CONNECT (answered 403 so no real network is used).
    Plain-HTTP targets arrive in absolute form and get a canned SSE stream back.
    """

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.server = None

    async def __aenter__(self) -> str:
        import asyncio

        async def handle(reader, writer) -> None:  # noqa: ANN001
            head = await reader.readuntil(b"\r\n\r\n")
            first = head.split(b"\r\n", 1)[0].decode()
            self.lines.append(first)
            if first.startswith("CONNECT "):
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            else:
                length = int(
                    next(
                        (ln.split(b":")[1] for ln in head.split(b"\r\n") if ln.lower().startswith(b"content-length")),
                        b"0",
                    )
                )
                await reader.readexactly(length)
                body = sse({"choices": [{"index": 0, "delta": {"content": "OK"}}]})
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\nConnection: close\r\n\r\n"
                    + body
                )
            await writer.drain()
            writer.close()

        self.server = await asyncio.start_server(handle, "127.0.0.1", 0)
        return f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"

    async def __aexit__(self, *exc: object) -> None:
        self.server.close()
        await self.server.wait_closed()


@pytest.mark.anyio
async def test_azure_calls_are_tunnelled_through_llm_proxy() -> None:
    async with _RecordingProxy() as proxy_url:
        client = OpenAIChatClient(make_settings(**AZURE, LLM_PROXY_URL=proxy_url))
        assert client.route.startswith("via proxy 127.0.0.1:")
        with pytest.raises(LlmError) as info:
            async for _ in client.stream([{"role": "user", "content": "hi"}], []):
                pass
    assert "proxy" in str(info.value) and "secret-key" not in str(info.value)


@pytest.mark.anyio
async def test_proxy_sees_connect_to_azure_host_only() -> None:
    recorder = _RecordingProxy()
    async with recorder as proxy_url:
        client = OpenAIChatClient(make_settings(**AZURE, LLM_PROXY_URL=proxy_url))
        with pytest.raises(LlmError):
            async for _ in client.stream([{"role": "user", "content": "hi"}], []):
                pass
    # TLS is tunnelled: the proxy only learns the host, never the path, key or prompt.
    assert recorder.lines and all(line == "CONNECT demo.openai.azure.com:443 HTTP/1.1" for line in recorder.lines)


@pytest.mark.anyio
async def test_stream_works_end_to_end_through_proxy() -> None:
    recorder = _RecordingProxy()
    async with recorder as proxy_url:
        settings = make_settings(
            LLM_PROVIDER="openai_compatible",
            OPENAI_COMPAT_BASE_URL="http://models.internal.example/v1",
            OPENAI_COMPAT_API_KEY="k" * 20,
            OPENAI_COMPAT_MODEL="gpt-4.1",
            LLM_PROXY_URL=proxy_url,
        )
        text = ""
        async for event in OpenAIChatClient(settings).stream([{"role": "user", "content": "hi"}], []):
            if isinstance(event, TextDelta):
                text += event.text
    assert text == "OK"
    assert recorder.lines[0].startswith("POST http://models.internal.example/v1/chat/completions")


def test_direct_route_without_proxy() -> None:
    assert OpenAIChatClient(make_settings(**AZURE)).route == "direct"


# ------------------------------------------------------------ gateway mode
GATEWAY = dict(LLM_PROVIDER="gateway", LLM_GATEWAY_URL="https://gw.example.com/completions/api?tenant=demo")

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
async def test_gateway_posts_to_url_as_given_without_key_or_model() -> None:
    seen: dict = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["url"] = str(request.url)
        seen["headers"] = {k.lower() for k in request.headers}
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, content=STREAM, headers={"content-type": "text/event-stream"})

    client = OpenAIChatClient(make_settings(**GATEWAY), transport=httpx2.MockTransport(handler))
    events = await collect(client)
    assert seen["url"] == "https://gw.example.com/completions/api?tenant=demo"
    assert "api-key" not in seen["headers"] and "authorization" not in seen["headers"]
    assert "model" not in seen["body"] and seen["body"]["stream"] is True
    assert isinstance(events[-1], TurnComplete) and len(events[-1].tool_calls) == 2
    assert client.last_mode == "stream"


@pytest.mark.anyio
async def test_gateway_optional_key_header_and_model() -> None:
    seen: dict = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, content=STREAM, headers={"content-type": "text/event-stream"})

    settings = make_settings(
        **GATEWAY,
        LLM_GATEWAY_KEY_HEADER="Ocp-Apim-Subscription-Key",
        LLM_GATEWAY_KEY="gk-1",
        LLM_GATEWAY_MODEL="gpt-4.1",
    )
    await collect(OpenAIChatClient(settings, transport=httpx2.MockTransport(handler)))
    assert seen["headers"]["ocp-apim-subscription-key"] == "gk-1"
    assert seen["body"]["model"] == "gpt-4.1"


@pytest.mark.anyio
@pytest.mark.parametrize("content_type", ["application/json", "text/plain"])
async def test_non_streamed_json_reply_is_accepted(content_type: str) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=json.dumps(COMPLETION).encode(), headers={"content-type": content_type})

    client = OpenAIChatClient(make_settings(**GATEWAY), transport=httpx2.MockTransport(handler))
    events = await collect(client)
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["Let me check."]
    done = events[-1]
    assert isinstance(done, TurnComplete)
    assert [(c.id, c.name) for c in done.tool_calls] == [("c1", "check_area_outage")]
    assert done.usage == {"total_tokens": 42}


def test_gateway_config_rules() -> None:
    from app.config import ConfigError

    assert make_settings(
        LLM_PROVIDER="gateway", LLM_GATEWAY_URL="host.example.com/completions/api"
    ).llm_gateway_url == ("https://host.example.com/completions/api")
    with pytest.raises(ConfigError, match="LLM_GATEWAY_URL"):
        make_settings(LLM_PROVIDER="gateway")
    with pytest.raises(ConfigError, match="both"):
        make_settings(**GATEWAY, LLM_GATEWAY_KEY_HEADER="x-key")
    # The usual mix-up: the gateway URL put into the forward-proxy setting.
    with pytest.raises(ConfigError, match="LLM_PROVIDER=gateway"):
        make_settings(LLM_PROXY_URL="https://host.example.com/completions/api")
    # Gateway mode doesn't need any AZURE_OPENAI_* values.
    assert make_settings(**GATEWAY).azure_openai_endpoint == ""
