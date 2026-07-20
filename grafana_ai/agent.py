import os
import requests as _requests
from dotenv import load_dotenv

# Set UTF-8 mode — required on Windows to avoid UnicodeDecodeError with litellm
os.environ["PYTHONUTF8"] = "1"

# Load .env from this directory
load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm

from grafana_ai.tools.grafana_wrapper import (
    get_dashboard_panels,
    list_dashboards,
    query_prometheus_metric,
    search_dashboards,
)

# ---------------------------------------------------------------------------
# Model configuration — qwen2.5:14b first, gemma4:12b as fallback
# ---------------------------------------------------------------------------
PRIMARY_MODEL  = "ollama_chat/qwen2.5:14b"
FALLBACK_MODEL = "ollama_chat/gemma4:12b"
OLLAMA_API_BASE = os.environ.get("OLLAMA_API_BASE", "http://localhost:11434")

# ---------------------------------------------------------------------------
# Synchronous Ollama model probe — safe at module import time
# ---------------------------------------------------------------------------
def _pick_model() -> LiteLlm:
    """Returns a LiteLlm for the first available Ollama model."""
    try:
        resp = _requests.get(f"{OLLAMA_API_BASE.rstrip('/')}/api/tags", timeout=3.0)
        resp.raise_for_status()
        pulled = [m.get("name", "") for m in resp.json().get("models", [])]
        for model_id in (PRIMARY_MODEL, FALLBACK_MODEL):
            short = model_id.replace("ollama_chat/", "")
            if any(short in name for name in pulled):
                print(f"[grafana_ai] Using Ollama model: {model_id}")
                return LiteLlm(model=model_id)
        raise RuntimeError(
            f"Neither '{PRIMARY_MODEL}' nor '{FALLBACK_MODEL}' found in Ollama. "
            f"Available models: {pulled}"
        )
    except _requests.exceptions.RequestException as exc:
        raise RuntimeError(
            f"Ollama is not reachable at {OLLAMA_API_BASE}. "
            "Make sure Ollama is running (`ollama serve`)."
        ) from exc




# ---------------------------------------------------------------------------
# Agent instruction
# ---------------------------------------------------------------------------
AGENT_INSTRUCTION = """\
You are a Grafana Observability Assistant.
Retrieve infrastructure data using your tools. Summarize results in natural language.

LIVE METRICS
For any request about live metric values, current usage, or monitoring data:
-> Call query_prometheus_metric(metric="<describe what the user wants>").
-> The wrapper automatically discovers the correct query from Grafana dashboards.
-> Never generate PromQL yourself. Never guess metric names.

EXPLICIT PROMQL
If the user explicitly provides a PromQL expression:
-> Call query_prometheus_metric(expr="<the exact expression>").
-> Never rewrite, optimize, or substitute it.

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

IMPORTANT
If a wrapper can answer the request, invoke it immediately.
Never ask for confirmation. Call the wrapper first, then summarize the result.

RULES
- For greetings or casual conversation: respond naturally without using any tools.
- Never fabricate dashboard names, UIDs, or metric values.
- Never expose raw JSON. Summarize naturally.
- Never output <think> tags or internal reasoning.
"""

# ---------------------------------------------------------------------------
# Build root_agent
# ---------------------------------------------------------------------------
root_agent = LlmAgent(
    model=_pick_model(),
    name="grafana_observability_agent",
    description="AI-powered Grafana observability assistant backed by Ollama and a unified tools wrapper.",
    instruction=AGENT_INSTRUCTION,
    tools=[
        list_dashboards,
        search_dashboards,
        get_dashboard_panels,
        query_prometheus_metric,
    ],
)
