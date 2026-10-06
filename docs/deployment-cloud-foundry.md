# Deploying to Cloud Foundry

The app is a single Python process (uvicorn) that serves the UI, the chat API and `/mcp`.

## One-time

```bash
cf login -a <api> -o <org> -s <space>
make llm-check    # optional: confirm the same Azure values work locally first

# Create the app without starting it, then set its values (they survive later pushes)
cf push sales-assistant --no-start -f manifest.yml
cf set-env sales-assistant AZURE_OPENAI_ENDPOINT    https://<resource>.openai.azure.com
cf set-env sales-assistant AZURE_OPENAI_API_KEY     <key>
cf set-env sales-assistant AZURE_OPENAI_DEPLOYMENT  gpt-4.1
cf set-env sales-assistant AZURE_OPENAI_API_VERSION 2024-10-21
# optional live data:
cf set-env sales-assistant LIVE_MCP_URL   https://<api-host>/mcp
cf set-env sales-assistant LIVE_MCP_TOKEN <token>

cf bind-service sales-assistant <your-proxy-service>   # sets HTTPS_PROXY for Azure calls
```

Keep these values out of `manifest.yml` (it's in git). Values set with `cf set-env` are kept across `cf push`; values in the manifest's `env:` would override them. Note that anyone with developer access to the space can read them with `cf env`.

## Every deploy

```bash
make cf-push      # builds the UI into app/static, then cf push
BASE_URL=https://<route> make smoke
```

The Python buildpack doesn't run npm, which is why the UI is built locally first. `.cfignore` excludes `web/`, tests, docs and `.env` from the upload.

To change a value: `cf set-env sales-assistant <NAME> <value>`, then `cf restage sales-assistant`.

## Configuration

- **Azure, live MCP and other settings:** app env vars (`cf set-env`). Non-secret defaults are under `env:` in `manifest.yml`.
- **Proxy:** comes from the bound proxy service (`HTTPS_PROXY`); no app setting.
- Precedence: env vars, then credentials of a bound user-provided service named `sales-assistant-secrets` (optional, if you prefer that route), then `.env` (local only; not uploaded).

## Why these manifest settings

| Setting | Reason |
|---|---|
| `instances: 1` | Sessions and metrics are in memory. Scale out only after moving them to Redis. |
| `health-check-type: http` on `/healthz` | `/healthz` is excluded from basic auth and rate limits. |
| `--proxy-headers --forwarded-allow-ips='*'` | Trust the CF router's `X-Forwarded-*` headers (client IP for rate limiting, https scheme). |
| `--timeout-keep-alive 75` | Above the router's keep-alive, avoiding spurious 502s. |
| `APP_ENV: dev` | Enforces 32+ char secrets and turns on HSTS. |

Streaming (SSE) works through the CF router. The app sends `Cache-Control: no-cache` and `X-Accel-Buffering: no` so intermediaries don't buffer the stream.

## The proxy service

On Cloud Foundry, Azure OpenAI is reached through your proxy service. Bind it to the app, either under `services:` in `manifest.yml` (there's a commented placeholder) or with:

```bash
cf bind-service sales-assistant <your-proxy-service>
cf restage sales-assistant
```

It sets `HTTPS_PROXY`, which the Azure OpenAI SDK uses automatically; there's no app setting for it. The same Azure values as local come from the secrets service. `HTTPS_PROXY` also applies to `LIVE_MCP_URL`; if your MCP server is internal, exclude it with `cf set-env sales-assistant NO_PROXY <mcp-host>` (see [configuration.md](configuration.md)).

Check the connection before the demo:

```bash
make llm-check          # local, with your .env (direct to Azure)
```

On Cloud Foundry, the startup log shows `llm=azure_openai:<deployment>` (or `mock`) and the live MCP URL, and you can run the same check inside the container if `cf ssh` is enabled in your space:

```bash
cf ssh sales-assistant
/tmp/lifecycle/shell          # loads the app's environment (Python, VCAP_SERVICES) and cds into the app
python -m app.llm_check
```

It prints the endpoint, deployment, the proxy in effect (it should say `HTTPS_PROXY <host>` on CF), and whether a plain call and a tool call work, or the failure with a hint.

## Exposing `/mcp` to other MCP clients

`/mcp` is on the same route. **Auth is currently off** (`MCP_AUTH_REQUIRED: "false"` in `manifest.yml`), so any MCP client that can reach the route can connect without a header; requests are rate limited per IP. To turn it on: set `MCP_AUTH_REQUIRED: "true"`, `cf set-env sales-assistant MCP_SERVER_TOKEN <32+ chars>`, `cf push`. Clients then send `Authorization: Bearer <MCP_SERVER_TOKEN>`. Set `MCP_ALLOWED_HOSTS=<your-route-host>` to turn on Host-header validation. A consumer assistant such as Muse can only reach it if the route is publicly reachable; that needs your security team's approval.

## Troubleshooting

| Symptom | Check |
|---|---|
| App crashes at start with `ConfigError` | A URL setting isn't a full URL, or `MCP_AUTH_REQUIRED=true` without a 32+ char `MCP_SERVER_TOKEN`. `cf logs sales-assistant --recent`. |
| Chat shows "having trouble thinking" | Azure settings wrong, proxy not bound, or quota hit. Logs show `LLM HTTP <status>`; run the check. |
| UI shows "Web UI not built" | Run `make build-web` before `cf push` (or use `make cf-push`). |
| "TLS certificate not trusted" | The proxy re-signs TLS. Set `SSL_CERT_FILE` to its CA bundle (PEM). |
| `LLM HTTP 401` | Wrong `AZURE_OPENAI_API_KEY` for this endpoint. |
| `LLM HTTP 404` | Wrong `AZURE_OPENAI_DEPLOYMENT` (deployment name, not model) or API version. |
| Works locally, `connection error` on CF | Proxy service not bound (`llm_check` shows `Proxy: none`), or it doesn't allow your Azure host. |
| Live cards say "sim (fallback)" | `GET /api/live/check` shows reachability and missing tool names. |
