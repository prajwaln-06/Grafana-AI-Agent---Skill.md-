"""Dashboard proposal, approval, compilation, and MCP mutation boundary."""
from __future__ import annotations

import copy
import asyncio
import hashlib
import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Any, Literal

from app.dashboard.intent import Intent, resolve_intent
from app.dashboard.results import (
    CATEGORICAL, DOCUMENTS, GEOGRAPHIC, GRAPH, HEATMAP, HISTOGRAM, SCALAR,
    TIME_SERIES, VISUALIZATION_RESULT_TYPES, compatibility_error, empty_result,
)

from .datasource import resolve_datasource
from .dashboard import fetch_dashboard_detail, fetch_dashboard_list, flatten_panels
from .promql import build_promql, infer_unit, TARGET_LABEL_CANDIDATES
from .prometheus import (
    execute_prometheus,
    list_prometheus_label_names,
    list_prometheus_label_values,
    list_prometheus_metric_metadata,
    list_prometheus_metric_names,
)
from .opensearch import build_opensearch_query, discover_fields, execute_opensearch, normalize_opensearch_result
from app.mcp.session import mcp_call, run_sync
from .utils import logger

SUPPORTED_VISUALIZATIONS = set(VISUALIZATION_RESULT_TYPES)
DATASOURCE_VISUALIZATIONS = {
    "prometheus": SUPPORTED_VISUALIZATIONS - {"logs"},
    "opensearch": set(SUPPORTED_VISUALIZATIONS),
    "elasticsearch": set(SUPPORTED_VISUALIZATIONS),
}
TIME_RANGES = {"15m", "1h", "6h", "12h", "24h", "7d"}

OPENSEARCH_REQUESTS = (
    {
        "name": "ssh failures", "patterns": ("ssh", "authentication failure", "failed authentication"),
        "index": "syslog-*", "query": 'Body:("failed authentication" OR "authentication failure")',
        "title": "SSH Authentication Failures", "visualization": "logs",
    },
    {
        "name": "heartbeat events", "patterns": ("heartbeat",), "index": "heartbeat", "query": "*",
        "title": "Heartbeat Events", "visualization": "table",
    },
    {
        "name": "application logs", "patterns": ("application log", "console log", "recent logs"),
        "index": "consolelog-*", "query": "*", "title": "Application Logs", "visualization": "logs",
    },
    {
        "name": "errors", "patterns": ("error", "errors"), "index": "consolelog-*", "query": "Body:error",
        "title": "Errors", "visualization": "logs",
    },
)


def _visualization_name(value: str) -> str:
    normalized = value.lower().replace(" ", "")
    return {"bar": "barchart", "pie": "piechart", "log": "logs", "nodegraph": "nodegraph"}.get(normalized, normalized)


def _requested_result_type(visualization: str, datasource: str = "prometheus") -> str:
    # Stable defaults for multi-shape visualizations; datasource determines the natural table shape.
    defaults = {
        "table": DOCUMENTS if datasource in {"opensearch", "elasticsearch"} else TIME_SERIES,
    }
    if visualization in defaults:
        return defaults[visualization]
    return {
        "logs": DOCUMENTS, "stat": SCALAR, "gauge": SCALAR, "barchart": CATEGORICAL,
        "piechart": CATEGORICAL, "timeseries": TIME_SERIES, "histogram": HISTOGRAM,
        "heatmap": HEATMAP, "geomap": GEOGRAPHIC, "nodegraph": GRAPH,
    }[visualization]


def _requested_opensearch_panels(text: str) -> list[dict[str, Any]]:
    lower = text.lower()
    visualization_pattern = re.compile(r"\b(logs?|table|stat|gauge|bar(?:\s*chart)?|time\s*series|timeseries|pie(?:\s*chart)?|histogram|heatmap|geomap|node\s*graph)\b")
    requested = []
    occupied = []
    for definition in OPENSEARCH_REQUESTS:
        if definition["name"] == "errors" and requested:
            continue
        positions = [(lower.find(pattern), pattern) for pattern in definition["patterns"] if lower.find(pattern) >= 0]
        if not positions:
            continue
        position, pattern = min(positions)
        span = (position, position + len(pattern))
        if any(span[0] < end and start < span[1] for start, end in occupied):
            continue
        clause_start = max(lower.rfind(",", 0, position), lower.rfind(";", 0, position), lower.rfind(" and ", 0, position)) + 1
        boundaries = [p for p in (lower.find(",", span[1]), lower.find(";", span[1]), lower.find(" and ", span[1])) if p >= 0]
        clause = lower[clause_start:min(boundaries) if boundaries else len(lower)]
        viz_match = visualization_pattern.search(clause)
        visualization = _visualization_name(viz_match.group(1)) if viz_match else definition["visualization"]
        result_type = _requested_result_type(visualization, "opensearch")
        requested.append({**definition, "position": position, "visualization": visualization, "resultType": result_type, "request": text})
        occupied.append(span)
    if not requested and re.search(r"\b(logs?|documents?|events?|opensearch|index)\b", lower):
        viz_match = visualization_pattern.search(lower)
        visualization = _visualization_name(viz_match.group(1)) if viz_match else "logs"
        index_match = re.search(r"\bindex\s+['\"]?([A-Za-z0-9_.*-]+)", text, re.I)
        requested.append({
            "name": "OpenSearch data", "patterns": (), "index": index_match.group(1) if index_match else "*",
            "query": "*", "title": "OpenSearch Data", "visualization": visualization,
            "resultType": _requested_result_type(visualization, "opensearch"), "position": 0, "request": text,
        })
    return sorted(requested, key=lambda item: item["position"])


def classify_dashboard_panels(text: str) -> list[dict[str, Any]]:
    """Classify requested panels deterministically without an extra model call.

    Only OpenSearch panels are pre-classified from text patterns.
    Prometheus panels require the live schema (discover_prometheus_schema) and
    are resolved at build time via _explicit_metric_panels or clarification.
    """
    panels = [{**panel, "datasourceType": "opensearch"} for panel in _requested_opensearch_panels(text)]
    return sorted(panels, key=lambda item: item["position"])


def _json(value: str) -> Any:
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None


def _require_mcp_content(value: Any, datasource: str) -> Any:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise RuntimeError(f"{datasource} MCP returned empty content.")
    if isinstance(value, str) and value.lstrip().lower().startswith("error:"):
        raise RuntimeError(f"{datasource} MCP failed: {value.split(':', 1)[1].strip()}")
    return value


def _unwrap_list(value: Any, keys: tuple[str, ...]) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in keys:
            if isinstance(value.get(key), list):
                return value[key]
        data = value.get("data")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return _unwrap_list(data, keys)
    return []


async def discover_prometheus_schema() -> dict:
    ds = await resolve_datasource("prometheus")
    if not ds:
        raise ValueError("No Prometheus datasource is available through Grafana MCP.")
    names = _unwrap_list(
        _json(await list_prometheus_metric_names(ds["uid"])),
        ("metrics", "metricNames", "result"),
    )
    names = sorted({str(item) for item in names if isinstance(item, str)})
    label_names = _unwrap_list(
        _json(await list_prometheus_label_names(ds["uid"])),
        ("labels", "labelNames", "result"),
    )
    target_label = next((label for label in TARGET_LABEL_CANDIDATES if label in label_names), None)
    if not target_label:
        raise ValueError("Prometheus exposes no supported target label.")
    targets = _unwrap_list(
        _json(await list_prometheus_label_values(ds["uid"], target_label)),
        ("values", "labelValues", "result"),
    )
    return {
        "datasource": ds,
        "metrics": names,
        "label_names": label_names,
        "target_label": target_label,
        "targets": sorted({str(item) for item in targets if isinstance(item, (str, int))}),
    }



