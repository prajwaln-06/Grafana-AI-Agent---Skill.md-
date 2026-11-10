import copy
import logging
import os
import secrets
from typing import Any, Dict, Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.adk_agent import run_adk_agent
from app.auth.dependencies import require_principal
from app.auth.environment import EnvironmentDenied, EnvironmentRequired, resolve_environment
from app.auth.audit import AuditEvent, record_denial, record_event
from app.auth.policy import (
    PolicyDecision,
    authorize_alert_approve,
    authorize_alert_execute,
    authorize_alert_propose,
    authorize_alert_rejection,
    authorize_dashboard_approve,
    authorize_dashboard_execute,
    authorize_dashboard_propose,
    authorize_promql,
    authorize_proposal_rejection,
)
from app.auth.principal import Principal
from app.auth.resource_scope import (
    AuthorizationDenied,
    authorize_proposal_scope,
    folder_in_scope,
    resolve_destination_folder,
)
from app.config import get_settings
from app.grafana_tools.dashboard_writing import (
    PROPOSALS,
    build_proposal,
    compile_dashboard,
    execute_approved_mutation,
    extract_requested_folder_uid,
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


async def _authorize_proposal_scope(principal: Principal, proposal: Dict[str, Any] | None) -> PolicyDecision:
    """Check that the caller may see the resources a proposal targets.

    A proposal carries the dashboard and folder it will touch; reading or
    modifying it must not disclose a resource outside the caller's scope.

    The rule itself lives in :mod:`app.auth.resource_scope` so that the HTTP
    API, the ADK agent and the MCP server all apply exactly the same check.
    """
    return await authorize_proposal_scope(principal, proposal)


def _resolve_environment(principal: Principal, requested: str | None) -> str | None:
    """Resolve the environment an operation targets (403/400 on failure)."""
    try:
        return resolve_environment(principal, requested)
    except EnvironmentDenied as exc:
        raise HTTPException(status_code=403, detail=f"RBAC policy denied: {exc.reason}")
    except EnvironmentRequired as exc:
        raise HTTPException(status_code=400, detail=f"RBAC policy denied: {exc.reason}")


def _authorize_alert_proposal_scope(principal: Principal, proposal: Dict[str, Any] | None) -> bool:
    """Check that the caller may see an alert proposal.

    An alert proposal targets a Grafana folder and an environment; both must
    be inside the caller's permitted scope.
    """
    if not proposal:
        return False
    ir = proposal.get("ir") or {}
    folder_uid = ir.get("folderUID")
    if folder_uid is not None and not folder_in_scope(principal, folder_uid):
        return False
    environment = ir.get("environment")
    if environment:
        from app.auth.policy import is_environment_in_scope
        if not is_environment_in_scope(principal, environment):
            return False
    return True


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
async def post_conversation_message(
    conversation_id: str,
    req: MessageRequest,
    principal: Principal = Depends(require_principal),
):
    request_text = req.request.strip()
    if not request_text:
        raise HTTPException(status_code=400, detail="Message is required.")

    # Observers are strictly read-only and cannot mutate dashboards or alerts
    if principal.role == "observer":
        if any(w in request_text.lower() for w in ["create", "add", "make", "build", "update", "modify", "delete", "remove"]):
            raise HTTPException(
                status_code=403,
                detail="RBAC policy denied: Role 'observer' cannot propose or modify dashboards/panels.",
            )

    _append_message(
        conversation_id,
        {"id": secrets.token_urlsafe(10), "role": "user", "text": request_text},
    )

    turn = run_adk_agent(
        request=request_text,
        conversation_id=conversation_id,
        target=req.target,
        time_range=req.timeRange or "1h",
        principal=principal,
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
async def create_proposal_direct(
    req: CreateProposalRequest,
    principal: Principal = Depends(require_principal),
):
    # The destination folder is resolved and authorized before anything is
    # built, so a proposal can never be created for an unauthorized folder.
    settings = get_settings()
    try:
        destination_folder = resolve_destination_folder(
            principal,
            extract_requested_folder_uid(req.request or ""),
            default_folder_uid=settings.grafana_default_folder_uid,
        )
    except AuthorizationDenied as exc:
        raise HTTPException(status_code=exc.status_code, detail=f"RBAC policy denied: {exc.reason}")

    dec = authorize_dashboard_propose(
        principal,
        operation="create",
        folder_uid=destination_folder,
        require_target=True,
    )
    if not dec.allowed:
        record_denial(
            AuditEvent.PROPOSAL_ACCESS_DENIED,
            principal=principal,
            reason=dec.reason,
            target_type="proposal",
            details={"action": "create", "folderUid": destination_folder},
            folder_uid=destination_folder,
        )
        raise HTTPException(status_code=403, detail=f"RBAC policy denied: {dec.reason}")

    cid = secrets.token_urlsafe(18)
    turn = run_adk_agent(
        request=req.request,
        conversation_id=cid,
        target=req.target,
        time_range=req.timeRange or "1h",
        principal=principal,
    )
    if turn.get("status") == "policy_denied":
        record_denial(
            AuditEvent.PROPOSAL_ACCESS_DENIED,
            principal=principal,
            reason=str(turn.get("reason") or "the request is outside your permitted scope."),
            target_type="proposal",
            details={"action": "create"},
        )
        raise HTTPException(
            status_code=403,
            detail=f"RBAC policy denied: {turn.get('reason') or 'the request is outside your permitted scope.'}",
        )
    proposal = turn.get("proposal") or {}
    # The creation itself is audited by the proposal store (so the chat
    # pipeline and the MCP server produce the same single record); this
    # route only records the denial path.
    return proposal


@router.post("/api/adk/chat")
async def adk_chat_endpoint(
    req: AdkChatRequest,
    principal: Principal = Depends(require_principal),
):
    if principal.role == "observer":
        if any(w in req.message.lower() for w in ["create", "add", "make", "build", "update", "modify", "delete", "remove"]):
            raise HTTPException(
                status_code=403,
                detail="RBAC policy denied: Role 'observer' cannot propose or modify dashboards/panels.",
            )

    cid = req.conversationId or req.sessionId or f"sess_{secrets.token_hex(8)}"
    result = run_adk_agent(
        request=req.message,
        conversation_id=cid,
        target=req.target,
        time_range=req.timeRange,
        principal=principal,
    )
    return result


@router.get("/api/proposals/{proposal_id}")
async def get_proposal(
    proposal_id: str,
    principal: Principal = Depends(require_principal),
):
    try:
        proposal = PROPOSALS.get(proposal_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Proposal not found")
    decision = await _authorize_proposal_scope(principal, proposal)
    if not decision.allowed:
        raise HTTPException(status_code=403, detail=f"RBAC policy denied: {decision.reason}")
    return proposal



@router.post("/api/proposals/{proposal_id}/preview")
async def preview_proposal(
    proposal_id: str,
    req: PreviewRequest,
    principal: Principal = Depends(require_principal),
):
    try:
        proposal = PROPOSALS.get(proposal_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Proposal not found")
    decision = await _authorize_proposal_scope(principal, proposal)
    if not decision.allowed:
        raise HTTPException(status_code=403, detail=f"RBAC policy denied: {decision.reason}")

    # A preview executes the panel queries of a client-supplied IR, so it is
    # an environment-scoped query operation and not a neutral read.
    environment = _resolve_environment(principal, None)
    for panel in (req.ir or {}).get("panels", []) or []:
        query = str(panel.get("query") or "")
        if not query:
            continue
        prom_dec = authorize_promql(principal, is_custom=True, expr=query, environment=environment)
        if not prom_dec.allowed:
            raise HTTPException(status_code=403, detail=f"RBAC policy denied: {prom_dec.reason}")

    try:
        fresh_ir = refresh_preview(req.ir, req.panelIds)
        return {"status": "success", "ir": fresh_ir}
    except Exception as e:
        logger.error("Preview refresh failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/proposals/{proposal_id}/approve")
async def approve_proposal(
    proposal_id: str,
    req: ActionRequest,
    principal: Principal = Depends(require_principal),
):
    try:
        prop = PROPOSALS.get(proposal_id)
        # The approver's authorization is re-evaluated here, not carried over
        # from whoever created the proposal.
        dec = authorize_dashboard_approve(principal, proposal=prop)
        if not dec.allowed:
            record_denial(
                AuditEvent.PROPOSAL_ACCESS_DENIED,
                principal=principal,
                reason=dec.reason,
                target_type="proposal",
                target_id=proposal_id,
                proposal_id=proposal_id,
                details={"action": "approve"},
            )
            raise HTTPException(status_code=403, detail=f"RBAC policy denied: {dec.reason}")
        scope_dec = await _authorize_proposal_scope(principal, prop)
        if not scope_dec.allowed:
            record_denial(
                AuditEvent.SCOPE_VIOLATION,
                principal=principal,
                reason=scope_dec.reason,
                target_type="proposal",
                target_id=proposal_id,
                proposal_id=proposal_id,
                details={"action": "approve"},
            )
            raise HTTPException(status_code=403, detail=f"RBAC policy denied: {scope_dec.reason}")
        version = req.version if req.version is not None else prop["version"]
        # The approver identity comes from the authenticated principal, never
        # from the request body.
        approved_item = PROPOSALS.approve(
            proposal_id,
            version,
            approver_id=principal.user_id,
            approver_role=(principal.role or "").lower(),
        )
        token = approved_item.get("approvalToken") if isinstance(approved_item, dict) else approved_item
        record_event(
            AuditEvent.PROPOSAL_APPROVED,
            principal=principal,
            decision="ALLOW",
            reason="proposal_approved",
            target_type="proposal",
            target_id=proposal_id,
            proposal_id=proposal_id,
            impact_class=dec.impact_class,
            details={
                "version": version,
                "proposerId": prop.get("proposer_id"),
                "requiresSecondApprover": dec.requires_second_approver,
                "operation": prop.get("operation"),
                "targetResource": prop.get("target_resource"),
            },
        )
        return {"status": "approved", "approvalToken": token, "version": version}
    except KeyError:
        raise HTTPException(status_code=404, detail="Proposal not found")
    except PermissionError as e:
        record_denial(
            AuditEvent.PROPOSAL_ACCESS_DENIED,
            principal=principal,
            reason=str(e),
            target_type="proposal",
            target_id=proposal_id,
            proposal_id=proposal_id,
            details={"action": "approve"},
        )
        raise HTTPException(status_code=403, detail=f"RBAC policy denied: {e}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/proposals/{proposal_id}/reject")
async def reject_proposal(
    proposal_id: str,
    req: ActionRequest,
    principal: Principal = Depends(require_principal),
):
    try:
        prop = PROPOSALS.get(proposal_id)
        # Rejection is a reviewer's act: it follows the same role rules as
        # approval, and the reviewer must be able to see the target.
        dec = authorize_proposal_rejection(principal, proposal=prop)
        if not dec.allowed:
            record_denial(
                AuditEvent.PROPOSAL_ACCESS_DENIED,
                principal=principal,
                reason=dec.reason,
                target_type="proposal",
                target_id=proposal_id,
                proposal_id=proposal_id,
                details={"action": "reject"},
            )
            raise HTTPException(status_code=403, detail=f"RBAC policy denied: {dec.reason}")
        scope_dec = await _authorize_proposal_scope(principal, prop)
        if not scope_dec.allowed:
            record_denial(
                AuditEvent.SCOPE_VIOLATION,
                principal=principal,
                reason=scope_dec.reason,
                target_type="proposal",
                target_id=proposal_id,
                proposal_id=proposal_id,
                details={"action": "reject"},
            )
            raise HTTPException(status_code=403, detail=f"RBAC policy denied: {scope_dec.reason}")
        version = req.version if req.version is not None else prop["version"]
        PROPOSALS.reject(
            proposal_id,
            version,
            approver_id=principal.user_id,
            approver_role=(principal.role or "").lower(),
        )
        record_event(
            AuditEvent.PROPOSAL_REJECTED,
            principal=principal,
            decision="ALLOW",
            reason="proposal_rejected",
            target_type="proposal",
            target_id=proposal_id,
            proposal_id=proposal_id,
            impact_class=prop.get("impact_class"),
            details={
                "version": version,
                "proposerId": prop.get("proposer_id"),
                "operation": prop.get("operation"),
            },
        )
        return {"status": "rejected", "version": version}
    except KeyError:
        raise HTTPException(status_code=404, detail="Proposal not found")
    except PermissionError as e:
        record_denial(
            AuditEvent.PROPOSAL_ACCESS_DENIED,
            principal=principal,
            reason=str(e),
            target_type="proposal",
            target_id=proposal_id,
            proposal_id=proposal_id,
            details={"action": "reject"},
        )
        raise HTTPException(status_code=403, detail=f"RBAC policy denied: {e}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/proposals/{proposal_id}/execute")
async def execute_proposal(
    proposal_id: str,
    req: ActionRequest,
    principal: Principal = Depends(require_principal),
):
    if not req.approvalToken:
        raise HTTPException(status_code=400, detail="Missing approval token")
    try:
        prop = PROPOSALS.get(proposal_id)
        # Authorization is revalidated independently of the earlier approval,
        # both for the role and for the scope of the target resources.
        dec = authorize_dashboard_execute(principal, proposal=prop)
        if not dec.allowed:
            record_denial(
                AuditEvent.PROPOSAL_EXECUTION_FAILED,
                principal=principal,
                reason=dec.reason,
                target_type="proposal",
                target_id=proposal_id,
                proposal_id=proposal_id,
                details={"action": "execute"},
            )
            raise HTTPException(status_code=403, detail=f"RBAC policy denied: {dec.reason}")
        scope_dec = await _authorize_proposal_scope(principal, prop)
        if not scope_dec.allowed:
            record_denial(
                AuditEvent.SCOPE_VIOLATION,
                principal=principal,
                reason=scope_dec.reason,
                target_type="proposal",
                target_id=proposal_id,
                proposal_id=proposal_id,
                details={"action": "execute"},
            )
            raise HTTPException(status_code=403, detail=f"RBAC policy denied: {scope_dec.reason}")

        version = req.version if req.version is not None else prop["version"]
        result = await execute_approved_mutation(proposal_id, version, req.approvalToken)
        dashboard_url = _grafana_url(result)
        result["dashboardUrl"] = dashboard_url

        record_event(
            AuditEvent.PROPOSAL_EXECUTED,
            principal=principal,
            decision="ALLOW",
            reason="proposal_executed",
            target_type="proposal",
            target_id=proposal_id,
            proposal_id=proposal_id,
            impact_class=prop.get("impact_class"),
            details={
                "version": version,
                "operation": prop.get("operation"),
                "targetResource": prop.get("target_resource"),
            },
        )

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
    except HTTPException:
        raise
    except KeyError:
        raise HTTPException(status_code=404, detail="Proposal not found in server memory (server restarted). Please ask to generate a new dashboard proposal.")
    except PermissionError as e:
        record_denial(
            AuditEvent.PROPOSAL_EXECUTION_FAILED,
            principal=principal,
            reason=str(e),
            target_type="proposal",
            target_id=proposal_id,
            proposal_id=proposal_id,
            details={"action": "execute"},
        )
        raise HTTPException(status_code=403, detail=str(e))
    except AuthorizationDenied as e:
        record_denial(
            AuditEvent.PROPOSAL_EXECUTION_FAILED,
            principal=principal,
            reason=e.reason,
            target_type="proposal",
            target_id=proposal_id,
            proposal_id=proposal_id,
            details={"action": "execute"},
        )
        raise HTTPException(status_code=e.status_code, detail=f"RBAC policy denied: {e.reason}")
    except ValueError as e:
        record_denial(
            AuditEvent.PROPOSAL_EXECUTION_FAILED,
            principal=principal,
            reason=str(e),
            target_type="proposal",
            target_id=proposal_id,
            proposal_id=proposal_id,
            details={"action": "execute"},
        )
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        record_denial(
            AuditEvent.PROPOSAL_EXECUTION_FAILED,
            principal=principal,
            reason=f"execution_error: {e}",
            target_type="proposal",
            target_id=proposal_id,
            proposal_id=proposal_id,
            details={"action": "execute"},
        )
        logger.exception("Failed to execute proposal: %s", e)
        raise HTTPException(status_code=500, detail=f"Execution error: {e}")


@router.put("/api/proposals/{proposal_id}")
async def modify_proposal(
    proposal_id: str,
    req: ModifyRequest,
    principal: Principal = Depends(require_principal),
):
    try:
        # The lookup proves the proposal exists before it is modified.
        PROPOSALS.get(proposal_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Proposal not found")

    # A modified IR is client-supplied content: its target must be inside the
    # caller's scope before it is stored as a new proposal version.
    scope_dec = await _authorize_proposal_scope(principal, {"ir": req.ir or {}})
    if not scope_dec.allowed:
        raise HTTPException(status_code=403, detail=f"RBAC policy denied: {scope_dec.reason}")

    operation = str((req.ir or {}).get("operation") or "update")
    dec = authorize_dashboard_propose(
        principal,
        operation=operation,
        dashboard_uid=(req.ir or {}).get("dashboardUid"),
        folder_uid=(req.ir or {}).get("folderUid"),
    )
    if not dec.allowed:
        raise HTTPException(status_code=403, detail=f"RBAC policy denied: {dec.reason}")
    try:
        updated = PROPOSALS.modify(proposal_id, req.ir)
        # The modification itself is audited by the proposal store.
        return updated
    except KeyError:
        raise HTTPException(status_code=404, detail="Proposal not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------------------------------------------------------------------------
# Alert rule proposal routes
# ---------------------------------------------------------------------------

@router.get("/api/alerts")
async def get_alerts(principal: Principal = Depends(require_principal)):
    """The alert rules the caller may manage (restricted to their folders)."""
    return {"alerts": list_alert_rules(principal)}


@router.get("/api/alert-proposals")
async def list_alert_proposals(principal: Principal = Depends(require_principal)):
    """The alert proposals the caller may see."""
    visible = []
    for proposal in ALERT_PROPOSALS.list_all():
        if _authorize_alert_proposal_scope(principal, proposal):
            visible.append(proposal)
    return {"proposals": visible}


@router.get("/api/alert-proposals/{proposal_id}")
async def get_alert_proposal(
    proposal_id: str,
    principal: Principal = Depends(require_principal),
):
    try:
        proposal = ALERT_PROPOSALS.get(proposal_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Alert proposal not found")
    if not _authorize_alert_proposal_scope(principal, proposal):
        raise HTTPException(
            status_code=403,
            detail="RBAC policy denied: this alert proposal is outside your permitted scope.",
        )
    return proposal


@router.post("/api/alert-proposals/{proposal_id}/approve")
async def approve_alert_proposal(
    proposal_id: str,
    req: ActionRequest,
    principal: Principal = Depends(require_principal),
):
    try:
        prop = ALERT_PROPOSALS.get(proposal_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Alert proposal not found")
    if not _authorize_alert_proposal_scope(principal, prop):
        record_denial(
            AuditEvent.SCOPE_VIOLATION,
            principal=principal,
            reason="this alert proposal is outside your permitted scope.",
            target_type="alert_proposal",
            target_id=proposal_id,
            proposal_id=proposal_id,
            details={"action": "approve"},
        )
        raise HTTPException(
            status_code=403,
            detail="RBAC policy denied: this alert proposal is outside your permitted scope.",
        )
    # The approver's own authorization is evaluated here -- including the
    # environment the rule targets.
    dec = authorize_alert_approve(principal, environment=(prop.get("ir") or {}).get("environment") or None)
    if not dec.allowed:
        record_denial(
            AuditEvent.PROPOSAL_ACCESS_DENIED,
            principal=principal,
            reason=dec.reason,
            target_type="alert_proposal",
            target_id=proposal_id,
            proposal_id=proposal_id,
            details={"action": "approve"},
        )
        raise HTTPException(status_code=403, detail=f"RBAC policy denied: {dec.reason}")
    # Separation of duties: a destructive alert proposal cannot be approved
    # by its own proposer.
    if prop.get("requires_second_approver") and prop.get("proposer_id") == principal.user_id:
        record_denial(
            AuditEvent.PROPOSAL_ACCESS_DENIED,
            principal=principal,
            reason="Policy Denied: Separation of duties requires a second, different approver.",
            target_type="alert_proposal",
            target_id=proposal_id,
            proposal_id=proposal_id,
            details={"action": "approve"},
        )
        raise HTTPException(
            status_code=403,
            detail="RBAC policy denied: Policy Denied: Separation of duties requires a second, different approver.",
        )
    try:
        version = req.version if req.version is not None else prop["version"]
        approved = ALERT_PROPOSALS.approve(
            proposal_id,
            version,
            approver_id=principal.user_id,
            approver_role=(principal.role or "").lower(),
        )
        record_event(
            AuditEvent.ALERT_APPROVED,
            principal=principal,
            decision="ALLOW",
            reason="alert_proposal_approved",
            target_type="alert_proposal",
            target_id=proposal_id,
            proposal_id=proposal_id,
            folder_uid=(prop.get("ir") or {}).get("folderUID"),
            environment=(prop.get("ir") or {}).get("environment") or None,
            impact_class=prop.get("impact_class"),
            details={
                "version": version,
                "proposerId": prop.get("proposer_id"),
                "title": (prop.get("ir") or {}).get("title"),
            },
        )
        return approved
    except PermissionError as e:
        record_denial(
            AuditEvent.PROPOSAL_ACCESS_DENIED,
            principal=principal,
            reason=str(e),
            target_type="alert_proposal",
            target_id=proposal_id,
            proposal_id=proposal_id,
            details={"action": "approve"},
        )
        raise HTTPException(status_code=403, detail=f"RBAC policy denied: {e}")
    except KeyError:
        raise HTTPException(status_code=404, detail="Alert proposal not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/alert-proposals/{proposal_id}/reject")
async def reject_alert_proposal(
    proposal_id: str,
    req: ActionRequest,
    principal: Principal = Depends(require_principal),
):
    try:
        prop = ALERT_PROPOSALS.get(proposal_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Alert proposal not found")
    # The reviewer must be able to see the target, and -- as with the
    # dashboard proposals -- may not reject their own proposal.
    if not _authorize_alert_proposal_scope(principal, prop):
        record_denial(
            AuditEvent.SCOPE_VIOLATION,
            principal=principal,
            reason="this alert proposal is outside your permitted scope.",
            target_type="alert_proposal",
            target_id=proposal_id,
            proposal_id=proposal_id,
            details={"action": "reject"},
        )
        raise HTTPException(
            status_code=403,
            detail="RBAC policy denied: this alert proposal is outside your permitted scope.",
        )
    # Rejection follows the same role rule as approval, and the same
    # separation-of-duties rule: a proposer may not reject their own
    # proposal.
    reject_dec = authorize_alert_rejection(principal, prop)
    if not reject_dec.allowed:
        record_denial(
            AuditEvent.PROPOSAL_ACCESS_DENIED,
            principal=principal,
            reason=reject_dec.reason,
            target_type="alert_proposal",
            target_id=proposal_id,
            proposal_id=proposal_id,
            details={"action": "reject"},
        )
        raise HTTPException(
            status_code=403,
            detail=f"RBAC policy denied: {reject_dec.reason}",
        )
    try:
        version = req.version if req.version is not None else prop["version"]
        rejected = ALERT_PROPOSALS.reject(
            proposal_id,
            version,
            approver_id=principal.user_id,
            approver_role=(principal.role or "").lower(),
        )
        record_event(
            AuditEvent.ALERT_REJECTED,
            principal=principal,
            decision="ALLOW",
            reason="alert_proposal_rejected",
            target_type="alert_proposal",
            target_id=proposal_id,
            proposal_id=proposal_id,
            folder_uid=(prop.get("ir") or {}).get("folderUID"),
            environment=(prop.get("ir") or {}).get("environment") or None,
            impact_class=prop.get("impact_class"),
            details={
                "version": version,
                "proposerId": prop.get("proposer_id"),
                "title": (prop.get("ir") or {}).get("title"),
            },
        )
        return rejected
    except KeyError:
        raise HTTPException(status_code=404, detail="Alert proposal not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/alert-proposals/{proposal_id}/execute")
async def execute_alert_proposal(
    proposal_id: str,
    req: ActionRequest,
    principal: Principal = Depends(require_principal),
):
    if not req.approvalToken:
        raise HTTPException(status_code=400, detail="Missing approval token")
    try:
        prop = ALERT_PROPOSALS.get(proposal_id)
        if not _authorize_alert_proposal_scope(principal, prop):
            record_denial(
                AuditEvent.SCOPE_VIOLATION,
                principal=principal,
                reason="this alert proposal is outside your permitted scope.",
                target_type="alert_proposal",
                target_id=proposal_id,
                proposal_id=proposal_id,
                details={"action": "execute"},
            )
            raise HTTPException(
                status_code=403,
                detail="RBAC policy denied: this alert proposal is outside your permitted scope.",
            )
        # The executor's authorization is re-evaluated here; the earlier
        # approval is never trusted on its own.
        dec = authorize_alert_execute(principal, environment=(prop.get("ir") or {}).get("environment") or None)
        if not dec.allowed:
            record_denial(
                AuditEvent.PROPOSAL_EXECUTION_FAILED,
                principal=principal,
                reason=dec.reason,
                target_type="alert_proposal",
                target_id=proposal_id,
                proposal_id=proposal_id,
                details={"action": "execute"},
            )
            raise HTTPException(status_code=403, detail=f"RBAC policy denied: {dec.reason}")
        version = req.version if req.version is not None else prop["version"]
        result = await execute_approved_alert(proposal_id, version, req.approvalToken)
        record_event(
            AuditEvent.PROPOSAL_EXECUTED,
            principal=principal,
            decision="ALLOW",
            reason="alert_proposal_executed",
            target_type="alert_proposal",
            target_id=proposal_id,
            proposal_id=proposal_id,
            folder_uid=(prop.get("ir") or {}).get("folderUID"),
            environment=(prop.get("ir") or {}).get("environment") or None,
            impact_class=prop.get("impact_class"),
            details={"version": version},
        )
        return {"status": "success", **result}
    except HTTPException:
        raise
    except KeyError:
        raise HTTPException(status_code=404, detail="Alert proposal not found")
    except PermissionError as e:
        record_denial(
            AuditEvent.PROPOSAL_EXECUTION_FAILED,
            principal=principal,
            reason=str(e),
            target_type="alert_proposal",
            target_id=proposal_id,
            proposal_id=proposal_id,
            details={"action": "execute"},
        )
        raise HTTPException(status_code=403, detail=str(e))
    except AuthorizationDenied as e:
        record_denial(
            AuditEvent.PROPOSAL_EXECUTION_FAILED,
            principal=principal,
            reason=e.reason,
            target_type="alert_proposal",
            target_id=proposal_id,
            proposal_id=proposal_id,
            details={"action": "execute"},
        )
        raise HTTPException(status_code=e.status_code, detail=f"RBAC policy denied: {e.reason}")
    except Exception as e:
        record_denial(
            AuditEvent.PROPOSAL_EXECUTION_FAILED,
            principal=principal,
            reason=f"execution_error: {e}",
            target_type="alert_proposal",
            target_id=proposal_id,
            proposal_id=proposal_id,
            details={"action": "execute"},
        )
        logger.exception("Failed to execute alert proposal: %s", e)
        raise HTTPException(status_code=500, detail=f"Execution error: {e}")

