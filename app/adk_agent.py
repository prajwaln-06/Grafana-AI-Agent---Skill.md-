import logging
import os
import time
from typing import Any, Optional

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types

from app.config import get_settings
from app.grafana_tools.wrapper import (
    clear_raw_json,
    get_dashboard_panels,
    get_raw_json,
    list_dashboards,
    query_opensearch_logs,
    query_prometheus_metric,
    search_dashboards,
)
from app.proposals.store import PROPOSALS
from app.proposals.tools import (
    execute_approved_mutation,
    propose_dashboard,
    resolve_dashboard_intent,
)

logger = logging.getLogger("grafana_ai.adk_agent")

AGENT_INSTRUCTION = """\
You are a Grafana Observability Assistant.
Retrieve infrastructure data using your tools. Summarize results in natural language.

LIVE METRICS
For any request about live metric values, current usage, or monitoring data:
-> Call query_prometheus_metric(metric="<describe what the user wants>").
-> The wrapper automatically discovers the correct query from Grafana dashboards and Prometheus.
-> Never generate PromQL yourself. Never guess metric names.

EXPLICIT PROMQL
If the user explicitly provides a PromQL expression:
-> Call query_prometheus_metric(expr="<the exact expression>").
-> Never rewrite, optimize, or substitute it.

OPENSEARCH LOGS
For log and indexed-document retrieval, call query_opensearch_logs. The wrapper resolves
the Grafana OpenSearch datasource and returns normalized documents. Use Lucene syntax for
OpenSearch. Never call a raw Grafana MCP Elasticsearch/OpenSearch tool directly.
OpenSearch indices and fields are discovered data, not Prometheus metrics. Preserve actual
index, query, time field, Body, Resource, Attributes, Severity, and SeverityText fields.

DASHBOARDS
To enumerate all available dashboards ("list dashboards", "show dashboards",
"what dashboards exist", "available dashboards", "show all dashboards"):
-> Call list_dashboards().
-> No arguments. Returns every dashboard with UID and tags.

To search for dashboards matching a specific keyword ("cpu", "node",
"exporter", "kubernetes", "linux", etc.):
-> Call search_dashboards(query="<keyword>").
-> Only use this when the user provides a specific search term.
-> Do NOT call search_dashboards with an empty query.

To inspect what panels a specific dashboard contains:
-> First call list_dashboards() or search_dashboards(query="<name>") to get the UID.
-> Then call get_dashboard_panels(uid="<uid>").

DASHBOARD WRITES
Before selecting a tool for mutations, call resolve_dashboard_intent(request="<the user's exact request>").
READ calls use query_prometheus_metric and never create a proposal.
CREATE, UPDATE, and REMOVE call propose_dashboard and present the proposal; this agent never executes mutation.
UNSPECIFIED requests receive clarification and no mutation tool call.

For a request to create, update, or remove a dashboard:
-> Call propose_dashboard(request="<the user's exact request>").
-> Present its structured proposal for review.
-> Explain that modifications and explicit application approval happen in the proposal UI.
-> This agent has no direct mutation tool; never claim the dashboard was created from proposal output.

RULES
- For greetings or casual conversation: respond naturally without using any tools.
- Never fabricate dashboard names, UIDs, or metric values.
- Never expose raw JSON. Summarize naturally.
"""

from google.adk.models.google_llm import Gemini

_agent_instance: Optional[LlmAgent] = None
_session_service = InMemorySessionService()
_runner_instance: Optional[Runner] = None


def get_adk_agent() -> LlmAgent:
    global _agent_instance
    if _agent_instance is None:
        settings = get_settings()
        model_name = settings.gemini_model or "gemini-3.7-flash"
        api_key = settings.gemini_api_key
        location = getattr(settings, "vertex_location", "global")
        project = getattr(settings, "vertex_project", "project-8d47da29-7cf0-45f0-b55")

        if api_key.startswith("AQ.") or os.environ.get("VERTEXAI", "").lower() in ("true", "1"):
            client_kwargs = {
                "vertexai": True,
                "api_key": api_key,
                "location": location,
                "project": project,
            }
        else:
            client_kwargs = {"api_key": api_key}

        gemini_model = Gemini(
            model=model_name,
            client_kwargs=client_kwargs,
        )

        _agent_instance = LlmAgent(
            model=gemini_model,
            name="grafana_observability_agent",
            description="AI-powered Grafana observability assistant with MCP tools and proposal review.",
            instruction=AGENT_INSTRUCTION,
            tools=[
                list_dashboards,
                search_dashboards,
                get_dashboard_panels,
                query_prometheus_metric,
                query_opensearch_logs,
                propose_dashboard,
                resolve_dashboard_intent,
            ],
        )
        logger.info("Initialized ADK LlmAgent with Vertex AI model=%s", model_name)
    return _agent_instance


