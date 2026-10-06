# Adding a scenario

A scenario is one YAML file in `scenarios/`. No code changes. It is loaded at startup, validated, shown on the home page and search page, and **automatically played end to end by the test suite** against its `expected:` block.

## Steps

1. Copy the closest existing file, e.g. `cp scenarios/speed-upgrade.yaml scenarios/gaming-lag.yaml`.
2. Change `id` (lowercase slug), `title`, `description`, `search_query`, `match_keywords`.
3. Give the customer a **unique** `customer.id` and set the fixture data that drives the diagnosis.
4. List the `tools` the assistant may use. Only these are visible to the model.
5. Optionally set `live_customer_id` to a real/test account for when `LIVE_MCP_URL` is configured.
6. Set `expected` so the test knows what "good" looks like.
7. `make test`, then restart the app (or let `make dev` reload it).

## Fields

| Field | Purpose |
|---|---|
| `id` | Unique slug |
| `title`, `description` | Shown in the UI (launcher card, sign-in account picker) |
| `demo_order` | Position on the home-page demo launcher and sign-in picker (lower first; default 100) |
| `intent` | `connectivity`, `speed`, `wifi`, `device`, `alert`, `account` or `general`; goes into the system prompt and steers the first tools called |
| `search_query` | Example search; shown as a chip on the search page |
| `match_keywords` | Words/phrases that route a typed search to this scenario (phrases score higher) |
| `assistant_brief` | Extra guidance appended to the system prompt |
| `tools` | Allowlist of MCP tools |
| `live_customer_id` | Account id sent to the live MCP server when `LIVE_MCP_URL` is set (the simulator uses its own clone id) |
| `suggested_replies` | Quick-reply chips in the chat |
| `customer` | Simulated fixture (see below) |
| `expected` | `must_call`, `must_not_call`, `confirm_action`, `outcome` |

## Customer fixture and how it maps to a diagnosis

`run_line_diagnostics` picks the first matching verdict:

| Fixture | Verdict |
|---|---|
| `outage.status: active` | `area_outage` |
| `fault: {...}` | `gateway_fault` (`fixable_by_reboot: true` lets a reboot clear it) |
| `line.downstream_snr_db < 30` or `abs(line.downstream_power_dbmv) > 10` | `signal_issue` |
| `wifi.coverage: weak` | `wifi_coverage` |
| `usage.peak_utilization_pct >= 85` | `plan_capacity` |
| otherwise | `healthy` |

Offers are blocked while the verdict is `area_outage`, `gateway_fault` or `signal_issue`. A plan is marked `recommended` only when peak utilization is 70% or more, sized to about 1.4x peak usage.

`plan_id` must exist in `data/catalog.yaml`. Plans of 800 Mbps or faster carry the free-mobile-year bundle (`bundle_promos` in the catalog).

Optional `billing` and rented-equipment blocks drive the account checkup:

```yaml
billing:
  autopay: false               # "Autopay and paperless are off" (fix: enroll_autopay)
  last_payment_status: ok      # "failed" adds a high-severity item
  card_expires_in_days: 40     # within 60 days adds a reminder
  promo: {name: Welcome offer, ends_in_days: 9, current_monthly_price: 70.00, after_promo_price: 85.00}
equipment:
  firmware: "6.2.1"            # older than firmware_latest in the catalog adds "Gateway update available"
  rented:
    - {id: tv-box-legacy, name: TV box, monthly_fee: 10.00, days_since_used: 140}
```

Optional fixture blocks for mobile flows:

```yaml
mobile:                       # null = no mobile service (an 800+ upgrade adds a free line)
  plan: Unlimited
  lines:
    - {line_id: LN-1, device: iPhone 15 Pro, device_condition: good}   # first line is the trade-in device
weather_alert:                # makes check_service_alerts return a storm and enables the storm data pass
  type: severe_storm
  headline: "Severe Thunderstorm Warning"
  window: "from 6 PM tonight through tomorrow evening"
  expected_impact: "Damaging winds; power and network interruptions possible"
```

Trade-in: the usual credit applies when the device is in `trade_in.eligible_devices` and in good condition; the valued-customer bonus applies at `valued_customer_min_tenure_months` or more. Devices live under `devices` in the catalog; add one with its `match` phrases and only facts you have verified.

## `expected.outcome` values

`truck_roll_avoided`, `credit_applied`, `offer_accepted`, `technician_booked`, `device_sold`, `storm_pass`, `account_fixed`. The test checks the matching dashboard metric moved.

## Example: signal problem that needs a technician

```yaml
id: noisy-line
title: "Internet cuts out when it rains"
description: "Out-of-spec signal; the assistant books a technician instead of rebooting or selling."
intent: connectivity
search_query: "internet cuts out when it rains"
match_keywords: [rain, weather, "cuts out"]
assistant_brief: Signal problems need a technician; don't suggest upgrades.
tools: [get_customer_profile, check_area_outage, run_line_diagnostics, schedule_technician]
customer:
  id: LUM-9009
  first_name: Alex
  plan_id: plus-500
  outage: null
  fault: null
  line: {gateway_online: true, downstream_snr_db: 24.0, downstream_power_dbmv: -12.5, upstream_power_dbmv: 52, drops_24h: 11}
  wifi: {coverage: good, connected_devices: 9}
  usage: {peak_utilization_pct: 40, measured_speed_mbps: 210}
expected:
  must_call: [run_line_diagnostics]
  must_not_call: [submit_upgrade_order]
  confirm_action: schedule_technician
  outcome: technician_booked
```

## Adding a new tool

1. Add the function to `app/mcp_server.py` with a clear description and the right annotation (`READ` or `ACTION`).
2. Implement simulated behaviour in `app/sim.py`.
3. Add a UI card in `web/src/components/ToolCard.tsx` (optional; there's a generic fallback).
4. Update `READ_TOOLS`/`ACTION_TOOLS` in `tests/test_mcp_contract.py`.
