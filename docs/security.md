# Security

Principle: **the prompt sets behaviour; code enforces anything that matters.** Every control below holds even if the model is fully manipulated, and `tests/test_guardrails.py` proves it by replacing the model with a scripted "compromised" one.

## Controls enforced in code

| Risk | Control | Where |
|---|---|---|
| Model reads or changes another customer's account (confused deputy) | `customer_id` removed from tool schemas; always injected from the session | `mcp_gateway.ToolSpec.llm_schema`, `orchestrator._execute` |
| Model takes an action the customer didn't want | Non-read-only tools become pending actions; they run only on a separate Confirm API call the model can't make; ids are single-use | `orchestrator._handle_calls`, `_handle_confirmation` |
| Model invents an offer or discount ("free gig") | Orders accepted only for `offer_id`s returned by `get_eligible_offers` in this session; the backend re-validates eligibility | `orchestrator`, `sim.submit_order` |
| Order placed without the customer seeing the price | When a scenario has `preview_order`, `submit_upgrade_order` is refused until that exact offer was previewed in the session; the confirm card shows the quoted totals | `orchestrator._handle_calls` |
| Model invents device specs or trade-in values | Device facts and trade-in amounts come only from `get_device_offer` (catalog sourced from Apple's announcement; trade-in rules server-side); amounts are price-checked | `sim.device_offer`, `guardrails` |
| Model quotes a made-up price | Dollar amounts must match money fields in tool results, otherwise they're replaced | `guardrails.check_prices` |
| Upsell while service is broken | Offers return `blocked: true` server-side while a fault is open | `sim.offers` |
| Model calls tools it shouldn't | Per-scenario allowlist; unknown arguments dropped; malformed JSON rejected | `orchestrator` |
| Runaway tool loops / cost | `MAX_TOOL_ROUNDS` per turn | `orchestrator._agent_loop` |
| Unauthenticated MCP access | `/mcp` requires `Authorization: Bearer MCP_SERVER_TOKEN` (constant-time compare); optional Host allowlist (DNS-rebinding protection) | `security.BearerTokenMiddleware`, `MCP_ALLOWED_HOSTS` |
| Handoff link replay or forgery | HS256 JWT, pinned algorithm, `aud`/`iss`/`exp`/`jti` required, 5-minute TTL, single use; no customer data in the URL; removed from the address bar on load | `handoff.py`, `Chat.tsx` |
| XSS from model output | UI renders markdown into React elements, never `innerHTML`; strict CSP (`script-src 'self'`, `frame-ancestors 'none'`) | `markdown.tsx`, `security.py` |
| Unauthenticated account access | Chat sessions require an OAuth access token (HS256, `iss`/`aud`/`exp`/`jti` required, 30-minute TTL) from the authorization-code + PKCE flow; the token's subject decides whose account is loaded, never the URL or scenario | `oauth.py`, `main.api_create_session` |
| OAuth code theft or misuse | Registered client only; redirect URI must be same-origin `/chat/callback` (no open redirect); S256 PKCE required; codes single-use, 60-second TTL, burned even on failed exchange; consent form parameters are signed so they can't be tampered with | `oauth.py` |
| Acting beyond granted permission | `account:manage` is optional at consent; without it every action tool is refused in code and the model is told it's view-only | `orchestrator._handle_calls`, `main.api_create_session` |
| Lingering access | Disconnect revokes the token, ends the session and deletes its simulated data; revoked tokens can't open new sessions | `main.api_disconnect`, `oauth.revoke` |
| Secrets in access logs | Session ids and handoff tokens are masked in uvicorn access logs and app logs | `logging_setup.AccessLogFilter`, `mask_path` |
| Handoff token in proxy/router logs | Token travels in the URL fragment (`/chat#ctx=`), which browsers never send to servers | `Search.tsx`, `Chat.tsx` |
| Over-sharing customer data | Identifiers and contact/identity fields are stripped from tool results before they reach the browser or the model | `guardrails.redact` |
| Ambiguous consent | Confirmations require a strict boolean; `"yes"` or `1` is rejected | `main.Confirmation` |
| Stuck or concurrent turns | One turn per session; a turn lock expires after 3 minutes; expired sessions release their simulated data | `sessions.py` |
| Wrong device quoted | Device lookup refuses other variants or generations (e.g. Pro Max, iPhone 17) and flags unpriced storage | `sim._find_device`, `sim.device_offer` |
| Abuse / large payloads | Per-IP rate limit on `/api` POSTs; 32 KB body limit; 2,000-char messages | `security.py`, `main.py` |
| Secrets in source or logs | Secrets from env or a CF user-provided service; `.env` git- and cf-ignored; app refuses to start outside `local` without 32+ char secrets; logs mask emails, phones, card numbers, bearer tokens and API keys | `config.py`, `logging_setup.py` |
| Leaking internals in errors | Browser gets generic messages; provider errors are summarised without keys | `orchestrator.run_turn`, `openai_chat._safe_error` |

## Known gaps (accepted for a prototype)

- **Simulated sign-in.** The identity provider is a mock: no passwords, demo accounts only, in-memory codes and revocations. The protocol is real, so production swaps in the company IdP (and validates its tokens via JWKS) without changing the chat flow. The backing APIs must also enforce the token's subject and scopes, not just this app.
- **Session ids are bearer secrets** (random, 192-bit) held in browser memory. No CSRF token is needed because the API takes JSON bodies and no cookies, but add one if you introduce cookie auth.
- **In-memory state**: rate-limit buckets, sessions and handoff replay protection are per instance.
- **Static bearer token for `/mcp`.** Fine for a demo. For production, use OAuth 2.1 or your gateway's auth (the MCP SDK supports a token verifier).
- **Price guard is heuristic.** It catches `$` amounts, not prices written out in words.
- **Prompt injection via tool data** is mitigated (tool output is labelled as data, and every consequential action is gated in code) but not eliminated as a class.

## Before going beyond a demo

1. Put the app behind corporate SSO (CF route service) or set `DEMO_BASIC_AUTH_*`.
2. Rotate `MCP_SERVER_TOKEN` and `HANDOFF_SECRET`, and store them only in the user-provided service.
3. Keep live **action** tools on `sim` unless a test account is used.
4. Review Azure OpenAI content filters and data-retention settings for customer data.
