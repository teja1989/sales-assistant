"""A mock OAuth 2.0 identity provider (authorization code + PKCE), built into the app for the demo.

It speaks the real protocol, so swapping in the company's identity provider later is a config change:
the browser does a standard redirect to /oauth/authorize, the customer signs in and consents to scopes,
the browser exchanges the one-time code (with its PKCE verifier) at /oauth/token, and the chat API only
accepts sessions that present a valid access token.

What is simulated: the sign-in itself (pick a demo account; no passwords). Everything else is enforced:
registered client, same-origin redirect URI, S256 PKCE, single-use 60-second codes, signed consent form,
short-lived signed tokens, scopes, and revocation.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import re
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlparse

import jwt
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

CLIENT_ID = "tidelink-web"
REDIRECT_PATH = "/chat/callback"
ISSUER = "mock-idp"
AUDIENCE = "tidelink-api"
CONSENT_AUD = "mock-idp-consent"
SCOPES = {
    "account:read": "View your account, services, usage and bills",
    "account:manage": "Make changes you confirm in the chat (orders, restarts, credits, settings)",
}
REQUIRED_SCOPE = "account:read"
CODE_TTL_S = 60


class OAuthError(Exception):
    def __init__(self, error: str, description: str, status: int = 400) -> None:
        super().__init__(description)
        self.error = error
        self.description = description
        self.status = status


ACCOUNT_NUMBER = re.compile(r"[A-Za-z0-9_\-]{1,64}")


@dataclass
class DemoAccount:
    subject: str
    name: str
    plan: str
    hint: str  # scenario id this account belongs to (used as login_hint)
    situation: str = ""  # what this demo customer is dealing with, shown on the account picker


@dataclass
class _Code:
    subject: str
    name: str
    scope: str
    redirect_uri: str
    challenge: str
    expires: float


def _b64url_sha256(text: str) -> str:
    digest = hashlib.sha256(text.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class MockIdentityProvider:
    def __init__(self, secret: str, token_ttl_s: int = 1800) -> None:
        if len(secret) < 16:
            raise ValueError("OAuth signing secret too short")
        self._secret = secret
        self._ttl = token_ttl_s
        self._codes: dict[str, _Code] = {}
        self._revoked: dict[str, float] = {}
        self._lock = threading.Lock()
        self.accounts: dict[str, DemoAccount] = {}
        # Live data mode: the sign-in is still simulated, but the person types a real account number.
        self.free_account_entry = False

    # ------------------------------------------------------------ accounts
    def set_accounts(self, accounts: list[DemoAccount]) -> None:
        self.accounts = {a.subject: a for a in accounts}

    # -------------------------------------------------------------- tokens
    def issue_token(self, subject: str, name: str, scope: str) -> tuple[str, dict[str, Any]]:
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": subject,
            "name": name,
            "scope": scope,
            "client_id": CLIENT_ID,
            "iat": now,
            "exp": now + self._ttl,
            "jti": secrets.token_urlsafe(12),
        }
        return jwt.encode(claims, self._secret, algorithm="HS256"), claims

    def verify(self, token: str) -> dict[str, Any]:
        try:
            claims = jwt.decode(
                token,
                self._secret,
                algorithms=["HS256"],
                audience=AUDIENCE,
                issuer=ISSUER,
                options={"require": ["exp", "iat", "jti", "sub", "aud", "iss"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise OAuthError("invalid_token", "Your sign-in expired. Please sign in again.", 401) from exc
        except jwt.PyJWTError as exc:
            raise OAuthError("invalid_token", "Invalid sign-in token.", 401) from exc
        if self.is_revoked(str(claims["jti"])):
            raise OAuthError("invalid_token", "This account connection was disconnected.", 401)
        if REQUIRED_SCOPE not in str(claims.get("scope", "")).split():
            raise OAuthError("insufficient_scope", "Viewing your account wasn't allowed.", 403)
        known = (
            ACCOUNT_NUMBER.fullmatch(str(claims["sub"])) if self.free_account_entry else claims["sub"] in self.accounts
        )
        if not known:
            raise OAuthError("invalid_token", "Unknown account.", 401)
        return claims

    def revoke(self, jti: str, expires_at: float | None = None) -> None:
        with self._lock:
            self._revoked[jti] = expires_at or time.time() + self._ttl

    def is_revoked(self, jti: str) -> bool:
        with self._lock:
            now = time.time()
            self._revoked = {k: v for k, v in self._revoked.items() if v > now}
            return jti in self._revoked

    # ------------------------------------------------------- authorization
    def _validate_authorize(self, params: dict[str, str], request: Request) -> None:
        if params.get("client_id") != CLIENT_ID:
            raise OAuthError("unauthorized_client", "Unknown application.")
        redirect = urlparse(params.get("redirect_uri", ""))
        if (
            redirect.path != REDIRECT_PATH
            or redirect.netloc != request.url.netloc
            or redirect.scheme not in ("http", "https")
            or redirect.query
            or redirect.fragment
        ):
            # Never redirect to an unregistered location (open-redirect / code theft protection).
            raise OAuthError("invalid_request", "That redirect address isn't registered for this application.")
        if params.get("response_type") != "code":
            raise OAuthError("unsupported_response_type", "Only the authorization code flow is supported.")
        challenge = params.get("code_challenge", "")
        if params.get("code_challenge_method") != "S256" or not 43 <= len(challenge) <= 128:
            raise OAuthError("invalid_request", "PKCE with S256 is required.")
        if not params.get("state") or len(params["state"]) > 200:
            raise OAuthError("invalid_request", "A state value is required.")
        requested = set(params.get("scope", "").split())
        if REQUIRED_SCOPE not in requested or not requested <= set(SCOPES):
            raise OAuthError("invalid_scope", "Unsupported or missing scopes.")

    async def authorize_page(self, request: Request) -> Response:
        params = {k: v for k, v in request.query_params.items()}
        try:
            self._validate_authorize(params, request)
        except OAuthError as exc:
            return _error_page(exc.description, exc.status)
        signed = jwt.encode(
            {**params, "aud": CONSENT_AUD, "exp": int(time.time()) + 600}, self._secret, algorithm="HS256"
        )
        hint = params.get("login_hint", "").removeprefix("scenario:")
        requested = params.get("scope", "").split()
        accounts = None if self.free_account_entry else list(self.accounts.values())
        return HTMLResponse(_consent_page(accounts, hint, requested, signed), headers={"Cache-Control": "no-store"})

    async def authorize_submit(self, request: Request) -> Response:
        form = await request.form()
        try:
            params = jwt.decode(str(form.get("request", "")), self._secret, algorithms=["HS256"], audience=CONSENT_AUD)
        except jwt.PyJWTError:
            return _error_page("This sign-in page expired. Please start again.", 400)
        params = {k: v for k, v in params.items() if k not in ("aud", "exp")}
        try:
            self._validate_authorize(params, request)
        except OAuthError as exc:
            return _error_page(exc.description, exc.status)
        redirect_uri = params["redirect_uri"]
        if form.get("decision") != "allow":
            return RedirectResponse(
                f"{redirect_uri}?{urlencode({'error': 'access_denied', 'state': params['state']})}", 303
            )
        entered = str(form.get("account", "")).strip()
        if self.free_account_entry:
            if not ACCOUNT_NUMBER.fullmatch(entered):
                return _error_page("Please enter a valid account number (letters, digits, - or _).", 400)
            account = DemoAccount(entered, "", "", "")
        else:
            account = self.accounts.get(entered)  # type: ignore[assignment]
            if account is None:
                return _error_page("Please choose an account.", 400)
        granted = [REQUIRED_SCOPE]
        if "account:manage" in params.get("scope", "").split() and form.get("allow_manage") == "on":
            granted.append("account:manage")
        code = secrets.token_urlsafe(32)
        with self._lock:
            now = time.time()
            self._codes = {k: v for k, v in self._codes.items() if v.expires > now}
            self._codes[code] = _Code(
                account.subject,
                account.name,
                " ".join(granted),
                redirect_uri,
                params["code_challenge"],
                now + CODE_TTL_S,
            )
        return RedirectResponse(f"{redirect_uri}?{urlencode({'code': code, 'state': params['state']})}", 303)

    async def token(self, request: Request) -> Response:
        form = await request.form()
        headers = {"Cache-Control": "no-store", "Pragma": "no-cache"}
        if form.get("grant_type") != "authorization_code":
            return JSONResponse({"error": "unsupported_grant_type"}, 400, headers=headers)
        if form.get("client_id") != CLIENT_ID:
            return JSONResponse({"error": "invalid_client"}, 401, headers=headers)
        with self._lock:
            entry = self._codes.pop(str(form.get("code", "")), None)  # single use, even if checks below fail
        if entry is None or entry.expires < time.time():
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "Code is invalid or expired."}, 400, headers=headers
            )
        if form.get("redirect_uri") != entry.redirect_uri:
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "Redirect URI mismatch."}, 400, headers=headers
            )
        verifier = str(form.get("code_verifier", ""))
        if not 43 <= len(verifier) <= 128 or not hmac.compare_digest(_b64url_sha256(verifier), entry.challenge):
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "PKCE verification failed."}, 400, headers=headers
            )
        token, claims = self.issue_token(entry.subject, entry.name, entry.scope)
        return JSONResponse(
            {"access_token": token, "token_type": "Bearer", "expires_in": self._ttl, "scope": claims["scope"]},
            headers=headers,
        )


# ---------------------------------------------------------------- pages
_PAGE_STYLE = """
body{margin:0;font-family:system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;background:#f4f5f7;color:#1d2330}
.sim{background:#2b2b33;color:#f2f2f5;font-size:13px;padding:8px 20px}
main{max-width:440px;margin:6vh auto;background:#fff;border:1px solid #e1e3e8;border-radius:14px;padding:28px}
h1{font-size:20px;margin:0 0 4px}p{color:#525a6b;line-height:1.5}
label{display:block;font-weight:600;margin:18px 0 6px}
select,#account{width:100%;box-sizing:border-box;font:inherit;padding:10px;border:1px solid #cfd3db;border-radius:8px;background:#fff}
fieldset{border:1px solid #e1e3e8;border-radius:10px;margin:18px 0 0;padding:12px 14px}
legend{font-weight:600;padding:0 4px}
.scope{display:flex;gap:10px;align-items:flex-start;margin:8px 0;font-weight:400}
.scope input{margin-top:4px}
.actions{display:flex;gap:10px;margin-top:22px}
button{font:inherit;font-weight:600;border-radius:999px;padding:10px 20px;cursor:pointer;border:1.5px solid #cfd3db;background:#fff}
button.allow{background:#1d2330;color:#fff;border-color:#1d2330}
.small{font-size:13px}
"""


def _consent_page(accounts: list[DemoAccount] | None, hint: str, requested: list[str], signed: str) -> str:
    """`accounts=None` (live data mode) shows an account-number field instead of the demo personas."""
    esc = html.escape

    def label(a: DemoAccount) -> str:
        text = f"{a.name} · {a.plan}"
        if a.situation:
            text += f" — {a.situation}"
        if a.hint == hint:
            text += " (matches this search)"
        return esc(text)

    if accounts is None:
        picker = (
            '<label for="account">Account number</label>'
            '<input id="account" name="account" required maxlength="64" autocomplete="off" '
            'pattern="[A-Za-z0-9_\\-]{1,64}" placeholder="e.g. a test account">'
            '<p class="small">Live data: Tidelink will use this account with your MCP server. '
            "Use a test account unless you mean to work on a real one.</p>"
        )
    else:
        options = "".join(
            f'<option value="{esc(a.subject)}"{" selected" if a.hint == hint else ""}>{label(a)}</option>'
            for a in accounts
        )
        picker = (
            f'<label for="account">Account (demo)</label><select id="account" name="account">{options}</select>'
            '<p class="small">Preselected to match the search. Pick another customer to see how Tidelink answers '
            "the same question with their account; the signed-in account always decides whose data is shown.</p>"
        )
    manage = ""
    if "account:manage" in requested:
        manage = (
            '<label class="scope"><input type="checkbox" name="allow_manage" checked> '
            f"<span>{esc(SCOPES['account:manage'])}</span></label>"
        )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Sign in</title>
<style>{_PAGE_STYLE}</style></head><body>
<div class="sim">Simulated identity provider for the demo. In production this is your company's sign-in page.</div>
<main>
<h1>Sign in to your provider account</h1>
<p>Tidelink, your provider's assistant, is asking to connect to your account.</p>
<form method="post" action="/oauth/authorize/consent">
<input type="hidden" name="request" value="{esc(signed)}">
{picker}
<fieldset><legend>Tidelink will be able to</legend>
<label class="scope"><input type="checkbox" checked disabled> <span>{esc(SCOPES["account:read"])} (required)</span></label>
{manage}
</fieldset>
<p class="small">Tidelink never sees your password. You can disconnect anytime from the chat.</p>
<div class="actions">
<button class="allow" type="submit" name="decision" value="allow">Allow and continue</button>
<button type="submit" name="decision" value="deny">Cancel</button>
</div></form></main></body></html>"""


def _error_page(message: str, status: int) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Sign-in problem</title>
<style>{_PAGE_STYLE}</style></head><body><main><h1>Sign-in problem</h1><p>{html.escape(message)}</p>
<p class="small"><a href="/search">Start again</a></p></main></body></html>""",
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )
