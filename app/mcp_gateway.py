"""Routes tool calls to the right MCP server: simulated (in-process) or live (HTTP).

Both paths speak real MCP through the official SDK client:
* sim  -> `Client(MCPServer)`: in-process MCP dispatch to our own server.
* live -> `Client(streamable_http_client(url))`: an external MCP server, with an
  Authorization header built from LIVE_MCP_AUTH_SCHEME + LIVE_MCP_TOKEN.

Live tool names can differ from ours; LIVE_TOOL_MAP renames them
(e.g. "get_customer_profile=getAccountSummary").
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver import MCPServer

from app.config import Settings

log = logging.getLogger(__name__)

Source = Literal["sim", "live"]


@dataclass
class ToolSpec:
    name: str
    title: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool

    def llm_schema(self, hidden_params: tuple[str, ...] = ("customer_id",)) -> dict[str, Any]:
        """OpenAI tool schema with server-injected params removed (the model never sees them)."""
        schema = json.loads(json.dumps(self.input_schema))
        props = schema.get("properties", {})
        for param in hidden_params:
            props.pop(param, None)
        schema["required"] = [r for r in schema.get("required", []) if r not in hidden_params]
        schema.pop("title", None)
        for prop in props.values():
            prop.pop("title", None)
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": schema},
        }


@dataclass
class ToolOutcome:
    tool: str
    source: str
    data: dict[str, Any]
    is_error: bool
    latency_ms: int
    fallback: bool = False
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


def _root_cause(exc: BaseException) -> str:
    """Unwrap anyio ExceptionGroups so logs say e.g. 'HTTPStatusError: 401', not 'ExceptionGroup'."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status:
        return f"{type(exc).__name__}: {status}"
    message = str(exc).strip().splitlines()[0][:80] if str(exc).strip() else ""
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _parse_result(result: Any) -> tuple[dict[str, Any], bool, str | None]:
    is_error = bool(getattr(result, "is_error", False))
    structured = getattr(result, "structured_content", None)
    text_parts = [getattr(c, "text", "") for c in (getattr(result, "content", None) or [])]
    text = "\n".join(t for t in text_parts if t)
    if is_error:
        return {"error": text or "Tool failed"}, True, text or "Tool failed"
    if isinstance(structured, dict):
        # FastMCP-style servers wrap non-object returns as {"result": ...}
        return structured, False, None
    if text:
        try:
            parsed = json.loads(text)
            return (parsed if isinstance(parsed, dict) else {"result": parsed}), False, None
        except json.JSONDecodeError:
            return {"text": text}, False, None
    return {}, False, None


