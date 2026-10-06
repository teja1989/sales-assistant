# Tidelink

A working prototype of an **MCP-connected chat assistant** for a home-internet and mobile provider. A customer searches in an external AI assistant, lands in our chat with their question already known, and the assistant uses MCP tools to **fix the problem first, then recommend an upgrade only when the data supports it**.

**Tidelink** is the name of the chat assistant; the provider itself stays unnamed and products use generic names. All customer, network, pricing, trade-in and weather data is simulated unless you connect a live MCP server. iPhone 18 Pro facts (starting price, storage, chip, camera, battery, colors, availability) come from Apple's September 2026 announcement.

## What it shows

| Scenario | What happens | Business outcome |
|---|---|---|
| Internet keeps dropping | Diagnostics find a gateway fault; remote reboot after the customer confirms | Truck roll avoided, no upsell while broken |
| Internet is down | Area outage found; ETA explained; goodwill credit applied on confirm | Contained without an agent, no pointless reboot |
| I want faster internet | Healthy line, usage at 97% of plan; right-sized upgrade on confirm | Data-backed upsell, incremental revenue |
| Slow upstairs | Plan has headroom, Wi-Fi is weak; mesh pod instead of a pricier plan | Honest sale that actually fixes the issue |
| Online games lag every evening | Gamer maxing out Plus 500; Turbo 800 at the same price for 12 months plus a free year of mobile; preview, one-tap order | Internet upsell + new mobile line |
| 4K streams buffer at night | Five 4K TVs saturate Gigabit; Ultra 2 Gig with the free mobile year | Higher-tier upsell + bundle |
| Storm coming | Severe storm forecast; free 48-hour unlimited mobile data in one tap; "stay safe" | Proactive care, loyalty |
| Looking at the iPhone 18 Pro | Verified device facts + personalized trade-in ($800 usual + $200 valued-customer bonus); preview, one-tap order | Device sale |

| Why is my bill going up? | Signed-in account review: promo ending, paying for unused speed, unused rented box, autopay off, outdated gateway, card expiring; fixes one at a time | About $30/month saved, retention and trust |

**Signed-in experience.** Customers connect their account with **OAuth 2.0 (authorization code + PKCE)** against a mock identity provider built into the app. The consent screen asks for "view my account" (required) and "make changes I confirm" (optional); view-only sessions can't change anything, enforced in code. Right after sign-in the chat opens with a proactive **account checkup**: what's good, and what needs attention across billing, service health, plan fit, and mobile and devices. Customers can disconnect at any time, which revokes the token and deletes the session's data.

Cross-line-of-business offers: **upgrade to 800 Mbps or faster and get one Unlimited mobile line free for 12 months.** Every order goes through a **preview** (line items, new monthly bill, due today, included benefits) and a single confirmation tap. The assistant is told to be decisive: sensible defaults, at most one question per reply.

A live **impact dashboard** counts containment, truck rolls avoided, offer conversion, new monthly revenue, upsells held back until a fault was fixed, devices sold, mobile bundles and new lines, storm passes, and guardrail interventions. Every number is derived from tool results and confirmed actions, never from what the model said.

## Quick start

Requirements: Python 3.12+, Node 18+ (Node 22 recommended), `make`.

```bash
make setup        # venv + pip deps, npm deps, .env, UI build
make run-mock     # no keys needed: offline scripted model
# open http://localhost:8000
```

For the real model and live data, set these in `.env` (empty = built-in simulation):

```bash
AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com   # official Azure OpenAI SDK
AZURE_OPENAI_API_KEY=<key>
AZURE_OPENAI_DEPLOYMENT=gpt-4.1
LIVE_MCP_URL=https://<api-host>/mcp                          # optional; live data, simulator as fallback
LIVE_MCP_TOKEN=<token>                                       # optional
```

Locally the app calls Azure directly; on Cloud Foundry the same values are used and calls go through your bound proxy service (`HTTPS_PROXY`). See [docs/configuration.md](docs/configuration.md). Check the model with `make llm-check` (one plain request and one tool-calling request), then `make dev` (auto-reload for Python and the UI) or `make run`.

Demo path: **Home → Start from a search → pick an example → Continue with Tidelink**, then open **Impact** in another tab.

## Make targets

| Command | What it does |
|---|---|
| `make setup` | One-time setup |
| `make dev` | API with reload + UI rebuild on change |
| `make run` / `make run-mock` | Run as on Cloud Foundry / with the offline model |
| `make test` | Python tests (scenarios, red-team guardrails, security, MCP contract, LLM client) |
| `make lint` / `make fmt` | Ruff lint / format |
| `make typecheck-web` | TypeScript check |
| `make check` | Everything CI runs |
| `make smoke` | End-to-end smoke test of a running app (`BASE_URL=...` for CF) |
| `make mcp-inspect` | Open MCP Inspector against `/mcp` |
| `make cf-push` | Build the UI and `cf push` (set Azure values once with `cf set-env`; see deployment doc) |

## How it fits together

```
Browser (React)
   │  SSE stream: text, tool cards, confirm cards
   ▼
Starlette app ── /api/*  chat API ── Orchestrator ── Azure OpenAI SDK (or mock)
   │                                      │
   │                                      ├─ MCP client (in-process) ─► our MCP server (simulated APIs)
   │                                      └─ MCP client (HTTP + Authorization) ─► live MCP server
   └── /mcp  the same MCP server over Streamable HTTP (bearer token optional: MCP_AUTH_REQUIRED)
```

Read more:

- [Architecture](docs/architecture.md): request flow, events, data-source routing
- [Adding a scenario](docs/adding-scenarios.md): YAML only, auto-tested
- [Connecting live data](docs/live-mcp.md): point flows at your real MCP endpoint
- [Security](docs/security.md): what is enforced in code, threat model, known gaps
- [Deploying to Cloud Foundry](docs/deployment-cloud-foundry.md)
- [Testing](docs/testing.md)
- [Demo script](docs/demo-script.md): a 7-minute walkthrough

## Project layout

```
app/
  main.py            app factory, routes, middleware wiring
  orchestrator.py    agent loop and server-side guardrails
  mcp_server.py      MCP tools (the contract)
  mcp_gateway.py     routes each tool to sim (in-process) or live (HTTP) MCP
  sim.py             simulated backend + business rules
  llm/               Azure OpenAI client (official SDK), offline mock
  scenarios.py       scenario YAML loader and matcher
  handoff.py         signed single-use search handoff tokens
  metrics.py         impact metrics
  security.py        headers, bearer auth, basic auth, rate limit, body limit
scenarios/*.yaml     one file per scenario
data/catalog.yaml    simulated plans, prices, promo, equipment
web/                 React + TypeScript UI (esbuild)
tests/               pytest suite
```

## Notes and limitations

- **Muse:** consumer Muse can't reach a server on a laptop or private network; it needs a public HTTPS MCP endpoint. This prototype demos the same MCP server through our own chat. Once `/mcp` is hosted publicly with a token, Muse (or any MCP client) can connect to it.
- **State is in memory** (sessions, metrics). Run one instance; add Redis before scaling out.
- **Build tooling:** the backend uses Starlette (the framework FastAPI is built on) and the UI is bundled with esbuild instead of Vite. Both keep dependencies small; the API shape is unchanged.
