"""Check that the configured model is reachable and can call tools.

    make llm-check            # uses .env / environment, same settings as the app
    python -m app.llm_check   # same thing; also works inside the Cloud Foundry container

Sends two tiny requests: a plain "Reply with OK", then one that should make the model call a
tool (the assistant depends on tool calling). Prints the route, latency, whether the answer
streamed, and a hint on failure. Never prints keys. Exit 0 = ready, 1 = failed, 2 = config problem.
"""

from __future__ import annotations

import asyncio
import sys
import time
from urllib.parse import urlsplit

from app.config import ConfigError, Settings, describe_proxy, load_settings
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


def _target(s: Settings) -> str:
    if s.llm_provider == "gateway":
        parts = urlsplit(s.llm_gateway_url)
        port = f":{parts.port}" if parts.port else ""
        return f"{parts.scheme}://{parts.hostname}{port}{parts.path}"  # no credentials or query string
    return str(urlsplit(s.azure_openai_endpoint or s.openai_compat_base_url).hostname)


async def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Config problem: {exc}")
        return 2
    if settings.llm_provider == "mock":
        print("LLM_PROVIDER=mock: nothing to check. Set LLM_PROVIDER=gateway (or azure_openai) in .env.")
        return 2
    print(f"Provider : {settings.llm_provider}")
    print(f"Target   : {_target(settings)}")
    print(f"Route    : {'proxy ' + describe_proxy(settings.llm_proxy_url) if settings.llm_proxy_url else 'direct'}")
    if settings.llm_provider == "gateway":
        key = settings.llm_gateway_key_header
        print(f"Key      : {'header ' + key if key else 'none sent'}")
        print(f"Model    : {settings.llm_gateway_model or 'not sent (gateway decides)'}")
    if settings.llm_ca_bundle:
        print(f"CA bundle: {settings.llm_ca_bundle}")

    llm = build_llm(settings)

    # 1) plain reply
    started = time.monotonic()
    reply = ""
    try:
        async for event in llm.stream([{"role": "user", "content": "Reply with the single word OK."}], []):
            if isinstance(event, TextDelta):
                reply += event.text
    except LlmError as exc:
        print(f"FAILED   : {exc}")
        print(_hint(str(exc)))
        return 1
    mode = getattr(llm, "last_mode", "")
    print(
        f"Reply    : OK in {time.monotonic() - started:.1f}s, {reply.strip()[:40]!r}"
        + (
            " (streamed)"
            if mode == "stream"
            else " (not streamed: answers appear all at once)"
            if mode == "json"
            else ""
        )
    )

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
        print(f"Tools    : FAILED ({exc})")
        print("Hint: the gateway may not accept the 'tools' field. The assistant needs tool calling to work.")
        return 1
    if "check_area_outage" in calls:
        print("Tools    : OK (model called the probe tool)")
    else:
        print("Tools    : no tool call. The gateway may drop 'tools', or the model ignored it; the chat will be weak.")
        return 1
    print("Ready.")
    return 0


def _hint(message: str) -> str:
    if "certificate" in message:
        return "Hint: TLS isn't trusted. Get the gateway/proxy CA (PEM) from your network team and set LLM_CA_BUNDLE."
    if "proxy error" in message:
        return "Hint: check LLM_PROXY_URL (host and port). Leave it empty if you call a gateway URL directly."
    if "HTTP 401" in message or "HTTP 403" in message:
        return "Hint: reached the server but not allowed. Does the gateway need a key (LLM_GATEWAY_KEY_HEADER/KEY)?"
    if "HTTP 404" in message or "HTTP 405" in message:
        return "Hint: wrong URL or method. Check LLM_GATEWAY_URL (full path, POST)."
    if "HTTP 400" in message:
        return "Hint: the gateway rejected the request body. Share the message above; it may want a different shape."
    if "via proxy" in message and "ConnectError" in message:
        return "Hint: couldn't reach the proxy itself. Check the LLM_PROXY_URL host and port from this machine."
    if "timed out" in message or "ConnectError" in message:
        return "Hint: no route to the host from this machine (VPN? firewall? needs LLM_PROXY_URL?)."
    return "Hint: see docs/deployment-cloud-foundry.md, Troubleshooting."


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
