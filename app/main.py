"""Application entry point: web UI + chat API + MCP server, in one ASGI app.

uvicorn app.main:app --port 8000
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field, StrictBool, ValidationError
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from app import __version__
from app.config import Settings, load_settings
from app.handoff import HandoffError, HandoffSigner
from app.llm import build_llm
from app.llm.base import LlmClient
from app.logging_setup import configure_logging
from app.mcp_gateway import McpGateway, _root_cause
from app.mcp_server import build_mcp_server
from app.metrics import Metrics
from app.orchestrator import Orchestrator
from app.prompts import build_system_prompt
from app.scenarios import Scenario, load_scenarios, match_scenario
from app.security import (
    BasicAuthMiddleware,
    BearerTokenMiddleware,
    BodySizeLimitMiddleware,
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
)
from app.sessions import SessionStore
from app.sim import SimStore

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ requests
class HandoffRequest(BaseModel):
    query: str = Field(default="", max_length=200)
    scenario_id: str | None = Field(default=None, max_length=50)


class SessionRequest(BaseModel):
    handoff_token: str = Field(min_length=10, max_length=2000)


class Confirmation(BaseModel):
    action_id: str = Field(min_length=3, max_length=40)
    approved: StrictBool  # "yes"/1 must not count as consent


class TurnRequest(BaseModel):
    kickoff: bool = False
    message: str | None = None
    confirmation: Confirmation | None = None


@dataclass
class AppState:
    settings: Settings
    store: SimStore
    gateway: McpGateway
    orchestrator: Orchestrator
    sessions: SessionStore
    signer: HandoffSigner
    metrics: Metrics
    llm: LlmClient
    scenarios: dict[str, Scenario] = field(default_factory=dict)


def _state(request: Request) -> AppState:
    return request.app.state.ctx


async def _parse(request: Request, model: type[BaseModel]) -> Any:
    try:
        raw = await request.body()
        return model.model_validate_json(raw or b"{}")
    except ValidationError as exc:
        raise HTTPException(422, detail=exc.errors(include_url=False, include_input=False)[0]["msg"]) from exc
    except ValueError as exc:
        raise HTTPException(413, detail="payload too large") from exc


# -------------------------------------------------------------------- routes
async def healthz(request: Request) -> Response:
    ctx = _state(request)
    return JSONResponse({"status": "ok", "version": __version__, "scenarios": len(ctx.scenarios)})


async def api_config(request: Request) -> Response:
    ctx = _state(request)
    s = ctx.settings
    return JSONResponse(
        {
            "app_name": s.app_name,
            "assistant_name": s.assistant_name,
            "brand_name": s.brand_name,
            "llm": ctx.llm.name,
            "live_configured": s.live_configured,
            "live_host": urlparse(s.live_mcp_url).hostname if s.live_configured else None,
            "data_source_override": s.data_source_override or None,
            "version": __version__,
        }
    )


async def api_scenarios(request: Request) -> Response:
    ctx = _state(request)
    override = ctx.settings.data_source_override
    items = []
    for sc in ctx.scenarios.values():
        view = sc.public_view()
        view["data_sources"] = {t: sc.source_for(t, override) for t in sc.tools}
        items.append(view)
    return JSONResponse({"scenarios": items})


async def api_handoff(request: Request) -> Response:
    ctx = _state(request)
    body: HandoffRequest = await _parse(request, HandoffRequest)
    scenario = ctx.scenarios.get(body.scenario_id or "") if body.scenario_id else None
    if scenario is None and body.query.strip():
        scenario = match_scenario(ctx.scenarios, body.query)
    if scenario is None:
        return JSONResponse({"matched": False, "message": "No assistant flow matches that search yet."}, 404)
    query = body.query.strip() or scenario.search_query
    token = ctx.signer.issue(scenario.id, query)
    return JSONResponse(
        {"matched": True, "token": token, "scenario": scenario.public_view(), "expires_in": ctx.settings.handoff_ttl_s}
    )


async def api_create_session(request: Request) -> Response:
    ctx = _state(request)
    body: SessionRequest = await _parse(request, SessionRequest)
    try:
        handoff = ctx.signer.redeem(body.handoff_token)
    except HandoffError as exc:
        return JSONResponse({"error": "invalid_handoff", "message": str(exc)}, 401)
    scenario = ctx.scenarios.get(handoff.scenario_id)
    if scenario is None:
        return JSONResponse({"error": "unknown_scenario"}, 404)
    customer_id = ctx.store.add_customer(scenario.customer, clone=True)
    session, _ = ctx.sessions.create(
        scenario=scenario,
        customer_id=customer_id,
        live_customer_id=scenario.live_customer_id,
        search_query=handoff.search_query,
    )
    session.messages.append(
        {
            "role": "system",
            "content": build_system_prompt(ctx.settings, scenario, handoff.search_query),
        }
    )
    ctx.metrics.session_started(session.id, scenario.id)
    profile = ctx.store.customer_profile(customer_id)
    override = ctx.settings.data_source_override
    return JSONResponse(
        {
            "session_id": session.id,
            "scenario": {
                **scenario.public_view(),
                "data_sources": {t: scenario.source_for(t, override) for t in scenario.tools},
            },
            "search_query": handoff.search_query,
            "customer": {"first_name": profile["first_name"], "plan": profile["plan"]["name"]},
        }
    )


async def api_turn(request: Request) -> Response:
    ctx = _state(request)
    session = ctx.sessions.get(request.path_params["session_id"])
    if session is None:
        return JSONResponse({"error": "session_not_found", "message": "Your session expired. Please start again."}, 404)
    body: TurnRequest = await _parse(request, TurnRequest)
    message = (body.message or "").strip()
    if not (body.kickoff or message or body.confirmation):
        return JSONResponse({"error": "empty_turn"}, 422)
    if len(message) > ctx.settings.max_user_message_chars:
        return JSONResponse({"error": "message_too_long"}, 422)
    if session.busy:
        return JSONResponse({"error": "busy", "message": "Still working on your last message."}, 409)
    if body.kickoff and len(session.messages) > 1:
        return JSONResponse({"error": "already_started"}, 409)

    session.mark_busy(True)  # claim before returning, so a second request can't slip in

    async def stream() -> AsyncIterator[bytes]:
        try:
            events = ctx.orchestrator.run_turn(
                session,
                user_text=message or None,
                kickoff=body.kickoff,
                confirmation=body.confirmation.model_dump() if body.confirmation else None,
            )
            async for event in events:
                yield f"event: {event['type']}\ndata: {json.dumps(event, default=str)}\n\n".encode()
        finally:
            session.mark_busy(False)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


async def api_metrics(request: Request) -> Response:
    return JSONResponse(_state(request).metrics.summary())


async def api_metrics_reset(request: Request) -> Response:
    _state(request).metrics.reset()
    return JSONResponse({"reset": True})


async def api_live_check(request: Request) -> Response:
    ctx = _state(request)
    if not ctx.settings.live_configured:
        return JSONResponse({"configured": False})
    try:
        tools = await ctx.gateway.list_live_tools()
    except Exception as exc:  # noqa: BLE001
        reason = _root_cause(exc)
        log.warning("Live MCP check failed: %s", reason)
        return JSONResponse({"configured": True, "reachable": False, "error": reason}, 502)
    mapped = {ours: ctx.settings.live_tool_map.get(ours, ours) for ours in ctx.gateway.specs}
    return JSONResponse(
        {
            "configured": True,
            "reachable": True,
            "remote_tools": tools,
            "missing_for_our_tools": sorted(o for o, r in mapped.items() if r not in tools),
        }
    )


def _spa(static_dir: Path):
    index = static_dir / "index.html"

    async def handler(request: Request) -> Response:
        if request.url.path.startswith(("/api/", "/mcp")):
            return JSONResponse({"error": "not_found"}, status_code=404)
        if index.is_file():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return JSONResponse({"message": "Web UI not built. Run `make build-web` (or `make dev`)."}, status_code=503)

    return handler


# ----------------------------------------------------------------- factory
def create_app(settings: Settings | None = None, llm: LlmClient | None = None) -> Starlette:
    settings = settings or load_settings()
    configure_logging(settings.log_level)

    store = SimStore()
    mcp_server = build_mcp_server(store)
    security = (
        TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=settings.mcp_allowed_hosts)
        if settings.mcp_allowed_hosts
        else TransportSecuritySettings(enable_dns_rebinding_protection=False)
    )
    mcp_app = mcp_server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,  # no sticky sessions needed behind the CF router
        json_response=True,
        transport_security=security,
    )
    gateway = McpGateway(settings, mcp_server)
    metrics = Metrics()
    llm = llm or build_llm(settings)
    ctx = AppState(
        settings=settings,
        store=store,
        gateway=gateway,
        orchestrator=Orchestrator(settings, llm, gateway, metrics, store.catalog),
        sessions=SessionStore(
            settings.session_ttl_s, settings.max_sessions, on_evict=lambda old: store.remove_customer(old.customer_id)
        ),
        signer=HandoffSigner(settings.handoff_secret, settings.handoff_ttl_s),
        metrics=metrics,
        llm=llm,
    )

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        specs = await gateway.load_specs()
        ctx.scenarios = load_scenarios(settings.scenarios_dir, set(specs))
        for scenario in ctx.scenarios.values():
            store.add_customer(scenario.customer)  # base copies, reachable via /mcp for tooling demos
        log.info(
            "Started %s v%s: env=%s llm=%s scenarios=%s live_mcp=%s",
            settings.app_name,
            __version__,
            settings.app_env,
            llm.name,
            ",".join(ctx.scenarios),
            "configured" if settings.live_configured else "off",
        )
        async with mcp_server.session_manager.run():
            yield

    spa = _spa(settings.static_dir)
    routes: list[Any] = [
        Route("/healthz", healthz),
        Route("/api/config", api_config),
        Route("/api/scenarios", api_scenarios),
        Route("/api/handoff", api_handoff, methods=["POST"]),
        Route("/api/sessions", api_create_session, methods=["POST"]),
        Route("/api/sessions/{session_id}/turn", api_turn, methods=["POST"]),
        Route("/api/metrics", api_metrics),
        Route("/api/metrics/reset", api_metrics_reset, methods=["POST"]),
        Route("/api/live/check", api_live_check),
        *mcp_app.routes,  # Route("/mcp", ...) from the MCP SDK
    ]
    assets = settings.static_dir / "assets"
    if assets.is_dir():
        routes.append(Mount("/assets", app=StaticFiles(directory=assets), name="assets"))
    routes += [Route("/", spa), Route("/{page:path}", spa)]

    middleware = [Middleware(SecurityHeadersMiddleware, hsts=not settings.is_local)]
    if settings.demo_basic_auth_user and settings.demo_basic_auth_password:
        middleware.append(
            Middleware(
                BasicAuthMiddleware,
                user=settings.demo_basic_auth_user,
                password=settings.demo_basic_auth_password,
                exclude=("/healthz", "/mcp"),
            )
        )
    middleware += [
        Middleware(BearerTokenMiddleware, prefix="/mcp", token=settings.mcp_server_token),
        Middleware(RateLimitMiddleware, per_minute=settings.rate_limit_per_minute),
        Middleware(BodySizeLimitMiddleware, max_bytes=32_768),
    ]

    async def not_found(request: Request, exc: HTTPException) -> Response:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    app = Starlette(
        routes=routes, middleware=middleware, lifespan=lifespan, exception_handlers={HTTPException: not_found}
    )
    app.state.ctx = ctx
    return app


def __getattr__(name: str) -> Any:
    # Lazy `app` so importing this module (e.g. in tests) doesn't require env config.
    if name == "app":
        return create_app()
    raise AttributeError(name)
