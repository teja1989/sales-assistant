"""Business rules in the simulated backend (these would live in the real APIs too)."""

from __future__ import annotations

import pytest

from app.config import ROOT_DIR
from app.scenarios import load_scenarios
from app.sim import SimError, SimStore

SCENARIOS = load_scenarios(ROOT_DIR / "scenarios")


@pytest.fixture
def store() -> SimStore:
    s = SimStore()
    for sc in SCENARIOS.values():
        s.add_customer(sc.customer)
    return s


def test_offers_blocked_during_fault(store: SimStore) -> None:
    assert store.offers("LUM-1001")["blocked"] is True
    assert store.offers("LUM-2002")["blocked"] is True


def test_order_rejected_while_fault_open(store: SimStore) -> None:
    with pytest.raises(SimError):
        store.submit_order("LUM-1001", "OFR-UPG-GIG-1000")


def test_reboot_clears_fixable_fault_then_offers_unblock(store: SimStore) -> None:
    result = store.reboot_gateway("LUM-1001")
    assert result["issue_resolved"] is True
    offers = store.offers("LUM-1001")
    assert offers["blocked"] is False
    # Light usage: no tier is recommended and the tool says so.
    assert not any(o["recommended"] for o in offers["offers"])
    assert "not recommended" in offers["advisory"]


def test_reboot_refused_during_outage(store: SimStore) -> None:
    with pytest.raises(SimError):
        store.reboot_gateway("LUM-2002")


def test_credit_only_once(store: SimStore) -> None:
    assert store.apply_service_credit("LUM-2002")["amount"] == 10.0
    with pytest.raises(SimError):
        store.apply_service_credit("LUM-2002")


def test_heavy_user_gets_right_sized_recommendation(store: SimStore) -> None:
    offers = store.offers("LUM-3003", "speed")["offers"]
    recommended = [o for o in offers if o["recommended"]]
    assert len(recommended) == 1 and recommended[0]["plan_id"] == "plus-500"
    assert recommended[0]["monthly_change"] == 15.0


def test_order_only_for_eligible_offer_and_idempotent(store: SimStore) -> None:
    with pytest.raises(SimError):
        store.submit_order("LUM-3003", "OFR-UPG-NOT-REAL")
    first = store.submit_order("LUM-3003", "OFR-UPG-PLUS-500")
    again = store.submit_order("LUM-3003", "OFR-UPG-PLUS-500")
    assert first["order_id"] == again["order_id"] and again["duplicate"] is True


def test_weak_wifi_speed_offers_carry_advisory(store: SimStore) -> None:
    assert "mesh pod" in store.offers("LUM-4004", "speed")["advisory"]
    pod = store.offers("LUM-4004", "wifi")["offers"][0]
    assert pod["recommended"] and pod["type"] == "equipment"


def test_clone_isolation(store: SimStore) -> None:
    clone = store.add_customer(SCENARIOS["gateway-fault"].customer, clone=True)
    store.reboot_gateway(clone)
    assert store.diagnostics("LUM-1001")["verdict"] == "gateway_fault"
    assert store.diagnostics(clone)["verdict"] == "healthy"


# ------------------------------------------------------------ mobile + devices
def test_800_and_faster_include_free_mobile_year(store: SimStore) -> None:
    offers = {o["plan_id"]: o for o in store.offers("LUM-5005", "speed")["offers"]}
    assert offers["turbo-800"]["recommended"] is True
    assert offers["turbo-800"]["bundle"]["months"] == 12
    assert all(o["bundle"] for o in offers.values())  # every tier above Plus 500 is 800+


def test_plan_below_800_has_no_bundle(store: SimStore) -> None:
    offers = {o["plan_id"]: o for o in store.offers("LUM-3003", "speed")["offers"]}
    assert offers["plus-500"]["bundle"] is None and offers["turbo-800"]["bundle"]


def test_bundle_order_adds_mobile_line_for_customer_without_mobile(store: SimStore) -> None:
    quote = store.preview_order("LUM-5005", "OFR-UPG-TURBO-800")
    assert quote["new_monthly_price"] == 70.0 and quote["due_today"] == 0.0
    assert quote["included_benefits"]
    order = store.submit_order("LUM-5005", "OFR-UPG-TURBO-800")
    assert order["mobile_line_added"] is True
    assert store.customer_profile("LUM-5005")["mobile"]["free_months_remaining"] == 12


