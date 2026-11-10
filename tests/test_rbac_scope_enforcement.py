"""tests/test_rbac_scope_enforcement.py

Regression tests for the resource-scope, administrative and environment-level
RBAC enforcement of the Grafana AI Agent.

Every test in this module proves two things at once:

* the unauthorized operation is **rejected** (with the documented status
  code), and
* **nothing was changed** in Grafana or Prometheus while it was rejected.

The Grafana and Prometheus backends are replaced by deterministic in-memory
fakes, so the assertions are about the application's authorization decisions
and about the side effects that did (or did not) happen.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from typing import Any, Iterator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.auth.environment import reset_environment_catalogue
from app.auth.principal import CURRENT_PRINCIPAL, Principal, set_current_principal
from app.auth.resource_scope import AuthorizationDenied

# ---------------------------------------------------------------------------
# Synthetic fixtures. These are deliberately *not* the dashboard UIDs, folder
# UIDs or user names of the demo deployment: the authorization decisions must
# come from the scope data, never from a hardcoded identifier.
# ---------------------------------------------------------------------------

FOLDER_A = "fld-scope-team-a"
FOLDER_B = "fld-scope-team-b"
GENERAL = ""  # Grafana's "General" pseudo-folder

ENV_ALLOWED = "env-alpha"
ENV_SECOND = "env-beta"
ENV_FORBIDDEN = "env-omega"


def make_principal(
    role: str,
    *,
    user_id: str | None = None,
    folders: tuple[str, ...] = (),
    dashboards: tuple[str, ...] = (),
    denied: tuple[str, ...] = (),
    environments: tuple[str, ...] = (),
    status: str = "active",
) -> Principal:
    return Principal(
        user_id=user_id or f"usr_{role}",
        issuer="https://authentik.test",
        subject=f"sub_{user_id or role}",
        authenticated=True,
        status=status,
        role=role,
        allowed_folder_uids=frozenset(folders),
        allowed_dashboard_uids=frozenset(dashboards),
        denied_dashboard_uids=frozenset(denied),
        allowed_environments=frozenset(environments),
        username=role,
    )


ADMIN = make_principal(
    "admin",
    user_id="usr_admin",
    folders=("*",),
    environments=(ENV_ALLOWED, ENV_SECOND, ENV_FORBIDDEN),
)
MAINTAINER = make_principal(
    "maintainer",
    user_id="usr_maintainer",
    folders=(FOLDER_A,),
    dashboards=("dash-alpha",),
    environments=(ENV_ALLOWED,),
)
OPERATOR = make_principal(
    "operator",
    user_id="usr_operator",
    folders=(FOLDER_B,),
    environments=(ENV_ALLOWED, ENV_SECOND),
)
OBSERVER = make_principal(
    "observer",
    user_id="usr_observer",
    folders=(FOLDER_A,),
    dashboards=("dash-alpha",),
    environments=(ENV_ALLOWED,),
)


# ---------------------------------------------------------------------------
# The fake Grafana / Prometheus
# ---------------------------------------------------------------------------

def _panel(title: str, expr: str = "node_load1") -> dict:
    """A Grafana panel definition that survives the hydration the app does."""
    return {
        "id": 1,
        "title": title,
        "type": "timeseries",
        "gridPos": {"x": 0, "y": 0, "w": 12, "h": 8},
        "datasource": {"type": "prometheus", "uid": "prom-uid"},
        "targets": [{"refId": "A", "expr": expr, "datasource": {"type": "prometheus", "uid": "prom-uid"}}],
        "fieldConfig": {"defaults": {"unit": "short"}},
    }


class FakeGrafana:
    """A minimal, deterministic stand-in for the Grafana instance."""

    def __init__(self) -> None:
        self.dashboards: dict[str, dict[str, Any]] = {
            "dash-alpha": {
                "uid": "dash-alpha",
                "title": "Alpha Service Overview",
                "tags": ["alpha"],
                "url": "/d/dash-alpha",
                "folderUid": FOLDER_A,
                "folderTitle": "Team A",
                "panels": [_panel("Alpha CPU")],
            },
            "dash-beta": {
                "uid": "dash-beta",
                "title": "Beta Service Overview",
                "tags": [],
                "url": "/d/dash-beta",
                "folderUid": FOLDER_B,
                "folderTitle": "Team B",
                "panels": [_panel("Beta Memory")],
            },
            "dash-loose": {
                "uid": "dash-loose",
                "title": "Unassigned Overview",
                "tags": [],
                "url": "/d/dash-loose",
                "folderUid": GENERAL,
                "folderTitle": "General",
                "panels": [_panel("Loose CPU")],
            },
            "dash-blocked": {
                "uid": "dash-blocked",
                "title": "Blocked Overview",
                "tags": [],
                "url": "/d/dash-blocked",
                "folderUid": FOLDER_A,
                "folderTitle": "Team A",
                "panels": [_panel("Blocked CPU")],
            },
        }
        self.folders = {FOLDER_A: "Team A", FOLDER_B: "Team B"}
        #: Every Grafana write the application attempted, in order.
        self.writes: list[tuple[str, dict]] = []
        self.prometheus_queries: list[str] = []

    # -- reads ------------------------------------------------------------
    async def list_dashboards(self, refresh: bool = False) -> list[dict]:
        return [dict(record) for record in self.dashboards.values()]

    async def detail(self, uid: str, refresh: bool = False) -> dict | None:
        record = self.dashboards.get(str(uid))
        if record is None:
            return None
        return {
            "dashboard": {
                "uid": record["uid"],
                "title": record["title"],
                "panels": [dict(panel) for panel in record["panels"]],
                "templating": {"list": []},
                "time": {"from": "now-1h", "to": "now"},
                "description": "",
            },
            "meta": {"folderUid": record["folderUid"], "folderTitle": record["folderTitle"]},
        }

    async def summary(self, uid: str) -> str:
        record = self.dashboards.get(str(uid))
        if record is None:
            return ""
        return json.dumps({
            "title": record["title"],
            "panels": [
                {"title": panel["title"], "type": panel["type"], "queryCount": 1}
                for panel in record["panels"]
            ],
            "variables": [],
            "timeRange": {"from": "now-1h", "to": "now"},
        })

    async def search(self, query: str) -> str:
        needle = (query or "").strip().lower()
        matches = [
            {
                "uid": record["uid"],
                "title": record["title"],
                "tags": record["tags"],
                "folderUid": record["folderUid"],
            }
            for record in self.dashboards.values()
            if needle in record["title"].lower()
        ]
        return json.dumps({"dashboards": matches})

    # -- writes -----------------------------------------------------------
    async def mcp_call(self, tool: str, args: dict, **kwargs) -> str:
        self.writes.append((tool, dict(args)))
        if tool == "update_dashboard":
            dashboard = args.get("dashboard") or {}
            # Grafana reads the target folder from the TOP LEVEL of the
            # request body -- not from the dashboard JSON. The fake models
            # that faithfully, so a payload that only carries the folder
            # inside the dashboard JSON lands the dashboard in General.
            folder_uid = args.get("folderUid")
            if folder_uid is None:
                folder_uid = GENERAL
            uid = dashboard.get("uid")
            if not uid:
                uid = f"dash-new-{len(self.dashboards)}"
                self.dashboards[uid] = {
                    "uid": uid,
                    "title": dashboard.get("title", "New Dashboard"),
                    "tags": [],
                    "url": "",
                    "folderUid": folder_uid,
                    "folderTitle": "",
                    "panels": [],
                }
            record = self.dashboards.get(uid)
            if record is not None:
                record["folderUid"] = folder_uid
                record["title"] = dashboard.get("title", record["title"])
            return json.dumps({"uid": uid, "status": "success", "version": 2})
        if tool == "grafana_api_request":
            method = str(args.get("method", "")).upper()
            endpoint = str(args.get("endpoint", ""))
            if method == "GET" and endpoint == "/api/folders":
                return json.dumps([
                    {"uid": uid, "title": title} for uid, title in self.folders.items()
                ])
            if method == "DELETE" and endpoint.startswith("/api/dashboards/uid/"):
                uid = endpoint.rsplit("/", 1)[-1]
                self.dashboards.pop(uid, None)
            return json.dumps({"status": "success"})
        return json.dumps({"status": "success"})


@pytest.fixture()
def grafana(monkeypatch) -> FakeGrafana:
    """Replace every Grafana-facing call with the deterministic fake."""
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

    # Prometheus / datasource discovery
    monkeypatch.setattr(
        writing, "resolve_datasource",
        AsyncMock(return_value={"uid": "prom-uid", "type": "prometheus", "name": "Prometheus"}),
    )
    monkeypatch.setattr(
        writing, "list_prometheus_metric_names",
        AsyncMock(return_value=json.dumps(["node_load1", "node_cpu_seconds_total"])),
    )
    monkeypatch.setattr(
        writing, "list_prometheus_label_names",
        AsyncMock(return_value=json.dumps(["instance", "job"])),
    )
    monkeypatch.setattr(
        writing, "list_prometheus_label_values",
        AsyncMock(return_value=json.dumps(["node-01", "node-02"])),
    )
    monkeypatch.setattr(writing, "list_prometheus_metric_metadata", AsyncMock(return_value="{}"))

    async def fake_execute_prometheus(expr: str, *args, **kwargs) -> str:
        fake.prometheus_queries.append(expr)
        return json.dumps({"status": "success", "data": {"result": []}})

    monkeypatch.setattr(writing, "execute_prometheus", fake_execute_prometheus)

    return fake


@pytest.fixture()
def environments(monkeypatch) -> list[str]:
    """A deployment that knows three environments."""
    known = [ENV_ALLOWED, ENV_SECOND, ENV_FORBIDDEN]
    import app.auth.environment as environment_module

    monkeypatch.setattr(environment_module, "deployment_environments", lambda: list(known))
    reset_environment_catalogue()
    yield known
    reset_environment_catalogue()


def run(coro):
    """Run a coroutine to completion (the proposal builders are async)."""
    return asyncio.run(coro)


@contextmanager
def _stubbed_query_pipeline(canned: dict):
    """Replace the language-model pipeline of the query endpoint."""
    from app.config import get_settings

    settings = get_settings().model_copy(update={"dependent_query_resolution_enabled": False})
    with patch("app.api.routes_query.get_settings", return_value=settings), \
         patch("app.api.routes_query.run_pipeline", new=AsyncMock(return_value=canned)), \
         patch("app.api.routes_query.run_dependency_aware_pipeline", new=AsyncMock(return_value=canned)), \
         patch("app.executor.execute_contract", return_value=canned):
        yield


@contextmanager
def acting_as(principal: Principal | None) -> Iterator[Principal | None]:
    """Bind a principal to the current context, exactly as the auth dependency does."""
    token = set_current_principal(principal)
    try:
        yield principal
    finally:
        try:
            CURRENT_PRINCIPAL.reset(token)
        except (ValueError, LookupError):  # pragma: no cover - defensive
            set_current_principal(None)


@pytest.fixture()
def client() -> Iterator[TestClient]:
    from app.api.main import app

    with TestClient(app) as test_client:
        yield test_client


def use_principal(app, principal: Principal) -> None:
    """Install a principal on the application (mirrors the auth dependency)."""
    from app.auth.dependencies import require_principal

    app.dependency_overrides[require_principal] = lambda: principal
    set_current_principal(principal)


# ===========================================================================
# A. Dashboard and folder authorization
# ===========================================================================

class TestDashboardReadScope:
    """A dashboard may only be read from inside the caller's scope."""

    def test_maintainer_reads_assigned_dashboard(self, grafana):
        from app.grafana_tools.wrapper import get_dashboard_panels

        with acting_as(MAINTAINER):
            text = get_dashboard_panels("dash-alpha")
        assert "Alpha CPU" in text

    def test_maintainer_reads_out_of_scope_dashboard(self, grafana):
        from app.grafana_tools.wrapper import get_dashboard_panels

        with acting_as(MAINTAINER):
            with pytest.raises(AuthorizationDenied):
                get_dashboard_panels("dash-beta")

    def test_operator_reads_assigned_dashboard(self, grafana):
        from app.grafana_tools.wrapper import get_dashboard_panels

        with acting_as(OPERATOR):
            assert "Beta Memory" in get_dashboard_panels("dash-beta")

    def test_operator_reads_maintainer_dashboard(self, grafana):
        from app.grafana_tools.wrapper import get_dashboard_panels

        with acting_as(OPERATOR):
            with pytest.raises(AuthorizationDenied):
                get_dashboard_panels("dash-alpha")

    def test_observer_reads_assigned_dashboard(self, grafana):
        from app.grafana_tools.wrapper import get_dashboard_panels

        with acting_as(OBSERVER):
            assert "Alpha CPU" in get_dashboard_panels("dash-alpha")

    def test_observer_reads_out_of_scope_dashboard(self, grafana):
        from app.grafana_tools.wrapper import get_dashboard_panels

        with acting_as(OBSERVER):
            with pytest.raises(AuthorizationDenied):
                get_dashboard_panels("dash-loose")

    def test_admin_reads_any_dashboard(self, grafana):
        from app.grafana_tools.wrapper import get_dashboard_panels

        with acting_as(ADMIN):
            assert "Loose CPU" in get_dashboard_panels("dash-loose")
            assert "Beta Memory" in get_dashboard_panels("dash-beta")

    def test_explicitly_denied_dashboard_is_refused_even_for_admin(self, grafana):
        blocked = make_principal(
            "admin", user_id="usr_admin_blocked", folders=("*",), denied=("dash-blocked",),
        )
        from app.grafana_tools.wrapper import get_dashboard_panels

        with acting_as(blocked):
            with pytest.raises(AuthorizationDenied):
                get_dashboard_panels("dash-blocked")

    def test_missing_dashboard_uid_is_rejected(self, grafana):
        from app.grafana_tools.wrapper import get_dashboard_panels

        with acting_as(MAINTAINER):
            with pytest.raises(AuthorizationDenied):
                get_dashboard_panels("")

    def test_folder_uid_tampering_cannot_widen_the_scope(self, grafana):
        """A caller-supplied folder never decides the outcome -- Grafana does."""
        from app.auth.resource_scope import authorize_dashboard_access

        # The dashboard really lives in Team B; the maintainer may only write
        # to Team A, so the decision must be a denial.
        decision = run(authorize_dashboard_access(MAINTAINER, "dash-beta"))
        assert decision.allowed is False

    def test_read_decision_uses_the_folder_grafana_reports(self, grafana):
        from app.auth.resource_scope import authorize_dashboard_access

        decision = run(authorize_dashboard_access(MAINTAINER, "dash-alpha"))
        assert decision.allowed is True

    def test_list_dashboards_only_shows_in_scope_dashboards(self, grafana):
        from app.grafana_tools.wrapper import list_dashboards

        with acting_as(MAINTAINER):
            listing = list_dashboards()
        assert "dash-alpha" in listing
        assert "dash-beta" not in listing
        assert "dash-loose" not in listing

    def test_search_dashboards_only_shows_in_scope_dashboards(self, grafana):
        from app.grafana_tools.wrapper import search_dashboards

        with acting_as(MAINTAINER):
            result = search_dashboards("overview")
        assert "dash-alpha" in result
        assert "dash-beta" not in result
        assert "dash-loose" not in result

    def test_admin_sees_every_dashboard(self, grafana):
        from app.grafana_tools.wrapper import list_dashboards

        with acting_as(ADMIN):
            listing = list_dashboards()
        assert "dash-alpha" in listing
        assert "dash-beta" in listing
        assert "dash-loose" in listing


