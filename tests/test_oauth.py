"""OAuth 2.0 authorization code + PKCE against the mock identity provider, and token-bound sessions."""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlparse

import jwt

from app.oauth import AUDIENCE, ISSUER
from tests.conftest import TEST_ENV, parse_sse, scripted_client, sign_in, start_session, turn

BASE = "http://testserver"
REDIRECT = f"{BASE}/chat/callback"


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_params(challenge: str, **overrides: str) -> dict[str, str]:
    params = {
        "response_type": "code",
        "client_id": "tidelink-web",
        "redirect_uri": REDIRECT,
        "scope": "account:read account:manage",
        "state": "st-123",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "login_hint": "scenario:account-checkup",
    }
    params.update(overrides)
    return params


def consent(client, challenge: str, allow_manage: bool = True, decision: str = "allow", **overrides: str):
    page = client.get(f"/oauth/authorize?{urlencode(authorize_params(challenge, **overrides))}")
    assert page.status_code == 200, page.text
    signed = re.search(r'name="request" value="([^"]+)"', page.text).group(1)
    form = {"request": signed, "account": "LUM-9100", "decision": decision}
    if allow_manage:
        form["allow_manage"] = "on"
    return page, client.post("/oauth/authorize/consent", data=form, follow_redirects=False)


def exchange(client, code: str, verifier: str, redirect_uri: str = REDIRECT):
    return client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": "tidelink-web",
            "code_verifier": verifier,
        },
    )


def code_from(response) -> str:
    assert response.status_code == 303, response.text
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["state"] == ["st-123"]
    return query["code"][0]


def open_session(client, access_token: str, scenario: str = "account-checkup"):
    handoff = client.post("/api/handoff", json={"scenario_id": scenario}).json()["token"]
    return client.post(
        "/api/sessions", json={"handoff_token": handoff}, headers={"Authorization": f"Bearer {access_token}"}
    )


# ------------------------------------------------------------- happy path
def test_full_authorization_code_pkce_flow(client) -> None:
    verifier, challenge = pkce()
    page, redirect = consent(client, challenge)
    assert "Morgan" in page.text and "never sees your password" in page.text
    token = exchange(client, code_from(redirect), verifier)
    assert token.status_code == 200 and token.headers["cache-control"] == "no-store"
    body = token.json()
    assert body["token_type"] == "Bearer" and body["scope"] == "account:read account:manage"
    session = open_session(client, body["access_token"])
    assert session.status_code == 200
    info = session.json()
    assert info["customer"]["first_name"] == "Morgan"
    assert info["access"] == {"connected": True, "can_make_changes": True}


def test_checkup_runs_first_after_sign_in(client) -> None:
    sid = start_session(client, "account-checkup")
    events = turn(client, sid, {"kickoff": True})
    first_tool = next(e for e in events if e["type"] == "tool_result")
    assert first_tool["tool"] == "get_account_checkup"
    assert first_tool["data"]["summary"]["attention_count"] >= 5
    assert first_tool["data"]["summary"]["potential_monthly_savings"] == 30.0


# ------------------------------------------------------------ protocol attacks
def test_wrong_pkce_verifier_is_rejected(client) -> None:
    _, challenge = pkce()
    _, redirect = consent(client, challenge)
    other_verifier, _ = pkce()
    res = exchange(client, code_from(redirect), other_verifier)
    assert res.status_code == 400 and res.json()["error"] == "invalid_grant"


def test_code_is_single_use(client) -> None:
    verifier, challenge = pkce()
    _, redirect = consent(client, challenge)
    code = code_from(redirect)
    assert exchange(client, code, verifier).status_code == 200
    assert exchange(client, code, verifier).json()["error"] == "invalid_grant"


def test_failed_exchange_still_burns_the_code(client) -> None:
    verifier, challenge = pkce()
    _, redirect = consent(client, challenge)
    code = code_from(redirect)
    assert exchange(client, code, "x" * 50).status_code == 400  # wrong verifier
    assert exchange(client, code, verifier).status_code == 400  # code is gone now


def test_redirect_uri_must_match_at_token_exchange(client) -> None:
    verifier, challenge = pkce()
    _, redirect = consent(client, challenge)
    res = exchange(client, code_from(redirect), verifier, redirect_uri=f"{BASE}/elsewhere")
    assert res.json()["error"] == "invalid_grant"


def test_foreign_or_unregistered_redirect_uri_never_redirects(client) -> None:
    _, challenge = pkce()
    for bad in ("https://evil.example/chat/callback", f"{BASE}/chat/other", f"{BASE}/chat/callback?x=1"):
        res = client.get(f"/oauth/authorize?{urlencode(authorize_params(challenge, redirect_uri=bad))}")
        assert res.status_code == 400 and "location" not in res.headers


def test_pkce_s256_is_required(client) -> None:
    for overrides in ({"code_challenge_method": "plain"}, {"code_challenge": "short"}):
        res = client.get(f"/oauth/authorize?{urlencode(authorize_params('a' * 43, **overrides))}")
        assert res.status_code == 400


def test_unknown_client_and_scope_rejected(client) -> None:
    _, challenge = pkce()
    assert client.get(f"/oauth/authorize?{urlencode(authorize_params(challenge, client_id='x'))}").status_code == 400
    bad_scope = authorize_params(challenge, scope="account:read admin:all")
    assert client.get(f"/oauth/authorize?{urlencode(bad_scope)}").status_code == 400


