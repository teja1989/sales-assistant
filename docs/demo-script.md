# Demo script (about 10 minutes)

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

## 6. Gamer: internet upsell that adds a mobile line (1 min)

Search "fix lag online gaming home internet evenings". Healthy line, 92% peak usage from gaming, downloads and Twitch streaming. It recommends **Turbo 800**: with the promo it's the same monthly price for 12 months, and it includes **a free year of mobile**. Tap **Yes, upgrade me**: the **order preview** shows line items, new bill and $0 due today. One tap on Confirm. Dashboard: offer accepted, mobile bundle, new mobile line.

(Optional: "4K streaming buffering on multiple TVs at night" shows the same pattern at the top tier.)

## 7. Storm: proactive care (45 s)

Search "storm warning tonight will my internet keep working". The assistant finds the severe-storm alert, reassures the customer, and offers **free unlimited mobile data for 48 hours**, one tap, no charge, then signs off with "stay safe".

## 8. Device: iPhone 18 Pro with a personalized trade-in (1 min)

Search "apple iphone 18 pro price and specs". No diagnostics needed; it shows the device facts (sourced from Apple's announcement) and the personalized trade-in: **$800 usual + $200 valued-customer bonus = $1,000 off**, so $199 or $8.29/month. **Yes, preview the order**, then Confirm.

## 9. Impact and safety (30 s)

Dashboard: containment rate, truck rolls avoided and estimated savings, offer conversion, new monthly revenue, upsells held back until fixed, devices sold and trade-in credits, mobile bundles and new lines, storm passes, guardrail interventions, sim vs live calls.

## Likely questions

- **Is this Muse?** It's the MCP server Muse would connect to, demoed through our own chat. Muse needs a public endpoint; this runs internally today.
- **Is the data real?** Simulated by default. Flows can be switched to the live MCP endpoint per tool; badges show which is which.
- **What if the model goes rogue?** Account access, actions, offers and prices are enforced in code. There's a red-team test suite for exactly that.
- **Are the iPhone facts real?** Yes: starting price, storage, chip, camera, battery and colors are from Apple's September 2026 announcement. Trade-in values, the bonus and financing are simulated.
- **Are these numbers real business impact?** No. They show the mechanism and what we'd measure. The next step is an A/B pilot.
