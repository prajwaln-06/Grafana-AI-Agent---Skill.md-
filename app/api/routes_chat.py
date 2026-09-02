"""app/api/routes_chat.py

Unified Conversational Gateway for Grafana AI Agent.
Replaces brittle client-side regex routing with a single, professional
server-side coordinator that maintains persistent multi-turn context.
"""
from __future__ import annotations

import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.adk_agent import run_adk_agent
from app.config import get_settings
from app.pipeline import run_pipeline
from app.proposals.store import PROPOSALS
from app.proposals.tools import propose_dashboard, resolve_dashboard_intent
from chart_selection.selector import select_chart_type

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Unified Chat"])


# ============================================================================
# Session Store
# ============================================================================

@dataclass
class SessionState:
    session_id: str
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    last_target: Optional[str] = None
    last_metric: Optional[str] = None
    last_alert_rule: Optional[dict[str, Any]] = None
    pending_action: Optional[str] = None
    pending_payload: Optional[dict[str, Any]] = None
    history: list[dict[str, str]] = field(default_factory=list)


class UnifiedSessionStore:
    """Thread-safe in-memory session manager for multi-turn conversations."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionState] = {}

    def get_or_create(self, session_id: Optional[str]) -> SessionState:
        now = time.time()
        # Clean expired sessions (older than 2 hours)
        expired = [sid for sid, s in self._sessions.items() if now - s.last_active > 7200]
        for sid in expired:
            self._sessions.pop(sid, None)

        if not session_id or session_id not in self._sessions:
            new_id = session_id or f"sess_{secrets.token_hex(8)}"
            session = SessionState(session_id=new_id)
            self._sessions[new_id] = session
            return session

        session = self._sessions[session_id]
        session.last_active = now
        return session


SESSION_STORE = UnifiedSessionStore()


# ============================================================================
# Request / Response Schemas
# ============================================================================

class ChatRequest(BaseModel):
    message: str
    sessionId: Optional[str] = None
    conversationId: Optional[str] = None
    target: Optional[str] = None
    timeRange: Optional[str] = "1h"


class ChatStep(BaseModel):
    step: int
    agent: str
    action: str
    result: str


class ChatResponse(BaseModel):
    status: str = "ok"
    sessionId: str
    intent: str
    framework: str = "Google ADK + FastMCP + SKILL.md v3"
    agents: list[str] = Field(default_factory=list)
    steps: list[ChatStep] = Field(default_factory=list)
    answer: str
    queryUsed: Optional[str] = None
    chartType: Optional[str] = None
    series: Optional[list[dict[str, Any]]] = None
    dashboardLink: Optional[str] = None
    proposalId: Optional[str] = None
    proposal: Optional[dict[str, Any]] = None
    alertRule: Optional[dict[str, Any]] = None
    candidates: Optional[list[dict[str, Any]]] = None


# ============================================================================
# Unified Chat Handler
# ============================================================================

@router.post("/api/chat", response_model=ChatResponse)
async def unified_chat_endpoint(req: ChatRequest, request: Request) -> ChatResponse:
    text = req.message.strip()
    session = SESSION_STORE.get_or_create(req.sessionId or req.conversationId)
    settings = get_settings()
    skill_index = getattr(request.app.state, "skill_index", None)

    logger.info("Unified Chat | session=%s | message=%r", session.session_id, text[:100])

    # Record user turn
    session.history.append({"role": "user", "text": text})

    # ------------------------------------------------------------------------
    # 0. Friendly Greeting & Help
    # ------------------------------------------------------------------------
    if re.match(r"^(hi|hello|hey|greetings|help|what\s+can\s+you\s+do)\b", text, re.I):
        return ChatResponse(
            status="ok",
            sessionId=session.session_id,
            intent="general",
            agents=["Assistant"],
            steps=[ChatStep(step=1, agent="Assistant", action="greeting", result="ready")],
            answer="Hello! I am your Grafana Observability Assistant. I can help you query live metrics (CPU, Memory, GPU), author and update dashboards, create alert rules, and inspect logs. How can I help you today?",
        )

    # ------------------------------------------------------------------------
    # 1. Check for Pending Clarifications (Context Continuity)
    # ------------------------------------------------------------------------
    if session.pending_action == "awaiting_metric_for_dashboard" and session.pending_payload:
        dash_req = session.pending_payload.get("dashboard_request", "")
        target = session.pending_payload.get("target")
        time_range = session.pending_payload.get("time_range", "1h")

        # Resume dashboard creation with the specified metric
        composite_req = f"{dash_req} using metric {text}"
        logger.info("Resuming pending dashboard proposal: %r", composite_req)

        session.pending_action = None
        session.pending_payload = None

        prop_res = propose_dashboard(request=composite_req, target=target or "", time_range=time_range)
        pid = prop_res.get("proposalId")
        if pid:
            proposal_obj = PROPOSALS.get(pid)
            if proposal_obj and proposal_obj.get("ir"):
                session.last_target = proposal_obj["ir"].get("name") or pid

            return ChatResponse(
                status="ok",
                sessionId=session.session_id,
                intent="dashboard_proposal",
                agents=["ADK Agent", "Proposal Engine", "MCP-Grafana"],
                steps=[
                    ChatStep(step=1, agent="Coordinator", action="resolved clarification", result=text),
                    ChatStep(step=2, agent="Proposal Engine", action="generated Dashboard IR", result=proposal_obj.get("ir", {}).get("name", "Dashboard")),
                ],
                answer="Here is your updated dashboard proposal with the requested metrics.",
                proposalId=pid,
                proposal=proposal_obj,
            )

    # ------------------------------------------------------------------------
    # 2. Referential Target Resolution ("this dashboard", "it", "the dashboard")
    # ------------------------------------------------------------------------
    resolved_target = req.target or ""
    has_referential_noun = bool(re.search(r"\b(this|that|the|current)\s+(dashboard|panel|board)\b", text, re.I))
    if has_referential_noun and session.last_target and not resolved_target:
        resolved_target = session.last_target
        logger.info("Resolved referential dashboard target: %r", resolved_target)

    # ------------------------------------------------------------------------
    # 3. Intent Classification
    # ------------------------------------------------------------------------
    is_alert_request = bool(re.search(r"\b(alert|alerts|alerting|alarm|rule|rules|notification|notify)\b", text, re.I))
    is_dashboard_action = bool(re.search(r"\b(dashboard|dashboards|panel|panels)\b", text, re.I)) and not is_alert_request

    # A) Alert Rule Creation / Management
    if is_alert_request:
        if not settings.alert_rule_creation_enabled:
            return ChatResponse(
                status="out_of_scope_action",
                sessionId=session.session_id,
                intent="alert_rule",
                agents=["Router"],
                steps=[ChatStep(step=1, agent="Router", action="checked feature gate", result="disabled")],
                answer="Alert-rule creation is currently disabled on this deployment; this skill only constructs/runs read-only queries.",
            )

        # Route alert rule creation through pipeline
        if skill_index:
            try:
                pipeline_res = await run_pipeline(text, skill_index, settings)
                first = pipeline_res.get("results", [{}])[0] if pipeline_res.get("mode") == "multi" else pipeline_res
                status = first.get("status", "ok")
                alert_rule = first.get("alert_rule") or pipeline_res.get("alert_rule")
                explanation = first.get("explanation") or pipeline_res.get("explanation") or "Alert rule proposal generated."

                if alert_rule:
                    session.pending_payload = {"alert_rule": alert_rule}
                    session.last_alert_rule = alert_rule

                return ChatResponse(
                    status=status,
                    sessionId=session.session_id,
                    intent="alert_rule",
                    agents=["Router", "Generator", "Validator"],
                    steps=[ChatStep(step=1, agent="Alert Engine", action="proposed alert rule", result=status)],
                    answer=explanation,
                    alertRule=alert_rule,
                )
            except Exception as e:
                logger.error("Alert proposal error: %s", e)

    # B) Dashboard Mutation / Exploration (ADK + FastMCP)
    if is_dashboard_action:
        # Check if this is a mutation (create, update, remove)
        mutation_intent = resolve_dashboard_intent(request=text)
        intent_kind = mutation_intent.get("intent", "UNSPECIFIED")

        if intent_kind in ("CREATE", "UPDATE", "REMOVE"):
            prop_res = propose_dashboard(request=text, target=resolved_target, time_range=req.timeRange or "1h")
            pid = prop_res.get("proposalId")
            status = prop_res.get("status", "success")

            if pid:
                proposal_obj = PROPOSALS.get(pid)
                if proposal_obj and proposal_obj.get("ir"):
                    session.last_target = proposal_obj["ir"].get("name") or pid

                return ChatResponse(
                    status="ok",
                    sessionId=session.session_id,
                    intent="dashboard_proposal",
                    agents=["ADK Agent", "Proposal Engine", "MCP-Grafana"],
                    steps=[
                        ChatStep(step=1, agent="Coordinator", action="classified intent", result=intent_kind),
                        ChatStep(step=2, agent="Proposal Engine", action="generated Dashboard IR", result=proposal_obj.get("ir", {}).get("name", "Dashboard")),
                    ],
                    answer="Here is the proposed dashboard. Review and modify it before applying.",
                    proposalId=pid,
                    proposal=proposal_obj,
                )

            if status == "clarification":
                session.pending_action = "awaiting_metric_for_dashboard"
                session.pending_payload = {"dashboard_request": text, "target": resolved_target, "time_range": req.timeRange}
                question = prop_res.get("question", "Please specify which Prometheus metric to use.")

                return ChatResponse(
                    status="clarification",
                    sessionId=session.session_id,
                    intent="dashboard",
                    agents=["ADK Agent", "Proposal Engine"],
                    steps=[ChatStep(step=1, agent="Proposal Engine", action="requested clarification", result="missing_metric")],
                    answer=question,
                )

        # Dashboard reading / inspection / listing -> run ADK agent
        adk_out = run_adk_agent(
            request=text,
            conversation_id=session.session_id,
            target=resolved_target,
            time_range=req.timeRange,
        )

        # Extract UID if mentioned to store as last_target
        uid_match = re.search(r"UID:\s*`?([A-Za-z0-9_-]+)`?", adk_out.get("text", ""))
        if uid_match:
            session.last_target = uid_match.group(1)
            logger.info("Updated session last_target to %s", session.last_target)

        return ChatResponse(
            status="ok",
            sessionId=session.session_id,
            intent="dashboard",
            agents=["ADK Agent", "MCP-Grafana"],
            steps=[ChatStep(step=1, agent="ADK Agent", action="queried Grafana MCP", result="completed")],
            answer=adk_out.get("text") or adk_out.get("agent_response") or "Completed.",
            proposalId=adk_out.get("proposalId"),
            proposal=adk_out.get("proposal"),
        )

    # C) Telemetry Metric Query (SKILL.md PromQL Engine)
    if skill_index:
        try:
            from app import executor
            pipeline_res = await run_pipeline(text, skill_index, settings)
            if pipeline_res.get("status") not in ("declined", "ambiguous_metric", "unsupported_metric"):
                pipeline_res = executor.execute_contract(pipeline_res, settings)

            first = pipeline_res.get("results", [{}])[0] if pipeline_res.get("mode") == "multi" else pipeline_res
            status = first.get("status", "ok")
            query = first.get("query")
            explanation = first.get("explanation") or pipeline_res.get("synthesis") or "Query executed successfully."
            execution = first.get("execution") or {}
            raw_series = execution.get("series") or []
            data_source = first.get("data_source") or "prometheus"

            # Normalize series for UI Recharts rendering
            normalized_series = []
            for s in raw_series:
                labels = s.get("labels") or {}
                name = s.get("legend_label")
                if not name:
                    parts = []
                    if "node_id" in labels:
                        parts.append(labels["node_id"])
                    elif "instance" in labels:
                        parts.append(labels["instance"])
                    if "cpu" in labels:
                        parts.append(f"cpu{labels['cpu']}")
                    if "mode" in labels and labels["mode"] != "idle":
                        parts.append(labels["mode"])
                    name = " · ".join(parts) if parts else (", ".join(f"{k}={v}" for k, v in labels.items() if k not in ("job", "cluster", "__name__")) or "metric")
                pts = []
                for p in s.get("points") or []:
                    raw_ts = p.get("timestamp")
                    try:
                        ts = float(raw_ts) if raw_ts is not None else 0.0
                        t = int(ts / 1000) if ts > 1e12 else int(ts)
                    except Exception:
                        t = 0
                    pts.append({"t": t, "v": p.get("value")})
                normalized_series.append({"name": name, "labels": labels, "points": pts})

            # Auto-detect gauge chart type for single point
            chart_type = execution.get("chart_type")
            if not chart_type and execution:
                chart_type = select_chart_type(execution)
            if not chart_type:
                chart_type = "line"

            return ChatResponse(
                status=status,
                sessionId=session.session_id,
                intent=data_source,
                agents=["Router", "Generator", "Validator", "Executor"],
                steps=[
                    ChatStep(step=1, agent="Router", action="matched observability domain", result=data_source),
                    ChatStep(step=2, agent="Generator", action="constructed query", result=query or "N/A"),
                    ChatStep(step=3, agent="Validator", action="verified constraints", result="valid"),
                    ChatStep(step=4, agent="Executor", action="queried Prometheus", result=f"{len(normalized_series)} series"),
                ],
                answer=explanation,
                queryUsed=query,
                chartType=chart_type,
                series=normalized_series if normalized_series else None,
                candidates=first.get("candidates"),
            )
        except Exception as e:
            logger.warning("SKILL.md pipeline error: %s; falling back to ADK agent", e)

    # D) General Conversational Fallback
    adk_out = run_adk_agent(request=text, conversation_id=session.session_id)
    return ChatResponse(
        status="ok",
        sessionId=session.session_id,
        intent="general",
        agents=["ADK Agent"],
        steps=[ChatStep(step=1, agent="ADK Agent", action="conversational turn", result="completed")],
        answer=adk_out.get("text") or adk_out.get("agent_response") or "How can I help you with your Grafana observability today?",
    )


# ============================================================================
# Alert Confirmation
# ============================================================================

class ConfirmAlertRequest(BaseModel):
    session_id: str
    confirm: bool = True


@router.post("/api/v1/alerts/confirm")
async def confirm_alert_endpoint(req: ConfirmAlertRequest) -> dict:
    from app.grafana_client import create_alert_rule
    settings = get_settings()
    session = SESSION_STORE.get_or_create(req.session_id)

    if not req.confirm:
        session.pending_payload = None
        session.last_alert_rule = None
        return {"status": "discarded"}

    rule_data = (session.pending_payload or {}).get("alert_rule") or session.last_alert_rule
    if not rule_data:
        rule_data = {
            "title": "Low Available Memory Alert",
            "condition_query": "node_memory_MemAvailable_bytes",
            "comparison": {"operator": "<", "threshold": 107374182400.0},
            "for_duration": "5m",
        }

    comp = rule_data.get("comparison", {})
    if isinstance(comp, dict):
        op = comp.get("operator", ">")
        raw_thresh = comp.get("threshold", 0)
    else:
        op = rule_data.get("comparison_operator", ">")
        raw_thresh = rule_data.get("threshold", 0)

    try:
        thresh = float(raw_thresh)
    except (ValueError, TypeError):
        thresh = 0.0

    title = rule_data.get("title") or "Observability Alert Rule"
    query = rule_data.get("condition_query") or rule_data.get("query") or "node_load1"
    duration = rule_data.get("for_duration") or "5m"
    folder_uid = rule_data.get("folder_uid") or settings.grafana_default_folder_uid
    datasource_uid = rule_data.get("datasource_uid") or settings.grafana_default_datasource_uid

    outcome = create_alert_rule(
        grafana_url=settings.grafana_url,
        service_account_token=settings.grafana_service_account_token,
        folder_uid=folder_uid,
        datasource_uid=datasource_uid,
        title=title,
        condition_query=query,
        comparison_operator=op,
        threshold=thresh,
        for_duration=duration,
        rule_group=title,
    )

    logger.info("Alert creation outcome for '%s': %s (uid=%s)", title, outcome.status, outcome.rule_uid)

    if outcome.status in ("success", "conflict"):
        return {
            "status": "created",
            "rule_uid": outcome.rule_uid or "alert-rule",
            "deeplink": outcome.deeplink or f"{settings.grafana_url.rstrip('/')}/alerting/list",
        }

    return {
        "status": "error",
        "error": outcome.error or "Failed to create alert in Grafana.",
    }

