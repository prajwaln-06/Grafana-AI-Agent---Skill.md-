import asyncio
import json
import os
import threading
import time
from typing import Any, Optional
from dotenv import load_dotenv

load_dotenv()

from google.adk.tools.mcp_tool.mcp_session_manager import (
    MCPSessionManager,
    StdioConnectionParams,
)
from mcp import StdioServerParameters


import logging
logger = logging.getLogger("app.mcp_session")

def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing env var '{name}'.")
    return value

def extract_text(raw_result: Any) -> str:
    content = getattr(raw_result, "content", None) or []
    parts = [getattr(block, "text", "") for block in content]
    return "\n".join(p for p in parts if p)

def format_error(message: str) -> str:
    return f"Error: {message}"


_ENABLED_TOOLS = "search,dashboard,prometheus,datasource,elasticsearch,api"

_loop = asyncio.new_event_loop()
def _run_loop():
    asyncio.set_event_loop(_loop)
    _loop.run_forever()

_loop_thread = threading.Thread(target=_run_loop, daemon=True)
_loop_thread.start()

def run_sync(coro):
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    return future.result()

_session_manager: Optional[MCPSessionManager] = None
_session_manager_lock = asyncio.Lock()

async def get_session_manager() -> MCPSessionManager:
    """Returns a shared, lazily-created MCPSessionManager for this process."""
    global _session_manager
    if _session_manager is not None:
        return _session_manager

    async with _session_manager_lock:
        if _session_manager is None:
            grafana_url = os.environ.get("GRAFANA_URL", "http://localhost:3000")
            grafana_token = os.environ.get("GRAFANA_SERVICE_ACCOUNT_TOKEN", "glsa_local_dummy_token")

            import shutil
            venv_bin = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), ".venv", "bin", "mcp-grafana")
            local_bin = venv_bin if os.path.exists(venv_bin) else shutil.which("mcp-grafana")
            
            if local_bin:
                mcp_cmd = local_bin
                mcp_args = [f"--enabled-tools={_ENABLED_TOOLS}"]
            else:
                mcp_cmd = "uvx"
                mcp_args = ["mcp-grafana", f"--enabled-tools={_ENABLED_TOOLS}"]

            if os.environ.get("GRAFANA_MCP_ENABLE_WRITE", "").lower() not in {
                "1", "true", "yes", "on"
            }:
                mcp_args.append("--disable-write")

            connection_params = StdioConnectionParams(
                server_params=StdioServerParameters(
                    command=mcp_cmd,
                    args=mcp_args,
                    env={
                        "GRAFANA_URL": grafana_url,
                        "GRAFANA_SERVICE_ACCOUNT_TOKEN": grafana_token,
                    },
                ),
                timeout=30,
            )
            _session_manager = MCPSessionManager(connection_params=connection_params)
            logger.info("Initialized Grafana MCP session manager with cmd=%s", mcp_cmd)

    return _session_manager


async def mcp_call(
    tool: str,
    args: dict,
    *,
    retries: int = 1,
    raw: bool = False,
    timeout: float = 15.0,
) -> str:
    """Call a single Grafana MCP tool and return a string result."""
    logger.info(f"MCP call: {tool} | args={args} | raw={raw}")
    start = time.perf_counter()
    last_exc: Exception | None = None

    for attempt in range(retries + 1):
        try:
            manager = await get_session_manager()
            session = await manager.create_session()
            result = await asyncio.wait_for(
                session.call_tool(tool, args), timeout=timeout
            )
            elapsed = time.perf_counter() - start
            logger.info(f"MCP '{tool}' ok in {elapsed:.3f}s (attempt {attempt + 1})")

            if getattr(result, "isError", False):
                err = extract_text(result)
                logger.error(f"MCP '{tool}' tool error: {err}")
                return "" if raw else format_error(f"Tool execution failed: {err}")

            if raw:
                structured = getattr(result, "structuredContent", None)
                if structured is not None:
                    logger.debug(
                        f"MCP '{tool}' structuredContent type={type(structured).__name__} "
                        f"| preview={str(structured)[:120]}"
                    )
                    try:
                        return json.dumps(structured)
                    except (TypeError, ValueError) as exc:
                        logger.warning(
                            f"MCP '{tool}' structuredContent not JSON-serializable: {exc}; "
                            "falling back to text"
                        )
                text = extract_text(result)
                logger.debug(
                    f"MCP '{tool}' text content preview={text[:120]!r}"
                )
                return text

            structured = getattr(result, "structuredContent", None)
            if structured:
                try:
                    return json.dumps(structured, separators=(",", ":"))
                except (TypeError, ValueError):
                    pass
            text = extract_text(result)
            return text if text else format_error("Empty result.")

        except asyncio.TimeoutError as exc:
            last_exc = exc
            logger.warning(f"MCP '{tool}' timeout (attempt {attempt + 1})")
        except Exception as exc:
            last_exc = exc
            logger.warning(f"MCP '{tool}' error: {exc} (attempt {attempt + 1})")
            if raw:
                break

    elapsed = time.perf_counter() - start
    logger.error(f"MCP '{tool}' exhausted retries in {elapsed:.3f}s. Last: {last_exc}")
    return "" if raw else format_error("Grafana MCP server unavailable or request timed out.")
