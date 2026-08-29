import asyncio
import json
from typing import Any, Optional

from app.mcp.session import mcp_call
from .utils import logger

_SUPPORTED_DS_TYPES = {"prometheus", "elasticsearch", "opensearch"}

_datasource_registry: Optional[dict[str, list[dict]]] = None
_datasource_lock = asyncio.Lock()


async def ensure_datasource_registry() -> dict[str, list[dict]]:
    """Discover and cache all Grafana datasources, grouped by type."""
    global _datasource_registry
    if _datasource_registry is not None:
        return _datasource_registry

    async with _datasource_lock:
        if _datasource_registry is not None:
            return _datasource_registry

        logger.info("Datasource registry: discovering …")
        registry: dict[str, list[dict]] = {}

        result_str = await mcp_call("list_datasources", {}, raw=True)
        if result_str:
            try:
                data = json.loads(result_str)
                sources = (
                    data
                    if isinstance(data, list)
                    else data.get("datasources", data.get("data", []))
                )
                for ds in sources:
                    if not isinstance(ds, dict):
                        continue
                    plugin_type = ds.get("type", "").lower()
                    ds_type = "opensearch" if plugin_type == "grafana-opensearch-datasource" else plugin_type
                    entry = {
                        "uid": ds.get("uid", ""),
                        "name": ds.get("name", ""),
                        "type": ds_type,
                        "plugin_type": plugin_type,
                        "is_default": ds.get("isDefault", False),
                    }
                    registry.setdefault(ds_type, []).append(entry)
            except json.JSONDecodeError:
                logger.error("list_datasources did not return valid JSON.")

        if "prometheus" not in registry:
            import os
            default_uid = os.environ.get("GRAFANA_DEFAULT_DATASOURCE_UID", "PBFA97CFB590B2093")
            registry.setdefault("prometheus", []).append({
                "uid": default_uid,
                "name": "Prometheus",
                "type": "prometheus",
                "plugin_type": "prometheus",
                "is_default": True,
            })

        for ds_type, entries in registry.items():
            names = ", ".join(e["name"] for e in entries)
            logger.info(f"  Registry: {ds_type}: {names}")

        _datasource_registry = registry
        return registry


async def resolve_datasource(ds_ref: Any, *, allow_generic_fallback: bool = True) -> Optional[dict]:
    """Resolve a panel datasource reference to a registry entry."""
    registry = await ensure_datasource_registry()

    ref_type: str | None = None
    ref_uid: str | None = None

    if isinstance(ds_ref, dict):
        ref_type = ds_ref.get("type", "").lower() or None
        ref_uid = ds_ref.get("uid", "").strip() or None
    elif isinstance(ds_ref, str):
        ref_type = ds_ref.lower().strip() or None

    logger.debug(
        f"Datasource resolution: raw ds_ref={ds_ref}, "
        f"extracted uid={ref_uid}, extracted type={ref_type}"
    )

    if ref_uid:
        for entries in registry.values():
            for e in entries:
                if e["uid"] == ref_uid:
                    logger.debug(f"  -> UID lookup succeeded: {e['name']}")
                    return e
        logger.debug(f"  -> UID lookup failed for '{ref_uid}'")

    if ref_type:
        if ref_type in registry:
            entries = registry[ref_type]
            for e in entries:
                if e.get("is_default"):
                    logger.debug(f"  -> TYPE fallback succeeded (default): {e['name']}")
                    return e
            logger.debug(f"  -> TYPE fallback succeeded (first): {entries[0]['name']}")
            return entries[0]
        
        # Treat opensearch and elasticsearch interchangeably
        if ref_type == "opensearch" and "elasticsearch" in registry:
            return registry["elasticsearch"][0]
        elif ref_type == "elasticsearch" and "opensearch" in registry:
            return registry["opensearch"][0]
            
        logger.debug(f"  -> TYPE fallback failed for '{ref_type}'")

    if allow_generic_fallback:
        for ds_type in _SUPPORTED_DS_TYPES:
            if ds_type in registry:
                logger.debug(f"  -> Fallback to generic default ({ds_type}): {registry[ds_type][0]['name']}")
                return registry[ds_type][0]

    logger.debug("  -> Resolution failed, no match found")
    return None


def datasources_summary() -> str:
    if not _datasource_registry:
        return "(no datasources discovered)"
    lines = []
    for ds_type, entries in sorted(_datasource_registry.items()):
        for e in entries:
            lines.append(f"- {e['name']} (type: {ds_type})")
    return "\n".join(lines)
