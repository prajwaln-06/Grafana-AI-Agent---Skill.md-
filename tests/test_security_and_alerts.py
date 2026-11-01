"""API-key + CORS enforcement, the OpenSearch status endpoint, and the alert-confirmation safety fixes
(HTTP endpoint and the chat "yes" path in app/agent.py)."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import run_server
from app import grafana_client


def client_with(monkeypatch, settings, **overrides):
    s = settings.model_copy(update=overrides)
    monkeypatch.setattr(run_server, "get_settings", lambda: s)
    return TestClient(run_server.app)


def test_open_when_no_api_key_is_configured(monkeypatch, settings):
    with client_with(monkeypatch, settings, api_key=None) as c:
        assert c.get("/api/v1/capabilities").status_code == 200


def test_api_key_is_enforced_on_every_route_except_health_probes(monkeypatch, settings):
    with client_with(monkeypatch, settings, api_key="s3cret") as c:
        assert c.get("/api/v1/capabilities").status_code == 401
        assert c.get("/api/v1/capabilities", headers={"X-API-Key": "wrong"}).status_code == 401
        assert c.post("/api/v1/admin/reload-skill").status_code == 401           # the admin route was open before
        assert c.get("/api/v1/opensearch/status").status_code == 401
        assert c.post("/api/v1/query", json={"question": "x"}).status_code == 401
        assert c.get("/api/v1/capabilities", headers={"X-API-Key": "s3cret"}).status_code == 200
        assert c.get("/readyz").status_code == 200


def test_api_key_comparison_is_constant_time():
    import inspect
    assert "hmac.compare_digest" in inspect.getsource(run_server._require_api_key)


def test_cors_preflight_needs_no_key_and_origins_come_from_settings(monkeypatch, settings):
    # CORS middleware is installed at import time from the startup settings (default "*").
    with client_with(monkeypatch, settings, api_key="s3cret") as c:
        r = c.options("/api/v1/query", headers={"Origin": "http://ui.test", "Access-Control-Request-Method": "POST"})
        assert r.status_code == 200 and "access-control-allow-origin" in r.headers


def test_opensearch_status_endpoint_reports_registry_limits_and_caches(monkeypatch, settings):
    with client_with(monkeypatch, settings, api_key=None) as c:
        body = c.get("/api/v1/opensearch/status").json()
    assert body["registry_valid"] is True
    assert {p["pattern_id"] for p in body["patterns"]} == {"fabric_health", "controller_log", "ilo_events"}
    assert all(isinstance(p["analyses"], list) for p in body["patterns"])
    assert body["limits"]["max_hits_size"] == settings.opensearch_max_hits_size
    assert isinstance(body["field_catalogs"], dict)          # nothing fetched: the endpoint never queries OpenSearch


def test_opensearch_status_reports_a_broken_registry_without_crashing(monkeypatch, settings, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    with client_with(monkeypatch, settings, api_key=None, opensearch_registry_path=bad) as c:
        body = c.get("/api/v1/opensearch/status").json()
    assert body["registry_valid"] is False and "patterns" not in body and body["registry_error"]


def test_field_cache_snapshot_never_fetches(monkeypatch, settings):
    from app.opensearch_catalog import runtime
    calls = []
    cache = runtime.get_field_cache(settings)
    monkeypatch.setattr(cache, "_fetch", lambda *a: calls.append(a))
    assert runtime.field_cache_snapshots() == {} and calls == []


def test_startup_readiness_log_reports_a_broken_registry(monkeypatch, settings, tmp_path, caplog):
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    run_server._log_opensearch_readiness(settings.model_copy(update={"opensearch_registry_path": bad}))
    assert "OpenSearch is DISABLED" in caplog.text
    caplog.clear()
    import logging
    with caplog.at_level(logging.INFO, logger="run_server"):
        run_server._log_opensearch_readiness(settings)
    assert "3 enabled index families" in caplog.text


# ---------------- alert confirmation (HTTP endpoint) ----------------

class FakeSessions:
    def __init__(self, state):
        self.session = SimpleNamespace(state=state)
        self.events = []

    async def get_session(self, **kw):
        return self.session

    async def append_event(self, session, event):
        self.events.append(event)


PROPOSAL = {"alert_rule": {"title": "t", "condition_query": "up == 0", "comparison": {"operator": ">", "threshold": 1},
                           "for_duration": "5m", "folder": "f", "datasource_uid": "d"}}


def confirm(monkeypatch, settings, outcome=None, flag=True, state=None, confirm_flag=True):
    s = settings.model_copy(update={"alert_rule_creation_enabled": flag})
    monkeypatch.setattr(run_server, "get_settings", lambda: s)
    sessions = FakeSessions({"pending_alert_proposal": PROPOSAL} if state is None else state)
    monkeypatch.setattr(run_server, "adk_web_server", SimpleNamespace(session_service=sessions))
    if outcome is not None:
        monkeypatch.setattr(grafana_client, "create_alert_rule", lambda **kw: outcome)
    body = run_server.ConfirmAlertRequest(session_id="x", confirm=confirm_flag)
    try:
        return asyncio.run(run_server.confirm_alert(body)), sessions, None
    except HTTPException as e:
        return None, sessions, e


def outcome(status, **kw):
    return SimpleNamespace(status=status, error=kw.get("error"), rule_uid=kw.get("rule_uid"), deeplink=kw.get("deeplink"))


def test_confirm_blocked_when_feature_off_even_with_a_pending_proposal(monkeypatch, settings):
    _, sessions, err = confirm(monkeypatch, settings, outcome("success"), flag=False)
    assert err.status_code == 403 and sessions.events == []


def test_confirm_success_clears_the_proposal(monkeypatch, settings):
    res, sessions, err = confirm(monkeypatch, settings, outcome("success", rule_uid="u", deeplink="l"))
    assert res["status"] == "created" and len(sessions.events) == 1


@pytest.mark.parametrize("status,code", [("endpoint_unreachable", 502), ("configuration_error", 500), ("other", 500)])
def test_confirm_keeps_the_proposal_when_grafana_fails_so_the_user_can_retry(monkeypatch, settings, status, code):
    _, sessions, err = confirm(monkeypatch, settings, outcome(status, error="down"))
    assert err.status_code == code and sessions.events == []


def test_confirm_clears_the_proposal_on_conflict_because_the_rule_already_exists(monkeypatch, settings):
    _, sessions, err = confirm(monkeypatch, settings, outcome("conflict", error="exists"))
    assert err.status_code == 409 and len(sessions.events) == 1


def test_discard_always_works_even_when_the_feature_is_off(monkeypatch, settings):
    res, sessions, _ = confirm(monkeypatch, settings, flag=False, confirm_flag=False)
    assert res == {"status": "discarded"} and len(sessions.events) == 1


def test_confirm_without_a_proposal_is_a_conflict(monkeypatch, settings):
    _, _, err = confirm(monkeypatch, settings, state={})
    assert err.status_code == 409


# ---------------- alert confirmation (chat "yes" path, app/agent.py) ----------------

def _chat_yes(monkeypatch, settings, outcome_value=None, flag=True, raise_exc=None, answer="yes"):
    from app import agent as agent_mod
    s = settings.model_copy(update={"alert_rule_creation_enabled": flag})
    monkeypatch.setattr(agent_mod, "get_settings", lambda: s)
    monkeypatch.setattr(agent_mod.ObservabilityQueryBuilderAgent, "_get_skill_index_or_reload", lambda self: None)

    def create(**kw):
        if raise_exc:
            raise raise_exc
        return outcome_value
    monkeypatch.setattr(grafana_client, "create_alert_rule", create)
    ctx = SimpleNamespace(
        user_content=SimpleNamespace(parts=[SimpleNamespace(text=answer)]),
        session=SimpleNamespace(state={"pending_alert_proposal": PROPOSAL}),
        invocation_id="inv-1")

    async def collect():
        return [e async for e in agent_mod.root_agent._run_async_impl(ctx)]
    events = asyncio.run(collect())
    assert len(events) == 1
    ev = events[0]
    payload = json.loads(ev.content.parts[0].text) if ev.content else None
    return ev, payload


def test_chat_yes_is_refused_and_the_proposal_kept_when_the_feature_is_off(monkeypatch, settings):
    ev, payload = _chat_yes(monkeypatch, settings, outcome("success"), flag=False)
    assert "disabled" in payload["result"]["error"]
    assert not ev.actions.state_delta                       # proposal NOT cleared


def test_chat_yes_keeps_the_proposal_on_a_transient_grafana_failure(monkeypatch, settings):
    ev, payload = _chat_yes(monkeypatch, settings, outcome("endpoint_unreachable", error="down"))
    assert "endpoint_unreachable" in payload["result"]["error"] and not ev.actions.state_delta
    ev, payload = _chat_yes(monkeypatch, settings, raise_exc=RuntimeError("boom"))
    assert "boom" in payload["result"]["error"] and not ev.actions.state_delta


def test_chat_yes_clears_the_proposal_on_success_and_on_conflict(monkeypatch, settings):
    ev, payload = _chat_yes(monkeypatch, settings, outcome("success", rule_uid="u", deeplink="l"))
    assert payload["result"]["status"] == "created" and ev.actions.state_delta.get("pending_alert_proposal", 1) is None
    ev, _ = _chat_yes(monkeypatch, settings, outcome("conflict", error="exists"))
    assert ev.actions.state_delta.get("pending_alert_proposal", 1) is None


def test_chat_discard_clears_the_proposal_even_when_the_feature_is_off(monkeypatch, settings):
    ev, payload = _chat_yes(monkeypatch, settings, flag=False, answer="no")
    assert payload["result"] == {"status": "discarded"} and ev.actions.state_delta.get("pending_alert_proposal", 1) is None
