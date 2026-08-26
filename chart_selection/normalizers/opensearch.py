import math
from typing import Any, Dict, Optional

def _parse_point(t: Any, raw_v: Any) -> Optional[Dict[str, Any]]:
    """
    Defensively parses and sanitizes a single aggregation point.
    Filters out NaN, +Inf, -Inf, and invalid values.
    """
    if raw_v is None:
        return None
    # For nested raw JSON / objects (like log sources in hits), preserve object structure
    if isinstance(raw_v, (dict, list)):
        return {"t": t, "v": raw_v}
        
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

def normalize_opensearch(response: dict) -> dict:
    """Normalize OpenSearch JSON response to standard points format."""
    normalized = {
        "series": [],
        "metadata": {
            "source": "opensearch",
            "kind": "logs"
        }
    }

    aggregations = response.get("aggregations", {})
    if aggregations:
        for agg_name, agg_data in aggregations.items():
            if "buckets" in agg_data:
                buckets = agg_data["buckets"]
                
                # Check for nested metric keys inside buckets
                metric_keys = []
                if buckets and isinstance(buckets, list):
                    for k, v in buckets[0].items():
                        if isinstance(v, dict) and "value" in v:
                            metric_keys.append(k)
                
                if not metric_keys:
                    points = []
                    for b in buckets:
                        val_t = b.get("key_as_string", b.get("key"))
                        raw_v = b.get("doc_count", 0)
                        pt = _parse_point(val_t, raw_v)
                        if pt is not None:
                            points.append(pt)
                    
                    normalized["series"].append({
                        "name": agg_name,
                        "points": points,
                        "labels": {}
                    })
                else:
                    for m_key in metric_keys:
                        points = []
                        for b in buckets:
                            val_t = b.get("key_as_string", b.get("key"))
                            raw_v = b.get(m_key, {}).get("value", 0)
                            pt = _parse_point(val_t, raw_v)
                            if pt is not None:
                                points.append(pt)
                        
                        normalized["series"].append({
                            "name": f"{agg_name} ({m_key})",
                            "points": points,
                            "labels": {}
                        })

    elif "hits" in response and "hits" in response["hits"]:
        hits = response["hits"]["hits"]
        if hits and isinstance(hits, list):
            points = []
            for h in hits:
                source = h.get("_source", {})
                timestamp = source.get("@timestamp", h.get("_id", "unknown"))
                pt = _parse_point(timestamp, source)
                if pt is not None:
                    points.append(pt)
                
            normalized["series"].append({
                "name": "logs",
                "points": points,
                "labels": {}
            })
            
    return normalized
