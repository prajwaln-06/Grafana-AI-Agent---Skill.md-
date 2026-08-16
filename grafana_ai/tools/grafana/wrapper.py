import json
import time
import re
from typing import Any

from .utils import logger, cap, normalize_to_text
from .session import run_sync
from .dashboard import (
    fetch_dashboard_list,
    search_dashboards_mcp,
    get_dashboard_summary_mcp,
)
from .discovery import ensure_panel_index
from .scoring import score_and_select
from .datasource import resolve_datasource, datasources_summary, _SUPPORTED_DS_TYPES
from .prometheus import execute_prometheus, format_prometheus_result
from .opensearch import execute_opensearch, normalize_opensearch_result


def list_dashboards() -> str:
    """Return every Grafana dashboard as a numbered human-readable list."""
    return run_sync(_list_dashboards_async())

async def _list_dashboards_async() -> str:
    logger.info("Wrapper: list_dashboards")
    start = time.perf_counter()

    dashboards = await fetch_dashboard_list()

    if not dashboards:
        return (
            "No dashboards were found in this Grafana instance.\n"
            "Check that GRAFANA_URL and GRAFANA_SERVICE_ACCOUNT_TOKEN are correct "
            "and that at least one dashboard exists."
        )

    lines = [f"Available dashboards ({len(dashboards)} total):\n"]
    for i, dash in enumerate(dashboards, 1):
        title = dash.get("title") or "(untitled)"
        uid   = dash.get("uid", "")
        tags  = dash.get("tags", [])

        lines.append(f"{i}. {title}")
        lines.append(f"   UID: {uid}")
        if tags:
            lines.append(f"   Tags: {', '.join(tags)}")
        lines.append("")

    elapsed = time.perf_counter() - start
    logger.info(f"Wrapper list_dashboards done in {elapsed:.3f}s — {len(dashboards)} dashboard(s)")
    return cap("\n".join(lines).strip())


def search_dashboards(query: str) -> str:
    """Search Grafana dashboards by title keyword. Returns titles and UIDs."""
    return run_sync(_search_dashboards_async(query))

async def _search_dashboards_async(query: str) -> str:
    logger.info(f"Wrapper: search_dashboards query='{query}'")
    start = time.perf_counter()

    if not query or not query.strip():
        return (
            "Error: search_dashboards requires a non-empty keyword. "
            "To see all dashboards, call list_dashboards() instead."
        )

    result_str = await search_dashboards_mcp(query)

    if result_str.startswith("Error"):
        return result_str

    normalized = normalize_to_text(result_str)
    elapsed = time.perf_counter() - start
    logger.info(f"Wrapper search_dashboards done in {elapsed:.3f}s")
    return cap(normalized)


def get_dashboard_panels(uid: str) -> str:
    """List panels and their purpose for a dashboard UID."""
    return run_sync(_get_dashboard_panels_async(uid))

async def _get_dashboard_panels_async(uid: str) -> str:
    logger.info(f"Wrapper: get_dashboard_panels uid='{uid}'")
    start = time.perf_counter()

    if not uid or not uid.strip():
        return "Error: Dashboard UID cannot be empty."

    result_str = await get_dashboard_summary_mcp(uid)

    if not result_str:
        return "Error: Dashboard not found or MCP unavailable."

    try:
        summary = json.loads(result_str)
    except json.JSONDecodeError:
        return "Error: Received invalid response from Grafana."

    if not isinstance(summary, dict):
        return "Error: Received invalid response from Grafana."

    title = summary.get("title", "Unknown")
    panels = summary.get("panels", [])

    if not panels:
        return f"Dashboard '{title}' (UID: {uid}) has no panels."

    lines = [f"Dashboard: {title} (UID: {uid})", f"Panels ({len(panels)} total):"]

    variables = summary.get("variables", [])
    if variables:
        var_names = ", ".join(
            v.get("name", "") for v in variables
            if isinstance(v, dict) and v.get("name")
        )
        if var_names:
            lines.append(f"Variables: {var_names}")

    time_range = summary.get("timeRange", {})
    if time_range.get("from") and time_range.get("to"):
        lines.append(f"Default time range: {time_range['from']} to {time_range['to']}")

    lines.append("")

    for i, panel in enumerate(panels, 1):
        if not isinstance(panel, dict):
            continue
        p_title = panel.get("title", "Untitled")
        p_type = panel.get("type", "")
        p_desc = panel.get("description", "").strip()
        q_count = panel.get("queryCount", 0)

        entry = f" {i}. {p_title} ({p_type})"
        if q_count:
            entry += f" [{q_count} quer{'y' if q_count == 1 else 'ies'}]"
        if p_desc:
            entry += f" — {p_desc[:80]}"
        lines.append(entry)

        if i >= 20:
            remaining = len(panels) - 20
            if remaining > 0:
                lines.append(f" ... and {remaining} more panels")
            break

    elapsed = time.perf_counter() - start
    logger.info(f"Wrapper get_dashboard_panels done in {elapsed:.3f}s")
    return cap("\n".join(lines))


