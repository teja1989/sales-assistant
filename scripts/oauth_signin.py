#!/usr/bin/env python3
"""Sign in through the mock identity provider (auth code + PKCE) and print an access token.

Used by scripts/smoke.sh. Standard library only, so it runs anywhere Python 3 does.
    python3 scripts/oauth_signin.py http://127.0.0.1:8000 [scenario-id]
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


def main() -> int:
    base = sys.argv[1].rstrip("/")
    scenario = sys.argv[2] if len(sys.argv) > 2 else "gateway-fault"
    auth = sys.argv[3] if len(sys.argv) > 3 else ""
    headers = {"Authorization": "Basic " + base64.b64encode(auth.encode()).decode()} if auth else {}
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    redirect = f"{base}/chat/callback"
    params = {
        "response_type": "code",
        "client_id": "tidelink-web",
        "redirect_uri": redirect,
        "scope": "account:read account:manage",
        "state": "smoke",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "login_hint": f"scenario:{scenario}",
    }
    page = urllib.request.urlopen(
        urllib.request.Request(f"{base}/oauth/authorize?{urllib.parse.urlencode(params)}", headers=headers)
    ).read().decode()
    signed = re.search(r'name="request" value="([^"]+)"', page).group(1)
    account = re.search(r'<option value="([^"]+)" selected', page) or re.search(r'<option value="([^"]+)"', page)
    form = urllib.parse.urlencode(
        {"request": signed, "account": account.group(1), "decision": "allow", "allow_manage": "on"}
    ).encode()
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        opener.open(urllib.request.Request(f"{base}/oauth/authorize/consent", data=form, headers=headers))
        print("consent did not redirect", file=sys.stderr)
        return 1
    except urllib.error.HTTPError as exc:
        location = exc.headers["Location"]
    code = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)["code"][0]
    token_form = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect,
            "client_id": "tidelink-web",
            "code_verifier": verifier,
        }
    ).encode()
    body = json.loads(
        urllib.request.urlopen(urllib.request.Request(f"{base}/oauth/token", data=token_form, headers=headers)).read()
    )
    print(body["access_token"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
