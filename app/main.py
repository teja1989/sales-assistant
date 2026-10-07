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
from app.config import Settings, display_url, load_settings
from app.handoff import HandoffError, HandoffSigner
from app.llm import build_llm
from app.llm.base import LlmClient
from app.logging_setup import configure_logging
from app.mcp_gateway import McpGateway, _root_cause
from app.mcp_server import build_mcp_server
from app.metrics import Metrics
from app.oauth import DemoAccount, MockIdentityProvider, OAuthError
from app.orchestrator import Orchestrator
from app.prompts import build_system_prompt
from app.scenarios import Scenario, in_demo_order, load_scenarios, match_scenario
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
    idp: MockIdentityProvider
    scenarios: dict[str, Scenario] = field(default_factory=dict)
    customers: dict[str, dict[str, Any]] = field(default_factory=dict)  # fixture by customer id


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
            "oauth_required": s.oauth_required,
            "tagline": s.tagline,
            "llm": ctx.llm.name,
            "mcp_auth_required": s.mcp_auth_required,
            "live_configured": s.live_configured,
            "live_host": urlparse(s.live_mcp_url).hostname if s.live_configured else None,
            "data_source": s.data_source,
            "version": __version__,
        }
    )


async def api_scenarios(request: Request) -> Response:
    ctx = _state(request)
    items = []
    for sc in in_demo_order(ctx.scenarios):
        view = sc.public_view()
        view["data_sources"] = {t: ctx.settings.data_source for t in _tool_names(ctx, sc)}
        if not ctx.settings.live:
            # Demo persona for the launcher: first name and plan only, never ids or contact data.
            view["persona"] = {"first_name": str(sc.customer["first_name"]), "plan": _plan_name(ctx, sc)}
        items.append(view)
    return JSONResponse({"scenarios": items})


def _tool_names(ctx: AppState, scenario: Scenario) -> list[str]:
    """Tools the assistant can use: the live server's in live mode, else the scenario's simulator tools."""
    return list(ctx.gateway.live_specs) if ctx.settings.live else list(scenario.tools)


def _plan_name(ctx: AppState, scenario: Scenario) -> str:
    return str(ctx.store.plan(str(scenario.customer["plan_id"]))["name"])


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


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    return header[7:].strip() if header.lower().startswith("bearer ") else ""


async def api_create_session(request: Request) -> Response:
    ctx = _state(request)
    # Validate the sign-in first so a bad token never burns the single-use handoff link.
    claims: dict[str, Any] | None = None
    if ctx.settings.oauth_required:
        token = _bearer(request)
        if not token:
            return JSONResponse({"error": "sign_in_required", "message": "Please sign in to continue."}, 401)
        try:
            claims = ctx.idp.verify(token)
        except OAuthError as exc:
            return JSONResponse({"error": exc.error, "message": exc.description}, exc.status)
    body: SessionRequest = await _parse(request, SessionRequest)
    try:
        handoff = ctx.signer.redeem(body.handoff_token)
    except HandoffError as exc:
        return JSONResponse({"error": "invalid_handoff", "message": str(exc)}, 401)
    scenario = ctx.scenarios.get(handoff.scenario_id)
    if scenario is None:
        return JSONResponse({"error": "unknown_scenario"}, 404)
    # The signed-in account decides whose data we see, never the URL or the scenario.
    if ctx.settings.live:
        account = str(claims["sub"]) if claims else ""
        if not account:
            return JSONResponse({"error": "sign_in_required", "message": "Please sign in to continue."}, 401)
        customer_id, live_account = account, account
    else:
        fixture = ctx.customers.get(claims["sub"]) if claims else scenario.customer
        if fixture is None:
            return JSONResponse({"error": "unknown_account"}, 401)
        customer_id, live_account = ctx.store.add_customer(fixture, clone=True), None
    session, _ = ctx.sessions.create(
        scenario=scenario,
        customer_id=customer_id,
        live_customer_id=live_account,
        search_query=handoff.search_query,
    )
    if claims:
        session.scopes = frozenset(str(claims.get("scope", "")).split())
        session.token_jti = str(claims["jti"])
    access_note = (
        "\n- Account access: the customer signed in and allowed you to view and, with their confirmation, change "
        "their account."
        if "account:manage" in session.scopes
        else "\n- Account access: VIEW ONLY. You cannot make changes; explain what could be done and how the "
        "customer can do it, or how to reconnect with permission to make changes."
    )
    session.messages.append(
        {
            "role": "system",
            "content": build_system_prompt(ctx.settings, scenario, handoff.search_query) + access_note,
        }
    )
    ctx.metrics.session_started(session.id, scenario.id)
    if ctx.settings.live:
        customer = {"first_name": "", "plan": ""}  # the assistant reads real details through the live tools
    else:
        profile = ctx.store.customer_profile(customer_id)
        customer = {"first_name": profile["first_name"], "plan": profile["plan"]["name"]}
    return JSONResponse(
        {
            "session_id": session.id,
            "scenario": {
                **scenario.public_view(),
                "data_sources": {t: ctx.settings.data_source for t in _tool_names(ctx, scenario)},
            },
            "search_query": handoff.search_query,
            "customer": customer,
            "access": {
                "connected": bool(claims),
                "can_make_changes": "account:manage" in session.scopes,
            },
        }
    )


