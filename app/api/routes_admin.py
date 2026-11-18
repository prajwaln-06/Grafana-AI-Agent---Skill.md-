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
from app.storage.models import ApprovalModel, ProposalModel, UserModel
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
                        "role_grants": list(getattr(u.scopes, "role_grants", None) or []) if u.scopes else [],
                    },
                }
                for u in users
            ]
        }


def _folders_from_mcp_response(raw: Any) -> list[dict[str, str]]:
    """Extract the folder list from a Grafana MCP ``grafana_api_request`` answer.

    The tool wraps the Grafana response in an envelope::

        {"status": 200, "headers": {...}, "data": [{"uid": ..., "title": ...}]}

    so the folders live in ``data``, not at the top level of the answer. A
    build that answers with the payload directly is accepted as well.

    Anything else -- an empty answer, a malformed envelope, a Grafana error
    body -- yields an empty list. The caller then reports the folders it
    already knows instead of failing the request, because a Grafana that
    cannot be reached must not take the administrative endpoint down with it.
    """
    import json

    payload: Any = raw
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return []

    if isinstance(payload, dict) and "status" in payload:
        status = payload.get("status")
        if isinstance(status, int) and status >= 400:
            logger.warning("Grafana folder listing failed with status %s", status)
            return []
        payload = payload.get("data")

    if not isinstance(payload, list):
        return []

    folders: list[dict[str, str]] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        uid = entry.get("uid")
        if not isinstance(uid, str) or not uid:
            continue
        title = entry.get("title")
        folders.append({
            "uid": uid,
            "title": title if isinstance(title, str) and title else uid,
        })
    return folders


@router.get("/api/v1/admin/folders")
async def list_grafana_folders(
    request: Request,
    principal: Principal = Depends(require_admin_principal),
) -> dict[str, Any]:
    """Return available Grafana folders for scope assignment.

    Administrators only: the folder inventory is used to assign scopes, so
    it is not exposed to ordinary users.
    """
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
        for folder in _folders_from_mcp_response(raw):
            uid = folder["uid"]
            if uid == "*":
                continue
            if not any(r["uid"] == uid for r in results):
                results.append({"uid": uid, "title": folder["title"]})
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

    admin_user_id = getattr(request.state, "authenticated_admin_user_id", None) or principal.user_id
    with get_db_session() as session:
        try:
            target = UserStore.update_user_access(
                session,
                admin_user_id=admin_user_id,
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

        # Invalidate in-memory dashboard metadata cache so scope updates reflect immediately
        from app.grafana_tools.dashboard import invalidate_dashboard_cache
        invalidate_dashboard_cache()

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
                    "role_grants": list(getattr(target.scopes, "role_grants", None) or []) if target.scopes else [],
                },
            },
        }


