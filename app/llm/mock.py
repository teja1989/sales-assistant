"""Deterministic offline "model" for demos without Azure and for repeatable tests.

It follows the same contract as a real model: it reads the conversation
(including tool results) and either streams text or requests tool calls. It
never invents prices: every number it says comes from a tool result.

Set LLM_PROVIDER=mock. Useful as a demo-day fallback if the LLM endpoint is down.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import AsyncIterator
from typing import Any

from app.llm.base import LlmEvent, TextDelta, ToolCall, TurnComplete

YES = re.compile(
    r"\b(yes|yeah|yep|sure|ok|okay|do it|go ahead|upgrade|sounds good|let'?s do|add it|please do|confirm)\b", re.I
)
NO = re.compile(r"\b(no|nope|not now|later|nah|cancel|don'?t)\b", re.I)


def _money(value: Any) -> str:
    return f"${float(value):.2f}"


class MockLlm:
    name = "mock"

    def __init__(self, delay_ms: int = 12) -> None:
        self.delay = max(delay_ms, 0) / 1000

    async def stream(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> AsyncIterator[LlmEvent]:
        available = {t["function"]["name"] for t in tools}
        text, calls = self.decide(messages, available)
        if text:
            for piece in re.findall(r"\S+\s*|\n+", text):
                yield TextDelta(piece)
                if self.delay:
                    await asyncio.sleep(self.delay)
        yield TurnComplete(
            tool_calls=[
                ToolCall(id=f"call_{uuid.uuid4().hex[:10]}", name=n, arguments=json.dumps(a)) for n, a in calls
            ],
            finish_reason="tool_calls" if calls else "stop",
        )

    # ------------------------------------------------------------------ brain
    def decide(self, messages: list[dict[str, Any]], available: set[str]) -> tuple[str, list[tuple[str, dict]]]:
        intent = "general"
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        match = re.search(r"SCENARIO_INTENT:\s*(\w+)", system)
        if match:
            intent = match.group(1)

        names: dict[str, str] = {}
        results: dict[str, dict[str, Any]] = {}
        for m in messages:
            if m["role"] == "assistant" and m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    names[tc["id"]] = tc["function"]["name"]
            elif m["role"] == "tool":
                try:
                    results[names.get(m["tool_call_id"], "?")] = json.loads(m["content"])
                except (json.JSONDecodeError, KeyError):
                    pass

        last = messages[-1]
        last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "") or ""
        can = lambda tool: tool in available  # noqa: E731

        def status(tool: str) -> str | None:
            r = results.get(tool)
            return r.get("status") if isinstance(r, dict) else None

        # 1. First look: gather facts in parallel.
        if "get_customer_profile" not in results:
            calls = [(t, {}) for t in ("get_customer_profile", "check_area_outage", "run_line_diagnostics") if can(t)]
            if intent == "speed" and can("get_usage_profile"):
                calls.append(("get_usage_profile", {}))
            return "Thanks for reaching out. I'm pulling up your account and running a quick remote check now.", calls

        # 2. React to the most recent tool result.
        if last["role"] == "tool":
            tool = names.get(last["tool_call_id"], "")
            data = results.get(tool, {})
            if data.get("status") == "awaiting_customer_confirmation":
                return "Tap **Confirm** on the card and I'll take care of it, or **Not now** to skip.", []
            if data.get("status") == "declined_by_customer":
                return "No problem, I won't do that. Is there anything else I can help with?", []
            if data.get("error"):
                return "I couldn't complete that step just now. I can connect you with a specialist if you'd like.", []
            narration = self._narrate_action(tool, data, results)
            if narration is not None:
                return narration

        profile = results["get_customer_profile"]
        name = profile.get("first_name", "there")
        plan = profile.get("plan", {})
        diag = results.get("run_line_diagnostics", {})
        verdict = diag.get("verdict", "healthy")
        offers = results.get("get_eligible_offers")
        ordered = "submit_upgrade_order" in results and results["submit_upgrade_order"].get("submitted")

        # 3. Follow-up user turns.
        if (
            last["role"] == "user"
            and offers is None
            and can("get_eligible_offers")
            and re.search(r"\b(upgrade|faster|more speed|plan)\b", last_user, re.I)
        ):
            return "Good question. Let me check what you're eligible for.", [("get_eligible_offers", {"need": "speed"})]
        if last["role"] == "user" and any(
            isinstance(r, dict) and r.get("status") == "awaiting_customer_confirmation" for r in results.values()
        ):
            return "Whenever you're ready, tap **Confirm** on the card (or **Not now** to skip).", []
        if last["role"] == "user" and offers and offers.get("offers") and not ordered:
            pick = next((o for o in offers["offers"] if o.get("recommended")), None)
            if pick and YES.search(last_user) and can("submit_upgrade_order"):
                return f"Great choice. I'll set up **{pick['name']}** for you.", [
                    ("submit_upgrade_order", {"offer_id": pick["offer_id"]})
                ]
            if NO.search(last_user):
                return "Totally fine. Your current plan stays as is. Anything else I can help with?", []

        # 4. Decide based on diagnostics.
        if verdict == "area_outage":
            outage = results.get("check_area_outage", {})
            eta = outage.get("estimated_restore_minutes")
            credit_done = status("apply_service_credit") is not None or "apply_service_credit" in results
            if outage.get("credit_eligible") and not credit_done and can("apply_service_credit"):
                return (
                    f"Hi {name}, I found the cause: there's a network outage in {outage.get('service_area', 'your area')} "
                    f"({outage.get('cause', 'equipment issue')}). Crews are on it, with service expected back in about "
                    f"**{eta} minutes**. Rebooting your gateway won't help until then.\n\n"
                    "Since this one's on us, I can add a service credit to your next bill."
                ), [("apply_service_credit", {})]
            return f"Service should be restored in about {eta} minutes. I'll stay here if you need anything else.", []

        if verdict == "gateway_fault":
            if "reboot_gateway" not in results and can("reboot_gateway"):
                drops = diag.get("connection_drops_24h", 0)
                return (
                    f"Hi {name}, good news: your area has no outage and your line signal is healthy. The problem is your "
                    f"gateway. It dropped the connection **{drops} times** in the last 24 hours and reports a firmware "
                    "fault that a remote restart usually clears.\n\nIt takes about 2 minutes. Want me to restart it now?"
                ), [("reboot_gateway", {})]
            return "Let me get a technician to look at the gateway.", (
                [("schedule_technician", {"issue_summary": "Gateway fault persists after remote reboot"})]
                if can("schedule_technician") and "schedule_technician" not in results
                else []
            )

        if verdict == "signal_issue" and "schedule_technician" not in results and can("schedule_technician"):
            return (
                f"Hi {name}, the signal levels on your line are out of range, which a restart won't fix. "
                "I'd like to book a technician."
            ), [("schedule_technician", {"issue_summary": "Downstream signal out of spec; intermittent drops"})]

        if verdict in ("wifi_coverage", "plan_capacity") and offers is None and can("get_eligible_offers"):
            need = "wifi" if verdict == "wifi_coverage" else "speed"
            calls = [("get_eligible_offers", {"need": need})]
            if need == "speed" and "get_usage_profile" not in results and can("get_usage_profile"):
                calls.insert(0, ("get_usage_profile", {}))
            return "Your connection itself is healthy. Let me check what would actually help.", calls

        if offers is not None and not ordered:
            return self._present_offers(name, plan, diag, results), []

        if verdict == "healthy":
            return (
                f"Hi {name}, everything checks out: no outage, healthy signal, and you're getting "
                f"{diag.get('measured_speed_mbps', plan.get('download_mbps'))} Mbps on your {plan.get('name')} plan. "
                "If a specific device is slow, try moving it closer to the gateway or restarting it."
            ), []

        return "Is there anything else I can help you with today?", []

    def _narrate_action(self, tool: str, data: dict[str, Any], results: dict[str, Any]):
        if tool == "reboot_gateway":
            if data.get("issue_resolved"):
                return (
                    "Your gateway is back online and the fault has cleared. Diagnostics now look healthy. "
                    "That should stop the drops. If it happens again, message me and I'll book a technician at no cost."
                ), []
            return "The restart finished but the fault is still there, so this needs a technician.", (
                [("schedule_technician", {"issue_summary": "Gateway fault persists after remote reboot"})]
            )
        if tool == "apply_service_credit" and data.get("applied"):
            return (
                f"Done. A **{_money(data['amount'])}** credit will appear on your {data.get('applies_to', 'next bill')}. "
                "I'm sorry for the trouble. Anything else I can help with?"
            ), []
        if tool == "schedule_technician" and data.get("scheduled"):
            fee = data.get("visit_fee", 0)
            fee_text = "There's no charge for this visit." if not fee else f"The visit fee is {_money(fee)}."
            return (f"You're booked: **{data['appointment_window']}** (ticket {data['ticket_id']}). {fee_text}"), []
        if tool == "submit_upgrade_order" and data.get("submitted"):
            price = data.get("new_monthly_price")
            price_text = f" Your new monthly price is **{_money(price)}**." if price else ""
            return (
                f"All set! Order **{data['order_id']}** for {data['item']} is confirmed. "
                f"{data.get('effective', '')}.{price_text} Anything else I can help with?"
            ), []
        return None

    def _present_offers(self, name: str, plan: dict[str, Any], diag: dict[str, Any], results: dict[str, Any]) -> str:
        offers = results["get_eligible_offers"]
        if offers.get("blocked"):
            return "Let's get the service issue fixed first. Upgrades can wait until everything is working."
        if not offers.get("offers"):
            return "You're already on our best option for this. Anything else I can help with?"
        pick = next((o for o in offers["offers"] if o.get("recommended")), None)
        if pick is None:
            usage = results.get("get_usage_profile", {})
            pct = usage.get("peak_utilization_pct")
            pct_text = f" At peak you use about **{pct}%** of it." if pct is not None else ""
            return (
                f"Honestly, {name}, you don't need a faster plan right now. Your {plan.get('name')} plan has plenty "
                f"of headroom.{pct_text} I'd rather save you the money. Anything else I can help with?"
            )
        if pick["type"] == "equipment":
            wifi = diag.get("wifi", {})
            return (
                f"Hi {name}, your internet connection is healthy and your plan has plenty of speed. The issue is "
                f"Wi-Fi reach: the **{wifi.get('weakest_room', 'far rooms')}** gets a weak signal "
                f"({wifi.get('weakest_rssi_dbm')} dBm). A faster plan **won't** fix that.\n\n"
                f"What will: a **{pick['name']}** for **{_money(pick['monthly_price'])}/month**. "
                "It ships free and sets up in about 10 minutes. Want me to add one?"
            )
        usage = results.get("get_usage_profile", {})
        promo = pick.get("promo") or {}
        promo_text = (
            f" With the current promo it's **{_money(promo['promo_monthly_price'])}/month** for "
            f"{promo['months']} months (then {_money(pick['monthly_price'])})."
            if promo
            else ""
        )
        return (
            f"Hi {name}, your connection is healthy, so this isn't a fault. You're simply outgrowing your plan. "
            f"At peak times your household uses about **{usage.get('peak_utilization_pct', '?')}%** of your "
            f"{plan.get('download_mbps')} Mbps, and you hit the limit about "
            f"**{usage.get('hours_at_plan_limit_30d', '?')} hours** in the last 30 days.\n\n"
            f"I'd recommend **{pick['name']}** ({pick.get('download_mbps')} Mbps): "
            f"{_money(pick['monthly_change'])}/month more than today.{promo_text}\n\nWant me to upgrade you?"
        )
