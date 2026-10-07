# Testing

```bash
make test           # Python suite (no network, no keys; uses the offline mock model)
make lint
make typecheck-web
make check          # all of the above + UI build
make smoke          # against a running app; BASE_URL=https://... for Cloud Foundry
```

## What's covered

| File | Focus |
|---|---|
| `tests/test_scenarios.py` | Plays **every** scenario YAML end to end and checks `expected:`; upsell blocked during a fault; declined actions don't run; confirmations can't be replayed; parallel sessions are isolated |
| `tests/test_guardrails.py` | Red team: a scripted "compromised" model tries fabricated offers, other customers' ids, disallowed tools, unconfirmed actions, fake prices, junk arguments, infinite loops and ordering without a preview |
| `tests/test_security.py` | `/mcp` bearer auth and the open-mode switch (rate limited), URL validation, per-process signing secrets, security headers, rate limit, body limit, basic-auth gate, handoff replay/forgery/`alg=none`/expiry, secret requirements, `VCAP_SERVICES` parsing, PII masking |
| `tests/test_sim_rules.py` | Business rules: no upsell while broken, right-sized recommendations, honest "you don't need it", credit once, idempotent orders, free mobile year on 800+, trade-in and valued-customer bonus, device orders, storm pass once |
| `tests/test_mcp_contract.py` | Tool inventory, read/action classification, `customer_id` hidden from the model, name-based lookups vs actions with overrides, many servers merged into one toolset (safe/unique names, gateway team prefixes), one server down keeps the others, calls routed to the owning server and counted, per-server tokens, live failures reported without simulated data, scenario validation |
| `tests/test_live_mode.py` | `DATA_MODE=live` end to end: account-number sign-in, live tools only, account injected even when the model names another, actions wait for Confirm then run live, outages shown as errors, personas hidden, config requirements |
| `tests/test_oauth.py` | Full authorization-code + PKCE flow; wrong verifier, code reuse, burned codes, redirect-URI mismatch, foreign redirect URIs, plain PKCE, unknown client/scope, tampered consent, cancel; sessions need valid tokens; identity from token wins; view-only blocks actions; disconnect and revocation; log masking |
| `tests/test_llm_client.py` | Azure SDK client: arguments passed to `chat.completions.create` (deployment, stream, tools), streamed chunks to events including split tool calls, error mapping without key leakage (401, timeout, certificate, connection), clear error if the SDK is missing |

## Testing with the real model

The automated suite uses the mock model so it's deterministic. Before a demo, run `make llm-check`, then each scenario once with the real model (`AZURE_OPENAI_*` set) and watch for:

- the assistant diagnosing before recommending anything;
- no offers in the outage and gateway-fault flows;
- prices only matching the offer cards (a guardrail notice means the model tried to improvise one);
- the confirm card appearing for reboot, credit and orders.

If Azure is unavailable on demo day, clear `AZURE_OPENAI_ENDPOINT` (or `make run-mock`): the flows and dashboard still work with the offline mock.

## Not automated yet

- The real `openai` SDK isn't installable in the build sandbox, so unit tests use a stand-in with the SDK's interface. `make llm-check` is the end-to-end check against the real SDK and Azure.

- Browser end-to-end tests (Playwright).
- A scored evaluation run against Azure (tool order, price accuracy, tone) across many phrasings of each search.
