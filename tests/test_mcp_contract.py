"""MCP server contract: tool names, read/action classification and LLM-facing schemas."""

from __future__ import annotations

import pytest

from app.config import ROOT_DIR
from app.mcp_gateway import McpGateway
from app.mcp_server import build_mcp_server
from app.scenarios import ScenarioError, load_scenarios
from app.sim import SimStore
from tests.conftest import make_settings

READ_TOOLS = {
    "get_customer_profile",
    "check_area_outage",
    "run_line_diagnostics",
    "get_usage_profile",
    "get_eligible_offers",
    "get_device_offer",
    "check_service_alerts",
    "preview_order",
    "get_account_checkup",
}
ACTION_TOOLS = {
    "reboot_gateway",
    "schedule_technician",
    "apply_service_credit",
    "submit_upgrade_order",
    "activate_storm_data_pass",
    "enroll_autopay",
    "return_unused_equipment",
    "update_gateway_firmware",
}


@pytest.fixture
def gateway() -> McpGateway:
    return McpGateway(make_settings(), build_mcp_server(SimStore()))


@pytest.mark.anyio
async def test_tool_inventory_and_classification(gateway: McpGateway) -> None:
    specs = await gateway.load_specs()
    assert set(specs) == READ_TOOLS | ACTION_TOOLS
    assert {n for n, s in specs.items() if s.read_only} == READ_TOOLS


@pytest.mark.anyio
async def test_llm_schema_hides_customer_id(gateway: McpGateway) -> None:
    specs = await gateway.load_specs()
    for spec in specs.values():
        assert "customer_id" in spec.input_schema["properties"]
        params = spec.llm_schema()["function"]["parameters"]
        assert "customer_id" not in params.get("properties", {})
        assert "customer_id" not in params.get("required", [])


def _gw_with_customer(**env: str) -> McpGateway:
    store = SimStore()
    store.add_customer({"id": "C-1", "first_name": "T", "plan_id": "plus-500"})
    return McpGateway(make_settings(**env), build_mcp_server(store))


@pytest.mark.anyio
async def test_without_live_url_everything_is_simulated() -> None:
    gw = _gw_with_customer(LIVE_MCP_URL="")
    assert gw.settings.data_source == "sim"
    out = await gw.call("sim", "get_customer_profile", {"customer_id": "C-1"})
    assert out.source == "sim" and not out.fallback and out.data["first_name"] == "T"


@pytest.mark.anyio
async def test_live_tool_missing_on_server_falls_back_to_sim(monkeypatch) -> None:
    gw = _gw_with_customer(LIVE_MCP_URL="https://live.example.com/mcp")
    assert gw.settings.data_source == "live"

    async def listing() -> list[str]:
        return ["some_other_tool"]

    monkeypatch.setattr(gw, "list_live_tools", listing)
    out = await gw.call("live", "get_customer_profile", {"customer_id": "LIVE-9"}, sim_customer_id="C-1")
    assert out.source == "sim" and out.fallback and out.data["first_name"] == "T"
    assert "not on the live server" in out.meta["fallback_reason"]


@pytest.mark.anyio
async def test_live_error_or_outage_falls_back_to_sim(monkeypatch) -> None:
    from app.mcp_gateway import ToolOutcome

    gw = _gw_with_customer(LIVE_MCP_URL="https://live.example.com/mcp")

    async def listing() -> list[str]:
        return ["get_customer_profile"]

    async def live_error(name, arguments, started):  # noqa: ANN001
        return ToolOutcome(name, "live", {"error": "customer not found"}, True, 5, error="not_found")

    async def live_down(name, arguments, started):  # noqa: ANN001
        raise ConnectionError("connection refused")

    monkeypatch.setattr(gw, "list_live_tools", listing)
    monkeypatch.setattr(gw, "_call_live", live_error)
    out = await gw.call("live", "get_customer_profile", {"customer_id": "LIVE-9"}, sim_customer_id="C-1")
    assert out.source == "sim" and out.fallback and "live returned an error" in out.meta["fallback_reason"]

    monkeypatch.setattr(gw, "_call_live", live_down)
    out = await gw.call("live", "get_customer_profile", {"customer_id": "LIVE-9"}, sim_customer_id="C-1")
    assert out.source == "sim" and out.fallback and "live call failed" in out.meta["fallback_reason"]
    assert gw._live_tools is None  # re-list after an outage


@pytest.mark.anyio
async def test_live_success_is_used(monkeypatch) -> None:
    from app.mcp_gateway import ToolOutcome

    gw = _gw_with_customer(LIVE_MCP_URL="https://live.example.com/mcp", LIVE_MCP_TOKEN="abc")
    assert gw._live_headers()["Authorization"] == "Bearer abc"
    assert _gw_with_customer(LIVE_MCP_TOKEN="Basic xyz")._live_headers()["Authorization"] == "Basic xyz"

    async def listing() -> list[str]:
        return ["get_customer_profile"]

    async def live_ok(name, arguments, started):  # noqa: ANN001
        return ToolOutcome(name, "live", {"first_name": "Real"}, False, 5)

    monkeypatch.setattr(gw, "list_live_tools", listing)
    monkeypatch.setattr(gw, "_call_live", live_ok)
    out = await gw.call("live", "get_customer_profile", {"customer_id": "LIVE-9"}, sim_customer_id="C-1")
    assert out.source == "live" and not out.fallback and out.data["first_name"] == "Real"


@pytest.mark.anyio
async def test_unknown_customer_is_a_clean_tool_error(gateway: McpGateway) -> None:
    out = await gateway.call("sim", "get_customer_profile", {"customer_id": "NOPE-1"})
    assert out.is_error and "not found" in out.data["error"].lower()


def test_all_scenarios_reference_real_tools() -> None:
    known = READ_TOOLS | ACTION_TOOLS
    scenarios = load_scenarios(ROOT_DIR / "scenarios", known)
    assert len(scenarios) >= 4


def test_scenario_validation_errors(tmp_path) -> None:
    (tmp_path / "bad.yaml").write_text(
        "id: Bad_Id\ntitle: t\ndescription: d\nintent: speed\nsearch_query: q\nassistant_brief: b\n"
        "tools: [get_customer_profile]\ncustomer: {id: X1, first_name: A, plan_id: plus-500}\n"
    )
    with pytest.raises(ScenarioError):
        load_scenarios(tmp_path)
