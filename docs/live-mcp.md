# Live data: connecting your MCP servers

One switch, `DATA_MODE`, and **no changes on your MCP servers**: the assistant adopts them as they are.

| | `DATA_MODE=sim` (default) | `DATA_MODE=live` |
|---|---|---|
| Data | Built-in simulator | Your MCP servers only, nothing simulated |
| Tools the assistant sees | The scenario's simulator tools | **Everything your servers list**, with their names, descriptions and input schemas |
| Sign-in (still mocked) | Pick a demo persona | Type an **account number** (real or test) |
| Model | Azure OpenAI or the offline mock | Azure OpenAI (the mock only knows simulator tools) |
| If a call fails | n/a | Shown as an error; the assistant says so. Never replaced with simulated data |

## Connect one server, many servers, or an org gateway

```bash
DATA_MODE=live

# one server
LIVE_MCP_URL=https://orders.example.com/mcp
LIVE_MCP_NAME=orders            # optional label (default: the host name)
LIVE_MCP_TOKEN=<token>          # optional, sent as "Authorization: Bearer <token>"

# more servers: one line, name=url pairs; each can have its own token
LIVE_MCP_SERVERS=sales=https://sales.example.com/mcp,billing=https://billing.example.com/mcp
LIVE_MCP_TOKEN_SALES=<token>
LIVE_MCP_TOKEN_BILLING=<token>
```

On Cloud Foundry set the same names with `cf set-env sales-assistant <NAME> <value>`, then `cf restage sales-assistant`.

**Adding a system is one entry in `LIVE_MCP_SERVERS`.** On the next turn the assistant can use its tools alongside everything else; no code, no prompt changes. That's the scaling story: each team ships an MCP server, and every MCP-aware assistant can use it.

**An org gateway** (one endpoint that federates team servers, e.g. behind your API gateway) is just one server. Tools are grouped by team from the prefixes such gateways add (`sales.getOffers`, `orders__getOrder`, `billing/payBill`, `support:openTicket`); the UI shows those teams.

### How tools from many servers are combined

- Servers are listed in parallel and refreshed every 5 minutes (and after a failure). **One server being down doesn't hide the others**; it's shown as down and its tools come back when it does.
- Names are made valid for the model (letters, digits, `_`, `-`; e.g. `billing.getBill` becomes `billing_getBill`). If two servers have the same tool name, both are kept, prefixed with their server (`orders__getStatus`, `sales__getStatus`). Calls always go to the right server under the tool's real name.
- Each tool card and trace row shows which system answered (`live · billing`). The **Connected systems** panel in the chat sidebar shows every server: status, tools (lookups/actions), calls and average latency.

## Lookups vs actions (no server changes)

The tool's name decides. Names starting with a lookup verb run straight away:
`get list check search find view read fetch lookup describe query retrieve show status count estimate calculate validate verify preview compare browse inspect`
(`getAccount`, `get_customer_profile`, `sales.listOffers`, `orders__viewOrder` ...).

Everything else (`submitOrder`, `rebootGateway`, `applyCredit`, `openTicket` ...) is an **action**: the customer sees a Confirm card first, and it never runs without that click.

Override per tool when a name misleads:

```bash
LIVE_READ_TOOLS=runDiagnostics,usageReport      # treat as lookups
LIVE_ACTION_TOOLS=getAndResetPin                 # treat as actions
```

## The customer's account

Any tool input named `customer_id`, `customerId`, `account_id`, `accountId`, `account_number` or `accountNumber` is filled with the account number entered at sign-in, on every call, and hidden from the model, so it can't look up a different account. Different name on your servers? `LIVE_CUSTOMER_PARAMS=acctNo,subscriberId`.

## Check before you chat

```bash
curl -s http://localhost:8000/api/live/check | python3 -m json.tool
```

For every server: reachable or the error. For every tool: its server and real name, team, lookup or action, which inputs Tidelink fills from the account and which come from the model.

## What changes in the conversation

- The assistant follows general support rules (diagnose before selling, quote only tool data, actions need Confirm, be honest about failures) instead of the demo's simulator playbook.
- Scenario cards and searches still start conversations; demo personas and their stories are hidden.
- Code guardrails that don't depend on tool names still apply: account injection, Confirm for actions, view-only access when "make changes" isn't allowed at sign-in, the price check (dollar amounts must appear in tool results), redaction of identifiers before they reach the browser, the tool-round limit.
- Simulator-specific rules (offer shown before ordering, preview before order, no upsell during an outage) rely on simulator tool names and don't apply to your tools.

## Authentication: today and next

Today each server takes an optional static token (`LIVE_MCP_TOKEN`, `LIVE_MCP_TOKEN_<NAME>`). When your MCP servers add **client-id authentication**, it plugs in per server in one place (`LiveServer` in `app/config.py` and `_live_headers` in `app/mcp_gateway.py`): exchange the client id and secret for a token (OAuth client credentials), cache it until it expires, send it as the bearer. Nothing else in the app changes.

## Safety

- Actions run against the account typed at sign-in. Use **test accounts** until you mean to change real ones.
- The sign-in is still simulated: anyone who can open the app can type any account number. Keep live mode behind the demo password (`DEMO_BASIC_AUTH_*`) or internal access until real SSO replaces the mock.
- Tokens are sent only to their own server, never to the browser, and are masked in logs.
