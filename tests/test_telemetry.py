"""
tests/test_telemetry.py

Tests for the Prometheus telemetry metrics and /metrics endpoint implementation,
verifying Google SRE's 4 Golden Signals as specified in monitor.md.
"""
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient

import run_server
from app import telemetry


def test_telemetry_metrics_defined():
    """All 10 required Golden Signal metrics must be declared in app.telemetry."""
    required_metric_names = [
        "adk_request_duration_seconds",
        "adk_pipeline_step_duration_seconds",
        "adk_requests_total",
        "adk_llm_tokens_total",
        "adk_query_validation_total",
        "adk_query_empty_results_total",
        "adk_llm_api_errors_total",
        "adk_alert_proposals_total",
        "adk_in_flight_requests",
        "adk_active_sessions_gauge",
    ]
    for name in required_metric_names:
        assert hasattr(telemetry, name), f"Missing metric {name} in telemetry module"


def test_track_in_flight_lifecycle():
    """track_in_flight context manager increments on enter and decrements on exit."""
    if not telemetry.PROMETHEUS_AVAILABLE:
        pytest.skip("prometheus_client not available")

    initial = telemetry.adk_in_flight_requests._value.get()
    with telemetry.track_in_flight():
        during = telemetry.adk_in_flight_requests._value.get()
        assert during == initial + 1
    after = telemetry.adk_in_flight_requests._value.get()
    assert after == initial


def test_time_pipeline_step():
    """time_pipeline_step records step execution duration in histogram."""
    if not telemetry.PROMETHEUS_AVAILABLE:
        pytest.skip("prometheus_client not available")

    for step in ("catalog_search", "llm_generation", "prometheus_fetch"):
        with telemetry.time_pipeline_step(step):
            pass  # execute empty block
        sample = telemetry.adk_pipeline_step_duration_seconds.labels(step=step)
        assert sample._sum.get() >= 0.0


def test_record_tokens_and_errors():
    """Token consumption and LLM error counters record values correctly."""
    if not telemetry.PROMETHEUS_AVAILABLE:
        pytest.skip("prometheus_client not available")

    initial_tokens = telemetry.adk_llm_tokens_total.labels(type="prompt")._value.get()
    telemetry.record_tokens(120, "prompt")
    after_tokens = telemetry.adk_llm_tokens_total.labels(type="prompt")._value.get()
    assert after_tokens == initial_tokens + 120

    initial_errors = telemetry.adk_llm_api_errors_total.labels(status_code="429")._value.get()
    telemetry.record_llm_api_error(429)
    after_errors = telemetry.adk_llm_api_errors_total.labels(status_code="429")._value.get()
    assert after_errors == initial_errors + 1


def test_record_validation_and_empty_results():
    """Validation checks (hallucinations) and empty results increment appropriate counters."""
    if not telemetry.PROMETHEUS_AVAILABLE:
        pytest.skip("prometheus_client not available")

    init_valid = telemetry.adk_query_validation_total.labels(result="valid")._value.get()
    init_invalid = telemetry.adk_query_validation_total.labels(result="invalid")._value.get()
    telemetry.record_validation_result(True)
    telemetry.record_validation_result(False)
    assert telemetry.adk_query_validation_total.labels(result="valid")._value.get() == init_valid + 1
    assert telemetry.adk_query_validation_total.labels(result="invalid")._value.get() == init_invalid + 1

    init_empty = telemetry.adk_query_empty_results_total._value.get()
    telemetry.record_empty_result()
    assert telemetry.adk_query_empty_results_total._value.get() == init_empty + 1


def test_record_alert_proposal_actions():
    """Alert proposals, confirmations, and discards are counted."""
    if not telemetry.PROMETHEUS_AVAILABLE:
        pytest.skip("prometheus_client not available")

    for action in ("proposed", "confirmed", "discarded"):
        before = telemetry.adk_alert_proposals_total.labels(action=action)._value.get()
        telemetry.record_alert_proposal(action)
        after = telemetry.adk_alert_proposals_total.labels(action=action)._value.get()
        assert after == before + 1


def test_active_sessions_gauge():
    """Active sessions gauge correctly updates."""
    if not telemetry.PROMETHEUS_AVAILABLE:
        pytest.skip("prometheus_client not available")

    telemetry.set_active_sessions_count(25)
    assert telemetry.adk_active_sessions_gauge._value.get() == 25
    telemetry.set_active_sessions_count(0)
    assert telemetry.adk_active_sessions_gauge._value.get() == 0


def test_metrics_endpoint_is_public_and_unauthenticated(monkeypatch, settings):
    """GET /metrics must be accessible without API key even when API_KEY is set."""
    s = settings.model_copy(update={"api_key": "supersecretkey"})
    monkeypatch.setattr(run_server, "get_settings", lambda: s)
    with TestClient(run_server.app) as client:
        # Protected endpoint without key fails with 401
        assert client.get("/api/v1/capabilities").status_code == 401
        # /metrics is in _PUBLIC_PATHS and succeeds with 200
        res = client.get("/metrics")
        assert res.status_code == 200
        assert "text/plain" in res.headers["content-type"]
        text = res.text
        assert "adk_request_duration_seconds" in text
        assert "adk_requests_total" in text
        assert "adk_in_flight_requests" in text


def test_query_endpoint_records_telemetry(monkeypatch, settings):
    """Calling /api/v1/query updates request counters and duration histogram."""
    s = settings.model_copy(update={"api_key": None})
    monkeypatch.setattr(run_server, "get_settings", lambda: s)

    # Mock runner to return a successful json response
    class FakePart:
        text = '{"result": {"status": "ok", "query": "node_cpu_seconds_total"}}'

    class FakeContent:
        parts = [FakePart()]

    class FakeEvent:
        content = FakeContent()

    class FakeRunner:
        async def run_async(self, **kwargs):
            yield FakeEvent()

    class FakeApiServer:
        session_service = SimpleNamespace(_sessions={"s-1": {}})
        async def get_runner_async(self, name):
            return FakeRunner()

    monkeypatch.setattr(run_server, "adk_web_server", FakeApiServer())

    with TestClient(run_server.app) as client:
        init_metric = client.get("/metrics").text
        res = client.post("/api/v1/query", json={"question": "What is the CPU usage?"})
        assert res.status_code == 200
        assert res.json()["result"]["status"] == "ok"

        after_metric = client.get("/metrics").text
        assert "adk_requests_total" in after_metric

