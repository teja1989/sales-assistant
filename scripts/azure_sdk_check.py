"""Standalone check: can the Azure OpenAI Python SDK reach your endpoint/proxy?

    pip install openai
    export AZURE_OPENAI_ENDPOINT="https://<proxy-or-resource-host>"   # what you'd pass as azure_endpoint
    export AZURE_OPENAI_API_KEY="<key>"
    export AZURE_OPENAI_DEPLOYMENT="<deployment name, e.g. gpt-4.1>"
    export AZURE_OPENAI_API_VERSION="2024-10-21"                       # optional
    python scripts/azure_sdk_check.py

Runs two calls (a plain reply, then a tool call, which the assistant depends on) and prints
the exact URL the SDK requested, so the app can be configured to match. Never prints the key.
"""

from __future__ import annotations

import os
import sys

import httpx
from openai import APIConnectionError, APIStatusError, AzureOpenAI


def need(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if not value:
        sys.exit(f"Set {name} (see the top of this script).")
    return value


endpoint = need("AZURE_OPENAI_ENDPOINT")
key = need("AZURE_OPENAI_API_KEY")
deployment = need("AZURE_OPENAI_DEPLOYMENT")
api_version = need("AZURE_OPENAI_API_VERSION", "2024-10-21")


def show_request(request: httpx.Request) -> None:
    headers = sorted(h for h in request.headers if h.lower() in ("api-key", "authorization"))
    print(f"  -> {request.method} {request.url}")
    print(f"     auth header(s): {', '.join(headers) or 'none'} (value hidden)")


client = AzureOpenAI(
    azure_endpoint=endpoint,
    api_key=key,
    api_version=api_version,
    http_client=httpx.Client(event_hooks={"request": [show_request]}, timeout=60),
    max_retries=0,
)

print(f"Endpoint: {endpoint}\nDeployment: {deployment}\nAPI version: {api_version}\n")

try:
    print("1) Plain reply")
    reply = client.chat.completions.create(
        model=deployment, messages=[{"role": "user", "content": "Reply with the single word OK."}], max_tokens=5
    )
    print(f"  OK: {reply.choices[0].message.content!r}\n")

    print("2) Tool call")
    tool = {
        "type": "function",
        "function": {
            "name": "check_area_outage",
            "description": "Check whether there is a network outage in the customer's area.",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    reply = client.chat.completions.create(
        model=deployment,
        messages=[{"role": "user", "content": "Is there an outage in my area right now? Please check."}],
        tools=[tool],
        max_tokens=50,
    )
    calls = [c.function.name for c in reply.choices[0].message.tool_calls or []]
    print(f"  {'OK' if calls else 'NO TOOL CALL'}: {calls or reply.choices[0].message.content!r}\n")
    print("Working. Send me the URL printed after '->' (host can be blanked) and I'll match the app to it.")
except APIStatusError as exc:
    print(f"  FAILED: HTTP {exc.status_code}: {str(exc.message)[:300]}")
    hints = {
        401: "Key rejected, or the endpoint expects a different header than api-key.",
        403: "Key valid but not allowed (network rules / permissions).",
        404: "Wrong deployment name, API version, or endpoint path.",
        400: "Request rejected: check API version and deployment model.",
    }
    print(f"  Hint: {hints.get(exc.status_code, 'see the message above.')}")
    sys.exit(1)
except APIConnectionError as exc:
    print(f"  FAILED: can't connect ({exc.__cause__ or exc}). Check the host, VPN/firewall, or certificates.")
    sys.exit(1)
