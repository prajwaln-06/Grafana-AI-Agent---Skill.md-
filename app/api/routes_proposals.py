import copy
import logging
import os
import secrets
from typing import Any, Dict, Optional
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.adk_agent import run_adk_agent
from app.grafana_tools.dashboard_writing import (
    PROPOSALS,
    build_proposal,
    compile_dashboard,
    execute_approved_mutation,
    refresh_preview,
)
from app.grafana_tools.alerting import (
    ALERT_PROPOSALS,
    execute_approved_alert,
    list_alert_rules,
)

logger = logging.getLogger("grafana_ai.routes_proposals")
router = APIRouter(tags=["proposals"])

_CONVERSATIONS: Dict[str, Dict[str, Any]] = {}


def _get_conversation(cid: str) -> Dict[str, Any]:
    if cid not in _CONVERSATIONS:
        _CONVERSATIONS[cid] = {
            "id": cid,
            "messages": [
                {
                    "id": "welcome",
                    "role": "assistant",
                    "kind": "text",
                    "text": "Ask me to inspect, query, or propose changes to your dashboards.",
                    "agent_response": "Ask me to inspect, query, or propose changes to your dashboards.",
                }
            ],
        }
    return _CONVERSATIONS[cid]


def _append_message(cid: str, msg: Dict[str, Any]) -> None:
    conv = _get_conversation(cid)
    conv["messages"].append(msg)


def _grafana_url(result: dict) -> str | None:
    path = result.get("url") or (result.get("grafanaResult") or {}).get("url")
    if not path:
        return None
    base = os.environ.get("GRAFANA_URL", "http://localhost:3000").rstrip("/")
    return f"{base}{path}" if path.startswith("/") else path


class AdkChatRequest(BaseModel):
    message: str
    conversationId: Optional[str] = None
    sessionId: Optional[str] = None
    target: Optional[str] = None
    timeRange: Optional[str] = None


class MessageRequest(BaseModel):
    request: str
    target: Optional[str] = None
    timeRange: Optional[str] = "1h"


class CreateConversationRequest(BaseModel):
    conversationId: Optional[str] = None


class CreateProposalRequest(BaseModel):
    request: str
    target: Optional[str] = None
    timeRange: Optional[str] = "1h"


class PreviewRequest(BaseModel):
    ir: Dict[str, Any]
    panelIds: Optional[list[str]] = None


class ModifyRequest(BaseModel):
    ir: Dict[str, Any]


class ActionRequest(BaseModel):
    version: Optional[int] = None
    approvalToken: Optional[str] = None
    conversationId: Optional[str] = None


@router.post("/api/conversations")
async def create_conversation(req: Optional[CreateConversationRequest] = None):
    cid = (req and req.conversationId) or secrets.token_urlsafe(18)
    return _get_conversation(cid)


@router.get("/api/conversations/{conversation_id}")
async def get_conversation(conversation_id: str):
    return _get_conversation(conversation_id)


@router.post("/api/conversations/{conversation_id}/messages")
async def post_conversation_message(conversation_id: str, req: MessageRequest):
    request_text = req.request.strip()
    if not request_text:
        raise HTTPException(status_code=400, detail="Message is required.")

    _append_message(
        conversation_id,
        {"id": secrets.token_urlsafe(10), "role": "user", "text": request_text},
    )

    turn = run_adk_agent(
        request=request_text,
        conversation_id=conversation_id,
        target=req.target,
        time_range=req.timeRange or "1h",
    )

    assistant_msg = {
        "id": secrets.token_urlsafe(10),
        "role": "assistant",
        "text": turn["text"],
        "kind": turn["kind"],
        "agent_response": turn.get("agent_response", turn["text"]),
        "raw_json": turn.get("raw_json", []),
    }
    if turn.get("proposalId"):
        assistant_msg["proposalId"] = turn["proposalId"]
        assistant_msg["proposal"] = turn.get("proposal")
    if turn.get("timings"):
        assistant_msg["timings"] = turn["timings"]

    _append_message(conversation_id, assistant_msg)

    conv = copy.deepcopy(_get_conversation(conversation_id))
    conv["message"] = assistant_msg
    return conv


@router.post("/api/proposals")
async def create_proposal_direct(req: CreateProposalRequest):
    cid = secrets.token_urlsafe(18)
    turn = run_adk_agent(
        request=req.request,
        conversation_id=cid,
        target=req.target,
        time_range=req.timeRange or "1h",
    )
    return turn.get("proposal")


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
        dashboard_url = _grafana_url(result)
        result["dashboardUrl"] = dashboard_url

        if req.conversationId:
            exec_msg = {
                "id": secrets.token_urlsafe(10),
                "role": "assistant",
                "kind": "execution",
                "text": f"Dashboard '{prop.get('ir', {}).get('name', 'Dashboard')}' successfully applied to Grafana.",
                "agent_response": f"Dashboard '{prop.get('ir', {}).get('name', 'Dashboard')}' successfully applied to Grafana.",
                "dashboardUrl": dashboard_url,
            }
            _append_message(req.conversationId, exec_msg)
            result["message"] = exec_msg

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


# ---------------------------------------------------------------------------
# Alert rule proposal routes
# ---------------------------------------------------------------------------

@router.get("/api/alerts")
async def get_alerts():
    return {"alerts": list_alert_rules()}


@router.get("/api/alert-proposals")
async def list_alert_proposals():
    return {"proposals": ALERT_PROPOSALS.list_all()}


@router.get("/api/alert-proposals/{proposal_id}")
async def get_alert_proposal(proposal_id: str):
    try:
        return ALERT_PROPOSALS.get(proposal_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Alert proposal not found")


@router.post("/api/alert-proposals/{proposal_id}/approve")
async def approve_alert_proposal(proposal_id: str, req: ActionRequest):
    try:
        prop = ALERT_PROPOSALS.get(proposal_id)
        version = req.version if req.version is not None else prop["version"]
        approved = ALERT_PROPOSALS.approve(proposal_id, version)
        return approved
    except KeyError:
        raise HTTPException(status_code=404, detail="Alert proposal not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/alert-proposals/{proposal_id}/reject")
async def reject_alert_proposal(proposal_id: str, req: ActionRequest):
    try:
        prop = ALERT_PROPOSALS.get(proposal_id)
        version = req.version if req.version is not None else prop["version"]
        rejected = ALERT_PROPOSALS.reject(proposal_id, version)
        return rejected
    except KeyError:
        raise HTTPException(status_code=404, detail="Alert proposal not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/alert-proposals/{proposal_id}/execute")
async def execute_alert_proposal(proposal_id: str, req: ActionRequest):
    if not req.approvalToken:
        raise HTTPException(status_code=400, detail="Missing approval token")
    try:
        prop = ALERT_PROPOSALS.get(proposal_id)
        version = req.version if req.version is not None else prop["version"]
        result = await execute_approved_alert(proposal_id, version, req.approvalToken)
        return {"status": "success", **result}
    except KeyError:
        raise HTTPException(status_code=404, detail="Alert proposal not found")
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        logger.exception("Failed to execute alert proposal: %s", e)
        raise HTTPException(status_code=500, detail=f"Execution error: {e}")

