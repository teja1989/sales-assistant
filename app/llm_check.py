"""Check that Azure OpenAI answers and can call tools, with the app's own settings.

    make llm-check            # local: uses .env
    python -m app.llm_check   # same; also works inside the Cloud Foundry container (cf ssh)

Sends two tiny requests through the official SDK: a plain "Reply with OK", then one that should
make the model call a tool (the assistant depends on tool calling). Shows whether a proxy
(HTTPS_PROXY) is in effect. Never prints the key. Exit 0 = ready, 1 = failed, 2 = config problem.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from urllib.parse import urlsplit

from app.config import ConfigError, display_url, load_settings
from app.llm import build_llm
from app.llm.base import LlmError, TextDelta, TurnComplete

PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": "check_area_outage",
        "description": "Check whether there is a network outage in the customer's area.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
}


def _proxy_in_effect(endpoint: str) -> str:
    """Which proxy the SDK's HTTP client will use for the endpoint (it honours these env vars)."""
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
    if not proxy:
        return "none (direct)"
    host = urlsplit(endpoint).hostname or ""
    no_proxy = (os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or "").split(",")
    if any(
        n.strip() and (host == n.strip().lstrip(".") or host.endswith("." + n.strip().lstrip("."))) for n in no_proxy
    ):
        return f"none: {host} is in NO_PROXY"
    parts = urlsplit(proxy)
    return f"HTTPS_PROXY {parts.hostname}:{parts.port}" if parts.port else f"HTTPS_PROXY {parts.hostname}"


async def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Config problem: {exc}")
        return 2
    if not settings.azure_openai_endpoint:
        print("AZURE_OPENAI_ENDPOINT is empty, so the app uses the offline mock model. Set the AZURE_OPENAI_* values.")
        return 2
    print(f"Endpoint  : {display_url(settings.azure_openai_endpoint)}")
    print(f"Deployment: {settings.azure_openai_deployment}")
    print(f"API ver.  : {settings.azure_openai_api_version}")
    print(f"Proxy     : {_proxy_in_effect(settings.azure_openai_endpoint)}")

    try:
        llm = build_llm(settings)
    except ConfigError as exc:
        print(f"Config problem: {exc}")
        return 2

    # 1) plain reply
    started = time.monotonic()
    reply = ""
    try:
        async for event in llm.stream([{"role": "user", "content": "Reply with the single word OK."}], []):
            if isinstance(event, TextDelta):
                reply += event.text
    except LlmError as exc:
        print(f"FAILED    : {exc}")
        print(_hint(str(exc)))
        return 1
    print(f"Reply     : OK in {time.monotonic() - started:.1f}s, {reply.strip()[:40]!r}")

    # 2) tool calling
    calls = []
    try:
        messages = [
            {"role": "system", "content": "You are a support assistant. Use tools when they help."},
            {"role": "user", "content": "Is there an outage in my area right now? Check, please."},
        ]
        async for event in llm.stream(messages, [PROBE_TOOL]):
            if isinstance(event, TurnComplete):
                calls = [c.name for c in event.tool_calls]
    except LlmError as exc:
        print(f"Tools     : FAILED ({exc})")
        print("Hint: the gateway may not accept the 'tools' field. The assistant needs tool calling to work.")
        return 1
    if "check_area_outage" in calls:
        print("Tools     : OK (model called the probe tool)")
    else:
        print("Tools     : no tool call. Check the deployment's model supports tools; the chat will be weak.")
        return 1
    print("Ready.")
    return 0


def _hint(message: str) -> str:
    if "certificate" in message:
        return "Hint: TLS isn't trusted (proxy re-signs certificates?). Point SSL_CERT_FILE at the CA bundle (PEM)."
    if "HTTP 401" in message:
        return "Hint: key rejected. Check AZURE_OPENAI_API_KEY belongs to this endpoint's resource."
    if "HTTP 403" in message:
        return "Hint: reached Azure but not allowed (network rules on the resource, or the proxy blocks it)."
    if "HTTP 404" in message:
        return "Hint: check AZURE_OPENAI_DEPLOYMENT (the deployment name, not the model) and AZURE_OPENAI_API_VERSION."
    if "HTTP 400" in message:
        return "Hint: request rejected. Check AZURE_OPENAI_API_VERSION and that the deployment supports tools."
    if "connection error" in message or "timed out" in message:
        return "Hint: can't reach the endpoint. Locally: VPN/firewall. On CF: is the proxy service bound (HTTPS_PROXY)?"
    return "Hint: see docs/configuration.md."


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
