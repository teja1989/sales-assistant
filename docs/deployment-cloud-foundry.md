# Deploying to Cloud Foundry

The app is a single Python process (uvicorn) that serves the UI, the chat API and `/mcp`.

## One-time

```bash
cf login -a <api> -o <org> -s <space>
make env          # if you don't have a .env yet
# edit .env: LLM_PROXY_URL (+ LLM_PROXY_KEY), and LIVE_MCP_URL (+ LIVE_MCP_TOKEN) if used
make llm-check    # confirms the proxy answers and can call tools
make cf-secrets   # creates the user-provided service "sales-assistant-secrets" from .env
```

`make cf-secrets` stores the four values (plus `MCP_SERVER_TOKEN` and `LIVE_TOOL_MAP` if you set them; see `KEYS` in `scripts/cf-secrets.sh`) and passes them through a temporary 0600 file, not the command line. Signing secrets are generated at startup, so there's nothing else to store.

## Every deploy

```bash
make cf-push      # builds the UI into app/static, then cf push
BASE_URL=https://<route> make smoke
```

The Python buildpack doesn't run npm, which is why the UI is built locally first. `.cfignore` excludes `web/`, tests, docs and `.env` from the upload.

## Configuration

- **Secrets and endpoints:** user-provided service credentials. The app reads `VCAP_SERVICES` and treats each credential key as an environment variable (keys are upper-cased).
- **Non-secret settings:** `env:` in `manifest.yml`, or `cf set-env`.
- Precedence: real environment variables, then the service credentials, then `.env`.

To change a secret: edit `.env`, `make cf-secrets`, then `cf restage sales-assistant`.

## Why these manifest settings

| Setting | Reason |
|---|---|
| `instances: 1` | Sessions and metrics are in memory. Scale out only after moving them to Redis. |
| `health-check-type: http` on `/healthz` | `/healthz` is excluded from basic auth and rate limits. |
| `--proxy-headers --forwarded-allow-ips='*'` | Trust the CF router's `X-Forwarded-*` headers (client IP for rate limiting, https scheme). |
| `--timeout-keep-alive 75` | Above the router's keep-alive, avoiding spurious 502s. |
| `APP_ENV: dev` | Enforces 32+ char secrets and turns on HSTS. |

Streaming (SSE) works through the CF router. The app sends `Cache-Control: no-cache` and `X-Accel-Buffering: no` so intermediaries don't buffer the stream.

## The model proxy

`LLM_PROXY_URL` is the full URL we POST chat requests to; `LLM_PROXY_KEY` is its key (see [configuration.md](configuration.md) for the header rule). If the proxy uses an internal certificate authority, calls fail with "TLS certificate not trusted": get the CA certificate (PEM) from your network team, commit it at e.g. `certs/corp-ca.pem` (a public certificate, not a secret) and set `LLM_CA_BUNDLE=certs/corp-ca.pem`.

Check the connection before the demo:

```bash
make llm-check          # local, with your .env
```

On Cloud Foundry, the startup log shows `llm=proxy` (or `mock`) and the live MCP URL, and you can run the same check inside the container if `cf ssh` is enabled in your space:

```bash
cf ssh sales-assistant
/tmp/lifecycle/shell          # loads the app's environment (Python, VCAP_SERVICES) and cds into the app
python -m app.llm_check
```

It prints the target, route, latency, whether replies stream, and whether tool calling works (the assistant depends on it), or the failure with a hint.

## Exposing `/mcp` to other MCP clients

`/mcp` is on the same route. **Auth is currently off** (`MCP_AUTH_REQUIRED: "false"` in `manifest.yml`), so any MCP client that can reach the route can connect without a header; requests are rate limited per IP. To turn it on: set `MCP_AUTH_REQUIRED: "true"`, put a 32+ char `MCP_SERVER_TOKEN` in `.env`, `make cf-secrets`, `cf push`. Clients then send `Authorization: Bearer <MCP_SERVER_TOKEN>`. Set `MCP_ALLOWED_HOSTS=<your-route-host>` to turn on Host-header validation. A consumer assistant such as Muse can only reach it if the route is publicly reachable; that needs your security team's approval.

## Troubleshooting

| Symptom | Check |
|---|---|
| App crashes at start with `ConfigError` | A URL setting isn't a full URL, or `MCP_AUTH_REQUIRED=true` without a 32+ char `MCP_SERVER_TOKEN`. `cf logs sales-assistant --recent`. |
| Chat shows "having trouble thinking" | Proxy URL or key wrong, or quota hit. Logs show `LLM HTTP <status>`; run `make llm-check`. |
| UI shows "Web UI not built" | Run `make build-web` before `cf push` (or use `make cf-push`). |
| "TLS certificate not trusted" | The proxy inspects TLS. Set `LLM_CA_BUNDLE` to its CA certificate (PEM). |
| `LLM HTTP 401/403` | The proxy rejected the key: check `LLM_PROXY_KEY` (plain value = `api-key` header, `Bearer x` = `Authorization`). |
| Live cards say "sim (fallback)" | `GET /api/live/check` shows reachability and missing tool names. |
