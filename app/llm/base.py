"""Provider-neutral streaming chat interface (OpenAI chat-completions shape)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON string as produced by the model


@dataclass
class TextDelta:
    text: str


@dataclass
class TurnComplete:
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None


LlmEvent = TextDelta | TurnComplete


class LlmError(Exception):
    """A provider failure, with a message safe to show in logs (never contains keys)."""

    def __init__(self, message: str, status: int | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class LlmClient(Protocol):
    name: str

    def stream(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> AsyncIterator[LlmEvent]:
        """Stream text deltas, then exactly one TurnComplete."""
        ...
