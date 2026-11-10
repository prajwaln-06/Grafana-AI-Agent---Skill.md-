import asyncio
import contextvars
import functools
import logging
import os
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import aclosing
from typing import Any, Optional

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types

from app.auth.audit import AuditEvent, record_denial
from app.auth.principal import Principal, get_current_principal, principal_scope
from app.auth.refresh import authorization_operation
from app.auth.resource_scope import AuthorizationDenied
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
from app.grafana_tools.wrapper import (
    list_alert_rules,
    propose_alert_rule,
    resolve_alert_intent,
)

logger = logging.getLogger("grafana_ai.adk_agent")


def scope_guarded(func):
    """Wrap an agent tool so a scope denial becomes a safe tool result.

    The tool boundary must never leak an unauthorized resource, but the
    authorization decision itself stays on the server: the tool reports the
    denial to the model instead of returning any dashboard content.

    The denial is also written to the audit log, with the *authenticated*
    principal as the actor -- never with anything the model supplied.
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        # Each tool call is one protected operation: the authorization data
        # is established once for it and never outlives it, so a revocation
        # that lands between two tool calls is always seen.
        with authorization_operation():
            try:
                return func(*args, **kwargs)
            except AuthorizationDenied as exc:
                logger.info("Tool %s denied by scope: %s", func.__name__, exc.reason)
                _audit_tool_denial(func.__name__, exc)
                status = "401" if getattr(exc, "status_code", 403) == 401 else "403"
                return (
                    f"Error: RBAC Access Denied ({status}) - {exc.reason}"
                )

    return wrapper


def _audit_tool_denial(tool_name: str, exc: AuthorizationDenied) -> None:
    """Record a denied agent tool call.

    Auditing must never break the refusal, so a failure to persist the
    record is logged and the denial still stands.
    """
    try:
        record_denial(
            AuditEvent.SCOPE_VIOLATION,
            principal=get_current_principal(),
            reason=exc.reason,
            target_type="adk_tool",
            target_id=tool_name,
            details={"statusCode": getattr(exc, "status_code", 403)},
        )
    except Exception as audit_exc:  # pragma: no cover - auditing must not break the refusal
        logger.warning("Could not write the tool-denial audit record: %s", audit_exc)

AGENT_INSTRUCTION = """\
You are a Grafana Observability Assistant.
Retrieve infrastructure data using your tools. Summarize results in natural language.

LIVE METRICS
For any request about live metric values, current usage, or monitoring data:
-> Call query_prometheus_metric(metric="<describe what the user wants>").
-> The wrapper automatically discovers the correct query from Grafana dashboards and Prometheus.
-> Never generate PromQL yourself. Never guess metric names.

ENVIRONMENT
Every metric query, dashboard operation and alert rule belongs to an environment.
-> Pass environment="<environment>" to query_prometheus_metric and propose_alert_rule
   whenever the user names one.
-> The environment is validated on the server against the caller's permitted
   environments. Never invent an environment name and never retry with a
   different one after a denial -- report the denial instead.

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

For a request to DELETE or REMOVE an entire dashboard:
-> Call propose_dashboard(request="<the user's exact request>").
-> If removeDashboard is true or operation is remove, state clearly that a deletion proposal has been created to delete the dashboard (mention its title and UID if available).
-> Direct the user to review and confirm the deletion in the proposal card below.
-> Do NOT say "Here is the proposed dashboard" and do NOT mention adding or modifying panels.

For a request to CREATE or UPDATE a dashboard:
-> Call propose_dashboard(request="<the user's exact request>").
-> Present its structured proposal for review.
-> Explain that modifications and explicit application approval happen in the proposal UI.
-> This agent has no direct mutation tool; never claim the dashboard was created from proposal output.

ALERT RULES
For any request about creating, viewing, listing, or managing Grafana alert rules:
-> Call resolve_alert_intent(request="<the user's exact request>") first to classify the intent.
-> For LIST ("show alerts", "list alert rules", "what alerts exist"):
   Call list_alert_rules() and summarize results.
-> For CREATE ("alert when...", "notify me if...", "create an alert"):
   Call propose_alert_rule(request="<the user's exact request>").
   Present the proposal details clearly: metric, PromQL, threshold, duration, severity, folder.
   Explain that the alert has NOT been created yet — the user must approve it via the
   /api/alert-proposals/ID/approve endpoint, then execute via /api/alert-proposals/ID/execute.
-> For DELETE / UPDATE: call propose_alert_rule with the full request; the backend handles the intent.

Alert key info to extract and relay to the user from the proposal:
- metric being monitored (cpu, memory, disk, etc.)
- the exact PromQL query that will be evaluated
- condition and threshold (e.g. greater than 90%)
- for duration (how long it must persist before firing)
- severity label (critical, warning, info)
- which Grafana folder the rule will live in
- the proposalId they need to approve