def classify_dashboard_panels(text: str) -> list[dict[str, Any]]:
    """Classify requested panels deterministically without an extra model call.

    Only OpenSearch panels are pre-classified from text patterns.
    Prometheus panels require the live schema (discover_prometheus_schema) and
    are resolved at build time via _explicit_metric_panels or clarification.
    """
    panels = [{**panel, "datasourceType": "opensearch"} for panel in _requested_opensearch_panels(text)]
    return sorted(panels, key=lambda item: item["position"])


def _suggest_metrics(term: str, discovered: list[str]) -> list[str]:
    """Return discovered metrics that contain any significant token from term."""
    tokens = {t for t in re.split(r'[^a-z0-9]+', term.lower()) if len(t) > 2}
    return sorted(
        {m for m in discovered if any(tok in m.lower() for tok in tokens)},
        key=len,
    )[:15]

def _explicit_metric_panels(text: str, discovered_metrics: list[str]) -> list[dict[str, Any]]:
    """Recognize only concrete metric names that discovery proved exist."""
    matches = []
    for metric in discovered_metrics:
        match = re.search(rf"(?<![A-Za-z0-9_:]){re.escape(metric)}(?![A-Za-z0-9_:])", text)
        if match:
            matches.append({"metric": metric, "position": match.start()})
    if len(matches) > 1:
        raise ValueError("Multiple concrete metrics were identified; specify the panels and visualizations you want for each metric.")
    if not matches:
        return []
    visualization_match = re.search(r"\b(time\s*series|timeseries|stat|gauge|bar\s*chart|barchart|table|pie\s*chart|piechart|histogram|heatmap|geomap|node\s*graph|nodegraph|logs?)\b", text, re.I)
    visualization = _visualization_name(visualization_match.group(1)) if visualization_match else "timeseries"
    return [{
        "measurement": None,
        "metric": matches[0]["metric"],
        "visualization": visualization,
        "existingVisualization": "",
        "allowEquivalent": bool(re.search(r"\b(another|additional|extra|second)\b", text, re.I)),
        "position": matches[0]["position"],
    }]


def _panel_matches_metric(panel: dict, metric: str) -> bool:
    searchable = " ".join((str(panel.get("title", "")), str(panel.get("metric", "")), str(panel.get("query", "")))).lower()
    return metric.lower() in searchable


def _metric_identities_from_query(query: str) -> list[str]:
    """Extract canonical metric selectors without treating full PromQL as metric identity."""
    value = str(query or "").strip()
    metrics = re.findall(r"([A-Za-z_:][A-Za-z0-9_:]*)\s*(?=\{|\[)", value)
    if not metrics:
        sanitized = re.sub(r'"(?:\\.|[^"\\])*"', " ", value)
        sanitized = re.sub(r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)", " ", sanitized)
        ignored = {
            "and", "bool", "by", "group_left", "group_right", "ignoring", "offset",
            "on", "or", "unless", "without",
        }
        for match in re.finditer(r"\b[A-Za-z_:][A-Za-z0-9_:]*\b", sanitized):
            token = match.group(0)
            remainder = sanitized[match.end():].lstrip()
            if token.lower() in ignored or remainder.startswith("("):
                continue
            metrics.append(token)
    return list(dict.fromkeys(metrics))


def _target_from_query(query: str) -> dict:
    for label in TARGET_LABEL_CANDIDATES:
        match = re.search(rf'\b{re.escape(label)}\s*=\s*"([^"]+)"', str(query or ""))
        if match:
            return {"label": label, "value": match.group(1)}
    return {}


def _panels_equivalent(left: dict, right: dict) -> bool:
    fields = ("metric", "index", "query", "visualizationType", "visualizationConfig", "target", "datasource")
    return all(left.get(field) == right.get(field) for field in fields)


def _metadata_type(metadata: Any, metric: str) -> str:
    if not isinstance(metadata, dict):
        return ""
    entry = metadata.get(metric, metadata)
    if isinstance(entry, list) and entry:
        entry = entry[0]
    return str(entry.get("type", "")).lower() if isinstance(entry, dict) else ""


def _promql_for_shape(metric: str, matcher: str, kind: str, dimension: str, metric_type: str) -> str:
    selector = f'{metric}{{{matcher}}}'
    if kind in {HISTOGRAM, HEATMAP}:
        if not metric.endswith("_bucket"):
            raise ValueError("Histogram and Heatmap require a discovered Prometheus histogram _bucket metric.")
        return f'sum by (le) (rate({selector}[5m]))'
    value = f'rate({selector}[5m])' if metric_type == "counter" or metric.endswith("_total") else selector
    if kind == CATEGORICAL:
        return f"sum by ({dimension}) ({value})"
    return value


def _validate_requested_panels(requested: list[dict[str, str]], panels: list[dict]) -> None:
    if len(panels) != len(requested):
        raise ValueError(f"Proposal integrity failure: requested {len(requested)} panels but generated {len(panels)}.")
    for index, (specification, panel) in enumerate(zip(requested, panels), 1):
        requested_visualization = specification.get("visualization")
        if requested_visualization and panel.get("visualizationType") != requested_visualization:
            raise ValueError(f"Proposal integrity failure: panel {index} requested {requested_visualization} but generated {panel.get('visualizationType')}.")
        if specification.get("metric") and not _panel_matches_metric(panel, specification["metric"]):
            raise ValueError(f"Proposal integrity failure: panel {index} does not match requested metric '{specification['metric']}'.")


def classify_intent(text: str) -> str:
    """Compatibility helper returning normalized uppercase intent names."""
    return resolve_intent(text).intent.value



def _normalize_query_result(raw: str) -> dict:
    data = _json(raw)
    if not isinstance(data, (dict, list)):
        return {"status": "error", "series": [], "error": raw}
    if isinstance(data, list):
        results = data
    else:
        payload = data.get("data", data.get("result", []))
        results = payload.get("result", []) if isinstance(payload, dict) else payload
    series = []
    for item in results if isinstance(results, list) else []:
        if not isinstance(item, dict):
            continue
        points = item.get("values", [])
        if not points and item.get("value"):
            points = [item["value"]]
        normalized = []
        for point in points:
            try:
                normalized.append({"timestamp": float(point[0]), "value": float(point[1])})
            except (TypeError, ValueError, IndexError):
                continue
        series.append({"labels": item.get("metric", item.get("labels", {})), "points": normalized})
    return {"status": "success" if series else "empty_result", "type": TIME_SERIES, "series": series, "seriesCount": len(series)}


def _latest_points(series_result: dict) -> list[tuple[dict, dict]]:
    latest = []
    for item in series_result.get("series", []):
        points = item.get("points", [])
        if points:
            latest.append((item.get("labels", {}), points[-1]))
    return latest


