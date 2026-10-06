# Connecting live data (your MCP server)

## 1. Configure

```bash
LIVE_MCP_URL=https://your-api.example.com/mcp
LIVE_MCP_TOKEN=<token>          # sent as "Authorization: Bearer <token>"
```

That's it: every flow now calls your server first. Anything it can't answer (tool missing, server unreachable, error such as customer not found) is answered by the simulator, and the tool card and trace show **sim (fallback)** with the reason. Live and simulated data are never confused.

## 2. Check connectivity and tool names

```bash
curl -s http://localhost:8000/api/live/check | python3 -m json.tool
```

The response lists the remote tools and `missing_for_our_tools`: our tool names your server doesn't have (those stay simulated).

## 3. Map tool names (only if they differ)

```bash
LIVE_TOOL_MAP=get_customer_profile=getAccountSummary,check_area_outage=getOutageStatus
```

Arguments are passed through unchanged (`customer_id` plus the tool's own fields). If your server uses different argument names, add a thin adapter tool on your server or extend `McpGateway._call_live`.

## 4. Point demo customers at real or test accounts

By default, live calls receive the demo customer's simulated id (e.g. `LUM-1001`), which your server won't know, so those calls fall back to the simulator. To query a real or test account, add it to the scenario file:

```yaml
live_customer_id: "123456789"
```

## Results format

The gateway accepts a tool result as `structuredContent` (preferred), a text block containing JSON, or plain text (wrapped as `{"text": ...}`).

## Safety notes

- **Actions go live too.** With `LIVE_MCP_URL` set, confirmed actions (reboot, order, credit) are sent to your server when it has those tools. Use **test accounts** in `live_customer_id`, or leave action tools off your server, unless you intend real changes.
- The live token is sent only to `LIVE_MCP_URL`, never to the browser, and is masked in logs.