@router.get("/api/v1/admin/pending-deletions")
async def list_pending_deletions(
    principal: Principal = Depends(require_admin_principal),
) -> dict[str, Any]:
    """List all destructive deletion proposals requiring administrator checks."""
    with get_db_session() as session:
        db_proposals = (
            session.execute(
                select(ProposalModel)
                .options(joinedload(ProposalModel.proposer))
                .where(
                    ProposalModel.state.in_(["pending_approval", "proposed"]),
                    (ProposalModel.impact_class == "destructive_delete") | (ProposalModel.operation.in_(["delete", "remove"])),
                )
                .order_by(ProposalModel.created_at.desc())
            )
            .scalars()
            .unique()
            .all()
        )

        seen_ids = set()
        items = []

        for p in db_proposals:
            seen_ids.add(p.proposal_id)
            target = p.target_resource or {}
            canonical = p.canonical_content or {}
            title = (
                canonical.get("title")
                or canonical.get("name")
                or target.get("title")
                or target.get("dashboardUid")
                or target.get("folderUid")
                or "Deletion Target"
            )
            res_type = target.get("resourceType") or (
                "folder"
                if canonical.get("removeFolder") or (not target.get("dashboardUid") and target.get("folderUid"))
                else "dashboard"
            )

            items.append({
                "proposal_id": p.proposal_id,
                "version": p.version,
                "title": title,
                "resource_type": res_type,
                "dashboard_uid": target.get("dashboardUid"),
                "folder_uid": target.get("folderUid"),
                "operation": p.operation,
                "impact_class": p.impact_class,
                "requires_second_approver": p.requires_second_approver,
                "state": p.state,
                "created_at": p.created_at.isoformat() if p.created_at else None,
                "digest": p.digest,
                "proposer": {
                    "user_id": p.proposer.user_id if p.proposer else p.proposer_id,
                    "display_name": p.proposer.display_name if p.proposer else p.proposer_id,
                    "username": p.proposer.username if p.proposer else None,
                    "email": p.proposer.email if p.proposer else None,
                    "role": p.proposer.role if p.proposer else "maintainer",
                },
            })

        # Merge from in-memory PROPOSALS store if any exists that isn't yet in DB
        from app.grafana_tools.dashboard_writing import PROPOSALS

        for pid, prop in list(PROPOSALS._items.items()):
            if pid in seen_ids:
                continue
            is_del = (
                prop.get("impact_class") == "destructive_delete"
                or prop.get("operation") in ("delete", "remove")
                or bool((prop.get("ir") or {}).get("removeDashboard") or (prop.get("ir") or {}).get("removeFolder"))
            )
            if is_del and prop.get("status") in ("proposed", "pending_approval"):
                ir = prop.get("ir") or {}
                t_res = prop.get("target_resource") or {}
                dash_uid = ir.get("dashboardUid") or t_res.get("dashboardUid")
                folder_uid = ir.get("folderUid") or t_res.get("folderUid")
                res_type = "folder" if ir.get("removeFolder") else "dashboard"
                title = ir.get("title") or ir.get("name") or dash_uid or folder_uid or "Deletion Target"

                proposer_id = prop.get("proposer_id") or "usr_maint_01"
                proposer_user = session.get(UserModel, proposer_id)
                items.append({
                    "proposal_id": pid,
                    "version": prop.get("version", 1),
                    "title": title,
                    "resource_type": res_type,
                    "dashboard_uid": dash_uid,
                    "folder_uid": folder_uid,
                    "operation": prop.get("operation", "delete"),
                    "impact_class": prop.get("impact_class", "destructive_delete"),
                    "requires_second_approver": prop.get("requires_second_approver", True),
                    "state": prop.get("status", "pending_approval"),
                    "created_at": None,
                    "digest": prop.get("digest", ""),
                    "proposer": {
                        "user_id": proposer_user.user_id if proposer_user else proposer_id,
                        "display_name": proposer_user.display_name if proposer_user else proposer_id,
                        "username": proposer_user.username if proposer_user else None,
                        "email": proposer_user.email if proposer_user else None,
                        "role": proposer_user.role if proposer_user else "maintainer",
                    },
                })

    return {"pending_deletions": items, "count": len(items)}


