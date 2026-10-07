"""Runtime configuration.

The settings that matter (see .env.example); the same names locally and on Cloud Foundry:
    AZURE_OPENAI_ENDPOINT / _API_KEY / _DEPLOYMENT / _API_VERSION
                    Azure OpenAI via the official SDK (empty endpoint = offline mock model).
    DATA_MODE       sim (default: built-in simulator, demo personas) | live (your MCP servers only)
    LIVE_MCP_URL    an MCP server used when DATA_MODE=live (LIVE_MCP_TOKEN optional, sent as Bearer)
    LIVE_MCP_SERVERS more servers: name=url,name2=url2 (tokens: LIVE_MCP_TOKEN_<NAME>)
Everything else has a sensible default; the optional knobs are listed in docs/configuration.md.

Values come from (highest priority first):
1. Process environment variables.
2. Credentials of a Cloud Foundry user-provided service (VCAP_SERVICES), so
   secrets never have to live in manifest.yml or `cf set-env`.
3. A local `.env` file (development only).
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent

LlmProvider = Literal["mock", "azure_openai"]
DataSource = Literal["sim", "live"]


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _read_dotenv(path: Path) -> dict[str, str]:
    """Minimal .env parser: KEY=VALUE lines, '#' comments, optional quotes."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        else:
            # Inline comment: whitespace followed by '#'
            hash_at = next((i for i in range(1, len(value)) if value[i] == "#" and value[i - 1].isspace()), -1)
            if hash_at != -1:
                value = value[:hash_at].rstrip()
            elif value.startswith("#"):
                value = ""
        values[key] = value
    return values


def _read_vcap_credentials(service_name: str) -> dict[str, str]:
    """Return credentials of the named user-provided service, if bound."""
    raw = os.environ.get("VCAP_SERVICES")
    if not raw:
        return {}
    try:
        services = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("VCAP_SERVICES is not valid JSON; ignoring it")
        return {}
    for instances in services.values():
        for instance in instances:
            if instance.get("name") == service_name:
                creds = instance.get("credentials") or {}
                return {str(k).upper(): str(v) for k, v in creds.items()}
    return {}


def _env_source() -> dict[str, str]:
    merged = _read_dotenv(ROOT_DIR / ".env")
    service_name = os.environ.get("SECRETS_SERVICE_NAME") or merged.get(
        "SECRETS_SERVICE_NAME", "sales-assistant-secrets"
    )
    merged.update(_read_vcap_credentials(service_name))
    merged.update(os.environ)
    return merged


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _int(value: str | None, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"Expected an integer, got {value!r}") from exc


def _float(value: str | None, default: float) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ConfigError(f"Expected a number, got {value!r}") from exc


