import json
import time
from typing import Optional

from app.mcp.session import mcp_call
from .utils import logger

_dashboard_list: Optional[list[dict]] = None
_dashboard_list_time: float = 0.0
_dashboard_details: dict[str, dict] = {}


async def fetch_dashboard_list(*, refresh: bool = False) -> list[dict]:
    """Fetch and cache the list of all dashboards (title, uid, tags)."""
    global _dashboard_list, _dashboard_list_time
    if _dashboard_list is not None and not refresh:
        return _dashboard_list

    dash_str = await mcp_call(
        "grafana_api_request",
        {"method": "GET", "endpoint": "/api/search?type=dash-db"},
        raw=True,
    )
    if not dash_str:
        logger.warning("Discovery: Grafana dashboard-list API returned empty string")
        return []

    dashboards: list[dict] = []
    try:
        data = json.loads(dash_str)
    except json.JSONDecodeError as exc:
        logger.warning(f"Discovery: Grafana dashboard-list API returned invalid JSON: {exc}")
        return []

    items: list = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for key in ("result", "data", "dashboards", "hits"):
            if key in data and isinstance(data[key], list):
                items = data[key]
                logger.info(f"Discovery: unwrapped list from envelope key '{key}'")
                break

    for d in items:
        if not isinstance(d, dict):
            continue
        uid = d.get("uid") or d.get("UID") or d.get("id", "")
        if not uid:
            continue
        dashboards.append({
            "uid": str(uid),
            "title": d.get("title", ""),
            "tags": [t for t in d.get("tags", []) if isinstance(t, str)],
            "url": d.get("url", ""),
        })

    _dashboard_list = dashboards
    _dashboard_list_time = time.monotonic()
    logger.info(f"Discovery: fetched {len(dashboards)} dashboard(s)")
    return dashboards


async def fetch_dashboard_detail(uid: str, *, refresh: bool = False) -> Optional[dict]:
    """Fetch and cache full dashboard JSON for a single UID."""
    if uid in _dashboard_details and not refresh:
        return _dashboard_details[uid]

    detail_str = await mcp_call("get_dashboard_by_uid", {"uid": uid}, raw=True)
    if not detail_str:
        logger.warning(f"Discovery: failed to fetch dashboard '{uid}'")
        return None
    try:
        detail = json.loads(detail_str)
        _dashboard_details[uid] = detail
        return detail
    except json.JSONDecodeError:
        logger.warning(f"Discovery: invalid JSON for dashboard '{uid}'")
        return None


def flatten_panels(raw_panels: list[dict]) -> list[dict]:
    """Recursively flatten rows and nested panels."""
    flat: list[dict] = []
    for p in raw_panels:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "row":
            flat.extend(flatten_panels(p.get("panels", [])))
        else:
            flat.append(p)
    return flat


async def search_dashboards_mcp(query: str) -> str:
    return await mcp_call("search_dashboards", {"query": query.strip()})


async def get_dashboard_summary_mcp(uid: str) -> str:
    return await mcp_call("get_dashboard_summary", {"uid": uid.strip()}, raw=True)


async def get_dashboard_panel_queries_mcp(uid: str) -> str:
    return await mcp_call("get_dashboard_panel_queries", {"uid": uid.strip()}, raw=True)
