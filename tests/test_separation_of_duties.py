"""tests/test_separation_of_duties.py

Regression tests for the proposal separation-of-duties rules.

The rules under test:

* the proposer identity is derived from the authenticated principal and is
  immutable once the proposal exists;
* the impact classification is derived from the proposal's operation, never
  from anything the client supplies;
* a destructive proposal cannot be approved by its own proposer, and the
  approver must have the right role and the right resource scope;
* an approval records who gave it;
* the approval is re-validated immediately before execution, and a payload
  that changed since the approval invalidates it;
* the HTTP API, the MCP proposal server and the ADK agent enforce the same
  rules;
* an unauthorized operation causes zero Grafana writes.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app.auth.principal import CURRENT_PRINCIPAL, Principal, set_current_principal
from tests.test_rbac_scope_enforcement import (
    ENV_ALLOWED,
    FOLDER_A,
    FOLDER_B,
    FakeGrafana,
    acting_as,
    make_principal,
)


@pytest.fixture()
def grafana(monkeypatch) -> FakeGrafana:
    """The deterministic Grafana fake (shared with the scope-enforcement suite)."""
    fake = FakeGrafana()

    import app.grafana_tools.alerting as alerting
    import app.grafana_tools.dashboard as dashboard_module
    import app.grafana_tools.dashboard_writing as writing
    import app.grafana_tools.wrapper as wrapper

    monkeypatch.setattr(dashboard_module, "fetch_dashboard_list", fake.list_dashboards)
    monkeypatch.setattr(dashboard_module, "fetch_dashboard_detail", fake.detail)
    monkeypatch.setattr(dashboard_module, "get_dashboard_summary_mcp", fake.summary)
    monkeypatch.setattr(dashboard_module, "search_dashboards_mcp", fake.search)

    monkeypatch.setattr(wrapper, "fetch_dashboard_list", fake.list_dashboards)
    monkeypatch.setattr(wrapper, "get_dashboard_summary_mcp", fake.summary)
    monkeypatch.setattr(wrapper, "search_dashboards_mcp", fake.search)

    monkeypatch.setattr(writing, "fetch_dashboard_list", fake.list_dashboards)
    monkeypatch.setattr(writing, "fetch_dashboard_detail", fake.detail)
    monkeypatch.setattr(writing, "mcp_call", fake.mcp_call)

    import app.grafana_tools.discovery as discovery
    monkeypatch.setattr(discovery, "fetch_dashboard_list", fake.list_dashboards)
    monkeypatch.setattr(discovery, "fetch_dashboard_detail", fake.detail)

    monkeypatch.setattr(alerting, "mcp_call", fake.mcp_call)

    monkeypatch.setattr(
        writing, "resolve_datasource",
        _async_return({"uid": "prom-uid", "type": "prometheus", "name": "Prometheus"}),
    )
    monkeypatch.setattr(
        writing, "list_prometheus_metric_names",
        _async_return(json.dumps(["node_load1", "node_cpu_seconds_total"])),
    )
    monkeypatch.setattr(
        writing, "list_prometheus_label_names",
        _async_return(json.dumps(["instance", "job"])),
    )
    monkeypatch.setattr(
        writing, "list_prometheus_label_values",
        _async_return(json.dumps(["node-01", "node-02"])),
    )
    monkeypatch.setattr(writing, "list_prometheus_metric_metadata", _async_return("{}"))

    async def fake_execute_prometheus(expr: str, *args, **kwargs) -> str:
        fake.prometheus_queries.append(expr)
        return json.dumps({"status": "success", "data": {"result": []}})

    monkeypatch.setattr(writing, "execute_prometheus", fake_execute_prometheus)
    return fake


def _async_return(value):
    async def _inner(*args, **kwargs):
        return value
    return _inner


def run(coro):
    return asyncio.run(coro)


PROPOSER = make_principal(
    "maintainer", user_id="usr_sod_proposer", folders=(FOLDER_A,),
    dashboards=("dash-alpha",), environments=(ENV_ALLOWED,),
)
OTHER_MAINTAINER = make_principal(
    "maintainer", user_id="usr_sod_other", folders=(FOLDER_A,),
    dashboards=("dash-alpha",), environments=(ENV_ALLOWED,),
)
OUT_OF_SCOPE_MAINTAINER = make_principal(
    "maintainer", user_id="usr_sod_outofscope", folders=(FOLDER_B,),
    environments=(ENV_ALLOWED,),
)
ADMIN_PRINCIPAL = make_principal(
    "admin", user_id="usr_sod_admin", folders=("*",), environments=(ENV_ALLOWED,),
)
OBSERVER_PRINCIPAL = make_principal(
    "observer", user_id="usr_sod_observer", folders=(FOLDER_A,),
    dashboards=("dash-alpha",), environments=(ENV_ALLOWED,),
)
OPERATOR_PRINCIPAL = make_principal(
    "operator", user_id="usr_sod_operator", folders=(FOLDER_B,),
    environments=(ENV_ALLOWED,),
)


def destructive_ir() -> dict:
    return {
        "operation": "remove",
        "name": "Alpha Service Overview",
        "dashboardUid": "dash-alpha",
        "folderUid": FOLDER_A,
        "removeDashboard": True,
        "panels": [],
    }


def routine_ir() -> dict:
    return {
        "operation": "create",
        "name": "Brand New Dashboard",
        "folderUid": FOLDER_A,
        "panels": [{
            "title": "Panel",
            "datasource": {"type": "prometheus", "uid": "prom-uid"},
            "visualizationType": "timeseries",
            "query": "node_load1",
        }],
    }


@contextmanager
def api_as(principal: Principal):
    """Bind a principal to the API under test, exactly as the auth dependency does."""
    from app.api.main import app
    from app.auth.dependencies import require_principal

    app.dependency_overrides[require_principal] = lambda: principal
    token = set_current_principal(principal)
    try:
        yield
    finally:
        app.dependency_overrides.pop(require_principal, None)
        try:
            CURRENT_PRINCIPAL.reset(token)
        except (ValueError, LookupError):  # pragma: no cover - defensive
            set_current_principal(None)


# ===========================================================================
# A. Proposer identity
# ===========================================================================

class TestProposerIdentity:
    """The proposer is the authenticated principal, and stays that way."""

    def test_the_proposer_is_the_authenticated_principal(self, grafana):
        from app.grafana_tools.dashboard_writing import PROPOSALS

        with acting_as(PROPOSER):
            proposal = PROPOSALS.create(destructive_ir())
        assert proposal["proposer_id"] == PROPOSER.user_id

    def test_a_client_supplied_proposer_is_ignored(self, grafana):
        from app.grafana_tools.dashboard_writing import PROPOSALS

        ir = routine_ir()
        ir["proposer_id"] = "usr_somebody_else"
        ir["owner"] = {"userId": "usr_somebody_else"}
        with acting_as(PROPOSER):
            proposal = PROPOSALS.create(ir)
        assert proposal["proposer_id"] == PROPOSER.user_id

    def test_the_proposer_is_immutable_after_a_modification(self, grafana):
        """Editing somebody else's proposal does not re-attribute it."""
        from app.grafana_tools.dashboard_writing import PROPOSALS

        with acting_as(PROPOSER):
            proposal = PROPOSALS.create(destructive_ir())
        with acting_as(OTHER_MAINTAINER):
            modified = PROPOSALS.modify(proposal["proposalId"], destructive_ir())
        assert modified["proposer_id"] == PROPOSER.user_id