class McpGateway:
    def __init__(self, settings: Settings, sim_server: MCPServer) -> None:
        self.settings = settings
        self.sim_server = sim_server
        self._specs: dict[str, ToolSpec] = {}

    # ----------------------------------------------------------- discovery
    async def load_specs(self) -> dict[str, ToolSpec]:
        """Read tool contracts from our MCP server (single source of truth)."""
        async with Client(self.sim_server) as client:
            listing = await client.list_tools()
        specs: dict[str, ToolSpec] = {}
        for tool in listing.tools:
            annotations = tool.annotations
            specs[tool.name] = ToolSpec(
                name=tool.name,
                title=tool.title or tool.name,
                description=tool.description or "",
                input_schema=tool.input_schema,
                read_only=bool(annotations and annotations.read_only_hint),
            )
        self._specs = specs
        return specs

    @property
    def specs(self) -> dict[str, ToolSpec]:
        return self._specs

    async def list_live_tools(self) -> list[str]:
        if not self.settings.live_configured:
            return []
        async with self._live_client() as client:
            listing = await client.list_tools()
        return [t.name for t in listing.tools]

    # ---------------------------------------------------------------- calls
    async def call(
        self, source: Source, name: str, arguments: dict[str, Any], sim_customer_id: str | None = None
    ) -> ToolOutcome:
        """Call a tool. On live failure (with fallback on) the simulator answers, using
        `sim_customer_id` because live and simulated customer ids can differ."""
        started = time.perf_counter()
        sim_arguments = {**arguments, "customer_id": sim_customer_id} if sim_customer_id else arguments
        if source == "live" and not self.settings.live_configured:
            if self.settings.live_fallback_to_sim:
                outcome = await self._call_sim(name, sim_arguments, started)
                outcome.fallback = True
                outcome.meta["fallback_reason"] = "LIVE_MCP_URL not configured"
                return outcome
            return ToolOutcome(
                name, "live", {"error": "Live data source not configured"}, True, 0, error="live_not_configured"
            )
        if source == "live":
            try:
                return await self._call_live(name, arguments, started)
            except Exception as exc:  # noqa: BLE001 - network/protocol errors from a remote system
                reason = _root_cause(exc)
                log.warning("Live MCP call %s failed: %s", name, reason)
                if self.settings.live_fallback_to_sim:
                    outcome = await self._call_sim(name, sim_arguments, time.perf_counter())
                    outcome.fallback = True
                    outcome.meta["fallback_reason"] = f"live call failed ({reason})"
                    return outcome
                return ToolOutcome(
                    name,
                    "live",
                    {"error": "Live system unavailable"},
                    True,
                    int((time.perf_counter() - started) * 1000),
                    error=reason,
                )
        return await self._call_sim(name, arguments, started)

    async def _call_sim(self, name: str, arguments: dict[str, Any], started: float) -> ToolOutcome:
        async with Client(self.sim_server) as client:
            result = await client.call_tool(name, arguments)
        data, is_error, error = _parse_result(result)
        return ToolOutcome(name, "sim", data, is_error, int((time.perf_counter() - started) * 1000), error=error)

    def _live_headers(self) -> dict[str, str]:
        headers = {"User-Agent": "tidelink-assist/0.1"}
        token = self.settings.live_mcp_token
        if token:
            scheme = self.settings.live_mcp_auth_scheme.strip()
            headers["Authorization"] = f"{scheme} {token}" if scheme else token
        return headers

    def _live_client(self) -> _LiveClient:
        return _LiveClient(self.settings.live_mcp_url, self._live_headers(), self.settings.live_mcp_timeout_s)

    async def _call_live(self, name: str, arguments: dict[str, Any], started: float) -> ToolOutcome:
        remote_name = self.settings.live_tool_map.get(name, name)
        async with self._live_client() as client:
            result = await client.call_tool(remote_name, arguments)
        data, is_error, error = _parse_result(result)
        outcome = ToolOutcome(name, "live", data, is_error, int((time.perf_counter() - started) * 1000), error=error)
        if remote_name != name:
            outcome.meta["remote_tool"] = remote_name
        return outcome


class _LiveClient:
    """Async context manager owning both the HTTP client and the MCP client."""

    def __init__(self, url: str, headers: dict[str, str], timeout_s: float) -> None:
        self._url = url
        self._headers = headers
        self._timeout = timeout_s
        self._http: httpx2.AsyncClient | None = None
        self._client: Client | None = None

    async def __aenter__(self) -> Client:
        self._http = httpx2.AsyncClient(
            headers=self._headers,
            timeout=httpx2.Timeout(self._timeout, read=self._timeout),
        )
        await self._http.__aenter__()
        try:
            self._client = Client(
                streamable_http_client(self._url, http_client=self._http),
                read_timeout_seconds=self._timeout,
            )
            return await self._client.__aenter__()
        except BaseException:
            await self._http.__aexit__(None, None, None)
            raise

    async def __aexit__(self, *exc: Any) -> None:
        try:
            if self._client is not None:
                await self._client.__aexit__(*exc)
        finally:
            if self._http is not None:
                await self._http.__aexit__(None, None, None)