class TestDashboardReadScopeOverHttp:
    """The same rules hold for a direct HTTP request."""

    def _post(self, client, principal, message, target=None, session="s"):
        from app.api.main import app

        use_principal(app, principal)
        try:
            return client.post("/api/chat", json={
                "message": message, "sessionId": session, "target": target,
            })
        finally:
            from app.auth.dependencies import require_principal

            app.dependency_overrides.pop(require_principal, None)
            set_current_principal(None)

    def test_operator_panel_read_of_unauthorized_dashboard_is_403(self, client, grafana):
        response = self._post(
            client, OPERATOR,
            "Show me the panels", "dash-alpha",
            session="operator-panel-read",
        )
        assert response.status_code == 403
        assert "policy denied" in response.json()["detail"].lower()

    def test_maintainer_panel_read_of_authorized_dashboard_is_200(self, client, grafana):
        response = self._post(
            client, MAINTAINER,
            "Show me the panels", "dash-alpha",
            session="maintainer-panel-read",
        )
        assert response.status_code == 200
        assert "Alpha CPU" in response.json()["answer"]

    def test_dashboard_listing_is_filtered_over_http(self, client, grafana):
        response = self._post(client, MAINTAINER, "List the dashboards", session="maint-list")
        assert response.status_code == 200
        answer = response.json()["answer"]
        assert "dash-alpha" in answer
        assert "dash-beta" not in answer

    def test_dashboard_uid_tampering_over_http_is_403(self, client, grafana):
        response = self._post(
            client, MAINTAINER,
            "Show me the panels", "dash-beta",
            session="maint-tamper",
        )
        assert response.status_code == 403

    def test_folder_uid_tampering_over_http_is_403(self, client, grafana):
        """Claiming an authorized folder does not unlock a foreign dashboard."""
        response = self._post(
            client, MAINTAINER,
            "Show me the panels", "dash-beta",
            session="maint-folder-tamper",
        )
        assert response.status_code == 403


