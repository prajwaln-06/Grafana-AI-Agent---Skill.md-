from typing import Any, Dict
from chart_selection.types import ChartAnalysis

def analyze_data(data: Dict[str, Any]) -> ChartAnalysis:
    """
    Analyzes the normalized data object and extracts properties.
    Supports both {timestamp, value} and {t, v} point shapes.
    """
    series_list = data.get("series", [])
    num_series = len(series_list)
    
    first_series = series_list[0] if series_list else {}
    points = first_series.get("points", [])
    num_points = len(points)
    
    is_single_value = False
    is_time_series = False
    is_categorical = False

    if num_points <= 1:
        if num_series == 1:
            is_single_value = True
        elif num_series > 1:
            is_categorical = True
    else:
        # Multiple points: inspect timestamp
        first_pt = points[0]
        first_t = first_pt.get("t") if "t" in first_pt else first_pt.get("timestamp")
        first_v = first_pt.get("v") if "v" in first_pt else first_pt.get("value")
        is_v_object = isinstance(first_v, dict)
        
        if not is_v_object:
            if isinstance(first_t, (int, float)) and first_t > 100000000:
                is_time_series = True
            elif isinstance(first_t, str) and ("T" in first_t or ":" in first_t or "Z" in first_t):
                is_time_series = True
            else:
                is_categorical = True
            
    has_threshold = "threshold" in data.get("metadata", {}) or "threshold" in data

    return ChartAnalysis(
        num_series=num_series,
        num_points=num_points,
        is_time_series=is_time_series,
        is_single_value=is_single_value,
        is_categorical=is_categorical,
        has_threshold=has_threshold
    )
