"""Regression tests from the quality / data-handling review."""

from __future__ import annotations

import logging
import time

import pytest

from app.config import ROOT_DIR
from app.guardrails import redact, truncate_for_model
from app.logging_setup import AccessLogFilter, PiiMaskingFilter, mask_path
from app.scenarios import load_scenarios
from app.sessions import BUSY_TIMEOUT_S, SessionStore
from app.sim import SimError, SimStore
from tests.conftest import start_session, turn

SCENARIOS = load_scenarios(ROOT_DIR / "scenarios")


# ------------------------------------------------------------- log hygiene
def test_access_log_masks_session_ids_and_tokens() -> None:
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d', None, None)
    record.args = ("1.2.3.4:5", "POST", "/api/sessions/nBvxOkumyqlWHGjWQH4mLos17O8D8JTE/turn", "1.1", 200)
    AccessLogFilter().filter(record)
    assert "nBvx" not in record.getMessage() and "/api/sessions/<id>/turn" in record.getMessage()
    assert mask_path("/chat?ctx=eyJhbGciOi.abc.def&x=1") == "/chat?ctx=<redacted>&x=1"


def test_app_log_masks_session_ids() -> None:
    record = logging.LogRecord("app", logging.INFO, __file__, 1, "path %s", ("/api/sessions/abcdefghijkl/turn",), None)
    PiiMaskingFilter().filter(record)
    assert "abcdefghijkl" not in record.getMessage()


# -------------------------------------------------------- data minimisation
def test_redact_removes_identifiers_at_any_depth() -> None:
    data = {
        "customer_id": "C1",
        "first_name": "Ana",
        "contact": {"email": "a@b.com", "phone": "555", "city": "X"},
        "lines": [{"imei": "35...", "device": "iPhone"}],
    }
    assert redact(data) == {"first_name": "Ana", "contact": {"city": "X"}, "lines": [{"device": "iPhone"}]}
    assert "C1" not in truncate_for_model(data)


def test_browser_events_carry_no_customer_ids(client) -> None:
    sid = start_session(client, "speed-upgrade")
    events = turn(client, sid, {"kickoff": True})
    for e in events:
        if e["type"] == "tool_result":
            assert "customer_id" not in str(e["data"]), e["tool"]


# ---------------------------------------------------------- session safety
def test_stale_busy_flag_expires() -> None:
    store = SessionStore(ttl_s=60, max_sessions=10)
    session, _ = store.create(
        scenario=SCENARIOS["speed-upgrade"], customer_id="x", live_customer_id=None, search_query="q"
    )
    session.mark_busy(True)
    assert session.busy
    session.busy_since = time.time() - BUSY_TIMEOUT_S - 1
    assert not session.busy


def test_concurrent_turn_is_rejected(client) -> None:
    sid = start_session(client, "speed-upgrade")
    session = client.app.state.ctx.sessions.get(sid)
    session.mark_busy(True)
    res = client.post(f"/api/sessions/{sid}/turn", json={"message": "hi"})
    assert res.status_code == 409


def test_evicted_sessions_release_simulated_customers() -> None:
    sim = SimStore()
    store = SessionStore(ttl_s=60, max_sessions=1, on_evict=lambda s: sim.remove_customer(s.customer_id))
    first = sim.add_customer(SCENARIOS["speed-upgrade"].customer, clone=True)
    store.create(scenario=SCENARIOS["speed-upgrade"], customer_id=first, live_customer_id=None, search_query="q")
    second = sim.add_customer(SCENARIOS["speed-upgrade"].customer, clone=True)
    store.create(scenario=SCENARIOS["speed-upgrade"], customer_id=second, live_customer_id=None, search_query="q")
    with pytest.raises(SimError):
        sim.customer_profile(first)
    assert sim.customer_profile(second)["first_name"] == "Priya"


# ---------------------------------------------------------------- devices
@pytest.mark.parametrize("query", ["iPhone 18 Pro Max", "iphone 17 pro", "Galaxy S26", "pro"])
def test_device_lookup_never_substitutes_a_different_model(query: str) -> None:
    sim = SimStore()
    cid = sim.add_customer({"id": "D1", "first_name": "D", "plan_id": "plus-500"})
    with pytest.raises(SimError):
        sim.device_offer(cid, query)


def test_unpriced_storage_is_flagged_not_guessed() -> None:
    sim = SimStore()
    cid = sim.add_customer({"id": "D2", "first_name": "D", "plan_id": "plus-500"})
    pricing = sim.device_offer(cid, "iPhone 18 Pro 1TB")["pricing"]
    assert pricing["priced_storage"] == "256GB" and "Do not estimate" in pricing["note"]


# ------------------------------------------------------------ handoff token
def test_handoff_token_is_not_in_query_string() -> None:
    source = (ROOT_DIR / "web" / "src" / "pages" / "Search.tsx").read_text()
    assert "/chat#ctx=" in source and "/chat?ctx=" not in source


def test_confirmation_requires_a_real_boolean(client) -> None:
    sid = start_session(client, "gateway-fault")
    res = client.post(f"/api/sessions/{sid}/turn", json={"confirmation": {"action_id": "act_123", "approved": "yes"}})
    assert res.status_code == 422


# ------------------------------------------------------------ conduct
def test_system_prompt_has_conduct_guardrails() -> None:
    from app.prompts import BASE_PROMPT

    for phrase in ("Never pressure", "Answer every question", "human specialist", "Stay in scope", "Protect privacy"):
        assert phrase in BASE_PROMPT, phrase


def test_request_for_a_person_is_honoured_immediately(client) -> None:
    sid = start_session(client, "speed-upgrade")
    turn(client, sid, {"kickoff": True})
    events = turn(client, sid, {"message": "I want to talk to a real person"})
    text = " ".join(e["text"] for e in events if e["type"] == "segment_end")
    assert "specialist" in text and not [e for e in events if e["type"] in ("tool_start", "confirm_required")]


def test_off_topic_is_declined_politely(client) -> None:
    sid = start_session(client, "speed-upgrade")
    turn(client, sid, {"kickoff": True})
    events = turn(client, sid, {"message": "Which stocks should I buy?"})
    text = " ".join(e["text"] for e in events if e["type"] == "segment_end")
    assert "outside what I can help with" in text


def test_company_brand_stays_out_of_products_and_data() -> None:
    """Tidelink is the assistant's name only; products and customer data stay unbranded."""
    for path in [ROOT_DIR / "data" / "catalog.yaml", *sorted((ROOT_DIR / "scenarios").glob("*.yaml"))]:
        assert "Tidelink" not in path.read_text(), path.name