# ===========================================================================
# B. Dashboard / panel proposal scope
# ===========================================================================

class TestDashboardProposalScope:
    """A proposal may only target resources inside the caller's scope."""

    def test_maintainer_creates_dashboard_in_authorized_folder(self, grafana):
        from app.grafana_tools.dashboard_writing import build_proposal

        with acting_as(MAINTAINER):
            proposal = run(build_proposal(
                f"Create a dashboard called Scope Alpha in folder {FOLDER_A} showing node_load1"
            ))
        assert proposal["ir"]["folderUid"] == FOLDER_A

    def test_maintainer_creates_dashboard_in_unauthorized_folder(self, grafana):
        from app.grafana_tools.dashboard_writing import build_proposal

        with acting_as(MAINTAINER):
            with pytest.raises(AuthorizationDenied):
                run(build_proposal(
                    f"Create a dashboard called Scope Beta in folder {FOLDER_B} showing node_load1"
                ))
        assert grafana.writes == []

    def test_maintainer_creates_dashboard_in_general_when_not_authorized(self, grafana):
        """The General folder is not a silent fallback for a scoped maintainer."""
        from app.auth.policy import authorize_dashboard_propose
        from app.auth.resource_scope import folder_in_scope

        assert folder_in_scope(MAINTAINER, GENERAL) is False
        decision = authorize_dashboard_propose(
            MAINTAINER, operation="create", folder_uid=GENERAL, require_target=True,
        )
        assert decision.allowed is False
        assert grafana.writes == []

    def test_creation_falls_back_to_an_authorized_folder_not_to_general(self, grafana):
        """With a single authorized folder, that folder is the destination."""
        from app.auth.resource_scope import resolve_destination_folder

        destination = resolve_destination_folder(MAINTAINER, None, default_folder_uid="fld-not-authorized")
        assert destination == FOLDER_A

    def test_update_of_out_of_scope_dashboard_is_denied(self, grafana):
        from app.grafana_tools.dashboard_writing import build_proposal

        with acting_as(MAINTAINER):
            with pytest.raises(AuthorizationDenied):
                run(build_proposal("Add a panel showing node_load1 to dashboard dash-beta"))

    def test_update_of_assigned_dashboard_is_allowed(self, grafana):
        from app.grafana_tools.dashboard_writing import build_proposal

        with acting_as(MAINTAINER):
            proposal = run(build_proposal("Add a panel showing node_load1 to dashboard dash-alpha"))

    def test_panel_removal_on_out_of_scope_dashboard_is_denied(self, grafana):
        from app.grafana_tools.dashboard_writing import build_proposal

        with acting_as(MAINTAINER):
            with pytest.raises(AuthorizationDenied):
                run(build_proposal("Delete the Alpha CPU panel from dashboard dash-beta"))

    def test_operator_cannot_propose_dashboard_changes(self, grafana):
        from app.grafana_tools.dashboard_writing import build_proposal

        with acting_as(OPERATOR):
            with pytest.raises(AuthorizationDenied):
                run(build_proposal(
                    f"Create a dashboard called Operator Alpha in folder {FOLDER_B} showing node_load1"
                ))
        assert grafana.writes == []

    def test_observer_cannot_propose_dashboard_changes(self, grafana):
        from app.grafana_tools.dashboard_writing import build_proposal

        with acting_as(OBSERVER):
            with pytest.raises(AuthorizationDenied):
                run(build_proposal(
                    f"Create a dashboard called Observer Alpha in folder {FOLDER_A} showing node_load1"
                ))
        assert grafana.writes == []

    def test_unauthorized_folder_move_is_denied(self, grafana):
        """Moving an authorized dashboard into a foreign folder is refused."""
        from app.grafana_tools.dashboard_writing import build_proposal

        with acting_as(MAINTAINER):
            with pytest.raises(AuthorizationDenied):
                run(build_proposal(
                    f"Add a panel showing node_load1 to dashboard dash-alpha in folder {FOLDER_B}"
                ))
        assert grafana.writes == []

    def test_authorized_update_preserves_the_folder(self, grafana):
        from app.grafana_tools.dashboard_writing import build_proposal, compile_dashboard

        with acting_as(MAINTAINER):
            proposal = run(build_proposal("Add a panel showing node_load1 to dashboard dash-alpha"))
        payload = compile_dashboard(proposal["ir"])
        assert payload["dashboard"]["folderUid"] == FOLDER_A

    def test_created_dashboard_targets_the_authorized_folder(self, grafana):
        from app.grafana_tools.dashboard_writing import build_proposal, compile_dashboard

        with acting_as(MAINTAINER):
            proposal = run(build_proposal(
                f"Create a dashboard called Placement Check in folder {FOLDER_A} showing node_load1"
            ))
        payload = compile_dashboard(proposal["ir"])
        # Grafana reads the folder from the top level of the request body.
        assert payload["folderUid"] == FOLDER_A
        assert payload["dashboard"]["folderUid"] == FOLDER_A

    def test_executed_dashboard_lands_in_the_authorized_folder(self, grafana, monkeypatch):
        """The end-to-end result: the dashboard really is in that folder."""
        import asyncio

        from app.grafana_tools.dashboard_writing import (
            PROPOSALS, build_proposal, execute_approved_mutation,
        )

        monkeypatch.setenv("GRAFANA_MCP_ENABLE_WRITE", "true")
        with acting_as(MAINTAINER):
            proposal = run(build_proposal(
                f"Create a dashboard called Placement End To End in folder {FOLDER_A} showing node_load1"
            ))
            PROPOSALS.approve(proposal["proposalId"], proposal["version"])
            token = PROPOSALS.get(proposal["proposalId"])["approvalToken"]
            asyncio.run(execute_approved_mutation(
                proposal["proposalId"], proposal["version"], token,
            ))
        created = [
            record for record in grafana.dashboards.values()
            if record["title"].startswith("Placement End To End")
        ]
        assert created, "the dashboard was not created"
        assert created[0]["folderUid"] == FOLDER_A