# ===========================================================================
# B. Impact classification
# ===========================================================================

class TestImpactClassification:
    """The impact class is computed server-side."""

    def test_a_removal_is_destructive(self, grafana):
        from app.grafana_tools.dashboard_writing import PROPOSALS

        with acting_as(PROPOSER):
            proposal = PROPOSALS.create(destructive_ir())
        assert proposal["impact_class"] == "destructive_delete"
        assert proposal["requires_second_approver"] is True

    def test_a_client_supplied_impact_class_cannot_downgrade_a_removal(self, grafana):
        from app.grafana_tools.dashboard_writing import PROPOSALS

        ir = destructive_ir()
        ir["impact_class"] = "routine_edit"
        ir["requires_second_approver"] = False
        with acting_as(PROPOSER):
            proposal = PROPOSALS.create(ir)
        assert proposal["impact_class"] == "destructive_delete"
        assert proposal["requires_second_approver"] is True

    def test_a_client_cannot_disable_the_second_approver_over_http(self, grafana):
        """The modify endpoint cannot downgrade a destructive proposal."""
        from app.grafana_tools.dashboard_writing import PROPOSALS

        with acting_as(PROPOSER):
            proposal = PROPOSALS.create(destructive_ir())

        ir = destructive_ir()
        ir["impact_class"] = "routine_edit"
        ir["requires_second_approver"] = False
        with api_as(PROPOSER):
            with TestClient(_app()) as client:
                response = client.put(
                    f"/api/proposals/{proposal['proposalId']}", json={"ir": ir}
                )
        assert response.status_code == 200
        assert response.json()["impact_class"] == "destructive_delete"
        assert response.json()["requires_second_approver"] is True

    def test_classification_is_recomputed_from_the_operation(self):
        from app.auth.policy import classify_impact

        assert classify_impact("remove", {}) == "destructive_delete"
        assert classify_impact("delete", {}) == "destructive_delete"
        assert classify_impact("update", {}) == "routine_edit"
        assert classify_impact("create", {}) == "routine_edit"
        # An operation of "remove" wins over a contradictory "operation" key
        # inside the payload.
        assert classify_impact("remove", {"operation": "update"}) == "destructive_delete"


