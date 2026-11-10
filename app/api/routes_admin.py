"""
api/routes_admin.py

Operational endpoint for picking up SKILL.md structural changes (a new
routing-table row, a new exporter, a frontmatter version bump) without a
full process restart, and administrative management endpoints (Section 5.3 & 5.4).
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.auth.audit import AuditEvent, record_denial, record_event
from app.auth.dependencies import require_admin_principal
from app.auth.principal import Principal
from app.catalog.runtime import reload_structured_catalog
from app.config import get_settings
from app.skill_index import SkillIndex, SkillIndexError
from app.storage.audit_store import AuditStore
from app.storage.database import get_db_session
from app.storage.models import UserModel
from app.storage.user_store import AdminSecurityError, UserStore

logger = logging.getLogger(__name__)
router = APIRouter()


def _audit(
    event_type: str,
    *,
    actor_id: str | None,
    decision: str,
    reason: str,
    target_type: str | None = None,
    target_id: str | None = None,
    details: dict[str, Any] | None = None,
    principal: Principal | None = None,
) -> None:
    """Record an administrative action in the append-only audit log.

    Best-effort: an audit sink failure is logged at ERROR level by the
    recorder and never changes the outcome of the administrative action.
    """
    record_event(
        event_type,
        principal=principal,
        decision=decision,
        reason=reason,
        target_type=target_type,
        target_id=target_id,
        details=details or {},
    )


class UserAccessUpdateRequest(BaseModel):
    role: str | None = Field(default=None, description="One of: admin, maintainer, operator, observer")
    status: str | None = Field(default=None, description="One of: active, pending, deactivated")
    scopes: dict[str, Any] | None = Field(default=None, description="Granular scope boundaries")


@router.post("/api/v1/admin/reload-skill")
async def reload_skill(
    request: Request,
    principal: Principal = Depends(require_admin_principal),
) -> dict:
    """Re-parses SKILL.md from disk and, only if that succeeds cleanly
    (routing table parses, every non-pending reference still exists),
    atomically swaps it in as the skill every subsequent request uses --
    both for routing (routes_query.py reads request.app.state.skill_index
    directly) and for the pipeline's Router/Generator phases (which receive
    that same object as a parameter, not a separate cached copy -- see
    skill_index.py's module docstring for why a single source of truth here
    matters for the "add a new domain and it's picked up automatically"
    guarantee).

    On failure, the previously-loaded skill keeps serving traffic
    unchanged -- a bad edit to SKILL.md never takes the service down.

    Only administrators may trigger a reload: the endpoint changes what every
    subsequent request does, so it is guarded on the server and audited.
    """
    settings = get_settings()
    try:
        new_index = SkillIndex.load(settings.skills_root)
    except SkillIndexError as e:
        logger.error("Skill reload failed, keeping previous skill loaded: %s", e)
        _audit(
            AuditEvent.ADMIN_SKILL_RELOAD_FAILED,
            actor_id=principal.user_id,
            decision="DENY",
            reason="skill_reload_failed",
            target_type="skill",
            details={"error": str(e)},
            principal=principal,
        )
        raise HTTPException(status_code=422, detail=f"Reload failed, previous skill still active: {e}")

    request.app.state.skill_index = new_index
    logger.info("Reloaded skill %r version %s (%d routing rows).",
                new_index.metadata.name, new_index.metadata.version, len(new_index.routing_rows))

    # Refresh the structured metric catalog alongside the skill: its keyword
    # enrichment reads the skill's Markdown, so the two should not drift.
    # Only after the skill reload succeeded (a failed skill reload keeps
    # everything as it was). A catalog problem never fails this endpoint --
    # reload_structured_catalog keeps the previous good catalog instead.
    reload_structured_catalog(settings)
    _audit(
        AuditEvent.ADMIN_SKILL_RELOADED,
        actor_id=principal.user_id,
        decision="ALLOW",
        reason="admin_skill_reload",
        target_type="skill",
        target_id=new_index.metadata.name,
        details={"version": new_index.metadata.version, "routing_rows": len(new_index.routing_rows)},
        principal=principal,
    )
    return {
        "status": "reloaded",
        "skill_name": new_index.metadata.name,
        "skill_version": new_index.metadata.version,
        "routing_rows": len(new_index.routing_rows),
    }


@router.get("/api/v1/admin/users")
async def list_users(
    principal: Principal = Depends(require_admin_principal),
) -> dict[str, Any]:
    """List all registered users, roles, statuses, and scopes per Section 5.3.

    Administrators only: the response exposes every user's identity and
    complete scope set.
    """
    with get_db_session() as session:
        users = (
            session.execute(
                select(UserModel)
                .options(joinedload(UserModel.scopes))
                .order_by(UserModel.created_at)
            )
            .scalars()
            .unique()
            .all()
        )

        _audit(
            AuditEvent.ADMIN_USERS_LISTED,
            actor_id=principal.user_id,
            decision="ALLOW",
            reason="admin_list_users",
            target_type="user_collection",
            details={"count": len(users)},
            principal=principal,
        )

        return {
            "users": [
                {
                    "user_id": u.user_id,
                    "display_name": u.display_name,
                    "username": u.username,
                    "email": u.email,
                    "role": u.role,
                    "status": u.status,
                    "authz_version": u.authz_version,
                    "created_at": u.created_at.isoformat() if u.created_at else None,
                    "last_seen_at": u.last_seen_at.isoformat() if u.last_seen_at else None,
                    "scopes": {
                        "allowed_folder_uids": list(u.scopes.allowed_folder_uids) if u.scopes else [],
                        "allowed_dashboard_uids": list(u.scopes.allowed_dashboard_uids) if u.scopes else [],
                        "denied_dashboard_uids": list(u.scopes.denied_dashboard_uids) if u.scopes else [],
                        "allowed_environments": list(u.scopes.allowed_environments) if u.scopes else [],
                    },
                }
                for u in users
            ]
        }


@router.get("/api/v1/admin/folders")
async def list_grafana_folders(
    request: Request,
    principal: Principal = Depends(require_admin_principal),
) -> dict[str, Any]:
    """Return available Grafana folders for scope assignment.

    Administrators only: the folder inventory is used to assign scopes, so
    it is not exposed to ordinary users.
    """
    import json
    from app.mcp.session import mcp_call
    settings = get_settings()
    results = [
        {"uid": "*", "title": "All Folders (*)"},
    ]
    if settings.grafana_default_folder_uid:
        results.append({
            "uid": settings.grafana_default_folder_uid,
            "title": f"Default ({settings.grafana_default_folder_uid})",
        })

    try:
        raw = await mcp_call(
            "grafana_api_request",
            {"method": "GET", "endpoint": "/api/folders"},
            raw=True,
            timeout=5.0,
        )
        folders = json.loads(raw) if raw else []
        if isinstance(folders, list):
            for f in folders:
                if isinstance(f, dict) and f.get("uid") and f.get("uid") != "*":
                    if not any(r["uid"] == f["uid"] for r in results):
                        results.append({"uid": f["uid"], "title": f.get("title", f["uid"])})
    except Exception as exc:
        logger.debug("Could not query Grafana folders: %s", exc)

    _audit(
        AuditEvent.ADMIN_FOLDERS_LISTED,
        actor_id=principal.user_id,
        decision="ALLOW",
        reason="admin_list_folders",
        target_type="folder_collection",
        details={"count": len(results)},
        principal=principal,
    )

    return {"folders": results}


@router.get("/api/v1/admin/seed-audit")
async def audit_seed_references(
    principal: Principal = Depends(require_admin_principal),
) -> dict[str, Any]:
    """Report the demo seed accounts whose Grafana references are stale.

    The demo accounts name Grafana folders and dashboards by UID. Those UIDs
    belong to a particular Grafana instance; on a deployment with different
    resources they are stale, and a demo user assigned a stale UID sees
    nothing in its scope.

    This endpoint only *reports*. It never repairs a reference: replacing a
    stale folder UID with an arbitrary existing folder would grant the demo
    account access to a resource it was never meant to see. The remediation
    is an explicit administrative assignment of the real UIDs.
    """
    from app.storage.seed_audit import audit_seed_references as _build_seed_report

    report = await _build_seed_report()
    _audit(
        AuditEvent.ADMIN_SEED_AUDIT_VIEWED,
        actor_id=principal.user_id,
        principal=principal,
        decision="ALLOW",
        reason="admin_seed_audit",
        target_type="seed_audit",
        details={
            "ok": report["ok"],
            "stale_reference_count": report["stale_reference_count"],
            "grafana_available": report["grafana_available"],
        },
    )
    return report


@router.get("/api/v1/admin/audit-log")
async def read_audit_log(
    limit: int = 100,
    offset: int = 0,
    event_type: str | None = None,
    principal: Principal = Depends(require_admin_principal),
) -> dict[str, Any]:
    """Read the append-only security audit log.

    Read-only by design: the audit log has no update and no delete
    operation, so no application API can rewrite or remove a historical
    record.
    """
    limit = max(1, min(int(limit or 100), 1000))
    offset = max(0, int(offset or 0))
    with get_db_session() as session:
        records = AuditStore.list_events(
            session, limit=limit, offset=offset, event_type=event_type
        )
        events = [
            {
                "id": record.id,
                "timestamp": record.timestamp.isoformat() if record.timestamp else None,
                "event_type": record.event_type,
                "actor_id": record.actor_id,
                "actor_issuer": record.actor_issuer,
                "actor_subject": record.actor_subject,
                "target_type": record.target_type,
                "target_id": record.target_id,
                "decision": record.decision,
                "reason": record.reason,
                "previous_state": record.previous_state,
                "new_state": record.new_state,
                "details": record.details,
            }
            for record in records
        ]

    _audit(
        AuditEvent.ADMIN_AUDIT_LOG_VIEWED,
        actor_id=principal.user_id,
        principal=principal,
        decision="ALLOW",
        reason="admin_read_audit_log",
        target_type="audit_log",
        details={"limit": limit, "offset": offset, "event_type": event_type, "count": len(events)},
    )
    return {"events": events, "limit": limit, "offset": offset}


@router.put("/api/v1/admin/users/{user_id}/access")
async def update_user_access(
    user_id: str,
    update: UserAccessUpdateRequest,
    request: Request,
    principal: Principal = Depends(require_admin_principal),
) -> dict[str, Any]:
    """Update user role, status, and scopes with strict administrative guardrails (Section 5.3 & 5.4)."""
    valid_roles = {"admin", "maintainer", "operator", "observer"}
    if update.role is not None and update.role not in valid_roles:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid role. Must be one of: {sorted(valid_roles)}",
        )

    valid_statuses = {"active", "pending", "deactivated"}
    if update.status is not None and update.status not in valid_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status. Must be one of: {sorted(valid_statuses)}",
        )

    with get_db_session() as session:
        try:
            target = UserStore.update_user_access(
                session,
                admin_user_id=principal.user_id,
                target_user_id=user_id,
                role=update.role,
                status=update.status,
                scopes=update.scopes,
                admin_role=(principal.role or "").lower(),
            )
            session.commit()
        except AdminSecurityError as exc:
            record_denial(
                AuditEvent.ADMIN_ACCESS_DENIED,
                principal=principal,
                reason=str(exc),
                target_type="user",
                target_id=user_id,
                details={"action": "update_user_access"},
            )
            raise HTTPException(status_code=403, detail=str(exc))
        except KeyError:
            raise HTTPException(status_code=404, detail=f"User {user_id} not found.")

        # A scope change can introduce or remove an environment name, so the
        # deployment's environment catalogue has to be refreshed.
        from app.auth.environment import reset_environment_catalogue
        reset_environment_catalogue()

        return {
            "status": "ok",
            "user": {
                "user_id": target.user_id,
                "display_name": target.display_name,
                "username": target.username,
                "email": target.email,
                "role": target.role,
                "status": target.status,
                "authz_version": target.authz_version,
                "scopes": {
                    "allowed_folder_uids": list(target.scopes.allowed_folder_uids) if target.scopes else [],
                    "allowed_dashboard_uids": list(target.scopes.allowed_dashboard_uids) if target.scopes else [],
                    "denied_dashboard_uids": list(target.scopes.denied_dashboard_uids) if target.scopes else [],
                    "allowed_environments": list(target.scopes.allowed_environments) if target.scopes else [],
                },
            },
        }
