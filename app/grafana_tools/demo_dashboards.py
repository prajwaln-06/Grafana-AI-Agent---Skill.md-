"""app/grafana_tools/demo_dashboards.py

Authoritative demo dashboards used for local development, RBAC testing, and
fallback when external Grafana OSS is offline or unauthenticated.
"""
from __future__ import annotations

DEMO_DASHBOARDS = {
    "dash-checkout-slo": {
        "folder_uid": "fld-payments",
        "title": "Checkout SLO Dashboard",
        "tags": ["payments", "slo", "production"],
        "panels": [
            {
                "title": "Checkout Success Rate (SLO)",
                "type": "timeseries",
                "description": "Measures 2xx vs total checkout endpoints over 5m window",
                "queryCount": 1,
            },
            {
                "title": "p99 Checkout Latency",
                "type": "timeseries",
                "description": "p99 latency for payment processing gateway",
                "queryCount": 1,
            },
            {
                "title": "Payment Errors by Gateway",
                "type": "barchart",
                "description": "Error breakdown across Stripe, PayPal, and Adyen",
                "queryCount": 2,
            },
        ],
    },
    "dash-global-overview": {
        "folder_uid": "afvlix95l3wg0d",
        "title": "Global Services Overview",
        "tags": ["overview", "infrastructure"],
        "panels": [
            {
                "title": "Global HTTP Request Volume",
                "type": "timeseries",
                "description": "Aggregated ingress traffic across all clusters",
                "queryCount": 1,
            },
            {
                "title": "Fleet CPU Utilization",
                "type": "gauge",
                "description": "Cluster-wide average CPU usage percentage",
                "queryCount": 1,
            },
        ],
    },
    "dash-alerts-system": {
        "folder_uid": "fid-alerts",
        "title": "Alertmanager System Telemetry",
        "tags": ["alerts", "telemetry", "observability"],
        "panels": [
            {
                "title": "Alertmanager Notification Latency (p99)",
                "type": "timeseries",
                "description": "End-to-end notification dispatch latency for critical alerts",
                "queryCount": 1,
            },
            {
                "title": "Active Firing Alerts Pipeline",
                "type": "stat",
                "description": "Real-time count of active firing alerts in pipeline",
                "queryCount": 1,
            },
            {
                "title": "Notification Dispatch Rate by Receiver",
                "type": "timeseries",
                "description": "Rate of pagerduty, slack, and webhook dispatches per minute",
                "queryCount": 3,
            },
            {
                "title": "Alert Silence Count by Matcher",
                "type": "table",
                "description": "Currently active silences and expiry times",
                "queryCount": 1,
            },
        ],
    },
    "dash-pci-compliance": {
        "folder_uid": "fld-payments",
        "title": "PCI Compliance Monitoring",
        "tags": ["pci", "security", "restricted"],
        "panels": [
            {
                "title": "Cardholder Data Environment Ingress Logs",
                "type": "logs",
                "description": "CDE access log audit trail",
                "queryCount": 1,
            },
        ],
    },
}