# ===========================================================================
# C. Second approval
# ===========================================================================

class TestSecondApproval:
    """A destructive proposal needs an approver other than its proposer."""

    def test_the_proposer_cannot_approve_their_own_destructive_proposal(self, grafana):
        from app.grafana_tools.dashboard_writing import PROPOSALS

        with acting_as(PROPOSER):
            proposal = PROPOSALS.create(destructive_ir())
            with pytest.raises(PermissionError):
                PROPOSALS.approve(
                    proposal["proposalId"],
                    proposal["version"],
                    approver_id=PROPOSER.user_id,
                    approver_role="maintainer",
                )

    def test_the_proposer_is_refused_over_http(self, grafana):
        from app.grafana_tools.dashboard_writing import PROPOSALS

        with acting_as(PROPOSER):
            proposal = PROPOSALS.create(destructive_ir())

        with api_as(PROPOSER):
            with TestClient(_app()) as client:
                response = client.post(
                    f"/api/proposals/{proposal['proposalId']}/approve",
                    json={"version": proposal["version"]},
                )
        assert response.status_code == 403
        assert "second" in response.json()["detail"].lower()

    def test_another_eligible_maintainer_can_approve(self, grafana):
        from app.grafana_tools.dashboard_writing import PROPOSALS

        with acting_as(PROPOSER):
            proposal = PROPOSALS.create(destructive_ir())

        with api_as(OTHER_MAINTAINER):
            with TestClient(_app()) as client:
                response = client.post(
                    f"/api/proposals/{proposal['proposalId']}/approve",
                    json={"version": proposal["version"]},
                )
        assert response.status_code == 200
        assert response.json()["status"] == "approved"

    def test_an_out_of_scope_maintainer_cannot_approve(self, grafana):
        from app.grafana_tools.dashboard_writing import PROPOSALS

        with acting_as(PROPOSER):
            proposal = PROPOSALS.create(destructive_ir())

        with api_as(OUT_OF_SCOPE_MAINTAINER):
            with TestClient(_app()) as client:
                response = client.post(
                    f"/api/proposals/{proposal['proposalId']}/approve",
                    json={"version": proposal["version"]},
                )
        assert response.status_code == 403

    def test_an_observer_cannot_approve(self, grafana):
        from app.grafana_tools.dashboard_writing import PROPOSALS

        with acting_as(PROPOSER):
            proposal = PROPOSALS.create(routine_ir())

        with api_as(OBSERVER_PRINCIPAL):
            with TestClient(_app()) as client:
                response = client.post(
                    f"/api/proposals/{proposal['proposalId']}/approve",
                    json={"version": proposal["version"]},
                )
        assert response.status_code == 403

    def test_an_operator_cannot_approve_a_dashboard_proposal(self, grafana):
        from app.grafana_tools.dashboard_writing import PROPOSALS

        with acting_as(PROPOSER):
            proposal = PROPOSALS.create(routine_ir())

        with api_as(OPERATOR_PRINCIPAL):
            with TestClient(_app()) as client:
                response = client.post(
                    f"/api/proposals/{proposal['proposalId']}/approve",
                    json={"version": proposal["version"]},
                )
        assert response.status_code == 403

    def test_the_approval_records_the_approver(self, grafana):
        from app.grafana_tools.dashboard_writing import PROPOSALS

        with acting_as(PROPOSER):
            proposal = PROPOSALS.create(destructive_ir())

        with api_as(OTHER_MAINTAINER):
            with TestClient(_app()) as client:
                response = client.post(
                    f"/api/proposals/{proposal['proposalId']}/approve",
                    json={"version": proposal["version"]},
                )
        assert response.status_code == 200

        stored = PROPOSALS.get(proposal["proposalId"])
        assert stored["approvedBy"] == OTHER_MAINTAINER.user_id
        assert stored["approverRole"] == "maintainer"
        assert stored["approvedAt"]

    def test_a_repeated_approval_is_refused(self, grafana):
        from app.grafana_tools.dashboard_writing import PROPOSALS

        with acting_as(PROPOSER):
            proposal = PROPOSALS.create(routine_ir())
        with acting_as(OTHER_MAINTAINER):
            PROPOSALS.approve(
                proposal["proposalId"], proposal["version"],
                approver_id=OTHER_MAINTAINER.user_id, approver_role="maintainer",
            )
            with pytest.raises(ValueError):
                PROPOSALS.approve(
                    proposal["proposalId"], proposal["version"],
                    approver_id=OTHER_MAINTAINER.user_id, approver_role="maintainer",
                )

    def test_the_proposer_cannot_reject_their_own_proposal(self, grafana):
        from app.grafana_tools.dashboard_writing import PROPOSALS

        with acting_as(PROPOSER):
            proposal = PROPOSALS.create(routine_ir())

        with api_as(PROPOSER):
            with TestClient(_app()) as client:
                response = client.post(
                    f"/api/proposals/{proposal['proposalId']}/reject",
                    json={"version": proposal["version"]},
                )
        assert response.status_code == 403