def _normalize_prometheus_shape(raw: str, kind: str, *, dimension: str = "", source_metric: str = "") -> dict:
    time_series = _normalize_query_result(raw)
    latest = _latest_points(time_series)
    if kind == TIME_SERIES:
        return time_series
    if kind == SCALAR:
        value = sum(point["value"] for _, point in latest) if latest else None
        return {"status": "success" if value is not None else "empty_result", "type": SCALAR, "value": value, "timestamp": max((p["timestamp"] for _, p in latest), default=None)}
    if kind == CATEGORICAL:
        buckets = [{"key": str(labels.get(dimension, "")), "value": point["value"]} for labels, point in latest if labels.get(dimension) is not None]
        return {"status": "success" if buckets else "empty_result", "type": CATEGORICAL, "dimension": dimension, "buckets": buckets}
    if kind == HISTOGRAM:
        cumulative = sorted(
            [(float(labels["le"]) if labels.get("le") not in {None, "+Inf"} else float("inf"), point["value"]) for labels, point in latest if "le" in labels],
            key=lambda item: item[0],
        )
        buckets, previous = [], 0.0
        for upper, count in cumulative:
            buckets.append({"lower": None if not buckets else buckets[-1]["upper"], "upper": upper, "count": max(0.0, count - previous)})
            previous = count
        return {"status": "success" if buckets else "empty_result", "type": HISTOGRAM, "metric": source_metric, "cumulative": False, "buckets": buckets}
    if kind == HEATMAP:
        cells, x_values, y_values, cumulative = [], set(), set(), {}
        for item in time_series.get("series", []):
            boundary = item.get("labels", {}).get("le")
            if boundary is None:
                continue
            numeric_boundary = float("inf") if boundary == "+Inf" else float(boundary)
            y_values.add(boundary)
            for point in item.get("points", []):
                x_values.add(point["timestamp"])
                cumulative.setdefault(point["timestamp"], []).append((numeric_boundary, boundary, point["value"]))
        for timestamp, buckets in cumulative.items():
            previous = 0.0
            for _, boundary, count in sorted(buckets):
                cells.append({"x": timestamp, "y": boundary, "value": max(0.0, count - previous)})
                previous = count
        return {"status": "success" if cells else "empty_result", "type": HEATMAP, "xBuckets": sorted(x_values), "yBuckets": sorted(y_values, key=lambda v: float("inf") if v == "+Inf" else float(v)), "cells": cells}
    if kind == GEOGRAPHIC:
        points = []
        for labels, point in latest:
            try:
                latitude = float(labels.get("latitude", labels.get("lat")))
                longitude = float(labels.get("longitude", labels.get("lon")))
            except (TypeError, ValueError):
                continue
            points.append({"latitude": latitude, "longitude": longitude, "value": point["value"], "labels": labels})
        return {"status": "success" if points else "empty_result", "type": GEOGRAPHIC, "points": points}
    if kind == GRAPH:
        nodes, edges, seen = [], [], set()
        for index, (labels, point) in enumerate(latest):
            source = labels.get("source") or labels.get("src")
            target = labels.get("target") or labels.get("dst")
            if not source or not target:
                continue
            for node in (source, target):
                if node not in seen:
                    seen.add(node); nodes.append({"id": str(node), "title": str(node)})
            edges.append({"id": f"edge-{index + 1}", "source": str(source), "target": str(target), "value": point["value"]})
        return {"status": "success" if edges else "empty_result", "type": GRAPH, "nodes": nodes, "edges": edges}
    return empty_result(kind)


async def _resolve_prometheus_panels(
    requested_panels: list[dict[str, Any]], request: str, target: str | None, time_range: str
) -> tuple[list[dict], dict]:
    schema = await discover_prometheus_schema()
    concrete = _explicit_metric_panels(request, schema["metrics"]) if not requested_panels else []
    requested_panels = requested_panels or concrete
    if not requested_panels:
        return [], schema
    supplied_target = str(target or "").strip()
    panel_tokens = {
        "logs", "table", "stat", "gauge", "bar", "barchart", "time_series", "timeseries",
        "pie", "piechart", "histogram", "heatmap", "geomap", "nodegraph",
    }
    supplied_parts = {part.strip().lower().replace(" ", "_") for part in supplied_target.split(",") if part.strip()}
    if supplied_parts and supplied_parts <= panel_tokens:
        supplied_target = ""
    if supplied_target and supplied_target not in schema["targets"]:
        target_like = bool(re.match(r"^(?:node|host|server|instance)[_.:-]?[A-Za-z0-9_.:-]+$", supplied_target, re.I))
        explicit_in_request = bool(re.search(rf"(?<![A-Za-z0-9_-]){re.escape(supplied_target)}(?![A-Za-z0-9_-])", request, re.I))
        if not (target_like and explicit_in_request):
            supplied_target = ""
    requested_target = supplied_target or next((t for t in schema["targets"] if t.lower() in request.lower()), None)
    if not requested_target:
        # CREATE requests without an explicit host still need a usable proposal.
        # Use deterministic discovery order; explicit invalid targets remain errors.
        requested_target = schema["targets"][0] if schema["targets"] else None
    if not requested_target:
        raise ValueError(f"Select a target from: {', '.join(schema['targets'])}.")
    if requested_target not in schema["targets"]:
        raise ValueError(f"Target '{requested_target}' was not discovered. Available: {', '.join(schema['targets'])}.")

    async def resolve_one(requested_panel: dict[str, Any]) -> dict:
        metric = requested_panel["metric"]
        if metric not in schema["metrics"]:
            raise ValueError(f"Metric '{metric}' is not available in this Prometheus instance.")
        visualization = requested_panel.get("visualization") or "timeseries"
        if visualization == "logs":
            raise ValueError("Prometheus cannot provide document results required by Logs.")
        kind = _requested_result_type(visualization, "prometheus")
        dimension = requested_panel.get("dimension") or ""
        if kind == CATEGORICAL:
            candidates = [label for label in schema["label_names"] if label != schema["target_label"]]
            dimension = dimension or (candidates[0] if candidates else "")
            if not dimension or dimension not in schema["label_names"]:
                raise ValueError("Bar Chart and Pie Chart require a discovered categorical Prometheus label; specify 'by <label>'.")
        if kind in {HISTOGRAM, HEATMAP} and not metric.endswith("_bucket"):
            bucket_metric = f"{metric}_bucket"
            if bucket_metric in schema["metrics"]:
                metric = bucket_metric
            else:
                raise ValueError("Histogram and Heatmap require a discovered Prometheus histogram _bucket metric.")
        if kind == GEOGRAPHIC and not ({"latitude", "longitude"} <= set(schema["label_names"]) or {"lat", "lon"} <= set(schema["label_names"])):
            raise ValueError("Geomap requires discovered latitude/longitude Prometheus labels.")
        if kind == GRAPH and not ({"source", "target"} <= set(schema["label_names"]) or {"src", "dst"} <= set(schema["label_names"])):
            raise ValueError("Node Graph requires discovered source/target Prometheus labels.")
        metadata_raw = await list_prometheus_metric_metadata(schema["datasource"]["uid"], metric)
        metadata = _json(metadata_raw)
        metric_type = _metadata_type(metadata, metric)
        matcher = f'{schema["target_label"]}="{requested_target}"'
        query = (
            build_promql(metric, schema["target_label"], requested_target, metric_type=metric_type)
            if kind not in {CATEGORICAL, HISTOGRAM, HEATMAP}
            else _promql_for_shape(metric, matcher, kind, dimension, metric_type)
        )
        result_raw = _require_mcp_content(
            await execute_prometheus(query, schema["datasource"]["uid"], time_range, 60),
            "Prometheus",
        )
        query_result = _normalize_prometheus_shape(result_raw, kind, dimension=dimension, source_metric=metric)
        title = metric.replace("_", " ").title()
        config = {"unit": infer_unit(metric)}
        return {
            "title": title, "metric": metric, "sourceMetrics": [metric],
            "metricMetadata": metadata, "metricSeries": [item["labels"] for item in _normalize_query_result(result_raw)["series"][:5]],
            "datasource": schema["datasource"], "query": query, "queryResult": query_result,
            "target": {"label": schema["target_label"], "value": requested_target},
            "visualizationType": visualization, "resultType": kind,
            "visualizationConfig": config, "variableRefs": [],
        }
    return list(await asyncio.gather(*(resolve_one(panel) for panel in requested_panels))), schema


