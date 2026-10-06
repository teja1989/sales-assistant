"""Every scenario YAML is played end to end and checked against its `expected:` block.

New scenarios are picked up automatically: add a YAML file and this test covers it.
"""

from __future__ import annotations

import pytest

from app.config import ROOT_DIR
from app.scenarios import load_scenarios
from tests.conftest import start_session, turn

SCENARIOS = load_scenarios(ROOT_DIR / "scenarios")

OUTCOME_METRIC = {
    "truck_roll_avoided": "truck_rolls_avoided",
    "credit_applied": "credits_applied",
    "offer_accepted": "offers_accepted",
    "technician_booked": "technician_visits_booked",
    "device_sold": "devices_sold",
    "storm_pass": "storm_data_passes",
}

OFFER_TOOLS = ("get_eligible_offers", "get_device_offer")


def play(client, scenario_id: str) -> tuple[list[dict], list[str]]:
    """Drive a conversation like a cooperative customer: approve actions, say yes to offers."""
    sid = start_session(client, scenario_id)
    events = turn(client, sid, {"kickoff": True})
    history = list(events)
    for _ in range(4):
        pending = [e for e in events if e["type"] == "confirm_required"]
        if pending:
            events = turn(client, sid, {"confirmation": {"action_id": pending[-1]["action_id"], "approved": True}})
        elif any(e["type"] == "tool_result" and e["tool"] in OFFER_TOOLS for e in history) and not any(
            e["type"] == "tool_result" and e["tool"] == "submit_upgrade_order" for e in history
        ):
            events = turn(client, sid, {"message": "Yes please, go ahead"})
        else:
            break
        history += events
    called = [e["tool"] for e in history if e["type"] == "tool_result"]
    return history, called


@pytest.mark.parametrize("scenario_id", sorted(SCENARIOS))
def test_scenario_meets_expectations(client, scenario_id: str) -> None:
    scenario = SCENARIOS[scenario_id]
    history, called = play(client, scenario_id)

    assert not [e for e in history if e["type"] == "error"], history
    for tool in scenario.expected.must_call:
        assert tool in called, f"{scenario_id}: expected {tool} to be called; got {called}"
    for tool in scenario.expected.must_not_call:
        assert tool not in called, f"{scenario_id}: {tool} must not be called"
    if scenario.expected.confirm_action:
        confirms = [e["tool"] for e in history if e["type"] == "confirm_required"]
        assert scenario.expected.confirm_action in confirms
        assert scenario.expected.confirm_action in called, "confirmed action should have executed"
    if scenario.expected.outcome:
        metrics = client.get("/api/metrics").json()
        assert metrics[OUTCOME_METRIC[scenario.expected.outcome]] >= 1, metrics


def test_upsell_blocked_while_fault_open(client) -> None:
    """Asking about upgrades before the gateway is fixed must not produce offers."""
    sid = start_session(client, "gateway-fault")
    turn(client, sid, {"kickoff": True})
    events = turn(client, sid, {"message": "Should I upgrade my plan?"})
    offers = [e for e in events if e["type"] == "tool_result" and e["tool"] == "get_eligible_offers"]
    assert offers and offers[0]["data"]["blocked"] is True
    assert client.get("/api/metrics").json()["upsell_blocked_fault_first"] == 1


def test_declined_action_does_not_execute(client) -> None:
    sid = start_session(client, "gateway-fault")
    events = turn(client, sid, {"kickoff": True})
    action = next(e for e in events if e["type"] == "confirm_required")
    events = turn(client, sid, {"confirmation": {"action_id": action["action_id"], "approved": False}})
    assert not [e for e in events if e["type"] == "tool_result" and e["tool"] == "reboot_gateway"]
    assert client.get("/api/metrics").json()["actions_declined"] == 1


def test_confirmation_cannot_be_replayed(client) -> None:
    sid = start_session(client, "gateway-fault")
    events = turn(client, sid, {"kickoff": True})
    action = next(e for e in events if e["type"] == "confirm_required")
    body = {"confirmation": {"action_id": action["action_id"], "approved": True}}
    first = turn(client, sid, body)
    second = turn(client, sid, body)
    assert any(e["type"] == "tool_result" and e["tool"] == "reboot_gateway" for e in first)
    assert not any(e["type"] == "tool_result" and e["tool"] == "reboot_gateway" for e in second)
    assert any(e["type"] == "notice" for e in second)


def test_sessions_are_isolated(client) -> None:
    """Two sessions on the same scenario get separate simulated customers."""
    a = start_session(client, "gateway-fault")
    b = start_session(client, "gateway-fault")
    events = turn(client, a, {"kickoff": True})
    action = next(e for e in events if e["type"] == "confirm_required")
    turn(client, a, {"confirmation": {"action_id": action["action_id"], "approved": True}})
    events_b = turn(client, b, {"kickoff": True})
    diag_b = next(e for e in events_b if e["type"] == "tool_result" and e["tool"] == "run_line_diagnostics")
    assert diag_b["data"]["verdict"] == "gateway_fault"