RULES
- For greetings or casual conversation: respond naturally without using any tools.
- Never fabricate dashboard names, UIDs, or metric values.
- Never expose raw JSON. Summarize naturally.
- CONTEXT RESOLUTION: When the user refers to "this dashboard", "the dashboard", or "it", inspect the prior conversation turns in this session to identify which dashboard name or UID was previously discussed. Do not ask for the dashboard name or UID again if it was already identified earlier in this chat.
- If the user asks to "update this dashboard" without specifying changes, explicitly confirm the dashboard you will be modifying (by name and UID) and ask what specific changes, metrics, or panels they want to apply.
"""

from google.adk.models.google_llm import Gemini

_agent_instance: Optional[LlmAgent] = None
_session_service = InMemorySessionService()
_runner_instance: Optional[Runner] = None


def _run_with_preserved_context(factory):
    """Run ``factory()`` in an event loop whose context is a copy of the caller's.

    The agent turn crosses two boundaries on its way to the tool wrappers:
    the event loop that drives the agent, and the MCP session bridge's
    background loop. Neither inherits the caller's context variables on its
    own, so the context is copied explicitly and the copy is handed to the
    task that runs the turn.

    Running the turn through ``Runner.run_async()`` (rather than through
    ADK's synchronous ``Runner.run()`` helper) makes that binding the
    application's own rather than a side effect of how ADK happens to
    spawn its worker thread -- a security-critical property should not
    depend on a library internal. The runner, the agent and the events are
    unchanged; ADK's own documentation recommends ``run_async`` for
    production use.
    """
    context = contextvars.copy_context()

    def _call() -> Any:
        return asyncio.run(factory())

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No loop is running in this thread: run the turn here, in the copy.
        return context.run(_call)

    # A loop is already running in this thread (the FastAPI request handler),
    # and a second loop cannot be started here. The turn therefore runs on a
    # worker thread whose context is the explicit copy taken above.
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="adk-turn") as pool:
        return pool.submit(context.run, _call).result()


async def _run_agent_turn(
    runner: Runner,
    *,
    user_id: str,
    session_id: str,
    message: genai_types.Content,
) -> dict[str, Any]:
    """Run one agent turn and collect what the caller needs from it.

    The turn is driven through ``Runner.run_async()`` so that it executes
    inside the invocation's context -- the context the authenticated
    Principal is bound to -- instead of on a thread whose context the
    application does not control.
    """
    outcome: dict[str, Any] = {
        "final_text": "",
        "proposal_id": None,
        "tool_outcome": None,
        "first_tool_call_at": None,
        "tool_response_at": None,
        "final_response_at": None,
    }

    async with aclosing(
        runner.run_async(user_id=user_id, session_id=session_id, new_message=message)
    ) as events:
        async for event in events:
            author = getattr(event, "author", "?")
            for fc in event.get_function_calls():
                if outcome["first_tool_call_at"] is None:
                    outcome["first_tool_call_at"] = time.perf_counter()
                logger.info("LLM tool call -> %s(%s)", fc.name, list((fc.args or {}).keys()))

            for fr in event.get_function_responses():
                logger.info("Tool response <- %s", fr.name)
                if fr.name == "propose_dashboard" and isinstance(fr.response, dict):
                    outcome["tool_response_at"] = time.perf_counter()
                    outcome["tool_outcome"] = fr.response
                    pid = fr.response.get("proposalId")
                    if pid:
                        logger.info("propose_dashboard returned proposalId=%s", pid)
                        outcome["proposal_id"] = pid

            if event.is_final_response() and event.content and event.content.parts:
                for part in event.content.parts:
                    if part.text:
                        outcome["final_response_at"] = time.perf_counter()
                        outcome["final_text"] = part.text
                        logger.info("Agent final response (author=%s): %s", author, part.text[:200])

            if outcome["proposal_id"] is not None:
                break

    return outcome


def get_adk_agent() -> LlmAgent:
    global _agent_instance
    if _agent_instance is None:
        settings = get_settings()
        selected_model = None

        # Check for local Ollama / LiteLLM preference
        use_litellm = os.environ.get("ADK_USE_LITELLM", "").lower() in ("true", "1") or os.environ.get("OLLAMA_MODEL")
        if use_litellm:
            try:
                from google.adk.models.lite_llm import LiteLlm
                ollama_model = os.environ.get("OLLAMA_MODEL", "qwen2.5:14b")
                model_name = f"ollama_chat/{ollama_model}" if not ollama_model.startswith("ollama_chat/") else ollama_model
                selected_model = LiteLlm(model=model_name)
                logger.info("Initialized ADK LlmAgent with LiteLLM model=%s", model_name)
            except Exception as e:
                logger.warning("Failed to initialize LiteLLM (%s); falling back to Gemini", e)

        if selected_model is None:
            model_name = settings.gemini_model if settings.gemini_model != "gemini-3.7-flash" else "gemini-2.5-flash"
            from app import llm_provider
            client_kwargs = llm_provider.build_client_kwargs(settings)
            selected_model = Gemini(
                model=model_name,
                client_kwargs=client_kwargs,
            )
            logger.info("Initialized ADK LlmAgent with %s model=%s",
                        llm_provider.describe_provider(settings), model_name)

        _agent_instance = LlmAgent(
            model=selected_model,
            name="grafana_observability_agent",
            description="AI-powered Grafana observability assistant with MCP tools and proposal review.",
            instruction=AGENT_INSTRUCTION,
            tools=[
                # Every tool the agent may call is an application-owned
                # wrapper. The upstream Grafana MCP tools are never exposed
                # to the agent directly: the wrapper is the security
                # boundary that authorizes the caller before it forwards
                # anything to Grafana.
                scope_guarded(list_dashboards),
                scope_guarded(search_dashboards),
                scope_guarded(get_dashboard_panels),
                scope_guarded(query_prometheus_metric),
                scope_guarded(query_opensearch_logs),
                scope_guarded(propose_dashboard),
                scope_guarded(resolve_dashboard_intent),
                scope_guarded(list_alert_rules),
                scope_guarded(propose_alert_rule),
                scope_guarded(resolve_alert_intent),
            ],
        )
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
    *,
    principal: Optional[Principal] = None,
) -> dict:
    """Runs a single conversation turn using ADK LlmAgent and Runner.

    ``principal`` is the authenticated caller resolved by the request
    dependency. It is bound to the turn's execution context for the duration
    of the invocation and removed again afterwards, so every wrapper the
    agent calls authorizes against the real caller and no identity survives
    the turn.
    """
    clear_raw_json()

    user_text = request
    if target:
        user_text += f"\nTarget node/host: {target}"
    if time_range and time_range != "1h":
        user_text += f"\nTime range: {time_range}"

    runner = get_adk_runner()
    user_id = f"user-{conversation_id or 'default'}"
    session_id = conversation_id or f"sess_{secrets.token_hex(8)}"

    message = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text=user_text)],
    )

    # The caller's identity is taken from the explicit argument when the
    # caller resolved one, and from the request context otherwise. It is
    # never taken from the request text, the model output or a tool argument.
    caller = principal if principal is not None else get_current_principal()
    if caller is None:
        logger.warning("Refusing an agent turn without an authenticated identity.")

    logger.info("ADK Runner start | session=%s | request=%r", session_id, user_text[:120])

    proposal_id: Optional[str] = None
    final_text = ""
    tool_outcome: Optional[dict] = None
    turn_started = time.perf_counter()
    first_tool_call_at: Optional[float] = None
    tool_response_at: Optional[float] = None
    final_response_at: Optional[float] = None

    # The Principal is bound to the invocation's context for exactly as long
    # as the turn runs, and unbound again when it ends.
    with principal_scope(caller):
        try:
            outcome = _run_with_preserved_context(
                lambda: _run_agent_turn(
                    runner,
                    user_id=user_id,
                    session_id=session_id,
                    message=message,
                )
            )
            final_text = outcome["final_text"]
            proposal_id = outcome["proposal_id"]
            tool_outcome = outcome["tool_outcome"]
            first_tool_call_at = outcome["first_tool_call_at"]
            tool_response_at = outcome["tool_response_at"]
            final_response_at = outcome["final_response_at"]
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

    if not proposal_id and any(w in request.lower() for w in ["dashboard", "panel", "create", "make", "propose", "add", "update", "modify"]):
        try:
            tool_outcome = propose_dashboard(request=request, target=target or "", time_range=time_range or "1h")
            if tool_outcome and tool_outcome.get("proposalId"):
                proposal_id = tool_outcome["proposalId"]
        except Exception as exc:
            logger.debug("Direct dashboard proposal fallback skipped: %s", exc)

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
            "sessionId": session_id,
            "conversationId": session_id,
        }

    if final_text:
        return {
            "kind": "text",
            "text": final_text,
            "agent_response": final_text,
            "raw_json": raw_jsons,
            "timings": timings,
            "sessionId": session_id,
            "conversationId": session_id,
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
            "sessionId": session_id,
            "conversationId": session_id,
        }

    return {
        "kind": "text",
        "text": "Agent completed the turn with no output.",
        "agent_response": "Agent completed the turn with no output.",
        "raw_json": raw_jsons,
        "timings": timings,
        "sessionId": session_id,
        "conversationId": session_id,
    }
