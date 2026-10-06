# Configuration

Same settings locally and on Cloud Foundry; the app calls Azure OpenAI directly in both.

| | Local | Cloud Foundry |
|---|---|---|
| Where settings live | `.env` (`make env` creates it) | app env vars: `cf set-env sales-assistant <NAME> <value>` |
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

The app uses the official **Azure OpenAI Python SDK** (`openai` package, `AsyncAzureOpenAI`), which requests `{endpoint}/openai/deployments/{deployment}/chat/completions?api-version=...` with streaming and tools. `make llm-check` makes a plain call and a tool call (the assistant needs tools).

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
| `LLM_TEMPERATURE` | not sent | Reasoning models (GPT-5 family) only accept the default; set it (e.g. `0.2`) only for older models like gpt-4.1 |
| `LLM_REASONING_EFFORT` | not sent | `low` / `medium` / `high` for reasoning models; `low` gives faster chat replies |
| `LLM_MAX_TOKENS`, `LLM_MAX_TOKENS_PARAM` | 4000, `max_completion_tokens` | Includes hidden reasoning tokens, so keep it generous. Use `max_tokens` only for old models/API versions that reject `max_completion_tokens` |
| `LLM_TIMEOUT_S` | 60 | Request timeout |
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