# ===========================================================================
# D. Execution-time re-validation
# ===========================================================================

class TestExecutionRevalidation:
    """The approval is re-checked immediately before the write."""

    def _approve(self, proposal, approver=OTHER_MAINTAINER):
        from app.grafana_tools.dashboard_writing import PROPOSALS

        with acting_as(approver):
            return PROPOSALS.approve(
                proposal["proposalId"], proposal["version"],
                approver_id=approver.user_id, approver_role=approver.role,
            )

    def test_a_payload_change_after_approval_invalidates_the_approval(self, grafana):
        from app.grafana_tools.dashboard_writing import PROPOSALS

        with acting_as(PROPOSER):
            proposal = PROPOSALS.create(routine_ir())
        approved = self._approve(proposal)

        # Tamper with the stored payload behind the store's back.
        with PROPOSALS._lock:
            PROPOSALS._items[proposal["proposalId"]]["ir"]["name"] = "Tampered"

        with pytest.raises(PermissionError):
            PROPOSALS.verified(
                proposal["proposalId"], proposal["version"], approved["approvalToken"]
            )

    def test_a_revoked_user_cannot_execute_an_approved_proposal(self, grafana):
        from app.grafana_tools.dashboard_writing import PROPOSALS

        with acting_as(PROPOSER):
            proposal = PROPOSALS.create(routine_ir())
        approved = self._approve(proposal)

        revoked = make_principal(
            "observer", user_id=OTHER_MAINTAINER.user_id, folders=(FOLDER_A,),
            dashboards=("dash-alpha",), environments=(ENV_ALLOWED,),
        )
        with api_as(revoked):
            with TestClient(_app()) as client:
                response = client.post(
                    f"/api/proposals/{proposal['proposalId']}/execute",
                    json={"version": proposal["version"], "approvalToken": approved["approvalToken"]},
                )
        assert response.status_code == 403
        assert grafana.writes == []

    def test_a_rejected_proposal_cannot_be_executed(self, grafana):
        from app.grafana_tools.dashboard_writing import PROPOSALS

        with acting_as(PROPOSER):
            proposal = PROPOSALS.create(routine_ir())
        approved = self._approve(proposal)
        with acting_as(OTHER_MAINTAINER):
            PROPOSALS.reject(
                proposal["proposalId"], proposal["version"],
                approver_id=OTHER_MAINTAINER.user_id, approver_role="maintainer",
            )

        with api_as(OTHER_MAINTAINER):
            with TestClient(_app()) as client:
                response = client.post(
                    f"/api/proposals/{proposal['proposalId']}/execute",
                    json={"version": proposal["version"], "approvalToken": approved["approvalToken"]},
                )
        assert response.status_code == 403
        assert grafana.writes == []

    def test_a_self_approved_destructive_proposal_cannot_be_executed(self, grafana):
        """Even if a token was obtained, the execution re-checks the rules."""
        from app.auth.policy import revalidate_proposal_for_execution
        from app.grafana_tools.dashboard_writing import PROPOSALS

        with acting_as(PROPOSER):
            proposal = PROPOSALS.create(destructive_ir())
        # Forge the record as if the proposer had approved it themselves.
        with PROPOSALS._lock:
            item = PROPOSALS._items[proposal["proposalId"]]
            item["status"] = "approved"
            item["approvedBy"] = PROPOSER.user_id
            item["approvedAt"] = "2026-01-01T00:00:00+00:00"
            forged = dict(item)

        decision = revalidate_proposal_for_execution(PROPOSER, forged)
        assert decision.allowed is False
        # The denial must name the separation-of-duties rule specifically --
        # not merely complain that the approval record is missing.
        assert "separation of duties" in decision.reason.lower()

    def test_a_destructive_proposal_approved_by_somebody_else_may_be_executed(self, grafana):
        """The recorded approver satisfies the second-approver requirement."""
        from app.auth.policy import revalidate_proposal_for_execution
        from app.grafana_tools.dashboard_writing import PROPOSALS

        with acting_as(PROPOSER):
            proposal = PROPOSALS.create(destructive_ir())
        with acting_as(OTHER_MAINTAINER):
            approved = PROPOSALS.approve(
                proposal["proposalId"], proposal["version"],
                approver_id=OTHER_MAINTAINER.user_id, approver_role="maintainer",
            )

        decision = revalidate_proposal_for_execution(OTHER_MAINTAINER, approved)
        assert decision.allowed is True
        assert decision.requires_second_approver is True

    def test_a_destructive_proposal_without_a_recorded_approver_is_refused(self, grafana):
        """An approval with no approver identity cannot satisfy the rule."""
        from app.auth.policy import revalidate_proposal_for_execution
        from app.grafana_tools.dashboard_writing import PROPOSALS

        with acting_as(PROPOSER):
            proposal = PROPOSALS.create(destructive_ir())
        with PROPOSALS._lock:
            item = PROPOSALS._items[proposal["proposalId"]]
            item["status"] = "approved"
            item["approvedBy"] = None
            forged = dict(item)

        decision = revalidate_proposal_for_execution(OTHER_MAINTAINER, forged)
        assert decision.allowed is False
        assert "second approver" in decision.reason.lower()

    def test_an_unapproved_proposal_cannot_be_executed(self, grafana):
        from app.grafana_tools.dashboard_writing import PROPOSALS

        with acting_as(PROPOSER):
            proposal = PROPOSALS.create(routine_ir())

        with api_as(OTHER_MAINTAINER):
            with TestClient(_app()) as client:
                response = client.post(
                    f"/api/proposals/{proposal['proposalId']}/execute",
                    json={"version": proposal["version"], "approvalToken": "not-a-token"},
                )
        assert response.status_code == 403
        assert grafana.writes == []

    def test_an_unauthorized_execution_causes_no_grafana_write(self, grafana):
        from app.grafana_tools.dashboard_writing import PROPOSALS

        with acting_as(PROPOSER):
            proposal = PROPOSALS.create(routine_ir())
        approved = self._approve(proposal)

        out_of_scope = make_principal(
            "maintainer", user_id="usr_sod_far", folders=(FOLDER_B,),
            environments=(ENV_ALLOWED,),
        )
        with api_as(out_of_scope):
            with TestClient(_app()) as client:
                response = client.post(
                    f"/api/proposals/{proposal['proposalId']}/execute",
                    json={"version": proposal["version"], "approvalToken": approved["approvalToken"]},
                )
        assert response.status_code == 403
        assert grafana.writes == []
        assert grafana.prometheus_queries == []


