"""Proposal/review/approval frontend — routes through the ADK agent.

POST /api/proposals passes the user's natural-language request through:
    root_agent (LlmAgent)
      → Qwen via LiteLLM / Ollama
      → propose_dashboard() tool
      → existing dashboard-writing backend
      → Grafana wrapper → Grafana MCP
and returns the resulting Dashboard IR proposal for human review.

No LLM bypass: if the ADK/Qwen path fails the endpoint reports the
error rather than silently falling back to the deterministic builder.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import secrets
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import urlparse

from dotenv import load_dotenv

# Load .env at entry-point startup so all configuration is available before
# the first request, including GRAFANA_MCP_ENABLE_WRITE on the health
# endpoint.  agent.py also calls load_dotenv(), which is a safe no-op if the
# vars are already set.
_ENV_FILE = Path(__file__).with_name(".env")
load_dotenv(_ENV_FILE, override=False)  # override=False: OS env takes precedence

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types as genai_types

from .tools.grafana.dashboard_writing import (
    PROPOSALS,
    execute_approved_mutation,
    refresh_preview,
)
from .tools.grafana.session import run_sync

logger = logging.getLogger(__name__)

STATIC = Path(__file__).with_name("static")
_conversation_lock = Lock()
_conversations: dict[str, dict] = {}


def _conversation(conversation_id: str) -> dict:
    with _conversation_lock:
        state = _conversations.setdefault(conversation_id, {"conversationId": conversation_id, "messages": [], "latestProposalId": None})
        return copy.deepcopy(state)


def _append_message(conversation_id: str, message: dict) -> dict:
    with _conversation_lock:
        state = _conversations.setdefault(conversation_id, {"conversationId": conversation_id, "messages": [], "latestProposalId": None})
        state["messages"].append(message)
        if message.get("proposalId"):
            state["latestProposalId"] = message["proposalId"]
        return copy.deepcopy(state)

# ---------------------------------------------------------------------------
# ADK Runner — lazily initialised on first proposal request.
# Ollama is probed inside agent._pick_model(); defer until needed.
# ---------------------------------------------------------------------------
_runner: Runner | None = None
_runner_lock = Lock()
_SESSION_SVC = InMemorySessionService()
_APP_NAME = "grafana_webapp"


def _get_runner() -> Runner:
    """Return the shared Runner, initialising it once on first call."""
    global _runner
    if _runner is not None:
        return _runner
    with _runner_lock:
        if _runner is None:
            # Import here so Ollama is not probed at module load time.
            from .agent import root_agent  # noqa: PLC0415
            logger.info("[webapp] Initialising ADK Runner with root_agent")
            _runner = Runner(
                app_name=_APP_NAME,
                agent=root_agent,
                session_service=_SESSION_SVC,
                auto_create_session=True,
            )
            logger.info("[webapp] ADK Runner ready")
    return _runner


def _run_agent_turn(request: str, conversation_id: str, target: str | None, time_range: str) -> dict:
    """Drive root_agent through the ADK Runner and return the Dashboard IR proposal.

    The agent is expected to call propose_dashboard() which stores the
    IR in PROPOSALS and returns the proposal dict.  We scan the event
    stream for a FunctionResponse from 'propose_dashboard' and extract
    the proposal ID so we can return the full stored proposal.

    Raises RuntimeError if the agent does not produce a proposal.
    No silent fallback to build_proposal().
    """
    from .tools.grafana.wrapper import clear_raw_json, get_raw_json
    clear_raw_json()

    # Build the user message — include target and time_range as context
    # so the agent has all the information it needs without us pre-parsing.
    user_text = request
    state = _conversation(conversation_id)
    if state.get("latestProposalId"):
        try:
            current = PROPOSALS.get(state["latestProposalId"])
            user_text += f"\nCurrent conversation dashboard: {current['ir'].get('name', '')}"
        except KeyError:
            pass
    if target:
        user_text += f"\nTarget node/host: {target}"
    if time_range and time_range != "1h":
        user_text += f"\nTime range: {time_range}"

    runner = _get_runner()

    user_id = f"webapp-user-{conversation_id}"
    session_id = conversation_id

    message = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text=user_text)],
    )

    logger.info(
        "[webapp] ADK run_async → root_agent | session=%s | request=%r",
        session_id,
        user_text[:120],
    )

    proposal_id: str | None = None
    final_text = ""
    tool_outcome: dict | None = None
    turn_started = time.perf_counter()
    first_tool_call_at: float | None = None
    tool_response_at: float | None = None
    final_response_at: float | None = None

    # Runner.run() is a synchronous generator backed by a daemon thread.
    # It is safe to call from the ThreadingHTTPServer worker threads.
    for event in runner.run(
        user_id=user_id,
        session_id=session_id,
        new_message=message,
    ):
        author = getattr(event, "author", "?")
        # Log every tool call so operators can verify the LLM was reached.
        for fc in event.get_function_calls():
            if first_tool_call_at is None:
                first_tool_call_at = time.perf_counter()
            logger.info("[webapp] LLM tool call → %s(%s)", fc.name, list((fc.args or {}).keys()))
        # Scan function responses for propose_dashboard result.
        for fr in event.get_function_responses():
            logger.info("[webapp] tool response ← %s", fr.name)
            if fr.name == "propose_dashboard" and isinstance(fr.response, dict):
                tool_response_at = time.perf_counter()
                tool_outcome = fr.response
                pid = fr.response.get("proposalId")
                if pid:
                    logger.info("[webapp] propose_dashboard returned proposalId=%s", pid)
                    proposal_id = pid
        # Log final agent text response.
        if event.is_final_response() and event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    final_response_at = time.perf_counter()
                    final_text = part.text
                    logger.info(
                        "[webapp] agent final response (author=%s): %s",
                        author,
                        part.text[:300],
                    )

        # Frontend renders proposal and terminal error outcomes deterministically.
        # Waiting for a second local-model pass adds latency and its prose is unused.
        if tool_outcome is not None:
            break

    turn_finished = time.perf_counter()
    timings = {
        "toolSelectionMs": round(((first_tool_call_at or turn_finished) - turn_started) * 1000, 1),
        "toolExecutionMs": round(((tool_response_at or turn_finished) - (first_tool_call_at or turn_started)) * 1000, 1),
        "finalContinuationMs": round(((final_response_at or tool_response_at or turn_finished) - (tool_response_at or turn_finished)) * 1000, 1),
        "totalMs": round((turn_finished - turn_started) * 1000, 1),
    }
    logger.info(
        "[webapp] turn_timing selection_ms=%.1f tool_ms=%.1f continuation_ms=%.1f total_ms=%.1f",
        timings["toolSelectionMs"], timings["toolExecutionMs"], timings["finalContinuationMs"], timings["totalMs"],
    )

    raw_jsons = get_raw_json()

    if proposal_id:
        return {
            "kind": "proposal",
            "status": (tool_outcome or {}).get("status", "success"),
            "text": "Here is the proposed dashboard. Review and modify it before applying.",
            "agent_response": "Here is the proposed dashboard. Review and modify it before applying.",
            "raw_json": raw_jsons,
            "proposal": PROPOSALS.get(proposal_id),
            "errors": (tool_outcome or {}).get("errors", []),
            "timings": timings,
        }
    if final_text:
        return {"kind": "text", "text": final_text, "agent_response": final_text, "raw_json": raw_jsons, "timings": timings}
    if tool_outcome:
        status = tool_outcome.get("status", "error")
        if status == "clarification":
            text = tool_outcome.get("question", "Please clarify the dashboard operation.")
        elif status == "unsupported":
            text = tool_outcome.get("reason", "Requested dashboard operation is unsupported.")
        else:
            errors = tool_outcome.get("errors", [])
            text = "; ".join(item.get("message", str(item)) if isinstance(item, dict) else str(item) for item in errors) or "Dashboard operation failed."
        return {"kind": "text", "status": status, "text": text, "agent_response": text, "raw_json": raw_jsons, "errors": tool_outcome.get("errors", []), "timings": timings}
    raise RuntimeError("The ADK agent returned no response. See server logs for the full agent trace.")


def _public_conversation(conversation_id: str) -> dict:
    state = _conversation(conversation_id)
    for message in state["messages"]:
        pid = message.get("proposalId")
        if pid:
            try:
                message["proposal"] = PROPOSALS.get(pid)
            except KeyError:
                message["proposal"] = None
    return state


def _grafana_url(result: dict) -> str | None:
    raw = result.get("grafanaResult")
    relative = raw.get("url") or raw.get("dashboardUrl") if isinstance(raw, dict) else None
    if not relative:
        return None
    if str(relative).startswith(("http://", "https://")):
        return str(relative)
    base = os.environ.get("GRAFANA_URL", "").rstrip("/")
    return f"{base}/{str(relative).lstrip('/')}" if base else None


class Handler(SimpleHTTPRequestHandler):
    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self) -> None:
        if self.path == "/api/health":
            self._send(200, {"status": "ok", "writeEnabled": os.environ.get("GRAFANA_MCP_ENABLE_WRITE", "").lower() in {"1", "true", "yes", "on"}})
            return
        if self.path.startswith("/api/proposals/"):
            try:
                self._send(200, PROPOSALS.get(self.path.rsplit("/", 1)[-1]))
            except KeyError as exc:
                self._send(404, {"error": str(exc)})
            return
        if self.path.startswith("/api/conversations/"):
            conversation_id = urlparse(self.path).path.rsplit("/", 1)[-1]
            self._send(200, _public_conversation(conversation_id))
            return
        self.path = "/index.html" if self.path == "/" else self.path
        return super().do_GET()

    def do_POST(self) -> None:
        try:
            data = self._body()
            path = urlparse(self.path).path
            if path == "/api/conversations":
                conversation_id = data.get("conversationId") or secrets.token_urlsafe(18)
                result = _public_conversation(conversation_id)
            elif path.endswith("/messages") and path.startswith("/api/conversations/"):
                conversation_id = path.split("/")[-2]
                request = str(data.get("request", "")).strip()
                if not request:
                    raise ValueError("Message is required.")
                _append_message(conversation_id, {"id": secrets.token_urlsafe(10), "role": "user", "text": request})
                turn = _run_agent_turn(request, conversation_id, data.get("target") or None, data.get("timeRange", "1h"))
                assistant = {"id": secrets.token_urlsafe(10), "role": "assistant", "text": turn["text"], "kind": turn["kind"]}
                if turn.get("proposal"):
                    assistant["proposalId"] = turn["proposal"]["proposalId"]
                if turn.get("timings"):
                    assistant["timings"] = turn["timings"]
                if "agent_response" in turn:
                    assistant["agent_response"] = turn["agent_response"]
                if "raw_json" in turn:
                    assistant["raw_json"] = turn["raw_json"]
                _append_message(conversation_id, assistant)
                result = _public_conversation(conversation_id)
                result["message"] = copy.deepcopy(result["messages"][-1])
            elif path == "/api/proposals":
                result = _run_agent_turn(data.get("request", ""), secrets.token_urlsafe(18), data.get("target") or None, data.get("timeRange", "1h")).get("proposal")
            elif path.endswith("/preview") and path.startswith("/api/proposals/"):
                payload = data
                panel_ids = {str(value) for value in payload.get("panelIds", [])}
                if not panel_ids:
                    raise ValueError("At least one panel ID is required for preview refresh.")
                refreshed = run_sync(refresh_preview(payload["ir"], panel_ids))
                result = {
                    "panels": [panel for panel in refreshed.get("panels", []) if str(panel.get("id")) in panel_ids],
                }
            elif path.endswith("/approve"):
                pid = path.split("/")[-2]
                result = PROPOSALS.approve(pid, int(data["version"]))
            elif path.endswith("/reject"):
                pid = path.split("/")[-2]
                result = PROPOSALS.reject(pid, int(data["version"]))
            elif path.endswith("/execute"):
                pid = path.split("/")[-2]
                result = run_sync(execute_approved_mutation(pid, int(data["version"]), data.get("approvalToken", "")))
                result["dashboardUrl"] = _grafana_url(result)
                conversation_id = data.get("conversationId")
                if conversation_id:
                    result["message"] = {"id": secrets.token_urlsafe(10), "role": "assistant", "kind": "execution", "text": "Dashboard created successfully.", "dashboardUrl": result["dashboardUrl"]}
                    _append_message(conversation_id, result["message"])
            else:
                self._send(404, {"error": "Endpoint not found."})
                return
            self._send(200, result)
        except (ValueError, KeyError, PermissionError) as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:
            self._send(502, {"error": str(exc)})

    def do_PUT(self) -> None:
        try:
            path = urlparse(self.path).path
            if not path.startswith("/api/proposals/"):
                self._send(404, {"error": "Endpoint not found."})
                return
            result = PROPOSALS.modify(path.rsplit("/", 1)[-1], self._body()["ir"])
            self._send(200, result)
        except (ValueError, KeyError) as exc:
            self._send(400, {"error": str(exc)})

    def translate_path(self, path: str) -> str:
        relative = path.split("?", 1)[0].lstrip("/") or "index.html"
        return str(STATIC / relative)


def main() -> None:
    host = os.environ.get("DASHBOARD_APP_HOST", "127.0.0.1")
    port = int(os.environ.get("DASHBOARD_APP_PORT", "8080"))
    print(f"Dashboard proposal UI: http://{host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
