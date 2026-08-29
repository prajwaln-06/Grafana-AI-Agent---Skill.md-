import json
from typing import Any

from app.dashboard.results import CATEGORICAL, DOCUMENTS, GEOGRAPHIC, GRAPH, HEATMAP, HISTOGRAM, SCALAR, TIME_SERIES, empty_result

from app.mcp.session import mcp_call
from .utils import logger


async def execute_opensearch(
    query: str,
    ds_uid: str,
    index: str,
    start_time: str = "now-1h",
    end_time: str = "now",
    limit: int = 20,
) -> str:
    """Execute the installed MCP v0.17.2 Elasticsearch/OpenSearch query tool."""
    logger.debug(f"Executing OpenSearch query: {query[:100]} on uid={ds_uid}")
    return await mcp_call(
        "query_elasticsearch",
        {
            "datasourceUid": ds_uid,
            "index": index,
            "query": query,
            "startTime": start_time,
            "endTime": end_time,
            "limit": limit,
        },
    )


def _documents(data: Any) -> tuple[list[dict], int, Any]:
    if isinstance(data, list):
        return [hit for hit in data if isinstance(hit, dict)], len(data), None
    if not isinstance(data, dict):
        return [], 0, None
    hits_block = data.get("hits", {})
    hits = hits_block.get("hits", []) if isinstance(hits_block, dict) else []
    if not hits and isinstance(data.get("documents"), list):
        hits = data["documents"]
    total_value = hits_block.get("total", len(hits)) if isinstance(hits_block, dict) else len(hits)
    total = total_value.get("value", len(hits)) if isinstance(total_value, dict) else total_value
    return [hit for hit in hits if isinstance(hit, dict)], int(total or 0), data.get("aggregations")


def discover_fields(data: Any) -> dict:
    """Infer observed field capabilities from a deterministic document sample."""
    hits, _, _ = _documents(data)
    fields: dict[str, dict] = {}
    for hit in hits:
        source = hit.get("_source", hit.get("source", {}))
        if not isinstance(source, dict):
            continue
        stack = [("", source)]
        while stack:
            prefix, value = stack.pop()
            for key, item in value.items():
                name = f"{prefix}.{key}" if prefix else key
                if isinstance(item, dict):
                    stack.append((name, item)); continue
                kind = "boolean" if isinstance(item, bool) else "number" if isinstance(item, (int, float)) else "date" if key in {"@timestamp", "Timestamp"} else "text"
                fields.setdefault(name, {"name": name, "type": kind, "values": []})
                if len(fields[name]["values"]) < 5 and item not in fields[name]["values"]:
                    fields[name]["values"].append(item)
    names = set(fields)
    return {
        "discoveryMode": "sampled_documents",
        "mappingDiscovery": False,
        "limitation": "Fields and types are observed from returned documents, not OpenSearch mappings.",
        "fields": sorted(fields.values(), key=lambda item: item["name"]),
        "timestampFields": sorted(name for name in names if name in {"@timestamp", "Timestamp"} or name.lower().endswith("timestamp")),
        "numericFields": sorted(name for name, field in fields.items() if field["type"] == "number"),
        "categoricalFields": sorted(name for name, field in fields.items() if field["type"] in {"text", "boolean"}),
        "latitudeField": next((name for name in names if name.lower() in {"latitude", "lat", "location.lat", "geo.lat"}), None),
        "longitudeField": next((name for name in names if name.lower() in {"longitude", "lon", "lng", "location.lon", "geo.lon"}), None),
        "sourceField": next((name for name in names if name.lower() in {"source", "src", "source.id", "source.name"}), None),
        "targetField": next((name for name in names if name.lower() in {"target", "dst", "destination", "target.id", "target.name"}), None),
    }


def build_opensearch_query(base_query: str, result_type: str, *, time_field: str = "@timestamp", dimension: str = "", value_field: str = "", x_field: str = "", y_field: str = "") -> str:
    """Build Elasticsearch/OpenSearch DSL for requested normalized result shape."""
    query_clause: dict = {"query_string": {"query": base_query or "*"}}
    body: dict[str, Any] = {"query": query_clause}
    if result_type in {DOCUMENTS, GEOGRAPHIC, GRAPH}:
        body.update({"size": 100, "sort": [{time_field: {"order": "desc", "unmapped_type": "date"}}]})
    elif result_type == SCALAR:
        body.update({"size": 0, "aggs": {"value": {"filter": {"match_all": {}}}}})
    elif result_type == CATEGORICAL:
        body.update({"size": 0, "aggs": {"categories": {"terms": {"field": dimension, "size": 50}}}})
    elif result_type == TIME_SERIES:
        body.update({"size": 0, "aggs": {"timeline": {"date_histogram": {"field": time_field, "fixed_interval": "1m", "min_doc_count": 0}}}})
    elif result_type == HISTOGRAM:
        body.update({"size": 0, "aggs": {"histogram": {"histogram": {"field": value_field, "interval": 10}}}})
    elif result_type == HEATMAP:
        x_aggregation = {"date_histogram": {"field": time_field, "fixed_interval": "5m", "min_doc_count": 0}} if not x_field else {"terms": {"field": x_field, "size": 50}}
        body.update({"size": 0, "aggs": {"x": {**x_aggregation, "aggs": {"y": {"histogram": {"field": y_field or value_field, "interval": 10}}}}}})
    return json.dumps(body, separators=(",", ":"), sort_keys=True)


