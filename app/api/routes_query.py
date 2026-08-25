"""api/routes_query.py -- POST /api/v1/query, the main entry point."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request

from app import executor
from app.api.schemas import QueryRequest, QueryResponse
from app.config import get_settings
from app.pipeline import PipelineContext, run_pipeline
from app.session_store import get_session_store

logger = logging.getLogger(__name__)
router = APIRouter()

CLARIFICATION_STATUSES = {"ambiguous_metric"}
CLARIFICATION_REASONS = {"parameter_requires_clarification"}
CONFIRMATION_REQUIRED_STATUSES = {"alert_rule_proposed"}
# SKILL.md Section 12: an alert_rule_proposed result is a PROPOSAL only and
# must never be auto-executed (see executor.py's module docstring). It
# needs a session held for the same *mechanical* reason CLARIFICATION_
# STATUSES does below -- the caller needs a session_id to act on this
# result in a second step -- but that second step is a different endpoint
# (POST /api/v1/alerts/confirm, app/api/routes_alerts.py) doing a
# fundamentally different thing: confirm-and-create, not answer-and-retry.
# Kept as its own named set rather than merged into CLARIFICATION_STATUSES
# so that distinction stays visible in code, not just in a comment.


@router.post("/api/v1/query", response_model=QueryResponse)
async def query(request: Request, body: QueryRequest) -> QueryResponse:
    settings = get_settings()
    skill_index = request.app.state.skill_index
    store = get_session_store(settings.session_ttl_seconds)

    context = PipelineContext()
    if body.session_id:
        entry = store.get(body.session_id)
        if entry is None:
            raise HTTPException(
                status_code=410,
                detail="This session has expired or doesn't exist. Please ask the "
                       "original question again.",
            )
        context = PipelineContext(
            previous_question=entry.question,
            previous_result=entry.result,
            clarification_answer=body.question,
        )
        store.delete(body.session_id)

    try:
        contract = await asyncio.wait_for(
            run_pipeline(body.question, skill_index, settings, context=context),
            timeout=settings.pipeline_timeout_seconds,
        )
    except asyncio.TimeoutError:
        # PIPELINE_TIMEOUT_SECONDS exists in .env for exactly this: a hung
        # Gemini call or an unresponsive Prometheus/OpenSearch backend
        # should never leave an HTTP request hanging indefinitely. Without
        # this wrapper the setting was declared but silently never enforced
        # anywhere -- a request could hang forever with no ceiling at all.
        logger.error("Pipeline timed out after %.0fs for question: %s",
                      settings.pipeline_timeout_seconds, body.question)
        raise HTTPException(
            status_code=504,
            detail=f"The query pipeline took longer than {settings.pipeline_timeout_seconds:.0f}s "
                   f"and was aborted. Please try again.",
        )
    except Exception:
        logger.exception("Pipeline failed for question: %s", body.question)
        raise HTTPException(status_code=502, detail="The query pipeline failed unexpectedly. Please try again.")

    if _needs_clarification(contract) or _needs_confirmation(contract):
        new_session_id = store.create(body.question, contract)
        return QueryResponse(result=contract, session_id=new_session_id)

    executed = executor.execute_contract(contract, settings)
    return QueryResponse(result=executed, session_id=None)


def _needs_clarification(contract: dict) -> bool:
    """True if the contract itself needs a clarifying answer before
    anything should be executed. Checks BOTH shapes SKILL.md §9 allows:
    a top-level status (mode: "single"), and a status nested inside any
    entry of a mode: "multi" response's `results` array -- §9 explicitly
    states results holds "the same per-status shapes" as single mode, so a
    multi-measurement question ("show me both X and Y") can have one
    measurement resolve cleanly while another comes back ambiguous. In that
    case we hold off on ALL execution, not just the ambiguous entry: once
    the user's answer changes the Generator's understanding of the request,
    it may reconstruct the whole multi-result differently, not just patch
    one entry."""
    if _entry_needs_clarification(contract):
        return True
    if contract.get("mode") == "multi":
        return any(_entry_needs_clarification(entry) for entry in contract.get("results", []))
    return False


def _entry_needs_clarification(entry: dict) -> bool:
    if entry.get("status") in CLARIFICATION_STATUSES:
        return True
    if entry.get("status") == "declined" and entry.get("reason") in CLARIFICATION_REASONS:
        return True
    return False


def _needs_confirmation(contract: dict) -> bool:
    """True if the contract itself (or, in a `mode: "multi"` response, any
    entry within it) is `alert_rule_proposed` -- SKILL.md §12.5 says this
    status is always `mode: "single"` on its own, but a compound request can
    still merge it alongside an `unmapped` entry for an unrelated topic
    (pipeline.py's unresolved-topics merge), so this checks both shapes
    exactly like `_needs_clarification` does, for the same reason: don't
    execute anything else in the response until the pending piece is
    resolved one way or another."""
    if _entry_needs_confirmation(contract):
        return True
    if contract.get("mode") == "multi":
        return any(_entry_needs_confirmation(entry) for entry in contract.get("results", []))
    return False


def _entry_needs_confirmation(entry: dict) -> bool:
    return entry.get("status") in CONFIRMATION_REQUIRED_STATUSES
