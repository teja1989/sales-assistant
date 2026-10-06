"""The Azure OpenAI client (official SDK), tested with a stand-in for the SDK client.

The real `openai` package isn't installable in the build sandbox, so these tests check our side
of the contract: the arguments we pass to `chat.completions.create`, how streamed chunks become
events, and how SDK errors are mapped. `make llm-check` exercises the real SDK end to end.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.config import ConfigError
from app.llm.azure_openai import AzureOpenAIClient
from app.llm.base import LlmError, TextDelta, TurnComplete
from tests.conftest import make_settings

AZURE = dict(
    AZURE_OPENAI_ENDPOINT="https://demo.openai.azure.com",
    AZURE_OPENAI_API_KEY="secret-key-123456",
    AZURE_OPENAI_DEPLOYMENT="gpt-41",
)


class _Chunk:
    """Mimics an SDK pydantic chunk (only model_dump is used)."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    def model_dump(self, exclude_none: bool = True) -> dict[str, Any]:
        return self.data


class _Stream:
    def __init__(self, chunks: list[dict[str, Any]]) -> None:
        self.chunks = chunks

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for c in self.chunks:
            yield _Chunk(c)


class FakeSdk:
    """Stands in for AsyncAzureOpenAI: records the create() call, returns a stream or raises."""

    def __init__(self, chunks: list[dict[str, Any]] | None = None, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._chunks = chunks or []
        self._error = error
        self.chat = self
        self.completions = self

    async def create(self, **kwargs: Any) -> _Stream:
        self.calls.append(kwargs)
        if self._error:
            raise self._error
        return _Stream(self._chunks)


CHUNKS = [
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
]


async def collect(client: AzureOpenAIClient, tools: list | None = None):
    return [e async for e in client.stream([{"role": "user", "content": "hi"}], tools or [])]


@pytest.mark.anyio
async def test_sdk_request_and_stream_parsing() -> None:
    sdk = FakeSdk(CHUNKS)
    client = AzureOpenAIClient(make_settings(**AZURE), client=sdk)
    tool = {"type": "function", "function": {"name": "run_line_diagnostics", "parameters": {"type": "object"}}}
    events = await collect(client, [tool])

    call = sdk.calls[0]
    assert call["model"] == "gpt-41"  # Azure: the deployment name
    assert call["stream"] is True and call["tools"] == [tool] and call["tool_choice"] == "auto"
    assert call["max_tokens"] == 700 and call["temperature"] == 0.2
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Checking now."
    done = events[-1]
    assert isinstance(done, TurnComplete) and done.finish_reason == "tool_calls"
    assert [(c.id, c.name, c.arguments) for c in done.tool_calls] == [
        ("call_a", "run_line_diagnostics", "{}"),
        ("call_b", "get_eligible_offers", '{"need":"speed"}'),
    ]


@pytest.mark.anyio
async def test_no_tools_means_no_tool_fields() -> None:
    sdk = FakeSdk([{"choices": [{"index": 0, "delta": {"content": "OK"}, "finish_reason": "stop"}]}])
    await collect(AzureOpenAIClient(make_settings(**AZURE), client=sdk))
    assert "tools" not in sdk.calls[0] and "tool_choice" not in sdk.calls[0]


@pytest.mark.anyio
async def test_content_filter_is_reported() -> None:
    sdk = FakeSdk([{"choices": [{"index": 0, "delta": {}, "finish_reason": "content_filter"}]}])
    with pytest.raises(LlmError):
        await collect(AzureOpenAIClient(make_settings(**AZURE), client=sdk))


class AuthenticationError(Exception):  # same shape as the SDK's APIStatusError subclasses
    def __init__(self) -> None:
        super().__init__("Error code: 401 secret-key-123456")
        self.status_code = 401
        self.body = {"error": {"code": "401", "message": "Access denied due to invalid subscription key"}}


class APITimeoutError(Exception):
    pass


class APIConnectionError(Exception):
    pass


@pytest.mark.anyio
async def test_status_errors_are_mapped_without_the_key() -> None:
    client = AzureOpenAIClient(make_settings(**AZURE), client=FakeSdk(error=AuthenticationError()))
    with pytest.raises(LlmError) as info:
        await collect(client)
    assert info.value.status == 401
    assert "invalid subscription key" in str(info.value) and "secret-key" not in str(info.value)


@pytest.mark.anyio
async def test_timeout_and_connection_errors_are_mapped() -> None:
    with pytest.raises(LlmError, match="timed out"):
        await collect(AzureOpenAIClient(make_settings(**AZURE), client=FakeSdk(error=APITimeoutError())))

    err = APIConnectionError("Connection error.")
    err.__cause__ = OSError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
    with pytest.raises(LlmError, match="certificate not trusted"):
        await collect(AzureOpenAIClient(make_settings(**AZURE), client=FakeSdk(error=err)))

    err = APIConnectionError("Connection error.")
    err.__cause__ = ConnectionRefusedError()
    with pytest.raises(LlmError, match="connection error: ConnectionRefusedError"):
        await collect(AzureOpenAIClient(make_settings(**AZURE), client=FakeSdk(error=err)))


def test_config_rules() -> None:
    assert make_settings().llm_provider == "mock"
    s = make_settings(**AZURE)
    assert s.llm_provider == "azure_openai" and s.azure_openai_api_version == "2024-10-21"
    assert make_settings(**{**AZURE, "AZURE_OPENAI_ENDPOINT": "demo.openai.azure.com/"}).azure_openai_endpoint == (
        "https://demo.openai.azure.com"
    )
    with pytest.raises(ConfigError, match="AZURE_OPENAI_DEPLOYMENT"):
        make_settings(**{**AZURE, "AZURE_OPENAI_DEPLOYMENT": ""})


def test_missing_sdk_gives_a_clear_error(monkeypatch) -> None:
    import builtins

    real_import = builtins.__import__

    def no_openai(name, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        if name == "openai":
            raise ModuleNotFoundError("No module named 'openai'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_openai)
    with pytest.raises(ConfigError, match="pip install"):
        AzureOpenAIClient(make_settings(**AZURE))


def test_proxy_detection(monkeypatch) -> None:
    from app.llm_check import _proxy_in_effect

    for var in ("HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)
    assert _proxy_in_effect("https://demo.openai.azure.com") == "none (direct)"
    monkeypatch.setenv("HTTPS_PROXY", "http://user:pw@proxy.corp:8080")
    assert _proxy_in_effect("https://demo.openai.azure.com") == "HTTPS_PROXY proxy.corp:8080"  # no credentials
    monkeypatch.setenv("NO_PROXY", "localhost,.azure.com")
    assert "NO_PROXY" in _proxy_in_effect("https://demo.openai.azure.com")
