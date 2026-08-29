import math
from typing import Any, Dict, Optional

def get_metric_name(metric: dict) -> str:
    """Extract a reasonable name from a Prometheus metric dictionary."""
    if not metric:
        return "value"
    for key, val in metric.items():
        if key not in ("__name__", "job", "instance"):
            return str(val)
    if "instance" in metric:
        return str(metric["instance"])
    if "__name__" in metric:
        return str(metric["__name__"])
    return "value"

def _parse_point(t: Any, raw_v: Any) -> Optional[Dict[str, Any]]:
    """
    Defensively parses and sanitizes a single telemetry point.
    Filters out NaN, +Inf, -Inf, nulls, and unparseable values.
    """
    if raw_v is None:
        return None
    val_str = str(raw_v).strip().lower()
    if val_str in ("nan", "inf", "+inf", "-inf", "null", "none"):
        return None
    try:
        v = float(raw_v)
        if math.isnan(v) or math.isinf(v):
            return None
        return {"t": t, "v": v}
    except (ValueError, TypeError):
        return None

def normalize_prometheus(response: dict) -> dict:
    """Normalize Prometheus JSON response into a standardized points format."""
    data = response.get("data", {})
    result_type = data.get("resultType", "")
    results = data.get("result", [])

    normalized = {
        "series": [],
        "metadata": {
            "source": "prometheus",
            "kind": "metric"
        }
    }

    if result_type == "matrix":
        for res in results:
            metric = res.get("metric", {})
            name = get_metric_name(metric)
            values = res.get("values", [])
            points = []
            for v in values:
                if isinstance(v, (list, tuple)) and len(v) >= 2:
                    pt = _parse_point(v[0], v[1])
                    if pt is not None:
                        points.append(pt)
            
            normalized["series"].append({
                "name": name,
                "points": points,
                "labels": metric
            })

    elif result_type == "vector":
        for res in results:
            metric = res.get("metric", {})
            name = get_metric_name(metric)
            value = res.get("value", [])
            if value and isinstance(value, (list, tuple)) and len(value) >= 2:
                pt = _parse_point(name, value[1])
                if pt is not None:
                    normalized["series"].append({
                        "name": name,
                        "points": [pt],
                        "labels": metric
                    })

    elif result_type in ("scalar", "string"):
        if isinstance(results, (list, tuple)) and len(results) >= 2:
            pt = _parse_point(results[0], results[1])
            if pt is not None:
                normalized["series"].append({
                    "name": "value",
                    "points": [pt],
                    "labels": {}
                })
            
    return normalized
