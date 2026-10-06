# Connecting live data (external MCP server)

Some flows can use real data from your own MCP server (for example your Python API's `/mcp` endpoint) while the rest stay simulated.

## 1. Configure the endpoint

In `.env` (local) or the CF user-provided service:

```bash
LIVE_MCP_URL=https://your-api.example.com/mcp
LIVE_MCP_TOKEN=<token>
LIVE_MCP_AUTH_SCHEME=Bearer      # sends "Authorization: Bearer <token>"; set empty to send the raw token
LIVE_FALLBACK_TO_SIM=true        # recommended for demos
```

## 2. Check connectivity and tool names

```bash
curl -s http://localhost:8000/api/live/check | python3 -m json.tool
```

The response lists the remote tools and `missing_for_our_tools`: our tool names that the live server doesn't have.

## 3. Map tool names (if they differ)

```bash
LIVE_TOOL_MAP=get_customer_profile=getAccountSummary,check_area_outage=getOutageStatus
```

Arguments are passed through unchanged (`customer_id` plus the tool's own fields). If your server uses different argument names, either add a thin adapter tool on your server or extend `McpGateway._call_live`.

## 4. Choose which flows use live data

Per scenario:

```yaml
data_sources:
  get_customer_profile: live
  check_area_outage: live
live_customer_id: "123456789"   # the real/test account to query
```

Or force everything: `DATA_SOURCE_OVERRIDE=live`.

## What the demo shows

Every tool card and the MCP trace show a **sim** or **live** badge. If a live call fails and fallback is on, the card reads **sim (fallback)** and the dashboard counts it under sim, so live and simulated data are never confused.

## Results format

The gateway accepts a tool result as `structuredContent` (preferred), or a text block containing JSON, or plain text (wrapped as `{"text": ...}`). Tool errors (`isError: true`) are shown to the model as errors and counted on the dashboard.

## Safety notes for live data

- Use a **test account** for demos. Simulated actions are harmless; live action tools (reboot, order) would change a real account. Keep action tools on `sim` unless you intend that.
- The live token is sent only to `LIVE_MCP_URL`, never to the browser, and is masked in logs.
