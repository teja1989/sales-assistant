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
HUMAN = re.compile(
    r"\b(human|real person|a person|agent|representative|someone real|speak to someone|talk to someone)\b", re.I
)
OFF_TOPIC = re.compile(
    r"\b(stock|stocks|invest|lawyer|legal advice|medical|doctor|diagnos\w*|election|politic\w*|vote)\b", re.I
)
FRUSTRATED = re.compile(r"\b(frustrat\w*|angry|ridiculous|terrible|unacceptable|fed up)\b", re.I)
NO = re.compile(r"\b(no|nope|not now|later|nah|cancel|don'?t)\b", re.I)


def _money(value: Any) -> str:
    return f"${float(value):,.2f}"


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

        query_match = re.search(r'searched for: "([^"]*)"', system)
        query = query_match.group(1) if query_match else ""

        # 1. First look: gather facts in parallel.
        if "get_customer_profile" not in results and intent == "device":
            calls = [("get_customer_profile", {})]
            if can("get_device_offer"):
                calls.append(("get_device_offer", {"model": query or last_user}))
            return "Happy to help. Let me pull up the details and what you'd personally get for it.", calls
        if "get_customer_profile" not in results and intent == "alert":
            calls = [(t, {}) for t in ("get_customer_profile", "check_service_alerts", "check_area_outage") if can(t)]
            return "Let me check the alerts for your area and your account.", calls
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
                prior = next((m.get("content") or "" for m in reversed(messages) if m["role"] == "assistant"), "")
                if "Confirm" in prior:
                    return "", []  # already told them to tap Confirm; don't repeat
                return "Tap **Confirm** on the card and I'll take care of it, or **Not now** to skip.", []
            if data.get("status") == "declined_by_customer":
                return "No problem, I won't do that. Is there anything else I can help with?", []
            if data.get("error"):
                detail = str(data["error"])
                if "catalog" in detail:
                    return f"I'm sorry, {detail.split(': ', 1)[-1]} I'm happy to share details on any of those.", []
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
        device = results.get("get_device_offer")
        ordered = "submit_upgrade_order" in results and results["submit_upgrade_order"].get("submitted")
        pick = None
        if device and isinstance(device.get("offer"), dict):
            pick = device["offer"]
        elif offers and offers.get("offers"):
            pick = next((o for o in offers["offers"] if o.get("recommended")), None)

        # 3. Follow-up user turns: human handoff and scope come first.
        if last["role"] == "user" and HUMAN.search(last_user):
            return (
                f"Of course, {name}. I'll connect you with a specialist now, and they'll see everything we've "
                "covered so you won't need to repeat yourself. Is there anything you'd like me to add for them?"
            ), []
        if last["role"] == "user" and OFF_TOPIC.search(last_user):
            return (
                "I'm sorry, that's outside what I can help with here. I can help with your internet, mobile "
                "service, devices and billing. Is there anything along those lines I can do for you?"
            ), []
        if last["role"] == "user" and FRUSTRATED.search(last_user) and not pick:
            return (
                f"I understand how disruptive this is, {name}, and I'm sorry for the trouble. "
                "I'm staying with you until it's sorted. Is there anything specific you need right now?"
            ), []
        if (
            last["role"] == "user"
            and offers is None
            and can("get_eligible_offers")
            and re.search(r"\b(upgrade|faster|more speed|plan)\b", last_user, re.I)
        ):
            return "Let me check what you're eligible for.", [("get_eligible_offers", {"need": "speed"})]
        if last["role"] == "user" and any(
            isinstance(r, dict) and r.get("status") == "awaiting_customer_confirmation" for r in results.values()
        ):
            return "Whenever you're ready, tap **Confirm** on the card (or **Not now** to skip).", []
        if last["role"] == "user" and pick and not ordered:
            if YES.search(last_user):
                if can("preview_order"):
                    return f"Thank you. Here's your order preview for **{pick['name']}**.", [
                        ("preview_order", {"offer_id": pick["offer_id"]})
                    ]
                if can("submit_upgrade_order"):
                    return f"Thank you. I'll set up **{pick['name']}** for you.", [
                        ("submit_upgrade_order", {"offer_id": pick["offer_id"]})
                    ]
            if NO.search(last_user):
                return "Totally fine. Nothing changes on your account. Anything else I can help with?", []

        # Device and alert flows don't need line diagnostics.
        if intent == "device" and device and not ordered:
            return self._present_device(name, device), []
        if intent == "alert":
            alerts = results.get("check_service_alerts", {})
            courtesy = alerts.get("courtesy") or {}
            if (
                alerts.get("alerts")
                and courtesy.get("storm_data_pass_eligible")
                and "activate_storm_data_pass" not in results
                and can("activate_storm_data_pass")
            ):
                alert = alerts["alerts"][0]
                return (
                    f"Hi {name}, there's a **{alert.get('headline', 'storm warning')}** for {alerts.get('service_area', 'your area')} "
                    f"{alert.get('window', 'over the next 48 hours')}. Your home internet is working normally right now, "
                    "but storms can knock out power.\n\n"
                    f"So you stay connected no matter what, I can turn on **free unlimited mobile data for the next {courtesy.get('hours', 48)} hours** "
                    "on all your lines, at no charge."
                ), [("activate_storm_data_pass", {})]
            if not alerts.get("alerts"):
                return f"Good news, {name}: there are no weather or network alerts for your area right now.", []
        if intent in ("alert", "device"):
            return "Is there anything else I can help you with today?", []

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
        if tool == "preview_order" and data.get("quote_id"):
            parts = []
            if data.get("type") == "device":
                parts.append(
                    f"**{data['item']}**: {_money(data['device_total'])} after your trade-in, or "
                    f"**{_money(data['monthly_installment'])}/month** for {data['installment_months']} months."
                )
            else:
                parts.append(
                    f"New monthly price: **{_money(data['new_monthly_price'])}** (today {_money(data['current_monthly_price'])})."
                )
            parts.append(f"Due today: **{_money(data['due_today'])}**.")
            if data.get("included_benefits"):
                parts.append("Included: " + "; ".join(data["included_benefits"]) + ".")
            return " ".join(parts) + " Tap **Confirm** to place the order.", [
                ("submit_upgrade_order", {"offer_id": data["offer_id"]})
            ]
        if tool == "activate_storm_data_pass" and data.get("activated"):
            return (
                f"Done. Free unlimited mobile data is on for **{data['hours']} hours** across your "
                f"{data['lines_covered']} line{'s' if data['lines_covered'] != 1 else ''}. It ends automatically, nothing to cancel.\n\n"
                "Stay safe. Charge your phones now, and if the power goes out, your phone can be a hotspot for your laptop."
            ), []
        if tool == "submit_upgrade_order" and data.get("submitted"):
            price = data.get("new_monthly_price")
            price_text = f" Your new monthly price is **{_money(price)}**." if price else ""
            extra = ""
            if data.get("included_benefits"):
                extra = " " + " ".join(f"Your **{b}** is active." for b in data["included_benefits"])
                if data.get("mobile_line_added"):
                    extra += " A SIM kit is on its way."
            if data.get("type") == "device":
                extra = " Your trade-in kit ships with the phone."
            return (
                f"All set. Order **{data['order_id']}** for {data['item']} is confirmed. "
                f"{data.get('effective', '')}.{price_text}{extra} Anything else I can help with?"
            ), []
        return None

    def _present_device(self, name: str, data: dict[str, Any]) -> str:
        device = data["device"]
        pricing = data["pricing"]
        trade = data["trade_in"]
        offer = data["offer"]
        highlights = "\n".join(f"- {h}" for h in device["highlights"][:4])
        text = (
            f"Hi {name}, here's the **{device['name']}**. {device['availability']}.\n\n{highlights}\n\n"
            f"Storage from {device['starting_storage']} to {device['storage_options'][-1]}, in {', '.join(device['colors'])}. "
            f"It starts at **{_money(pricing['full_price'])}**.\n\n"
        )
        if trade.get("eligible"):
            text += (
                f"Upgrade now for a personalized offer: trading in your **{trade['current_device']}** is usually worth "
                f"**{_money(trade['usual_credit'])}**, and as a valued customer you get an extra "
                f"**{_money(trade['valued_customer_bonus_credit'])}**. That's **{_money(trade['total_trade_in_credit'])}** off, "
                f"so it's **{_money(offer['price_after_trade_in'])}** or **{_money(offer['monthly_installment_after_trade_in'])}/month**.\n\n"
                "Want me to preview the order?"
            )
        else:
            text += f"That's **{_money(pricing['monthly_installment'])}/month** over {pricing['installment_months']} months. Want me to preview the order?"
        return text

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
        bundle_text = ""
        if pick.get("bundle"):
            bundle_text = f"\n\nBonus: it includes **{pick['bundle']['name']}** (worth {_money(pick['bundle']['worth_monthly_price'])}/month)."
        else:
            alt = next((o for o in offers["offers"] if o.get("bundle")), None)
            if alt:
                diff = alt["monthly_price"] - pick["monthly_price"]
                bundle_text = (
                    f"\n\nWorth knowing: **{alt['name']}** is {_money(diff)}/month more than that and includes "
                    f"**{alt['bundle']['name']}**."
                )
        activities = ", ".join(usage.get("top_activities", [])[:3])
        activity_text = f" (mostly {activities})" if activities else ""
        return (
            f"Hi {name}, your connection is healthy, so this isn't a fault. You're simply outgrowing your plan{activity_text}. "
            f"At peak times your household uses about **{usage.get('peak_utilization_pct', '?')}%** of your "
            f"{plan.get('download_mbps')} Mbps, and you hit the limit about "
            f"**{usage.get('hours_at_plan_limit_30d', '?')} hours** in the last 30 days.\n\n"
            f"I'd recommend **{pick['name']}** ({pick.get('download_mbps')} Mbps): "
            f"{_money(pick['monthly_change'])}/month more than today.{promo_text}{bundle_text}\n\nWant me to upgrade you?"
        )