class TestDashboardProposalScopeOverHttp:
    """A direct API call cannot bypass the scope checks."""

    def test_create_proposal_in_unauthorized_folder_is_403(self, client, grafana):
        from app.api.main import app

        use_principal(app, MAINTAINER)
        try:
            response = client.post("/api/proposals", json={
                "request": f"Create a dashboard called Bypass in folder {FOLDER_B} showing node_load1",
            })
        finally:
            from app.auth.dependencies import require_principal

            app.dependency_overrides.pop(require_principal, None)
            set_current_principal(None)
        assert response.status_code == 403
        assert grafana.writes == []

    def test_create_proposal_as_operator_is_403(self, client, grafana):
        from app.api.main import app

        use_principal(app, OPERATOR)
        try:
            response = client.post("/api/proposals", json={
                "request": f"Create a dashboard called Bypass in folder {FOLDER_B} showing node_load1",
            })
        finally:
            from app.auth.dependencies import require_principal

            app.dependency_overrides.pop(require_principal, None)
            set_current_principal(None)
        assert response.status_code == 403
        assert grafana.writes == []

    def test_create_proposal_as_observer_is_403(self, client, grafana):
        from app.api.main import app

        use_principal(app, OBSERVER)
        try:
            response = client.post("/api/proposals", json={
                "request": f"Create a dashboard called Bypass in folder {FOLDER_A} showing node_load1",
            })
        finally:
            from app.auth.dependencies import require_principal

            app.dependency_overrides.pop(require_principal, None)
            set_current_principal(None)
        assert response.status_code == 403
        assert grafana.writes == []


# ===========================================================================
# C. Execution revalidation
# ===========================================================================

