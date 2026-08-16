"""Modular Grafana observability wrappers."""
from .wrapper import (
    list_dashboards,
    search_dashboards,
    get_dashboard_panels,
    query_prometheus_metric,
    query_logs,
    query_opensearch_logs,
)
from .dashboard_writing import propose_dashboard, execute_approved_dashboard, resolve_dashboard_intent

__all__ = [
    "list_dashboards",
    "search_dashboards",
    "get_dashboard_panels",
    "query_prometheus_metric",
    "query_logs",
    "query_opensearch_logs",
    "propose_dashboard",
    "execute_approved_dashboard",
    "resolve_dashboard_intent",
]
