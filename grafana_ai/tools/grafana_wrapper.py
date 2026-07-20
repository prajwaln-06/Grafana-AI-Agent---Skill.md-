# app/tools/grafana_wrapper.py
"""Final production wrapper layer around the Grafana MCP server."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Optional

from google.adk.tools.mcp_tool.mcp_session_manager import (
    MCPSessionManager,
    StdioConnectionParams,
)
from mcp import StdioServerParameters

logger = logging.getLogger("hpe_adk.grafana_wrapper")
if not logger.handlers:
    logging.basicConfig(level=logging.DEBUG)
    logger.setLevel(logging.DEBUG)


_ENABLED_TOOLS = "search,dashboard,prometheus,datasource"
_MAX_RESULT_CHARS = 3500


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing env var '{name}'.")
    return value


# ---------------------------------------------------------------------------
# MCP session — singleton, lazily created
# ---------------------------------------------------------------------------

_session_manager: Optional[MCPSessionManager] = None
_session_manager_lock = asyncio.Lock()


async def _get_session_manager() -> MCPSessionManager:
    """Returns a shared, lazily-created MCPSessionManager for this process."""
    global _session_manager
    if _session_manager is not None:
        return _session_manager

    async with _session_manager_lock:
        if _session_manager is None:
            grafana_url = _require_env("GRAFANA_URL")
            grafana_token = _require_env("GRAFANA_SERVICE_ACCOUNT_TOKEN")

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


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _extract_text(raw_result: Any) -> str:
    content = getattr(raw_result, "content", None) or []
    parts = [getattr(block, "text", "") for block in content]
    return "\n".join(p for p in parts if p)


def _format_error(message: str) -> str:
    return f"Error: {message}"


def _cap(payload: str, limit: int = _MAX_RESULT_CHARS) -> str:
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


def _normalize_to_text(json_str: str) -> str:
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


# ---------------------------------------------------------------------------
# Unified MCP call — single code path for all internal + public calls.
#
# raw=False (default): LLM-facing path — formats result, retries, caps.
# raw=True           : Discovery path — returns "" on any error, no retry overhead.
# ---------------------------------------------------------------------------

async def _mcp_call(
    tool: str,
    args: dict,
    *,
    retries: int = 1,
    raw: bool = False,
    timeout: float = 15.0,
) -> str:
    """Call a single Grafana MCP tool and return a string result.

    Args:
        tool:    MCP tool name.
        args:    Arguments dict.
        retries: Additional attempts after the first (total = retries + 1).
        raw:     If True, return plain text/JSON with no formatting; return ""
                 on any error. Used internally for discovery to avoid polluting
                 the LLM with error messages.
        timeout: Per-attempt timeout in seconds.
    """
    logger.info(f"MCP call: {tool} | args={args} | raw={raw}")
    start = time.perf_counter()
    last_exc: Exception | None = None

    for attempt in range(retries + 1):
        try:
            manager = await _get_session_manager()
            session = await manager.create_session()
            result = await asyncio.wait_for(
                session.call_tool(tool, args), timeout=timeout
            )
            elapsed = time.perf_counter() - start
            logger.info(f"MCP '{tool}' ok in {elapsed:.3f}s (attempt {attempt + 1})")

            if getattr(result, "isError", False):
                err = _extract_text(result)
                logger.error(f"MCP '{tool}' tool error: {err}")
                return "" if raw else _format_error(f"Tool execution failed: {err}")

            if raw:
                structured = getattr(result, "structuredContent", None)
                if structured is not None:
                    # Log the type so format mismatches are instantly visible.
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
                text = _extract_text(result)
                logger.debug(
                    f"MCP '{tool}' text content preview={text[:120]!r}"
                )
                return text

            # LLM-facing: structured → compact JSON, else text
            structured = getattr(result, "structuredContent", None)
            if structured:
                try:
                    return json.dumps(structured, separators=(",", ":"))
                except (TypeError, ValueError):
                    pass
            text = _extract_text(result)
            return text if text else _format_error("Empty result.")

        except asyncio.TimeoutError as exc:
            last_exc = exc
            logger.warning(f"MCP '{tool}' timeout (attempt {attempt + 1})")
        except Exception as exc:
            last_exc = exc
            logger.warning(f"MCP '{tool}' error: {exc} (attempt {attempt + 1})")
            if raw:
                # Do not retry on hard errors for discovery calls
                break

    elapsed = time.perf_counter() - start
    logger.error(f"MCP '{tool}' exhausted retries in {elapsed:.3f}s. Last: {last_exc}")
    return "" if raw else _format_error("Grafana MCP server unavailable or request timed out.")


# ---------------------------------------------------------------------------
# Datasource Registry
# Discovers ALL datasources once and indexes them by type.
# OpenSearch will register here as "elasticsearch" when added.
# ---------------------------------------------------------------------------

_SUPPORTED_DS_TYPES = {"prometheus"}

_datasource_registry: Optional[dict[str, list[dict]]] = None
_datasource_lock = asyncio.Lock()


async def _ensure_datasource_registry() -> dict[str, list[dict]]:
    """Discover and cache all Grafana datasources, grouped by type."""
    global _datasource_registry
    if _datasource_registry is not None:
        return _datasource_registry

    async with _datasource_lock:
        if _datasource_registry is not None:
            return _datasource_registry

        logger.info("Datasource registry: discovering …")
        registry: dict[str, list[dict]] = {}

        result_str = await _mcp_call("list_datasources", {}, raw=True)
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
                    ds_type = ds.get("type", "").lower()
                    entry = {
                        "uid": ds.get("uid", ""),
                        "name": ds.get("name", ""),
                        "type": ds_type,
                        "is_default": ds.get("isDefault", False),
                    }
                    registry.setdefault(ds_type, []).append(entry)
            except json.JSONDecodeError:
                logger.error("list_datasources did not return valid JSON.")

        for ds_type, entries in registry.items():
            names = ", ".join(e["name"] for e in entries)
            logger.info(f"  Registry: {ds_type}: {names}")

        _datasource_registry = registry
        return registry


async def _resolve_datasource(ds_ref: Any) -> Optional[dict]:
    """Resolve a panel datasource reference to a registry entry.

    Accepts:
      - {"type": "prometheus", "uid": "xxx"}  (panel target format)
      - "prometheus"  (legacy string)
      - None / missing  → returns default Prometheus datasource
    """
    registry = await _ensure_datasource_registry()

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
        logger.debug(f"  -> TYPE fallback failed for '{ref_type}'")

    # No reference — fall back to default Prometheus
    for ds_type in _SUPPORTED_DS_TYPES:
        if ds_type in registry:
            logger.debug(f"  -> Fallback to generic default ({ds_type}): {registry[ds_type][0]['name']}")
            return registry[ds_type][0]

    logger.debug("  -> Resolution failed, no match found")
    return None


def _datasources_summary() -> str:
    if not _datasource_registry:
        return "(no datasources discovered)"
    lines = []
    for ds_type, entries in sorted(_datasource_registry.items()):
        for e in entries:
            lines.append(f"- {e['name']} (type: {ds_type})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Panel Index — dynamic discovery across all dashboards
# ---------------------------------------------------------------------------

_discovery_lock = asyncio.Lock()
_dashboard_list: Optional[list[dict]] = None
_dashboard_list_time: float = 0.0
_dashboard_details: dict[str, dict] = {}
_panel_index: Optional[list[dict]] = None
_panel_index_time: float = 0.0
_INDEX_TTL_SECONDS = 300.0


async def _fetch_dashboard_list() -> list[dict]:
    """Fetch and cache the list of all dashboards (title, uid, tags)."""
    global _dashboard_list, _dashboard_list_time
    if _dashboard_list is not None:
        return _dashboard_list

    dash_str = await _mcp_call("search_dashboards", {"query": ""}, raw=True)
    if not dash_str:
        logger.warning("Discovery: search_dashboards returned empty string")
        return []  # Do NOT cache; allow retry on next call

    logger.debug(f"Discovery: search_dashboards raw preview={dash_str[:200]!r}")

    dashboards: list[dict] = []
    try:
        data = json.loads(dash_str)
    except json.JSONDecodeError as exc:
        logger.warning(f"Discovery: search_dashboards returned invalid JSON: {exc}")
        logger.warning(f"Discovery: raw string was: {dash_str[:300]!r}")
        return []

    logger.debug(f"Discovery: parsed data type={type(data).__name__}")

    # Normalise: the MCP may return a bare list OR wrap it in a dict envelope.
    # Known envelope keys from various MCP/Grafana versions:
    #   {"result": [...]}  {"data": [...]}  {"dashboards": [...]}  {"hits": [...]}
    items: list = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for key in ("result", "data", "dashboards", "hits"):
            if key in data and isinstance(data[key], list):
                items = data[key]
                logger.info(f"Discovery: unwrapped list from envelope key '{key}'")
                break
        if not items:
            logger.warning(
                f"Discovery: search_dashboards returned a dict with no known list key. "
                f"Keys present: {list(data.keys())}"
            )
    else:
        logger.warning(
            f"Discovery: search_dashboards returned unexpected type {type(data).__name__}"
        )

    for d in items:
        if not isinstance(d, dict):
            logger.debug(f"Discovery: skipping non-dict item: {d!r}")
            continue
        uid = d.get("uid") or d.get("UID") or d.get("id", "")
        if not uid:
            logger.debug(f"Discovery: skipping item with no uid: {list(d.keys())}")
            continue
        dashboards.append({
            "uid": str(uid),
            "title": d.get("title", ""),
            "tags": [t for t in d.get("tags", []) if isinstance(t, str)],
            "url": d.get("url", ""),
        })

    if not dashboards and items:
        logger.warning(
            f"Discovery: {len(items)} item(s) returned but none had a usable uid. "
            f"First item keys: {list(items[0].keys()) if items else []}"
        )

    _dashboard_list = dashboards
    _dashboard_list_time = time.monotonic()
    logger.info(f"Discovery: fetched {len(dashboards)} dashboard(s)")
    return dashboards


async def _fetch_dashboard_detail(uid: str) -> Optional[dict]:
    """Fetch and cache full dashboard JSON for a single UID."""
    if uid in _dashboard_details:
        return _dashboard_details[uid]

    detail_str = await _mcp_call("get_dashboard_by_uid", {"uid": uid}, raw=True)
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


def _flatten_panels(raw_panels: list[dict]) -> list[dict]:
    """Recursively flatten rows and nested panels."""
    flat: list[dict] = []
    for p in raw_panels:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "row":
            flat.extend(_flatten_panels(p.get("panels", [])))
        else:
            flat.append(p)
    return flat


async def _ensure_panel_index() -> list[dict]:
    """Build and cache a flat, richly-annotated panel index across all dashboards."""
    global _panel_index, _panel_index_time, _dashboard_list

    if _panel_index is not None:
        age = time.monotonic() - _panel_index_time
        if age < _INDEX_TTL_SECONDS:
            return _panel_index
        logger.info(f"Discovery: panel index is {age:.0f}s old, refreshing …")
        _panel_index = None
        _dashboard_list = None
        _dashboard_details.clear()

    async with _discovery_lock:
        if _panel_index is not None:
            return _panel_index

        logger.info("Discovery: building panel index …")
        t0 = time.perf_counter()

        dashboards = await _fetch_dashboard_list()
        if not dashboards:
            logger.warning("Discovery: no dashboards found — index will NOT be cached")
            return []

        index: list[dict] = []
        seen_queries: set[str] = set()

        dashboard_details = await asyncio.gather(
            *[_fetch_dashboard_detail(dash["uid"]) for dash in dashboards],
            return_exceptions=True
        )

        for dash, detail in zip(dashboards, dashboard_details):
            if isinstance(detail, Exception) or not detail:
                if isinstance(detail, Exception):
                    logger.warning(f"Discovery: error fetching dashboard '{dash['uid']}': {detail}")
                continue

            dash_obj = detail.get("dashboard", detail)
            panels = _flatten_panels(dash_obj.get("panels", []))

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
                    dedup_key = f"{uid}:{expr.strip()}"
                    if dedup_key in seen_queries:
                        continue
                    seen_queries.add(dedup_key)

                    index.append({
                        "dashboard_uid": uid,
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


# ---------------------------------------------------------------------------
# Panel Scoring / Selection
# ---------------------------------------------------------------------------

_CONFIDENCE_THRESHOLD = 3
_AMBIGUITY_RATIO = 0.85

_STOP_WORDS = {
    "show", "get", "current", "usage", "the", "of", "a", "an",
    "metrics", "metric", "dashboard", "panel", "display", "find", "me",
    "what", "is",
}

# Domain alias groups mapping synonyms to a unified concept.
# By grouping them, a search for 'k8s' will successfully match a dashboard 
# that only uses the word 'kubernetes' or a metric prefix like 'kube_'.
_DOMAIN_ALIASES = [
    {"kubernetes", "k8s", "kube"},
    {"postgres", "postgresql"},
    {"mongodb", "mongo"},
    {"opensearch"},
    {"elasticsearch"},
    {"gpu", "nvidia"},
    # Singletons for specific domains
    {"docker"}, {"redis"}, {"mysql"}, {"nginx"}, {"cadvisor"},
    {"loki"}, {"tempo"}, {"promtail"}
]

_QUALIFIER_MAP: dict[str, set[str]] = {}
for alias_set in _DOMAIN_ALIASES:
    for alias in alias_set:
        _QUALIFIER_MAP[alias] = alias_set



def _score_entry(tokens: list[str], full_keyword: str, entry: dict) -> int:
    panel = entry["panel_title"].lower()
    dash = entry["dashboard_title"].lower()
    desc = entry.get("panel_description", "").lower()
    query = entry["query"].lower()
    tags = " ".join(t.lower() for t in entry.get("dashboard_tags", []))

    # Robustly tokenize the PromQL query into identifiers (e.g., metric names, labels)
    query_tokens = set(re.findall(r'[A-Za-z_][A-Za-z0-9_]*', query))

    score = 0
    if full_keyword in panel:
        score += 10
        
    for tok in tokens:
        if len(tok) < 2 or tok in _STOP_WORDS:
            continue
            
        # If the token belongs to a domain qualifier, check all of its aliases
        aliases_to_check = _QUALIFIER_MAP.get(tok, {tok})
        
        best_tok_score = 0
        matched = False
        
        for alias in aliases_to_check:
            alias_score = 0
            if alias in panel:
                alias_score += 4
            if alias in desc:
                alias_score += 3
            if alias in tags:
                alias_score += 2
            if any(alias in qt for qt in query_tokens):
                alias_score += 1
            if alias in dash:
                alias_score += 1
                
            if alias_score > 0:
                matched = True
                best_tok_score = max(best_tok_score, alias_score)
                
        score += best_tok_score
            
        # Hard penalty for missing domain qualifiers.
        # If the user asked for a specific domain (e.g. 'kubernetes') and NONE
        # of its aliases exist in this panel's context, this is a false positive.
        if not matched and tok in _QUALIFIER_MAP:
            return 0

    if not panel.strip():
        score = max(0, score - 5)
    return score


async def _score_and_select(keyword: str) -> dict | str | None:
    """Find the best-matching panel entry for a natural-language keyword.

    Returns:
        dict  — winning panel entry
        str   — ambiguity message asking for clarification
        None  — no match
    """
    index = await _ensure_panel_index()
    if not index:
        return None

    kw_lower = keyword.lower().strip()
    tokens = kw_lower.replace("_", " ").split()

    scored: list[tuple[int, dict]] = []
    for entry in index:
        s = _score_entry(tokens, kw_lower, entry)
        if s >= _CONFIDENCE_THRESHOLD:
            scored.append((s, entry))

    if not scored:
        return None

    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best = scored[0]

    if len(scored) >= 2:
        second_score, second = scored[1]
        different_dash = best["dashboard_uid"] != second["dashboard_uid"]
        if different_dash and second_score / best_score >= _AMBIGUITY_RATIO:
            seen_uids: set[str] = set()
            candidates: list[str] = []
            for _, e in scored[:5]:
                if e["dashboard_uid"] not in seen_uids:
                    seen_uids.add(e["dashboard_uid"])
                    candidates.append(
                        f"- '{e['panel_title']}' in dashboard '{e['dashboard_title']}'"
                    )
            logger.info(f"Discovery: ambiguous match for '{keyword}', {len(candidates)} candidates")
            return (
                f"Multiple panels match '{keyword}':\n"
                + "\n".join(candidates)
                + "\n\nPlease be more specific about which metric you want."
            )

    logger.info(
        f"Discovery: '{keyword}' → panel '{best['panel_title']}' "
        f"in '{best['dashboard_title']}' (score={best_score}) → {best['query'][:100]}"
    )
    return best


# ---------------------------------------------------------------------------
# Query Execution — datasource-aware, extensible
# ---------------------------------------------------------------------------

def _preprocess_promql(expr: str, lookback: str) -> str:
    """Transform Grafana dashboard expressions into valid, executable PromQL."""
    original = expr
    
    # 1. Substitute built-in duration/interval variables (Parse Error preventers)
    # These must be substituted because they appear where PromQL expects duration
    # literals (e.g., [5m]). Unsubstituted, they cause a Prometheus parse error.
    expr = re.sub(r'\$__rate_interval\b', '5m', expr)
    expr = re.sub(r'\$\{__rate_interval\}', '5m', expr)
    expr = re.sub(r'\$__interval\b', '5m', expr)
    expr = re.sub(r'\$\{__interval\}', '5m', expr)
    expr = re.sub(r'\$interval\b', '5m', expr)
    
    expr = re.sub(r'\$__range\b', lookback, expr)
    expr = re.sub(r'\$\{__range\}', lookback, expr)
    
    expr = re.sub(r'\$__range_s\b', '3600', expr)
    expr = re.sub(r'\$\{__range_s\}', '3600', expr)
    expr = re.sub(r'\$__range_ms\b', '3600000', expr)
    expr = re.sub(r'\$\{__range_ms\}', '3600000', expr)

    # 2. Substitute dashboard template variables in label matchers
    # These do not cause parse errors, but would return empty data because
    # PromQL treats them as literal strings (e.g., instance="$node").
    def _label_repl(match: re.Match) -> str:
        label = match.group(1)
        op = match.group(2)
        if op == '=':
            op = '=~'
        elif op == '!=':
            op = '!~'
        return f'{label}{op}".*"'

    expr = re.sub(r'([a-zA-Z_:][a-zA-Z0-9_:]*)\s*(=|!=|=~|!~)\s*"[^"]*\$[a-zA-Z0-9_]+[^"]*"', _label_repl, expr)
    expr = re.sub(r'([a-zA-Z_:][a-zA-Z0-9_:]*)\s*(=|!=|=~|!~)\s*"[^"]*\$\{[a-zA-Z0-9_]+\}[^"]*"', _label_repl, expr)

    if expr != original:
        logger.debug(f"Preprocessing PromQL:\nOriginal: {original}\nPreprocessed: {expr}")
        
    return expr


async def _execute_prometheus(
    expr: str, ds_uid: str, lookback: str, step_seconds: int
) -> str:
    """Execute a PromQL range query via the MCP query_prometheus tool."""
    preprocessed_expr = _preprocess_promql(expr, lookback)
    
    return await _mcp_call(
        "query_prometheus",
        {
            "datasourceUid": ds_uid,
            "expr": preprocessed_expr,
            "queryType": "range",
            "startTime": f"now-{lookback}",
            "endTime": "now",
            "stepSeconds": step_seconds,
        },
    )


async def _execute_query(
    expr: str,
    ds_ref: Any,
    lookback: str = "1h",
    step_seconds: int = 60,
) -> str:
    """Route query execution to the appropriate datasource executor."""
    logger.debug(f"Routing query. ds_ref provided to router: {ds_ref}")
    resolved = await _resolve_datasource(ds_ref)
    if resolved is None:
        return (
            "Error: Could not resolve a datasource for this panel.\n\n"
            f"Available datasources:\n{_datasources_summary()}"
        )

    ds_type = resolved["type"]
    ds_uid = resolved["uid"]

    if ds_type not in _SUPPORTED_DS_TYPES:
        return (
            f"Error: Datasource type '{ds_type}' is not yet supported.\n"
            f"Currently supported: {', '.join(sorted(_SUPPORTED_DS_TYPES))}.\n\n"
            f"Available datasources:\n{_datasources_summary()}"
        )

    logger.info(f"  Executing on: {resolved['name']} (type={ds_type}, uid={ds_uid})")
    return await _execute_prometheus(expr, ds_uid, lookback, step_seconds)


# ---------------------------------------------------------------------------
# Result formatting
# ---------------------------------------------------------------------------

def _format_prometheus_result(
    results: list[dict], context: str = "", hints: Optional[dict] = None
) -> str:
    """Human-friendly summary of Prometheus range-query results.

    Args:
        results:  Prometheus result list from data.result.
        context:  Optional discovery context string (which panel/dashboard).
        hints:    Optional hints dict from QueryPrometheusResult when results
                  are empty. The MCP populates this with diagnostic reasons.
    """
    if not results:
        # Forward MCP-provided diagnostic hints when available.
        # The query_prometheus tool populates hints.message with specific
        # reasons (e.g. wrong time range, metric doesn't exist, etc.).
        if hints:
            hint_msg = hints.get("message", "")
            if hint_msg:
                return f"No metrics were returned.\n\nDiagnostic hint: {hint_msg}"

        return (
            "No metrics were returned.\n\n"
            "Possible reasons:\n"
            "- metric does not exist in this environment\n"
            "- scrape target is down\n"
            "- selected time range has no samples\n"
            "- datasource returned no matching series"
        )

    lines: list[str] = []
    if context:
        lines.append(context)
        lines.append("")
    lines.append(f"{len(results)} series returned.\n")

    for series in results[:10]:
        meta = series.get("metric", {})
        name = meta.get("__name__", "")
        label_parts = [f"{k}={v}" for k, v in meta.items() if k != "__name__"]
        identifier = name
        if label_parts:
            identifier += "{" + ", ".join(label_parts[:4]) + "}"

        values = series.get("values", [])
        value = series.get("value", [])

        if values:
            _, val_raw = values[-1]
        elif value:
            _, val_raw = value
        else:
            lines.append(f"{identifier}: no data")
            continue

        try:
            val_str = f"{float(val_raw):.2f}"
        except (ValueError, TypeError):
            val_str = str(val_raw)
        lines.append(f"{identifier}: {val_str}")

    if len(results) > 10:
        lines.append(f"… and {len(results) - 10} more series")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# PUBLIC WRAPPER: list_dashboards
# Use for: "list dashboards", "show all dashboards", "what dashboards exist"
# ---------------------------------------------------------------------------

async def list_dashboards() -> str:
    """Return every Grafana dashboard as a numbered human-readable list."""
    logger.info("Wrapper: list_dashboards")
    start = time.perf_counter()

    dashboards = await _fetch_dashboard_list()

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
    return _cap("\n".join(lines).strip())


# ---------------------------------------------------------------------------
# PUBLIC WRAPPER: search_dashboards
# Use for: keyword searches — "cpu", "node", "kubernetes", etc.
# ---------------------------------------------------------------------------

async def search_dashboards(query: str) -> str:
    """Search Grafana dashboards by title keyword. Returns titles and UIDs."""
    logger.info(f"Wrapper: search_dashboards query='{query}'")
    start = time.perf_counter()

    if not query or not query.strip():
        return (
            "Error: search_dashboards requires a non-empty keyword. "
            "To see all dashboards, call list_dashboards() instead."
        )

    result_str = await _mcp_call("search_dashboards", {"query": query.strip()})

    if result_str.startswith("Error"):
        return result_str

    normalized = _normalize_to_text(result_str)
    elapsed = time.perf_counter() - start
    logger.info(f"Wrapper search_dashboards done in {elapsed:.3f}s")
    return _cap(normalized)


# ---------------------------------------------------------------------------
# PUBLIC WRAPPER: get_dashboard_panels
# Replaced: get_dashboard_overview + get_dashboard_panel_queries (public)
# Output: human-readable panel catalogue — NO raw PromQL exposed to LLM
# ---------------------------------------------------------------------------

async def get_dashboard_panels(uid: str) -> str:
    """List panels and their purpose for a dashboard UID."""
    logger.info(f"Wrapper: get_dashboard_panels uid='{uid}'")
    start = time.perf_counter()

    if not uid or not uid.strip():
        return "Error: Dashboard UID cannot be empty."

    # Use get_dashboard_summary (compact struct) instead of get_dashboard_by_uid
    # (full JSON). The summary provides title, panelCount, panels with
    # id/title/type/description/queryCount, variables, and timeRange — everything
    # needed for display — at ~1% of the payload cost of the full dashboard JSON.
    result_str = await _mcp_call(
        "get_dashboard_summary", {"uid": uid.strip()}, raw=True
    )

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

    # Optional header: variables and default time range
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
    return _cap("\n".join(lines))


# ---------------------------------------------------------------------------
# PUBLIC WRAPPER: query_prometheus_metric
# ---------------------------------------------------------------------------

async def query_prometheus_metric(
    metric: str = "",
    expr: str = "",
    lookback: str = "1h",
    step_seconds: int = 60,
) -> str:
    """Query live metrics. Use metric= for natural language or expr= for explicit PromQL."""
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

        result = await _score_and_select(keyword)

        if result is None:
            index = await _ensure_panel_index()
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
            # Ambiguity — return clarification request to the LLM
            return result

        final_expr = result["query"]
        ds_ref = result.get("datasource")
        discovery_context = (
            f"Discovered from panel '{result['panel_title']}' "
            f"in dashboard '{result['dashboard_title']}'"
        )
        logger.info(f"  Selected query: {final_expr[:120]}")

    result_str = await _execute_query(final_expr, ds_ref, lookback, step_seconds)

    if result_str.startswith("Error"):
        elapsed = time.perf_counter() - start
        logger.info(f"Wrapper query_prometheus_metric failed in {elapsed:.3f}s")
        return result_str

    try:
        data = json.loads(result_str)
        if "data" in data and "result" in data["data"]:
            # Extract MCP-level hints for empty-result diagnostics.
            # query_prometheus returns {data: {result: [...]}, hints: {...}, warnings: [...]}
            hints = data.get("hints") if isinstance(data.get("hints"), dict) else None
            normalized = _format_prometheus_result(
                data["data"]["result"], discovery_context, hints
            )
        else:
            normalized = _normalize_to_text(result_str)
    except json.JSONDecodeError:
        normalized = result_str

    elapsed = time.perf_counter() - start
    logger.info(f"Wrapper query_prometheus_metric done in {elapsed:.3f}s")
    return _cap(normalized)