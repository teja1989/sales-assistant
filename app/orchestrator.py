"""The agent loop: model <-> MCP tools, streamed to the browser as events.

Security model (enforced here, independent of the prompt):
1. Tool allowlist per scenario. The model only sees and can only call listed tools.
2. Identity injection. `customer_id` is hidden from the model and always set
   server-side from the session, so a manipulated model cannot read or change
   another account (prevents confused-deputy attacks).
3. Argument hygiene. Arguments must be a JSON object; unknown keys are dropped.
4. Human-in-the-loop. Any tool not marked readOnly becomes a pending action;
   it executes only when the customer taps Confirm (a separate API call the
   model cannot make).
5. Offer integrity. Orders are accepted only for offer_ids previously returned
   by get_eligible_offers in this session.
6. Price verification. Dollar amounts in replies must match tool results;
   others are replaced before the turn ends.
7. Bounded loops. At most MAX_TOOL_ROUNDS model rounds per turn.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import AsyncIterator
from typing import Any

from app.config import Settings
from app.guardrails import check_prices, collect_amounts, truncate_for_model
from app.llm.base import LlmClient, LlmError, TextDelta, ToolCall, TurnComplete
from app.mcp_gateway import McpGateway, ToolOutcome, ToolSpec
from app.metrics import Metrics
from app.prompts import KICKOFF_MESSAGE
from app.sessions import PendingAction, Session

log = logging.getLogger(__name__)

Event = dict[str, Any]


class Orchestrator:
    def __init__(
        self, settings: Settings, llm: LlmClient, gateway: McpGateway, metrics: Metrics, catalog: dict[str, Any]
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.gateway = gateway
        self.metrics = metrics
        self.catalog = catalog

    # --------------------------------------------------------------- helpers
    def _specs(self, session: Session) -> dict[str, ToolSpec]:
        specs = self.gateway.specs
        return {name: specs[name] for name in session.scenario.tools if name in specs}

    def tool_schemas(self, session: Session) -> list[dict[str, Any]]:
        return [spec.llm_schema() for spec in self._specs(session).values()]

    def _source(self, session: Session, tool: str) -> str:
        return session.scenario.source_for(tool, self.settings.data_source_override)

    def _clean_args(self, spec: ToolSpec, raw: str) -> dict[str, Any]:
        try:
            args = json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("arguments were not valid JSON") from exc
        if not isinstance(args, dict):
            raise ValueError("arguments must be a JSON object")
        allowed = set(spec.input_schema.get("properties", {})) - {"customer_id"}
        return {k: v for k, v in args.items() if k in allowed}

    def _action_summary(self, session: Session, tool: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if tool == "reboot_gateway":
            return "Restart your gateway. You'll be offline for about 2 minutes.", {}
        if tool == "schedule_technician":
            return "Book the first available technician visit at no charge.", {"issue": args.get("issue_summary", "")}
        if tool == "apply_service_credit":
            amount = self.catalog.get("credits", {}).get("outage_credit")
            details = {"amount": amount} if amount is not None else {}
            return "Add an outage service credit to your next bill.", details
        if tool == "submit_upgrade_order":
            offer = session.offers_seen.get(args.get("offer_id", ""), {})
            details = {
                k: offer.get(k)
                for k in ("name", "download_mbps", "monthly_price", "monthly_change", "promo", "terms", "type")
                if offer.get(k) is not None
            }
            verb = "Add" if offer.get("type") == "equipment" else "Upgrade to"
            return f"{verb} {offer.get('name', 'the selected offer')}.", details
        return "Run this action on your account.", {}

    def _record(self, session: Session, outcome: ToolOutcome) -> None:
        if not outcome.is_error:
            collect_amounts(outcome.data, session.verified_amounts)
            if outcome.tool == "get_eligible_offers" and not outcome.data.get("blocked"):
                for offer in outcome.data.get("offers", []):
                    if isinstance(offer, dict) and offer.get("offer_id"):
                        session.offers_seen[str(offer["offer_id"])] = offer
        source = outcome.source + (" (fallback)" if outcome.fallback else "")
        self.metrics.tool_result(session.id, outcome.tool, source, outcome.data, outcome.latency_ms, outcome.is_error)

    def _result_event(self, call_id: str, spec: ToolSpec, outcome: ToolOutcome) -> Event:
        return {
            "type": "tool_result",
            "call_id": call_id,
            "tool": outcome.tool,
            "title": spec.title,
            "source": outcome.source,
            "fallback": outcome.fallback,
            "latency_ms": outcome.latency_ms,
            "is_error": outcome.is_error,
            "data": outcome.data,
        }

    async def _execute(self, session: Session, spec: ToolSpec, args: dict[str, Any]) -> ToolOutcome:
        source = self._source(session, spec.name)
        full_args = {**args, "customer_id": session.customer_id_for(source)}
        return await self.gateway.call(
            source,
            spec.name,
            full_args,  # type: ignore[arg-type]
            sim_customer_id=session.customer_id,
        )

    # ------------------------------------------------------------ main entry
    async def run_turn(
        self,
        session: Session,
        *,
        user_text: str | None = None,
        kickoff: bool = False,
        confirmation: dict[str, Any] | None = None,
    ) -> AsyncIterator[Event]:
        yield {"type": "turn_start"}
        try:
            if confirmation is not None:
                async for event in self._handle_confirmation(session, confirmation):
                    yield event
            elif kickoff:
                session.messages.append({"role": "user", "content": KICKOFF_MESSAGE})
            elif user_text is not None:
                session.messages.append({"role": "user", "content": user_text})

            async for event in self._agent_loop(session):
                yield event
        except LlmError as exc:
            log.error("LLM failure in session: %s", exc)
            yield {
                "type": "error",
                "code": "llm_unavailable",
                "message": "I'm having trouble thinking right now. Please try again in a moment.",
            }
        except Exception:  # noqa: BLE001 - never leak internals to the browser
            log.exception("Unexpected orchestrator failure")
            yield {"type": "error", "code": "internal", "message": "Something went wrong on our side."}
        yield {"type": "done", "pending_actions": list(session.pending)}

    async def _handle_confirmation(self, session: Session, confirmation: dict[str, Any]) -> AsyncIterator[Event]:
        action_id = str(confirmation.get("action_id", ""))
        approved = bool(confirmation.get("approved"))
        pending = session.pending.pop(action_id, None)
        if pending is None:
            yield {"type": "notice", "message": "That request already expired or was handled."}
            return
        spec = self.gateway.specs[pending.tool]
        call_id = f"call_{secrets.token_hex(6)}"
        session.messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": pending.tool, "arguments": json.dumps(pending.arguments)},
                    }
                ],
            }
        )
        self.metrics.confirmation(session.id, pending.tool, approved)
        yield {"type": "action_resolved", "action_id": action_id, "approved": approved, "tool": pending.tool}
        if not approved:
            session.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": json.dumps({"status": "declined_by_customer"}),
                }
            )
            return
        yield {
            "type": "tool_start",
            "call_id": call_id,
            "tool": pending.tool,
            "title": spec.title,
            "source": self._source(session, pending.tool),
        }
        outcome = await self._execute(session, spec, pending.arguments)
        self._record(session, outcome)
        yield self._result_event(call_id, spec, outcome)
        session.messages.append({"role": "tool", "tool_call_id": call_id, "content": truncate_for_model(outcome.data)})

    async def _agent_loop(self, session: Session) -> AsyncIterator[Event]:
        specs = self._specs(session)
        tools = self.tool_schemas(session)
        for _ in range(self.settings.max_tool_rounds):
            segment = f"seg_{secrets.token_hex(4)}"
            parts: list[str] = []
            complete: TurnComplete | None = None
            async for event in self.llm.stream(session.messages, tools):
                if isinstance(event, TextDelta):
                    parts.append(event.text)
                    yield {"type": "text", "segment": segment, "delta": event.text}
                else:
                    complete = event
            text = "".join(parts).strip()
            if text:
                cleaned, unverified = check_prices(text, session.verified_amounts)
                if unverified:
                    self.metrics.guardrail(session.id, "unverified_price")
                    yield {
                        "type": "guardrail",
                        "kind": "unverified_price",
                        "segment": segment,
                        "message": "Removed a price that didn't come from our systems.",
                    }
                    text = cleaned
                yield {"type": "segment_end", "segment": segment, "text": text}
            calls = complete.tool_calls if complete else []
            assistant: dict[str, Any] = {"role": "assistant", "content": text or None}
            if calls:
                assistant["tool_calls"] = [
                    {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                    for c in calls
                ]
            session.messages.append(assistant)
            if not calls:
                return
            async for event in self._handle_calls(session, specs, calls):
                yield event
        log.warning("Max tool rounds reached for session")
        self.metrics.guardrail(session.id, "max_rounds")
        yield {
            "type": "segment_end",
            "segment": "seg_limit",
            "text": "Let me connect you with a specialist who can take this further.",
        }

    async def _handle_calls(
        self, session: Session, specs: dict[str, ToolSpec], calls: list[ToolCall]
    ) -> AsyncIterator[Event]:
        reads: list[tuple[ToolCall, ToolSpec, dict[str, Any]]] = []
        results: dict[str, str] = {}

        for call in calls:
            spec = specs.get(call.name)
            if spec is None:
                self.metrics.guardrail(session.id, "tool_not_allowed")
                results[call.id] = json.dumps({"error": f"Tool '{call.name}' is not available here."})
                continue
            try:
                args = self._clean_args(spec, call.arguments)
            except ValueError as exc:
                results[call.id] = json.dumps({"error": str(exc)})
                continue
            if spec.read_only:
                reads.append((call, spec, args))
                continue
            # Action tool -> needs explicit customer confirmation.
            if spec.name == "submit_upgrade_order" and args.get("offer_id") not in session.offers_seen:
                self.metrics.guardrail(session.id, "offer_not_presented")
                results[call.id] = json.dumps(
                    {
                        "error": "offer_not_presented",
                        "detail": "Call get_eligible_offers and use an offer_id it returned.",
                    }
                )
                continue
            for existing_id, existing in list(session.pending.items()):
                if existing.tool == spec.name:  # one pending action per tool
                    session.pending.pop(existing_id)
                    yield {
                        "type": "action_resolved",
                        "action_id": existing_id,
                        "approved": False,
                        "tool": spec.name,
                        "superseded": True,
                    }
            summary, details = self._action_summary(session, spec.name, args)
            action = PendingAction(
                action_id=f"act_{secrets.token_hex(6)}",
                tool=spec.name,
                arguments=args,
                title=spec.title,
                summary=summary,
            )
            session.pending[action.action_id] = action
            yield {
                "type": "confirm_required",
                "action_id": action.action_id,
                "tool": spec.name,
                "title": spec.title,
                "summary": summary,
                "details": details,
            }
            results[call.id] = json.dumps(
                {
                    "status": "awaiting_customer_confirmation",
                    "detail": "The customer sees a Confirm button. Do not say it is done.",
                }
            )

        if reads:
            for call, spec, _ in reads:
                yield {
                    "type": "tool_start",
                    "call_id": call.id,
                    "tool": spec.name,
                    "title": spec.title,
                    "source": self._source(session, spec.name),
                }
            outcomes = await asyncio.gather(*(self._execute(session, spec, args) for _, spec, args in reads))
            for (call, spec, _), outcome in zip(reads, outcomes, strict=True):
                self._record(session, outcome)
                yield self._result_event(call.id, spec, outcome)
                results[call.id] = truncate_for_model(outcome.data)

        for call in calls:  # one tool message per call, in the model's order
            session.messages.append({"role": "tool", "tool_call_id": call.id, "content": results[call.id]})
