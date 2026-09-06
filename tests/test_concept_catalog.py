"""Tests for Concept Catalog and Smart Metric Resolution."""
import pytest

from app.grafana_tools.concept_catalog import (
    CONCEPTS,
    explain_disambiguation,
    explain_not_found,
    extract_visualization_override,
    resolve_concept_panels,
)
from app.grafana_tools.promql import build_promql, infer_unit


# Sample realistic Prometheus live metrics set
MOCK_LIVE_METRICS = {
    "node_cpu_seconds_total",
    "node_memory_MemAvailable_bytes",
    "node_memory_MemTotal_bytes",
    "node_filesystem_avail_bytes",
    "node_filesystem_size_bytes",
    "node_network_receive_bytes_total",
    "node_network_transmit_bytes_total",
    "node_load1",
    "node_boot_time_seconds",
    "DCGM_FI_DEV_GPU_TEMP",
    "DCGM_FI_DEV_GPU_UTIL",
    "DCGM_FI_DEV_FB_USED",
    "up",
}


def test_concepts_structure():
    """Verify that all concepts in the catalog have required attributes."""
    assert len(CONCEPTS) >= 6
    for concept in CONCEPTS:
        assert "id" in concept
        assert "title" in concept
        assert "synonyms" in concept and len(concept["synonyms"]) > 0
        assert "candidates" in concept and len(concept["candidates"]) > 0
        assert "default_viz" in concept
        assert "description" in concept


def test_resolve_memory_concept():
    """'add memory panel to fleet overview' resolves to node_memory_MemAvailable_bytes."""
    panels = resolve_concept_panels("add memory panel to fleet overview", MOCK_LIVE_METRICS)
    assert len(panels) == 1
    p = panels[0]
    assert p["metric"] == "node_memory_MemAvailable_bytes"
    assert p["title"] == "Memory Utilization"
    assert p["visualization"] == "timeseries"
    assert p["conceptId"] == "memory"


def test_resolve_ram_with_visualization_override():
    """'add ram gauge panel' resolves to gauge visualization."""
    panels = resolve_concept_panels("add ram gauge panel to fleet overview", MOCK_LIVE_METRICS)
    assert len(panels) == 1
    p = panels[0]
    assert p["metric"] == "node_memory_MemAvailable_bytes"
    assert p["visualization"] == "gauge"


def test_resolve_gpu_temp():
    """'add gpu temp panel' resolves to DCGM_FI_DEV_GPU_TEMP with gauge viz."""
    panels = resolve_concept_panels("add gpu temp panel to fleet overview", MOCK_LIVE_METRICS)
    assert len(panels) == 1
    p = panels[0]
    assert p["metric"] == "DCGM_FI_DEV_GPU_TEMP"
    assert p["title"] == "GPU Temperature"
    assert p["visualization"] == "gauge"


def test_dcgm_query_triggers_disambiguation():
    """Asking for a generic DCGM panel presents disambiguation choices without defaulting."""
    explanation = explain_disambiguation("create a dcgm panel", sorted(MOCK_LIVE_METRICS))
    assert "I found" in explanation
    assert "DCGM_FI_DEV_GPU_UTIL" in explanation
    assert "DCGM_FI_DEV_GPU_TEMP" in explanation


def test_resolve_multiple_concepts():
    """'create dashboard for cpu and memory' resolves both concepts in prompt order."""
    panels = resolve_concept_panels("create dashboard for cpu and memory", MOCK_LIVE_METRICS)
    assert len(panels) == 2
    metrics = [p["metric"] for p in panels]
    assert metrics == ["node_cpu_seconds_total", "node_memory_MemAvailable_bytes"]


def test_candidate_fallback_when_primary_missing():
    """If primary candidate is missing, fall back to second candidate."""
    restricted_metrics = {
        "container_memory_working_set_bytes",
        "node_load1",
    }
    panels = resolve_concept_panels("show memory usage", restricted_metrics)
    assert len(panels) == 1
    assert panels[0]["metric"] == "container_memory_working_set_bytes"


def test_no_hallucination_when_no_candidates_in_cluster():
    """If none of the candidates exist in live metrics, concept should not match."""
    empty_metrics: set[str] = set()
    panels = resolve_concept_panels("add memory panel", empty_metrics)
    assert panels == []


def test_explain_disambiguation_multiple_options():
    """Scenario 2: When multiple metrics match, present numbered options with rich SRE descriptions."""
    explanation = explain_disambiguation("disk", sorted(MOCK_LIVE_METRICS))
    assert "I found" in explanation
    assert "node_filesystem_avail_bytes" in explanation
    assert "Tip: You can click an option directly" in explanation
    # Must use rich descriptions rather than lazy fallback
    assert "Prometheus metric:" not in explanation


def test_get_disambiguation_candidates():
    """Verify that get_disambiguation_candidates returns live metrics prioritized by relevance."""
    from app.grafana_tools.concept_catalog import get_disambiguation_candidates
    candidates = get_disambiguation_candidates("gpu", sorted(MOCK_LIVE_METRICS))
    assert len(candidates) >= 2
    # Primary operational metrics should come first
    assert candidates[0] == "DCGM_FI_DEV_GPU_UTIL"
    assert candidates[1] == "DCGM_FI_DEV_GPU_TEMP"


