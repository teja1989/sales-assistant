# Live data: using your MCP server

One switch: `DATA_MODE`.

| | `DATA_MODE=sim` (default) | `DATA_MODE=live` |
|---|---|---|
| Data | Built-in simulator | Your MCP server at `LIVE_MCP_URL`, nothing simulated |
| Tools the assistant sees | The scenario's simulator tools | **Whatever your server lists**, with its names, descriptions and input schemas |
| Sign-in (still mocked) | Pick a demo persona | Type an **account number** (real or test) |
| Model | Azure OpenAI or the offline mock | Azure OpenAI (required: the mock only knows simulator tools) |
| If a call fails | n/a | Shown as an error; the assistant says so. Never replaced with simulated data |

## Turn it on

```bash
DATA_MODE=live
LIVE_MCP_URL=https://<your-api>/mcp
LIVE_MCP_TOKEN=<token>           # optional; sent as "Authorization: Bearer <token>"
# plus the AZURE_OPENAI_* values
```

On Cloud Foundry: `cf set-env sales-assistant DATA_MODE live` (and the others), then `cf restage sales-assistant`. Switch back with `DATA_MODE sim`.

Check what the app sees before chatting:

```bash
curl -s http://localhost:8000/api/live/check | python3 -m json.tool
```

It lists every tool with how Tidelink will use it: `read` or `action (needs Confirm)`, which inputs Tidelink fills from the signed-in account, and which inputs the model provides.

## What your MCP server should provide

Nothing has to match the simulator. To get the best results:

1. **Good tool descriptions.** They're the model's only guide to what each tool does and when to use it.
2. **Mark read-only tools** with the MCP annotation `readOnlyHint: true`. Any tool **without** it is treated as an action: the customer sees a Confirm card first and it never runs without that click. Unmarked lookups still work, they just ask for confirmation.
3. **Account input.** Name it one of `customer_id`, `customerId`, `account_id`, `accountId`, `account_number`, `accountNumber` (or set `LIVE_CUSTOMER_PARAMS=yourName,...`). Tidelink fills it with the signed-in account number on every call and hides it from the model, so the model can't query a different account.
4. **Results** as `structuredContent` (preferred), JSON text, or plain text. Errors with `isError: true`.

In Java/Spring AI or other SDKs, the read-only hint is set on the tool's annotations; check `/api/live/check` shows `read` for your lookups.

## What changes in the conversation

- The assistant follows general support rules (diagnose before selling, quote only tool data, actions need Confirm, be honest about failures) instead of the demo's simulator-specific playbook.
- The scenario cards and searches still start the conversation, but the demo personas and their stories are hidden.
- Code guardrails that don't depend on tool names still apply: account injection, Confirm for actions, view-only access when "make changes" isn't allowed at sign-in, the price check (dollar amounts must appear in tool results), redaction of identifiers before they reach the browser, the tool-round limit.
- Simulator-specific guardrails (offer must be shown before ordering, preview before order, no upsell during an outage) rely on simulator tool names, so they don't apply to your tools. Build equivalent rules into your server's actions if you need them.

## Safety

- Actions run against the account you typed. Use **test accounts** until you mean to change real ones.
- The sign-in is still simulated: anyone who can open the app can type any account number. Keep live mode behind the demo password (`DEMO_BASIC_AUTH_*`) or internal access only, until real SSO replaces the mock.
- The live token is sent only to `LIVE_MCP_URL`, never to the browser, and is masked in logs.
