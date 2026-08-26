from chart_selection.normalizers import (
    normalize,
    normalize_prometheus,
    normalize_opensearch,
)
from chart_selection.analyzer import analyze_data
from chart_selection.selector import select_chart_type
from chart_selection.types import ChartAnalysis
from chart_selection.renderer import render_chart


def build_chart(raw_backend_response: dict) -> dict:
    """
    End-to-End Pipeline: Takes raw backend JSON, normalizes it, selects the chart type,
    and returns a complete renderable UI chart configuration.
    """
    normalized = normalize(raw_backend_response)
    analysis = analyze_data(normalized)
    chart_type = select_chart_type(analysis)
    return render_chart(normalized, chart_type)


__all__ = [
    "normalize",
    "normalize_prometheus",
    "normalize_opensearch",
    "analyze_data",
    "select_chart_type",
    "ChartAnalysis",
    "render_chart",
    "build_chart",
]

