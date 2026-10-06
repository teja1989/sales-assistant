"""Logging with PII masking.

Every log record passes through `PiiMaskingFilter`, so emails, phone numbers,
card-like digit runs, bearer tokens and API keys are masked even if a developer
accidentally logs them.
"""

from __future__ import annotations

import logging
import re
import sys

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1***"),
    (re.compile(r"(?i)(api[-_]?key[\"'=:\s]+)[A-Za-z0-9._-]{8,}"), r"\1***"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "<email>"),
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "<card>"),
    (re.compile(r"(?<!\d)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}(?!\d)"), "<phone>"),
]


def mask_pii(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class PiiMaskingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - never break logging
            return True
        record.msg = mask_pii(message)
        record.args = None
        return True


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(PiiMaskingFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # Quiet noisy libraries unless debugging.
    for noisy in ("httpx", "httpx2", "httpcore", "httpcore2", "mcp"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, root.level))