async def _execute_query_router(
    expr: str,
    ds_ref: Any,
    lookback: str = "1h",
    step_seconds: int = 60,
) -> tuple[str, str]:
    """Route query execution to the appropriate datasource executor."""
    logger.debug(f"Routing query. ds_ref provided to router: {ds_ref}")
    resolved = await resolve_datasource(ds_ref)
    if resolved is None:
        return (
            "Error: Could not resolve a datasource for this panel.\n\n"
            f"Available datasources:\n{datasources_summary()}",
            ""
        )

    ds_type = resolved["type"]
    ds_uid = resolved["uid"]

    if ds_type not in _SUPPORTED_DS_TYPES:
        return (
            f"Error: Datasource type '{ds_type}' is not yet supported.\n"
            f"Currently supported: {', '.join(sorted(_SUPPORTED_DS_TYPES))}.\n\n"
            f"Available datasources:\n{datasources_summary()}",
            ds_type
        )
    if ds_type != "prometheus":
        return (
            f"Error: query_prometheus_metric requires a Prometheus datasource, but resolved '{ds_type}'.",
            ds_type,
        )

    logger.info(f"  Executing on: {resolved['name']} (type={ds_type}, uid={ds_uid})")
    
    res = await execute_prometheus(expr, ds_uid, lookback, step_seconds)
    return res, ds_type


def query_prometheus_metric(
    metric: str = "",
    expr: str = "",
    lookback: str = "1h",
    step_seconds: int = 60,
) -> str:
    """Query live metrics. Use metric= for natural language or expr= for explicit PromQL."""
    return run_sync(_query_prometheus_metric_async(metric, expr, lookback, step_seconds))

async def _query_prometheus_metric_async(
    metric: str = "",
    expr: str = "",
    lookback: str = "1h",
    step_seconds: int = 60,
) -> str:
    logger.info(f"Wrapper: query_prometheus_metric metric='{metric}' expr='{expr}'")
    start = time.perf_counter()

    has_metric = metric is not None and str(metric).strip() != ""
    has_expr   = expr   is not None and str(expr).strip()   != ""

    if has_metric and has_expr:
        return "Error: Provide either 'metric' OR 'expr', not both."
    if not has_metric and not has_expr:
        return "Error: Provide either a natural-language 'metric' description or an explicit PromQL 'expr'."

    final_expr: str
    discovery_context = ""
    ds_ref: Any = None

    if has_expr:
        final_expr = str(expr).strip()
        logger.info(f"  Route: explicit PromQL → {final_expr}")
    else:
        keyword = str(metric).strip()
        logger.info(f"  Route: dynamic discovery for '{keyword}'")

        result = await score_and_select(keyword)

        if result is None:
            index = await ensure_panel_index()
            if index:
                titles = sorted({e["panel_title"] for e in index if e["panel_title"]})[:20]
                available = "\n".join(f"- {t}" for t in titles)
                return (
                    f"No panel matching '{keyword}' was found.\n\n"
                    f"Available panels:\n{available}"
                )
            return (
                f"No panel matching '{keyword}' was found. "
                "No dashboards are available in this Grafana instance."
            )

        if isinstance(result, str):
            return result

        final_expr = result["query"]
        ds_ref = result.get("datasource")
        discovery_context = (
            f"Discovered from panel '{result['panel_title']}' "
            f"in dashboard '{result['dashboard_title']}'"
        )
        logger.info(f"  Selected query: {final_expr[:120]}")

    result_str, ds_type = await _execute_query_router(final_expr, ds_ref, lookback, step_seconds)

    if result_str.startswith("Error"):
        elapsed = time.perf_counter() - start
        logger.info(f"Wrapper query_prometheus_metric failed in {elapsed:.3f}s")
        return result_str

    try:
        data = json.loads(result_str)
        hints = data.get("hints") if isinstance(data, dict) and isinstance(data.get("hints"), dict) else None
        
        if "data" in data and "result" in data["data"]:
            normalized = format_prometheus_result(
                data["data"]["result"], discovery_context, hints
            )
        else:
            normalized = normalize_to_text(result_str)
    except json.JSONDecodeError:
        normalized = result_str

    elapsed = time.perf_counter() - start
    logger.info(f"Wrapper query_prometheus_metric done in {elapsed:.3f}s")
    return cap(normalized)

