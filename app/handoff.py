"""Signed, short-lived, single-use handoff tokens.

Models the moment a customer searches in an external assistant (e.g. Muse) and
lands in our chat with context. The token carries only the scenario id and the
search phrase: no customer data in URLs, browser history or proxy logs.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass

import jwt

AUDIENCE = "lumora-assist"
ISSUER = "search-handoff"


class HandoffError(ValueError):
    pass


@dataclass
class HandoffContext:
    scenario_id: str
    search_query: str
    jti: str


class HandoffSigner:
    def __init__(self, secret: str, ttl_s: int) -> None:
        if len(secret) < 16:
            raise ValueError("handoff secret too short")
        self._secret = secret
        self._ttl = ttl_s
        self._used: dict[str, float] = {}
        self._lock = threading.Lock()

    def issue(self, scenario_id: str, search_query: str) -> str:
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "iat": now,
            "exp": now + self._ttl,
            "jti": secrets.token_urlsafe(12),
            "scn": scenario_id,
            "q": search_query[:200],
        }
        return jwt.encode(claims, self._secret, algorithm="HS256")

    def redeem(self, token: str) -> HandoffContext:
        try:
            claims = jwt.decode(
                token,
                self._secret,
                algorithms=["HS256"],  # pinned: rejects alg=none and algorithm confusion
                audience=AUDIENCE,
                issuer=ISSUER,
                options={"require": ["exp", "iat", "jti", "aud", "iss"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise HandoffError("Handoff link expired. Please search again.") from exc
        except jwt.PyJWTError as exc:
            raise HandoffError("Invalid handoff link.") from exc
        jti = str(claims["jti"])
        with self._lock:
            now = time.time()
            self._used = {k: v for k, v in self._used.items() if v > now}
            if jti in self._used:
                raise HandoffError("This handoff link was already used.")
            self._used[jti] = float(claims["exp"])
        return HandoffContext(scenario_id=str(claims["scn"]), search_query=str(claims.get("q", "")), jti=jti)