def _csv(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


@dataclass(frozen=True)
class LiveServer:
    """One MCP server the assistant can use in live mode. `token` is sent as a bearer token today;
    this is the one place to add client-id/secret authentication per server later."""

    name: str
    url: str
    token: str = ""


@dataclass(frozen=True)
class Settings:
    app_env: Literal["local", "dev", "prod", "test"] = "local"
    app_name: str = "Tidelink"
    assistant_name: str = "Tidelink"
    brand_name: str = ""  # optional company name; empty keeps the provider unnamed
    tagline: str = "Always on, like the tide."
    log_level: str = "INFO"

    # Model: Azure OpenAI through the official SDK. Empty endpoint = offline mock model.
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_deployment: str = ""
    azure_openai_api_version: str = "2024-10-21"
    # Defaults suit reasoning models (GPT-5 family): no temperature (they only accept the default),
    # max_completion_tokens with room for hidden reasoning tokens. LLM_TEMPERATURE / LLM_REASONING_EFFORT
    # are sent only when set.
    llm_temperature: float | None = None
    llm_reasoning_effort: str = ""  # e.g. low | medium | high (faster replies with "low")
    llm_max_tokens: int = 4000
    llm_max_tokens_param: str = "max_completion_tokens"
    llm_timeout_s: float = 60.0
    mock_stream_delay_ms: int = 12

    # Our own MCP server (/mcp)
    mcp_auth_required: bool = False  # true = clients must send Authorization: Bearer MCP_SERVER_TOKEN
    mcp_server_token: str = ""
    mcp_allowed_hosts: list[str] = field(default_factory=list)

    # Data: "sim" = built-in simulator and demo personas; "live" = the MCP server at LIVE_MCP_URL only.
    # In live mode the assistant uses whatever tools that server exposes, and the signed-in account
    # number is injected into any input named in live_customer_params (hidden from the model).
    data_mode: DataSource = "sim"
    live_servers: tuple[LiveServer, ...] = ()
    live_mcp_timeout_s: float = 20.0
    # Read vs action without any server change: tool names starting with a lookup verb run directly,
    # everything else waits for the customer's Confirm. Override per tool by name.
    live_read_tools: frozenset[str] = frozenset()
    live_action_tools: frozenset[str] = frozenset()
    live_customer_params: tuple[str, ...] = (
        "customer_id",
        "customerId",
        "account_id",
        "accountId",
        "account_number",
        "accountNumber",
    )

    # Handoff + sessions (signing secrets are generated per process; sessions live in memory anyway)
    handoff_secret: str = ""
    oauth_signing_secret: str = ""
    oauth_required: bool = True
    oauth_token_ttl_s: int = 1800
    handoff_ttl_s: int = 300
    session_ttl_s: int = 7200
    max_sessions: int = 500
    max_tool_rounds: int = 6
    max_user_message_chars: int = 2000

    # Access control / abuse protection
    demo_basic_auth_user: str = ""
    demo_basic_auth_password: str = ""
    rate_limit_per_minute: int = 40

    scenarios_dir: Path = ROOT_DIR / "scenarios"
    static_dir: Path = ROOT_DIR / "app" / "static"

    @property
    def llm_provider(self) -> LlmProvider:
        return "azure_openai" if self.azure_openai_endpoint else "mock"

    @property
    def live_configured(self) -> bool:
        return bool(self.live_servers)

    @property
    def live_mcp_url(self) -> str:
        """First live server's URL (kept for display and older callers)."""
        return self.live_servers[0].url if self.live_servers else ""

    @property
    def data_source(self) -> DataSource:
        """Where tool calls go (DATA_MODE)."""
        return self.data_mode

    @property
    def live(self) -> bool:
        return self.data_mode == "live"

    @property
    def is_local(self) -> bool:
        return self.app_env in ("local", "test")


def load_settings(overrides: dict[str, str] | None = None) -> Settings:
    env = _env_source()
    if overrides:
        env.update(overrides)
    get = env.get

    app_env = (get("APP_ENV") or "local").lower()
    if app_env not in ("local", "dev", "prod", "test"):
        raise ConfigError("APP_ENV must be one of local, dev, prod, test")

    mcp_auth = _bool(get("MCP_AUTH_REQUIRED"), False)
    mcp_token = (get("MCP_SERVER_TOKEN") or "") if mcp_auth else ""
    if mcp_auth:
        if app_env in ("local", "test"):
            if not mcp_token:
                mcp_token = secrets.token_urlsafe(32)
                log.warning("MCP_AUTH_REQUIRED=true but MCP_SERVER_TOKEN not set; generated one for this run")
        elif len(mcp_token) < 32:
            raise ConfigError("MCP_AUTH_REQUIRED=true needs MCP_SERVER_TOKEN (32+ chars)")

    settings = Settings(
        app_env=app_env,  # type: ignore[arg-type]
        app_name=get("APP_NAME") or "Tidelink",
        assistant_name=get("ASSISTANT_NAME") or "Tidelink",
        brand_name=get("BRAND_NAME") or "",
        tagline=get("TAGLINE") or "Always on, like the tide.",
        log_level=(get("LOG_LEVEL") or "INFO").upper(),
        azure_openai_endpoint=_with_scheme((get("AZURE_OPENAI_ENDPOINT") or "").strip()).rstrip("/"),
        azure_openai_api_key=(get("AZURE_OPENAI_API_KEY") or "").strip(),
        azure_openai_deployment=(get("AZURE_OPENAI_DEPLOYMENT") or "").strip(),
        azure_openai_api_version=(get("AZURE_OPENAI_API_VERSION") or "2024-10-21").strip(),
        llm_temperature=_float(get("LLM_TEMPERATURE"), 0.0) if get("LLM_TEMPERATURE") else None,
        llm_reasoning_effort=(get("LLM_REASONING_EFFORT") or "").strip().lower(),
        llm_max_tokens=_int(get("LLM_MAX_TOKENS"), 4000),
        llm_max_tokens_param=(get("LLM_MAX_TOKENS_PARAM") or "max_completion_tokens").strip(),
        llm_timeout_s=_float(get("LLM_TIMEOUT_S"), 60.0),
        mock_stream_delay_ms=_int(get("MOCK_STREAM_DELAY_MS"), 12),
        mcp_auth_required=mcp_auth,
        mcp_server_token=mcp_token,
        mcp_allowed_hosts=_csv(get("MCP_ALLOWED_HOSTS")),
        data_mode=_data_mode(get("DATA_MODE")),
        live_servers=_live_servers(get),
        live_mcp_timeout_s=_float(get("LIVE_MCP_TIMEOUT_S"), 20.0),
        live_read_tools=frozenset(_csv(get("LIVE_READ_TOOLS"))),
        live_action_tools=frozenset(_csv(get("LIVE_ACTION_TOOLS"))),
        live_customer_params=tuple(_csv(get("LIVE_CUSTOMER_PARAMS")))
        or Settings.__dataclass_fields__["live_customer_params"].default,
        handoff_secret=secrets.token_urlsafe(32),
        oauth_signing_secret=secrets.token_urlsafe(32),
        oauth_required=_bool(get("OAUTH_REQUIRED"), True),
        oauth_token_ttl_s=_int(get("OAUTH_TOKEN_TTL_S"), 1800),
        handoff_ttl_s=_int(get("HANDOFF_TTL_S"), 300),
        session_ttl_s=_int(get("SESSION_TTL_S"), 7200),
        max_sessions=_int(get("MAX_SESSIONS"), 500),
        max_tool_rounds=_int(get("MAX_TOOL_ROUNDS"), 6),
        max_user_message_chars=_int(get("MAX_USER_MESSAGE_CHARS"), 2000),
        demo_basic_auth_user=get("DEMO_BASIC_AUTH_USER") or "",
        demo_basic_auth_password=get("DEMO_BASIC_AUTH_PASSWORD") or "",
        rate_limit_per_minute=_int(get("RATE_LIMIT_PER_MINUTE"), 40),
        scenarios_dir=Path(get("SCENARIOS_DIR") or ROOT_DIR / "scenarios"),
        static_dir=Path(get("STATIC_DIR") or ROOT_DIR / "app" / "static"),
    )
    _validate(settings)
    return settings


def _validate(s: Settings) -> None:
    urls = [("AZURE_OPENAI_ENDPOINT", s.azure_openai_endpoint)] + [
        (f"live MCP server '{srv.name}'", srv.url) for srv in s.live_servers
    ]
    for name, url in urls:
        if url:
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise ConfigError(f"{name} must be a full URL, e.g. https://host.example.com/path")
    if s.live:
        problems = [
            msg
            for ok, msg in (
                (s.live_servers, "LIVE_MCP_URL or LIVE_MCP_SERVERS (your MCP servers)"),
                (s.azure_openai_endpoint, "AZURE_OPENAI_* (the offline mock model only knows the simulator's tools)"),
                (s.oauth_required, "OAUTH_REQUIRED=true (sign-in provides the account number)"),
            )
            if not ok
        ]
        if problems:
            raise ConfigError(f"DATA_MODE=live needs {'; '.join(problems)}")
    if s.azure_openai_endpoint:
        missing = [
            name
            for name, value in (
                ("AZURE_OPENAI_API_KEY", s.azure_openai_api_key),
                ("AZURE_OPENAI_DEPLOYMENT", s.azure_openai_deployment),
            )
            if not value
        ]
        if missing:
            raise ConfigError(f"AZURE_OPENAI_ENDPOINT is set, so {' and '.join(missing)} must be set too")


def display_url(url: str) -> str:
    """scheme://host[:port]/path for logs and checks: no credentials, no query string."""
    if not url:
        return ""
    parts = urlsplit(url)
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname}{port}{parts.path}"


