import os
import requests as _requests
from dotenv import load_dotenv

# Set UTF-8 mode — required on Windows to avoid UnicodeDecodeError with litellm
os.environ["PYTHONUTF8"] = "1"

# Load .env from this directory
load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset, StdioConnectionParams
from mcp import StdioServerParameters

# ---------------------------------------------------------------------------
# Model configuration — qwen2.5:14b first, gemma4:12b as fallback
# ---------------------------------------------------------------------------
PRIMARY_MODEL  = "ollama_chat/qwen2.5:14b"
FALLBACK_MODEL = "ollama_chat/gemma4:12b"

# ---------------------------------------------------------------------------
# MCP tool whitelist
# ---------------------------------------------------------------------------
ALLOWED_MCP_TOOLS = [
    "search_dashboards",
    "get_dashboard_by_uid",
    "get_dashboard_summary",
    "list_datasources",
    "get_datasource",
    "check_datasources_health",
    "query_prometheus",
    "list_alert_groups",
]

# ---------------------------------------------------------------------------
# Agent instruction
# ---------------------------------------------------------------------------
AGENT_INSTRUCTION = (
    "You are an AI-powered Grafana Observability Assistant. "
    "You have access to a curated set of Grafana tools:\n"
    "  • search_dashboards — find dashboards by name or tag\n"
    "  • get_dashboard_by_uid — fetch full dashboard JSON by UID\n"
    "  • get_dashboard_summary — get a concise summary of a dashboard\n"
    "  • list_datasources — list all configured Grafana datasources\n"
    "  • get_datasource — get details for a specific datasource\n"
    "  • check_datasources_health — check health of all datasources\n"
    "  • query_prometheus — run PromQL instant or range queries\n"
    "  • list_alert_groups — list active alert groups\n\n"
    "Always use these tools when the user asks about dashboards, metrics, "
    "datasources, or alerts. Do NOT use LaTeX math formatting (like $$) for "
    "formulas; write them in plain text instead."
)

# ---------------------------------------------------------------------------
# Synchronous Ollama model probe — safe at module import time
# ---------------------------------------------------------------------------
def _pick_model() -> LiteLlm:
    """Returns a LiteLlm for the first available Ollama model."""
    try:
        resp = _requests.get("http://localhost:11434/api/tags", timeout=3.0)
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
            "Ollama is not reachable at http://localhost:11434. "
            "Make sure Ollama is running (`ollama serve`)."
        ) from exc

# ---------------------------------------------------------------------------
# Build root_agent
# McpToolset is instantiated inline — ADK manages the MCP subprocess lifecycle
# per-invocation, so no asyncio.run() needed at import time.
# ---------------------------------------------------------------------------
root_agent = LlmAgent(
    model=_pick_model(),
    name="grafana_observability_agent",
    description="AI-powered Grafana observability assistant backed by Ollama and the Grafana MCP server.",
    instruction=AGENT_INSTRUCTION,
    tools=[
        McpToolset(
            connection_params=StdioConnectionParams(
                server_params=StdioServerParameters(
                    command="uvx",
                    args=["mcp-grafana"],
                    env={
                        "GRAFANA_URL": os.environ.get("GRAFANA_URL", "http://localhost:3000"),
                        "GRAFANA_SERVICE_ACCOUNT_TOKEN": os.environ.get(
                            "GRAFANA_SERVICE_ACCOUNT_TOKEN", ""
                        ),
                    },
                ),
            ),
            tool_filter=ALLOWED_MCP_TOOLS,
        )
    ],
)
