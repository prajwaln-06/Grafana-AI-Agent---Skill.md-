def render_chart(normalized_data: dict, chart_type: str) -> dict:
    """
    Renders the normalized data into a standard Chart Configuration JSON.
    Directly provides React/Recharts-ready point tuples: [{"t": timestamp, "v": value}].
    """
    config = {
        "chart_type": chart_type.upper(),
        "title": "Auto-Generated Chart",
        "series": []
    }
    
    metadata = normalized_data.get("metadata", {})
    if "source" in metadata:
        config["title"] = f"{metadata['source'].capitalize()} Data"
        
    series_list = normalized_data.get("series", [])
    
    if chart_type.lower() == "table":
        config["columns"] = ["Timestamp", "Details"]
        rows = []
        for s in series_list:
            for pt in s.get("points", []):
                rows.append({"Timestamp": pt.get("t"), "Details": pt.get("v")})
        config["data"] = rows
    else:
        for s in series_list:
            config["series"].append({
                "name": s.get("name", "Unknown"),
                "points": s.get("points", [])
            })
            
    return config