def test_device_offer_trade_in_and_valued_bonus(store: SimStore) -> None:
    offer = store.device_offer("LUM-8008", "Apple 18 Pro")
    trade = offer["trade_in"]
    assert (trade["usual_credit"], trade["valued_customer_bonus_credit"], trade["total_trade_in_credit"]) == (
        800.0,
        200.0,
        1000.0,
    )
    assert offer["offer"]["price_after_trade_in"] == 199.0
    assert offer["device"]["facts_source"].startswith("Apple Newsroom")


def test_no_valued_bonus_for_new_customers(store: SimStore) -> None:
    cid = store.add_customer(
        {
            "id": "NEW-1",
            "first_name": "N",
            "plan_id": "plus-500",
            "tenure_months": 3,
            "mobile": {"lines": [{"device": "iPhone 15 Pro", "device_condition": "good"}]},
        }
    )
    trade = store.device_offer(cid, "iphone 18 pro")["trade_in"]
    assert trade["usual_credit"] == 800.0 and trade["valued_customer_bonus_credit"] == 0.0


def test_unknown_device_is_a_clean_error(store: SimStore) -> None:
    with pytest.raises(SimError, match="isn't in our catalog"):
        store.device_offer("LUM-8008", "Galaxy Z Fold 12")


def test_device_order(store: SimStore) -> None:
    order = store.submit_order("LUM-8008", "OFR-DEV-IPHONE-18-PRO")
    assert order["type"] == "device" and order["trade_in_credit"] == 1000.0 and order["full_price"] == 1199.0


def test_storm_pass_once_and_only_with_alert(store: SimStore) -> None:
    alerts = store.service_alerts("LUM-7007")
    assert alerts["alerts"] and alerts["courtesy"]["storm_data_pass_eligible"]
    assert store.activate_storm_pass("LUM-7007")["lines_covered"] == 3
    with pytest.raises(SimError):
        store.activate_storm_pass("LUM-7007")
    with pytest.raises(SimError):
        store.activate_storm_pass("LUM-3003")  # no alert, no mobile


# ------------------------------------------------------------ account checkup
def test_checkup_finds_good_and_bad(store: SimStore) -> None:
    result = store.account_checkup("LUM-9100")
    ids = {a["id"] for a in result["attention"]}
    assert {"overpaying", "promo_ending", "autopay", "firmware", "card_expiring", "unused-tv-box-legacy"} <= ids
    assert "No outages in your area" in result["good"]
    assert result["summary"]["potential_monthly_savings"] == 30.0
    severities = [a["severity"] for a in result["attention"]]
    assert severities == sorted(severities, key=["high", "medium", "low", "info"].index)


def test_healthy_account_has_mostly_good_news(store: SimStore) -> None:
    result = store.account_checkup("LUM-6006")  # streaming household, no billing issues
    assert not [a for a in result["attention"] if a["severity"] == "high"]
    assert result["good"]


def test_checkup_fixes_are_idempotent_and_safe(store: SimStore) -> None:
    assert store.enroll_autopay("LUM-9100")["monthly_discount"] == 5.0
    with pytest.raises(SimError):
        store.enroll_autopay("LUM-9100")
    assert store.return_unused_equipment("LUM-9100", "tv-box-legacy")["monthly_fee_removed"] == 10.0
    with pytest.raises(SimError):
        store.return_unused_equipment("LUM-9100", "tv-box-legacy")
    assert store.update_gateway_firmware("LUM-9100")["to_version"] == "6.4.0"
    with pytest.raises(SimError):
        store.update_gateway_firmware("LUM-9100")
    ids = {a["id"] for a in store.account_checkup("LUM-9100")["attention"]}
    assert not ids & {"autopay", "firmware", "unused-tv-box-legacy"}


def test_right_sizing_offer_and_switch(store: SimStore) -> None:
    offers = store.offers("LUM-9100", "save")["offers"]
    pick = next(o for o in offers if o["recommended"])
    assert pick["plan_id"] == "plus-500" and pick["monthly_savings"] == 15.0
    quote = store.preview_order("LUM-9100", pick["offer_id"])
    assert quote["current_monthly_price"] == 70.0 and quote["price_if_unchanged"] == 85.0
    order = store.submit_order("LUM-9100", pick["offer_id"])
    assert order["type"] == "plan_change" and order["monthly_savings"] == 15.0
    assert store.customer_profile("LUM-9100")["plan"]["id"] == "plus-500"
