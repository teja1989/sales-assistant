"""Check that the model proxy (LLM_PROXY_URL) answers and can call tools.

    make llm-check            # uses .env / environment, same settings as the app
    python -m app.llm_check   # same thing; also works inside the Cloud Foundry container

Sends two tiny requests to LLM_PROXY_URL: a plain "Reply with OK", then one that should make the model call a
tool (the assistant depends on tool calling). Prints latency, whether the answer
streamed, and a hint on failure. Never prints keys. Exit 0 = ready, 1 = failed, 2 = config problem.
"""

from __future__ import annotations

import asyncio
import sys
import time

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


async def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Config problem: {exc}")
        return 2
    if not settings.llm_proxy_url:
        print("LLM_PROXY_URL is empty, so the app uses the offline mock model. Set it in .env to check the proxy.")
        return 2
    print(f"Proxy    : {display_url(settings.llm_proxy_url)}")
    key = settings.llm_proxy_key
    sent_as = (
        ("Authorization header" if key.lower().startswith("bearer ") else "api-key header") if key else "none sent"
    )
    print(f"Key      : {sent_as}")
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
        return "Hint: TLS isn't trusted. Get the proxy's CA certificate (PEM) and set LLM_CA_BUNDLE to its path."
    if "HTTP 401" in message or "HTTP 403" in message:
        return (
            "Hint: the proxy rejected the key. Set LLM_PROXY_KEY: plain value -> api-key header, "
            '"Bearer <token>" -> Authorization header.'
        )
    if "HTTP 404" in message or "HTTP 405" in message:
        return "Hint: wrong URL or method. LLM_PROXY_URL must be the full URL we POST chat requests to."
    if "HTTP 400" in message:
        return "Hint: the proxy rejected the request body. Share the message above."
    if "timed out" in message or "ConnectError" in message:
        return "Hint: no route to the proxy from this machine (VPN? firewall? host name?)."
    return "Hint: see docs/configuration.md."


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
