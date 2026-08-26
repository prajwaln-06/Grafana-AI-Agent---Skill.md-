"""Shared normalized query-result contracts and visualization compatibility."""
from __future__ import annotations

from typing import Any

DOCUMENTS = "documents"
SCALAR = "scalar"
TIME_SERIES = "time_series"
CATEGORICAL = "categorical_buckets"
HISTOGRAM = "histogram"
HEATMAP = "heatmap"
GEOGRAPHIC = "geographic"
GRAPH = "graph"

VISUALIZATION_RESULT_TYPES = {
    "logs": {DOCUMENTS},
    "table": {DOCUMENTS, TIME_SERIES, CATEGORICAL, HISTOGRAM, GEOGRAPHIC, GRAPH},
    "stat": {SCALAR},
    "gauge": {SCALAR},
    "barchart": {CATEGORICAL},
    "piechart": {CATEGORICAL},
    "timeseries": {TIME_SERIES},
    "histogram": {HISTOGRAM},
    "heatmap": {HEATMAP},
    "geomap": {GEOGRAPHIC},
    "nodegraph": {GRAPH},
}

DATASOURCE_RESULT_TYPES = {
    "prometheus": {SCALAR, TIME_SERIES, CATEGORICAL, HISTOGRAM, HEATMAP, GEOGRAPHIC, GRAPH},
    "opensearch": {DOCUMENTS, SCALAR, TIME_SERIES, CATEGORICAL, HISTOGRAM, HEATMAP, GEOGRAPHIC, GRAPH},
    "elasticsearch": {DOCUMENTS, SCALAR, TIME_SERIES, CATEGORICAL, HISTOGRAM, HEATMAP, GEOGRAPHIC, GRAPH},
}


def result_type(result: Any) -> str:
    return str(result.get("type", "")) if isinstance(result, dict) else ""


def compatibility_error(datasource_type: str, visualization: str, result: Any) -> str | None:
    datasource_type = "opensearch" if datasource_type == "grafana-opensearch-datasource" else datasource_type
    kind = result_type(result)
    if visualization not in VISUALIZATION_RESULT_TYPES:
        return f"unsupported visualization: {visualization}"
    if datasource_type not in DATASOURCE_RESULT_TYPES:
        return f"unsupported datasource: {datasource_type}"
    if kind not in DATASOURCE_RESULT_TYPES[datasource_type]:
        return f"datasource {datasource_type} cannot provide result type {kind or 'unknown'}"
    if kind not in VISUALIZATION_RESULT_TYPES[visualization]:
        expected = ", ".join(sorted(VISUALIZATION_RESULT_TYPES[visualization]))
        return f"visualization {visualization} requires {expected}, got {kind or 'unknown'}"
    return None


def empty_result(kind: str, **metadata: Any) -> dict:
    payload: dict[str, Any] = {"status": "empty_result", "type": kind}
    payload.update(metadata)
    payload.update({
        DOCUMENTS: {"documents": []},
        TIME_SERIES: {"series": []},
        CATEGORICAL: {"buckets": []},
        HISTOGRAM: {"buckets": [], "cumulative": False},
        HEATMAP: {"xBuckets": [], "yBuckets": [], "cells": []},
        GEOGRAPHIC: {"points": []},
        GRAPH: {"nodes": [], "edges": []},
        SCALAR: {"value": None},
    }[kind])
    return payload