async def _resolve_opensearch_panels(requested_panels: list[dict[str, Any]], time_range: str) -> tuple[list[dict], dict]:
    datasource = await resolve_datasource("opensearch", allow_generic_fallback=False)
    if not datasource:
        raise ValueError("No OpenSearch datasource is available through Grafana MCP.")

    async def resolve_one(specification: dict[str, Any]) -> dict:
        discovery_raw = _require_mcp_content(
            await execute_opensearch("*", datasource["uid"], specification["index"], f"now-{time_range}", "now", 100),
            "OpenSearch",
        )
        discovery_data = _json(discovery_raw)
        schema = discover_fields(discovery_data if discovery_data is not None else discovery_raw)
        kind = specification["resultType"]
        time_field = (schema["timestampFields"] or ["@timestamp"])[0]
        dimension_match = re.search(r"\bby\s+([A-Za-z_][A-Za-z0-9_.-]*)", specification.get("request", ""), re.I)
        dimension = dimension_match.group(1) if dimension_match else next((f for f in schema["categoricalFields"] if f not in {"Body", "body", "message"}), "")
        value_field = schema["numericFields"][0] if schema["numericFields"] else ""
        if kind == CATEGORICAL and not dimension:
            raise ValueError("Bar Chart and Pie Chart require a discovered categorical OpenSearch field.")
        if kind in {HISTOGRAM, HEATMAP} and not value_field:
            raise ValueError("Histogram and Heatmap require a discovered numeric OpenSearch field.")
        if kind == GEOGRAPHIC and not (schema["latitudeField"] and schema["longitudeField"]):
            raise ValueError("Geomap requires discovered latitude and longitude OpenSearch fields.")
        if kind == GRAPH and not (schema["sourceField"] and schema["targetField"]):
            raise ValueError("Node Graph requires discovered source and target OpenSearch fields.")
        field_config = {
            "dimension": dimension, "valueField": value_field, "interval": 10,
            "latitudeField": schema["latitudeField"], "longitudeField": schema["longitudeField"],
            "sourceField": schema["sourceField"], "targetField": schema["targetField"],
        }
        query = specification["query"] if kind in {DOCUMENTS, GEOGRAPHIC, GRAPH} else build_opensearch_query(specification["query"], kind, time_field=time_field, dimension=dimension, value_field=value_field, y_field=value_field)
        if datasource.get("type", "").lower() in {"opensearch", "grafana-opensearch-datasource"} and kind not in {DOCUMENTS, GEOGRAPHIC, GRAPH}:
            raise ValueError("Installed Grafana MCP exposes Lucene document queries for OpenSearch but not Query DSL aggregations; requested aggregation visualization is unsupported until MCP adds this capability.")
        raw = _require_mcp_content(
            await execute_opensearch(query, datasource["uid"], specification["index"], f"now-{time_range}", "now", 100),
            "OpenSearch",
        )
        parsed = _json(raw)
        normalized = normalize_opensearch_result(
            parsed if parsed is not None else raw, datasource, query, specification["index"], f"now-{time_range}", "now", kind, field_config
        )
        return {
            "title": specification["title"], "metric": "", "sourceMetrics": [], "metricMetadata": {}, "metricSeries": [],
            "datasource": datasource, "index": specification["index"], "query": query,
            "timeField": time_field, "queryResult": normalized, "resultType": kind,
            "relevantFields": [field["name"] for field in schema["fields"]], "fieldDiscovery": {"mode": schema["discoveryMode"], "mappingDiscovery": schema["mappingDiscovery"], "limitation": schema["limitation"]}, "fieldConfig": field_config,
            "target": {}, "visualizationType": specification["visualization"], "visualizationConfig": {"unit": "short"}, "variableRefs": [],
        }
    return list(await asyncio.gather(*(resolve_one(panel) for panel in requested_panels))), datasource


def _datasource_error(datasource: str, exc: BaseException) -> dict[str, str]:
    return {
        "datasource": datasource,
        "type": type(exc).__name__,
        "message": str(exc) or "Datasource resolution failed without an error message.",
    }


async def _resolve_datasource_groups(
    groups: list[tuple[str, Any]],
) -> tuple[dict[str, tuple[list[dict], dict]], list[dict[str, str]]]:
    """Resolve independent datasource groups concurrently without losing partial success."""
    if not groups:
        return {}, []
    async def timed(datasource: str, work: Any) -> Any:
        started = time.perf_counter()
        try:
            return await work
        finally:
            logger.info(
                "[dashboard] datasource_resolution datasource=%s duration_ms=%.1f",
                datasource,
                (time.perf_counter() - started) * 1000,
            )

    values = await asyncio.gather(
        *(timed(datasource, work) for datasource, work in groups),
        return_exceptions=True,
    )
    resolved: dict[str, tuple[list[dict], dict]] = {}
    errors: list[dict[str, str]] = []
    for (datasource, _), value in zip(groups, values):
        if isinstance(value, BaseException):
            errors.append(_datasource_error(datasource, value))
        else:
            resolved[datasource] = value
    return resolved, errors


