import asyncio
import json
import threading
import time
from typing import Any, Optional

from google.adk.tools.mcp_tool.mcp_session_manager import (
    MCPSessionManager,
    StdioConnectionParams,
)
from mcp import StdioServerParameters

from .utils import require_env, extract_text, format_error, logger

_ENABLED_TOOLS = "search,dashboard,prometheus,datasource,elasticsearch"

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
            grafana_url = require_env("GRAFANA_URL")
            grafana_token = require_env("GRAFANA_SERVICE_ACCOUNT_TOKEN")

            connection_params = StdioConnectionParams(
                server_params=StdioServerParameters(
                    command="uvx",
                    args=[
                        "mcp-grafana",
                        f"--enabled-tools={_ENABLED_TOOLS}",
                        "--disable-write",
                    ],
                    env={
                        "GRAFANA_URL": grafana_url,
                        "GRAFANA_SERVICE_ACCOUNT_TOKEN": grafana_token,
                    },
                ),
                timeout=30,
            )
            _session_manager = MCPSessionManager(connection_params=connection_params)
            logger.info("Initialized Grafana MCP session manager")

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
