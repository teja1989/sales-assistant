"""MCP server contract: tool names, read/action classification and LLM-facing schemas."""

from __future__ import annotations

import re
from typing import Any

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


def _tool(name: str, props: dict | None = None, description: str = "") -> Any:
    from types import SimpleNamespace as NS

    return NS(
        name=name,
        title=None,
        description=description,
        input_schema={"type": "object", "properties": props or {}},
        annotations=None,  # we never rely on server annotations
    )


def _gw_live(listings: dict[str, list], **env: str) -> McpGateway:
    """Gateway over fake live servers: listings = {server_name: [tools]}."""
    servers = ",".join(f"{n}=https://{n}.example.com/mcp" for n in listings)
    gw = _gw_with_customer(DATA_MODE="sim", LIVE_MCP_SERVERS=servers, **env)

    async def list_server(server):  # noqa: ANN001
        tools = listings[server.name]
        if isinstance(tools, Exception):
            raise tools
        return [gw._live_spec(server.name, t) for t in tools]

    gw._list_server = list_server
    return gw


def test_read_or_action_from_tool_name_with_overrides() -> None:
    gw = _gw_with_customer(LIVE_READ_TOOLS="runDiagnostics", LIVE_ACTION_TOOLS="getAndResetToken")
    kinds = {
        n: gw._live_spec("s", _tool(n)).read_only
        for n in (
            "getAccount",
            "get_customer_profile",
            "listOrders",
            "check_area_outage",
            "searchOffers",
            "sales.getOffers",
            "orders__viewOrder",
            "submitOrder",
            "rebootGateway",
            "applyCredit",
            "runDiagnostics",
            "getAndResetToken",
        )
    }
    assert kinds == {
        "getAccount": True,
        "get_customer_profile": True,
        "listOrders": True,
        "check_area_outage": True,
        "searchOffers": True,
        "sales.getOffers": True,
        "orders__viewOrder": True,
        "submitOrder": False,
        "rebootGateway": False,
        "applyCredit": False,
        "runDiagnostics": True,  # override: lookup
        "getAndResetToken": False,  # override: action
    }


@pytest.mark.anyio
async def test_many_servers_merge_into_one_toolset() -> None:
    gw = _gw_live(
        {
            "orders": [_tool("getOrder", {"accountId": {}}), _tool("getStatus")],
            "sales": [_tool("getOffers", {"customer_id": {}}), _tool("getStatus")],
            "gateway": [_tool("billing.getBill"), _tool("billing/payBill"), _tool("support:openTicket")],
        }
    )
    specs = await gw.refresh_live_specs(force=True)
    # Unique, model-safe names; clashing names are prefixed with their server.
    assert set(specs) == {
        "getOrder",
        "orders__getStatus",
        "getOffers",
        "sales__getStatus",
        "billing_getBill",
        "billing_payBill",
        "support_openTicket",
    }
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", n) for n in specs)
    # Each tool remembers its server and real name; gateway prefixes become teams.
    assert (specs["orders__getStatus"].server, specs["orders__getStatus"].remote_name) == ("orders", "getStatus")
    assert (specs["billing_payBill"].server, specs["billing_payBill"].remote_name) == ("gateway", "billing/payBill")
    assert specs["billing_getBill"].group == "billing" and specs["support_openTicket"].group == "support"
    assert specs["getOrder"].group == "orders"
    assert specs["getOrder"].hidden == ("accountId",) and specs["getOffers"].hidden == ("customer_id",)
    systems = {s["name"]: s for s in gw.systems()}
    assert systems["gateway"]["groups"] == ["billing", "support"] and len(systems["orders"]["tools"]) == 2


@pytest.mark.anyio
async def test_one_server_down_keeps_the_others() -> None:
    gw = _gw_live({"orders": [_tool("getOrder")], "sales": ConnectionError("refused")})
    specs = await gw.refresh_live_specs(force=True)
    assert set(specs) == {"getOrder"}
    systems = {s["name"]: s for s in gw.systems()}
    assert systems["orders"]["reachable"] is True and systems["sales"]["reachable"] is False
    assert "ConnectionError" in systems["sales"]["error"]
    with pytest.raises(ConnectionError):
        await _gw_live({"a": ConnectionError("x")}).refresh_live_specs(force=True)


@pytest.mark.anyio
async def test_calls_route_to_the_owning_server_and_are_counted() -> None:
    from app.mcp_gateway import ToolOutcome

    gw = _gw_live({"orders": [_tool("getStatus")], "sales": [_tool("getStatus")]})
    await gw.refresh_live_specs(force=True)
    seen = []

    async def call_live(spec, arguments, started):  # noqa: ANN001
        seen.append((spec.server, spec.remote_name))
        if spec.server == "sales":
            raise ConnectionError("refused")
        return ToolOutcome(spec.name, "live", {"ok": True}, False, 7)

    gw._call_live = call_live
    ok = await gw.call("live", "orders__getStatus", {})
    down = await gw.call("live", "sales__getStatus", {})
    assert seen == [("orders", "getStatus"), ("sales", "getStatus")]
    assert ok.meta == {"server": "orders", "group": "orders"} and not ok.is_error
    assert down.is_error and "sales system is unavailable" in down.data["error"]  # never simulated data
    systems = {s["name"]: s for s in gw.systems()}
    assert systems["orders"]["calls"] == 1 and systems["orders"]["avg_ms"] == 7
    assert systems["sales"]["errors"] == 1 and systems["sales"]["reachable"] is False


def test_per_server_tokens_and_config() -> None:
    from app.config import ConfigError

    gw = _gw_with_customer(
        LIVE_MCP_URL="https://one.example.com/mcp",
        LIVE_MCP_NAME="Orders",
        LIVE_MCP_TOKEN="abc",
        LIVE_MCP_SERVERS="sales=https://sales.example.com/mcp, billing=billing.example.com/mcp",
        LIVE_MCP_TOKEN_SALES="Basic xyz",
    )
    servers = {s.name: s for s in gw.settings.live_servers}
    assert set(servers) == {"orders", "sales", "billing"}
    assert gw._live_headers(servers["orders"])["Authorization"] == "Bearer abc"
    assert gw._live_headers(servers["sales"])["Authorization"] == "Basic xyz"
    assert "Authorization" not in gw._live_headers(servers["billing"])
    assert servers["billing"].url == "https://billing.example.com/mcp"
    with pytest.raises(ConfigError, match="unique"):
        _gw_with_customer(LIVE_MCP_SERVERS="a=https://a.example.com/mcp,a=https://b.example.com/mcp")
    with pytest.raises(ConfigError, match="name=url"):
        _gw_with_customer(LIVE_MCP_SERVERS="https://a.example.com/mcp")


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