async def api_disconnect(request: Request) -> Response:
    """Customer disconnects their account: revoke the token and drop the session and its data."""
    ctx = _state(request)
    session = ctx.sessions.remove(request.path_params["session_id"])
    if session is None:
        return JSONResponse({"disconnected": True})
    if session.token_jti:
        ctx.idp.revoke(session.token_jti)
    return JSONResponse({"disconnected": True})


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
    if session.token_jti and ctx.idp.is_revoked(session.token_jti):
        ctx.sessions.remove(session.id)
        return JSONResponse({"error": "disconnected", "message": "Your account was disconnected."}, 401)
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
    """What the live MCP server exposes and how Tidelink will use it (no account data)."""
    ctx = _state(request)
    if not ctx.settings.live_configured:
        return JSONResponse({"configured": False, "data_mode": ctx.settings.data_mode})
    try:
        specs = await ctx.gateway.refresh_live_specs(force=True)
    except Exception as exc:  # noqa: BLE001
        reason = _root_cause(exc)
        log.warning("Live MCP check failed: %s", reason)
        return JSONResponse({"configured": True, "reachable": False, "error": reason}, 502)
    return JSONResponse(
        {
            "configured": True,
            "reachable": True,
            "data_mode": ctx.settings.data_mode,
            "tools": [
                {
                    "name": s.name,
                    "kind": "read" if s.read_only else "action (needs Confirm)",
                    "account_inputs_filled_by_tidelink": list(s.hidden),
                    "inputs_from_model": sorted(set(s.input_schema.get("properties", {})) - set(s.hidden)),
                }
                for s in specs.values()
            ],
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
        idp=MockIdentityProvider(settings.oauth_signing_secret, settings.oauth_token_ttl_s),
    )

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        specs = await gateway.load_specs()
        ctx.scenarios = load_scenarios(settings.scenarios_dir, set(specs))
        accounts = []
        for scenario in in_demo_order(ctx.scenarios):
            store.add_customer(scenario.customer)  # base copies, reachable via /mcp for tooling demos
            cid = str(scenario.customer["id"])
            ctx.customers[cid] = scenario.customer
            plan = store.plan(scenario.customer["plan_id"])["name"]
            accounts.append(DemoAccount(cid, str(scenario.customer["first_name"]), plan, scenario.id, scenario.title))
        ctx.idp.set_accounts(accounts)
        ctx.idp.free_account_entry = settings.live
        if settings.live:
            try:
                live = await gateway.refresh_live_specs(force=True)
                log.info("Live MCP tools: %s", ", ".join(live) or "(none)")
            except Exception as exc:  # noqa: BLE001 - start anyway; each turn retries
                log.warning("Live MCP server not reachable at startup: %s", _root_cause(exc))
        log.info(
            "Started %s v%s: env=%s llm=%s scenarios=%s data=%s mcp_auth=%s",
            settings.app_name,
            __version__,
            settings.app_env,
            llm.name,
            ",".join(ctx.scenarios),
            f"live {display_url(settings.live_mcp_url)}" if settings.live else "sim",
            "bearer" if settings.mcp_auth_required else "OFF",
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
        Route("/api/sessions/{session_id}/disconnect", api_disconnect, methods=["POST"]),
        Route("/oauth/authorize", ctx.idp.authorize_page, methods=["GET"]),
        Route("/oauth/authorize/consent", ctx.idp.authorize_submit, methods=["POST"]),
        Route("/oauth/token", ctx.idp.token, methods=["POST"]),
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
    if settings.mcp_auth_required:
        middleware.append(Middleware(BearerTokenMiddleware, prefix="/mcp", token=settings.mcp_server_token))
    else:
        log.warning("MCP_AUTH_REQUIRED=false: /mcp accepts requests without a token (rate limited)")
    middleware += [
        Middleware(
            RateLimitMiddleware,
            per_minute=settings.rate_limit_per_minute,
            extra_prefixes=() if settings.mcp_auth_required else ("/mcp",),
        ),
        Middleware(BodySizeLimitMiddleware, max_bytes=32_768),
    ]

    async def not_found(request: Request, exc: HTTPException) -> Response:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    app = Starlette(
        routes=routes, middleware=middleware, lifespan=lifespan, exception_handlers={HTTPException: not_found}
    )
    app.state.ctx = ctx
    return app


_app: Starlette | None = None


def __getattr__(name: str) -> Any:
    # Lazy `app` so importing this module (e.g. in tests) doesn't require env config.
    # Cached: uvicorn looks the attribute up more than once, and we want exactly one app.
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(name)
