import json
import logging
import os
from typing import Any

logger = logging.getLogger("hpe_adk.grafana_wrapper")
if not logger.handlers:
    logging.basicConfig(level=logging.DEBUG)
    logger.setLevel(logging.DEBUG)

def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing env var '{name}'.")
    return value

_MAX_RESULT_CHARS = 3500

def extract_text(raw_result: Any) -> str:
    content = getattr(raw_result, "content", None) or []
    parts = [getattr(block, "text", "") for block in content]
    return "\n".join(p for p in parts if p)

def format_error(message: str) -> str:
    return f"Error: {message}"

def cap(payload: str, limit: int = _MAX_RESULT_CHARS) -> str:
    """Truncate oversized tool results to prevent LLM context overflow."""
    if len(payload) <= limit:
        return payload
    truncated = payload[:limit]
    for marker in ("},", "}]", "}"):
        idx = truncated.rfind(marker)
        if idx > limit // 2:
            truncated = truncated[: idx + len(marker)]
            break
    return truncated + "...[TRUNCATED]"

def normalize_to_text(json_str: str) -> str:
    """Convert a JSON string to a compact human-readable representation."""
    try:
        data = json.loads(json_str)
        if isinstance(data, list):
            if not data:
                return "No results found."
            items = []
            for item in data:
                if isinstance(item, dict):
                    title = item.get("title", item.get("name", "Unknown"))
                    uid = item.get("uid", item.get("id", "Unknown UID"))
                    url = item.get("url", "")
                    items.append(f"- {title} (UID: {uid}) {url}")
                else:
                    items.append(f"- {item}")
            return "\n".join(items)
        elif isinstance(data, dict):
            items = []
            for k, v in data.items():
                if k in ("meta", "dashboard") and isinstance(v, dict):
                    items.append(f"{k}: <complex object hidden for brevity>")
                else:
                    items.append(f"{k}: {v}")
            return "\n".join(items)
    except json.JSONDecodeError:
        pass
    return json_str
