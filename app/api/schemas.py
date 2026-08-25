"""api/schemas.py -- Pydantic models for the HTTP API's request/response bodies."""
from __future__ import annotations

from pydantic import BaseModel, Field


class QueryRequest(BaseModel):
    question: str = Field(..., min_length=1, description="The user's natural-language question.")
    session_id: str | None = Field(
        default=None,
        description="Present when this call is a follow-up answering a prior "
                    "clarification request (see `clarification` in a previous "
                    "response). Omit for a fresh question.",
    )


class QueryResponse(BaseModel):
    """Deliberately untyped beyond the envelope: the actual payload shape is
    SKILL.md §9's Output Contract plus executor.py's `execution` block, both
    of which are the skill package's evolving contract, not this API layer's
    concern to re-declare and risk drifting out of sync with. `session_id`
    is populated only when the result needs a clarification follow-up."""
    result: dict
    session_id: str | None = None


class HealthResponse(BaseModel):
    status: str
    skill_name: str | None = None
    skill_version: str | None = None


class CapabilitiesResponse(BaseModel):
    skill_name: str
    skill_version: str
    routing_rows: list[dict]


class AlertConfirmRequest(BaseModel):
    """SKILL.md Section 12's confirmation step. `session_id` must come from
    a prior `QueryResponse` whose `result.status` was `alert_rule_proposed`
    -- this is the ONLY input the confirmation endpoint accepts; it never
    takes a restated title/query/threshold, precisely so nothing about the
    rule that gets created can differ from what the user already saw and is
    confirming (see app/api/routes_alerts.py)."""
    session_id: str = Field(..., min_length=1, description="session_id from the alert_rule_proposed response being confirmed.")
    confirm: bool = Field(
        default=True,
        description="True (default) to actually create the proposed rule in Grafana. False "
                    "discards the proposal without creating anything -- the session is deleted "
                    "either way, since a proposal is single-use regardless of the answer.",
    )


class AlertConfirmResponse(BaseModel):
    """Deliberately narrow and explicit (unlike QueryResponse's `dict`
    payload) -- this endpoint performs a real write, so its response shape
    is a first-class contract of this API, not something to leave loosely
    typed.

    Only returned (HTTP 200) for the two non-error outcomes, 'created' and
    'discarded' -- see app/api/routes_alerts.py. Every other outcome
    (expired/unknown session, session isn't a pending alert proposal,
    feature disabled, Grafana misconfigured/unreachable/erroring, a
    conflicting rule already exists) is a 4xx/5xx HTTP error with a plain
    `{"detail": "..."}` body instead of this shape, matching this API's
    existing convention for session errors (see POST /api/v1/query's
    handling of an expired clarification session).
    """
    status: str = Field(..., description="'created' or 'discarded' -- the only two values ever returned "
                                          "in a 200 response body; see the class docstring for every other outcome.")
    rule_uid: str | None = None
    deeplink: str | None = None
    error: str | None = None
