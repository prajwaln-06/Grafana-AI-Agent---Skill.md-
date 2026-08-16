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
    query_opensearch_logs,
)
from grafana_ai.tools.grafana.dashboard_writing import (
    execute_approved_dashboard,
    propose_dashboard,
    resolve_dashboard_intent,
)

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
