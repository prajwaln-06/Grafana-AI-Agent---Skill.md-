import asyncio
import time
from typing import Optional

from .utils import logger
from .dashboard import fetch_dashboard_list, fetch_dashboard_detail, flatten_panels

_discovery_lock = asyncio.Lock()
_panel_index: Optional[list[dict]] = None
_panel_index_time: float = 0.0
_INDEX_TTL_SECONDS = 300.0


async def ensure_panel_index() -> list[dict]:
    """Build and cache a flat, richly-annotated panel index across all dashboards."""
    global _panel_index, _panel_index_time

    if _panel_index is not None:
        age = time.monotonic() - _panel_index_time
        if age < _INDEX_TTL_SECONDS:
            return _panel_index
        logger.info(f"Discovery: panel index is {age:.0f}s old, refreshing …")
        _panel_index = None

    async with _discovery_lock:
        if _panel_index is not None:
            return _panel_index

        logger.info("Discovery: building panel index …")
        t0 = time.perf_counter()

        dashboards = await fetch_dashboard_list()
        if not dashboards:
            logger.warning("Discovery: no dashboards found — index will NOT be cached")
            return []

        index: list[dict] = []
        seen_queries: set[str] = set()

        dashboard_details = await asyncio.gather(
            *[fetch_dashboard_detail(dash["uid"]) for dash in dashboards],
            return_exceptions=True
        )

        for dash, detail in zip(dashboards, dashboard_details):
            if isinstance(detail, Exception) or not detail:
                if isinstance(detail, Exception):
                    logger.warning(f"Discovery: error fetching dashboard '{dash['uid']}': {detail}")
                continue

            dash_obj = detail.get("dashboard", detail)
            panels = flatten_panels(dash_obj.get("panels", []))

            for panel in panels:
                panel_title = panel.get("title", "")
                panel_desc = panel.get("description", "")
                panel_type = panel.get("type", "")
                panel_ds = panel.get("datasource", None)

                for target in panel.get("targets", []):
                    expr = target.get("expr", "")
                    if not expr or not expr.strip():
                        continue

                    target_ds = target.get("datasource", panel_ds)
                    dedup_key = f"{dash['uid']}:{expr.strip()}"
                    if dedup_key in seen_queries:
                        continue
                    seen_queries.add(dedup_key)

                    index.append({
                        "dashboard_uid": dash["uid"],
                        "dashboard_title": dash["title"],
                        "dashboard_tags": dash.get("tags", []),
                        "panel_title": panel_title,
                        "panel_description": panel_desc,
                        "panel_type": panel_type,
                        "datasource": target_ds,
                        "query": expr.strip(),
                    })

        _panel_index = index
        _panel_index_time = time.monotonic()
        elapsed = time.perf_counter() - t0
        logger.info(
            f"Discovery: indexed {len(index)} unique panel queries "
            f"across {len(dashboards)} dashboard(s) in {elapsed:.2f}s"
        )
        return index
