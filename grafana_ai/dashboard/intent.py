"""Deterministic dashboard intent boundary used before tool selection."""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Intent(str, Enum):
    READ = "READ"
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    REMOVE = "REMOVE"
    UNSPECIFIED = "UNSPECIFIED"


@dataclass(frozen=True)
class IntentResolution:
    intent: Intent
    confidence: str
    reason: str
    operation: str | None = None


_CREATE = re.compile(r"\b(create|build|make|generate|set up)\b.*\b(dashboard|monitoring dashboard|grafana dashboard)\b", re.I)
_REMOVE = re.compile(r"\b(delete|remove|drop)\b", re.I)
_UPDATE = re.compile(r"\b(add|change|modify|update|edit|include|exclude|set|switch|put)\b", re.I)
_READ = re.compile(r"\b(what|show|fetch|get|query|compare|current|available|list|how much|how many|utili[sz]ation|usage|value|metrics?)\b", re.I)
_DASHBOARD_CONTEXT = re.compile(r"\b(dashboard|panel|grafana|this dashboard|existing dashboard)\b", re.I)
_MUTATION_CONTEXT = re.compile(r"\b(to|in|on|from)\s+(this|the|my|an?|existing)?\s*(dashboard|panel)\b|\bpanel\b", re.I)
_NAMED_UPDATE_DESTINATION = re.compile(r"\b(add|change|modify|update|edit|include|exclude|set|switch|put)\b.+\b(to|in|on)\s+(?:my|the)?\s*[A-Za-z0-9][A-Za-z0-9 _.-]+(?:[.!?]|$)", re.I)


def resolve_intent(text: str) -> IntentResolution:
    """Resolve explicit operation language before selecting a tool.

    Read language wins unless the user explicitly requests a dashboard
    operation. Ambiguous mixed requests are returned as UNSPECIFIED.
    """
    value = (text or "").strip()
    if not value:
        return IntentResolution(Intent.UNSPECIFIED, "low", "No request was supplied.")
    has_read = bool(_READ.search(value))
    has_dashboard = bool(_DASHBOARD_CONTEXT.search(value))
    has_mutation = bool(_CREATE.search(value) or _REMOVE.search(value) or (_UPDATE.search(value) and (_MUTATION_CONTEXT.search(value) or _NAMED_UPDATE_DESTINATION.search(value) or has_dashboard)))
    if has_read and has_mutation and re.search(r"\bshow\b.*\bput\b.*\bdashboard\b", value, re.I):
        return IntentResolution(Intent.UNSPECIFIED, "medium", "The request combines retrieval language with a dashboard change; clarify the desired operation.")
    if _CREATE.search(value):
        return IntentResolution(Intent.CREATE, "high", "Explicit dashboard creation language.", "create")
    if _REMOVE.search(value) and (has_dashboard or _MUTATION_CONTEXT.search(value)):
        operation = "delete_dashboard" if re.search(r"\b(delete|remove)\b.*\bdashboard\b", value, re.I) else "remove_panel"
        return IntentResolution(Intent.REMOVE, "high", "Explicit dashboard or panel removal language.", operation)
    if _UPDATE.search(value) and (has_dashboard or _MUTATION_CONTEXT.search(value) or _NAMED_UPDATE_DESTINATION.search(value)):
        return IntentResolution(Intent.UPDATE, "high", "Explicit dashboard or panel modification language.", "update")
    if has_read and not has_mutation:
        return IntentResolution(Intent.READ, "high", "Retrieval or comparison language without an explicit mutation.", "read")
    return IntentResolution(Intent.UNSPECIFIED, "low", "The desired dashboard operation is not explicit.")
