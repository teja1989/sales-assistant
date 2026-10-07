from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from starlette.testclient import TestClient

from app.config import load_settings
from app.llm.base import LlmEvent, TextDelta, ToolCall, TurnComplete
from app.main import create_app

TEST_ENV = {
    "APP_ENV": "test",
    "AZURE_OPENAI_ENDPOINT": "",  # offline mock model, whatever a developer's .env says
    "AZURE_OPENAI_API_KEY": "",
    "AZURE_OPENAI_DEPLOYMENT": "",
    "MOCK_STREAM_DELAY_MS": "0",
    "MCP_AUTH_REQUIRED": "true",  # most security tests exercise /mcp with auth on
    "MCP_SERVER_TOKEN": "test-mcp-token-0123456789abcdef",
    "RATE_LIMIT_PER_MINUTE": "1000",
    "DATA_MODE": "sim",
    "LIVE_MCP_URL": "",
    "LIVE_MCP_TOKEN": "",
    "DEMO_BASIC_AUTH_USER": "",
    "DEMO_BASIC_AUTH_PASSWORD": "",
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def make_settings(**overrides: str):
    return load_settings({**TEST_ENV, **overrides})


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app(make_settings())) as c:
        yield c


def parse_sse(text: str) -> list[dict[str, Any]]:
    events = []
    for block in text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


FULL_SCOPE = "account:read account:manage"


def sign_in(c: TestClient, scenario_id: str, scope: str = FULL_SCOPE) -> str:
    """Issue an access token for the scenario's customer (the full OAuth flow is tested in test_oauth.py)."""
    ctx = c.app.state.ctx
    customer = ctx.scenarios[scenario_id].customer
    token, _ = ctx.idp.issue_token(str(customer["id"]), str(customer["first_name"]), scope)
    return token


def start_session(c: TestClient, scenario_id: str, scope: str = FULL_SCOPE) -> str:
    handoff = c.post("/api/handoff", json={"scenario_id": scenario_id})
    assert handoff.status_code == 200, handoff.text
    created = c.post(
        "/api/sessions",
        json={"handoff_token": handoff.json()["token"]},
        headers={"Authorization": f"Bearer {sign_in(c, scenario_id, scope)}"},
    )
    assert created.status_code == 200, created.text
    return created.json()["session_id"]


def turn(c: TestClient, session_id: str, body: dict[str, Any]) -> list[dict[str, Any]]:
    res = c.post(f"/api/sessions/{session_id}/turn", json=body)
    assert res.status_code == 200, res.text
    return parse_sse(res.text)


class ScriptedLlm:
    """A fake model that returns pre-scripted turns. Used to simulate a manipulated model."""

    name = "scripted"

    def __init__(self, script: list[tuple[str, list[tuple[str, dict[str, Any] | str]]]]) -> None:
        self.script = list(script)
        self.calls: list[list[dict[str, Any]]] = []

    async def stream(self, messages, tools):  # noqa: ANN001
        self.calls.append([json.loads(json.dumps(m)) for m in messages])
        text, calls = self.script.pop(0) if self.script else ("Done.", [])
        events: list[LlmEvent] = []
        if text:
            events.append(TextDelta(text))
        events.append(
            TurnComplete(
                tool_calls=[
                    ToolCall(
                        id=f"call_{i}_{len(self.calls)}",
                        name=name,
                        arguments=args if isinstance(args, str) else json.dumps(args),
                    )
                    for i, (name, args) in enumerate(calls)
                ]
            )
        )
        for event in events:
            yield event


def scripted_client(script, **overrides: str) -> tuple[TestClient, ScriptedLlm]:
    llm = ScriptedLlm(script)
    return TestClient(create_app(make_settings(**overrides), llm=llm)), llm
