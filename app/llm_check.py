"""Check that the configured model is reachable (directly or through LLM_PROXY_URL).

    make llm-check            # uses .env / environment, same settings as the app
    python -m app.llm_check   # same thing; also works inside the Cloud Foundry container

Sends one tiny chat request ("Reply with OK") and prints the route, latency and reply.
It never prints keys. Exit code 0 = reachable, 1 = failed, 2 = configuration problem.
"""

from __future__ import annotations

import asyncio
import sys
import time
from urllib.parse import urlsplit

from app.config import ConfigError, describe_proxy, load_settings
from app.llm import build_llm
from app.llm.base import LlmError, TextDelta


async def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Config problem: {exc}")
        return 2
    if settings.llm_provider == "mock":
        print("LLM_PROVIDER=mock: nothing to check. Set LLM_PROVIDER=azure_openai in .env.")
        return 2
    endpoint = settings.azure_openai_endpoint or settings.openai_compat_base_url
    print(f"Provider : {settings.llm_provider}")
    print(f"Endpoint : {urlsplit(endpoint).hostname}")
    print(f"Route    : {'proxy ' + describe_proxy(settings.llm_proxy_url) if settings.llm_proxy_url else 'direct'}")
    if settings.llm_ca_bundle:
        print(f"CA bundle: {settings.llm_ca_bundle}")

    llm = build_llm(settings)
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
    print(f"OK       : {time.monotonic() - started:.1f}s, reply {reply.strip()[:40]!r}")
    return 0


def _hint(message: str) -> str:
    if "certificate" in message:
        return "Hint: the proxy inspects TLS. Get its CA (PEM) from your network team and set LLM_CA_BUNDLE."
    if "proxy error" in message:
        return "Hint: check LLM_PROXY_URL (host and port) and that the proxy allows the Azure host."
    if "HTTP 401" in message or "HTTP 403" in message:
        return "Hint: the request reached Azure; check AZURE_OPENAI_API_KEY (or the proxy's allow rules for 403)."
    if "HTTP 404" in message:
        return "Hint: check AZURE_OPENAI_DEPLOYMENT and AZURE_OPENAI_API_VERSION."
    if "via proxy" in message and "ConnectError" in message:
        return "Hint: couldn't reach the proxy itself. Check the LLM_PROXY_URL host and port from this machine."
    if "timed out" in message or "ConnectError" in message:
        return "Hint: no route to the host. Is LLM_PROXY_URL needed (or wrong) on this network?"
    return "Hint: see docs/deployment-cloud-foundry.md, Troubleshooting."


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