def _escape_lucene(text: str) -> str:
    """Safely escapes Lucene special characters to prevent syntax errors and injection."""
    escape_chars = r'([+\-&|!(){}\[\]^"~*?:\\/])'
    return re.sub(escape_chars, r'\\\1', text)

def query_opensearch_logs(
    query: str = "*",
    service: str = "",
    host: str = "",
    severity: str = "",
    index: str = "consolelog-*,heartbeat,syslog-*",
    lookback: str = "1h",
    limit: int = 20,
) -> dict:
    """Query OpenSearch documents through Grafana MCP and return normalized documents."""
    return run_sync(_query_opensearch_logs_async(query, service, host, severity, index, lookback, limit))

async def _query_opensearch_logs_async(
    query: str = "*",
    service: str = "",
    host: str = "",
    severity: str = "",
    index: str = "consolelog-*,heartbeat,syslog-*",
    lookback: str = "1h",
    limit: int = 20,
) -> dict:
    logger.info(f"Wrapper: query_opensearch_logs query='{query}' index='{index}' lookback='{lookback}' limit={limit}")
    start = time.perf_counter()

    if not re.match(r'^\d+[smhdw]$', lookback):
        return {"status": "error", "error": f"Invalid lookback format '{lookback}'. Use values such as 15m, 1h, or 7d."}
    if not index or not index.strip():
        return {"status": "error", "error": "At least one OpenSearch index or index pattern is required."}
    try:
        normalized_limit = int(limit)
    except (TypeError, ValueError):
        return {"status": "error", "error": "limit must be an integer between 1 and 100."}
    if not 1 <= normalized_limit <= 100:
        return {"status": "error", "error": "limit must be between 1 and 100."}

    ds = await resolve_datasource("opensearch", allow_generic_fallback=False)
    if not ds:
        return {"status": "error", "error": "Could not resolve an OpenSearch/Elasticsearch datasource through Grafana MCP.", "availableDatasources": datasources_summary()}

    stripped_query = (query or "*").strip() or "*"
    if stripped_query.startswith("{") and ds["type"] == "opensearch":
        return {"status": "error", "error": "The installed MCP tool supports Query DSL only for Elasticsearch datasources; OpenSearch queries must use Lucene syntax."}
    lucene_parts = []
    if stripped_query != "*":
        if stripped_query.startswith("{"):
            if any((service, host, severity)):
                return {"status": "error", "error": "Structured filters cannot be combined with an Elasticsearch Query DSL string."}
            final_query = stripped_query
        elif ":" not in stripped_query and '"' not in stripped_query:
            lucene_parts.append(f'Body:({_escape_lucene(stripped_query)})')
        else:
            lucene_parts.append(stripped_query)
    if service and service.strip():
        lucene_parts.append(f'Resource.service.name:"{_escape_lucene(service.strip())}"')
    if host and host.strip():
        lucene_parts.append(f'Resource.host.name:"{_escape_lucene(host.strip())}"')
    if severity and severity.strip():
        lvl = severity.strip().upper()
        if lvl not in ("INFO", "WARN", "WARNING", "ERROR", "DEBUG", "TRACE", "CRITICAL"):
            return {"status": "error", "error": f"Invalid severity '{lvl}'."}
        lucene_parts.append(f'(Severity:"{lvl.lower()}" OR SeverityText:"{lvl}")')

    if not stripped_query.startswith("{"):
        final_query = " AND ".join(lucene_parts) if lucene_parts else "*"

    start_time = f"now-{lookback}"
    result_str = await execute_opensearch(final_query, ds["uid"], index.strip(), start_time, "now", normalized_limit)

    if result_str.startswith("Error"):
        return {"status": "error", "error": result_str}

    try:
        data = json.loads(result_str)
        normalized = normalize_opensearch_result(data, ds, final_query, index.strip(), start_time, "now")
    except json.JSONDecodeError:
        normalized = {"status": "error", "error": "Grafana MCP returned an invalid OpenSearch response."}

    elapsed = time.perf_counter() - start
    logger.info(f"Wrapper query_opensearch_logs done in {elapsed:.3f}s")
    return normalized


query_logs = query_opensearch_logs
