"""Output guardrails that hold even if the model is wrong or manipulated."""

from __future__ import annotations

import re
from typing import Any

PRICE_RE = re.compile(r"\$\s?(\d{1,4}(?:,\d{3})*(?:\.\d{1,2})?)")


def _norm(amount: str) -> str:
    return f"{float(amount.replace(',', '')):.2f}"


MONEY_KEY = re.compile(r"price|amount|change|discount|fee|credit|cost|total|due|installment|charge", re.I)


def collect_amounts(value: Any, out: set[str], key: str = "") -> None:
    """Record money values from a tool result (numbers under money-like keys, or '$x' in strings)."""
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        if MONEY_KEY.search(key):
            out.add(f"{abs(float(value)):.2f}")
    elif isinstance(value, dict):
        for k, v in value.items():
            collect_amounts(v, out, str(k))
    elif isinstance(value, list):
        for v in value:
            collect_amounts(v, out, key)
    elif isinstance(value, str):
        for m in PRICE_RE.finditer(value):
            out.add(_norm(m.group(1)))


def check_prices(text: str, verified: set[str]) -> tuple[str, list[str]]:
    """Replace any $ amount that did not come from a tool result.

    Returns (possibly rewritten text, list of unverified amounts).
    """
    unverified: list[str] = []

    def replace(match: re.Match[str]) -> str:
        amount = _norm(match.group(1))
        if amount in verified:
            return match.group(0)
        unverified.append(match.group(0))
        return "[price on the offer card]"

    return PRICE_RE.sub(replace, text), unverified


def truncate_for_model(data: dict[str, Any], limit: int = 6000) -> str:
    import json

    text = json.dumps(data, default=str)
    if len(text) <= limit:
        return text
    return json.dumps({"truncated": True, "preview": text[:limit]})
