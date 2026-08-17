"""
session_store.py

In-memory store carrying context across a clarification exchange: when a
response comes back `ambiguous_metric` or `declined` with reason
`parameter_requires_clarification`, the frontend gets a `session_id` back
alongside it. Its follow-up call includes that `session_id` plus the user's
answer; the pipeline then has the original question available instead of
having to re-derive full context from the follow-up text alone.

Deliberately in-memory (confirmed acceptable for now) rather than Redis-
backed: single-process, lost on restart, TTL-expired entries swept lazily
on access. If this ever needs to survive restarts or run across multiple
instances, swap this module's implementation for a Redis-backed one behind
the same three functions -- nothing outside this file should need to change.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from threading import Lock


@dataclass
class SessionEntry:
    question: str
    result: dict
    created_at: float = field(default_factory=time.time)


class SessionStore:
    def __init__(self, ttl_seconds: int = 600):
        self._ttl_seconds = ttl_seconds
        self._entries: dict[str, SessionEntry] = {}
        self._lock = Lock()

    def create(self, question: str, result: dict) -> str:
        session_id = str(uuid.uuid4())
        with self._lock:
            self._sweep_expired()
            self._entries[session_id] = SessionEntry(question=question, result=result)
        return session_id

    def get(self, session_id: str) -> SessionEntry | None:
        with self._lock:
            self._sweep_expired()
            return self._entries.get(session_id)

    def delete(self, session_id: str) -> None:
        with self._lock:
            self._entries.pop(session_id, None)

    def _sweep_expired(self) -> None:
        now = time.time()
        expired = [sid for sid, entry in self._entries.items()
                   if now - entry.created_at > self._ttl_seconds]
        for sid in expired:
            del self._entries[sid]


_store: SessionStore | None = None


def get_session_store(ttl_seconds: int = 600) -> SessionStore:
    global _store
    if _store is None:
        _store = SessionStore(ttl_seconds=ttl_seconds)
    return _store
