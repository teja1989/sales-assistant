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
