"""DATA_MODE=live end to end through the app: account-number sign-in, live tool discovery,
account injection, read vs action (Confirm), and honest failures. The live MCP server is
replaced by a fake at the gateway boundary; a scripted model drives the tool calls."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from types import SimpleNamespace as NS
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import pytest

from app.config import ConfigError
from app.mcp_gateway import ToolOutcome
from tests.conftest import make_settings, scripted_client, turn

LIVE = dict(
    DATA_MODE="live",
    LIVE_MCP_URL="https://live.example.com/mcp",
    AZURE_OPENAI_ENDPOINT="https://demo.openai.azure.com",
    AZURE_OPENAI_API_KEY="k",
    AZURE_OPENAI_DEPLOYMENT="gpt",
)

LIVE_TOOLS = [
    NS(
        name="getAccountSummary",
        title=None,
        description="Account summary for the customer.",
        input_schema={"type": "object", "properties": {"accountId": {"type": "string"}}, "required": ["accountId"]},
        annotations=NS(title="Account summary", read_only_hint=True),
    ),
    NS(
        name="restartModem",
        title=None,
        description="Restart the customer's modem remotely. Takes about two minutes.",
        input_schema={"type": "object", "properties": {"accountId": {"type": "string"}, "reason": {"type": "string"}}},
        annotations=NS(title="Restart modem", read_only_hint=False),
    ),
]


class FakeLiveServer:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail

    def install(self, gateway) -> None:  # noqa: ANN001
        async def refresh(force: bool = False):
            gateway._live_specs = {t.name: gateway._live_spec(t) for t in LIVE_TOOLS}
            return gateway._live_specs

        async def call_live(name, arguments, started):  # noqa: ANN001
            self.calls.append((name, arguments))
            if self.fail:
                raise ConnectionError("refused")
            return ToolOutcome(name, "live", {"status": "ok", "plan": "Real Gig"}, False, 3)

        gateway.refresh_live_specs = refresh
        gateway._call_live = call_live


def _sign_in_with_account(c, account: str) -> str:  # noqa: ANN001
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = {
        "response_type": "code",
        "client_id": "tidelink-web",
        "redirect_uri": "http://testserver/chat/callback",
        "scope": "account:read account:manage",
        "state": "s1",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    page = c.get(f"/oauth/authorize?{urlencode(params)}")
    assert 'name="account" required' in page.text and "<select" not in page.text  # account-number field
    signed = re.search(r'name="request" value="([^"]+)"', page.text).group(1)
    res = c.post(
        "/oauth/authorize/consent",
        data={"request": signed, "account": account, "allow_manage": "on", "decision": "allow"},
        follow_redirects=False,
    )
    assert res.status_code == 303, res.text
    code = parse_qs(urlparse(res.headers["location"]).query)["code"][0]
    tok = c.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": params["redirect_uri"],
            "client_id": "tidelink-web",
            "code_verifier": verifier,
        },
    )
    return tok.json()["access_token"]


def _start(c, account: str = "ACC-777") -> str:  # noqa: ANN001
    handoff = c.post("/api/handoff", json={"scenario_id": "gateway-fault"}).json()["token"]
    token = _sign_in_with_account(c, account)
    res = c.post("/api/sessions", json={"handoff_token": handoff}, headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["customer"] == {"first_name": "", "plan": ""}  # no simulated persona in live mode
    return body["session_id"]


def test_live_mode_reads_use_signed_in_account_and_actions_need_confirm() -> None:
    script = [
        ("Let me look.", [("getAccountSummary", {"accountId": "SOMEONE-ELSE"})]),  # model tries another account
        ("Restarting needs your OK.", [("restartModem", {"reason": "drops", "accountId": "X"})]),
        ("Done.", []),
    ]
    client, llm = scripted_client(script, **LIVE)
    live = FakeLiveServer()
    with client as c:
        live.install(c.app.state.ctx.gateway)
        sid = _start(c)
        events = turn(c, sid, {"message": "my internet keeps dropping"})

    # Read ran live with the signed-in account, whatever the model said.
    assert live.calls[0] == ("getAccountSummary", {"accountId": "ACC-777"})
    results = [e for e in events if e["type"] == "tool_result"]
    assert results[0]["source"] == "live" and not results[0]["fallback"]
    # Action was not run; it waits for Confirm, with the server-filled account hidden from the card.
    confirm = next(e for e in events if e["type"] == "confirm_required")
    assert confirm["tool"] == "restartModem" and confirm["details"] == {"reason": "drops"}
    assert confirm["summary"] == "Restart the customer's modem remotely."
    assert len(live.calls) == 1
    # The model saw the live tools without the account input, and no simulator tools.
    tools_seen = {t["function"]["name"]: t for t in llm_tools(llm)}
    assert set(tools_seen) == {"getAccountSummary", "restartModem"}
    assert "accountId" not in tools_seen["getAccountSummary"]["function"]["parameters"]["properties"]
    system = llm.calls[0][0]["content"]
    assert "account is selected automatically" in system and "get_device_offer" not in system


def llm_tools(llm) -> list[dict[str, Any]]:  # noqa: ANN001
    return llm.tools_seen


@pytest.fixture(autouse=True)
def _record_tools(monkeypatch) -> None:
    from tests import conftest

    original = conftest.ScriptedLlm.stream

    async def stream(self, messages, tools):  # noqa: ANN001
        self.tools_seen = json.loads(json.dumps(tools))
        async for e in original(self, messages, tools):
            yield e

    monkeypatch.setattr(conftest.ScriptedLlm, "stream", stream)


def test_live_confirmed_action_runs_with_account() -> None:
    script = [("Restarting needs your OK.", [("restartModem", {"reason": "drops"})]), ("Restarted.", [])]
    client, _ = scripted_client(script, **LIVE)
    live = FakeLiveServer()
    with client as c:
        live.install(c.app.state.ctx.gateway)
        sid = _start(c, "ACC-1")
        events = turn(c, sid, {"message": "restart it"})
        action_id = next(e for e in events if e["type"] == "confirm_required")["action_id"]
        events = turn(c, sid, {"confirmation": {"action_id": action_id, "approved": True}})
    assert live.calls == [("restartModem", {"reason": "drops", "accountId": "ACC-1"})]
    assert any(e["type"] == "tool_result" and e["source"] == "live" for e in events)


def test_live_outage_is_an_honest_error_not_sim_data() -> None:
    client, _ = scripted_client([("Checking.", [("getAccountSummary", {})]), ("It's down.", [])], **LIVE)
    live = FakeLiveServer(fail=True)
    with client as c:
        live.install(c.app.state.ctx.gateway)
        sid = _start(c)
        events = turn(c, sid, {"message": "what plan am I on"})
    result = next(e for e in events if e["type"] == "tool_result")
    assert result["source"] == "live" and result["is_error"] and "plan" not in result["data"]


def test_live_sign_in_rejects_bad_account_numbers() -> None:
    client, _ = scripted_client([], **LIVE)
    with client as c:
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        params = {
            "response_type": "code",
            "client_id": "tidelink-web",
            "redirect_uri": "http://testserver/chat/callback",
            "scope": "account:read",
            "state": "s",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        page = c.get(f"/oauth/authorize?{urlencode(params)}")
        signed = re.search(r'name="request" value="([^"]+)"', page.text).group(1)
        for bad in ("", "a b", "<script>", "x" * 65):
            res = c.post("/oauth/authorize/consent", data={"request": signed, "account": bad, "decision": "allow"})
            assert res.status_code == 400


def test_live_mode_hides_personas_and_reports_data_mode() -> None:
    client, _ = scripted_client([], **LIVE)
    with client as c:
        items = c.get("/api/scenarios").json()["scenarios"]
        assert items and all("persona" not in s for s in items)
        assert c.get("/api/config").json()["data_source"] == "live"


def test_live_mode_config_requirements() -> None:
    with pytest.raises(ConfigError, match="LIVE_MCP_URL"):
        make_settings(**{**LIVE, "LIVE_MCP_URL": ""})
    with pytest.raises(ConfigError, match="AZURE_OPENAI"):
        make_settings(**{**LIVE, "AZURE_OPENAI_ENDPOINT": ""})
    with pytest.raises(ConfigError, match="OAUTH_REQUIRED"):
        make_settings(**LIVE, OAUTH_REQUIRED="false")
    with pytest.raises(ConfigError, match="DATA_MODE"):
        make_settings(DATA_MODE="hybrid")
    assert make_settings().data_mode == "sim"
