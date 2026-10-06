# Deploying to Cloud Foundry

The app is a single Python process (uvicorn) that serves the UI, the chat API and `/mcp`.

## One-time

```bash
cf login -a <api> -o <org> -s <space>
make env          # if you don't have a .env yet
# edit .env: LLM_PROVIDER=gateway and LLM_GATEWAY_URL (or azure_openai + AZURE_OPENAI_*); LIVE_MCP_* if used
make llm-check    # confirms the model answers and can call tools
make cf-secrets   # creates the user-provided service "sales-assistant-secrets" from .env
```

`make cf-secrets` stores only secret and endpoint values (including the new `OAUTH_SIGNING_SECRET`) (see `KEYS` in `scripts/cf-secrets.sh`) and passes them through a temporary 0600 file, not the command line.

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

## Calling Azure through your gateway (recommended setup)

If you have an internal URL in front of Azure that adds the Azure key itself and picks the deployment, use gateway mode. No `AZURE_OPENAI_*` values are needed.

```bash
# .env (then: make cf-secrets && cf restage sales-assistant)
LLM_PROVIDER=gateway                                   # manifest.yml already sets this
LLM_GATEWAY_URL=https://<gateway-host>/completions/api # full URL, used exactly as given (query string kept)
```

- We POST the standard chat-completions body (`messages`, `tools`, `stream: true`) with no key and no `model`.
- If the gateway wants a key, set `LLM_GATEWAY_KEY_HEADER` (e.g. `Ocp-Apim-Subscription-Key`) and `LLM_GATEWAY_KEY`. If it wants a model name in the body, set `LLM_GATEWAY_MODEL`.
- If the gateway doesn't stream, it still works: the reply arrives all at once instead of word by word. `make llm-check` tells you which you have.
- `LLM_PROXY_URL` is something else (below). Leave it empty unless your network also requires a forward proxy to reach the gateway.

## Reaching the model through a forward proxy

Only if the app can't reach the model host directly and your network gives you a forward proxy (`http://host:port`, no path), route **only the model calls** through it:

```bash
LLM_PROXY_URL=http://proxy.corp.example:8080
```

- Only model traffic uses it. Live MCP calls and everything else go direct, so internal hosts don't get sent to the proxy by mistake. (A global `HTTPS_PROXY` would also be picked up by the model client when `LLM_PROXY_URL` is empty, but it affects every outbound call.)
- HTTPS is tunnelled with `CONNECT`: TLS still ends at the model host, so the proxy sees the host name but not keys or prompts.
- If the proxy or gateway **inspects TLS** (re-signs certificates), Python won't trust it and calls fail with "TLS certificate not trusted". Get the CA certificate (PEM) from your network team, commit it at e.g. `certs/corp-ca.pem` (it's a public certificate, not a secret) and set `LLM_CA_BUNDLE=certs/corp-ca.pem`.
- Putting a URL with a path into `LLM_PROXY_URL` is rejected with a message pointing to `LLM_GATEWAY_URL`.

Check the connection before the demo:

```bash
make llm-check          # local, with your .env
```

On Cloud Foundry, the startup log shows the model and route (e.g. `llm=gateway (direct)`), and you can run the same check inside the container if `cf ssh` is enabled in your space:

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
| App crashes at start with `ConfigError` | Missing `AZURE_OPENAI_*` or secrets shorter than 32 chars. `cf logs sales-assistant --recent`. |
| Chat shows "having trouble thinking" | Azure key, endpoint or deployment name wrong, or quota hit. Logs show `LLM HTTP <status>`. |
| UI shows "Web UI not built" | Run `make build-web` before `cf push` (or use `make cf-push`). |
| `make llm-check` says it couldn't reach the proxy | Wrong `LLM_PROXY_URL` host/port, or the proxy isn't reachable from this network. |
| "TLS certificate not trusted" | The proxy inspects TLS. Set `LLM_CA_BUNDLE` to its CA certificate (PEM). |
| HTTP 403 through the proxy | The proxy doesn't allow your Azure host, or Azure's network rules block the proxy's IP. |
| Live cards say "sim (fallback)" | `GET /api/live/check` shows reachability and missing tool names. |
