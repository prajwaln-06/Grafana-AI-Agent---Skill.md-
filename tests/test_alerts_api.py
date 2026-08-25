from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app import grafana_client
from app.config import Settings
from app.session_store import get_session_store


def _client():
    from app.api import main as main_module
    return TestClient(main_module.app)


def _enabled_settings(**overrides):
    base = dict(
        gemini_api_key="test-key", alert_rule_creation_enabled=True,
        grafana_url="http://localhost:3000", grafana_service_account_token="glsa_test",
        grafana_default_folder_uid="alerts-folder", grafana_default_datasource_uid="prom-uid",
    )
    base.update(overrides)
    return Settings(**base)


_ALERT_PROPOSAL = {
    "mode": "single", "status": "alert_rule_proposed",
    "reference_used": "references/node-exporter/cpu.md",
    "measurement_used": {"type": "raw_metric", "name": "node_cpu_seconds_total", "source_metrics": []},
    "data_source": "prometheus",
    "alert_rule": {
        "title": "High CPU utilization",
        "condition_query": '100 - (avg(rate(node_cpu_seconds_total{mode="idle"}[1m])) * 100)',
        "comparison": {"operator": ">", "threshold": 90},
        "for_duration": "5m",
        "folder": "alerts-folder",
        "datasource_uid": None,
    },
    "explanation": "Derived from the verified idle-based CPU utilization expression. Proposal only.",
}


def _seed_session(settings, result=None):
    store = get_session_store(settings.session_ttl_seconds)
    return store.create("alert me if CPU exceeds 90% for 5m", result or _ALERT_PROPOSAL)


def test_confirm_disabled_on_this_deployment_returns_403():
    disabled = Settings(gemini_api_key="test-key", alert_rule_creation_enabled=False)
    session_id = _seed_session(disabled)
    with patch("app.api.routes_alerts.get_settings", return_value=disabled):
        with _client() as client:
            r = client.post("/api/v1/alerts/confirm", json={"session_id": session_id})
    assert r.status_code == 403


def test_confirm_unknown_session_returns_410():
    enabled = _enabled_settings()
    with patch("app.api.routes_alerts.get_settings", return_value=enabled):
        with _client() as client:
            r = client.post("/api/v1/alerts/confirm", json={"session_id": "does-not-exist"})
    assert r.status_code == 410


def test_confirm_session_that_is_not_an_alert_proposal_returns_409():
    enabled = _enabled_settings()
    non_alert_result = {"mode": "single", "status": "ambiguous_metric", "candidates": [], "clarification": "?"}
    session_id = _seed_session(enabled, result=non_alert_result)
    with patch("app.api.routes_alerts.get_settings", return_value=enabled):
        with _client() as client:
            r = client.post("/api/v1/alerts/confirm", json={"session_id": session_id})
    assert r.status_code == 409


def test_confirm_false_discards_without_calling_grafana():
    enabled = _enabled_settings()
    session_id = _seed_session(enabled)
    with patch("app.api.routes_alerts.get_settings", return_value=enabled), \
         patch("app.api.routes_alerts.grafana_client.create_alert_rule") as mock_create:
        with _client() as client:
            r = client.post("/api/v1/alerts/confirm", json={"session_id": session_id, "confirm": False})
    assert r.status_code == 200
    assert r.json()["status"] == "discarded"
    assert not mock_create.called


def test_confirm_true_creates_rule_and_returns_deeplink():
    enabled = _enabled_settings()
    session_id = _seed_session(enabled)
    fake_outcome = grafana_client.AlertRuleOutcome(
        status="success", rule_uid="abc123",
        deeplink="http://localhost:3000/alerting/grafana/abc123/view",
    )
    with patch("app.api.routes_alerts.get_settings", return_value=enabled), \
         patch("app.api.routes_alerts.grafana_client.create_alert_rule", return_value=fake_outcome) as mock_create:
        with _client() as client:
            r = client.post("/api/v1/alerts/confirm", json={"session_id": session_id})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "created"
    assert body["rule_uid"] == "abc123"
    assert body["deeplink"] == "http://localhost:3000/alerting/grafana/abc123/view"

    # Confirms the confirmation endpoint resolves datasource_uid from
    # deployment config at confirmation time (Section 12.5), never trusting
    # whatever (null) value sat in the stored proposal.
    _, call_kwargs = mock_create.call_args
    assert call_kwargs["datasource_uid"] == "prom-uid"
    assert call_kwargs["folder_uid"] == "alerts-folder"
    assert call_kwargs["title"] == "High CPU utilization"
    assert call_kwargs["comparison_operator"] == ">"
    assert call_kwargs["threshold"] == 90
    assert call_kwargs["for_duration"] == "5m"


