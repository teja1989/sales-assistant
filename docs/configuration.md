# Configuration

Same settings locally and on Cloud Foundry. The only difference is the network path to Azure:

| | Local | Cloud Foundry |
|---|---|---|
| Where settings live | `.env` (`make env` creates it) | user-provided service `sales-assistant-secrets` (`make cf-secrets` copies them from `.env`) |
| Path to Azure OpenAI | direct | through your **proxy service**, bound to the app; it sets `HTTPS_PROXY`, which the SDK uses automatically |
| Check | `make llm-check` | `cf ssh`, then `python -m app.llm_check` (see deployment doc) |

## Settings

| Setting | What it is | Empty means |
|---|---|---|
| `AZURE_OPENAI_ENDPOINT` | `https://<resource>.openai.azure.com` (what the SDK calls `azure_endpoint`) | Offline mock model |
| `AZURE_OPENAI_API_KEY` | Key for that resource (sent by the SDK as the `api-key` header) | required with an endpoint |
| `AZURE_OPENAI_DEPLOYMENT` | Deployment name, e.g. `gpt-4.1` (not always the model name) | required with an endpoint |
| `AZURE_OPENAI_API_VERSION` | Default `2024-10-21` | default used |
| `LIVE_MCP_URL` | Your MCP server for real data, e.g. `https://api.example.com/mcp` | Simulator for every tool |
| `LIVE_MCP_TOKEN` | Token for it, sent as `Authorization: Bearer <token>` (a value with its own scheme, e.g. `Basic xyz`, is sent as given) | No token sent |

The app uses the official **Azure OpenAI Python SDK** (`openai` package, `AsyncAzureOpenAI`), which requests `{endpoint}/openai/deployments/{deployment}/chat/completions?api-version=...` with streaming and tools. `make llm-check` makes a plain call and a tool call (the assistant needs tools) and shows whether a proxy is in effect.

### The proxy on Cloud Foundry

The SDK's HTTP client honours the standard proxy variables (`HTTPS_PROXY`, `NO_PROXY`). Bind the proxy service and nothing else is needed. Two things to know:

- **`HTTPS_PROXY` applies to the app's other outbound HTTPS calls too**, including `LIVE_MCP_URL`. If your MCP server is internal and shouldn't go through the proxy, add its host to `NO_PROXY` (e.g. `cf set-env sales-assistant NO_PROXY api.internal.example`). Otherwise those calls fall back to the simulator if the proxy can't reach it.
- If the proxy re-signs TLS certificates, calls fail with "TLS certificate not trusted": point `SSL_CERT_FILE` at the CA bundle (PEM).

### How live data is used

When `LIVE_MCP_URL` is set, **every** tool call goes there first. The simulator answers instead, and the trace shows **sim (fallback)** with the reason, when the live server:
- doesn't have that tool (the tool list is cached for 5 minutes),
- can't be reached, or
- returns an error (e.g. customer not found).

Scenario files can map a demo customer to a real or test account with `live_customer_id` (see [live-mcp.md](live-mcp.md)).

## Optional knobs

You shouldn't need these for the demo. All have defaults.

| Setting | Default | Purpose |
|---|---|---|
| `LLM_TIMEOUT_S`, `LLM_TEMPERATURE`, `LLM_MAX_TOKENS`, `LLM_MAX_TOKENS_PARAM` | 60, 0.2, 700, `max_tokens` | Model request tuning (`max_completion_tokens` for reasoning models) |
| `LIVE_TOOL_MAP` | empty | Rename tools for the live server: `ours=theirs,ours2=theirs2` |
| `LIVE_MCP_TIMEOUT_S` | 20 | Live call timeout |
| `MCP_AUTH_REQUIRED`, `MCP_SERVER_TOKEN` | false, empty | Require `Authorization: Bearer <token>` on our own `/mcp` (32+ chars outside local). Turn on before exposing `/mcp` |
| `MCP_ALLOWED_HOSTS` | empty | Host-header allowlist for `/mcp` |
| `DEMO_BASIC_AUTH_USER`, `DEMO_BASIC_AUTH_PASSWORD` | empty | Password-protect the UI and API |
| `OAUTH_REQUIRED` | true | Simulated sign-in before chat |
| `APP_NAME`, `ASSISTANT_NAME`, `BRAND_NAME`, `TAGLINE` | Tidelink, Tidelink, empty, "Always on, like the tide." | Branding |
| `RATE_LIMIT_PER_MINUTE`, `SESSION_TTL_S`, `MAX_TOOL_ROUNDS` | 40, 7200, 6 | Limits |
| `APP_ENV`, `LOG_LEVEL` | local, INFO | `manifest.yml` sets `APP_ENV=dev` on Cloud Foundry |

**Signing secrets are generated at startup.** The handoff link and the simulated sign-in tokens are signed with random per-process secrets; sessions live in memory anyway, so a restart already ends them. This is also why the app runs as a single instance (`instances: 1`).

Precedence: real environment variables, then the Cloud Foundry service credentials, then `.env`. A value exported in your terminal overrides `.env`; if an edit seems ignored, check `echo $AZURE_OPENAI_ENDPOINT`.
