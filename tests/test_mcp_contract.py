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
}
ACTION_TOOLS = {"reboot_gateway", "schedule_technician", "apply_service_credit", "submit_upgrade_order"}


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


@pytest.mark.anyio
async def test_live_without_url_falls_back_to_sim() -> None:
    store = SimStore()
    store.add_customer({"id": "C-1", "first_name": "T", "plan_id": "plus-500"})
    gw = McpGateway(make_settings(LIVE_MCP_URL="", LIVE_FALLBACK_TO_SIM="true"), build_mcp_server(store))
    out = await gw.call("live", "get_customer_profile", {"customer_id": "LIVE-9"}, sim_customer_id="C-1")
    assert out.source == "sim" and out.fallback and out.data["first_name"] == "T"


@pytest.mark.anyio
async def test_live_without_url_and_no_fallback_errors() -> None:
    gw = McpGateway(make_settings(LIVE_MCP_URL="", LIVE_FALLBACK_TO_SIM="false"), build_mcp_server(SimStore()))
    out = await gw.call("live", "get_customer_profile", {"customer_id": "x"})
    assert out.is_error and out.source == "live"


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
