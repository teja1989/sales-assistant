from __future__ import annotations

import base64
import time

import jwt
import pytest
from starlette.testclient import TestClient

from app.config import ConfigError
from app.handoff import AUDIENCE, ISSUER, HandoffError, HandoffSigner
from app.logging_setup import mask_pii
from app.main import create_app
from tests.conftest import TEST_ENV, make_settings, sign_in, start_session

MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}


# ---------------------------------------------------------------- /mcp auth
def test_mcp_requires_bearer_token(client) -> None:
    assert client.post("/mcp", json=INIT, headers=MCP_HEADERS).status_code == 401
    bad = client.post("/mcp", json=INIT, headers={**MCP_HEADERS, "Authorization": "Bearer nope"})
    assert bad.status_code == 401


def test_mcp_accepts_valid_token(client) -> None:
    res = client.post(
        "/mcp", json=INIT, headers={**MCP_HEADERS, "Authorization": f"Bearer {TEST_ENV['MCP_SERVER_TOKEN']}"}
    )
    assert res.status_code != 401


# ---------------------------------------------------------- web hardening
def test_security_headers(client) -> None:
    res = client.get("/healthz")
    assert res.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in res.headers["content-security-policy"]
    assert res.headers["x-frame-options"] == "DENY"


def test_unknown_session_and_bad_input(client) -> None:
    assert client.post("/api/sessions/nope/turn", json={"message": "hi"}).status_code == 404
    sid = start_session(client, "speed-upgrade")
    assert client.post(f"/api/sessions/{sid}/turn", json={}).status_code == 422
    too_long = client.post(f"/api/sessions/{sid}/turn", json={"message": "x" * 2001})
    assert too_long.status_code == 422


def test_body_size_limit(client) -> None:
    res = client.post("/api/handoff", content=b"{" + b" " * 40_000 + b"}", headers={"Content-Type": "application/json"})
    assert res.status_code == 413


