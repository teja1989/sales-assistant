"""The MCP server: Tidelink's account, network and order tools.

This is the same server whether it is reached:
* in-process by the chat orchestrator (direct MCP dispatch, no HTTP hop), or
* over Streamable HTTP at /mcp by any MCP client (MCP Inspector, another agent,
  Muse once hosted publicly), protected by a bearer token.

Tool annotations matter: `readOnlyHint=True` tools run freely; every other tool
is treated by the orchestrator as an action that needs the customer's explicit
confirmation in the UI before it executes.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from app.sim import SimError, SimStore

CustomerId = Annotated[str, Field(description="Customer account id.", min_length=3, max_length=64)]

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
ACTION = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)


def build_mcp_server(store: SimStore, name: str = "tidelink-network") -> MCPServer:
    server = MCPServer(
        name,
        title="Tidelink account and network tools",
        instructions=(
            "Tools for an internet and mobile provider: customer profile, outage and weather "
            "alerts, line diagnostics, usage, plan/equipment/device offers, order preview and actions "
            "(reboot, technician, credit, storm data pass, order). Action tools change customer state; "
            "callers must get explicit customer confirmation before invoking them."
        ),
        version="0.1.0",
    )

    def run(fn: Any, *args: Any) -> dict[str, Any]:
        try:
            return fn(*args)
        except SimError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(
        title="Get customer profile",
        description="Customer's first name, current internet plan, monthly price, equipment and tenure.",
        annotations=READ,
    )
    def get_customer_profile(customer_id: CustomerId) -> dict[str, Any]:
        return run(store.customer_profile, customer_id)

    @server.tool(
        title="Check area outage",
        description="Whether a network outage affects the customer's service area, with cause and restore ETA.",
        annotations=READ,
    )
    def check_area_outage(customer_id: CustomerId) -> dict[str, Any]:
        return run(store.area_outage, customer_id)

    @server.tool(
        title="Run line diagnostics",
        description=(
            "Remote health check of the customer's gateway, line signal and Wi-Fi. Returns a verdict: "
            "area_outage, gateway_fault, signal_issue, wifi_coverage, plan_capacity or healthy."
        ),
        annotations=READ,
    )
    def run_line_diagnostics(customer_id: CustomerId) -> dict[str, Any]:
        return run(store.diagnostics, customer_id)

    @server.tool(
        title="Get usage profile",
        description="How much of the current plan's speed the household uses at peak, devices and activities.",
        annotations=READ,
    )
    def get_usage_profile(customer_id: CustomerId) -> dict[str, Any]:
        return run(store.usage, customer_id)

    @server.tool(
        title="Get eligible offers",
        description=(
            "Offers the customer is eligible for right now, with exact prices. need='speed' for plan "
            "upgrades, need='wifi' for whole-home Wi-Fi equipment. Returns blocked=true while a service "
            "issue is open; never offer anything in that case."
        ),
        annotations=READ,
    )
    def get_eligible_offers(
        customer_id: CustomerId,
        need: Annotated[Literal["speed", "wifi"], Field(description="What the customer needs.")] = "speed",
    ) -> dict[str, Any]:
        return run(store.offers, customer_id, need)

    @server.tool(
        title="Get device offer",
        description=(
            "Facts, price and the customer's personalized trade-in offer for a phone (e.g. 'iPhone 18 Pro'). "
            "Use these facts only; do not add specs from memory. Returns an offer_id for preview_order."
        ),
        annotations=READ,
    )
    def get_device_offer(
        customer_id: CustomerId,
        model: Annotated[str, Field(description="Device the customer asked about.", max_length=80)],
    ) -> dict[str, Any]:
        return run(store.device_offer, customer_id, model)

    @server.tool(
        title="Check service alerts",
        description=(
            "Weather and network alerts for the customer's area (e.g. an incoming storm) and any courtesy "
            "benefit they qualify for, such as a free storm data pass."
        ),
        annotations=READ,
    )
    def check_service_alerts(customer_id: CustomerId) -> dict[str, Any]:
        return run(store.service_alerts, customer_id)

    @server.tool(
        title="Preview order",
        description=(
            "Price quote for an offer_id before ordering: line items, promos, included benefits (e.g. free "
            "mobile year), trade-in credit, monthly before/after and amount due today. Always preview before "
            "submit_upgrade_order."
        ),
        annotations=READ,
    )
    def preview_order(
        customer_id: CustomerId,
        offer_id: Annotated[
            str, Field(description="offer_id from get_eligible_offers or get_device_offer.", max_length=64)
        ],
    ) -> dict[str, Any]:
        return run(store.preview_order, customer_id, offer_id)

    @server.tool(
        title="Activate storm data pass",
        description=(
            "Turn on free unlimited mobile data for all lines during a storm alert (no charge). Requires confirmation."
        ),
        annotations=ACTION,
    )
    def activate_storm_data_pass(customer_id: CustomerId) -> dict[str, Any]:
        return run(store.activate_storm_pass, customer_id)

    @server.tool(
        title="Reboot gateway",
        description="Remotely restart the customer's gateway (about 2 minutes offline). Requires confirmation.",
        annotations=ACTION,
    )
    def reboot_gateway(customer_id: CustomerId) -> dict[str, Any]:
        return run(store.reboot_gateway, customer_id)

    @server.tool(
        title="Schedule technician",
        description="Book the first available technician visit for an issue that cannot be fixed remotely.",
        annotations=ACTION,
    )
    def schedule_technician(
        customer_id: CustomerId,
        issue_summary: Annotated[str, Field(description="One-sentence issue summary.", max_length=200)],
    ) -> dict[str, Any]:
        return run(store.schedule_technician, customer_id, issue_summary)

    @server.tool(
        title="Apply outage credit",
        description="Apply the standard service credit for an active area outage (once per outage).",
        annotations=ACTION,
    )
    def apply_service_credit(customer_id: CustomerId) -> dict[str, Any]:
        return run(store.apply_service_credit, customer_id)

    @server.tool(
        title="Submit order",
        description=(
            "Place the order for an offer_id (plan upgrade, Wi-Fi equipment or device) after preview_order. "
            "Requires confirmation."
        ),
        annotations=ACTION,
    )
    def submit_upgrade_order(
        customer_id: CustomerId,
        offer_id: Annotated[str, Field(description="offer_id that was previewed.", max_length=64)],
    ) -> dict[str, Any]:
        return run(store.submit_order, customer_id, offer_id)

    return server
