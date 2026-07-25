"""
Compatibility adapter for existing agents.
Forwards all public wrapper calls to the new modular architecture.
"""
from grafana_ai.tools.grafana.wrapper import (
    list_dashboards,
    search_dashboards,
    get_dashboard_panels,
    query_prometheus_metric,
    query_logs,
)

__all__ = [
    "list_dashboards",
    "search_dashboards",
    "get_dashboard_panels",
    "query_prometheus_metric",
    "query_logs",
]