def test_rate_limit() -> None:
    with TestClient(create_app(make_settings(RATE_LIMIT_PER_MINUTE="3"))) as c:
        codes = [c.post("/api/handoff", json={"scenario_id": "speed-upgrade"}).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and 429 in codes[3:]


def test_basic_auth_gate_when_configured() -> None:
    settings = make_settings(DEMO_BASIC_AUTH_USER="demo", DEMO_BASIC_AUTH_PASSWORD="s3cret-pass")
    with TestClient(create_app(settings)) as c:
        assert c.get("/api/config").status_code == 401
        assert c.get("/healthz").status_code == 200  # platform health checks stay open
        token = base64.b64encode(b"demo:s3cret-pass").decode()
        assert c.get("/api/config", headers={"Authorization": f"Basic {token}"}).status_code == 200


def test_config_response_has_no_secrets(client) -> None:
    body = client.get("/api/config").text
    ctx = client.app.state.ctx
    for secret in (TEST_ENV["MCP_SERVER_TOKEN"], ctx.settings.handoff_secret, ctx.settings.oauth_signing_secret):
        assert secret not in body


# --------------------------------------------------------------- handoff
def test_handoff_token_single_use_and_tamper_proof() -> None:
    signer = HandoffSigner("x" * 32, ttl_s=60)
    token = signer.issue("speed-upgrade", "faster internet")
    assert signer.redeem(token).scenario_id == "speed-upgrade"
    with pytest.raises(HandoffError):
        signer.redeem(token)  # replay
    forged = jwt.encode(
        {"scn": "x", "aud": AUDIENCE, "iss": ISSUER, "jti": "1", "iat": 0, "exp": int(time.time()) + 60},
        "wrong-secret-wrong-secret-wrong-secret!",
        algorithm="HS256",
    )
    with pytest.raises(HandoffError):
        signer.redeem(forged)


def test_handoff_rejects_alg_none_and_expired() -> None:
    signer = HandoffSigner("y" * 32, ttl_s=60)
    claims = {"scn": "x", "aud": AUDIENCE, "iss": ISSUER, "jti": "2", "iat": 0, "exp": int(time.time()) + 60}
    unsigned = jwt.encode(claims, key=None, algorithm="none")
    with pytest.raises(HandoffError):
        signer.redeem(unsigned)
    expired = jwt.encode({**claims, "exp": int(time.time()) - 10}, "y" * 32, algorithm="HS256")
    with pytest.raises(HandoffError, match="expired"):
        signer.redeem(expired)


def test_session_endpoint_rejects_reused_handoff(client) -> None:
    token = client.post("/api/handoff", json={"scenario_id": "speed-upgrade"}).json()["token"]
    auth = {"Authorization": f"Bearer {sign_in(client, 'speed-upgrade')}"}
    assert client.post("/api/sessions", json={"handoff_token": token}, headers=auth).status_code == 200
    assert client.post("/api/sessions", json={"handoff_token": token}, headers=auth).status_code == 401


# ---------------------------------------------------------------- config
def test_signing_secrets_are_generated_per_process() -> None:
    a, b = make_settings(HANDOFF_SECRET="ignored"), make_settings()
    assert len(a.handoff_secret) >= 32 and len(a.oauth_signing_secret) >= 32
    assert a.handoff_secret != "ignored" and a.handoff_secret != b.handoff_secret


def test_vcap_user_provided_service(monkeypatch) -> None:
    monkeypatch.setenv(
        "VCAP_SERVICES",
        (
            '{"user-provided":[{"name":"sales-assistant-secrets","credentials":'
            '{"azure_openai_api_key":"from-vcap","live_mcp_url":"https://live.example.com/mcp","live_mcp_token":"lt"}}]}'
        ),
    )
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LIVE_MCP_TOKEN", raising=False)
    monkeypatch.delenv("LIVE_MCP_URL", raising=False)
    overrides = {
        k: v for k, v in TEST_ENV.items() if k not in ("AZURE_OPENAI_API_KEY", "LIVE_MCP_URL", "LIVE_MCP_TOKEN")
    }
    from app.config import load_settings

    s = load_settings(overrides)
    assert s.azure_openai_api_key == "from-vcap"
    assert s.live_servers[0].token == "lt" and s.live_servers[0].name == "live-example-com"


def test_pii_masking() -> None:
    text = mask_pii("mail jane@example.com call 415-555-0134 card 4111 1111 1111 1111 Bearer abcdefghijklmnop")
    assert "jane@" not in text and "555-0134" not in text and "4111" not in text and "abcdefghijklmnop" not in text


# ------------------------------------------------- /mcp auth switch, config
def test_mcp_open_when_auth_disabled() -> None:
    settings = make_settings(MCP_AUTH_REQUIRED="false")
    assert settings.mcp_server_token == ""
    with TestClient(create_app(settings)) as c:
        res = c.post("/mcp", json=INIT, headers=MCP_HEADERS)
        assert res.status_code != 401
        assert c.get("/api/config").json()["mcp_auth_required"] is False


def test_open_mcp_is_rate_limited() -> None:
    settings = make_settings(MCP_AUTH_REQUIRED="false", RATE_LIMIT_PER_MINUTE="3")
    with TestClient(create_app(settings)) as c:
        codes = [c.post("/mcp", json=INIT, headers=MCP_HEADERS).status_code for _ in range(5)]
        assert 429 in codes


def test_mcp_auth_defaults_off_and_token_rules() -> None:
    from app.config import load_settings

    defaults = load_settings({k: v for k, v in TEST_ENV.items() if k not in ("MCP_AUTH_REQUIRED", "MCP_SERVER_TOKEN")})
    assert defaults.mcp_auth_required is False and defaults.mcp_server_token == ""
    assert make_settings(APP_ENV="prod", MCP_AUTH_REQUIRED="false").mcp_auth_required is False
    with pytest.raises(ConfigError, match="MCP_SERVER_TOKEN"):
        make_settings(APP_ENV="prod", MCP_AUTH_REQUIRED="true", MCP_SERVER_TOKEN="short")


def test_url_settings_validated(tmp_path) -> None:
    with pytest.raises(ConfigError, match="live MCP server"):
        make_settings(LIVE_MCP_URL="https:///mcp")
    assert make_settings(LIVE_MCP_URL="api.example.com/mcp").live_mcp_url == "https://api.example.com/mcp"
