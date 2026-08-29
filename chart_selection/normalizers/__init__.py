from .prometheus import normalize_prometheus
from .opensearch import normalize_opensearch

def normalize(raw_response: dict) -> dict:
    """
    Universal normalizer that auto-detects the data source based on the JSON shape.
    """
    if raw_response.get("status") == "error" or "error" in raw_response:
        raise ValueError(f"Error in data source response: {raw_response.get('error', raw_response)}")

    if "data" in raw_response and "resultType" in raw_response["data"]:
        return normalize_prometheus(raw_response)
        
    if "aggregations" in raw_response or "hits" in raw_response:
        return normalize_opensearch(raw_response)
        
    raise ValueError("Unknown data format! Could not auto-detect if response is Prometheus or OpenSearch.")

__all__ = ["normalize", "normalize_prometheus", "normalize_opensearch"]