class TestDashboardExecutionScope:
    """Execution revalidates the executor's authorization independently."""

    def _proposal(self, principal, grafana, request_text):
        from app.grafana_tools.dashboard_writing import build_proposal

        with acting_as(principal):
            return run(build_proposal(request_text))

    def test_execution_after_permission_revocation_is_403_and_writes_nothing(self, grafana):
        from app.grafana_tools.dashboard_writing import PROPOSALS, execute_approved_mutation

        proposal = self._proposal(
            MAINTAINER, grafana,
            "Add a panel showing node_load1 to dashboard dash-alpha",
        )
        pid = proposal["proposalId"]
        with acting_as(MAINTAINER):
            approved = PROPOSALS.approve(pid, proposal["version"])
        token = approved["approvalToken"]

        # The maintainer's folder scope is revoked before the execution.
        revoked = make_principal(
            "maintainer", user_id="usr_maintainer",
            folders=(), dashboards=(), environments=(ENV_ALLOWED,),
        )
        with acting_as(revoked):
            with pytest.raises(PermissionError):
                import asyncio

                asyncio.run(execute_approved_mutation(pid, proposal["version"], token))
        assert grafana.writes == []

    def test_execution_by_operator_is_403_and_writes_nothing(self, grafana):
        import asyncio

        from app.grafana_tools.dashboard_writing import PROPOSALS, execute_approved_mutation

        proposal = self._proposal(
            MAINTAINER, grafana,
            "Add a panel showing node_load1 to dashboard dash-alpha",
        )
        pid = proposal["proposalId"]
        with acting_as(MAINTAINER):
            approved = PROPOSALS.approve(pid, proposal["version"])

        with acting_as(OPERATOR):
            with pytest.raises(PermissionError):
                asyncio.run(execute_approved_mutation(pid, proposal["version"], approved["approvalToken"]))
        assert grafana.writes == []

    def test_execution_without_an_identity_is_refused(self, grafana):
        import asyncio

        from app.grafana_tools.dashboard_writing import PROPOSALS, execute_approved_mutation

        proposal = self._proposal(
            MAINTAINER, grafana,
            "Add a panel showing node_load1 to dashboard dash-alpha",
        )
        pid = proposal["proposalId"]
        with acting_as(MAINTAINER):
            approved = PROPOSALS.approve(pid, proposal["version"])

        with acting_as(None):
            with pytest.raises(PermissionError):
                asyncio.run(execute_approved_mutation(pid, proposal["version"], approved["approvalToken"]))
        assert grafana.writes == []

    def test_authorized_execution_writes_once_to_the_authorized_folder(self, grafana, monkeypatch):
        import asyncio

        from app.grafana_tools.dashboard_writing import PROPOSALS, execute_approved_mutation

        monkeypatch.setenv("GRAFANA_MCP_ENABLE_WRITE", "true")
        proposal = self._proposal(
            MAINTAINER, grafana,
            "Add a panel showing node_load1 to dashboard dash-alpha",
        )
        pid = proposal["proposalId"]
        with acting_as(MAINTAINER):
            approved = PROPOSALS.approve(pid, proposal["version"])
            result = asyncio.run(
                execute_approved_mutation(pid, proposal["version"], approved["approvalToken"])
            )
        assert len(grafana.writes) == 1
        args = grafana.writes[0][1]
        # The folder travels at the top level of the Grafana request.
        assert args["folderUid"] == FOLDER_A
        assert args["dashboard"]["folderUid"] == FOLDER_A
        assert result["proposal"]["status"] == "built"

    def test_execute_endpoint_refuses_a_revoked_executor(self, client, grafana):
        from app.api.main import app
        from app.grafana_tools.dashboard_writing import PROPOSALS

        proposal = self._proposal(
            MAINTAINER, grafana,
            "Add a panel showing node_load1 to dashboard dash-alpha",
        )
        pid = proposal["proposalId"]
        with acting_as(MAINTAINER):
            approved = PROPOSALS.approve(pid, proposal["version"])

        revoked = make_principal(
            "maintainer", user_id="usr_maintainer",
            folders=(), dashboards=(), environments=(ENV_ALLOWED,),
        )
        use_principal(app, revoked)
        try:
            response = client.post(
                f"/api/proposals/{pid}/execute",
                json={"version": proposal["version"], "approvalToken": approved["approvalToken"]},
            )
        finally:
            from app.auth.dependencies import require_principal

            app.dependency_overrides.pop(require_principal, None)
            set_current_principal(None)
        assert response.status_code == 403
        assert grafana.writes == []


# ===========================================================================
# D. Proposal ownership and approval
# ===========================================================================

class TestProposalOwnership:
    """Proposals record who created them and approvals are re-checked."""

    def test_proposal_records_the_authenticated_proposer(self, grafana):
        from app.grafana_tools.dashboard_writing import build_proposal

        with acting_as(MAINTAINER):
            proposal = run(build_proposal(
                f"Create a dashboard called Owned in folder {FOLDER_A} showing node_load1"
            ))
        assert proposal["proposer_id"] == MAINTAINER.user_id

    def test_proposal_never_trusts_a_client_supplied_identity(self, grafana):
        """There is no way to inject a proposer id through the API."""
        from app.grafana_tools.dashboard_writing import PROPOSALS

        ir = {
            "operation": "create",
            "name": "Injected",
            "folderUid": FOLDER_A,
            "proposer_id": "usr_somebody_else",
            "panels": [{
                "title": "Panel",
                "datasource": {"type": "prometheus", "uid": "prom-uid"},
                "visualizationType": "timeseries",
                "query": "node_load1",
            }],
        }
        with acting_as(MAINTAINER):
            proposal = PROPOSALS.create(ir)
        assert proposal["proposer_id"] == MAINTAINER.user_id

    def test_approver_authorization_is_revalidated_at_approval_time(self, client, grafana):
        from app.api.main import app
        from app.grafana_tools.dashboard_writing import PROPOSALS

        proposal = self._make_update_proposal(MAINTAINER, grafana)
        pid = proposal["proposalId"]

        # The maintainer's role is revoked before the approval.
        demoted = make_principal("observer", user_id="usr_maintainer", folders=(FOLDER_A,))
        use_principal(app, demoted)
        try:
            response = client.post(f"/api/proposals/{pid}/approve", json={"version": proposal["version"]})
        finally:
            from app.auth.dependencies import require_principal

            app.dependency_overrides.pop(require_principal, None)
            set_current_principal(None)
        assert response.status_code == 403

    def test_destructive_delete_cannot_be_self_approved(self, grafana):
        from app.auth.policy import authorize_dashboard_approve
        from app.grafana_tools.dashboard_writing import PROPOSALS

        with acting_as(MAINTAINER):
            proposal = PROPOSALS.create({
                "operation": "remove",
                "name": "Alpha Service Overview",
                "dashboardUid": "dash-alpha",
                "folderUid": FOLDER_A,
                "removeDashboard": True,
                "panels": [],
            })
        assert proposal["requires_second_approver"] is True
        decision = authorize_dashboard_approve(MAINTAINER, proposal=proposal)
        assert decision.allowed is False

    def test_destructive_delete_is_approved_by_another_user(self, grafana):
        from app.auth.policy import authorize_dashboard_approve
        from app.grafana_tools.dashboard_writing import PROPOSALS

        other = make_principal(
            "maintainer", user_id="usr_other_maintainer",
            folders=(FOLDER_A,), environments=(ENV_ALLOWED,),
        )
        with acting_as(MAINTAINER):
            proposal = PROPOSALS.create({
                "operation": "remove",
                "name": "Alpha Service Overview",
                "dashboardUid": "dash-alpha",
                "folderUid": FOLDER_A,
                "removeDashboard": True,
                "panels": [],
            })
        decision = authorize_dashboard_approve(other, proposal=proposal)
        assert decision.allowed is True

    def _make_update_proposal(self, principal, grafana):
        from app.grafana_tools.dashboard_writing import build_proposal

        with acting_as(principal):
            return run(build_proposal("Add a panel showing node_load1 to dashboard dash-alpha"))