def get_adk_runner() -> Runner:
    global _runner_instance
    if _runner_instance is None:
        agent = get_adk_agent()
        _runner_instance = Runner(
            app_name="grafana_observability_app",
            agent=agent,
            session_service=_session_service,
            auto_create_session=True,
        )
        logger.info("Initialized ADK Runner with InMemorySessionService")
    return _runner_instance


def run_adk_agent(
    request: str,
    conversation_id: str,
    target: Optional[str] = None,
    time_range: Optional[str] = None,
) -> dict:
    """Runs a single conversation turn using ADK LlmAgent and Runner."""
    clear_raw_json()

    user_text = request
    if target:
        user_text += f"\nTarget node/host: {target}"
    if time_range and time_range != "1h":
        user_text += f"\nTime range: {time_range}"

    runner = get_adk_runner()
    user_id = f"user-{conversation_id}"
    session_id = conversation_id

    message = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text=user_text)],
    )

    logger.info("ADK Runner start | session=%s | request=%r", session_id, user_text[:120])

    proposal_id: Optional[str] = None
    final_text = ""
    tool_outcome: Optional[dict] = None
    turn_started = time.perf_counter()
    first_tool_call_at: Optional[float] = None
    tool_response_at: Optional[float] = None
    final_response_at: Optional[float] = None

    try:
        for event in runner.run(
            user_id=user_id,
            session_id=session_id,
            new_message=message,
        ):
            author = getattr(event, "author", "?")
            for fc in event.get_function_calls():
                if first_tool_call_at is None:
                    first_tool_call_at = time.perf_counter()
                logger.info("LLM tool call -> %s(%s)", fc.name, list((fc.args or {}).keys()))

            for fr in event.get_function_responses():
                logger.info("Tool response <- %s", fr.name)
                if fr.name == "propose_dashboard" and isinstance(fr.response, dict):
                    tool_response_at = time.perf_counter()
                    tool_outcome = fr.response
                    pid = fr.response.get("proposalId")
                    if pid:
                        logger.info("propose_dashboard returned proposalId=%s", pid)
                        proposal_id = pid

            if event.is_final_response() and event.content and event.content.parts:
                for part in event.content.parts:
                    if part.text:
                        final_response_at = time.perf_counter()
                        final_text = part.text
                        logger.info("Agent final response (author=%s): %s", author, part.text[:200])

            if tool_outcome is not None:
                break
    except Exception as e:
        logger.error("Error during ADK agent execution: %s", e, exc_info=True)
        final_text = f"Encountered error while running agent: {str(e)}"

    turn_finished = time.perf_counter()
    timings = {
        "toolSelectionMs": round(((first_tool_call_at or turn_finished) - turn_started) * 1000, 1),
        "toolExecutionMs": round(((tool_response_at or turn_finished) - (first_tool_call_at or turn_started)) * 1000, 1),
        "finalContinuationMs": round(((final_response_at or tool_response_at or turn_finished) - (tool_response_at or turn_finished)) * 1000, 1),
        "totalMs": round((turn_finished - turn_started) * 1000, 1),
    }

    raw_jsons = get_raw_json()

    if proposal_id:
        return {
            "kind": "proposal",
            "status": (tool_outcome or {}).get("status", "success"),
            "text": "Here is the proposed dashboard. Review and modify it before applying.",
            "agent_response": "Here is the proposed dashboard. Review and modify it before applying.",
            "raw_json": raw_jsons,
            "proposalId": proposal_id,
            "proposal": PROPOSALS.get(proposal_id),
            "errors": (tool_outcome or {}).get("errors", []),
            "timings": timings,
        }

    if final_text:
        return {
            "kind": "text",
            "text": final_text,
            "agent_response": final_text,
            "raw_json": raw_jsons,
            "timings": timings,
        }

    if tool_outcome:
        status = tool_outcome.get("status", "error")
        if status == "clarification":
            text = tool_outcome.get("question", "Please clarify the dashboard operation.")
        elif status == "unsupported":
            text = tool_outcome.get("reason", "Requested dashboard operation is unsupported.")
        else:
            errors = tool_outcome.get("errors", [])
            text = "; ".join(item.get("message", str(item)) if isinstance(item, dict) else str(item) for item in errors) or "Dashboard operation failed."
        return {
            "kind": "text",
            "status": status,
            "text": text,
            "agent_response": text,
            "raw_json": raw_jsons,
            "errors": tool_outcome.get("errors", []),
            "timings": timings,
        }

    return {
        "kind": "text",
        "text": "Agent completed the turn with no output.",
        "agent_response": "Agent completed the turn with no output.",
        "raw_json": raw_jsons,
        "timings": timings,
    }
