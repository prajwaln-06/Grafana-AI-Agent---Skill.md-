"""tests/test_policy.py

Comprehensive test suite verifying RBAC policy enforcement for all roles,
specifically validating that the 'observer' role is strictly denied from:
1. Executing Prometheus queries (via /api/chat, /api/v1/query, /api/query, and wrapper tools).
2. Creating, proposing, modifying, approving, or executing dashboards and panels.
3. Proposing, approving, or executing alert rules.
And that the observer CAN read dashboards and inspect panels.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.main import app
from app.auth.dependencies import require_principal
from app.auth.policy import (
    authorize_alert_approve,
    authorize_alert_execute,
    authorize_alert_propose,
    authorize_dashboard_approve,
    authorize_dashboard_execute,
    authorize_dashboard_propose,
    authorize_dashboard_read,
    authorize_promql,
)
from app.auth.principal import Principal
from app.proposals.store import PROPOSALS


def _make_principal(role: str, active: bool = True) -> Principal:
    return Principal(
        user_id=f"usr_test_{role}",
        issuer="https://authentik.test",
        subject=f"sub_{role}",
        authenticated=True,
        status="active" if active else "pending",
        role=role,
        allowed_folder_uids=frozenset({"*"}),
        allowed_environments=frozenset({"staging", "production"}),
        username=f"user_{role}",
    )


def _valid_ir() -> dict:
    return {
        "name": "Test Dashboard",
        "operation": "create",
        "panels": [
            {
                "title": "CPU Panel",
                "datasource": {"type": "prometheus", "uid": "prom-uid"},
                "visualizationType": "timeseries",
                "query": "node_cpu_seconds_total",
            }
        ],
    }


# ============================================================================
# Unit Tests for Policy Functions
# ============================================================================

class TestPolicyModule:
    """Validate policy function decisions across roles."""

    def test_observer_dashboard_restrictions(self):
        observer = _make_principal("observer")
        
        # Read is allowed
        assert authorize_dashboard_read(observer).allowed is True

        # Mutation operations are strictly denied
        assert authorize_dashboard_propose(observer, operation="create").allowed is False
        assert authorize_dashboard_propose(observer, operation="update").allowed is False
        assert authorize_dashboard_propose(observer, operation="delete").allowed is False
        assert authorize_dashboard_approve(observer).allowed is False
        assert authorize_dashboard_execute(observer).allowed is False

    def test_observer_promql_restrictions(self):
        observer = _make_principal("observer")
        
        # Observers cannot execute PromQL
        dec = authorize_promql(observer, is_custom=True, expr="node_cpu_seconds_total")
        assert dec.allowed is False
        assert "observer" in dec.reason.lower()

        # Even catalog PromQL is denied for observers
        dec_cat = authorize_promql(observer, is_custom=False, expr="catalog_query")
        assert dec_cat.allowed is False

    def test_observer_alert_restrictions(self):
        observer = _make_principal("observer")
        
        assert authorize_alert_propose(observer).allowed is False
        assert authorize_alert_approve(observer).allowed is False
        assert authorize_alert_execute(observer).allowed is False

    def test_maintainer_permissions(self):
        maintainer = _make_principal("maintainer")
        
        # Dashboards: allowed
        assert authorize_dashboard_read(maintainer).allowed is True
        assert authorize_dashboard_propose(maintainer, operation="create").allowed is True
        assert authorize_dashboard_approve(maintainer).allowed is True
        assert authorize_dashboard_execute(maintainer).allowed is True

        # PromQL: allowed
        assert authorize_promql(maintainer, is_custom=True, expr="node_cpu_seconds_total").allowed is True

        # Alerts: Maintainers cannot manage alerts (restricted to Operator / Admin)
        assert authorize_alert_propose(maintainer).allowed is False

    def test_operator_permissions(self):
        operator = _make_principal("operator")
        
        # Dashboards: Operators cannot create or modify dashboards
        assert authorize_dashboard_propose(operator, operation="create").allowed is False

        # Alerts: allowed
        assert authorize_alert_propose(operator).allowed is True
        assert authorize_alert_approve(operator).allowed is True
        assert authorize_alert_execute(operator).allowed is True

        # PromQL: allowed
        assert authorize_promql(operator, is_custom=True, expr="node_cpu_seconds_total").allowed is True

    def test_admin_permissions(self):
        admin = _make_principal("admin")
        
        assert authorize_dashboard_propose(admin, operation="create").allowed is True
        assert authorize_dashboard_approve(admin).allowed is True
        assert authorize_dashboard_execute(admin).allowed is True
        assert authorize_alert_propose(admin).allowed is True
        assert authorize_promql(admin, is_custom=True, expr="node_cpu_seconds_total").allowed is True


# ============================================================================
# Integration Tests for API Endpoints with Observer Role
# ============================================================================

class TestObserverEndpointEnforcement:
    """Validate that API endpoints block observer users from prohibited actions."""

    def test_observer_chat_blocked_from_query(self):
        observer = _make_principal("observer")
        app.dependency_overrides[require_principal] = lambda: observer
        try:
            with TestClient(app) as client:
                res = client.post(
                    "/api/chat",
                    json={"message": "how much cpu is used right now?"},
                )
                assert res.status_code == 200
                data = res.json()
                assert data["status"] == "policy_denied"
                assert "Access Denied" in data["answer"]
                assert "observer" in data["answer"].lower()
        finally:
            app.dependency_overrides.pop(require_principal, None)

    def test_observer_chat_blocked_from_creating_dashboard(self):
        observer = _make_principal("observer")
        app.dependency_overrides[require_principal] = lambda: observer
        try:
            with TestClient(app) as client:
                res = client.post(
                    "/api/chat",
                    json={"message": "create a dashboard for node metrics"},
                )
                assert res.status_code == 200
                data = res.json()
                assert data["status"] == "policy_denied"
                assert "Access Denied" in data["answer"]
                assert "observer" in data["answer"].lower()
        finally:
            app.dependency_overrides.pop(require_principal, None)

    def test_observer_chat_blocked_from_adding_panel(self):
        observer = _make_principal("observer")
        app.dependency_overrides[require_principal] = lambda: observer
        try:
            with TestClient(app) as client:
                res = client.post(
                    "/api/chat",
                    json={"message": "add panel showing memory usage to dashboard"},
                )
                assert res.status_code == 200
                data = res.json()
                assert data["status"] == "policy_denied"
                assert "Access Denied" in data["answer"]
        finally:
            app.dependency_overrides.pop(require_principal, None)

    def test_observer_chat_blocked_from_alert_creation(self):
        observer = _make_principal("observer")
        app.dependency_overrides[require_principal] = lambda: observer
        try:
            with TestClient(app) as client:
                res = client.post(
                    "/api/chat",
                    json={"message": "create alert when memory is > 85% for 5m"},
                )
                assert res.status_code == 200
                data = res.json()
                assert data["status"] == "policy_denied"
                assert "Access Denied" in data["answer"]
        finally:
            app.dependency_overrides.pop(require_principal, None)

    def test_observer_v1_query_endpoint_forbidden(self):
        observer = _make_principal("observer")
        app.dependency_overrides[require_principal] = lambda: observer
        try:
            with TestClient(app) as client:
                res = client.post(
                    "/api/v1/query",
                    json={"question": "node_cpu_seconds_total"},
                )
                assert res.status_code == 403
                assert "policy denied" in res.json()["detail"].lower()
        finally:
            app.dependency_overrides.pop(require_principal, None)

    def test_observer_catalog_query_endpoint_forbidden(self):
        observer = _make_principal("observer")
        app.dependency_overrides[require_principal] = lambda: observer
        try:
            with TestClient(app) as client:
                res = client.post(
                    "/api/query",
                    json={"sourceId": "prometheus", "metricId": "cpu", "queryId": "cpu_utilization"},
                )
                assert res.status_code == 403
                assert "policy denied" in res.json()["detail"].lower()
        finally:
            app.dependency_overrides.pop(require_principal, None)

    def test_observer_direct_proposal_forbidden(self):
        observer = _make_principal("observer")
        app.dependency_overrides[require_principal] = lambda: observer
        try:
            with TestClient(app) as client:
                res = client.post(
                    "/api/proposals",
                    json={"request": "create dashboard for memory"},
                )
                assert res.status_code == 403
                assert "policy denied" in res.json()["detail"].lower()
        finally:
            app.dependency_overrides.pop(require_principal, None)

    def test_observer_approve_proposal_forbidden(self):
        observer = _make_principal("observer")
        app.dependency_overrides[require_principal] = lambda: observer
        try:
            prop = PROPOSALS.create(_valid_ir())
            pid = prop["proposalId"]

            with TestClient(app) as client:
                res = client.post(f"/api/proposals/{pid}/approve", json={"version": 1})
                assert res.status_code == 403
                assert "policy denied" in res.json()["detail"].lower()
        finally:
            app.dependency_overrides.pop(require_principal, None)

    def test_observer_execute_proposal_forbidden(self):
        observer = _make_principal("observer")
        app.dependency_overrides[require_principal] = lambda: observer
        try:
            prop = PROPOSALS.create(_valid_ir())
            pid = prop["proposalId"]

            with TestClient(app) as client:
                res = client.post(
                    f"/api/proposals/{pid}/execute",
                    json={"version": 1, "approvalToken": "fake-token"},
                )
                assert res.status_code == 403
                assert "policy denied" in res.json()["detail"].lower()
        finally:
            app.dependency_overrides.pop(require_principal, None)

    def test_observer_confirm_alert_forbidden(self):
        observer = _make_principal("observer")
        app.dependency_overrides[require_principal] = lambda: observer
        try:
            with TestClient(app) as client:
                res = client.post(
                    "/api/v1/alerts/confirm",
                    json={"session_id": "test_sess", "confirm": True},
                )
                assert res.status_code == 403
                assert "policy denied" in res.json()["detail"].lower()
        finally:
            app.dependency_overrides.pop(require_principal, None)
