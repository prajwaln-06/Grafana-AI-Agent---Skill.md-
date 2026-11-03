"""
app/auth/id_token_store.py

Server-side retention of the ID token that belongs to an application session.

Why this exists
---------------
The OIDC RP-Initiated Logout needs the ``id_token_hint`` from the token
response: Authentik (like most providers) refuses a logout request that
carries a ``post_logout_redirect_uri`` without a valid ``id_token_hint``, and
without that redirect the user is never returned to the application.

The ID token therefore has to survive from the callback to the logout. It is
*not* put into the signed session cookie: the cookie is integrity-protected
but not encrypted, and the application's session must never carry a token.
Instead the cookie holds an opaque handle (``sid``) and the token itself stays
here, on the server.

Deliberately in-memory (the same trade-off as ``app/session_store.py``):
single-process, lost on a restart, entries expire with the session. If this
ever needs to survive restarts or run across several instances, swap this
module's implementation for a Redis-backed one behind the same four
functions -- nothing outside this file should need to change.
"""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field


@dataclass
class IdTokenEntry:
    """One retained ID token."""

    id_token: str
    issuer: str = ""
    subject: str = ""
    created_at: float = field(default_factory=time.time)


class IdTokenStore:
    """An in-memory store that maps an opaque session handle to an ID token."""

    def __init__(self, ttl_seconds: int = 3600) -> None:
        self._ttl_seconds = int(ttl_seconds)
        self._entries: dict[str, IdTokenEntry] = {}
        self._lock = threading.Lock()

    # -- API --------------------------------------------------------------

    def put(self, id_token: str, *, issuer: str = "", subject: str = "") -> str:
        """Retain ``id_token`` and return the handle it is stored under."""
        session_id = secrets.token_urlsafe(32)
        with self._lock:
            self._sweep_expired()
            self._entries[session_id] = IdTokenEntry(
                id_token=id_token, issuer=issuer, subject=subject
            )
        return session_id

    def get(self, session_id: str | None) -> str | None:
        """The ID token stored under ``session_id``, or ``None``."""
        if not session_id:
            return None
        with self._lock:
            self._sweep_expired()
            entry = self._entries.get(session_id)
            return entry.id_token if entry is not None else None

    def delete(self, session_id: str | None) -> None:
        """Forget the ID token stored under ``session_id``."""
        if not session_id:
            return
        with self._lock:
            self._entries.pop(session_id, None)

    def clear(self) -> None:
        """Forget every retained ID token."""
        with self._lock:
            self._entries.clear()

    # -- housekeeping -----------------------------------------------------

    def _sweep_expired(self) -> None:
        now = time.time()
        expired = [
            session_id
            for session_id, entry in self._entries.items()
            if now - entry.created_at > self._ttl_seconds
        ]
        for session_id in expired:
            del self._entries[session_id]


_store_lock = threading.Lock()
_store: IdTokenStore | None = None


def get_id_token_store(ttl_seconds: int = 3600) -> IdTokenStore:
    """The process-wide ID token store."""
    global _store
    with _store_lock:
        if _store is None:
            _store = IdTokenStore(ttl_seconds=ttl_seconds)
        return _store


__all__ = ["IdTokenEntry", "IdTokenStore", "get_id_token_store"]
