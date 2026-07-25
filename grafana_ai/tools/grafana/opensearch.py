import json
from typing import Optional

from .session import mcp_call
from .utils import logger

async def execute_opensearch(query: str, ds_uid: str, lookback: str = "1h", limit: int = 20) -> str:
    """Execute an OpenSearch / Elasticsearch query via the MCP tool."""
    logger.debug(f"Executing OpenSearch query: {query[:100]} on uid={ds_uid}")
    
    # We use query_elasticsearch since it supports both OpenSearch and Elasticsearch
    return await mcp_call(
        "query_elasticsearch",
        {
            "datasourceUid": ds_uid,
            "query": query,
            "timeRange": lookback,
            "maxDataPoints": limit,
        }
    )

def format_opensearch_result(data: dict, context: str = "", hints: Optional[dict] = None, limit: int = 20) -> str:
    """Format OpenSearch results naturally."""
    
    # Extract hits
    hits = []
    
    if "responses" in data:
        for resp in data["responses"]:
            if "hits" in resp and "hits" in resp["hits"]:
                hits.extend(resp["hits"]["hits"])
    elif "hits" in data and "hits" in data["hits"]:
        hits.extend(data["hits"]["hits"])
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and "fields" in item:
                # Basic dataframe fallback
                pass
                
    if not hits:
        if hints and "message" in hints:
            return f"No documents were found.\n\nDiagnostic hint: {hints['message']}"
        return (
            "No documents were found.\n\n"
            "Possible reasons:\n"
            "- no logs/documents match the query in the selected time range\n"
            "- index does not exist\n"
            "- datasource is unreachable"
        )
        
    lines = []
    if context:
        lines.append(context)
        lines.append("")
        
    lines.append(f"Found {len(hits)} matching documents.\n")
    
    indexes = set()
    for h in hits:
        if "_index" in h:
            indexes.add(h["_index"])
            
    if indexes:
        lines.append("Index:")
        for idx in sorted(indexes):
            lines.append(idx)
        lines.append("")
        
    lines.append("Top results:")
    
    for h in hits[:limit]:
        source = h.get("_source", {})
        if not source:
            continue
            
        lines.append("")
        important = ["timestamp", "@timestamp", "time", "host", "hostname", "severity", "level", "message", "msg", "log"]
        
        for key in important:
            if key in source:
                lines.append(f"• {key}: {source[key]}")
                
        other_count = 0
        for k, v in source.items():
            if k not in important:
                if other_count < 5:
                    lines.append(f"• {k}: {v}")
                    other_count += 1
                else:
                    lines.append("• ...")
                    break

    if len(hits) > limit:
        lines.append(f"\n... and {len(hits) - limit} more documents")
        
    return "\n".join(lines)
