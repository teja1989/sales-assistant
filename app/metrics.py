"""Business-impact metrics for the dashboard.

Every metric is derived from what actually happened in a session (tool results
and confirmed actions), not from what the model said. Storage is in-memory and
resets on restart; that is fine for a demo, and the event shape is ready to be
shipped to a real analytics sink.
"""

from __future__ import annotations

import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any

# Demo assumption, editable in one place and shown on the dashboard.
TRUCK_ROLL_COST_USD = 95.0


@dataclass
class SessionStats:
    scenario_id: str
    started_at: float
    outcomes: set[str] = field(default_factory=set)
    resolved_at: float | None = None


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.counters: Counter[str] = Counter()
            self.by_scenario: dict[str, Counter[str]] = {}
            self.sessions: dict[str, SessionStats] = {}
            self.tool_latency_ms: dict[str, list[int]] = {}
            self.monthly_revenue_delta = 0.0
            self.credits_issued = 0.0
            self.device_revenue = 0.0
            self.trade_in_credits = 0.0
            self.events: deque[dict[str, Any]] = deque(maxlen=150)

    def _bump(self, session_id: str | None, key: str, n: int = 1) -> None:
        self.counters[key] += n
        if session_id and session_id in self.sessions:
            scenario = self.sessions[session_id].scenario_id
            self.by_scenario.setdefault(scenario, Counter())[key] += n

    def _event(self, session_id: str | None, kind: str, detail: str = "") -> None:
        scenario = self.sessions[session_id].scenario_id if session_id in self.sessions else None
        self.events.appendleft({"ts": time.time(), "kind": kind, "scenario": scenario, "detail": detail})

    def _outcome(self, session_id: str, outcome: str) -> bool:
        stats = self.sessions.get(session_id)
        if stats is None or outcome in stats.outcomes:
            return False
        stats.outcomes.add(outcome)
        return True

    def _resolve(self, session_id: str) -> None:
        stats = self.sessions.get(session_id)
        if stats and stats.resolved_at is None:
            stats.resolved_at = time.time()
            self._bump(session_id, "resolved_without_agent")

    # ------------------------------------------------------------ recorders
    def session_started(self, session_id: str, scenario_id: str) -> None:
        with self._lock:
            self.sessions[session_id] = SessionStats(scenario_id, time.time())
            self._bump(session_id, "sessions")
            self._event(session_id, "session_started", scenario_id)

    def guardrail(self, session_id: str, kind: str) -> None:
        with self._lock:
            self._bump(session_id, "guardrail_interventions")
            self._event(session_id, "guardrail", kind)

    def confirmation(self, session_id: str, tool: str, approved: bool) -> None:
        with self._lock:
            self._bump(session_id, "actions_confirmed" if approved else "actions_declined")
            self._event(session_id, "action_confirmed" if approved else "action_declined", tool)

    def tool_result(
        self, session_id: str, tool: str, source: str, data: dict[str, Any], latency_ms: int, is_error: bool
    ) -> None:
        with self._lock:
            self._bump(session_id, "tool_calls")
            self._bump(session_id, f"tool_calls_{source.split()[0]}")
            self.tool_latency_ms.setdefault(tool, []).append(latency_ms)
            if is_error:
                self._bump(session_id, "tool_errors")
                self._event(session_id, "tool_error", tool)
                return
            if tool == "run_line_diagnostics":
                verdict = data.get("verdict")
                if verdict in ("area_outage", "gateway_fault", "signal_issue") and self._outcome(session_id, "fault"):
                    self._bump(session_id, "faults_detected")
                    self._event(session_id, "fault_detected", str(verdict))
            elif tool == "get_eligible_offers":
                if data.get("blocked"):
                    if self._outcome(session_id, "upsell_blocked"):
                        self._bump(session_id, "upsell_blocked_fault_first")
                        self._event(session_id, "upsell_blocked", str(data.get("blocked_reason")))
                elif data.get("offers") and self._outcome(session_id, "offer_presented"):
                    self._bump(session_id, "offers_presented")
                    self._event(session_id, "offer_presented", ", ".join(o["name"] for o in data["offers"]))
            elif tool == "get_device_offer" and isinstance(data.get("offer"), dict):
                if self._outcome(session_id, "offer_presented"):
                    self._bump(session_id, "offers_presented")
                    self._event(session_id, "offer_presented", str(data["offer"].get("name")))
            elif tool == "preview_order" and data.get("quote_id"):
                self._bump(session_id, "orders_previewed")
                self._event(session_id, "order_previewed", str(data.get("item")))
            elif tool == "activate_storm_data_pass" and data.get("activated"):
                self._bump(session_id, "storm_data_passes")
                self._event(
                    session_id, "storm_pass_activated", f"{data.get('hours')}h, {data.get('lines_covered')} lines"
                )
                self._resolve(session_id)
            elif tool == "reboot_gateway" and data.get("issue_resolved"):
                if self._outcome(session_id, "truck_roll_avoided"):
                    self._bump(session_id, "truck_rolls_avoided")
                    self._event(session_id, "truck_roll_avoided", "remote reboot fixed gateway fault")
                    self._resolve(session_id)
            elif tool == "apply_service_credit" and data.get("applied"):
                self.credits_issued += float(data.get("amount", 0))
                self._bump(session_id, "credits_applied")
                self._event(session_id, "credit_applied", f"${float(data.get('amount', 0)):.2f}")
                self._resolve(session_id)
            elif tool == "schedule_technician" and data.get("scheduled"):
                self._bump(session_id, "technician_visits_booked")
                self._event(session_id, "technician_booked", str(data.get("appointment_window")))
            elif tool == "submit_upgrade_order" and data.get("submitted") and not data.get("duplicate"):
                self._bump(session_id, "offers_accepted")
                self.monthly_revenue_delta += float(data.get("monthly_change") or 0)
                if data.get("type") == "device":
                    self._bump(session_id, "devices_sold")
                    self.device_revenue += float(data.get("full_price") or 0)
                    self.trade_in_credits += float(data.get("trade_in_credit") or 0)
                if data.get("included_benefits"):
                    self._bump(session_id, "mobile_bundles")
                if data.get("mobile_line_added"):
                    self._bump(session_id, "new_mobile_lines")
                self._event(session_id, "offer_accepted", str(data.get("item")))
                self._resolve(session_id)

    # -------------------------------------------------------------- summary
    def summary(self) -> dict[str, Any]:
        with self._lock:
            c = self.counters
            sessions = c["sessions"]
            presented = c["offers_presented"]
            resolution_times = [
                s.resolved_at - s.started_at for s in self.sessions.values() if s.resolved_at is not None
            ]
            latency = {tool: round(sum(v) / len(v)) for tool, v in self.tool_latency_ms.items() if v}
            return {
                "sessions": sessions,
                "resolved_without_agent": c["resolved_without_agent"],
                "containment_rate": round(c["resolved_without_agent"] / sessions, 3) if sessions else 0.0,
                "faults_detected": c["faults_detected"],
                "truck_rolls_avoided": c["truck_rolls_avoided"],
                "truck_roll_cost_assumption_usd": TRUCK_ROLL_COST_USD,
                "estimated_field_cost_avoided_usd": round(c["truck_rolls_avoided"] * TRUCK_ROLL_COST_USD, 2),
                "technician_visits_booked": c["technician_visits_booked"],
                "credits_applied": c["credits_applied"],
                "credits_issued_usd": round(self.credits_issued, 2),
                "offers_presented": presented,
                "offers_accepted": c["offers_accepted"],
                "offer_conversion_rate": round(c["offers_accepted"] / presented, 3) if presented else 0.0,
                "upsell_blocked_fault_first": c["upsell_blocked_fault_first"],
                "orders_previewed": c["orders_previewed"],
                "devices_sold": c["devices_sold"],
                "device_sales_usd": round(self.device_revenue, 2),
                "trade_in_credits_usd": round(self.trade_in_credits, 2),
                "mobile_bundles": c["mobile_bundles"],
                "new_mobile_lines": c["new_mobile_lines"],
                "storm_data_passes": c["storm_data_passes"],
                "incremental_monthly_revenue_usd": round(self.monthly_revenue_delta, 2),
                "actions_confirmed": c["actions_confirmed"],
                "actions_declined": c["actions_declined"],
                "guardrail_interventions": c["guardrail_interventions"],
                "tool_calls": c["tool_calls"],
                "tool_calls_sim": c["tool_calls_sim"],
                "tool_calls_live": c["tool_calls_live"],
                "tool_errors": c["tool_errors"],
                "avg_time_to_resolution_s": round(sum(resolution_times) / len(resolution_times), 1)
                if resolution_times
                else None,
                "avg_tool_latency_ms": latency,
                "by_scenario": {k: dict(v) for k, v in self.by_scenario.items()},
                "recent_events": list(self.events)[:40],
                "note": "Simulated demo data. In-memory; resets when the app restarts.",
            }
