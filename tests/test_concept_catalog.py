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
    """Scenario 2: When multiple metrics match, present numbered options with descriptions."""
    explanation = explain_disambiguation("disk", sorted(MOCK_LIVE_METRICS))
    assert "I found" in explanation
    assert "node_filesystem_avail_bytes" in explanation
    assert "Tip: You can reply with the number" in explanation


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
