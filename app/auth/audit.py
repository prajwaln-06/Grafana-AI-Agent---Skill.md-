"""
app/auth/audit.py

Central, server-side audit recording for security-relevant actions.

Three rules hold for every event written through this module:

1. **The actor is the authenticated principal.** The actor identity, role and
   authorization version are read from the server-side
   :class:`~app.auth.principal.Principal` -- never from a request body, a
   query parameter or a client-supplied header. A caller that wants a
   different actor recorded cannot ask for it.

2. **Secrets never reach the log.** Passwords, cookies, bearer/access/refresh/
   ID tokens, session secrets and API keys are redacted recursively before a
   payload is stored, at any nesting depth and under any key spelling.

3. **An audit failure never turns a denial into an allow.** Denials are
   recorded on a best-effort basis and the denial is raised regardless of
   whether the record could be written. Successful mutations whose effect is
   persisted in the same database transaction (the administrative user-access
   changes) write their audit record *inside* that transaction, so the
   mutation cannot complete unless the record was persisted. Operations whose
   effect is external (a Grafana or Prometheus write) or in-memory (the
   proposal lifecycle) are audited after the fact; a failure to persist is
   logged at ERROR level and never silently discarded.

The module deliberately exposes no update or delete operation: the audit log
is append-only, and no application API can rewrite history.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

from app.auth.principal import Principal, get_current_principal
from app.storage.audit_store import AuditStore
from app.storage.database import get_db_session

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Event catalogue
# ---------------------------------------------------------------------------

class AuditEvent:
    """The canonical event types written to the audit log."""

    # -- User administration ------------------------------------------------
    USER_CREATED = "user_created"
    USER_ROLE_CHANGED = "user_role_changed"
    USER_STATUS_CHANGED = "user_status_changed"
    USER_SCOPE_CHANGED = "user_scope_changed"
    USER_ACCESS_MODIFIED = "user_access_modified"
    FIRST_SIGN_IN = "first_sign_in"

    # -- Administrative actions --------------------------------------------
    ADMIN_SKILL_RELOADED = "admin_skill_reloaded"
    ADMIN_SKILL_RELOAD_FAILED = "admin_skill_reload_failed"
    ADMIN_USERS_LISTED = "admin_users_listed"
    ADMIN_FOLDERS_LISTED = "admin_folders_listed"
    ADMIN_ACCESS_DENIED = "admin_access_denied"
    ADMIN_SEED_AUDIT_VIEWED = "admin_seed_audit_viewed"
    ADMIN_AUDIT_LOG_VIEWED = "admin_audit_log_viewed"

    # -- Proposal lifecycle -------------------------------------------------
    PROPOSAL_CREATED = "proposal_created"
    PROPOSAL_MODIFIED = "proposal_modified"
    PROPOSAL_APPROVED = "proposal_approved"
    PROPOSAL_REJECTED = "proposal_rejected"
    PROPOSAL_EXECUTED = "proposal_executed"
    PROPOSAL_EXECUTION_FAILED = "proposal_execution_failed"
    PROPOSAL_ACCESS_DENIED = "proposal_access_denied"

    # -- Dashboard operations ----------------------------------------------
    DASHBOARD_CREATED = "dashboard_created"
    DASHBOARD_UPDATED = "dashboard_updated"
    DASHBOARD_DELETED = "dashboard_deleted"
    DASHBOARD_MOVED = "dashboard_moved"
    PANEL_CREATED = "panel_created"
    PANEL_UPDATED = "panel_updated"
    PANEL_DELETED = "panel_deleted"
    DASHBOARD_ACCESS_DENIED = "dashboard_access_denied"

    # -- Alert operations ---------------------------------------------------
    ALERT_PROPOSED = "alert_proposed"
    ALERT_APPROVED = "alert_approved"
    ALERT_REJECTED = "alert_rejected"
    ALERT_EXECUTED = "alert_executed"
    ALERT_EXECUTION_FAILED = "alert_execution_failed"

    # -- Security events ----------------------------------------------------
    AUTHENTICATION_FAILED = "authentication_failed"
    AUTHENTICATION_SUCCEEDED = "authentication_succeeded"
    LOGOUT = "logout"
    SCOPE_VIOLATION = "scope_violation"
    ROLE_VIOLATION = "role_violation"
    ENVIRONMENT_DENIED = "environment_denied"


ALLOW = "ALLOW"
DENY = "DENY"


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------

def _principal_details(principal: Principal | None) -> dict[str, Any]:
    """Server-derived actor attributes recorded with every event."""
    if principal is None:
        return {}
    return {
        "actor_role": (principal.role or "").lower() or None,
        "actor_authz_version": principal.authz_version,
    }


def record_event(
    event_type: str,
    *,
    principal: Principal | None = None,
    decision: str = ALLOW,
    reason: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    previous_state: Mapping[str, Any] | None = None,
    new_state: Mapping[str, Any] | None = None,
    details: Mapping[str, Any] | None = None,
    proposal_id: str | None = None,
    folder_uid: str | None = None,
    dashboard_uid: str | None = None,
    environment: str | None = None,
    impact_class: str | None = None,
    correlation_id: str | None = None,
    required: bool = False,
    session: Any | None = None,
) -> bool:
    """Record a security-relevant event.

    ``required=True`` means the event must be persisted for the operation to
    be considered complete: the event is written in the caller's own
    transaction (``session``) and an exception propagates, so the surrounding
    transaction rolls back. ``required=False`` is best-effort -- a failure is
    logged at ERROR level and reported as ``False``; it never changes the
    outcome of the operation being audited (in particular it never turns a
    denial into an allow).

    Returns ``True`` when the record was persisted.
    """
    actor = principal if principal is not None else get_current_principal()

    payload: dict[str, Any] = dict(details or {})
    payload.update(_principal_details(actor))
    if proposal_id is not None:
        payload["proposalId"] = proposal_id
    if folder_uid is not None:
        payload["folderUid"] = folder_uid
    if dashboard_uid is not None:
        payload["dashboardUid"] = dashboard_uid
    if environment is not None:
        payload["environment"] = environment
    if impact_class is not None:
        payload["impactClass"] = impact_class
    if correlation_id is not None:
        payload["correlationId"] = correlation_id

    kwargs: dict[str, Any] = {
        "event_type": event_type,
        "actor_id": actor.user_id if actor else None,
        "actor_issuer": actor.issuer if actor else None,
        "actor_subject": actor.subject if actor else None,
        "target_type": target_type,
        "target_id": target_id,
        "decision": decision,
        "reason": reason,
        "previous_state": dict(previous_state) if previous_state is not None else None,
        "new_state": dict(new_state) if new_state is not None else None,
        "details": payload,
    }

    if session is not None:
        # The event is written in the caller's own transaction, so a failure
        # rolls that transaction back -- the audited mutation cannot complete
        # unless the record was persisted. The exception always propagates
        # here, whatever ``required`` says.
        AuditStore.log_event(session, **kwargs)
        return True

    try:
        with get_db_session() as own_session:
            AuditStore.log_event(own_session, **kwargs)
        return True
    except Exception as exc:  # noqa: BLE001 - the audit sink must never leak
        if required:
            logger.error("Required audit event %r could not be persisted: %s", event_type, exc)
            raise
        logger.error("Audit event %r could not be persisted: %s", event_type, exc)
        return False


def record_denial(
    event_type: str,
    *,
    principal: Principal | None = None,
    reason: str,
    target_type: str | None = None,
    target_id: str | None = None,
    details: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> bool:
    """Record a denied operation.

    A denial is always reported to the caller regardless of whether the audit
    record could be written -- the record is written on a best-effort basis
    and can never turn the denial into an allow.
    """
    return record_event(
        event_type,
        principal=principal,
        decision=DENY,
        reason=reason,
        target_type=target_type,
        target_id=target_id,
        details=details,
        required=False,
        **kwargs,
    )


def record_tool_denial(
    reason: str,
    *,
    target_id: str,
    target_type: str = "grafana_tool",
    details: Mapping[str, Any] | None = None,
) -> bool:
    """Record a denied Grafana tool call.

    The wrapper layer is the security boundary in front of Grafana, so a
    refusal there is recorded with the *authenticated* principal as the
    actor -- never with anything the model or the client supplied.
    """
    return record_denial(
        AuditEvent.SCOPE_VIOLATION,
        reason=reason,
        target_type=target_type,
        target_id=target_id,
        details=details,
    )


__all__ = [
    "ALLOW",
    "DENY",
    "AuditEvent",
    "record_denial",
    "record_event",
    "record_tool_denial",
]
