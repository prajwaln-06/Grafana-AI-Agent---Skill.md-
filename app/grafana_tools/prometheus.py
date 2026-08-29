import json
import re
from typing import Optional

from app.mcp.session import mcp_call
from .utils import logger

def preprocess_promql(expr: str, lookback: str) -> str:
    """Transform Grafana dashboard expressions into valid, executable PromQL."""
    original = expr
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

async def execute_prometheus(expr: str, ds_uid: str, lookback: str, step_seconds: int) -> str:
    """Execute a PromQL range query via the MCP query_prometheus tool."""
    preprocessed_expr = preprocess_promql(expr, lookback)
    return await mcp_call(
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

async def query_prometheus_histogram(expr: str, ds_uid: str, lookback: str) -> str:
    """Execute a PromQL histogram query."""
    preprocessed_expr = preprocess_promql(expr, lookback)
    return await mcp_call(
        "query_prometheus_histogram",
        {
            "datasourceUid": ds_uid,
            "expr": preprocessed_expr,
        }
    )

async def list_prometheus_metric_names(ds_uid: str) -> str:
    """List all available metric names using Grafana MCP pagination."""
    limit = 100
    page = 1
    metrics: list[str] = []
    while True:
        raw = await mcp_call(
            "list_prometheus_metric_names",
            {"datasourceUid": ds_uid, "limit": limit, "page": page},
        )
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw
        items = payload
        if isinstance(payload, dict):
            items = next(
                (payload[key] for key in ("metrics", "metricNames", "result", "data") if isinstance(payload.get(key), list)),
                [],
            )
        if not isinstance(items, list):
            return raw
        metrics.extend(str(item) for item in items if isinstance(item, str))
        if len(items) < limit:
            break
        page += 1
    return json.dumps(metrics)

async def list_prometheus_metric_metadata(ds_uid: str, metric: str) -> str:
    """List metadata for a specific metric."""
    return await mcp_call("list_prometheus_metric_metadata", {"datasourceUid": ds_uid, "metric": metric})

async def list_prometheus_label_names(ds_uid: str) -> str:
    """List all available label names."""
    return await mcp_call("list_prometheus_label_names", {"datasourceUid": ds_uid})

async def list_prometheus_label_values(ds_uid: str, label: str) -> str:
    """List all values for a specific label."""
    return await mcp_call("list_prometheus_label_values", {"datasourceUid": ds_uid, "labelName": label})

def format_prometheus_result(results: list[dict], context: str = "", hints: Optional[dict] = None) -> str:
    """Human-friendly summary of Prometheus range-query results."""
    if not results:
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
