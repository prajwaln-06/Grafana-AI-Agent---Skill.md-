"""
api/routes_alerts.py -- POST /api/v1/alerts/confirm.

SKILL.md Section 12.1: this is the ONLY code path in this entire backend
that ever creates anything in Grafana. Nothing in app/pipeline.py, app/
validator.py, or app/executor.py can reach app/grafana_client.py -- the
propose/confirm boundary Section 12 describes is enforced structurally, not
just by convention: this module is the sole caller of
grafana_client.create_alert_rule, and it only calls it in response to an
explicit, separate POST from the frontend, never as a continuation of the
original /api/v1/query request.

This endpoint deliberately accepts nothing about the rule itself -- no
title, query, threshold, or folder in the request body. The ONLY input is a
`session_id` referencing an already-validated `alert_rule_proposed` result
that the frontend already showed the user (from a prior POST /api/v1/query
response). This means there is no way for a confirmation call to create a
rule that differs even slightly from what the user already saw and agreed
to -- the alternative (accepting a restated payload) would reopen exactly
the "never fabricate/never let a request drift from what was verified"
problem SKILL.md Section 12.4 exists to close.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from app import grafana_client
from app.api.schemas import AlertConfirmRequest, AlertConfirmResponse
from app.config import get_settings
from app.session_store import get_session_store

logger = logging.getLogger(__name__)
router = APIRouter()

# grafana_client.AlertRuleOutcome.status -> (AlertConfirmResponse.status, HTTP status code)
_OUTCOME_STATUS_MAP = {
    "success": ("created", 200),
    "configuration_error": ("configuration_error", 500),
    "endpoint_unreachable": ("grafana_unreachable", 502),
    "timeout": ("grafana_unreachable", 502),
    "endpoint_error": ("grafana_error", 502),
    "conflict": ("conflict", 409),
}


@router.post("/api/v1/alerts/confirm", response_model=AlertConfirmResponse)
async def confirm_alert_rule(request: Request, body: AlertConfirmRequest) -> AlertConfirmResponse:
    try:
        return await _confirm_alert_rule_impl(body)
    except HTTPException:
        # Our own deliberate 4xx/5xx responses (session missing, feature off,
        # Grafana rejected the rule, etc.) already carry a meaningful detail
        # message -- let FastAPI serialize them normally.
        raise
    except Exception as e:
        # Everything else was previously masked as a bare "Internal Server
        # Error" -- unhelpful for the caller and worse for debugging.
        # Log the full traceback (surfaces in uvicorn console) and give the
        # caller enough to file a useful bug report.
        logger.exception("Unhandled exception confirming alert rule for session %s", body.session_id)
        raise HTTPException(
            status_code=500,
            detail=(
                f"Unexpected error during alert-rule confirmation: {type(e).__name__}: {e}. "
                f"Check the server console for the full traceback."
            ),
        )


async def _confirm_alert_rule_impl(body: AlertConfirmRequest) -> AlertConfirmResponse:
    settings = get_settings()

    if not settings.alert_rule_creation_enabled:
        # Matches the exact same fail-closed posture as pipeline.py's
        # defense-in-depth check: even if a session somehow exists (e.g.
        # the flag was disabled after the proposal was created), this
        # deployment never actually writes to Grafana while the flag is
        # off, full stop.
        raise HTTPException(status_code=403, detail="Alert-rule creation is disabled on this deployment.")

    store = get_session_store(settings.session_ttl_seconds)
    entry = store.get(body.session_id)
    if entry is None:
        raise HTTPException(
            status_code=410,
            detail="This session has expired or doesn't exist. Please ask the "
                   "original alert-creation question again.",
        )

    alert_entry = _find_alert_rule_proposed_entry(entry.result)
    if alert_entry is None:
        raise HTTPException(
            status_code=409,
            detail="This session is not a pending alert-rule confirmation "
                   "(its result is not, or no longer, 'alert_rule_proposed').",
        )

    if not body.confirm:
        # Single-use either way -- discarding still consumes the session,
        # exactly like routes_query.py consumes a clarification session on
        # its follow-up call, confirmed or not.
        store.delete(body.session_id)
        return AlertConfirmResponse(status="discarded")

    alert_rule = alert_entry.get("alert_rule") or {}
    comparison = alert_rule.get("comparison") or {}

    outcome = grafana_client.create_alert_rule(
        grafana_url=settings.grafana_url,
        service_account_token=settings.grafana_service_account_token,
        folder_uid=alert_rule.get("folder"),
        # Section 12.5: the proposal's own datasource_uid is always null --
        # it's resolved here, at confirmation time, from deployment config,
        # exactly as SKILL.md §12.5 and pipeline.py's _apply_alert_rule_
        # defaults describe. This is the one point in the whole flow where
        # a live Grafana datasource UID enters the picture at all.
        datasource_uid=settings.grafana_default_datasource_uid,
        title=alert_rule.get("title", ""),
        condition_query=alert_rule.get("condition_query", ""),
        comparison_operator=comparison.get("operator", ""),
        threshold=comparison.get("threshold"),
        for_duration=alert_rule.get("for_duration", ""),
        timeout=settings.grafana_timeout_seconds,
    )
    store.delete(body.session_id)

    if outcome.status not in _OUTCOME_STATUS_MAP:
        # Should be unreachable -- grafana_client.py's status vocabulary is
        # closed and this map covers all of it -- but fail loudly rather
        # than silently mislabeling an unrecognized outcome as success.
        logger.error("Unrecognized AlertRuleOutcome.status %r from grafana_client -- treating as an error.",
                      outcome.status)
        raise HTTPException(status_code=502, detail=f"Unexpected outcome from Grafana client: {outcome.status}")

    response_status, http_status = _OUTCOME_STATUS_MAP[outcome.status]
    if http_status != 200:
        logger.warning("Alert-rule creation for session %s did not succeed: %s (%s)",
                        body.session_id, outcome.status, outcome.error)
        raise HTTPException(
            status_code=http_status,
            detail=outcome.error or f"Alert-rule creation failed: {outcome.status}",
        )

    return AlertConfirmResponse(status=response_status, rule_uid=outcome.rule_uid, deeplink=outcome.deeplink)


def _find_alert_rule_proposed_entry(result: dict) -> dict | None:
    """Handles both shapes SKILL.md §9 allows, exactly like routes_query.py's
    `_needs_clarification` does for clarification statuses: a top-level
    `mode: "single"` result, or a `status: "alert_rule_proposed"` entry
    nested inside a `mode: "multi"` response's `results` array (possible if
    an alert-creation request was merged with an unrelated unresolved
    topic -- see pipeline.py's unresolved-topics merge)."""
    if result.get("status") == "alert_rule_proposed":
        return result
    if result.get("mode") == "multi":
        for candidate in result.get("results", []):
            if candidate.get("status") == "alert_rule_proposed":
                return candidate
    return None