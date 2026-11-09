"""
tests/test_grafana_dashboard.py

Validates the Grafana Dashboard JSON specification and auto-provisioning configs
for the AI Agent's 4 Golden Signals monitoring suite.
"""
from __future__ import annotations

import json
from pathlib import Path
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARDS_DIR = REPO_ROOT / "grafana" / "dashboards"
PROVISIONING_DIR = REPO_ROOT / "grafana" / "provisioning"
DASHBOARD_PATH = DASHBOARDS_DIR / "ai_agent_golden_signals.json"
DATASOURCE_PROVISIONING = PROVISIONING_DIR / "datasources" / "datasource.yml"
DASHBOARD_PROVISIONING = PROVISIONING_DIR / "dashboards" / "dashboard.yml"


def test_dashboard_file_exists_and_is_valid_json():
    """Asserts that the dashboard JSON exists and parses cleanly."""
    assert DASHBOARD_PATH.exists(), f"Dashboard JSON missing at {DASHBOARD_PATH}"
    data = json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert data["uid"] == "ai-agent-golden-signals"
    assert "AI Agent" in data["title"]
    assert "golden-signals" in data["tags"]
    assert data["schemaVersion"] >= 36


def test_dashboard_contains_all_four_golden_signal_rows():
    """Asserts that all 4 Golden Signal rows exist in the dashboard."""
    data = json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))
    rows = [p for p in data.get("panels", []) if p.get("type") == "row"]
    row_titles = [r.get("title", "") for r in rows]

    assert any("Latency" in title for title in row_titles), "Latency row missing"
    assert any("Traffic" in title for title in row_titles), "Traffic row missing"
    assert any("Errors" in title for title in row_titles), "Errors row missing"
    assert any("Saturation" in title for title in row_titles), "Saturation row missing"


def test_dashboard_panels_cover_all_telemetry_metrics():
    """Asserts that panels execute PromQL queries covering all 10 Golden Signal metrics."""
    data = json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))
    panels = [p for p in data.get("panels", []) if p.get("type") != "row"]
    
    all_exprs: list[str] = []
    for panel in panels:
        targets = panel.get("targets", [])
        for target in targets:
            expr = target.get("expr", "")
            if expr:
                all_exprs.append(expr)

    combined_exprs = " ".join(all_exprs)

    # 1. Latency metrics
    assert "adk_request_duration_seconds" in combined_exprs
    assert "adk_pipeline_step_duration_seconds" in combined_exprs

    # 2. Traffic metrics
    assert "adk_requests_total" in combined_exprs
    assert "adk_llm_tokens_total" in combined_exprs

    # 3. Error & Quality metrics
    assert "adk_query_validation_total" in combined_exprs
    assert "adk_query_empty_results_total" in combined_exprs
    assert "adk_llm_api_errors_total" in combined_exprs
    assert "adk_alert_proposals_total" in combined_exprs

    # 4. Saturation metrics
    assert "adk_in_flight_requests" in combined_exprs
    assert "adk_active_sessions_gauge" in combined_exprs
    assert "process_resident_memory_bytes" in combined_exprs


def test_dashboard_visualizations_and_sla_thresholds():
    """Verifies that panels have expected visualization types and threshold standards."""
    data = json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))
    panels = {p["id"]: p for p in data.get("panels", []) if p.get("type") != "row"}

    # Latency Stat panel (Panel 1) - p95 threshold at 4s and 8s
    p1 = panels[1]
    assert p1["type"] == "stat"
    steps = p1["fieldConfig"]["defaults"]["thresholds"]["steps"]
    values = [s["value"] for s in steps if s["value"] is not None]
    assert 4 in values
    assert 8 in values

    # Token Burn Gauge panel (Panel 5) - thresholds at 500k and 1M
    p5 = panels[5]
    assert p5["type"] == "gauge"
    steps_5 = p5["fieldConfig"]["defaults"]["thresholds"]["steps"]
    values_5 = [s["value"] for s in steps_5 if s["value"] is not None]
    assert 500000 in values_5
    assert 1000000 in values_5

    # Memory RSS timeseries (Panel 13)
    p13 = panels[13]
    assert p13["type"] == "timeseries"


def test_datasource_provisioning_config():
    """Verifies datasource.yml auto-provisions Prometheus."""
    assert DATASOURCE_PROVISIONING.exists(), f"Datasource provisioning missing at {DATASOURCE_PROVISIONING}"
    content = yaml.safe_load(DATASOURCE_PROVISIONING.read_text(encoding="utf-8"))
    assert content.get("apiVersion") == 1
    datasources = content.get("datasources", [])
    assert len(datasources) >= 1
    prom = datasources[0]
    assert prom["type"] == "prometheus"
    assert prom["uid"] == "prometheus"
    assert "9090" in prom["url"] and ("prometheus" in prom["url"] or "host.docker.internal" in prom["url"])
    assert prom["isDefault"] is True


def test_dashboard_provisioning_provider():
    """Verifies dashboard.yml auto-provisions dashboards."""
    assert DASHBOARD_PROVISIONING.exists(), f"Dashboard provisioning missing at {DASHBOARD_PROVISIONING}"
    content = yaml.safe_load(DASHBOARD_PROVISIONING.read_text(encoding="utf-8"))
    assert content.get("apiVersion") == 1
    providers = content.get("providers", [])
    assert len(providers) >= 1
    provider = providers[0]
    assert provider["type"] == "file"
    assert provider["options"]["path"] == "/etc/grafana/dashboards"