async def build_proposal(request: str, target: str | None = None, time_range: str = "1h") -> dict:
    resolution = resolve_intent(request)
    if resolution.intent == Intent.UNSPECIFIED:
        raise ValueError(f"Clarification required: {resolution.reason}")
    if resolution.intent == Intent.READ:
        raise ValueError("READ requests use query_prometheus_metric and never create a dashboard proposal.")
    if resolution.intent == Intent.UPDATE:
        return await _build_update_proposal(request, target, time_range)
    if resolution.intent == Intent.REMOVE:
        return await _build_remove_proposal(request)
    if resolution.intent != Intent.CREATE:
        raise ValueError(f"Unsupported dashboard intent: {resolution.intent.value}")
    if time_range not in TIME_RANGES:
        raise ValueError(f"Unsupported time range. Choose one of: {', '.join(sorted(TIME_RANGES))}.")
    dashboard_name = _create_dashboard_name(request)
    classified = classify_dashboard_panels(request)
    prometheus_requested = [panel for panel in classified if panel["datasourceType"] == "prometheus"]
    opensearch_requested = [panel for panel in classified if panel["datasourceType"] == "opensearch"]
    if not classified:
        schema = await discover_prometheus_schema()
        prometheus_requested = _explicit_metric_panels(request, schema["metrics"])
        if not prometheus_requested:
            suggestions = _suggest_metrics(request, schema["metrics"])
            hint = (
                f" Metrics matching your request: {', '.join(suggestions)}."
                if suggestions else ""
            )
            raise ValueError(
                f"Clarification required: no exact Prometheus metric name was found in your request.{hint} "
                f"Please use the metric name as it appears in your Prometheus instance "
                f"(e.g. 'node_cpu_seconds_total', 'http_requests_total')."
            )
    groups = []
    if prometheus_requested:
        groups.append(("prometheus", _resolve_prometheus_panels(prometheus_requested, request, target, time_range)))
    if opensearch_requested:
        groups.append(("opensearch", _resolve_opensearch_panels(opensearch_requested, time_range)))
    by_type, resolution_errors = await _resolve_datasource_groups(groups)
    if not by_type:
        detail = "; ".join(f"{item['datasource']}: {item['message']}" for item in resolution_errors)
        raise RuntimeError(detail or "No datasource produced a dashboard panel.")
    queues = {kind: list(value[0]) for kind, value in by_type.items()}
    panels = [
        queues[item["datasourceType"]].pop(0)
        for item in classified
        if item["datasourceType"] in queues and queues[item["datasourceType"]]
    ] if classified else by_type["prometheus"][0]
    for index, panel in enumerate(panels):
        panel.update({"id": f"panel-{index + 1}", "layout": {"x": (index % 2) * 12, "y": (index // 2) * 8}, "size": {"w": 12, "h": 8}})
    primary_datasource = panels[0]["datasource"]
    targets = by_type.get("prometheus", ({}, {}))[1].get("targets", []) if "prometheus" in by_type else []
    ir = {
        "operation": "create",
        "name": dashboard_name,
        "description": "Unified Prometheus and OpenSearch monitoring dashboard",
        "datasource": primary_datasource,
        "availableTargets": targets,
        "variables": [],
        "timeConfig": {"from": f"now-{time_range}", "to": "now"},
        "panels": panels,
    }
    if resolution_errors:
        ir["resolutionStatus"] = "partial"
        ir["resolutionErrors"] = resolution_errors
    if prometheus_requested and "prometheus" in by_type:
        _validate_requested_panels(prometheus_requested, [p for p in ir["panels"] if p["datasource"]["type"] == "prometheus"])
    return PROPOSALS.create(ir)


def _clean_dashboard_name(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip(" \t\r\n.?!'\"")).strip()


def _create_dashboard_name(request: str) -> str:
    """Return only an explicit CREATE name; never infer an existing-dashboard identity."""
    quoted = re.search(r"\bdashboard\s+(?:called|named)\s+['\"]([^'\"]+)['\"]", request, re.I)
    if quoted:
        return _clean_dashboard_name(quoted.group(1))
    named = re.search(r"\bdashboard\s+(?:called|named)\s+(.+?)(?=\s+(?:for|with)\b|[.!?]|$)", request, re.I)
    if named:
        return _clean_dashboard_name(named.group(1))
    return "Observability Dashboard"


def _dashboard_identity(request: str, intent: Intent) -> str:
    quoted = re.search(r"\bdashboard\s+(?:called|named)\s+['\"]([^'\"]+)['\"]", request, re.I)
    if quoted:
        return _clean_dashboard_name(quoted.group(1))
    named = re.search(r"\bdashboard\s+(?:called|named)\s+(.+?)(?=\s+(?:for|with)\b|[.!?]|$)", request, re.I)
    if named:
        return _clean_dashboard_name(named.group(1))
    possessive = re.search(r"\b(?:my|the)\s+(.+?)\s+dashboard\b", request, re.I)
    if possessive:
        return _clean_dashboard_name(possessive.group(1))
    dashboard_reference = re.search(
        r"\bdashboard\s+['\"]?(.+?)['\"]?(?=\s+to\s+(?:a|an|the)?\s*(?:gauge|stat|time\s*series|timeseries|bar\s*chart|table)\b|[.!?]|$)",
        request,
        re.I,
    )
    if dashboard_reference:
        name = _clean_dashboard_name(dashboard_reference.group(1))
        if name and name.lower() not in {"this", "the", "my", "existing"}:
            return name
    destination = re.search(
        r"\b(?:to|in|on)\s+(?:my|the)?\s*['\"]?(.+?)['\"]?(?=\s+to\s+(?:a|an|the)?\s*(?:gauge|stat|time\s*series|timeseries|bar\s*chart|table)\b|[.!?]|$)",
        request,
        re.I,
    )
    if destination:
        return _clean_dashboard_name(destination.group(1))
    raise ValueError("Identify the dashboard by name or UID before continuing.")


def _normalized_dashboard_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


async def _find_dashboards(name: str) -> list[dict]:
    dashboards = await fetch_dashboard_list(refresh=True)
    wanted = _normalized_dashboard_name(name)
    exact = [item for item in dashboards if _normalized_dashboard_name(str(item.get("title", ""))) == wanted]
    if exact:
        return exact
    return [
        item for item in dashboards
        if wanted and (
            wanted in _normalized_dashboard_name(str(item.get("title", "")))
            or _normalized_dashboard_name(str(item.get("title", ""))) in wanted
        )
    ]


async def _resolve_dashboard(request: str, operation: str = "update") -> tuple[str, dict]:
    uid_match = re.search(r"\buid\s+['\"]?([A-Za-z0-9_.:-]+)['\"]?", request, re.I)
    if uid_match:
        uid = uid_match.group(1)
    else:
        name = _dashboard_identity(request, Intent.UPDATE)
        candidates = await _find_dashboards(name)
        if not candidates:
            raise ValueError(f"Dashboard '{name}' was not found. Identify an existing dashboard before {operation}.")
        if len(candidates) > 1:
            raise ValueError(f"Multiple dashboards match '{name}'. Identify the intended dashboard by UID or exact title.")
        uid = str(candidates[0]["uid"])
    detail = await fetch_dashboard_detail(uid, refresh=True)
    if not detail:
        raise ValueError(f"Dashboard '{uid}' was not found through Grafana MCP.")
    return uid, detail.get("dashboard", detail)


def _hydrate_dashboard(uid: str, raw: dict) -> dict:
    datasource = raw.get("datasource") or {"type": "prometheus", "uid": ""}
    panels = []
    for index, panel in enumerate(flatten_panels(raw.get("panels", []))):
        targets = panel.get("targets") or []
        target = targets[0] if targets else {}
        panel_datasource = target.get("datasource", panel.get("datasource", datasource))
        datasource_type = str(panel_datasource.get("type", datasource.get("type", "prometheus"))).lower()
        is_opensearch = datasource_type in {"opensearch", "elasticsearch", "grafana-opensearch-datasource"}
        query_value = target.get("query", "") if is_opensearch else target.get("expr", "")
        if isinstance(query_value, dict):
            query_value = json.dumps(query_value, sort_keys=True)
        query = str(query_value)
        source_metrics = [] if is_opensearch else _metric_identities_from_query(query)
        panels.append({
            "id": str(panel.get("id", f"panel-{index + 1}")),
            "title": panel.get("title", f"Panel {index + 1}"),
            "metric": source_metrics[0] if source_metrics else "",
            "sourceMetrics": source_metrics,
            "metricMetadata": {},
            "metricSeries": [],
            "datasource": panel_datasource,
            "index": target.get("alias", target.get("index", "")) if is_opensearch else None,
            "timeField": target.get("timeField", "@timestamp") if is_opensearch else None,
            "query": query,
            "queryResult": {},
            "resultType": _requested_result_type(panel.get("type", "timeseries"), "opensearch" if is_opensearch else "prometheus") if panel.get("type", "timeseries") in SUPPORTED_VISUALIZATIONS else "",
            "target": _target_from_query(query),
            "visualizationType": panel.get("type", "timeseries"),
            "visualizationConfig": panel.get("fieldConfig", {}).get("defaults", {}),
            "fieldConfig": {},
            "layout": {"x": panel.get("gridPos", {}).get("x", 0), "y": panel.get("gridPos", {}).get("y", index * 8)},
            "size": {"w": panel.get("gridPos", {}).get("w", 12), "h": panel.get("gridPos", {}).get("h", 8)},
            "variableRefs": [],
            "grafanaPanel": copy.deepcopy(panel),
        })
    available_targets = list(dict.fromkeys(
        panel["target"]["value"] for panel in panels if panel.get("target", {}).get("value")
    ))
    return {
        "operation": "update",
        "dashboardUid": uid,
        "name": raw.get("title", uid),
        "description": raw.get("description", ""),
        "datasource": datasource,
        "availableTargets": available_targets,
        "variables": raw.get("templating", {}).get("list", []),
        "timeConfig": raw.get("time", {"from": "now-1h", "to": "now"}),
        "panels": panels,
    }


async def refresh_preview(ir: dict, panel_ids: set[str] | None = None) -> dict:
    """Execute current panel queries and return IR with fresh normalized preview results."""
    refreshed = copy.deepcopy(ir)
    time_from = str(refreshed.get("timeConfig", {}).get("from", "now-1h"))
    time_range = time_from.removeprefix("now-") if time_from.startswith("now-") else "1h"

    async def populate(panel: dict) -> None:
        if panel_ids is not None and str(panel.get("id")) not in panel_ids:
            return
        datasource_uid = panel.get("datasource", {}).get("uid")
        if not panel.get("query") or not datasource_uid:
            panel["queryResult"] = {
                "status": "error", "type": panel.get("resultType", ""),
                "error": "Panel query or datasource UID is missing.",
            }
            return
        try:
            datasource_type = panel.get("datasource", {}).get("type", "prometheus").lower()
            if datasource_type in {"opensearch", "elasticsearch", "grafana-opensearch-datasource"}:
                raw = _require_mcp_content(
                    await execute_opensearch(panel["query"], datasource_uid, panel.get("index") or "*", f"now-{time_range}", "now", 20),
                    "OpenSearch",
                )
                parsed = _json(raw)
                kind = panel.get("resultType") or _requested_result_type(panel["visualizationType"], "opensearch")
                try:
                    panel["queryResult"] = normalize_opensearch_result(parsed if parsed is not None else raw, panel["datasource"], panel["query"], panel.get("index") or "*", f"now-{time_range}", "now", kind, panel.get("fieldConfig"))
                    incompatible = compatibility_error("opensearch", panel.get("visualizationType", ""), panel["queryResult"])
                    if incompatible:
                        panel["queryResult"] = {"status": "incompatible", "type": panel["queryResult"].get("type", kind), "error": incompatible}
                except Exception as exc:
                    panel["queryResult"] = {"status": "normalization_error", "type": kind, "error": str(exc)}
            else:
                raw = _require_mcp_content(await execute_prometheus(panel["query"], datasource_uid, time_range, 60), "Prometheus")
                kind = panel.get("resultType") or _requested_result_type(panel["visualizationType"], "prometheus")
                try:
                    panel["queryResult"] = _normalize_prometheus_shape(raw, kind, source_metric=panel.get("metric", ""))
                    panel["metricSeries"] = [item["labels"] for item in _normalize_query_result(raw)["series"][:5]]
                    incompatible = compatibility_error("prometheus", panel.get("visualizationType", ""), panel["queryResult"])
                    if incompatible:
                        panel["queryResult"] = {"status": "incompatible", "type": panel["queryResult"].get("type", kind), "error": incompatible}
                except Exception as exc:
                    panel["queryResult"] = {"status": "normalization_error", "type": kind, "error": str(exc)}
        except Exception as exc:
            panel["queryResult"] = {"status": "error", "type": panel.get("resultType", ""), "error": str(exc)}
    await asyncio.gather(*(populate(panel) for panel in refreshed.get("panels", [])))
    return refreshed


async def _populate_query_results(ir: dict, time_range: str) -> None:
    refreshed = await refresh_preview({**ir, "timeConfig": {**ir.get("timeConfig", {}), "from": f"now-{time_range}"}})
    ir.clear()
    ir.update(refreshed)


async def _build_update_proposal(request: str, target: str | None, time_range: str) -> dict:
    uid, raw = await _resolve_dashboard(request, "update")
    ir = _hydrate_dashboard(uid, raw)
    await _populate_query_results(ir, time_range)
    classified_panels = classify_dashboard_panels(request)
    requested_panels = [panel for panel in classified_panels if panel["datasourceType"] == "prometheus"]
    requested_opensearch = [panel for panel in classified_panels if panel["datasourceType"] == "opensearch"]
    wanted = [panel["measurement"] for panel in requested_panels]
    is_add = bool(re.search(r"\b(add|include|append|insert)\b", request, re.I))
    is_change = bool(re.search(r"\b(change|modify|edit|set|switch|convert)\b", request, re.I))
    if is_change:
        changed = 0
        for specification in classified_panels:
            visualization = specification.get("visualization")
            if specification["datasourceType"] == "prometheus":
                metric = specification.get("metric") or ""
                matches = [
                    panel for panel in ir["panels"]
                    if metric and _panel_matches_metric(panel, metric)
                ]
            else:
                matches = [panel for panel in ir["panels"] if specification["name"] in panel.get("title", "").lower() or specification["index"] == panel.get("index")]
            existing_visualization = specification.get("existingVisualization")
            if existing_visualization:
                matches = [panel for panel in matches if panel.get("visualizationType") == existing_visualization]
            if len(matches) != 1:
                label = specification.get("measurement") or specification.get("name") or "requested"
                raise ValueError(f"Identify exactly one existing {label} panel to modify.")
            if visualization:
                ds_kind = "opensearch" if str(matches[0].get("datasource", {}).get("type", "")).lower() in {"opensearch", "elasticsearch", "grafana-opensearch-datasource"} else "prometheus"
                result_kind = _requested_result_type(visualization, ds_kind)
                if ds_kind == "opensearch" and result_kind not in {DOCUMENTS, GEOGRAPHIC, GRAPH}:
                    raise ValueError(
                        "Installed Grafana MCP exposes Lucene document queries for OpenSearch but not Query DSL "
                        "aggregations; requested aggregation visualization is unsupported until MCP adds this capability."
                    )
                matches[0]["visualizationType"] = visualization
                matches[0]["resultType"] = result_kind
                matches[0]["queryResult"] = empty_result(matches[0]["resultType"])
                matches[0]["visualizationConfig"] = {"unit": matches[0].get("visualizationConfig", {}).get("unit", "short")}
                changed += 1
        if changed:
            return PROPOSALS.create(ir)
    if not is_add:
        if not wanted and not requested_opensearch:
            raise ValueError("UPDATE requires a discovered measurement to add or an identifiable panel/configuration change.")
        raise ValueError("UPDATE must explicitly add a panel or modify an existing panel.")
    if not classified_panels:
        schema = await discover_prometheus_schema()
        requested_panels = _explicit_metric_panels(request, schema["metrics"])
        classified_panels = [{**panel, "datasourceType": "prometheus"} for panel in requested_panels]
    if not classified_panels:
        raise ValueError("UPDATE requires a supported measurement, OpenSearch request, or explicit discovered metric.")
    groups = []
    if requested_panels:
        groups.append(("prometheus", _resolve_prometheus_panels(requested_panels, request, target, time_range)))
    if requested_opensearch:
        groups.append(("opensearch", _resolve_opensearch_panels(requested_opensearch, time_range)))
    by_type, resolution_errors = await _resolve_datasource_groups(groups)
    if not by_type:
        detail = "; ".join(f"{item['datasource']}: {item['message']}" for item in resolution_errors)
        raise RuntimeError(detail or "No datasource produced a dashboard panel update.")
    queues = {kind: list(value[0]) for kind, value in by_type.items()}
    if "prometheus" in by_type:
        ir["availableTargets"] = by_type["prometheus"][1]["targets"]
    successful_specs = [
        specification for specification in classified_panels
        if specification["datasourceType"] in queues and queues[specification["datasourceType"]]
    ]
    additions = [queues[specification["datasourceType"]].pop(0) for specification in successful_specs]
    for specification, candidate in zip(successful_specs, additions):
        candidate.update({"id": f"panel-{len(ir['panels']) + 1}", "layout": {"x": 0, "y": len(ir["panels"]) * 8}, "size": {"w": 12, "h": 8}})
        if not specification.get("allowEquivalent") and any(_panels_equivalent(panel, candidate) for panel in ir["panels"]):
            raise ValueError("Dashboard already contains an equivalent panel; use 'another' to request an intentional duplicate.")
        ir["panels"].append(candidate)
    ir["timeConfig"] = {"from": f"now-{time_range}", "to": "now"}
    if resolution_errors:
        ir["resolutionStatus"] = "partial"
        ir["resolutionErrors"] = resolution_errors
    return PROPOSALS.create(ir)


async def _build_remove_proposal(request: str) -> dict:
    uid, raw = await _resolve_dashboard(request, "remove")
    ir = _hydrate_dashboard(uid, raw)
    if re.search(r"\b(delete|remove)\s+(?:this|the|my)?\s*dashboard\b", request, re.I):
        ir["operation"] = "remove"
        ir["removeDashboard"] = True
        return PROPOSALS.create(ir)
    specifications = classify_dashboard_panels(request)
    if specifications:
        matches = []
        for specification in specifications:
            if specification["datasourceType"] == "prometheus":
                metric = specification.get("metric") or ""
                candidates = [
                    panel for panel in ir["panels"]
                    if metric and _panel_matches_metric(panel, metric)
                ]
                label = metric or "prometheus"
            else:
                candidates = [panel for panel in ir["panels"] if specification["index"] == panel.get("index") or specification["name"] in panel.get("title", "").lower()]
                label = specification["name"]
            requested_visualization = specification.get("visualization")
            if requested_visualization:
                candidates = [panel for panel in candidates if panel.get("visualizationType") == requested_visualization]
            if not candidates:
                raise ValueError(f"Identify one or more existing {label} panels to remove.")
            for candidate in candidates:
                if candidate not in matches:
                    matches.append(candidate)
    else:
        panel_match = re.search(r"(?:remove|delete)\s+(?:the\s+)?(.+?)\s+panel", request, re.I)
        requested = panel_match.group(1).strip() if panel_match else ""
        matches = [p for p in ir["panels"] if requested and requested.lower() in p["title"].lower()]
        if len(matches) != 1:
            raise ValueError("Identify one or more existing panels to remove.")
    ir["operation"] = "remove"
    removed_ids = {panel["id"] for panel in matches}
    ir["removePanelId"] = matches[0]["id"] if len(matches) == 1 else None
    ir["removePanelIds"] = sorted(removed_ids)
    ir["panels"] = [p for p in ir["panels"] if p["id"] not in removed_ids]
    return PROPOSALS.create(ir)


def validate_ir(ir: dict) -> list[str]:
    errors = []
    if ir.get("operation") not in {"create", "update", "remove"}:
        errors.append("operation must be create, update, or remove")
    if not ir.get("name"):
        errors.append("dashboard name is required")
    if not ir.get("panels") and ir.get("operation") != "remove":
        errors.append("at least one panel is required")
    seen = set()
    for panel in ir.get("panels", []):
        if panel.get("id") in seen:
            errors.append(f"duplicate panel id: {panel.get('id')}")
        seen.add(panel.get("id"))
        if not panel.get("query"):
            errors.append(f"panel {panel.get('id')} has no query")
        if panel.get("visualizationType") not in SUPPORTED_VISUALIZATIONS:
            errors.append(f"unsupported visualization: {panel.get('visualizationType')}")
        datasource_type = str(panel.get("datasource", {}).get("type", "prometheus")).lower()
        if datasource_type == "grafana-opensearch-datasource":
            datasource_type = "opensearch"
        if datasource_type not in DATASOURCE_VISUALIZATIONS:
            errors.append(f"unsupported datasource: {datasource_type}")
        elif panel.get("visualizationType") not in DATASOURCE_VISUALIZATIONS[datasource_type]:
            errors.append(f"visualization {panel.get('visualizationType')} is incompatible with {datasource_type}")
        if datasource_type in {"opensearch", "elasticsearch"} and not panel.get("index"):
            errors.append(f"panel {panel.get('id')} has no OpenSearch index")
        if panel.get("queryResult", {}).get("type"):
            error = compatibility_error(datasource_type, panel.get("visualizationType", ""), panel["queryResult"])
            if error:
                errors.append(f"panel {panel.get('id')}: {error}")
    return errors


def _visualization_options(visualization: str, config: dict) -> dict:
    reducers = {"stat": ["lastNotNull"], "gauge": ["lastNotNull"], "piechart": ["lastNotNull"]}
    if visualization == "logs":
        return {"showTime": True, "showLabels": True, "wrapLogMessage": True, "sortOrder": "Descending"}
    if visualization == "table":
        return {"showHeader": True, "cellHeight": "sm"}
    if visualization in reducers:
        base = {"reduceOptions": {"values": False, "calcs": reducers[visualization], "fields": ""}, "orientation": "auto"}
        if visualization == "gauge": base.update({"showThresholdLabels": False, "showThresholdMarkers": True})
        if visualization == "piechart": base.update({"displayLabels": ["name", "percent"], "legend": {"displayMode": "list", "placement": "right"}, "pieType": "pie", "tooltip": {"mode": "single"}})
        return base
    if visualization == "timeseries":
        return {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi", "sort": "none"}}
    if visualization == "barchart":
        return {"orientation": "auto", "xTickLabelRotation": 0, "showValue": "auto", "legend": {"displayMode": "list", "placement": "bottom"}}
    if visualization == "histogram":
        return {"bucketSize": config.get("bucketSize"), "legend": {"displayMode": "list", "placement": "bottom"}}
    if visualization == "heatmap":
        return {"calculate": False, "color": {"mode": "scheme", "scheme": "Spectral"}, "legend": {"show": True}, "tooltip": {"show": True, "yHistogram": False}}
    if visualization == "geomap":
        return {"view": {"id": "fit", "lat": 0, "lon": 0, "zoom": 1}, "layers": [{"type": "markers", "config": {"showLegend": True}, "location": {"mode": "coords", "latitude": "latitude", "longitude": "longitude"}}]}
    if visualization == "nodegraph":
        return {"nodes": {"mainStatUnit": config.get("unit", "short")}, "edges": {"mainStatUnit": config.get("unit", "short")}}
    return {}


def _field_defaults(visualization: str, config: dict) -> dict:
    defaults = {"unit": config.get("unit", "short")}
    if visualization == "gauge":
        defaults.update({"min": config.get("min"), "max": config.get("max"), "thresholds": {"mode": "absolute", "steps": config.get("thresholds", [{"color": "green", "value": None}])}})
    return defaults


def compile_dashboard(ir: dict) -> dict:
    errors = validate_ir(ir)
    if errors:
        raise ValueError("; ".join(errors))
    if ir.get("operation") == "remove" and ir.get("removeDashboard"):
        return {"operation": "delete_dashboard", "uid": ir["dashboardUid"], "title": ir["name"]}
    panels = []
    for idx, panel in enumerate(ir["panels"], 1):
        config = panel.get("visualizationConfig", {})
        datasource_type = str(panel.get("datasource", {}).get("type", "prometheus")).lower()
        is_opensearch = datasource_type in {"opensearch", "elasticsearch", "grafana-opensearch-datasource"}
        datasource_json = {
            "type": "grafana-opensearch-datasource" if is_opensearch else "prometheus",
            "uid": panel["datasource"]["uid"],
        }
        target_json = ({
            "refId": "A", "datasource": datasource_json, "query": panel["query"],
            "alias": panel.get("index"), "timeField": panel.get("timeField", "@timestamp"),
            "metrics": [{"id": "1", "type": "logs" if panel.get("resultType") == DOCUMENTS else "raw_data"}],
            "resultType": panel.get("resultType", panel.get("queryResult", {}).get("type")),
            "queryDsl": _json(panel["query"]),
        } if is_opensearch else {"refId": "A", "datasource": datasource_json, "expr": panel["query"], "range": panel.get("resultType") not in {SCALAR, CATEGORICAL}, "instant": panel.get("resultType") in {SCALAR, CATEGORICAL}})
        if panel.get("grafanaPanel") and ir.get("operation") == "update":
            compiled = copy.deepcopy(panel["grafanaPanel"])
            compiled["title"] = panel["title"]
            compiled["type"] = panel["visualizationType"]
            compiled["gridPos"] = {**panel["layout"], **panel["size"]}
            compiled["options"] = _visualization_options(panel["visualizationType"], config)
            compiled["fieldConfig"] = {"defaults": _field_defaults(panel["visualizationType"], config), "overrides": []}
            if panel.get("query"):
                compiled["datasource"] = datasource_json
                compiled["targets"] = [target_json]
            panels.append(compiled)
            continue
        panels.append({
            "id": idx,
            "title": panel["title"],
            "type": panel["visualizationType"],
            "datasource": datasource_json,
            "targets": [target_json],
            "gridPos": {**panel["layout"], **panel["size"]},
            "fieldConfig": {
                "defaults": _field_defaults(panel["visualizationType"], config),
                "overrides": [],
            },
            "options": _visualization_options(panel["visualizationType"], config),
        })
    return {
        "dashboard": {
            "id": None,
            "uid": ir.get("dashboardUid") if ir.get("operation") in {"update", "remove"} else None,
            "title": ir["name"],
            "description": ir.get("description", ""),
            "timezone": "browser",
            "schemaVersion": 39,
            "version": 0,
            "refresh": "10s",
            "time": ir["timeConfig"],
            "templating": {"list": ir.get("variables", [])},
            "panels": panels,
        },
        "overwrite": ir.get("operation") in {"update", "remove"},
        "message": "Updated from an explicitly approved dashboard proposal" if ir.get("operation") in {"update", "remove"} else "Created from an explicitly approved dashboard proposal",
    }


class ProposalStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, dict] = {}

    @staticmethod
    def _digest(ir: dict) -> str:
        return hashlib.sha256(json.dumps(ir, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def create(self, ir: dict, proposal_id: str | None = None) -> dict:
        errors = validate_ir(ir)
        if errors:
            raise ValueError("; ".join(errors))
        with self._lock:
            pid = proposal_id or secrets.token_urlsafe(12)
            previous = self._items.get(pid)
            version = (previous or {}).get("version", 0) + 1
            item = {"proposalId": pid, "version": version, "status": "proposed", "approvalToken": None, "approvedAt": None, "ir": copy.deepcopy(ir)}
            item["digest"] = self._digest(item["ir"])
            self._items[pid] = item
            return copy.deepcopy(item)

    def get(self, pid: str) -> dict:
        with self._lock:
            if pid not in self._items:
                raise KeyError("Proposal not found.")
            return copy.deepcopy(self._items[pid])

    def modify(self, pid: str, ir: dict) -> dict:
        return self.create(ir, pid)

    def approve(self, pid: str, version: int) -> dict:
        with self._lock:
            item = self._items.get(pid)
            if not item or item["version"] != version:
                raise ValueError("Approval version is stale or proposal does not exist.")
            item["status"] = "approved"
            item["approvalToken"] = secrets.token_urlsafe(24)
            item["approvedAt"] = datetime.now(timezone.utc).isoformat()
            return copy.deepcopy(item)

    def reject(self, pid: str, version: int) -> dict:
        with self._lock:
            item = self._items.get(pid)
            if not item or item["version"] != version:
                raise ValueError("Rejection version is stale or proposal does not exist.")
            item["status"] = "rejected"
            item["approvalToken"] = None
            item["approvedAt"] = None
            return copy.deepcopy(item)

    def set_status(self, pid: str, status: str) -> dict:
        with self._lock:
            item = self._items[pid]
            item["status"] = status
            return copy.deepcopy(item)

    def verified(self, pid: str, version: int, token: str) -> dict:
        item = self.get(pid)
        if item["status"] != "approved" or item["version"] != version or not secrets.compare_digest(item.get("approvalToken") or "", token or ""):
            raise PermissionError("A valid application-level approval for this exact proposal version is required.")
        if item["digest"] != self._digest(item["ir"]):
            raise PermissionError("Approved proposal content no longer matches its approval digest.")
        return item


PROPOSALS = ProposalStore()


async def execute_approved_mutation(proposal_id: str, version: int, approval_token: str) -> dict:
    item = PROPOSALS.verified(proposal_id, version, approval_token)
    if os.environ.get("GRAFANA_MCP_ENABLE_WRITE", "").lower() not in {"1", "true", "yes", "on"}:
        raise PermissionError("Grafana MCP writes are disabled. Set GRAFANA_MCP_ENABLE_WRITE=true for manual E2E testing.")
    payload = compile_dashboard(item["ir"])
    PROPOSALS.set_status(proposal_id, "executing")
    try:
        if item["ir"].get("operation") == "remove" and item["ir"].get("removeDashboard"):
            result = await mcp_call(
                "grafana_api_request",
                {"method": "DELETE", "endpoint": f"/api/dashboards/uid/{item['ir']['dashboardUid']}"},
                raw=True,
                timeout=30.0,
            )
        elif item["ir"].get("operation") in {"update", "remove"}:
            result = await mcp_call("update_dashboard", {"dashboard": payload["dashboard"], "overwrite": True, "message": payload.get("message", "Approved dashboard update")}, raw=True, timeout=30.0)
        else:
            result = await mcp_call("update_dashboard", payload, raw=True, timeout=30.0)
    except Exception:
        PROPOSALS.set_status(proposal_id, "failed")
        raise
    if not result:
        PROPOSALS.set_status(proposal_id, "failed")
        raise RuntimeError("Grafana MCP returned no mutation result.")
    final = PROPOSALS.set_status(proposal_id, "built")
    parsed = _json(result)
    return {"proposal": final, "grafanaPayload": payload, "grafanaResult": parsed if parsed is not None else result}


def propose_dashboard(
    request: str,
    target: str = "",
    time_range: Literal["15m", "1h", "6h", "12h", "24h", "7d"] = "1h",
) -> dict:
    """ADK boundary: always return a non-empty, JSON-serializable terminal outcome."""
    equivalent_durations = {
        "900": "15m",
        "3600": "1h",
        "21600": "6h",
        "43200": "12h",
        "86400": "24h",
        "604800": "7d",
    }
    canonical_time_range = equivalent_durations.get(str(time_range), str(time_range))
    tool_started = time.perf_counter()
    try:
        proposal = run_sync(build_proposal(request, target or None, canonical_time_range))
        proposal_ready = time.perf_counter()
        if not isinstance(proposal, dict) or not proposal:
            raise RuntimeError("Dashboard proposal builder returned no usable result.")
        # Round-trip at the tool boundary. This rejects coroutines, sessions, exceptions,
        # and custom runtime objects before ADK tries to encode the FunctionResponse.
        proposal = json.loads(json.dumps(proposal, allow_nan=False))
        serialized = time.perf_counter()
        errors = proposal.get("ir", {}).get("resolutionErrors", [])
        status = "partial" if errors else "success"
        ir = proposal.get("ir", {})
        panels = [
            {
                "datasource": str(panel.get("datasource", {}).get("type", "unknown")),
                "title": str(panel.get("title", "Panel")),
                "visualization": str(panel.get("visualizationType", "unknown")),
            }
            for panel in ir.get("panels", [])
        ]
        outcome = {
            "status": status,
            "proposalId": proposal.get("proposalId"),
            "dashboardName": ir.get("name"),
            "operation": ir.get("operation"),
            "panelCount": len(panels),
            "panels": panels,
            "errors": errors,
        }
        result = json.loads(json.dumps(outcome, allow_nan=False))
        logger.info(
            "[dashboard] tool_complete status=%s build_ms=%.1f serialization_ms=%.1f full_proposal_bytes=%d model_result_bytes=%d total_ms=%.1f",
            status,
            (proposal_ready - tool_started) * 1000,
            (serialized - proposal_ready) * 1000,
            len(json.dumps(proposal, separators=(",", ":"))),
            len(json.dumps(result, separators=(",", ":"))),
            (time.perf_counter() - tool_started) * 1000,
        )
        return result
    except Exception as exc:
        message = str(exc) or "Dashboard operation failed without an error message."
        lower = message.lower()
        if "clarification" in lower or "clarify" in lower or "identify exactly one" in lower:
            status = "clarification"
            outcome = {"status": status, "question": message, "errors": []}
        elif "unsupported" in lower or "cannot provide" in lower or "requires a discovered" in lower:
            status = "unsupported"
            outcome = {"status": status, "reason": message, "errors": []}
        else:
            status = "error"
            outcome = {
                "status": status,
                "errors": [{"type": type(exc).__name__, "message": message}],
            }
        # Keep failures useful to operators without leaking exception objects to ADK.
        result = json.loads(json.dumps(outcome, allow_nan=False))
        logger.info(
            "[dashboard] tool_complete status=%s model_result_bytes=%d total_ms=%.1f",
            status,
            len(json.dumps(result, separators=(",", ":"))),
            (time.perf_counter() - tool_started) * 1000,
        )
        return result


def resolve_dashboard_intent(request: str) -> dict:
    """Return normalized intent metadata for ADK/tool routing."""
    resolution = resolve_intent(request)
    return {"intent": resolution.intent.value, "confidence": resolution.confidence, "reason": resolution.reason, "operation": resolution.operation}


def execute_approved_dashboard(proposal_id: str, version: int, approval_token: str) -> dict:
    return run_sync(execute_approved_mutation(proposal_id, version, approval_token))
