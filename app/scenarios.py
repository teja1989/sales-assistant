"""Scenario definitions.

A scenario is one YAML file in `scenarios/`. It describes who the customer is
(simulated fixture data), how they arrive from the search handoff, which tools
the assistant may use, where each tool's data comes from (sim or live) and what
a good outcome looks like (used by the automated tests).

Adding a scenario never requires code changes. See docs/adding-scenarios.md.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Intent = Literal["connectivity", "speed", "wifi", "device", "alert", "account", "general"]


class Expectations(BaseModel):
    model_config = ConfigDict(extra="forbid")

    must_call: list[str] = Field(default_factory=list)
    must_not_call: list[str] = Field(default_factory=list)
    confirm_action: str | None = None
    outcome: str | None = None


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    demo_order: int = 100  # position on the home-page demo launcher (lower first)
    description: str
    intent: Intent
    search_query: str
    match_keywords: list[str] = Field(default_factory=list)
    assistant_brief: str
    tools: list[str]
    data_sources: dict[str, Literal["sim", "live"]] = Field(default_factory=dict)
    live_customer_id: str | None = None
    suggested_replies: list[str] = Field(default_factory=list)
    customer: dict[str, Any]
    expected: Expectations = Field(default_factory=Expectations)

    @field_validator("id")
    @classmethod
    def _slug(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,48}", value):
            raise ValueError("id must be a lowercase slug (a-z, 0-9, '-')")
        return value

    @model_validator(mode="after")
    def _check(self) -> Scenario:
        unknown = set(self.data_sources) - set(self.tools)
        if unknown:
            raise ValueError(f"data_sources references tools not listed in tools: {sorted(unknown)}")
        for key in ("id", "first_name", "plan_id"):
            if key not in self.customer:
                raise ValueError(f"customer.{key} is required")
        return self

    def source_for(self, tool: str, override: str = "") -> Literal["sim", "live"]:
        if override in ("sim", "live"):
            return override  # type: ignore[return-value]
        return self.data_sources.get(tool, "sim")

    def public_view(self) -> dict[str, Any]:
        """Fields safe to send to the browser (no fixture internals)."""
        return {
            "id": self.id,
            "title": self.title,
            "demo_order": self.demo_order,
            "description": self.description,
            "intent": self.intent,
            "search_query": self.search_query,
            "suggested_replies": self.suggested_replies,
            "data_sources": {tool: self.source_for(tool) for tool in self.tools},
        }


class ScenarioError(ValueError):
    pass


def load_scenarios(directory: Path, known_tools: set[str] | None = None) -> dict[str, Scenario]:
    if not directory.is_dir():
        raise ScenarioError(f"Scenario directory not found: {directory}")
    scenarios: dict[str, Scenario] = {}
    customer_ids: dict[str, str] = {}
    for path in sorted(directory.glob("*.y*ml")):
        if path.name.startswith("_"):
            continue
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            scenario = Scenario.model_validate(raw)
        except Exception as exc:  # noqa: BLE001 - surface file name with the error
            raise ScenarioError(f"{path.name}: {exc}") from exc
        if scenario.id in scenarios:
            raise ScenarioError(f"{path.name}: duplicate scenario id {scenario.id!r}")
        cid = str(scenario.customer["id"])
        if cid in customer_ids:
            raise ScenarioError(f"{path.name}: customer id {cid} already used by {customer_ids[cid]}")
        customer_ids[cid] = scenario.id
        if known_tools is not None:
            unknown = set(scenario.tools) - known_tools
            if unknown:
                raise ScenarioError(f"{path.name}: unknown tools {sorted(unknown)}")
        scenarios[scenario.id] = scenario
    if not scenarios:
        raise ScenarioError(f"No scenarios found in {directory}")
    return scenarios


def in_demo_order(scenarios: dict[str, Scenario]) -> list[Scenario]:
    """Scenarios in launcher order (demo_order, then id)."""
    return sorted(scenarios.values(), key=lambda s: (s.demo_order, s.id))


def match_scenario(scenarios: dict[str, Scenario], query: str) -> Scenario | None:
    """Pick the scenario whose keywords best match a search query."""
    words = set(re.findall(r"[a-z0-9]+", query.lower()))
    text = query.lower()
    best: tuple[int, Scenario] | None = None
    for scenario in scenarios.values():
        score = 0
        for keyword in scenario.match_keywords:
            kw = keyword.lower()
            if " " in kw:
                score += 3 if kw in text else 0
            elif kw in words:
                score += 2
        if score and (best is None or score > best[0]):
            best = (score, scenario)
    return best[1] if best else None
