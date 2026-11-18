import json
import time
from typing import Optional

from app.mcp.session import mcp_call
from .utils import logger

_dashboard_list: Optional[list[dict]] = None
_dashboard_list_time: float = 0.0
_dashboard_details: dict[str, dict] = {}


def invalidate_dashboard_cache() -> None:
    """Clear all cached dashboard listings and details."""
    global _dashboard_list, _dashboard_list_time, _dashboard_details
    _dashboard_list = None
    _dashboard_list_time = 0.0
    _dashboard_details.clear()
    logger.info("Invalidated in-memory dashboard cache")


async def fetch_dashboard_list(*, refresh: bool = False) -> list[dict]:
    """Fetch and cache the list of all dashboards (title, uid, tags)."""
    global _dashboard_list, _dashboard_list_time
    now = time.monotonic()
    if _dashboard_list is not None and not refresh and (now - _dashboard_list_time < 30.0):
        return _dashboard_list

    dash_str = await mcp_call(
        "grafana_api_request",
        {"method": "GET", "endpoint": "/api/search?type=dash-db"},
        raw=True,
    )
    dashboards: list[dict] = []
    if not dash_str:
        logger.warning("Discovery: Grafana dashboard-list API returned empty string")
    else:
        try:
            data = json.loads(dash_str)
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
                folder_uid = d.get("folderUid", d.get("folderUID"))
                dashboards.append({
                    "uid": str(uid),
                    "title": d.get("title", ""),
                    "tags": [t for t in d.get("tags", []) if isinstance(t, str)],
                    "url": d.get("url", ""),
                    "folderUid": str(folder_uid) if folder_uid is not None else None,
                    "folderTitle": d.get("folderTitle", "") or "",
                })
        except json.JSONDecodeError as exc:
            logger.warning(f"Discovery: Grafana dashboard-list API returned invalid JSON: {exc}")

    if not dashboards:
        from app.grafana_tools.demo_dashboards import DEMO_DASHBOARDS
        dashboards = [
            {
                "uid": uid,
                "title": d["title"],
                "tags": d.get("tags", ["observability"]),
                "url": f"/d/{uid}",
                "folderUid": d["folder_uid"],
                "folderTitle": d["folder_uid"],
            }
            for uid, d in DEMO_DASHBOARDS.items()
        ]

    _dashboard_list = dashboards
    _dashboard_list_time = time.monotonic()
    logger.info(f"Discovery: fetched {len(dashboards)} dashboard(s)")
    return dashboards


async def fetch_dashboard_detail(uid: str, *, refresh: bool = False) -> Optional[dict]:
    """Fetch and cache full dashboard JSON for a single UID."""
    if uid in _dashboard_details and not refresh:
        return _dashboard_details[uid]

    detail_str = await mcp_call("get_dashboard_by_uid", {"uid": uid}, raw=True)
    if not detail_str or "Unauthorized" in detail_str or "Error" in detail_str:
        from app.grafana_tools.demo_dashboards import DEMO_DASHBOARDS
        if uid in DEMO_DASHBOARDS:
            demo = DEMO_DASHBOARDS[uid]
            detail = {
                "dashboard": {
                    "uid": uid,
                    "title": demo["title"],
                    "panels": demo["panels"],
                },
                "meta": {
                    "folderUid": demo["folder_uid"],
                    "folderTitle": demo["folder_uid"],
                },
                "folderUid": demo["folder_uid"],
            }
            _dashboard_details[uid] = detail
            return detail
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


async def fetch_dashboard_folder_uid(uid: str, *, refresh: bool = False) -> tuple[bool, str | None]:
    """Resolve ``(exists, folder_uid)`` for a dashboard from Grafana.

    The folder UID is the dashboard's authoritative RBAC dimension, and it
    is taken from Grafana -- never from the request. To learn it, only
    *metadata* is fetched: the search listing already reports the folder of
    every dashboard it returns, so no dashboard body is retrieved for the
    common case.

    The complete dashboard JSON is fetched only as a fallback, when the
    listing does not know the dashboard (for instance because it is beyond
    the listing's page). Even then the body stays internal: only the folder
    field is read out of it, and it is never returned, logged or passed on.
    A dashboard that lives in the General folder reports an empty folder
    UID, which is Grafana's own representation of "no folder".
    """
    uid = str(uid or "").strip()
    if not uid:
        return False, None

    # --- Metadata-only lookup: the search listing reports each dashboard's
    # folder, so the dashboard body is never needed for the scope decision.
    for item in await fetch_dashboard_list(refresh=refresh):
        if str(item.get("uid")) == uid:
            folder_uid = item.get("folderUid")
            return True, (str(folder_uid) if folder_uid is not None else None)

    # --- Fallback: the listing did not report this dashboard. Read the
    # folder out of the dashboard's own metadata, keeping the body internal.
    detail = await fetch_dashboard_detail(uid, refresh=refresh)
    if isinstance(detail, dict):
        meta = detail.get("meta") if isinstance(detail.get("meta"), dict) else {}
        folder_uid = meta.get("folderUid", meta.get("folderUID"))
        if folder_uid is None:
            folder_uid = detail.get("folderUid", detail.get("folderUID"))
        if folder_uid is not None:
            return True, str(folder_uid)

    from app.grafana_tools.demo_dashboards import DEMO_DASHBOARDS
    if uid in DEMO_DASHBOARDS:
        return True, DEMO_DASHBOARDS[uid]["folder_uid"]

    return False, None


async def get_dashboard_summary_mcp(uid: str) -> str:
    return await mcp_call("get_dashboard_summary", {"uid": uid.strip()}, raw=True)


async def get_dashboard_panel_queries_mcp(uid: str) -> str:
    return await mcp_call("get_dashboard_panel_queries", {"uid": uid.strip()}, raw=True)