def _live_servers(get: Any) -> tuple[LiveServer, ...]:
    """LIVE_MCP_URL (+ LIVE_MCP_NAME, LIVE_MCP_TOKEN) and/or LIVE_MCP_SERVERS=name=url,... with
    per-server tokens in LIVE_MCP_TOKEN_<NAME>."""
    servers: list[LiveServer] = []
    url = (get("LIVE_MCP_URL") or "").strip()
    if url:
        name = _server_name(get("LIVE_MCP_NAME") or urlsplit(_with_scheme(url)).hostname or "mcp")
        servers.append(LiveServer(name, _with_scheme(url), (get("LIVE_MCP_TOKEN") or "").strip()))
    for entry in _csv(get("LIVE_MCP_SERVERS")):
        if "=" not in entry:
            raise ConfigError(f"LIVE_MCP_SERVERS entries must be name=url, got {entry!r}")
        raw_name, _, raw_url = entry.partition("=")
        name = _server_name(raw_name)
        token = (get(f"LIVE_MCP_TOKEN_{name.upper().replace('-', '_')}") or "").strip()
        servers.append(LiveServer(name, _with_scheme(raw_url.strip()), token))
    names = [s.name for s in servers]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise ConfigError(f"Live MCP server names must be unique: {', '.join(duplicates)}")
    return tuple(servers)


def _server_name(raw: str) -> str:
    name = "".join(ch if ch.isalnum() else "-" for ch in raw.strip().lower()).strip("-")
    if not name:
        raise ConfigError(f"Invalid live MCP server name: {raw!r}")
    return name[:40]


def _data_mode(value: str | None) -> DataSource:
    mode = (value or "sim").strip().lower()
    if mode not in ("sim", "live"):
        raise ConfigError("DATA_MODE must be sim or live")
    return mode  # type: ignore[return-value]


def _with_scheme(url: str) -> str:
    """Accept "host.example.com/path" for convenience: assume https."""
    if url and "://" not in url:
        return f"https://{url}"
    return url
