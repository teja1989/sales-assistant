# Adding a scenario

A scenario is one YAML file in `scenarios/`. No code changes. It is loaded at startup, validated, shown on the home page and search page, and **automatically played end to end by the test suite** against its `expected:` block.

## Steps

1. Copy the closest existing file, e.g. `cp scenarios/speed-upgrade.yaml scenarios/gaming-lag.yaml`.
2. Change `id` (lowercase slug), `title`, `description`, `search_query`, `match_keywords`.
3. Give the customer a **unique** `customer.id` and set the fixture data that drives the diagnosis.
4. List the `tools` the assistant may use. Only these are visible to the model.
5. Optionally route tools to live data with `data_sources`.
6. Set `expected` so the test knows what "good" looks like.
7. `make test`, then restart the app (or let `make dev` reload it).

## Fields

| Field | Purpose |
|---|---|
| `id` | Unique slug |
| `title`, `description` | Shown in the UI |
| `intent` | `connectivity`, `speed`, `wifi` or `general`; goes into the system prompt |
| `search_query` | Example search; shown as a chip on the search page |
| `match_keywords` | Words/phrases that route a typed search to this scenario (phrases score higher) |
| `assistant_brief` | Extra guidance appended to the system prompt |
| `tools` | Allowlist of MCP tools |
| `data_sources` | `tool: sim|live`; unlisted tools use `sim` |
| `live_customer_id` | Account id to send to the live MCP server (sim uses its own clone id) |
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

`plan_id` must exist in `data/catalog.yaml`.

## `expected.outcome` values

`truck_roll_avoided`, `credit_applied`, `offer_accepted`, `technician_booked`. The test checks the matching dashboard metric moved.

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
  id: LUM-5005
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
