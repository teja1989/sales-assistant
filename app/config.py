"""Runtime configuration.

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
from typing import Literal
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent

LlmProvider = Literal["mock", "azure_openai", "openai_compatible", "gateway"]
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


def _mapping(value: str | None) -> dict[str, str]:
    """Parse 'a=b,c=d' into a dict."""
    result: dict[str, str] = {}
    for pair in _csv(value):
        if "=" not in pair:
            raise ConfigError(f"Expected key=value pairs, got {pair!r}")
        key, _, val = pair.partition("=")
        result[key.strip()] = val.strip()
    return result


@dataclass(frozen=True)
class Settings:
    app_env: Literal["local", "dev", "prod", "test"] = "local"
    app_name: str = "Tidelink"
    assistant_name: str = "Tidelink"
    brand_name: str = ""  # optional company name; empty keeps the provider unnamed
    tagline: str = "Always on, like the tide."
    log_level: str = "INFO"

    # LLM
    llm_provider: LlmProvider = "mock"
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_deployment: str = ""
    azure_openai_api_version: str = "2024-10-21"
    openai_compat_base_url: str = ""
    openai_compat_api_key: str = ""
    openai_compat_model: str = ""
    openai_compat_key_header: Literal["authorization", "api-key"] = "authorization"
    # gateway: one full URL that fronts Azure (e.g. an internal API gateway that adds the key itself)
    llm_gateway_url: str = ""
    llm_gateway_key_header: str = ""  # optional, e.g. Ocp-Apim-Subscription-Key; empty = send no key
    llm_gateway_key: str = ""
    llm_gateway_model: str = ""  # optional "model" field; empty when the gateway picks the deployment
    llm_temperature: float = 0.2
    llm_max_tokens: int = 700
    llm_max_tokens_param: str = "max_tokens"
    llm_timeout_s: float = 60.0
    llm_proxy_url: str = ""  # forward HTTP proxy used only for LLM calls (e.g. http://proxy.corp:8080)
    llm_ca_bundle: str = ""  # extra CA bundle (PEM) for TLS-inspecting proxies or internal gateways
    mock_stream_delay_ms: int = 12

    # Our own MCP server (/mcp)
    mcp_auth_required: bool = True  # false = /mcp open to any client (rate limited); turn on before exposing it
    mcp_server_token: str = ""
    mcp_allowed_hosts: list[str] = field(default_factory=list)

    # External ("live") MCP server
    live_mcp_url: str = ""
    live_mcp_token: str = ""
    live_mcp_auth_scheme: str = "Bearer"
    live_mcp_timeout_s: float = 20.0
    live_tool_map: dict[str, str] = field(default_factory=dict)
    live_fallback_to_sim: bool = True
    data_source_override: Literal["", "sim", "live"] = ""

    # Handoff + sessions
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
    def live_configured(self) -> bool:
        return bool(self.live_mcp_url)

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

    provider = (get("LLM_PROVIDER") or "mock").lower()
    if provider not in ("mock", "azure_openai", "openai_compatible", "gateway"):
        raise ConfigError("LLM_PROVIDER must be mock, azure_openai, openai_compatible or gateway")

    override = (get("DATA_SOURCE_OVERRIDE") or "").lower()
    if override not in ("", "sim", "live"):
        raise ConfigError("DATA_SOURCE_OVERRIDE must be empty, sim or live")

    key_header = (get("OPENAI_COMPAT_KEY_HEADER") or "authorization").lower()
    if key_header not in ("authorization", "api-key"):
        raise ConfigError("OPENAI_COMPAT_KEY_HEADER must be authorization or api-key")

    local = app_env in ("local", "test")
    mcp_auth = _bool(get("MCP_AUTH_REQUIRED"), True)
    mcp_token = (get("MCP_SERVER_TOKEN") or "") if mcp_auth else ""
    handoff_secret = get("HANDOFF_SECRET") or ""
    oauth_secret = get("OAUTH_SIGNING_SECRET") or ""
    if not local:
        missing = [
            name
            for name, value in (
                *((("MCP_SERVER_TOKEN", mcp_token),) if mcp_auth else ()),
                ("HANDOFF_SECRET", handoff_secret),
                ("OAUTH_SIGNING_SECRET", oauth_secret),
            )
            if len(value) < 32
        ]
        if missing:
            raise ConfigError(
                f"{', '.join(missing)} must be set (32+ chars) when APP_ENV={app_env}. "
                "Bind the secrets user-provided service or set the env vars."
            )
    else:
        # Local convenience: generate ephemeral secrets so the app starts, and say so.
        if mcp_auth and not mcp_token:
            mcp_token = secrets.token_urlsafe(32)
            log.warning("MCP_SERVER_TOKEN not set; generated an ephemeral token for this run")
        if not handoff_secret:
            handoff_secret = secrets.token_urlsafe(32)
        if not oauth_secret:
            oauth_secret = secrets.token_urlsafe(32)

    settings = Settings(
        app_env=app_env,  # type: ignore[arg-type]
        app_name=get("APP_NAME") or "Tidelink",
        assistant_name=get("ASSISTANT_NAME") or "Tidelink",
        brand_name=get("BRAND_NAME") or "",
        tagline=get("TAGLINE") or "Always on, like the tide.",
        log_level=(get("LOG_LEVEL") or "INFO").upper(),
        llm_provider=provider,  # type: ignore[arg-type]
        azure_openai_endpoint=(get("AZURE_OPENAI_ENDPOINT") or "").rstrip("/"),
        azure_openai_api_key=get("AZURE_OPENAI_API_KEY") or "",
        azure_openai_deployment=get("AZURE_OPENAI_DEPLOYMENT") or "",
        azure_openai_api_version=get("AZURE_OPENAI_API_VERSION") or "2024-10-21",
        openai_compat_base_url=(get("OPENAI_COMPAT_BASE_URL") or "").rstrip("/"),
        openai_compat_api_key=get("OPENAI_COMPAT_API_KEY") or "",
        openai_compat_model=get("OPENAI_COMPAT_MODEL") or "",
        openai_compat_key_header=key_header,  # type: ignore[arg-type]
        llm_gateway_url=_with_scheme((get("LLM_GATEWAY_URL") or "").strip()),
        llm_gateway_key_header=(get("LLM_GATEWAY_KEY_HEADER") or "").strip(),
        llm_gateway_key=get("LLM_GATEWAY_KEY") or "",
        llm_gateway_model=(get("LLM_GATEWAY_MODEL") or "").strip(),
        llm_temperature=_float(get("LLM_TEMPERATURE"), 0.2),
        llm_max_tokens=_int(get("LLM_MAX_TOKENS"), 700),
        llm_max_tokens_param=get("LLM_MAX_TOKENS_PARAM") or "max_tokens",
        llm_timeout_s=_float(get("LLM_TIMEOUT_S"), 60.0),
        llm_proxy_url=(get("LLM_PROXY_URL") or "").strip(),
        llm_ca_bundle=(get("LLM_CA_BUNDLE") or "").strip(),
        mock_stream_delay_ms=_int(get("MOCK_STREAM_DELAY_MS"), 12),
        mcp_auth_required=mcp_auth,
        mcp_server_token=mcp_token,
        mcp_allowed_hosts=_csv(get("MCP_ALLOWED_HOSTS")),
        live_mcp_url=(get("LIVE_MCP_URL") or "").strip(),
        live_mcp_token=get("LIVE_MCP_TOKEN") or "",
        live_mcp_auth_scheme=get("LIVE_MCP_AUTH_SCHEME", "Bearer") or "",
        live_mcp_timeout_s=_float(get("LIVE_MCP_TIMEOUT_S"), 20.0),
        live_tool_map=_mapping(get("LIVE_TOOL_MAP")),
        live_fallback_to_sim=_bool(get("LIVE_FALLBACK_TO_SIM"), True),
        data_source_override=override,  # type: ignore[arg-type]
        handoff_secret=handoff_secret,
        oauth_signing_secret=oauth_secret,
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
    _validate_llm(settings)
    return settings


def _validate_llm(s: Settings) -> None:
    if s.llm_proxy_url:
        proxy = urlsplit(s.llm_proxy_url)
        if proxy.scheme not in ("http", "https") or not proxy.hostname:
            raise ConfigError("LLM_PROXY_URL must look like http://proxy-host:port")
        if proxy.path not in ("", "/") or proxy.query:
            raise ConfigError(
                "LLM_PROXY_URL is for a forward proxy (http://host:port) and must not have a path. "
                "If this is the URL you POST chat requests to, set LLM_PROVIDER=gateway and LLM_GATEWAY_URL instead."
            )
    if s.llm_ca_bundle and not Path(s.llm_ca_bundle).is_file():
        raise ConfigError(f"LLM_CA_BUNDLE file not found: {s.llm_ca_bundle}")
    if s.llm_provider == "azure_openai":
        missing = [
            name
            for name, value in (
                ("AZURE_OPENAI_ENDPOINT", s.azure_openai_endpoint),
                ("AZURE_OPENAI_API_KEY", s.azure_openai_api_key),
                ("AZURE_OPENAI_DEPLOYMENT", s.azure_openai_deployment),
            )
            if not value
        ]
        if missing:
            raise ConfigError(f"LLM_PROVIDER=azure_openai requires {', '.join(missing)}")
        if not s.azure_openai_endpoint.startswith("https://"):
            raise ConfigError("AZURE_OPENAI_ENDPOINT must start with https://")
    elif s.llm_provider == "gateway":
        if not s.llm_gateway_url:
            raise ConfigError("LLM_PROVIDER=gateway requires LLM_GATEWAY_URL (the full URL to POST chat requests to)")
        gw = urlsplit(s.llm_gateway_url)
        if gw.scheme not in ("http", "https") or not gw.hostname:
            raise ConfigError("LLM_GATEWAY_URL must be a full URL, e.g. https://gateway.example.com/completions/api")
        if bool(s.llm_gateway_key_header) != bool(s.llm_gateway_key):
            raise ConfigError("Set both LLM_GATEWAY_KEY_HEADER and LLM_GATEWAY_KEY, or neither")
    elif s.llm_provider == "openai_compatible":
        missing = [
            name
            for name, value in (
                ("OPENAI_COMPAT_BASE_URL", s.openai_compat_base_url),
                ("OPENAI_COMPAT_API_KEY", s.openai_compat_api_key),
                ("OPENAI_COMPAT_MODEL", s.openai_compat_model),
            )
            if not value
        ]
        if missing:
            raise ConfigError(f"LLM_PROVIDER=openai_compatible requires {', '.join(missing)}")


def describe_proxy(url: str) -> str:
    """Proxy host:port for logs and status, never credentials."""
    if not url:
        return ""
    parts = urlsplit(url)
    return f"{parts.hostname}:{parts.port}" if parts.port else str(parts.hostname)


def _with_scheme(url: str) -> str:
    """Accept "host.example.com/path" for convenience: assume https."""
    if url and "://" not in url:
        return f"https://{url}"
    return url
