"""Routes tool calls to the MCP servers for the current DATA_MODE.

Both paths speak real MCP through the official SDK client:
* sim  -> `Client(MCPServer)`: in-process MCP dispatch to our own simulator.
* live -> `Client(streamable_http_client(url))`: one or many MCP servers (LIVE_MCP_URL /
  LIVE_MCP_SERVERS), each with its own optional bearer token.

Live mode adopts the servers as they are; nothing on them changes for this assistant:
* tools are whatever each server lists (name, description, input schema), discovered in parallel
  and refreshed every few minutes; one server being down doesn't hide the others' tools;
* names are made safe for the model (letters, digits, _ and -) and unique across servers;
* tools are grouped by team from name prefixes a gateway adds ("sales.getOffers",
  "orders__getOrder"), otherwise by server;
* read vs action comes from the tool name (lookup verbs run directly; anything else needs the
  customer's Confirm), with LIVE_READ_TOOLS / LIVE_ACTION_TOOLS overrides.
There is no fallback to simulated data.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver import MCPServer

from app.config import LiveServer, Settings

log = logging.getLogger(__name__)

LIVE_TOOLS_TTL_S = 300

# First word of a tool name that marks a lookup. Anything else is treated as an action (Confirm).
READ_VERBS = frozenset(
    "get list check search find view read fetch lookup describe query retrieve show status "
    "count estimate calculate validate verify preview compare browse inspect".split()
)
_GROUP_SEPARATORS = re.compile(r"\.|__|/|:")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_-]")

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
    server: str = "sim"  # which MCP server answers this tool
    remote_name: str = ""  # the tool's name on that server (may differ from `name`)
    group: str = ""  # team/domain shown in the UI (from a gateway prefix, else the server name)

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
        self._servers = {s.name: s for s in settings.live_servers}
        self._server_tools: dict[str, list[ToolSpec]] = {}  # last good listing per server
        self.server_status: dict[str, dict[str, Any]] = {
            s.name: {"reachable": None, "tools": 0, "error": None, "calls": 0, "errors": 0, "total_ms": 0}
            for s in settings.live_servers
        }

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
        specs = await self.refresh_live_specs(force=True)
        return [s.remote_name for s in specs.values()]

    async def refresh_live_specs(self, force: bool = False) -> dict[str, ToolSpec]:
        """Discover every live server's tools in parallel (cached for a few minutes). A server that
        fails keeps its last good tool list; raises only if no server has ever answered."""
        now = time.monotonic()
        if not force and self._live_specs and now - self._live_specs_at < LIVE_TOOLS_TTL_S:
            return self._live_specs
        servers = list(self._servers.values())
        results = await asyncio.gather(*(self._list_server(s) for s in servers), return_exceptions=True)
        errors: list[str] = []
        for server, result in zip(servers, results, strict=True):
            status = self.server_status[server.name]
            if isinstance(result, BaseException):
                status.update(reachable=False, error=_root_cause(result))
                errors.append(f"{server.name}: {status['error']}")
                log.warning("Live MCP server %s not reachable: %s", server.name, status["error"])
            else:
                self._server_tools[server.name] = result
                status.update(reachable=True, error=None, tools=len(result))
        if not self._server_tools:
            raise ConnectionError("; ".join(errors) or "no live MCP servers configured")
        self._live_specs = self._merge(self._server_tools)
        self._live_specs_at = now
        return self._live_specs

    async def _list_server(self, server: LiveServer) -> list[ToolSpec]:
        async with self._live_client(server) as client:
            listing = await client.list_tools()
        return [self._live_spec(server.name, tool) for tool in listing.tools]

    def _merge(self, per_server: dict[str, list[ToolSpec]]) -> dict[str, ToolSpec]:
        """Expose all servers' tools under names that are valid for the model and unique."""
        counts: dict[str, int] = {}
        for specs in per_server.values():
            for spec in specs:
                counts[spec.name] = counts.get(spec.name, 0) + 1
        merged: dict[str, ToolSpec] = {}
        for server, specs in per_server.items():
            for spec in specs:
                name = spec.name if counts[spec.name] == 1 else _safe_name(f"{server}__{spec.name}")
                while name in merged:  # still clashing after prefixing: number it
                    name = _safe_name(f"{name}_{len(merged)}")
                merged[name] = ToolSpec(**{**spec.__dict__, "name": name})
        return merged

    def _live_spec(self, server: str, tool: Any) -> ToolSpec:
        schema = tool.input_schema if isinstance(tool.input_schema, dict) else {"type": "object", "properties": {}}
        props = schema.get("properties") or {}
        group, base = _split_group(tool.name)
        return ToolSpec(
            name=_safe_name(tool.name),
            title=tool.title or _humanize(base),
            description=tool.description or "",
            input_schema=schema,
            read_only=self._is_read(tool.name, base),
            hidden=tuple(p for p in self.settings.live_customer_params if p in props),
            server=server,
            remote_name=tool.name,
            group=group or server,
        )

    def _is_read(self, full_name: str, base: str) -> bool:
        if full_name in self.settings.live_read_tools or base in self.settings.live_read_tools:
            return True
        if full_name in self.settings.live_action_tools or base in self.settings.live_action_tools:
            return False
        words = _words(base)
        return bool(words) and words[0] in READ_VERBS

    @property
    def live_specs(self) -> dict[str, ToolSpec]:
        return self._live_specs

    # ---------------------------------------------------------------- calls
    async def call(self, source: Source, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        """Call a tool on the simulator or the live server that owns it. Live failures are reported
        as tool errors (shown to the customer honestly), never replaced with simulated data."""
        started = time.perf_counter()
        if source != "live":
            return await self._call_sim(name, arguments, started)
        spec = self._live_specs.get(name)
        if spec is None:
            return ToolOutcome(name, "live", {"error": f"Tool '{name}' is not available."}, True, 0, error="unknown")
        status = self.server_status[spec.server]
        try:
            outcome = await self._call_live(spec, arguments, started)
        except Exception as exc:  # noqa: BLE001 - network/protocol errors from a remote system
            reason = _root_cause(exc)
            log.warning("Live MCP call %s on %s failed: %s", spec.remote_name, spec.server, reason)
            self._live_specs_at = 0.0  # re-list next turn; the server may have changed or restarted
            status.update(reachable=False, error=reason)
            outcome = ToolOutcome(
                name,
                "live",
                {"error": f"The {spec.group} system is unavailable right now."},
                True,
                int((time.perf_counter() - started) * 1000),
                error=reason,
            )
        status["calls"] += 1
        status["total_ms"] += outcome.latency_ms
        status["errors"] += int(outcome.is_error)
        outcome.meta.update(server=spec.server, group=spec.group)
        return outcome

    def systems(self) -> list[dict[str, Any]]:
        """Per-server view for the UI: status, tools by group, call counts and latency (no secrets)."""
        out = []
        for name, server in self._servers.items():
            st = self.server_status[name]
            tools = [s for s in self._live_specs.values() if s.server == name]
            out.append(
                {
                    "name": name,
                    "host": urlsplit_host(server.url),
                    "reachable": st["reachable"],
                    "error": st["error"],
                    "tools": [
                        {"name": s.name, "group": s.group, "kind": "read" if s.read_only else "action"} for s in tools
                    ],
                    "groups": sorted({s.group for s in tools}),
                    "calls": st["calls"],
                    "errors": st["errors"],
                    "avg_ms": round(st["total_ms"] / st["calls"]) if st["calls"] else None,
                }
            )
        return out

    async def _call_sim(self, name: str, arguments: dict[str, Any], started: float) -> ToolOutcome:
        async with Client(self.sim_server) as client:
            result = await client.call_tool(name, arguments)
        data, is_error, error = _parse_result(result)
        return ToolOutcome(name, "sim", data, is_error, int((time.perf_counter() - started) * 1000), error=error)

    @staticmethod
    def _live_headers(server: LiveServer) -> dict[str, str]:
        headers = {"User-Agent": "tidelink-assist/0.1"}
        token = server.token
        if token:
            # "Bearer x" / "Basic x" are sent as given; a bare token gets "Bearer ".
            has_scheme = " " in token and token.split(" ", 1)[0].isalpha()
            headers["Authorization"] = token if has_scheme else f"Bearer {token}"
        return headers

    def _live_client(self, server: LiveServer) -> _LiveClient:
        return _LiveClient(server.url, self._live_headers(server), self.settings.live_mcp_timeout_s)

    async def _call_live(self, spec: ToolSpec, arguments: dict[str, Any], started: float) -> ToolOutcome:
        async with self._live_client(self._servers[spec.server]) as client:
            result = await client.call_tool(spec.remote_name, arguments)
        data, is_error, error = _parse_result(result)
        return ToolOutcome(spec.name, "live", data, is_error, int((time.perf_counter() - started) * 1000), error=error)


def _split_group(name: str) -> tuple[str, str]:
    """'sales.getOffers' -> ('sales', 'getOffers'); 'getOffers' -> ('', 'getOffers')."""
    parts = _GROUP_SEPARATORS.split(name, maxsplit=1)
    if len(parts) == 2 and parts[0] and parts[1]:
        return parts[0], parts[1]
    return "", name


def _words(name: str) -> list[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return [w.lower() for w in re.split(r"[^A-Za-z0-9]+", spaced) if w]


def _humanize(name: str) -> str:
    words = _words(name)
    return " ".join(words).capitalize() if words else name


def _safe_name(name: str) -> str:
    """OpenAI function names: ^[a-zA-Z0-9_-]{1,64}$."""
    return (_SAFE_NAME.sub("_", name) or "tool")[:64]


def urlsplit_host(url: str) -> str:
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    return f"{parts.hostname}:{parts.port}" if parts.port else str(parts.hostname)


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