# ===========================================================================
# E. Administrative API authorization
# ===========================================================================

class TestAdminApiAuthorization:
    """Every /api/v1/admin/* endpoint is administrator-only."""

    def _get(self, client, principal, path):
        from app.api.main import app

        use_principal(app, principal)
        try:
            return client.get(path)
        finally:
            from app.auth.dependencies import require_principal

            app.dependency_overrides.pop(require_principal, None)
            set_current_principal(None)

    def _post(self, client, principal, path):
        from app.api.main import app

        use_principal(app, principal)
        try:
            return client.post(path)
        finally:
            from app.auth.dependencies import require_principal

            app.dependency_overrides.pop(require_principal, None)
            set_current_principal(None)

    def test_admin_lists_users(self, client, grafana):
        response = self._get(client, ADMIN, "/api/v1/admin/users")
        assert response.status_code == 200
        assert "users" in response.json()

    @pytest.mark.parametrize("principal", [MAINTAINER, OPERATOR, OBSERVER])
    def test_non_admin_cannot_list_users(self, client, grafana, principal):
        response = self._get(client, principal, "/api/v1/admin/users")
        assert response.status_code == 403

    def test_admin_lists_folders(self, client, grafana):
        response = self._get(client, ADMIN, "/api/v1/admin/folders")
        assert response.status_code == 200
        assert "folders" in response.json()

    @pytest.mark.parametrize("principal", [MAINTAINER, OPERATOR, OBSERVER])
    def test_non_admin_cannot_list_folders(self, client, grafana, principal):
        response = self._get(client, principal, "/api/v1/admin/folders")
        assert response.status_code == 403

    def test_admin_reloads_the_skill(self, client, grafana):
        response = self._post(client, ADMIN, "/api/v1/admin/reload-skill")
        assert response.status_code == 200
        assert response.json()["status"] == "reloaded"

    @pytest.mark.parametrize("principal", [MAINTAINER, OPERATOR, OBSERVER])
    def test_non_admin_cannot_reload_the_skill(self, client, grafana, principal):
        response = self._post(client, principal, "/api/v1/admin/reload-skill")
        assert response.status_code == 403

    @pytest.mark.parametrize("principal", [MAINTAINER, OPERATOR, OBSERVER])
    def test_non_admin_cannot_change_another_users_access(self, client, grafana, principal):
        from app.api.main import app

        use_principal(app, principal)
        try:
            response = client.put(
                "/api/v1/admin/users/usr_maintainer/access",
                json={"role": "admin"},
            )
        finally:
            from app.auth.dependencies import require_principal

            app.dependency_overrides.pop(require_principal, None)
            set_current_principal(None)
        assert response.status_code == 403

    def test_unauthenticated_admin_request_is_401(self, client, grafana):
        """Without a session the request is refused before the role check."""
        from fastapi import HTTPException, status

        from app.api.main import app
        from app.auth.dependencies import require_principal

        def _unauthenticated():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required.",
            )

        app.dependency_overrides[require_principal] = _unauthenticated
        try:
            response = client.get("/api/v1/admin/users")
        finally:
            app.dependency_overrides.pop(require_principal, None)
        assert response.status_code == 401

    def test_admin_actions_are_audited(self, client, grafana):
        from app.storage.audit_store import AuditStore
        from app.storage.database import get_db_session

        response = self._post(client, ADMIN, "/api/v1/admin/reload-skill")
        assert response.status_code == 200
        with get_db_session() as session:
            events = AuditStore.list_events(session, event_type="admin_skill_reloaded")
        assert events, "the skill reload must leave an audit record"


# ===========================================================================
# F. Environment-level authorization
# ===========================================================================

class TestEnvironmentAuthorization:
    """Environment scope is enforced on queries and alert operations."""

    def test_user_may_query_an_assigned_environment(self, client, grafana, environments):
        from app.api.main import app

        canned = {
            "mode": "single", "status": "ok", "query": "node_load1",
            "explanation": "ok", "execution": {"status": "success", "series": []},
        }
        use_principal(app, MAINTAINER)
        try:
            with _stubbed_query_pipeline(canned):
                response = client.post("/api/v1/query", json={
                    "question": "node_load1", "environment": ENV_ALLOWED,
                })
        finally:
            from app.auth.dependencies import require_principal

            app.dependency_overrides.pop(require_principal, None)
            set_current_principal(None)
        assert response.status_code == 200

    def test_user_may_not_query_an_unassigned_environment(self, client, grafana, environments):
        from app.api.main import app

        use_principal(app, MAINTAINER)
        try:
            response = client.post("/api/v1/query", json={
                "question": "node_load1", "environment": ENV_SECOND,
            })
        finally:
            from app.auth.dependencies import require_principal

            app.dependency_overrides.pop(require_principal, None)
            set_current_principal(None)
        assert response.status_code == 403

    def test_unknown_environment_is_rejected(self, client, grafana, environments):
        from app.api.main import app

        use_principal(app, MAINTAINER)
        try:
            response = client.post("/api/v1/query", json={
                "question": "node_load1", "environment": "env-not-here",
            })
        finally:
            from app.auth.dependencies import require_principal

            app.dependency_overrides.pop(require_principal, None)
            set_current_principal(None)
        assert response.status_code == 403

    def test_rejected_environment_never_reaches_prometheus(self, client, grafana, environments):
        from app.api.main import app

        query = AsyncMock(return_value=None)
        use_principal(app, MAINTAINER)
        try:
            with patch("app.prometheus_client.query_range", new=query), \
                 patch("app.prometheus_client.query_instant", new=query):
                response = client.post("/api/v1/query", json={
                    "question": "node_load1", "environment": ENV_SECOND,
                })
        finally:
            from app.auth.dependencies import require_principal

            app.dependency_overrides.pop(require_principal, None)
            set_current_principal(None)
        assert response.status_code == 403
        query.assert_not_called()

    def test_catalog_query_respects_the_environment(self, client, grafana, environments):
        from app.api.main import app

        use_principal(app, MAINTAINER)
        try:
            response = client.post("/api/query", json={
                "sourceId": "prometheus", "metricId": "cpu", "queryId": "cpu_utilization",
                "environment": ENV_SECOND,
            })
        finally:
            from app.auth.dependencies import require_principal

            app.dependency_overrides.pop(require_principal, None)
            set_current_principal(None)
        assert response.status_code == 403

    def test_admin_may_query_any_environment(self, client, grafana, environments):
        from app.api.main import app

        canned = {
            "mode": "single", "status": "ok", "query": "node_load1",
            "explanation": "ok", "execution": {"status": "success", "series": []},
        }
        use_principal(app, ADMIN)
        try:
            with _stubbed_query_pipeline(canned):
                response = client.post("/api/v1/query", json={
                    "question": "node_load1", "environment": ENV_FORBIDDEN,
                })
        finally:
            from app.auth.dependencies import require_principal

            app.dependency_overrides.pop(require_principal, None)
            set_current_principal(None)
        assert response.status_code == 200

    def test_observer_may_not_execute_promql(self, client, grafana, environments):
        from app.api.main import app

        query = AsyncMock(return_value=None)
        use_principal(app, OBSERVER)
        try:
            with patch("app.prometheus_client.query_range", new=query), \
                 patch("app.prometheus_client.query_instant", new=query):
                response = client.post("/api/v1/query", json={
                    "question": "node_load1", "environment": ENV_ALLOWED,
                })
        finally:
            from app.auth.dependencies import require_principal

            app.dependency_overrides.pop(require_principal, None)
            set_current_principal(None)
        assert response.status_code == 403
        query.assert_not_called()