# ===========================================================================
# E. The MCP proposal server enforces the same rules
# ===========================================================================

class TestMcpPath:
    """The MCP proposal tools apply the same authorization as the HTTP API."""

    def test_the_mcp_server_imports_and_registers_its_tools(self):
        """The MCP 2.x compatibility shim keeps the server usable."""
        import app.mcp.server as server

        tools = asyncio.run(server.mcp.list_tools())
        names = {tool.name for tool in tools}
        assert "approve_dashboard_proposal" in names
        assert "reject_dashboard_proposal" in names
        assert "execute_dashboard_proposal" in names

    def test_mcp_refuses_to_approve_without_an_principal(self, grafana):
        import app.mcp.server as server

        with pytest.raises(Exception):
            run(server.approve_dashboard_proposal("whatever", 1))

    def test_mcp_refuses_a_self_approval_of_a_destructive_proposal(self, grafana):
        import app.mcp.server as server
        from app.grafana_tools.dashboard_writing import PROPOSALS

        with acting_as(PROPOSER):
            proposal = PROPOSALS.create(destructive_ir())

        with acting_as(PROPOSER):
            with pytest.raises(PermissionError):
                run(server.approve_dashboard_proposal(
                    proposal["proposalId"], proposal["version"]
                ))

    def test_mcp_refuses_an_approval_by_an_unauthorized_role(self, grafana):
        import app.mcp.server as server
        from app.grafana_tools.dashboard_writing import PROPOSALS

        with acting_as(PROPOSER):
            proposal = PROPOSALS.create(routine_ir())

        with acting_as(OBSERVER_PRINCIPAL):
            with pytest.raises(PermissionError):
                run(server.approve_dashboard_proposal(
                    proposal["proposalId"], proposal["version"]
                ))

    def test_mcp_refuses_an_approval_by_an_out_of_scope_user(self, grafana):
        import app.mcp.server as server
        from app.grafana_tools.dashboard_writing import PROPOSALS

        with acting_as(PROPOSER):
            proposal = PROPOSALS.create(destructive_ir())

        with acting_as(OUT_OF_SCOPE_MAINTAINER):
            with pytest.raises(PermissionError):
                run(server.approve_dashboard_proposal(
                    proposal["proposalId"], proposal["version"]
                ))

    def test_mcp_allows_an_eligible_second_approver(self, grafana):
        import app.mcp.server as server
        from app.grafana_tools.dashboard_writing import PROPOSALS

        with acting_as(PROPOSER):
            proposal = PROPOSALS.create(destructive_ir())

        with acting_as(OTHER_MAINTAINER):
            approved = run(server.approve_dashboard_proposal(
                proposal["proposalId"], proposal["version"]
            ))
        assert approved["status"] == "approved"
        assert approved["approvedBy"] == OTHER_MAINTAINER.user_id

    def test_mcp_refuses_a_modification_by_an_unauthorized_role(self, grafana):
        import app.mcp.server as server
        from app.grafana_tools.dashboard_writing import PROPOSALS

        with acting_as(PROPOSER):
            proposal = PROPOSALS.create(routine_ir())

        with acting_as(OBSERVER_PRINCIPAL):
            with pytest.raises(PermissionError):
                run(server.modify_dashboard_proposal(
                    proposal["proposalId"], json.dumps(routine_ir())
                ))