def test_tampered_consent_form_is_rejected(client) -> None:
    res = client.post(
        "/oauth/authorize/consent",
        data={"request": "eyJhbGciOiJub25lIn0.eyJ4IjoxfQ.", "account": "LUM-9100", "decision": "allow"},
        follow_redirects=False,
    )
    assert res.status_code == 400


def test_cancel_returns_access_denied(client) -> None:
    _, challenge = pkce()
    _, res = consent(client, challenge, decision="deny")
    query = parse_qs(urlparse(res.headers["location"]).query)
    assert query["error"] == ["access_denied"] and "code" not in query


# ----------------------------------------------------------- session binding
def test_session_requires_a_valid_token(client) -> None:
    handoff = client.post("/api/handoff", json={"scenario_id": "speed-upgrade"}).json()["token"]
    assert client.post("/api/sessions", json={"handoff_token": handoff}).status_code == 401
    bad = client.post("/api/sessions", json={"handoff_token": handoff}, headers={"Authorization": "Bearer junk"})
    assert bad.status_code == 401
    # The handoff link survives failed sign-ins (validation happens before it's redeemed).
    good = client.post(
        "/api/sessions",
        json={"handoff_token": handoff},
        headers={"Authorization": f"Bearer {sign_in(client, 'speed-upgrade')}"},
    )
    assert good.status_code == 200


def test_expired_and_forged_tokens_rejected(client) -> None:
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": "LUM-9100", "scope": "account:read", "jti": "j", "iat": now - 99}
    expired = jwt.encode({**claims, "exp": now - 10}, TEST_ENV["HANDOFF_SECRET"] + "x", algorithm="HS256")
    forged = jwt.encode({**claims, "exp": now + 600}, "not-the-real-signing-secret-000000", algorithm="HS256")
    for token in (expired, forged):
        assert open_session(client, token).status_code == 401


def test_signed_in_identity_decides_whose_data_is_shown(client) -> None:
    """A Morgan token used with a Priya-scenario link still shows Morgan's account, never Priya's."""
    token, _ = client.app.state.ctx.idp.issue_token("LUM-9100", "Morgan", "account:read account:manage")
    res = open_session(client, token, scenario="speed-upgrade")
    assert res.json()["customer"]["first_name"] == "Morgan"


def test_view_only_access_blocks_actions_in_code() -> None:
    c, llm = scripted_client([("Returning it now.", [("enroll_autopay", {})]), ("Ok.", [])])
    with c:
        sid = start_session(c, "account-checkup", scope="account:read")
        events = turn(c, sid, {"message": "turn on autopay"})
    assert not [e for e in events if e["type"] == "confirm_required"]
    assert any(e["type"] == "notice" and "view-only" in e["message"] for e in events)
    assert any("permission_not_granted" in m["content"] for m in llm.calls[-1] if m["role"] == "tool")
    assert "VIEW ONLY" in llm.calls[-1][0]["content"]


def test_disconnect_revokes_and_ends_the_session(client) -> None:
    sid = start_session(client, "account-checkup")
    assert client.post(f"/api/sessions/{sid}/disconnect").json() == {"disconnected": True}
    res = client.post(f"/api/sessions/{sid}/turn", json={"message": "hi"})
    assert res.status_code in (401, 404)


def test_revoked_token_cannot_open_new_sessions(client) -> None:
    token, claims = client.app.state.ctx.idp.issue_token("LUM-9100", "Morgan", "account:read")
    client.app.state.ctx.idp.revoke(claims["jti"])
    assert open_session(client, token).status_code == 401


def test_oauth_codes_and_state_are_masked_in_logs() -> None:
    from app.logging_setup import mask_path

    masked = mask_path("/chat/callback?code=abc123def&state=xyz")
    assert "abc123def" not in masked and "xyz" not in masked


def test_kickoff_stream_is_valid_sse(client) -> None:
    sid = start_session(client, "account-checkup")
    res = client.post(f"/api/sessions/{sid}/turn", json={"kickoff": True})
    assert res.headers["content-type"].startswith("text/event-stream")
    assert parse_sse(res.text)[-1]["type"] == "done"


def test_account_picker_labels_situation_and_preselects_match(client) -> None:
    _, challenge = pkce()
    page = client.get(f"/oauth/authorize?{urlencode(authorize_params(challenge, login_hint='scenario:gamer-upgrade'))}")
    assert page.status_code == 200
    html_text = page.text
    selected = [line for line in html_text.split("<option") if " selected" in line.split(">")[0]]
    assert len(selected) == 1
    assert "Jordan" in selected[0] and "Online games lag every evening" in selected[0]
    assert "(matches this search)" in selected[0]
    assert html_text.count("(matches this search)") == 1
    # Launcher order: the account checkup persona comes first.
    assert html_text.index("Morgan") < html_text.index("Dana")


def test_scenarios_api_has_persona_in_demo_order_without_ids(client) -> None:
    items = client.get("/api/scenarios").json()["scenarios"]
    orders = [s["demo_order"] for s in items]
    assert orders == sorted(orders)
    assert items[0]["id"] == "account-checkup"
    for s in items:
        assert set(s["persona"]) == {"first_name", "plan"}
    assert "LUM-" not in str(items)
