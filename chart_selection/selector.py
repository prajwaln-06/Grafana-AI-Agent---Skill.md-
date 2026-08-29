from typing import Any, Dict, Optional
from chart_selection.analyzer import analyze_data

def select_chart_type(data: Dict[str, Any], default_chart_type: Optional[str] = None) -> str:
    """
    Selects the appropriate chart type based on the data.
    """
    if default_chart_type is not None:
        return default_chart_type

    info = analyze_data(data)

    if info.is_single_value:
        return "gauge"
    if info.is_time_series:
        return "line"
    if info.is_categorical:
        return "bar"

    return "table"
