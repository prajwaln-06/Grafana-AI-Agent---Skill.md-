"""Modular Grafana observability wrappers."""
from .wrapper import (
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
