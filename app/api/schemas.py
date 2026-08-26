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
