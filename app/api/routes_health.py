"""api/routes_health.py -- health, readiness, and capabilities introspection."""
from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.schemas import CapabilitiesResponse, HealthResponse

router = APIRouter()


@router.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    """Liveness only -- the process is up and responding. Does not check
    that the skill package loaded or that backends are reachable; use
    /readyz for that."""
    return HealthResponse(status="ok")


@router.get("/readyz", response_model=HealthResponse)
async def readyz(request: Request) -> HealthResponse:
    """Readiness -- confirms the skill package actually loaded at startup.
    Does NOT ping Prometheus/OpenSearch on every call (that would make this
    endpoint as slow/flaky as the backends themselves); those are checked
    per-request by the executor and surfaced as execution_status instead."""
    skill_index = getattr(request.app.state, "skill_index", None)
    if skill_index is None:
        return HealthResponse(status="not_ready")
    return HealthResponse(status="ready", skill_name=skill_index.metadata.name,
                           skill_version=skill_index.metadata.version)


@router.get("/api/v1/capabilities", response_model=CapabilitiesResponse)
async def capabilities(request: Request) -> CapabilitiesResponse:
    """Introspection endpoint: exposes the currently-loaded skill's routing
    table so the frontend can know what's actually covered (e.g. grey out
    an OpenSearch-dependent UI affordance while its routing rows are still
    absent or marked infrastructure-only) instead of learning that from a
    failed query."""
    skill_index = request.app.state.skill_index
    rows = [
        {
            "topic": row.topic,
            "data_sources": list(row.data_sources),
            "reference_path": row.reference_path,
            "pending": row.pending,
            "note": row.note,
        }
        for row in skill_index.routing_rows
    ]
    return CapabilitiesResponse(
        skill_name=skill_index.metadata.name,
        skill_version=skill_index.metadata.version,
        routing_rows=rows,
    )