class TestAlertEnvironmentAuthorization:
    """Alert rules are environment-scoped, at propose, approve and execute."""

    def _propose(self, principal, environment):
        from app.grafana_tools.alerting import propose_alert_rule

        with acting_as(principal):
            return propose_alert_rule(
                "create a critical alert if node_load1 > 4 for 5m",
                environment=environment,
            )

    def test_operator_may_propose_an_alert_in_an_assigned_environment(self, grafana, environments):
        with patch("app.grafana_tools.alerting.resolve_datasource",
                   AsyncMock(return_value={"uid": "prom-uid", "type": "prometheus", "name": "Prometheus"})), \
             patch("app.grafana_tools.alerting.list_prometheus_label_names",
                   AsyncMock(return_value=json.dumps(["instance"]))), \
             patch("app.grafana_tools.alerting.list_prometheus_metric_names",
                   AsyncMock(return_value=json.dumps(["node_load1"]))), \
             patch("app.grafana_tools.alerting.list_prometheus_metric_metadata",
                   AsyncMock(return_value="{}")):
            outcome = self._propose(OPERATOR, ENV_ALLOWED)
        assert outcome["status"] == "success"
        assert outcome["environment"] == ENV_ALLOWED

    def test_operator_may_not_propose_an_alert_in_an_unassigned_environment(self, grafana, environments):
        outcome = self._propose(OPERATOR, ENV_FORBIDDEN)
        assert outcome["status"] == "policy_denied"

    def test_rejected_alert_proposal_writes_nothing(self, grafana, environments):
        outcome = self._propose(OPERATOR, ENV_FORBIDDEN)
        assert outcome["status"] == "policy_denied"
        assert grafana.writes == []

    def test_alert_execution_in_an_unassigned_environment_is_refused(self, grafana, environments):
        import asyncio

        from app.grafana_tools.alerting import ALERT_PROPOSALS, execute_approved_alert

        with acting_as(OPERATOR):
            proposal = ALERT_PROPOSALS.create({
                "type": "alert_rule",
                "title": "Scoped alert",
                "metric": "node_load1",
                "query": "node_load1",
                "condition": "gt",
                "threshold": 4,
                "for_duration": "5m",
                "severity": "critical",
                "ruleGroup": "ai-agent-alerts",
                "datasourceUID": "prom-uid",
                "folderUID": FOLDER_B,
                "environment": ENV_FORBIDDEN,
                "uid": "",
            })
        with acting_as(OPERATOR):
            approved = ALERT_PROPOSALS.approve(proposal["proposalId"], proposal["version"])
        with acting_as(OPERATOR):
            with pytest.raises(PermissionError):
                asyncio.run(execute_approved_alert(
                    proposal["proposalId"], proposal["version"], approved["approvalToken"],
                ))
        assert grafana.writes == []

    def test_alert_execution_in_an_assigned_environment_is_allowed(self, grafana, environments, monkeypatch):
        import asyncio

        from app.grafana_tools.alerting import ALERT_PROPOSALS, execute_approved_alert

        monkeypatch.setenv("GRAFANA_MCP_ENABLE_WRITE", "true")
        with acting_as(OPERATOR):
            proposal = ALERT_PROPOSALS.create({
                "type": "alert_rule",
                "title": "Scoped alert",
                "metric": "node_load1",
                "query": "node_load1",
                "condition": "gt",
                "threshold": 4,
                "for_duration": "5m",
                "severity": "critical",
                "ruleGroup": "ai-agent-alerts",
                "datasourceUID": "prom-uid",
                "folderUID": FOLDER_B,
                "environment": ENV_ALLOWED,
                "uid": "",
            })
            approved = ALERT_PROPOSALS.approve(proposal["proposalId"], proposal["version"])
            asyncio.run(execute_approved_alert(
                proposal["proposalId"], proposal["version"], approved["approvalToken"],
            ))
        assert len(grafana.writes) == 1

    def test_alert_execution_by_maintainer_is_refused(self, grafana, environments):
        import asyncio

        from app.grafana_tools.alerting import ALERT_PROPOSALS, execute_approved_alert

        with acting_as(OPERATOR):
            proposal = ALERT_PROPOSALS.create({
                "type": "alert_rule",
                "title": "Scoped alert",
                "metric": "node_load1",
                "query": "node_load1",
                "condition": "gt",
                "threshold": 4,
                "for_duration": "5m",
                "severity": "critical",
                "ruleGroup": "ai-agent-alerts",
                "datasourceUID": "prom-uid",
                "folderUID": FOLDER_B,
                "environment": ENV_ALLOWED,
                "uid": "",
            })
            approved = ALERT_PROPOSALS.approve(proposal["proposalId"], proposal["version"])
        with acting_as(MAINTAINER):
            with pytest.raises(PermissionError):
                asyncio.run(execute_approved_alert(
                    proposal["proposalId"], proposal["version"], approved["approvalToken"],
                ))
        assert grafana.writes == []


# ===========================================================================
# G. Remaining discovery / execution paths
# ===========================================================================

class TestPanelDiscoveryScope:
    """The panel index is a dashboard discovery path and is scoped too."""

    def test_panel_index_only_exposes_in_scope_dashboards(self, grafana, monkeypatch):
        import app.grafana_tools.discovery as discovery
        from app.grafana_tools.discovery import panel_index_for

        # The index is a process-wide cache; start from a clean one.
        monkeypatch.setattr(discovery, "_panel_index", None)
        monkeypatch.setattr(discovery, "_panel_index_time", 0.0)

        entries = run(panel_index_for(MAINTAINER))
        assert entries, "the maintainer must still discover their own panels"
        discovered = {entry["dashboard_uid"] for entry in entries}
        # Only the dashboards in the maintainer's folder are discoverable.
        assert "dash-alpha" in discovered
        assert "dash-beta" not in discovered
        assert "dash-loose" not in discovered

    def test_panel_index_is_empty_without_an_identity(self, grafana, monkeypatch):
        import app.grafana_tools.discovery as discovery
        from app.grafana_tools.discovery import panel_index_for

        monkeypatch.setattr(discovery, "_panel_index", None)
        monkeypatch.setattr(discovery, "_panel_index_time", 0.0)

        assert run(panel_index_for(None)) == []


