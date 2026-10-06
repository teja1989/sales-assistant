"""Simulated backend systems (account, network diagnostics, offers, orders).

This stands in for the real telecom APIs. Business rules that must never be
left to the language model live here, server side, exactly where they would
live in production:

* No upsell while a fault is open (outage, gateway fault, signal problem).
* Prices come only from the catalog.
* Orders can only be placed for offers the customer is currently eligible for.
* Credits are applied at most once per outage.
"""

from __future__ import annotations

import copy
import secrets
import threading
from pathlib import Path
from typing import Any

import yaml

from app.config import ROOT_DIR

CATALOG_PATH = ROOT_DIR / "data" / "catalog.yaml"


class SimError(Exception):
    """An anticipated business-rule failure (returned to the model as a tool error)."""


def load_catalog(path: Path = CATALOG_PATH) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _money(value: float) -> float:
    return round(float(value) + 0.0, 2)


class SimStore:
    """In-memory customer state. Each chat session gets its own isolated clone."""

    def __init__(self, catalog: dict[str, Any] | None = None) -> None:
        self.catalog = catalog or load_catalog()
        self._plans = {p["id"]: p for p in self.catalog["plans"]}
        self._equipment = {e["id"]: e for e in self.catalog["equipment"]}
        self._devices = {d["id"]: d for d in self.catalog.get("devices", [])}
        self._customers: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ setup
    def add_customer(self, fixture: dict[str, Any], clone: bool = False) -> str:
        """Register a fixture. With clone=True a unique copy is created per session."""
        data = copy.deepcopy(fixture)
        if data["plan_id"] not in self._plans:
            raise SimError(f"Unknown plan_id {data['plan_id']!r} for customer {data['id']}")
        if clone:
            data["id"] = f"{data['id']}-{secrets.token_hex(3)}"
        data.setdefault("orders", [])
        data.setdefault("credits", [])
        data.setdefault("tickets", [])
        data.setdefault("benefits", [])
        with self._lock:
            self._customers[data["id"]] = data
        return data["id"]

    def remove_customer(self, customer_id: str) -> None:
        with self._lock:
            self._customers.pop(customer_id, None)

    def _get(self, customer_id: str) -> dict[str, Any]:
        customer = self._customers.get(customer_id)
        if customer is None:
            raise SimError("Customer not found")
        return customer

    def plan(self, plan_id: str) -> dict[str, Any]:
        return self._plans[plan_id]

    # ------------------------------------------------------------ read tools
    def customer_profile(self, customer_id: str) -> dict[str, Any]:
        c = self._get(customer_id)
        plan = self._plans[c["plan_id"]]
        return {
            "customer_id": c["id"],
            "first_name": c["first_name"],
            "tenure_months": c.get("tenure_months"),
            "service_area": c.get("region"),
            "plan": {
                "id": plan["id"],
                "name": plan["name"],
                "download_mbps": plan["download_mbps"],
                "upload_mbps": plan["upload_mbps"],
                "monthly_price": _money(plan["monthly_price"]),
            },
            "equipment": c.get("equipment", {}),
            "mobile": self._mobile_summary(c),
            "recent_orders": [o["order_id"] for o in c["orders"]],
        }

    @staticmethod
    def _mobile_summary(c: dict[str, Any]) -> dict[str, Any] | None:
        mobile = c.get("mobile")
        if not mobile or not mobile.get("lines"):
            return None
        return {
            "plan": mobile.get("plan", "Lumora Unlimited"),
            "lines": len(mobile["lines"]),
            "devices": [line.get("device") for line in mobile["lines"]],
            "free_months_remaining": mobile.get("free_months_remaining", 0),
        }

    def area_outage(self, customer_id: str) -> dict[str, Any]:
        c = self._get(customer_id)
        outage = c.get("outage")
        if not outage or outage.get("status") == "resolved":
            return {"outage_active": False, "service_area": c.get("region")}
        return {
            "outage_active": True,
            "service_area": c.get("region"),
            "outage_id": outage["id"],
            "cause": outage["cause"],
            "estimated_restore_minutes": outage["eta_minutes"],
            "homes_affected": outage.get("affected_homes"),
            "credit_eligible": not any(cr["reason"] == "outage" for cr in c["credits"]),
        }

    def _verdict(self, c: dict[str, Any]) -> tuple[str, str]:
        line = c.get("line", {})
        wifi = c.get("wifi", {})
        usage = c.get("usage", {})
        outage = c.get("outage")
        if outage and outage.get("status") != "resolved":
            return "area_outage", "A network outage in the service area is affecting this home."
        fault = c.get("fault")
        if fault:
            return "gateway_fault", fault["description"]
        snr = line.get("downstream_snr_db", 40)
        power = line.get("downstream_power_dbmv", 0)
        if snr < 30 or abs(power) > 10:
            return "signal_issue", "Signal levels on the line are out of range; a technician visit is needed."
        if wifi.get("coverage") == "weak":
            return "wifi_coverage", f"Wi-Fi signal is weak in the {wifi.get('weakest_room', 'far rooms')}."
        if usage.get("peak_utilization_pct", 0) >= 85:
            return "plan_capacity", "The connection is healthy but regularly maxes out the current plan."
        return "healthy", "Everything looks healthy."

    def diagnostics(self, customer_id: str) -> dict[str, Any]:
        c = self._get(customer_id)
        verdict, summary = self._verdict(c)
        line = c.get("line", {})
        wifi = c.get("wifi", {})
        return {
            "verdict": verdict,
            "summary": summary,
            "gateway_online": line.get("gateway_online", True) and verdict != "area_outage",
            "fault_code": (c.get("fault") or {}).get("code"),
            "fixable_by_remote_reboot": bool((c.get("fault") or {}).get("fixable_by_reboot")),
            "signal": {
                "downstream_snr_db": line.get("downstream_snr_db"),
                "downstream_power_dbmv": line.get("downstream_power_dbmv"),
                "upstream_power_dbmv": line.get("upstream_power_dbmv"),
                "within_spec": verdict not in ("signal_issue",),
            },
            "connection_drops_24h": line.get("drops_24h", 0),
            "gateway_uptime_hours": line.get("uptime_hours"),
            "measured_speed_mbps": c.get("usage", {}).get("measured_speed_mbps"),
            "wifi": {
                "coverage": wifi.get("coverage", "good"),
                "connected_devices": wifi.get("connected_devices"),
                "weakest_room": wifi.get("weakest_room"),
                "weakest_rssi_dbm": wifi.get("weakest_rssi_dbm"),
                "interference": wifi.get("interference", "low"),
            },
        }

    def usage(self, customer_id: str) -> dict[str, Any]:
        c = self._get(customer_id)
        u = c.get("usage", {})
        plan = self._plans[c["plan_id"]]
        return {
            "plan_download_mbps": plan["download_mbps"],
            "peak_utilization_pct": u.get("peak_utilization_pct"),
            "hours_at_plan_limit_30d": u.get("hours_at_cap_30d"),
            "peak_hours": u.get("peak_hours"),
            "connected_devices": c.get("wifi", {}).get("connected_devices"),
            "top_activities": u.get("top_activities", []),
            "measured_speed_mbps": u.get("measured_speed_mbps"),
        }

    def _bundle_for(self, plan: dict[str, Any]) -> dict[str, Any] | None:
        promo = self.catalog.get("bundle_promos", {}).get("mobile_free_year")
        if not promo or plan["download_mbps"] < promo["min_download_mbps"]:
            return None
        return {
            "name": promo["description"],
            "months": promo["months"],
            "worth_monthly_price": _money(promo["worth_monthly_price"]),
        }

    # ------------------------------------------------------------ mobile + devices
    def _find_device(self, model: str) -> dict[str, Any]:
        text = " ".join(model.lower().replace("-", " ").split())
        for device in self._devices.values():
            names = [device["name"].lower(), *device.get("match", [])]
            if any(name in text or text in name for name in names if len(text) >= 3):
                return device
        available = ", ".join(d["name"] for d in self._devices.values())
        raise SimError(f"That device isn't in our catalog yet. Available: {available}.")

    def _trade_in(self, c: dict[str, Any]) -> dict[str, Any]:
        rules = self.catalog["trade_in"]
        lines = (c.get("mobile") or {}).get("lines") or []
        current = lines[0] if lines else {}
        device = current.get("device")
        eligible = bool(device) and device in rules["eligible_devices"] and current.get("device_condition") == "good"
        valued = c.get("tenure_months", 0) >= rules["valued_customer_min_tenure_months"]
        usual = rules["usual_credit"] if eligible else 0.0
        bonus = rules["valued_customer_bonus_credit"] if eligible and valued else 0.0
        return {
            "current_device": device,
            "eligible": eligible,
            "usual_credit": _money(usual),
            "valued_customer_bonus_credit": _money(bonus),
            "total_trade_in_credit": _money(usual + bonus),
            "valued_customer_reason": f"Customer for {c.get('tenure_months', 0)} months" if valued else None,
            "terms": rules["terms"],
        }

    def device_offer(self, customer_id: str, model: str) -> dict[str, Any]:
        c = self._get(customer_id)
        device = self._find_device(model)
        trade = self._trade_in(c)
        price = device["starting_price"]
        months = device["installment_months"]
        net = max(price - trade["total_trade_in_credit"], 0.0)
        return {
            "device": {
                "name": device["name"],
                "maker": device["maker"],
                "starting_storage": device["starting_storage"],
                "storage_options": device["storage_options"],
                "highlights": device["highlights"],
                "colors": device["colors"],
                "availability": device["available"],
                "facts_source": device["source"],
            },
            "pricing": {
                "full_price": _money(price),
                "installment_months": months,
                "monthly_installment": _money(price / months),
            },
            "trade_in": trade,
            "offer": {
                "offer_id": f"OFR-DEV-{device['id'].upper()}",
                "type": "device",
                "name": f"{device['name']} {device['starting_storage']}",
                "full_price": _money(price),
                "trade_in_credit": trade["total_trade_in_credit"],
                "price_after_trade_in": _money(net),
                "monthly_installment_after_trade_in": _money(net / months),
                "installment_months": months,
                "recommended": True,
            },
        }

    def service_alerts(self, customer_id: str) -> dict[str, Any]:
        c = self._get(customer_id)
        alert = c.get("weather_alert")
        if not alert:
            return {"alerts": [], "service_area": c.get("region")}
        has_mobile = bool((c.get("mobile") or {}).get("lines"))
        active_pass = any(b["type"] == "storm_data_pass" for b in c["benefits"])
        return {
            "service_area": c.get("region"),
            "alerts": [alert],
            "courtesy": {
                "storm_data_pass_eligible": has_mobile and not active_pass,
                "already_active": active_pass,
                "hours": self.catalog["mobile"]["storm_data_pass_hours"],
                "price": 0.0,
                "description": "Unlimited mobile data on every line, free during the storm window",
            },
            "safety_tips": [
                "Charge phones and battery packs now",
                "Your phone can be a hotspot if home internet or power drops",
                "Avoid downed lines; report them to your power company",
            ],
        }

    def activate_storm_pass(self, customer_id: str) -> dict[str, Any]:
        with self._lock:
            c = self._get(customer_id)
            if not c.get("weather_alert"):
                raise SimError("No active weather alert for this area.")
            lines = (c.get("mobile") or {}).get("lines") or []
            if not lines:
                raise SimError("The storm data pass is for accounts with Lumora mobile lines.")
            if any(b["type"] == "storm_data_pass" for b in c["benefits"]):
                raise SimError("The storm data pass is already active.")
            hours = self.catalog["mobile"]["storm_data_pass_hours"]
            benefit = {"type": "storm_data_pass", "id": f"SDP-{secrets.token_hex(3).upper()}", "hours": hours}
            c["benefits"].append(benefit)
        return {
            "activated": True,
            "pass_id": benefit["id"],
            "hours": hours,
            "lines_covered": len(lines),
            "charge": 0.0,
            "ends": f"Automatically in {hours} hours; nothing to cancel",
        }

    # ------------------------------------------------------------ orders
    def _resolve_offer(self, customer_id: str, offer_id: str) -> dict[str, Any]:
        if offer_id.startswith("OFR-DEV-"):
            device_id = offer_id.removeprefix("OFR-DEV-").lower()
            device = self._devices.get(device_id)
            if device is None:
                raise SimError("That offer is not available for this account.")
            return self.device_offer(customer_id, device["name"])["offer"]
        need = "wifi" if offer_id.startswith("OFR-EQP-") else "speed"
        eligible = self.offers(customer_id, need)
        if eligible["blocked"]:
            raise SimError("Orders are paused while a service issue is open.")
        offer = next((o for o in eligible["offers"] if o["offer_id"] == offer_id), None)
        if offer is None:
            raise SimError("That offer is not available for this account.")
        return offer

    def preview_order(self, customer_id: str, offer_id: str) -> dict[str, Any]:
        c = self._get(customer_id)
        offer = self._resolve_offer(customer_id, offer_id)
        current = self._plans[c["plan_id"]]
        items: list[dict[str, Any]] = []
        benefits: list[str] = []
        due_today = 0.0
        if offer["type"] == "device":
            months = offer["installment_months"]
            items = [
                {"label": offer["name"], "amount": offer["full_price"]},
                {"label": "Trade-in credit", "amount": -offer["trade_in_credit"]},
            ]
            monthly_after = offer["monthly_installment_after_trade_in"]
            summary = {
                "device_total": offer["price_after_trade_in"],
                "monthly_installment": monthly_after,
                "installment_months": months,
                "current_monthly_price": _money(current["monthly_price"]),
                "new_monthly_price": _money(current["monthly_price"] + monthly_after),
            }
            effective = "Ships free in 1-2 business days; trade-in kit included"
        elif offer["type"] == "equipment":
            items = [{"label": f"{offer['name']} (monthly)", "amount": offer["monthly_price"]}]
            summary = {
                "current_monthly_price": _money(current["monthly_price"]),
                "new_monthly_price": _money(current["monthly_price"] + offer["monthly_price"]),
            }
            effective = "Ships free in 2 business days"
        else:
            promo = offer.get("promo") or {}
            items = [{"label": f"{offer['name']} internet (monthly)", "amount": offer["monthly_price"]}]
            new_monthly = offer["monthly_price"]
            if promo:
                items.append(
                    {"label": f"Upgrade promo, {promo['months']} months", "amount": -promo["monthly_discount"]}
                )
                new_monthly = promo["promo_monthly_price"]
            bundle = offer.get("bundle")
            if bundle:
                items.append(
                    {"label": f"{bundle['name']}", "amount": 0.0, "worth_monthly_price": bundle["worth_monthly_price"]}
                )
                benefits.append(bundle["name"])
            summary = {
                "current_monthly_price": _money(current["monthly_price"]),
                "new_monthly_price": _money(new_monthly),
                "monthly_change": _money(new_monthly - current["monthly_price"]),
                "price_after_promo": offer["monthly_price"],
            }
            effective = "Takes effect within 15 minutes; no technician needed"
        return {
            "quote_id": f"QTE-{secrets.token_hex(3).upper()}",
            "offer_id": offer_id,
            "item": offer["name"],
            "type": offer["type"],
            "line_items": items,
            "due_today": _money(due_today),
            **summary,
            "included_benefits": benefits,
            "effective": effective,
            "note": "Taxes and fees not included. Simulated quote.",
        }

    def _blocking_fault(self, c: dict[str, Any]) -> str | None:
        verdict, _ = self._verdict(c)
        if verdict in ("area_outage", "gateway_fault", "signal_issue"):
            return verdict
        return None

    def offers(self, customer_id: str, need: str = "speed") -> dict[str, Any]:
        """Eligible offers. Upsell is blocked server-side while any fault is open."""
        c = self._get(customer_id)
        blocking = self._blocking_fault(c)
        if blocking:
            return {
                "blocked": True,
                "blocked_reason": f"open_issue:{blocking}",
                "message": "Offers are paused until the open service issue is fixed.",
                "offers": [],
            }
        current = self._plans[c["plan_id"]]
        promo = self.catalog["upgrade_promo"]
        result: list[dict[str, Any]] = []
        if need in ("wifi", "wifi_coverage"):
            pod = self._equipment["mesh-pod"]
            result.append(
                {
                    "offer_id": "OFR-EQP-MESH-POD",
                    "type": "equipment",
                    "name": pod["name"],
                    "description": pod["description"],
                    "monthly_price": _money(pod["monthly_price"]),
                    "monthly_change": _money(pod["monthly_price"]),
                    "promo": None,
                    "recommended": c.get("wifi", {}).get("coverage") == "weak",
                    "why": "Fixes weak-room coverage; a faster plan would not reach those rooms.",
                    "terms": "Return anytime to stop the monthly charge.",
                }
            )
        else:
            usage = c.get("usage", {})
            peak_mbps = current["download_mbps"] * usage.get("peak_utilization_pct", 0) / 100
            needed = peak_mbps * 1.4
            upgrades = [p for p in self.catalog["plans"] if p["download_mbps"] > current["download_mbps"]]
            outgrowing = usage.get("peak_utilization_pct", 0) >= 70
            recommended_id = (
                next(
                    (p["id"] for p in upgrades if p["download_mbps"] >= needed),
                    upgrades[-1]["id"] if upgrades else None,
                )
                if outgrowing
                else None
            )
            for plan in upgrades:
                delta = plan["monthly_price"] - current["monthly_price"]
                discount = promo["monthly_discount"]
                result.append(
                    {
                        "offer_id": f"OFR-UPG-{plan['id'].upper()}",
                        "type": "plan_upgrade",
                        "name": plan["name"],
                        "plan_id": plan["id"],
                        "download_mbps": plan["download_mbps"],
                        "upload_mbps": plan["upload_mbps"],
                        "monthly_price": _money(plan["monthly_price"]),
                        "current_monthly_price": _money(current["monthly_price"]),
                        "monthly_change": _money(delta),
                        "promo": {
                            "months": promo["months"],
                            "monthly_discount": _money(discount),
                            "promo_monthly_price": _money(plan["monthly_price"] - discount),
                            "promo_monthly_change": _money(max(delta - discount, 0)),
                        },
                        "bundle": self._bundle_for(plan),
                        "recommended": plan["id"] == recommended_id,
                        "why": (
                            f"Peak usage is about {round(peak_mbps)} Mbps on a "
                            f"{current['download_mbps']} Mbps plan; this tier leaves headroom."
                        ),
                        "terms": promo["terms"],
                    }
                )
        response: dict[str, Any] = {"blocked": False, "need": need, "offers": result}
        if need == "speed" and c.get("usage", {}).get("peak_utilization_pct", 0) < 70:
            response["advisory"] = (
                "Usage is well within the current plan. An upgrade is not recommended; say so honestly."
            )
        if need == "speed" and self._verdict(c)[0] == "wifi_coverage":
            response["advisory"] = (
                "Diagnostics show weak Wi-Fi coverage. A faster plan will not fix weak rooms; "
                "recommend the mesh pod first."
            )
        return response

    # ---------------------------------------------------------- action tools
    def reboot_gateway(self, customer_id: str) -> dict[str, Any]:
        with self._lock:
            c = self._get(customer_id)
            verdict, _ = self._verdict(c)
            if verdict == "area_outage":
                raise SimError("A reboot will not help during an area outage.")
            fault = c.get("fault")
            fixed = bool(fault and fault.get("fixable_by_reboot"))
            if fixed:
                c["fault"] = None
                c.setdefault("line", {})["drops_24h"] = 0
                c["line"]["uptime_hours"] = 0
                c["line"]["gateway_online"] = True
        after = self.diagnostics(customer_id)
        return {
            "rebooted": True,
            "issue_resolved": fixed,
            "duration_seconds": 140,
            "post_reboot_verdict": after["verdict"],
            "post_reboot_summary": after["summary"],
        }

    def schedule_technician(self, customer_id: str, issue_summary: str) -> dict[str, Any]:
        with self._lock:
            c = self._get(customer_id)
            ticket = {
                "ticket_id": f"TKT-{secrets.token_hex(3).upper()}",
                "appointment_window": self.catalog["technician"]["first_available"],
                "visit_fee": _money(self.catalog["technician"]["visit_fee"]),
                "issue_summary": issue_summary[:200],
            }
            c["tickets"].append(ticket)
        return {"scheduled": True, **ticket}

    def apply_service_credit(self, customer_id: str) -> dict[str, Any]:
        with self._lock:
            c = self._get(customer_id)
            outage = c.get("outage")
            if not outage or outage.get("status") == "resolved":
                raise SimError("Service credits are only available during an active outage.")
            if any(cr["reason"] == "outage" for cr in c["credits"]):
                raise SimError("A credit has already been applied for this outage.")
            credit = {
                "credit_id": f"CRD-{secrets.token_hex(3).upper()}",
                "reason": "outage",
                "amount": _money(self.catalog["credits"]["outage_credit"]),
                "applies_to": "next bill",
            }
            c["credits"].append(credit)
        return {"applied": True, **credit}

    def submit_order(self, customer_id: str, offer_id: str) -> dict[str, Any]:
        c = self._get(customer_id)
        for order in c["orders"]:
            if order["offer_id"] == offer_id:
                return {"submitted": True, "duplicate": True, **order}
        offer = self._resolve_offer(customer_id, offer_id)
        quote = self.preview_order(customer_id, offer_id)
        with self._lock:
            order = {
                "order_id": f"ORD-{secrets.token_hex(4).upper()}",
                "offer_id": offer_id,
                "type": offer["type"],
                "item": offer["name"],
                "monthly_change": 0.0,
                "new_monthly_price": quote.get("new_monthly_price"),
                "included_benefits": list(quote["included_benefits"]),
                "effective": quote["effective"],
            }
            if offer["type"] == "plan_upgrade":
                order["monthly_change"] = _money(offer["monthly_change"])
                c["plan_id"] = offer["plan_id"]
                c.setdefault("usage", {})["peak_utilization_pct"] = 55
                if offer.get("bundle"):
                    order["mobile_line_added"] = self._apply_mobile_free_year(c)
            elif offer["type"] == "equipment":
                order["monthly_change"] = _money(offer["monthly_price"])
                c.setdefault("wifi", {})["coverage"] = "pending_pod_install"
            else:  # device
                order["device_total"] = offer["price_after_trade_in"]
                order["trade_in_credit"] = offer["trade_in_credit"]
                order["full_price"] = offer["full_price"]
                order["monthly_installment"] = offer["monthly_installment_after_trade_in"]
                lines = (c.get("mobile") or {}).get("lines") or []
                if lines:
                    lines[0]["device"] = offer["name"]
            c["orders"].append(order)
        return {"submitted": True, "duplicate": False, **order}

    def _apply_mobile_free_year(self, c: dict[str, Any]) -> bool:
        """Add the free Unlimited line (new line if they have no mobile). Returns True if a line was added."""
        promo = self.catalog["bundle_promos"]["mobile_free_year"]
        mobile = c.setdefault("mobile", {}) or {}
        c["mobile"] = mobile
        lines = mobile.setdefault("lines", [])
        added = not lines
        if added:
            lines.append(
                {
                    "line_id": f"LN-{secrets.token_hex(2).upper()}",
                    "device": "SIM kit (bring your phone)",
                    "device_condition": None,
                }
            )
        mobile["plan"] = promo["line_name"]
        mobile["free_months_remaining"] = promo["months"]
        c["benefits"].append({"type": "mobile_free_year", "months": promo["months"]})
        return added