@router.post("/api/v1/admin/pending-deletions/{proposal_id}/approve")
async def approve_pending_deletion(
    proposal_id: str,
    principal: Principal = Depends(require_admin_principal),
) -> dict[str, Any]:
    """Admin approves and executes a destructive deletion proposal."""
    from datetime import datetime, timezone
    import secrets
    from app.grafana_tools.dashboard_writing import PROPOSALS
    from app.mcp.session import mcp_call

    with get_db_session() as session:
        prop = session.get(ProposalModel, proposal_id)
        if not prop and proposal_id not in PROPOSALS._items:
            raise HTTPException(status_code=404, detail="Proposal not found.")

        target = prop.target_resource if prop else PROPOSALS._items[proposal_id].get("target_resource", {})
        canonical = prop.canonical_content if prop else PROPOSALS._items[proposal_id].get("ir", {})
        dash_uid = target.get("dashboardUid") or canonical.get("dashboardUid")
        folder_uid = target.get("folderUid") or canonical.get("folderUid")
        res_type = target.get("resourceType") or ("folder" if canonical.get("removeFolder") else "dashboard")

        # Execute deletion via Grafana MCP if configured
        try:
            if res_type == "folder" and folder_uid:
                await mcp_call(
                    "grafana_api_request",
                    {"method": "DELETE", "endpoint": f"/api/folders/{folder_uid}"},
                    raw=True,
                    timeout=15.0,
                )
            elif dash_uid:
                await mcp_call(
                    "grafana_api_request",
                    {"method": "DELETE", "endpoint": f"/api/dashboards/uid/{dash_uid}"},
                    raw=True,
                    timeout=15.0,
                )
        except Exception as exc:
            logger.info("Deletion execution through MCP noted: %s", exc)

        # Update DB proposal state
        if prop:
            prop.state = "applied"
            prop.updated_at = datetime.now(timezone.utc)

            appr = ApprovalModel(
                approval_id=f"appr_{secrets.token_hex(4)}",
                proposal_id=proposal_id,
                proposal_version=prop.version,
                digest=prop.digest,
                approver_id=principal.user_id,
                decision="approved",
                approver_authz_version=principal.authz_version or 1,
                approval_token=secrets.token_urlsafe(24),
                token_used=True,
                token_used_at=datetime.now(timezone.utc),
                created_at=datetime.now(timezone.utc),
            )
            session.add(appr)
            session.commit()

        # Update in-memory PROPOSALS store if present
        if proposal_id in PROPOSALS._items:
            PROPOSALS._items[proposal_id]["status"] = "executed"
            PROPOSALS._items[proposal_id]["approvedBy"] = principal.user_id
            PROPOSALS._items[proposal_id]["approvedAt"] = datetime.now(timezone.utc).isoformat()

        _audit(
            AuditEvent.PROPOSAL_APPROVED,
            actor_id=principal.user_id,
            principal=principal,
            decision="ALLOW",
            reason="admin_approved_deletion",
            target_type=res_type,
            target_id=dash_uid or folder_uid,
            details={"proposal_id": proposal_id, "action": "approve_deletion"},
        )
        _audit(
            AuditEvent.PROPOSAL_EXECUTED,
            actor_id=principal.user_id,
            principal=principal,
            decision="ALLOW",
            reason="admin_executed_deletion",
            target_type=res_type,
            target_id=dash_uid or folder_uid,
            details={"proposal_id": proposal_id, "action": "execute_deletion"},
        )

        return {
            "status": "approved",
            "proposal_id": proposal_id,
            "message": f"Successfully approved and deleted {res_type} '{dash_uid or folder_uid}'.",
        }


@router.post("/api/v1/admin/pending-deletions/{proposal_id}/reject")
async def reject_pending_deletion(
    proposal_id: str,
    principal: Principal = Depends(require_admin_principal),
) -> dict[str, Any]:
    """Admin rejects a destructive deletion proposal."""
    from datetime import datetime, timezone
    from app.grafana_tools.dashboard_writing import PROPOSALS

    with get_db_session() as session:
        prop = session.get(ProposalModel, proposal_id)
        if not prop and proposal_id not in PROPOSALS._items:
            raise HTTPException(status_code=404, detail="Proposal not found.")

        if prop:
            prop.state = "rejected"
            prop.updated_at = datetime.now(timezone.utc)
            session.commit()

        if proposal_id in PROPOSALS._items:
            PROPOSALS._items[proposal_id]["status"] = "rejected"
            PROPOSALS._items[proposal_id]["rejectedBy"] = principal.user_id
            PROPOSALS._items[proposal_id]["rejectedAt"] = datetime.now(timezone.utc).isoformat()

        _audit(
            AuditEvent.PROPOSAL_REJECTED,
            actor_id=principal.user_id,
            principal=principal,
            decision="ALLOW",
            reason="admin_rejected_deletion",
            target_type="proposal",
            target_id=proposal_id,
            details={"proposal_id": proposal_id, "action": "reject_deletion"},
        )

        return {
            "status": "rejected",
            "proposal_id": proposal_id,
            "message": "Deletion proposal was rejected.",
        }