# ===========================================================================
# F. The ADK proposal path enforces the same rules
# ===========================================================================

class TestAdkPath:
    """The ADK agent's proposal tool applies the same authorization."""

    def test_the_adk_proposal_tool_refuses_an_operator(self, grafana):
        from app.grafana_tools.dashboard_writing import propose_dashboard

        with acting_as(OPERATOR_PRINCIPAL):
            outcome = propose_dashboard("Create a dashboard called Nope in folder %s" % FOLDER_A)
        assert outcome["status"] == "policy_denied"

    def test_the_adk_proposal_tool_refuses_an_observer(self, grafana):
        from app.grafana_tools.dashboard_writing import propose_dashboard

        with acting_as(OBSERVER_PRINCIPAL):
            outcome = propose_dashboard("Create a dashboard called Nope in folder %s" % FOLDER_A)
        assert outcome["status"] == "policy_denied"

    def test_the_adk_proposal_tool_records_the_server_side_proposer(self, grafana):
        from app.grafana_tools.dashboard_writing import propose_dashboard

        with acting_as(PROPOSER):
            outcome = propose_dashboard(
                "Create a dashboard called Adk Owned showing node_load1 in folder %s" % FOLDER_A
            )
        assert outcome["proposalId"]
        from app.grafana_tools.dashboard_writing import PROPOSALS

        stored = PROPOSALS.get(outcome["proposalId"])
        assert stored["proposer_id"] == PROPOSER.user_id

    def test_an_out_of_scope_target_is_a_clean_denial_not_a_crash(self, grafana):
        """A scope denial must surface as a denial, not as an internal error.

        The proposal builder refuses an out-of-scope target by raising
        ``AuthorizationDenied``; the tool boundary turns that into a
        ``policy_denied`` outcome. The handler that does so must not itself
        raise -- a bug there turned every out-of-scope update into an HTTP
        500.
        """
        from app.grafana_tools.dashboard_writing import propose_dashboard

        out_of_scope = make_principal(
            "maintainer", user_id="usr_sod_outofscope2", folders=(FOLDER_B,),
            environments=(ENV_ALLOWED,),
        )
        with acting_as(out_of_scope):
            outcome = propose_dashboard(
                "Add a panel showing node_load1 to dashboard dash-alpha"
            )
        assert outcome["status"] == "policy_denied"
        assert "outside permitted scope" in outcome["reason"]
        assert "proposalId" not in outcome


def _app():
    from app.api.main import app

    return app
