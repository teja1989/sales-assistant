# Deploying to Cloud Foundry

The app is a single Python process (uvicorn) that serves the UI, the chat API and `/mcp`.

## One-time

```bash
cf login -a <api> -o <org> -s <space>
make env          # if you don't have a .env yet
# edit .env: LLM_PROVIDER=azure_openai and the AZURE_OPENAI_* values; LIVE_MCP_* if used
make cf-secrets   # creates the user-provided service "sales-assistant-secrets" from .env
```

`make cf-secrets` stores only secret and endpoint values (see `KEYS` in `scripts/cf-secrets.sh`) and passes them through a temporary 0600 file, not the command line.

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

## Exposing `/mcp` to other MCP clients

`/mcp` is on the same route. Clients need `Authorization: Bearer <MCP_SERVER_TOKEN>`. Set `MCP_ALLOWED_HOSTS=<your-route-host>` to turn on Host-header validation. A consumer assistant such as Muse can only reach it if the route is publicly reachable; that needs your security team's approval.

## Troubleshooting

| Symptom | Check |
|---|---|
| App crashes at start with `ConfigError` | Missing `AZURE_OPENAI_*` or secrets shorter than 32 chars. `cf logs sales-assistant --recent`. |
| Chat shows "having trouble thinking" | Azure key, endpoint or deployment name wrong, or quota hit. Logs show `LLM HTTP <status>`. |
| UI shows "Web UI not built" | Run `make build-web` before `cf push` (or use `make cf-push`). |
| Live cards say "sim (fallback)" | `GET /api/live/check` shows reachability and missing tool names. |
