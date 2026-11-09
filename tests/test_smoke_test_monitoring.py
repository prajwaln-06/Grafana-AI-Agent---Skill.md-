"""
tests/test_smoke_test_monitoring.py

Unit tests for the Prometheus configuration and monitoring smoke test script.
"""
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
import yaml

from scripts import smoke_test_monitoring

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_prometheus_yaml_syntax_and_config():
    """prometheus.yml must exist and be valid YAML with required scrape targets."""
    prom_yaml_path = REPO_ROOT / "prometheus" / "prometheus.yml"
    assert prom_yaml_path.exists(), "prometheus/prometheus.yml is missing"
    data = yaml.safe_load(prom_yaml_path.read_text(encoding="utf-8"))

    assert "global" in data
    assert data["global"].get("scrape_interval") == "15s"
    assert "scrape_configs" in data

    jobs = {sc["job_name"]: sc for sc in data["scrape_configs"]}
    assert "ai-agent-backend" in jobs
    backend_job = jobs["ai-agent-backend"]
    assert backend_job.get("metrics_path") == "/metrics"


def test_alert_rules_yaml_syntax():
    """alert_rules.yml must exist and be valid YAML with all 4 Golden Signal alerts."""
    rules_path = REPO_ROOT / "prometheus" / "alert_rules.yml"
    assert rules_path.exists(), "prometheus/alert_rules.yml is missing"
    data = yaml.safe_load(rules_path.read_text(encoding="utf-8"))

    assert "groups" in data
    group = data["groups"][0]
    alert_names = {rule["alert"] for rule in group["rules"]}
    assert "AIAgentServiceDown" in alert_names
    assert "AIAgentHighLatency" in alert_names
    assert "AIAgentCriticalLatency" in alert_names
    assert "AIAgentLLMGenerationSlow" in alert_names
    assert "AIAgentDatabaseFetchSlow" in alert_names
    assert "AIAgentTokenBurnSpike" in alert_names
    assert "AIAgentTokenBudgetWarning" in alert_names
    assert "AIAgentRequestSurge" in alert_names
    assert "AIAgentHighServerErrorRate" in alert_names
    assert "AIAgentUpstreamQuotaExhausted" in alert_names
    assert "AIAgentUpstreamProviderOutage" in alert_names
    assert "AIAgentHighHallucinationRate" in alert_names
    assert "AIAgentHighEmptyResultRate" in alert_names
    assert "AIAgentHighInMemorySessionCount" in alert_names
    assert "AIAgentCriticalSessionMemoryLeak" in alert_names
    assert "AIAgentConcurrencySaturation" in alert_names
    assert "AIAgentProcessMemoryHigh" in alert_names


def test_check_agent_metrics_all_present():
    """check_agent_metrics correctly identifies all present metrics."""
    mock_metrics_text = "\n".join(smoke_test_monitoring.REQUIRED_METRICS)
    mock_resp = MagicMock(status_code=200, text=mock_metrics_text)

    with patch("requests.get", return_value=mock_resp):
        results = smoke_test_monitoring.check_agent_metrics("http://localhost:8000")
        assert all(results.values())


def test_check_agent_metrics_handles_unreachable():
    """check_agent_metrics handles connection failure gracefully."""
    with patch("requests.get", side_effect=Exception("Connection refused")):
        results = smoke_test_monitoring.check_agent_metrics("http://localhost:8000")
        assert not any(results.values())


def test_check_prometheus_scraper_healthy():
    """check_prometheus_scraper detects an active, healthy Prometheus scrape target."""
    ready_resp = MagicMock(status_code=200)
    targets_resp = MagicMock(
        status_code=200,
        json=lambda: {
            "data": {
                "activeTargets": [
                    {"labels": {"job": "ai-agent-backend"}, "health": "up"}
                ]
            }
        },
    )

    def side_effect(url, **kwargs):
        if "ready" in url:
            return ready_resp
        return targets_resp

    with patch("requests.get", side_effect=side_effect):
        is_healthy = smoke_test_monitoring.check_prometheus_scraper("http://localhost:9090")
        assert is_healthy is True