def test_confirm_is_single_use_second_call_returns_410():
    enabled = _enabled_settings()
    session_id = _seed_session(enabled)
    fake_outcome = grafana_client.AlertRuleOutcome(status="success", rule_uid="abc123", deeplink="http://x/y")
    with patch("app.api.routes_alerts.get_settings", return_value=enabled), \
         patch("app.api.routes_alerts.grafana_client.create_alert_rule", return_value=fake_outcome):
        with _client() as client:
            r1 = client.post("/api/v1/alerts/confirm", json={"session_id": session_id})
            assert r1.status_code == 200
            r2 = client.post("/api/v1/alerts/confirm", json={"session_id": session_id})
    assert r2.status_code == 410


def test_confirm_grafana_configuration_error_returns_500():
    enabled = _enabled_settings()
    session_id = _seed_session(enabled)
    fake_outcome = grafana_client.AlertRuleOutcome(status="configuration_error", error="missing token")
    with patch("app.api.routes_alerts.get_settings", return_value=enabled), \
         patch("app.api.routes_alerts.grafana_client.create_alert_rule", return_value=fake_outcome):
        with _client() as client:
            r = client.post("/api/v1/alerts/confirm", json={"session_id": session_id})
    assert r.status_code == 500


def test_confirm_grafana_unreachable_returns_502():
    enabled = _enabled_settings()
    session_id = _seed_session(enabled)
    fake_outcome = grafana_client.AlertRuleOutcome(status="endpoint_unreachable", error="connection refused")
    with patch("app.api.routes_alerts.get_settings", return_value=enabled), \
         patch("app.api.routes_alerts.grafana_client.create_alert_rule", return_value=fake_outcome):
        with _client() as client:
            r = client.post("/api/v1/alerts/confirm", json={"session_id": session_id})
    assert r.status_code == 502


def test_confirm_conflict_returns_409():
    enabled = _enabled_settings()
    session_id = _seed_session(enabled)
    fake_outcome = grafana_client.AlertRuleOutcome(status="conflict", error="rule already exists")
    with patch("app.api.routes_alerts.get_settings", return_value=enabled), \
         patch("app.api.routes_alerts.grafana_client.create_alert_rule", return_value=fake_outcome):
        with _client() as client:
            r = client.post("/api/v1/alerts/confirm", json={"session_id": session_id})
    assert r.status_code == 409


def test_confirm_finds_alert_proposal_nested_in_multi_mode_result():
    """SKILL.md Section 12.5's mode:"single" guarantee is about the status's
    own shape; a compound question can still merge it with an unrelated
    unmapped entry (pipeline.py's unresolved-topics merge). Confirms the
    endpoint still finds it."""
    enabled = _enabled_settings()
    multi_result = {
        "mode": "multi",
        "results": [
            {"status": "unmapped", "explanation": "no reference covers this other topic"},
            _ALERT_PROPOSAL,
        ],
        "synthesis": None,
    }
    session_id = _seed_session(enabled, result=multi_result)
    fake_outcome = grafana_client.AlertRuleOutcome(status="success", rule_uid="abc123", deeplink="http://x/y")
    with patch("app.api.routes_alerts.get_settings", return_value=enabled), \
         patch("app.api.routes_alerts.grafana_client.create_alert_rule", return_value=fake_outcome):
        with _client() as client:
            r = client.post("/api/v1/alerts/confirm", json={"session_id": session_id})
    assert r.status_code == 200
    assert r.json()["status"] == "created"