def test_explain_disambiguation_dcgm_metrics_rich_explanation():
    """Verify DCGM metric disambiguation provides human-readable titles and proper SRE explanations."""
    dcgm_metrics = [
        "DCGM_FI_DEV_ECC_DBE_VOL_TOTAL",
        "DCGM_FI_DEV_FB_FREE",
        "DCGM_FI_DEV_GPU_TEMP",
        "DCGM_FI_DEV_GPU_UTIL",
    ]
    explanation = explain_disambiguation("dcgm", dcgm_metrics)
    assert "GPU Utilization" in explanation
    assert "GPU Temperature" in explanation
    assert "GPU VRAM Free" in explanation
    assert "Double-Bit ECC Errors" in explanation
    assert "Prometheus metric:" not in explanation
    # Core utilization/temp should be ordered before ECC hardware counters
    assert explanation.find("GPU Utilization") < explanation.find("Double-Bit ECC Errors")


def test_explain_not_found_unmonitored_service():
    """Scenario 3: When an unmonitored service is requested, show discovered services."""
    explanation = explain_not_found("redis cache queries", sorted(MOCK_LIVE_METRICS))
    assert "Could not find" in explanation
    assert "Currently monitored services on your cluster:" in explanation
    assert "Linux Host Metrics (`node_exporter`)" in explanation
    assert "NVIDIA GPU Metrics (`dcgm-exporter`)" in explanation


def test_promql_formulas_for_catalog_metrics():
    """Verify that PromQL queries generated for catalog metrics are valid and complete."""
    # Memory formula calculates percentage used
    mem_q = build_promql("node_memory_MemAvailable_bytes", "instance", "node-01")
    assert "100 * (1 - node_memory_MemAvailable_bytes" in mem_q
    assert "node_memory_MemTotal_bytes" in mem_q

    # CPU formula calculates percentage utilized by inverting idle
    cpu_q = build_promql("node_cpu_seconds_total", "instance", "node-01")
    assert "100 - (avg by (instance) (rate(node_cpu_seconds_total" in cpu_q
    assert 'mode="idle"' in cpu_q

    # GPU Temp is raw gauge
    gpu_q = build_promql("DCGM_FI_DEV_GPU_TEMP", "instance", "node-01")
    assert 'DCGM_FI_DEV_GPU_TEMP{instance="node-01"}' == gpu_q

    # Network transmit
    net_q = build_promql("node_network_transmit_bytes_total", "instance", "node-01")
    assert "rate(node_network_transmit_bytes_total" in net_q
    assert 'device!~"lo"' in net_q

    # Uptime
    uptime_q = build_promql("node_boot_time_seconds", "instance", "node-01")
    assert "time() - node_boot_time_seconds" in uptime_q


def test_numeric_selection_from_clarification():
    """Verify that replying with '5' resolves candidate 5 from pending clarification."""
    from fastapi.testclient import TestClient
    from app.api.main import app
    from app.api.routes_chat import SESSION_STORE

    client = TestClient(app)
    session_id = "test_num_selection_session"
    session = SESSION_STORE.get_or_create(session_id)
    session.pending_action = "awaiting_metric_for_dashboard"
    session.pending_payload = {
        "dashboard_request": "add gpu metric to observability overview",
        "target": "node-01",
        "time_range": "1h",
        "candidates": [
            "DCGM_FI_DEV_FB_FREE",
            "DCGM_FI_DEV_FB_USED",
            "DCGM_FI_DEV_SM_CLOCK",
            "DCGM_FI_DEV_POWER_USAGE",
            "DCGM_FI_DEV_GPU_TEMP",
            "DCGM_FI_DEV_GPU_UTIL",
        ],
    }
    # User sends "5"
    resp = client.post("/api/chat", json={"message": "5", "sessionId": session_id})
    assert resp.status_code == 200
    data = resp.json()
    # It must not say "no discernible observability intent"
    assert "no discernible observability intent" not in data["answer"].lower()
    # It should have resolved to option 5: DCGM_FI_DEV_GPU_TEMP
    assert "DCGM_FI_DEV_GPU_TEMP" in data["answer"] or data.get("proposalId") is not None


def test_delete_dashboard_proposal():
    """Verify that 'delete dashboard Observability Overview' generates a proper delete proposal."""
    from fastapi.testclient import TestClient
    from app.api.main import app

    client = TestClient(app)
    resp = client.post(
        "/api/chat",
        json={"message": "Delete dashboard Observability Overview", "sessionId": "test_del_session"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "proposalId" in data
    assert data["proposalId"] is not None
    assert "proposal to delete" in data["answer"].lower()
    assert "Observability Overview" in data["answer"]
    # Ensure it didn't default to generic create wording
    assert "here is the proposed dashboard" not in data["answer"].lower()
    prop = data.get("proposal", {})
    ir = prop.get("ir", {})
    assert ir.get("removeDashboard") is True
    assert ir.get("operation") == "remove"

