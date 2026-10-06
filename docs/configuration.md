# Configuration

## The four settings

Put these in `.env` for local runs (`make env` creates it). On Cloud Foundry, `make cf-secrets` copies them into the user-provided service `sales-assistant-secrets`. Leave any of them empty and the app uses its built-in simulation for that part.

| Setting | What it is | Empty means |
|---|---|---|
| `LLM_PROXY_URL` | The full URL we POST chat requests to: your proxy in front of Azure OpenAI. Used exactly as given, query string included; `host/path` without a scheme gets `https://`. | Offline mock model (scripted flows, no network) |
| `LLM_PROXY_KEY` | Key for the proxy. `Bearer <token>` is sent as the `Authorization` header; any other value is sent as the `api-key` header. | No key sent |
| `LIVE_MCP_URL` | Your MCP server for real data, e.g. `https://api.example.com/mcp`. | Simulator for every tool |
| `LIVE_MCP_TOKEN` | Token for it, sent as `Authorization: Bearer <token>`. A value that already has a scheme (`Basic xyz`) is sent as given. | No token sent |

Check the model with `make llm-check`; check the live server with `curl -s localhost:8000/api/live/check`.

### What the app sends to `LLM_PROXY_URL`

The standard OpenAI chat-completions body: `messages`, `tools`, `tool_choice`, `stream: true`, `temperature`, `max_tokens`. No `model` field (the proxy picks the deployment). Streamed (SSE) or non-streamed JSON replies both work. Tool calling must work through the proxy: `make llm-check` tests it.

### How live data is used

When `LIVE_MCP_URL` is set, **every** tool call goes there first. The simulator answers instead, and the trace shows **sim (fallback)** with the reason, when the live server:
- doesn't have that tool (the tool list is cached for 5 minutes),
- can't be reached, or
- returns an error, e.g. the customer isn't found.

Scenario files can map a demo customer to a real or test account with `live_customer_id` (see [live-mcp.md](live-mcp.md)).

## Optional knobs

You shouldn't need these for the demo. All have defaults.

| Setting | Default | Purpose |
|---|---|---|
| `LLM_CA_BUNDLE` | empty | PEM file to trust if the proxy uses an internal certificate authority ("TLS certificate not trusted") |
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

**Signing secrets are generated at startup.** The handoff link and the simulated sign-in tokens are signed with random per-process secrets. Sessions live in memory anyway, so a restart already ends them. This is also why the app must run as a single instance (`instances: 1`).

Precedence: real environment variables, then the Cloud Foundry service credentials, then `.env`. A value exported in your terminal overrides `.env`. If an edit to `.env` seems ignored, check `echo $LLM_PROXY_URL`.
