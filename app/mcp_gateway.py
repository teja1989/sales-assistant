"""Routes tool calls to the MCP server for the current DATA_MODE.

Both paths speak real MCP through the official SDK client:
* sim  -> `Client(MCPServer)`: in-process MCP dispatch to our own simulator.
* live -> `Client(streamable_http_client(url))`: your MCP server, with an Authorization header
  from LIVE_MCP_TOKEN ("Bearer <token>" unless it already has a scheme).

In live mode the tools are whatever that server lists (name, description, input schema,
readOnlyHint), refreshed every few minutes. There is no fallback to simulated data.
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

LIVE_TOOLS_TTL_S = 300

Source = Literal["sim", "live"]


@dataclass
class ToolSpec:
    name: str
    title: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool
    # Inputs filled by the server from the signed-in account; removed from what the model sees.
    hidden: tuple[str, ...] = ("customer_id",)

    def llm_schema(self) -> dict[str, Any]:
        """OpenAI tool schema with server-injected params removed (the model never sees them)."""
        schema = json.loads(json.dumps(self.input_schema))
        schema.setdefault("type", "object")
        props = schema.setdefault("properties", {})
        hidden_params = self.hidden
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
        self._live_specs: dict[str, ToolSpec] = {}
        self._live_specs_at = 0.0

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

    async def refresh_live_specs(self, force: bool = False) -> dict[str, ToolSpec]:
        """Discover the live server's tools (cached for a few minutes). On failure keep the last list."""
        now = time.monotonic()
        if not force and self._live_specs and now - self._live_specs_at < LIVE_TOOLS_TTL_S:
            return self._live_specs
        async with self._live_client() as client:
            listing = await client.list_tools()
        self._live_specs = {t.name: self._live_spec(t) for t in listing.tools}
        self._live_specs_at = now
        return self._live_specs

    def _live_spec(self, tool: Any) -> ToolSpec:
        schema = tool.input_schema if isinstance(tool.input_schema, dict) else {"type": "object", "properties": {}}
        props = schema.get("properties") or {}
        annotations = tool.annotations
        return ToolSpec(
            name=tool.name,
            title=(annotations.title if annotations and annotations.title else None) or tool.title or tool.name,
            description=tool.description or "",
            input_schema=schema,
            # Unmarked tools are treated as actions: they need the customer's Confirm.
            read_only=bool(annotations and annotations.read_only_hint),
            hidden=tuple(p for p in self.settings.live_customer_params if p in props),
        )

    @property
    def live_specs(self) -> dict[str, ToolSpec]:
        return self._live_specs

    # ---------------------------------------------------------------- calls
    async def call(self, source: Source, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        """Call a tool on the simulator or the live server. Live failures are reported as tool
        errors (shown to the customer honestly), never replaced with simulated data."""
        started = time.perf_counter()
        if source != "live":
            return await self._call_sim(name, arguments, started)
        try:
            return await self._call_live(name, arguments, started)
        except Exception as exc:  # noqa: BLE001 - network/protocol errors from a remote system
            reason = _root_cause(exc)
            log.warning("Live MCP call %s failed: %s", name, reason)
            self._live_specs_at = 0.0  # re-list next turn; the server may have changed or restarted
            return ToolOutcome(
                name,
                "live",
                {"error": "The account system is unavailable right now."},
                True,
                int((time.perf_counter() - started) * 1000),
                error=reason,
            )

    async def _call_sim(self, name: str, arguments: dict[str, Any], started: float) -> ToolOutcome:
        async with Client(self.sim_server) as client:
            result = await client.call_tool(name, arguments)
        data, is_error, error = _parse_result(result)
        return ToolOutcome(name, "sim", data, is_error, int((time.perf_counter() - started) * 1000), error=error)

    def _live_headers(self) -> dict[str, str]:
        headers = {"User-Agent": "tidelink-assist/0.1"}
        token = self.settings.live_mcp_token
        if token:
            # "Bearer x" / "Basic x" are sent as given; a bare token gets "Bearer ".
            has_scheme = " " in token and token.split(" ", 1)[0].isalpha()
            headers["Authorization"] = token if has_scheme else f"Bearer {token}"
        return headers

    def _live_client(self) -> _LiveClient:
        return _LiveClient(self.settings.live_mcp_url, self._live_headers(), self.settings.live_mcp_timeout_s)

    async def _call_live(self, name: str, arguments: dict[str, Any], started: float) -> ToolOutcome:
        async with self._live_client() as client:
            result = await client.call_tool(name, arguments)
        data, is_error, error = _parse_result(result)
        return ToolOutcome(name, "live", data, is_error, int((time.perf_counter() - started) * 1000), error=error)


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
