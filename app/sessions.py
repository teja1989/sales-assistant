"""In-memory chat sessions.

Prototype trade-off: sessions live in process memory, so run ONE instance on
Cloud Foundry (manifest default). Multiple instances need a shared store such
as Redis; see docs/deployment-cloud-foundry.md.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from app.scenarios import Scenario


@dataclass
class PendingAction:
    action_id: str
    tool: str
    arguments: dict[str, Any]
    title: str
    summary: str
    created_at: float = field(default_factory=time.time)


@dataclass
class Session:
    id: str
    scenario: Scenario
    customer_id: str  # simulated customer clone owned by this session
    live_customer_id: str | None
    search_query: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    pending: dict[str, PendingAction] = field(default_factory=dict)
    offers_seen: dict[str, dict[str, Any]] = field(default_factory=dict)
    previews: dict[str, dict[str, Any]] = field(default_factory=dict)
    verified_amounts: set[str] = field(default_factory=set)
    flags: set[str] = field(default_factory=set)
    created_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    resolved_at: float | None = None
    busy: bool = False  # one turn at a time per session (single event loop, no lock needed)

    def customer_id_for(self, source: str) -> str:
        if source == "live" and self.live_customer_id:
            return self.live_customer_id
        return self.customer_id


class SessionStore:
    def __init__(self, ttl_s: int, max_sessions: int) -> None:
        self.ttl_s = ttl_s
        self.max_sessions = max_sessions
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self, **kwargs: Any) -> tuple[Session, list[Session]]:
        """Create a session; returns (session, evicted sessions) so callers can clean up."""
        with self._lock:
            evicted = self._evict_locked()
            if len(self._sessions) >= self.max_sessions:
                oldest = min(self._sessions.values(), key=lambda s: s.last_seen)
                evicted.append(self._sessions.pop(oldest.id))
            session = Session(id=secrets.token_urlsafe(24), **kwargs)
            self._sessions[session.id] = session
            return session, evicted

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if time.time() - session.last_seen > self.ttl_s:
                self._sessions.pop(session_id, None)
                return None
            session.last_seen = time.time()
            return session

    def _evict_locked(self) -> list[Session]:
        now = time.time()
        expired = [s for s in self._sessions.values() if now - s.last_seen > self.ttl_s]
        for s in expired:
            self._sessions.pop(s.id, None)
        return expired

    def __len__(self) -> int:
        return len(self._sessions)
