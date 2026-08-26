import logging
from typing import Any, Dict, Optional
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.adk_agent import run_adk_agent
from app.grafana_tools.dashboard_writing import (
    PROPOSALS,
    compile_dashboard,
    execute_approved_mutation,
    refresh_preview,
)

logger = logging.getLogger("grafana_ai.routes_proposals")
router = APIRouter(tags=["proposals"])


class AdkChatRequest(BaseModel):
    message: str
    conversationId: Optional[str] = None
    sessionId: Optional[str] = None
    target: Optional[str] = None
    timeRange: Optional[str] = None


class PreviewRequest(BaseModel):
    ir: Dict[str, Any]
    panelIds: Optional[list[str]] = None


class ModifyRequest(BaseModel):
    ir: Dict[str, Any]


class ActionRequest(BaseModel):
    version: Optional[int] = None
    approvalToken: Optional[str] = None


@router.post("/api/adk/chat")
async def adk_chat_endpoint(req: AdkChatRequest):
    cid = req.conversationId or req.sessionId or "default-session"
    result = run_adk_agent(
        request=req.message,
        conversation_id=cid,
        target=req.target,
        time_range=req.timeRange,
    )
    return result


@router.get("/api/proposals/{proposal_id}")
async def get_proposal(proposal_id: str):
    try:
        proposal = PROPOSALS.get(proposal_id)
        return proposal
    except KeyError:
        raise HTTPException(status_code=404, detail="Proposal not found")


@router.post("/api/proposals/{proposal_id}/preview")
async def preview_proposal(proposal_id: str, req: PreviewRequest):
    try:
        fresh_ir = refresh_preview(req.ir, req.panelIds)
        return {"status": "success", "ir": fresh_ir}
    except Exception as e:
        logger.error("Preview refresh failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/proposals/{proposal_id}/approve")
async def approve_proposal(proposal_id: str, req: ActionRequest):
    try:
        prop = PROPOSALS.get(proposal_id)
        version = req.version if req.version is not None else prop["version"]
        approved_item = PROPOSALS.approve(proposal_id, version)
        token = approved_item.get("approvalToken") if isinstance(approved_item, dict) else approved_item
        return {"status": "approved", "approvalToken": token, "version": version}
    except KeyError:
        raise HTTPException(status_code=404, detail="Proposal not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/proposals/{proposal_id}/reject")
async def reject_proposal(proposal_id: str, req: ActionRequest):
    try:
        prop = PROPOSALS.get(proposal_id)
        version = req.version if req.version is not None else prop["version"]
        PROPOSALS.reject(proposal_id, version)
        return {"status": "rejected", "version": version}
    except KeyError:
        raise HTTPException(status_code=404, detail="Proposal not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/proposals/{proposal_id}/execute")
async def execute_proposal(proposal_id: str, req: ActionRequest):
    if not req.approvalToken:
        raise HTTPException(status_code=400, detail="Missing approval token")
    try:
        prop = PROPOSALS.get(proposal_id)
        version = req.version if req.version is not None else prop["version"]
        result = await execute_approved_mutation(proposal_id, version, req.approvalToken)
        return {"status": "success", **result}
    except KeyError:
        raise HTTPException(status_code=404, detail="Proposal not found in server memory (server restarted). Please ask to generate a new dashboard proposal.")
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Failed to execute proposal: %s", e)
        raise HTTPException(status_code=500, detail=f"Execution error: {e}")


@router.put("/api/proposals/{proposal_id}")
async def modify_proposal(proposal_id: str, req: ModifyRequest):
    try:
        updated = PROPOSALS.modify(proposal_id, req.ir)
        return updated
    except KeyError:
        raise HTTPException(status_code=404, detail="Proposal not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
