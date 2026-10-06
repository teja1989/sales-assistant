# Demo script (about 7 minutes)

Setup: app running (`make run` with Azure, or `make run-mock`). Two tabs: **Home** and **Impact** (`/dashboard`). Click **Reset numbers** on the dashboard first.

## 1. The problem (30 s)

Customers ask AI assistants about their internet before they ever call us. Today that ends in a generic answer or a phone queue. This prototype shows the handoff into our own assistant, connected to our systems through MCP.

## 2. Broken, not slow: no upsell (2 min)

1. Home → **Start from a search** → "why does my home internet keep dropping" → **Continue in Lumora Assist**.
2. Point out: no "what's your account number". It greets the customer by name and already knows the question.
3. Open **MCP trace** (right side): profile, outage and diagnostics ran in parallel, each badged **sim** or **live** with latency.
4. Diagnosis: gateway fault, 7 drops in 24 hours. It proposes a remote reboot. **Nothing happens until the customer confirms.**
5. Before confirming, tap **"Should I upgrade my plan?"**. The offers card says **Offers paused**: we don't sell to someone whose service is broken. That rule lives in the API, not the prompt.
6. Confirm the reboot. Fixed. Dashboard: truck roll avoided, upsell held back.

## 3. Outage: honesty and goodwill (1 min)

Search "my internet is not working right now". The assistant finds the area outage, gives the cause and ETA, says a reboot won't help, and offers a credit. Confirm.

## 4. Real upsell, backed by data (2 min)

Search "how to get faster internet speed at home". Line healthy; usage at 97% of plan, 46 hours at the limit this month. It recommends the right-sized tier (not the most expensive), with the price from the catalog. **Yes, upgrade me** → confirm card shows price, change and promo → Confirm.

## 5. The honest sale (1 min)

Search "internet slow in upstairs bedroom". Plan has headroom; the bedroom has weak Wi-Fi. The assistant says a faster plan won't fix it and recommends a mesh pod. This is the trust story: the right product, not the biggest one.

## 6. Impact and safety (30 s)

Dashboard: containment rate, truck rolls avoided and estimated savings, offer conversion, new monthly revenue, upsells held back until fixed, guardrail interventions, sim vs live calls.

## Likely questions

- **Is this Muse?** It's the MCP server Muse would connect to, demoed through our own chat. Muse needs a public endpoint; this runs internally today.
- **Is the data real?** Simulated by default. Flows can be switched to the live MCP endpoint per tool; badges show which is which.
- **What if the model goes rogue?** Account access, actions, offers and prices are enforced in code. There's a red-team test suite for exactly that.
- **Are these numbers real business impact?** No. They show the mechanism and what we'd measure. The next step is an A/B pilot.
