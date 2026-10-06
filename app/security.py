"""HTTP security middleware (pure ASGI so streaming responses are never buffered)."""

from __future__ import annotations

import base64
import hmac
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key == name:
            return value.decode("latin-1")
    return None


def client_ip(scope: Scope) -> str:
    """Cloud Foundry's router appends the real client to X-Forwarded-For."""
    forwarded = _header(scope, b"x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    client = scope.get("client")
    return client[0] if client else "unknown"


async def _json_response(
    send: Send, status: int, body: dict[str, Any], headers: list[tuple[bytes, bytes]] | None = None
) -> None:
    payload = json.dumps(body).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
                *(headers or []),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, hsts: bool) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def wrapped(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers += [
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-frame-options", b"DENY"),
                    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                    (b"content-security-policy", CSP.encode()),
                ]
                if self.hsts:
                    headers.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, wrapped)


class BearerTokenMiddleware:
    """Protects a path prefix (the /mcp endpoint) with a static bearer token."""

    def __init__(self, app: ASGIApp, prefix: str, token: str) -> None:
        self.app = app
        self.prefix = prefix
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"].startswith(self.prefix):
            provided = (_header(scope, b"authorization") or "").encode()
            if not hmac.compare_digest(provided, self.expected):
                await _json_response(
                    send, 401, {"error": "unauthorized"}, [(b"www-authenticate", b'Bearer realm="mcp"')]
                )
                return
        await self.app(scope, receive, send)


class BasicAuthMiddleware:
    """Optional demo gate for the web UI and API. /healthz and /mcp are excluded."""

    def __init__(self, app: ASGIApp, user: str, password: str, exclude: tuple[str, ...]) -> None:
        self.app = app
        self.expected = b"Basic " + base64.b64encode(f"{user}:{password}".encode())
        self.exclude = exclude

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not scope["path"].startswith(self.exclude):
            provided = (_header(scope, b"authorization") or "").encode()
            if not hmac.compare_digest(provided, self.expected):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"www-authenticate", b'Basic realm="demo"'), (b"content-length", b"0")],
                    }
                )
                await send({"type": "http.response.body", "body": b""})
                return
        await self.app(scope, receive, send)


class RateLimitMiddleware:
    """Token bucket per client IP for POST requests under /api."""

    def __init__(self, app: ASGIApp, per_minute: int) -> None:
        self.app = app
        self.capacity = max(per_minute, 1)
        self.refill = self.capacity / 60.0
        self.buckets: dict[str, tuple[float, float]] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] == "POST" and scope["path"].startswith("/api/"):
            ip = client_ip(scope)
            now = time.monotonic()
            tokens, last = self.buckets.get(ip, (float(self.capacity), now))
            tokens = min(self.capacity, tokens + (now - last) * self.refill)
            if tokens < 1:
                await _json_response(
                    send, 429, {"error": "rate_limited", "message": "Too many requests."}, [(b"retry-after", b"10")]
                )
                return
            self.buckets[ip] = (tokens - 1, now)
            if len(self.buckets) > 10_000:  # bound memory
                self.buckets.clear()
        await self.app(scope, receive, send)


class BodySizeLimitMiddleware:
    """Reject oversized JSON bodies on /api (the MCP SDK enforces its own limit on /mcp)."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            await self.app(scope, receive, send)
            return
        length = _header(scope, b"content-length")
        if length and length.isdigit() and int(length) > self.max_bytes:
            await _json_response(send, 413, {"error": "payload_too_large"})
            return
        received = 0

        async def limited() -> dict[str, Any]:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise ValueError("payload too large")
            return message

        await self.app(scope, limited, send)
