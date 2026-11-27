"""Shared telemetry helpers for the FastAPI API server."""
from __future__ import annotations

import sys

from app import telemetry


def count_api_active_sessions() -> int:
    """Count retained in-memory session/conversation contexts across loaded API stores."""
    total = 0

    chat_mod = sys.modules.get("app.api.routes_chat")
    if chat_mod is not None:
        store = getattr(chat_mod, "SESSION_STORE", None)
        sessions = getattr(store, "_sessions", None)
        if isinstance(sessions, dict):
            total += len(sessions)

    session_mod = sys.modules.get("app.session_store")
    if session_mod is not None:
        store = getattr(session_mod, "_store", None)
        entries = getattr(store, "_entries", None)
        if isinstance(entries, dict):
            total += len(entries)

    proposals_mod = sys.modules.get("app.api.routes_proposals")
    if proposals_mod is not None:
        convs = getattr(proposals_mod, "_CONVERSATIONS", None)
        if isinstance(convs, dict):
            total += len(convs)

    adk_mod = sys.modules.get("app.adk_agent")
    if adk_mod is not None:
        svc = getattr(adk_mod, "_session_service", None)
        adk_sessions = getattr(svc, "sessions", getattr(svc, "_sessions", None))
        total += telemetry.count_nested_sessions(adk_sessions)

    return total