def normalize_opensearch_result(
    data: Any,
    datasource: dict,
    query: str,
    index: str,
    start_time: str,
    end_time: str,
    result_type: str | None = None,
    field_config: dict | None = None,
) -> dict:
    """Return a stable document-oriented result derived from the MCP response."""
    hits, total, aggregations = _documents(data)
    documents = []
    fields = set()
    for hit in hits:
        source = hit.get("_source", hit.get("source", {}))
        if not isinstance(source, dict):
            source = {}
        fields.update(source)
        documents.append({
            "timestamp": source.get("@timestamp", source.get("Timestamp")),
            "index": hit.get("_index", hit.get("index")),
            "id": hit.get("_id", hit.get("id")),
            "body": source.get("Body", source.get("body", source.get("message"))),
            "severity": source.get("SeverityText", source.get("Severity", source.get("severity"))),
            "resource": source.get("Resource", source.get("resource", {})),
            "attributes": source.get("Attributes", source.get("attributes", {})),
            "fields": source,
        })
    kind = result_type or DOCUMENTS
    result = {
        "status": "success",
        "type": kind,
        "datasource": {"uid": datasource["uid"], "type": datasource["type"], "name": datasource["name"]},
        "query": query,
        "index": index,
        "timeRange": {"start": start_time, "end": end_time},
        "total": total,
        "documents": documents,
        "fields": sorted(fields),
    }
    if aggregations is not None:
        result["aggregations"] = aggregations
        result["aggregation"] = aggregations
    config = field_config or {}
    if kind == SCALAR:
        value_agg = (aggregations or {}).get("value", {})
        result.update({"value": value_agg.get("value", value_agg.get("doc_count")) if aggregations is not None else None, "field": config.get("valueField")})
    elif kind in {CATEGORICAL, TIME_SERIES, HISTOGRAM}:
        aggregation_name = {CATEGORICAL: "categories", TIME_SERIES: "timeline", HISTOGRAM: "histogram"}[kind]
        raw_buckets = (aggregations or {}).get(aggregation_name, {}).get("buckets", [])
        if kind == CATEGORICAL:
            result.update({"dimension": config.get("dimension"), "buckets": [{"key": b.get("key_as_string", b.get("key")), "value": b.get("doc_count", 0)} for b in raw_buckets]})
        elif kind == TIME_SERIES:
            result.update({"series": [{"labels": {}, "points": [{"timestamp": (b.get("key", 0) / 1000), "value": b.get("doc_count", 0)} for b in raw_buckets]}]})
        else:
            result.update({"field": config.get("valueField"), "cumulative": False, "buckets": [{"lower": b.get("key"), "upper": b.get("key") + config.get("interval", 10), "count": b.get("doc_count", 0)} for b in raw_buckets if isinstance(b.get("key"), (int, float))]})
    elif kind == HEATMAP:
        cells, xs, ys = [], [], []
        for x_bucket in (aggregations or {}).get("x", {}).get("buckets", []):
            x = x_bucket.get("key", 0) / 1000
            xs.append(x)
            for y_bucket in x_bucket.get("y", {}).get("buckets", []):
                y = y_bucket.get("key")
                ys.append(y); cells.append({"x": x, "y": y, "value": y_bucket.get("doc_count", 0)})
        result.update({"xBuckets": sorted(set(xs)), "yBuckets": sorted(set(ys)), "cells": cells})
    elif kind == GEOGRAPHIC:
        lat_field, lon_field = config.get("latitudeField"), config.get("longitudeField")
        points = []
        for document in documents:
            source = document.get("fields", {})
            try:
                points.append({"latitude": float(_field_value(source, lat_field)), "longitude": float(_field_value(source, lon_field)), "label": document.get("body"), "value": 1})
            except (TypeError, ValueError):
                continue
        result["points"] = points
    elif kind == GRAPH:
        source_field, target_field = config.get("sourceField"), config.get("targetField")
        nodes, edges, seen = [], [], set()
        for index_number, document in enumerate(documents):
            source = _field_value(document.get("fields", {}), source_field)
            target = _field_value(document.get("fields", {}), target_field)
            if source is None or target is None:
                continue
            for node in (source, target):
                if str(node) not in seen:
                    seen.add(str(node)); nodes.append({"id": str(node), "title": str(node)})
            edges.append({"id": document.get("id") or f"edge-{index_number + 1}", "source": str(source), "target": str(target), "value": 1})
        result.update({"nodes": nodes, "edges": edges})
    if kind not in {DOCUMENTS, GEOGRAPHIC, GRAPH} and aggregations is None:
        result.update(empty_result(kind))
        result.update({"status": "error", "error": f"OpenSearch response has no aggregation required for {kind}"})
    return result


def _field_value(source: dict, path: str | None) -> Any:
    value: Any = source
    for part in str(path or "").split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value
