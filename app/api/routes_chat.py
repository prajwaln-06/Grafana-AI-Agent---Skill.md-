"""app/api/routes_chat.py

Unified Conversational Gateway for Grafana AI Agent.
Replaces brittle client-side regex routing with a single, professional
server-side coordinator that maintains persistent multi-turn context.
"""
from __future__ import annotations

from datetime import datetime
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
    if not skill_index:
        from app.skill_index import SkillIndex
        try:
            skill_index = SkillIndex.load(settings.skills_root)
            request.app.state.skill_index = skill_index
        except Exception as err:
            logger.error("Could not load skill_index: %s", err)

    logger.info("Unified Chat | session=%s | message=%r", session.session_id, text[:100])

    def respond(res: ChatResponse) -> ChatResponse:
        session.history.append({"role": "assistant", "text": res.answer})
        return res

    # Record user turn
    session.history.append({"role": "user", "text": text})

    # ------------------------------------------------------------------------
    # 0. Friendly Greeting & Help
    # ------------------------------------------------------------------------
    if re.match(r"^(hi|hello|hey|greetings|help|what\s+can\s+you\s+do)\b", text, re.I):
        return respond(ChatResponse(
            status="ok",
            sessionId=session.session_id,
            intent="general",
            agents=["Assistant"],
            steps=[ChatStep(step=1, agent="Assistant", action="greeting", result="ready")],
            answer="Hello! I am your Grafana Observability Assistant. I can help you query live metrics (CPU, Memory, GPU), author and update dashboards, create alert rules, and inspect logs. How can I help you today?",
        ))

    # ------------------------------------------------------------------------
    # 1. Check for Pending Clarifications (Context Continuity)
    # ------------------------------------------------------------------------
    is_numeric_choice = bool(re.match(r"^#?(\d+)[\.\)]?$", text.strip()))

    if session.pending_action == "awaiting_dashboard_uid" and session.pending_payload:
        dash_req = session.pending_payload.get("dashboard_request", "")
        candidates = list(session.pending_payload.get("candidates") or [])
        chosen_uid = None
        cleaned_text = text.strip().rstrip(" \t\r\n.?!,;:'\"")
        cleaned_text = re.sub(r"^uid\s+", "", cleaned_text, flags=re.I).strip()
        if is_numeric_choice and candidates:
            idx = int(re.match(r"^#?(\d+)", text.strip()).group(1)) - 1
            if 0 <= idx < len(candidates):
                chosen_uid = candidates[idx]
        elif candidates:
            matched = next((c for c in candidates if cleaned_text.lower() == c.lower()), None)
            if matched:
                chosen_uid = matched
        elif re.match(r"^[A-Za-z0-9_-]+$", cleaned_text) and not re.search(r"\b(list|show|get|delete|add|create|search|what|how)\b", cleaned_text, re.I):
            chosen_uid = cleaned_text

        session.pending_action = None
        session.pending_payload = None
        is_numeric_choice = False

        if chosen_uid:
            text = f"{dash_req} UID {chosen_uid}"
            if session.history and session.history[-1].get("role") == "user":
                session.history[-1]["text"] = text

    if (session.pending_action == "awaiting_metric_for_dashboard" and session.pending_payload) or (
        is_numeric_choice and session.history
    ):
        dash_req = (session.pending_payload or {}).get("dashboard_request", "")
        target = (session.pending_payload or {}).get("target") or session.last_target or ""
        time_range = (session.pending_payload or {}).get("time_range", "1h")
        candidates = list((session.pending_payload or {}).get("candidates") or [])

        # If candidates not in pending_payload, extract from recent assistant history
        if not candidates and session.history:
            last_assistant_msg = next((h["text"] for h in reversed(session.history) if h.get("role") == "assistant"), "")
            candidates = re.findall(r"\(`([A-Za-z0-9_:]+)`\)", last_assistant_msg)
            if not dash_req:
                dash_req = next((h["text"] for h in reversed(session.history[:-1]) if h.get("role") == "user" and h.get("text") != text), "add panel to dashboard")

        chosen_metric = None
        if is_numeric_choice and candidates:
            num_val = int(re.match(r"^#?(\d+)", text.strip()).group(1))
            idx = num_val - 1
            if 0 <= idx < len(candidates):
                chosen_metric = candidates[idx]
                logger.info("Resolved numeric choice %s to metric: %s", num_val, chosen_metric)
        elif candidates:
            cleaned_text = text.strip().rstrip(" \t\r\n.?!,;:'\"")
            matched = next((c for c in candidates if cleaned_text.lower() == c.lower() or cleaned_text.lower() in c.lower() or c.lower() in cleaned_text.lower()), None)
            if matched:
                chosen_metric = matched
                logger.info("Resolved textual choice %r to metric: %s", text, chosen_metric)
        elif re.match(r"^[A-Za-z0-9_:]+$", text.strip()) and not re.search(r"\b(list|show|get|delete|add|create|search|what|how)\b", text, re.I):
            chosen_metric = text.strip()

        session.pending_action = None
        session.pending_payload = None

        if chosen_metric:
            composite_req = f"{dash_req} using metric {chosen_metric}"
            logger.info("Resuming pending dashboard proposal: %r", composite_req)

            prop_res = propose_dashboard(request=composite_req, target=target or "", time_range=time_range)
            pid = prop_res.get("proposalId")
            if pid:
                proposal_obj = PROPOSALS.get(pid)
                if proposal_obj and proposal_obj.get("ir"):
                    session.last_target = proposal_obj["ir"].get("name") or pid

                return respond(ChatResponse(
                    status="ok",
                    sessionId=session.session_id,
                    intent="dashboard_proposal",
                    agents=["ADK Agent", "Proposal Engine", "MCP-Grafana"],
                    steps=[
                        ChatStep(step=1, agent="Coordinator", action="resolved clarification", result=chosen_metric),
                        ChatStep(step=2, agent="Proposal Engine", action="generated Dashboard IR", result=proposal_obj.get("ir", {}).get("name", "Dashboard")),
                    ],
                    answer=f"Added panel with `{chosen_metric}` to the dashboard. Review and modify it before applying.",
                    proposalId=pid,
                    proposal=proposal_obj,
                ))

            if prop_res.get("status") == "clarification":
                new_candidates = prop_res.get("candidates") or re.findall(r"\(`([A-Za-z0-9_:]+)`\)", prop_res.get("question", ""))
                session.pending_action = "awaiting_metric_for_dashboard"
                session.pending_payload = {
                    "dashboard_request": dash_req,
                    "target": target,
                    "time_range": time_range,
                    "candidates": new_candidates,
                }
                return respond(ChatResponse(
                    status="clarification",
                    sessionId=session.session_id,
                    intent="dashboard",
                    agents=["ADK Agent", "Proposal Engine"],
                    steps=[ChatStep(step=1, agent="Proposal Engine", action="requested clarification", result="missing_metric")],
                    answer=prop_res.get("question", "Please clarify which metric to use."),
                    candidates=[{"name": m, "purpose": m} for m in new_candidates] if new_candidates else None,
                ))

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
    _resolved_mutation = resolve_dashboard_intent(request=text).get("intent")
    is_dashboard_action = (
        bool(re.search(r"\b(dashboard|dashboards|panel|panels)\b", text, re.I))
        or _resolved_mutation in ("CREATE", "UPDATE", "REMOVE")
    ) and not is_alert_request

    # A) Alert Rule Creation / Management
    if is_alert_request:
        if not settings.alert_rule_creation_enabled:
            return respond(ChatResponse(
                status="out_of_scope_action",
                sessionId=session.session_id,
                intent="alert_rule",
                agents=["Router"],
                steps=[ChatStep(step=1, agent="Router", action="checked feature gate", result="disabled")],
                answer="Alert-rule creation is currently disabled on this deployment; this skill only constructs/runs read-only queries.",
            ))

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

                return respond(ChatResponse(
                    status=status,
                    sessionId=session.session_id,
                    intent="alert_rule",
                    agents=["Router", "Generator", "Validator"],
                    steps=[ChatStep(step=1, agent="Alert Engine", action="proposed alert rule", result=status)],
                    answer=explanation,
                    alertRule=alert_rule,
                ))
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

                ir_data = (proposal_obj or {}).get("ir", {})
                is_delete = bool(ir_data.get("removeDashboard") or (ir_data.get("operation") == "remove" and ir_data.get("removeDashboard")))
                dash_name = ir_data.get("name", "Dashboard")
                dash_uid = ir_data.get("dashboardUid", "")

                if is_delete:
                    answer_text = f"I have prepared a proposal to delete the dashboard **{dash_name}**" + (f" (UID: `{dash_uid}`)" if dash_uid else "") + ". Please review and confirm the deletion below."
                    action_result = f"deletion proposal for {dash_name}"
                elif ir_data.get("operation") == "remove":
                    answer_text = f"I have prepared a proposal to remove panel(s) from **{dash_name}**. Review and modify it before applying."
                    action_result = f"panel removal for {dash_name}"
                elif ir_data.get("operation") == "update":
                    answer_text = f"I have prepared an update for the dashboard **{dash_name}**. Review and modify it before applying."
                    action_result = f"update proposal for {dash_name}"
                else:
                    answer_text = f"Here is the proposed dashboard **{dash_name}**. Review and modify it before applying."
                    action_result = f"created proposal for {dash_name}"

                return respond(ChatResponse(
                    status="ok",
                    sessionId=session.session_id,
                    intent="dashboard_proposal",
                    agents=["ADK Agent", "Proposal Engine", "MCP-Grafana"],
                    steps=[
                        ChatStep(step=1, agent="Coordinator", action="classified intent", result="delete_dashboard" if is_delete else intent_kind),
                        ChatStep(step=2, agent="Proposal Engine", action="generated Dashboard IR", result=action_result),
                    ],
                    answer=answer_text,
                    proposalId=pid,
                    proposal=proposal_obj,
                ))

            if status == "clarification":
                question = prop_res.get("question", "Please specify which Prometheus metric to use.")
                candidates = prop_res.get("candidates") or re.findall(r"\(`([A-Za-z0-9_:]+)`\)", question)
                if "multiple dashboards match" in question.lower():
                    session.pending_action = "awaiting_dashboard_uid"
                    session.pending_payload = {
                        "dashboard_request": text,
                        "candidates": re.findall(r"`([A-Za-z0-9_.:-]+)`", question),
                        "time_range": req.timeRange,
                    }
                else:
                    session.pending_action = "awaiting_metric_for_dashboard"
                    session.pending_payload = {
                        "dashboard_request": text,
                        "target": resolved_target,
                        "time_range": req.timeRange,
                        "candidates": candidates,
                    }

                return respond(ChatResponse(
                    status="clarification",
                    sessionId=session.session_id,
                    intent="dashboard",
                    agents=["ADK Agent", "Proposal Engine"],
                    steps=[ChatStep(step=1, agent="Proposal Engine", action="requested clarification", result="missing_metric")],
                    answer=question,
                    candidates=[{"name": m, "purpose": m} for m in candidates] if candidates else None,
                ))

            if status in ("error", "unsupported"):
                err_msg = ""
                if prop_res.get("errors"):
                    err_msg = prop_res["errors"][0].get("message", "")
                if not err_msg:
                    err_msg = prop_res.get("reason") or "Failed to process dashboard request."
                if "multiple dashboards match" in err_msg.lower():
                    session.pending_action = "awaiting_dashboard_uid"
                    session.pending_payload = {
                        "dashboard_request": text,
                        "candidates": re.findall(r"`([A-Za-z0-9_.:-]+)`", err_msg),
                        "time_range": req.timeRange,
                    }
                return respond(ChatResponse(
                    status=status,
                    sessionId=session.session_id,
                    intent="dashboard",
                    agents=["Proposal Engine"],
                    steps=[ChatStep(step=1, agent="Proposal Engine", action="validated dashboard request", result=status)],
                    answer=err_msg,
                ))

        # Check if user is asking to inspect panels inside a dashboard
        is_panel_inspection = (
            bool(re.search(r"\b(panel|panels)\b", text, re.I))
            and not bool(re.search(r"\b(add|create|insert|append|remove|delete|drop)\b", text, re.I))
        )
        if is_panel_inspection:
            from app.grafana_tools.dashboard_writing import _dashboard_identity, _find_dashboards
            from app.grafana_tools.wrapper import get_dashboard_panels, list_dashboards
            from app.mcp.session import run_sync
            from app.dashboard.intent import Intent

            target_uid = None
            target_name = None

            # 1. Referential or explicit target
            if has_referential_noun and session.last_target:
                target_uid = session.last_target
            elif resolved_target:
                target_uid = resolved_target

            # 2. Extract from request text
            if not target_uid:
                try:
                    target_name = _dashboard_identity(text, Intent.READ)
                except Exception:
                    m = re.search(r"\b(?:in|on|for)\s+(?:the\s+)?([A-Za-z0-9 _.-]+?)(?:\s+dashboard|\s+board|[.!?]|$)", text, re.I)
                    if m:
                        cand = m.group(1).strip()
                        if cand.lower() not in ("this", "that", "the", "a", "an", "all", "my"):
                            target_name = cand

            if target_name and not target_uid:
                dashes = run_sync(_find_dashboards(target_name))
                if dashes:
                    target_uid = dashes[0].get("uid")
                    target_name = dashes[0].get("title", target_name)
                else:
                    target_uid = target_name

            if target_uid:
                panels_text = get_dashboard_panels(target_uid)
                session.last_target = target_uid
                return respond(ChatResponse(
                    status="ok",
                    sessionId=session.session_id,
                    intent="dashboard",
                    agents=["ADK Agent", "MCP-Grafana"],
                    steps=[ChatStep(step=1, agent="MCP-Grafana", action=f"inspected panels for {target_name or target_uid}", result="success")],
                    answer=panels_text,
                ))
            else:
                dash_list_text = list_dashboards()
                return respond(ChatResponse(
                    status="ok",
                    sessionId=session.session_id,
                    intent="dashboard",
                    agents=["ADK Agent", "MCP-Grafana"],
                    steps=[ChatStep(step=1, agent="MCP-Grafana", action="listed dashboards for panel inspection", result="success")],
                    answer=f"Please specify which dashboard you would like to inspect panels for:\n\n{dash_list_text}",
                ))

        # Check if user is asking to list, search, or find dashboards
        is_list_search = (
            bool(re.search(r"\b(list|show|find|search|get|display|pick|view|see|available)\b", text, re.I))
            or intent_kind == "READ"
        ) and not is_panel_inspection
        if is_list_search or intent_kind == "READ":
            from app.grafana_tools.wrapper import list_dashboards, search_dashboards
            search_kw = None
            kw_match = re.search(r"\b(?:search|find)\s+(?:dashboards?\s+)?(?:for|matching|about|with)?\s*['\"]?([A-Za-z0-9_-]+)['\"]?", text, re.I)
            if kw_match:
                cand = kw_match.group(1)
                if cand.lower() not in ("dashboard", "dashboards", "all", "the", "for", "me"):
                    search_kw = cand
            if not search_kw:
                named_kw = re.search(r"\b(fleet|overview|logs|node|gpu|cpu|observability|demo|kubernetes|linux)\b", text, re.I)
                if named_kw and named_kw.group(1).lower() not in ("dashboard", "dashboards") and bool(re.search(r"\b(search|find)\b", text, re.I)):
                    search_kw = named_kw.group(1)

            answer_text = ""
            if search_kw:
                answer_text = search_dashboards(search_kw)
            if not answer_text or answer_text.startswith("Error"):
                answer_text = list_dashboards()

            # Store the first dashboard's UID as session.last_target if found
            first_uid_match = re.search(r"UID:\s*`?([A-Za-z0-9_-]+)`?", answer_text) or re.search(r"\(`([A-Za-z0-9_-]+)`\)", answer_text)
            if first_uid_match:
                session.last_target = first_uid_match.group(1)

            return respond(ChatResponse(
                status="ok",
                sessionId=session.session_id,
                intent="dashboard",
                agents=["ADK Agent", "MCP-Grafana"],
                steps=[ChatStep(step=1, agent="MCP-Grafana", action="searched Grafana dashboards" if search_kw else "listed Grafana dashboards", result="success")],
                answer=answer_text,
            ))

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

        adk_answer = adk_out.get("text") or adk_out.get("agent_response") or "Completed."
        candidates = re.findall(r"\(`([A-Za-z0-9_:]+)`\)", adk_answer)
        if candidates and ("which one would you like" in adk_answer.lower() or "reply with the number" in adk_answer.lower() or "clarification" in adk_answer.lower()):
            session.pending_action = "awaiting_metric_for_dashboard"
            session.pending_payload = {
                "dashboard_request": text,
                "target": resolved_target,
                "time_range": req.timeRange,
                "candidates": candidates,
            }

        return respond(ChatResponse(
            status="ok",
            sessionId=session.session_id,
            intent="dashboard",
            agents=["ADK Agent", "MCP-Grafana"],
            steps=[ChatStep(step=1, agent="ADK Agent", action="queried Grafana MCP", result="completed")],
            answer=adk_answer,
            proposalId=adk_out.get("proposalId"),
            proposal=adk_out.get("proposal"),
            candidates=[{"name": m, "purpose": m} for m in candidates] if candidates else None,
        ))

    # C) Telemetry Metric Query (SKILL.md PromQL Engine)
    if skill_index:
        try:
            from app import executor
            from app.pipeline import run_dependency_aware_pipeline, synthesize_executed_contract

            if settings.dependent_query_resolution_enabled:
                staged_res = await run_dependency_aware_pipeline(text, skill_index, settings)
                pipeline_res = staged_res.contract
                if not staged_res.already_executed and pipeline_res.get("status") not in ("declined", "ambiguous_metric", "unsupported_metric"):
                    pipeline_res = executor.execute_contract(pipeline_res, settings)
                pipeline_res = synthesize_executed_contract(pipeline_res)
            else:
                pipeline_res = await run_pipeline(text, skill_index, settings)
                if pipeline_res.get("status") not in ("declined", "ambiguous_metric", "unsupported_metric"):
                    pipeline_res = executor.execute_contract(pipeline_res, settings)

            results_list = pipeline_res.get("results") if pipeline_res.get("mode") == "multi" else [pipeline_res]
            first = results_list[0] if results_list else {}
            status = first.get("status", "ok")
            query = " ; ".join(r.get("query") for r in results_list if r.get("query")) or first.get("query")
            explanation = pipeline_res.get("synthesis") or first.get("explanation") or "Query executed successfully."
            
            raw_series = []
            for r in results_list:
                exec_obj = r.get("execution") or {}
                raw_series.extend(exec_obj.get("series") or [])

            execution = first.get("execution") or {}
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
                    t = 0
                    if isinstance(raw_ts, str):
                        try:
                            clean_iso = raw_ts.replace("Z", "+00:00")
                            t = int(datetime.fromisoformat(clean_iso).timestamp())
                        except Exception:
                            try:
                                ts = float(raw_ts)
                                t = int(ts / 1000) if ts > 1e12 else int(ts)
                            except Exception:
                                t = 0
                    elif isinstance(raw_ts, (int, float)):
                        t = int(raw_ts / 1000) if raw_ts > 1e12 else int(raw_ts)
                    pts.append({"t": t, "v": p.get("value")})
                normalized_series.append({"name": name, "labels": labels, "points": pts})

            # Auto-detect gauge chart type for single point
            chart_type = execution.get("chart_type")
            if not chart_type and execution:
                chart_type = select_chart_type(execution)
            if not chart_type:
                chart_type = "line"

            return respond(ChatResponse(
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
            ))
        except Exception as e:
            logger.warning("SKILL.md pipeline error: %s; falling back to ADK agent", e)

    # D) General Conversational Fallback
    adk_out = run_adk_agent(request=text, conversation_id=session.session_id)
    return respond(ChatResponse(
        status="ok",
        sessionId=session.session_id,
        intent="general",
        agents=["ADK Agent"],
        steps=[ChatStep(step=1, agent="ADK Agent", action="conversational turn", result="completed")],
        answer=adk_out.get("text") or adk_out.get("agent_response") or "How can I help you with your Grafana observability today?",
    ))


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