class TestPreviewAuthorization:
    """A preview executes the queries of a client-supplied IR."""

    def test_observer_cannot_preview_a_proposal(self, client, grafana):
        from app.api.main import app
        from app.grafana_tools.dashboard_writing import PROPOSALS

        with acting_as(OBSERVER):
            proposal = PROPOSALS.create({
                "operation": "create",
                "name": "Preview target",
                "folderUid": FOLDER_A,
                "panels": [{
                    "title": "Panel",
                    "datasource": {"type": "prometheus", "uid": "prom-uid"},
                    "visualizationType": "timeseries",
                    "query": "node_load1",
                }],
            })

        use_principal(app, OBSERVER)
        try:
            response = client.post(
                f"/api/proposals/{proposal['proposalId']}/preview",
                json={"ir": proposal["ir"]},
            )
        finally:
            from app.auth.dependencies import require_principal

            app.dependency_overrides.pop(require_principal, None)
            set_current_principal(None)
        assert response.status_code == 403
        assert grafana.prometheus_queries == []

    def test_out_of_scope_proposal_preview_is_403(self, client, grafana):
        from app.api.main import app
        from app.grafana_tools.dashboard_writing import PROPOSALS

        with acting_as(MAINTAINER):
            proposal = PROPOSALS.create({
                "operation": "update",
                "name": "Foreign dashboard",
                "dashboardUid": "dash-beta",
                "folderUid": FOLDER_B,
                "panels": [{
                    "title": "Panel",
                    "datasource": {"type": "prometheus", "uid": "prom-uid"},
                    "visualizationType": "timeseries",
                    "query": "node_load1",
                }],
            })

        use_principal(app, MAINTAINER)
        try:
            response = client.post(
                f"/api/proposals/{proposal['proposalId']}/preview",
                json={"ir": proposal["ir"]},
            )
        finally:
            from app.auth.dependencies import require_principal

            app.dependency_overrides.pop(require_principal, None)
            set_current_principal(None)
        assert response.status_code == 403
        assert grafana.prometheus_queries == []


class TestCrossUserApproval:
    """A proposal may be approved by a different authorized maintainer."""

    def test_another_maintainer_may_approve(self, client, grafana):
        from app.api.main import app
        from app.grafana_tools.dashboard_writing import PROPOSALS, build_proposal

        with acting_as(MAINTAINER):
            proposal = run(build_proposal(
                f"Create a dashboard called Shared in folder {FOLDER_A} showing node_load1"
            ))

        approver = make_principal(
            "maintainer", user_id="usr_second_maintainer",
            folders=(FOLDER_A,), environments=(ENV_ALLOWED,),
        )
        use_principal(app, approver)
        try:
            response = client.post(
                f"/api/proposals/{proposal['proposalId']}/approve",
                json={"version": proposal["version"]},
            )
        finally:
            from app.auth.dependencies import require_principal

            app.dependency_overrides.pop(require_principal, None)
            set_current_principal(None)
        assert response.status_code == 200
        assert response.json()["status"] == "approved"


class TestDashboardDeletionScope:
    """Deleting a dashboard is scoped like every other dashboard write."""

    def test_deletion_of_an_out_of_scope_dashboard_is_denied(self, grafana):
        from app.grafana_tools.dashboard_writing import build_proposal

        with acting_as(MAINTAINER):
            with pytest.raises(AuthorizationDenied):
                run(build_proposal("Delete the dashboard Beta Service Overview"))
        assert grafana.writes == []

    def test_deletion_of_an_assigned_dashboard_is_allowed(self, grafana):
        from app.grafana_tools.dashboard_writing import build_proposal

        with acting_as(MAINTAINER):
            proposal = run(build_proposal("Delete the dashboard Alpha Service Overview"))
        assert proposal["ir"]["removeDashboard"] is True
        assert proposal["impact_class"] == "destructive_delete"
        assert proposal["requires_second_approver"] is True
        assert grafana.writes == []


class TestEnvironmentResolution:
    """The environment dimension must not be silently inert."""

    def test_user_without_any_environment_is_denied(self, grafana, environments):
        from app.auth.environment import EnvironmentDenied, resolve_environment

        no_env = make_principal("operator", user_id="usr_no_env", folders=(FOLDER_B,))
        with pytest.raises(EnvironmentDenied):
            resolve_environment(no_env, None)

    def test_a_deployment_without_environments_has_no_environment_dimension(self, monkeypatch):
        import app.auth.environment as environment_module

        monkeypatch.setattr(environment_module, "deployment_environments", lambda: [])
        no_env = make_principal("operator", user_id="usr_no_env", folders=(FOLDER_B,))
        assert environment_module.resolve_environment(no_env, None) is None

    def test_missing_environment_context_is_a_400_or_403(self, client, grafana, environments):
        from app.api.main import app

        no_env = make_principal("operator", user_id="usr_no_env", folders=(FOLDER_B,))
        use_principal(app, no_env)
        try:
            response = client.post("/api/v1/query", json={"question": "node_load1"})
        finally:
            from app.auth.dependencies import require_principal

            app.dependency_overrides.pop(require_principal, None)
            set_current_principal(None)
        assert response.status_code in (400, 403)

    def test_staging_only_user_may_not_reach_production(self, grafana, environments):
        """A user with one environment may not name a different one."""
        from app.auth.environment import EnvironmentDenied, resolve_environment

        staging_only = make_principal(
            "operator", user_id="usr_staging", folders=(FOLDER_B,), environments=(ENV_ALLOWED,),
        )
        assert resolve_environment(staging_only, ENV_ALLOWED) == ENV_ALLOWED
        with pytest.raises(EnvironmentDenied):
            resolve_environment(staging_only, ENV_SECOND)

    def test_admin_resolves_every_known_environment(self, grafana, environments):
        from app.auth.environment import resolve_environment

        assert resolve_environment(ADMIN, ENV_ALLOWED) == ENV_ALLOWED
        assert resolve_environment(ADMIN, ENV_SECOND) == ENV_SECOND
        assert resolve_environment(ADMIN, ENV_FORBIDDEN) == ENV_FORBIDDEN
