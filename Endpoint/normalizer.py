"""
normalizer.py

Converts a backend's raw query result into ONE consistent shape, regardless
of whether the data came from Prometheus or (in future) OpenSearch:

    [
      {
        "labels": {"instance": "...", "cpu": "0", ...},
        "points": [
          {"timestamp": "2026-08-01T12:00:00Z", "value": 34.2},
          ...
        ]
      },
      ...
    ]

This is the shape any downstream agent (chart-building, summarization,
whatever comes next) should consume — it never needs to know Prometheus's
"matrix"/"vector" distinction or OpenSearch's aggregation-bucket shape.
"""
from datetime import datetime, timezone


def _ts_to_iso(unix_ts) -> str:
    return datetime.fromtimestamp(float(unix_ts), tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def normalize_prometheus_result(raw_json: dict) -> list:
    """
    raw_json: the full Prometheus response body (the dict with
    "status"/"data" keys), as returned in
    PrometheusExecutionResult.raw_json on a STATUS_SUCCESS result.

    Handles both resultType "matrix" (query_range's normal shape — a
    series of [timestamp, value] pairs per label set) and "vector"
    (a single instantaneous [timestamp, value] per label set), since a
    misconfigured or future instant-query path could hand either shape
    to this function. Anything else is left as an empty series list
    rather than guessed at.
    """
    data = raw_json.get("data", {})
    result_type = data.get("resultType")
    result = data.get("result", [])

    series = []

    if result_type == "matrix":
        for entry in result:
            labels = entry.get("metric", {})
            points = [
                {"timestamp": _ts_to_iso(ts), "value": _safe_float(val)}
                for ts, val in entry.get("values", [])
            ]
            series.append({"labels": labels, "points": points})

    elif result_type == "vector":
        for entry in result:
            labels = entry.get("metric", {})
            ts, val = entry.get("value", [None, None])
            points = []
            if ts is not None:
                points = [{"timestamp": _ts_to_iso(ts), "value": _safe_float(val)}]
            series.append({"labels": labels, "points": points})

    # Any other resultType (scalar/string) is intentionally not handled:
    # none of the queries this skill generates should ever produce one,
    # so silently normalizing them would hide a real upstream bug.

    return series


def _safe_float(val):
    """
    Prometheus encodes sample values as strings (including special values
    like "NaN", "+Inf", "-Inf"). Python's float() already parses all of
    these correctly, so this wrapper exists only to make that explicit
    and give a clear error if Prometheus ever changes that encoding.
    """
    return float(val)
