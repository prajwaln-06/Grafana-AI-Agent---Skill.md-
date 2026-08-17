"""
api/routes_admin.py

Operational endpoint for picking up SKILL.md structural changes (a new
routing-table row, a new exporter, a frontmatter version bump) without a
full process restart.

Why this is needed at all: reference file CONTENT (an existing domain
file's text, an existing overview.md's Metric Directory table) is read
fresh from disk on every request already -- see skill_index.py's
read_reference()/metric_directory(), which never cache -- so editing an
existing file's content is already live with zero action needed. Only
SKILL.md's own routing table and frontmatter are snapshotted once (in
SkillIndex.routing_rows / .metadata, computed at SkillIndex.load() time),
since those genuinely need re-parsing as a whole, not a small text read.
This endpoint is the explicit trigger for that re-parse.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from app.config import get_settings
from app.skill_index import SkillIndex, SkillIndexError

logger = logging.getLogger(__name__)
router = APIRouter()


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
    return {
        "status": "reloaded",
        "skill_name": new_index.metadata.name,
        "skill_version": new_index.metadata.version,
        "routing_rows": len(new_index.routing_rows),
    }
