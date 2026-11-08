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

from app.auth.dependencies import current_principal, require_principal
from app.auth.principal import Principal
from app.catalog.runtime import reload_structured_catalog
from app.config import get_settings
from app.skill_index import SkillIndex, SkillIndexError
from app.storage.database import get_db_session
from app.storage.models import UserModel
from app.storage.user_store import AdminSecurityError, UserStore

logger = logging.getLogger(__name__)
router = APIRouter()


class UserAccessUpdateRequest(BaseModel):
    role: str | None = Field(default=None, description="One of: admin, maintainer, operator, observer")
    status: str | None = Field(default=None, description="One of: active, pending, deactivated")
    scopes: dict[str, Any] | None = Field(default=None, description="Granular scope boundaries")


@router.post("/api/v1/admin/reload-skill")
async def reload_skill(request: Request) -> dict:
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
    unchanged -- a bad edit to SKILL.md never takes the service down."""
    settings = get_settings()
    try:
        new_index = SkillIndex.load(settings.skills_root)
    except SkillIndexError as e:
        logger.error("Skill reload failed, keeping previous skill loaded: %s", e)
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
    return {
        "status": "reloaded",
        "skill_name": new_index.metadata.name,
        "skill_version": new_index.metadata.version,
        "routing_rows": len(new_index.routing_rows),
    }


@router.get("/api/v1/admin/users")
async def list_users() -> dict[str, Any]:
    """List all registered users, roles, statuses, and scopes per Section 5.3."""
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
async def list_grafana_folders(request: Request) -> dict[str, Any]:
    """Return available Grafana folders for scope assignment."""
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

    return {"folders": results}


@router.put("/api/v1/admin/users/{user_id}/access")
async def update_user_access(
    user_id: str,
    update: UserAccessUpdateRequest,
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    """Update user role, status, and scopes with strict administrative guardrails (Section 5.3 & 5.4)."""
    if not principal or principal.role != "admin":
        raise HTTPException(status_code=403, detail="Administrator role required.")

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
            )
            session.commit()
        except AdminSecurityError as exc:
            raise HTTPException(status_code=403, detail=str(exc))
        except KeyError:
            raise HTTPException(status_code=404, detail=f"User {user_id} not found.")

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
