"""tests/test_identity_propagation.py

Identity propagation and wrapper-level RBAC enforcement across the real
path of this architecture:

    Browser -> FastAPI -> Authenticated Principal -> ADK agent
            -> application wrapper functions -> upstream Grafana MCP -> Grafana

The tests here prove three things:

* the authenticated Principal survives every boundary between the FastAPI
  request and the application wrapper -- including the thread and event-loop
  boundaries the ADK runner and the MCP session bridge introduce;
* every wrapper that forwards a call to the upstream Grafana MCP server
  authorizes the caller *before* the call is forwarded, so a denied
  operation produces zero downstream invocations; and
* the raw, unguarded upstream Grafana MCP tools are never exposed to the
  agent.

The upstream Grafana MCP client is replaced by a recorder, so every test can
assert exactly which downstream calls were made -- and, just as importantly,
that none were made.
"""
from __future__ import annotations

import asyncio
import json
import threading
from contextlib import contextmanager
from typing import Any, Iterator

import pytest

from app.auth.environment import reset_environment_catalogue
from app.auth.principal import (
    CURRENT_PRINCIPAL,
    Principal,
    get_current_principal,
    principal_scope,
    set_current_principal,
)
from app.auth.resource_scope import AuthorizationDenied

# ---------------------------------------------------------------------------
# Synthetic scope data. None of these identifiers belong to the demo
# deployment: the authorization decisions must come from the scope data.
# ---------------------------------------------------------------------------

FOLDER_A = "fld-identity-team-a"
FOLDER_B = "fld-identity-team-b"
DASH_A = "dash-identity-a"
DASH_B = "dash-identity-b"
ENV_ALLOWED = "env-identity-alpha"
ENV_FORBIDDEN = "env-identity-forbidden"


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
    user_id="usr_identity_admin",
    folders=("*",),
    environments=(ENV_ALLOWED, ENV_FORBIDDEN),
)
MAINTAINER = make_principal(
    "maintainer",
    user_id="usr_identity_maintainer",
    folders=(FOLDER_A,),
    environments=(ENV_ALLOWED,),
)
OPERATOR = make_principal(
    "operator",
    user_id="usr_identity_operator",
    folders=(FOLDER_A,),
    environments=(ENV_ALLOWED,),
)
OBSERVER = make_principal(
    "observer",
    user_id="usr_identity_observer",
    folders=(FOLDER_A,),
    environments=(ENV_ALLOWED,),
)


@contextmanager
def acting_as(principal: Principal | None) -> Iterator[None]:
    """Bind a principal to the current context for the duration of a block."""
    token = set_current_principal(principal)
    try:
        yield
    finally:
        try:
            CURRENT_PRINCIPAL.reset(token)
        except (ValueError, LookupError):  # pragma: no cover - defensive
            set_current_principal(None)


# ---------------------------------------------------------------------------
# The fake upstream Grafana MCP client.
#
# It replaces ``mcp_call`` in every module that talks to the upstream
# Grafana MCP server and records each invocation, so a test can prove that a
# denied operation never reached Grafana.
# ---------------------------------------------------------------------------

class RecordingMcp:
    """Records every call the application would forward to Grafana MCP."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    # -- recording ---------------------------------------------------------
    @property
    def tools(self) -> list[str]:
        with self._lock:
            return [call["tool"] for call in self.calls]

    @property
    def count(self) -> int:
        with self._lock:
            return len(self.calls)

    def reset(self) -> None:
        with self._lock:
            self.calls.clear()

    # -- the fake call -----------------------------------------------------
    async def mcp_call(
        self,
        tool: str,
        args: dict[str, Any] | None = None,
        *,
        retries: int = 1,
        raw: bool = False,
        timeout: float = 15.0,
    ) -> str:
        args = dict(args or {})
        with self._lock:
            self.calls.append({"tool": tool, "args": args})
        return self._respond(tool, args)

    def _respond(self, tool: str, args: dict[str, Any]) -> str:
        if tool == "grafana_api_request":
            method = str(args.get("method") or "GET").upper()
            endpoint = str(args.get("endpoint") or "")
            if method == "GET" and endpoint.startswith("/api/search"):
                return json.dumps(
                    [
                        {
                            "uid": DASH_A,
                            "title": "Team A dashboard",
                            "tags": ["a"],
                            "folderUid": FOLDER_A,
                        },
                        {
                            "uid": DASH_B,
                            "title": "Team B dashboard",
                            "tags": ["b"],
                            "folderUid": FOLDER_B,
                        },
                    ]
                )
            if method == "GET" and endpoint.startswith("/api/dashboards/uid/"):
                uid = endpoint.rsplit("/", 1)[-1]
                folder = FOLDER_A if uid == DASH_A else FOLDER_B
                return json.dumps(
                    {
                        "dashboard": {
                            "uid": uid,
                            "title": f"Dashboard {uid}",
                            "panels": [{"id": 1, "title": "CPU", "type": "timeseries"}],
                        },
                        "meta": {"folderUid": folder},
                    }
                )
            if method == "GET" and endpoint.startswith("/api/folders"):
                return json.dumps(
                    [
                        {"uid": FOLDER_A, "title": "Team A"},
                        {"uid": FOLDER_B, "title": "Team B"},
                    ]
                )
            if method == "GET" and endpoint.startswith("/api/v1/provisioning/alert-rules"):
                return json.dumps(
                    [
                        {"uid": "rule-a", "title": "Rule A", "folderUID": FOLDER_A},
                        {"uid": "rule-b", "title": "Rule B", "folderUID": FOLDER_B},
                    ]
                )
            # Any write reaches the recorder, which is what the "no mutation"
            # assertions are about.
            return json.dumps({"status": "ok", "uid": "written"})

        if tool == "get_dashboard_by_uid":
            uid = str(args.get("uid") or "")
            folder = FOLDER_A if uid == DASH_A else FOLDER_B
            return json.dumps(
                {
                    "dashboard": {
                        "uid": uid,
                        "title": f"Dashboard {uid}",
                        "panels": [{"id": 1, "title": "CPU", "type": "timeseries"}],
                    },
                    "meta": {"folderUid": folder},
                }
            )

        if tool == "get_dashboard_summary":
            uid = str(args.get("uid") or "")
            return json.dumps(
                {
                    "uid": uid,
                    "title": f"Dashboard {uid}",
                    "panels": [
                        {
                            "title": "CPU",
                            "type": "timeseries",
                            "description": "CPU usage",
                            "queryCount": 1,
                        }
                    ],
                    "variables": [],
                    "timeRange": {"from": "now-1h", "to": "now"},
                }
            )

        if tool == "search_dashboards":
            return json.dumps(
                {
                    "dashboards": [
                        {"uid": DASH_A, "title": "Team A dashboard", "tags": [], "folderUid": FOLDER_A},
                        {"uid": DASH_B, "title": "Team B dashboard", "tags": [], "folderUid": FOLDER_B},
                    ]
                }
            )

        if tool == "list_datasources":
            return json.dumps(
                [
                    {
                        "uid": "ds-prom",
                        "name": "Prometheus",
                        "type": "prometheus",
                        "isDefault": True,
                    }
                ]
            )

        if tool == "query_prometheus":
            return json.dumps(
                {
                    "data": {
                        "result": [
                            {
                                "metric": {"__name__": "up", "instance": "node-1"},
                                "values": [[1, "1"]],
                            }
                        ]
                    }
                }
            )

        if tool == "list_prometheus_metric_names":
            return json.dumps(["node_cpu_seconds_total"])

        if tool == "list_prometheus_label_names":
            return json.dumps(["instance", "__name__"])

        if tool == "list_prometheus_metric_metadata":
            return json.dumps({"node_cpu_seconds_total": {"type": "counter"}})

        if tool == "query_elasticsearch":
            return json.dumps({"hits": {"hits": []}})

        return json.dumps({})


#: The modules that hold their own reference to ``mcp_call``.
_MCP_CALLERS = (
    "app.grafana_tools.dashboard",
    "app.grafana_tools.prometheus",
    "app.grafana_tools.opensearch",
    "app.grafana_tools.datasource",
    "app.grafana_tools.dashboard_writing",
    "app.grafana_tools.alerting",
)


@pytest.fixture()
def mcp(monkeypatch) -> Iterator[RecordingMcp]:
    """Replace the upstream Grafana MCP client with a recorder."""
    import importlib

    recorder = RecordingMcp()
    for module_name in _MCP_CALLERS:
        module = importlib.import_module(module_name)
        if hasattr(module, "mcp_call"):
            monkeypatch.setattr(module, "mcp_call", recorder.mcp_call)
    return recorder


@pytest.fixture(autouse=True)
def _reset_caches():
    """Keep the module-level caches from leaking between tests."""
    import app.grafana_tools.dashboard as dashboard_module
    import app.grafana_tools.datasource as datasource_module
    import app.grafana_tools.discovery as discovery_module

    dashboard_module._dashboard_list = None
    dashboard_module._dashboard_details = {}
    datasource_module._datasource_registry = None
    discovery_module._panel_index = None
    reset_environment_catalogue()
    yield
    dashboard_module._dashboard_list = None
    dashboard_module._dashboard_details = {}
    datasource_module._datasource_registry = None
    discovery_module._panel_index = None
    reset_environment_catalogue()


# ---------------------------------------------------------------------------
# 1. The Principal reaches the wrappers at all.
# ---------------------------------------------------------------------------

class TestIdentityReachesTheWrappers:
    def test_a_wrapper_without_an_identity_is_refused_with_401(self, mcp):
        """No identity means no operation -- and no downstream call."""
        from app.grafana_tools.wrapper import list_dashboards

        with pytest.raises(AuthorizationDenied) as excinfo:
            list_dashboards()
        assert excinfo.value.status_code == 401
        assert mcp.count == 0

    def test_an_authenticated_caller_reaches_the_upstream_client(self, mcp):
        from app.grafana_tools.wrapper import list_dashboards

        with acting_as(ADMIN):
            output = list_dashboards()

        assert "Team A dashboard" in output
        assert mcp.count >= 1

    def test_the_identity_survives_the_mcp_bridge_thread(self):
        """The MCP session bridge runs on its own thread and event loop."""
        from app.mcp.session import run_sync

        async def _read_identity() -> str:
            principal = get_current_principal()
            return principal.user_id if principal else ""

        with acting_as(MAINTAINER):
            assert run_sync(_read_identity()) == MAINTAINER.user_id

    def test_the_identity_survives_the_adk_thread_boundary(self):
        """The ADK turn runs on a worker thread; the identity must come with it."""
        from app.adk_agent import _run_with_preserved_context

        async def _read_identity() -> str:
            principal = get_current_principal()
            return principal.user_id if principal else ""

        with acting_as(OPERATOR):
            assert _run_with_preserved_context(_read_identity) == OPERATOR.user_id

    def test_the_identity_is_unbound_after_the_invocation(self):
        from app.adk_agent import _run_with_preserved_context

        async def _read_identity() -> str:
            principal = get_current_principal()
            return principal.user_id if principal else ""

        with acting_as(ADMIN):
            _run_with_preserved_context(_read_identity)
            # The invocation's own binding is restored, not replaced.
            assert get_current_principal() is ADMIN

        assert get_current_principal() is None

    def test_concurrent_invocations_do_not_share_an_identity(self):
        """Two callers running at the same time never see each other."""
        from app.adk_agent import _run_with_preserved_context

        observed: dict[str, str] = {}
        start = threading.Barrier(2)

        async def _read_identity() -> str:
            principal = get_current_principal()
            return principal.user_id if principal else ""

        def _worker(principal: Principal) -> None:
            with acting_as(principal):
                start.wait(timeout=10)
                observed[principal.user_id] = _run_with_preserved_context(_read_identity)

        threads = [
            threading.Thread(target=_worker, args=(principal,))
            for principal in (MAINTAINER, OBSERVER)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert observed == {
            MAINTAINER.user_id: MAINTAINER.user_id,
            OBSERVER.user_id: OBSERVER.user_id,
        }


# ---------------------------------------------------------------------------
# 2. The real ADK path: a runner that calls an application wrapper.
# ---------------------------------------------------------------------------

def _stub_model(script: list[dict[str, Any]]):
    """A model that replays a scripted list of function calls and texts."""
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse
    from google.genai import types as genai_types

    class _Stub(BaseLlm):
        model: str = "stub-model"
        script: list[dict[str, Any]] = []
        turn: int = 0
        lock: Any = None

        async def generate_content_async(self, llm_request, stream: bool = False):
            with self.lock:
                step = self.script[self.turn] if self.turn < len(self.script) else {"text": "done"}
                self.turn += 1
            if "name" in step:
                part = genai_types.Part(
                    function_call=genai_types.FunctionCall(
                        name=step["name"], args=step.get("args") or {}
                    )
                )
            else:
                part = genai_types.Part(text=step.get("text") or "")
            yield LlmResponse(content=genai_types.Content(role="model", parts=[part]))

    return _Stub(model="stub-model", script=script, turn=0, lock=threading.Lock())


def _install_stub_runner(monkeypatch, script: list[dict[str, Any]], tools: list[Any]):
    """Point the agent module at a runner whose model is a stub."""
    from google.adk.agents import LlmAgent
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService

    import app.adk_agent as adk_agent

    agent = LlmAgent(
        name="identity_test_agent",
        model=_stub_model(script),
        description="Test agent.",
        instruction="Call the tool.",
        tools=tools,
    )
    runner = Runner(
        app_name="identity_test",
        agent=agent,
        session_service=InMemorySessionService(),
        auto_create_session=True,
    )
    monkeypatch.setattr(adk_agent, "_agent_instance", agent)
    monkeypatch.setattr(adk_agent, "_runner_instance", runner)
    return agent, runner


class TestAdkInvocationCarriesTheIdentity:
    def test_the_agent_reaches_a_wrapper_with_the_callers_identity(self, mcp, monkeypatch):
        """The full chain: ADK -> wrapper -> upstream Grafana MCP."""
        from app.adk_agent import scope_guarded
        from app.grafana_tools.wrapper import list_dashboards

        seen: dict[str, Any] = {}

        def _recording_wrapper() -> str:
            principal = get_current_principal()
            seen["user_id"] = principal.user_id if principal else None
            return list_dashboards()

        _install_stub_runner(
            monkeypatch,
            [{"name": "_recording_wrapper", "args": {}}, {"text": "done"}],
            [scope_guarded(_recording_wrapper)],
        )

        import app.adk_agent as adk_agent

        with acting_as(MAINTAINER):
            adk_agent.run_adk_agent(
                request="show me the dashboards",
                conversation_id="conv-identity-1",
                principal=MAINTAINER,
            )

        assert seen["user_id"] == MAINTAINER.user_id
        assert mcp.count >= 1

    def test_a_denied_agent_tool_never_reaches_grafana(self, mcp, monkeypatch):
        """An observer's PromQL query is refused before the MCP client runs."""
        from app.adk_agent import scope_guarded
        from app.grafana_tools.wrapper import query_prometheus_metric

        _install_stub_runner(
            monkeypatch,
            [
                {"name": "query_prometheus_metric", "args": {"expr": "up"}},
                {"text": "done"},
            ],
            [scope_guarded(query_prometheus_metric)],
        )

        import app.adk_agent as adk_agent

        with acting_as(OBSERVER):
            adk_agent.run_adk_agent(
                request="what is the CPU usage",
                conversation_id="conv-identity-2",
                principal=OBSERVER,
            )

        assert mcp.count == 0

    def _observe_the_identity(self, monkeypatch) -> dict[str, Any]:
        """Install a runner whose only tool records the identity it sees."""
        from app.adk_agent import scope_guarded
        from app.grafana_tools.wrapper import list_dashboards

        seen: dict[str, Any] = {}

        def _recording_wrapper() -> str:
            principal = get_current_principal()
            seen["user_id"] = principal.user_id if principal else None
            return list_dashboards()

        _install_stub_runner(
            monkeypatch,
            [{"name": "_recording_wrapper", "args": {}}, {"text": "done"}],
            [scope_guarded(_recording_wrapper)],
        )
        return seen

    def test_the_explicit_principal_is_bound_without_an_ambient_context(
        self, mcp, monkeypatch
    ):
        """The route's principal reaches the wrapper on its own merit."""
        seen = self._observe_the_identity(monkeypatch)

        import app.adk_agent as adk_agent

        with acting_as(None):
            adk_agent.run_adk_agent(
                request="show me the dashboards",
                conversation_id="conv-identity-3",
                principal=MAINTAINER,
            )

        assert seen["user_id"] == MAINTAINER.user_id

    def test_the_invocation_clears_its_identity_afterwards(self, mcp, monkeypatch):
        """No identity survives the turn in the caller's context."""
        seen = self._observe_the_identity(monkeypatch)

        import app.adk_agent as adk_agent

        with acting_as(None):
            adk_agent.run_adk_agent(
                request="show me the dashboards",
                conversation_id="conv-identity-4",
                principal=OBSERVER,
            )
            assert get_current_principal() is None

        assert seen["user_id"] == OBSERVER.user_id

    def test_a_turn_without_any_identity_reaches_nothing(self, mcp, monkeypatch):
        """A turn with no identity at all is refused at the wrapper."""
        from app.adk_agent import scope_guarded
        from app.grafana_tools.wrapper import list_dashboards

        _install_stub_runner(
            monkeypatch,
            [{"name": "list_dashboards", "args": {}}, {"text": "done"}],
            [scope_guarded(list_dashboards)],
        )

        import app.adk_agent as adk_agent

        with acting_as(None):
            adk_agent.run_adk_agent(
                request="show me the dashboards",
                conversation_id="conv-identity-5",
            )

        assert mcp.count == 0


# ---------------------------------------------------------------------------
# 3. Role behaviour at the wrapper boundary.
# ---------------------------------------------------------------------------

class TestRoleBoundaries:
    def test_an_admin_may_read_and_query(self, mcp):
        from app.grafana_tools.wrapper import list_dashboards, query_prometheus_metric

        with acting_as(ADMIN):
            listing = list_dashboards()
            answer = query_prometheus_metric(expr="up")

        assert "Team A dashboard" in listing
        assert "Team B dashboard" in listing
        assert "Error" not in answer
        assert "query_prometheus" in mcp.tools

    def test_a_maintainer_only_sees_its_own_folder(self, mcp):
        from app.grafana_tools.wrapper import list_dashboards

        with acting_as(MAINTAINER):
            listing = list_dashboards()

        assert "Team A dashboard" in listing
        assert "Team B dashboard" not in listing
        # The upstream read still happened -- the *result* was filtered.
        assert mcp.count >= 1

    def test_a_maintainer_cannot_read_an_out_of_scope_dashboard(self, mcp):
        from app.grafana_tools.wrapper import get_dashboard_panels

        with acting_as(MAINTAINER):
            with pytest.raises(AuthorizationDenied) as excinfo:
                get_dashboard_panels(DASH_B)

        assert excinfo.value.status_code == 403
        # The only call that reached Grafana was the authoritative lookup of
        # the dashboard's folder -- the scope decision cannot be made without
        # it. The dashboard's *content* was never fetched.
        assert "get_dashboard_summary" not in mcp.tools
        assert "update_dashboard" not in mcp.tools

    def test_a_maintainer_may_read_an_in_scope_dashboard(self, mcp):
        from app.grafana_tools.wrapper import get_dashboard_panels

        with acting_as(MAINTAINER):
            output = get_dashboard_panels(DASH_A)

        assert "CPU" in output
        assert mcp.count >= 1

    def test_an_operator_may_query_but_not_propose_dashboards(self, mcp):
        from app.grafana_tools.dashboard_writing import propose_dashboard
        from app.grafana_tools.wrapper import query_prometheus_metric

        with acting_as(OPERATOR):
            answer = query_prometheus_metric(expr="up")
            before = mcp.count
            outcome = propose_dashboard("Create a dashboard called Nope")
            after = mcp.count

        assert "Error" not in answer
        assert outcome["status"] == "policy_denied"
        # The refused proposal never reached the Grafana MCP client.
        assert after == before
        assert "update_dashboard" not in mcp.tools

    def test_an_observer_is_read_only(self, mcp):
        from app.grafana_tools.wrapper import get_dashboard_panels, query_prometheus_metric

        with acting_as(OBSERVER):
            answer = query_prometheus_metric(expr="up")
            # A read of an in-scope dashboard is still permitted.
            panels = get_dashboard_panels(DASH_A)

        assert answer.startswith("Error: RBAC Access Denied")
        assert "CPU" in panels
        assert "query_prometheus" not in mcp.tools

    def test_an_inactive_account_is_refused(self, mcp):
        from app.grafana_tools.wrapper import list_dashboards

        inactive = make_principal(
            "maintainer", user_id="usr_identity_inactive", folders=(FOLDER_A,),
            environments=(ENV_ALLOWED,), status="deactivated",
        )
        with acting_as(inactive):
            with pytest.raises(AuthorizationDenied) as excinfo:
                list_dashboards()
        assert excinfo.value.status_code == 403
        assert mcp.count == 0


# ---------------------------------------------------------------------------
# 4. Environment scope.
# ---------------------------------------------------------------------------

class TestEnvironmentScope:
    def test_a_query_outside_the_permitted_environment_is_refused(self, mcp):
        from app.grafana_tools.wrapper import query_prometheus_metric

        with acting_as(MAINTAINER):
            answer = query_prometheus_metric(expr="up", environment=ENV_FORBIDDEN)

        assert answer.startswith("Error: RBAC Access Denied")
        assert mcp.count == 0

    def test_a_query_inside_the_permitted_environment_is_allowed(self, mcp):
        from app.grafana_tools.wrapper import query_prometheus_metric

        with acting_as(MAINTAINER):
            answer = query_prometheus_metric(expr="up", environment=ENV_ALLOWED)

        assert "Error" not in answer
        assert "query_prometheus" in mcp.tools

    def test_alert_rules_are_filtered_by_folder(self, mcp):
        from app.grafana_tools.wrapper import list_alert_rules

        with acting_as(MAINTAINER):
            listing = list_alert_rules()

        assert "Rule A" in listing
        assert "Rule B" not in listing
        assert mcp.count >= 1

    def test_an_alert_listing_without_an_identity_is_refused(self, mcp):
        from app.grafana_tools.wrapper import list_alert_rules

        with pytest.raises(AuthorizationDenied) as excinfo:
            list_alert_rules()
        assert excinfo.value.status_code == 401
        assert mcp.count == 0


# ---------------------------------------------------------------------------
# 5. Revocation takes effect on the next operation.
# ---------------------------------------------------------------------------

class TestRevocation:
    def test_a_revocation_in_the_role_store_applies_to_the_next_operation(
        self, mcp, audit_db
    ):
        """The principal is re-resolved from the store for every request."""
        from app.auth.principal import Principal as StorePrincipal
        from app.grafana_tools.wrapper import list_dashboards
        from app.storage.database import get_db_session
        from app.storage.user_store import UserStore

        issuer = "https://authentik.test"
        subject = "sub_identity_store_revocation"

        with get_db_session() as session:
            user = UserStore.create_pending_user(
                session, issuer=issuer, subject=subject, username="store-user"
            )
            user.status = "active"
            user.role = "maintainer"
            user.scopes.allowed_folder_uids = [FOLDER_A]
            user.scopes.allowed_environments = [ENV_ALLOWED]
            session.flush()
            user_id = user.user_id

        def _resolve() -> Principal:
            """Resolve the principal the way the request dependency does."""
            with get_db_session() as session:
                record = UserStore.get_user_by_identity(session, issuer, subject)
                return StorePrincipal.from_user_model(record)

        with acting_as(_resolve()):
            before = list_dashboards()
        assert "Team A dashboard" in before
        assert "Team B dashboard" not in before

        # An administrator revokes the folder scope in the role store.
        with get_db_session() as session:
            UserStore.update_user_access(
                session,
                admin_user_id=ADMIN.user_id,
                target_user_id=user_id,
                scopes={"allowed_folder_uids": [FOLDER_B]},
                admin_role="admin",
            )

        with acting_as(_resolve()):
            after = list_dashboards()
        assert "Team A dashboard" not in after
        assert "Team B dashboard" in after

    def test_a_revoked_folder_is_gone_on_the_next_operation(self, mcp):
        """The principal is re-resolved from the store for every request."""
        from app.grafana_tools.wrapper import list_dashboards

        with acting_as(MAINTAINER):
            before = list_dashboards()
        assert "Team B dashboard" not in before

        revoked = make_principal(
            "maintainer",
            user_id=MAINTAINER.user_id,
            folders=(FOLDER_B,),
            environments=(ENV_ALLOWED,),
        )
        with acting_as(revoked):
            after = list_dashboards()

        assert "Team A dashboard" not in after
        assert "Team B dashboard" in after

    def test_a_revoked_executor_cannot_execute_and_nothing_is_written(self, mcp, monkeypatch):
        """Execution re-validates the executor against the *current* scope."""
        from app.grafana_tools.dashboard_writing import PROPOSALS, execute_approved_mutation

        proposer = make_principal(
            "maintainer", user_id="usr_identity_proposer", folders=(FOLDER_A,),
            environments=(ENV_ALLOWED,),
        )
        approver = make_principal(
            "maintainer", user_id="usr_identity_approver", folders=(FOLDER_A,),
            environments=(ENV_ALLOWED,),
        )
        ir = {
            "operation": "create",
            "name": "Revocation dashboard",
            "panels": [
                {
                    "id": "panel-1",
                    "title": "CPU",
                    "metric": "up",
                    "query": "up",
                    "visualizationType": "timeseries",
                    "layout": {"x": 0, "y": 0},
                    "size": {"w": 12, "h": 8},
                    "datasource": {"type": "prometheus", "uid": "ds-prom"},
                }
            ],
            "folderUid": FOLDER_A,
        }

        with acting_as(proposer):
            proposal = PROPOSALS.create(ir)
        with acting_as(approver):
            approved = PROPOSALS.approve(
                proposal["proposalId"], proposal["version"],
                approver_id=approver.user_id, approver_role=approver.role,
            )

        revoked = make_principal(
            "maintainer", user_id=approver.user_id, folders=(),
            environments=(ENV_ALLOWED,),
        )
        monkeypatch.setenv("GRAFANA_MCP_ENABLE_WRITE", "true")
        with acting_as(revoked):
            with pytest.raises((AuthorizationDenied, PermissionError)):
                asyncio.run(
                    execute_approved_mutation(
                        proposal["proposalId"], proposal["version"], approved["approvalToken"]
                    )
                )

        assert "update_dashboard" not in mcp.tools


# ---------------------------------------------------------------------------
# 6. No unguarded upstream tool is exposed to the agent.
# ---------------------------------------------------------------------------

class TestNoUnguardedMcpTools:
    def test_the_agent_only_exposes_application_wrappers(self):
        import app.adk_agent as adk_agent
        import app.grafana_tools.wrapper as wrapper_module

        agent = adk_agent.get_adk_agent()
        names = {getattr(tool, "__name__", repr(tool)) for tool in agent.tools}

        # Every tool is one of the application's own wrapper functions.
        assert names <= {
            "list_dashboards",
            "search_dashboards",
            "get_dashboard_panels",
            "query_prometheus_metric",
            "query_opensearch_logs",
            "propose_dashboard",
            "resolve_dashboard_intent",
            "list_alert_rules",
            "propose_alert_rule",
            "resolve_alert_intent",
        }
        assert wrapper_module.list_dashboards.__module__ == "app.grafana_tools.wrapper"

    def test_no_mcp_toolset_is_registered_with_the_agent(self):
        """The upstream Grafana MCP tools are never handed to the agent."""
        import inspect

        import app.adk_agent as adk_agent

        source = inspect.getsource(adk_agent)
        assert "MCPToolset" not in source
        assert "mcp_tool" not in source
        # The only MCP import is the upstream client used by the wrappers.
        assert "from mcp import" not in source

    def test_the_tool_schemas_carry_no_identity_arguments(self):
        """Identity is never a tool argument -- it is server-side state."""
        import inspect

        from app.grafana_tools import alerting, dashboard_writing, wrapper

        for func in (
            wrapper.list_dashboards,
            wrapper.search_dashboards,
            wrapper.get_dashboard_panels,
            wrapper.query_prometheus_metric,
            wrapper.query_opensearch_logs,
            wrapper.list_alert_rules,
            wrapper.propose_alert_rule,
            dashboard_writing.propose_dashboard,
            alerting.propose_alert_rule,
        ):
            parameters = set(inspect.signature(func).parameters)
            assert not parameters & {"user_id", "userId", "role", "scopes", "principal", "token"}


# ---------------------------------------------------------------------------
# 7. The audit log records the real actor.
# ---------------------------------------------------------------------------

@pytest.fixture()
def audit_db():
    """Make sure the audit tables exist in the isolated test database."""
    from app.storage.database import init_db

    init_db(auto_seed=False)
    return True


class TestAuditRecordsTheRealActor:
    def test_a_denied_agent_tool_is_audited_with_the_authenticated_actor(
        self, mcp, monkeypatch, audit_db
    ):
        from app.adk_agent import scope_guarded
        from app.auth.audit import AuditEvent
        from app.grafana_tools.wrapper import query_prometheus_metric
        from app.storage.audit_store import AuditStore
        from app.storage.database import get_db_session

        _install_stub_runner(
            monkeypatch,
            [
                {"name": "query_prometheus_metric", "args": {"expr": "up"}},
                {"text": "done"},
            ],
            [scope_guarded(query_prometheus_metric)],
        )

        import app.adk_agent as adk_agent

        with acting_as(OBSERVER):
            adk_agent.run_adk_agent(
                request="what is the CPU usage",
                conversation_id="conv-audit-1",
                principal=OBSERVER,
            )

        with get_db_session() as session:
            events = [
                (event.actor_id, event.decision)
                for event in AuditStore.list_events(
                    session, event_type=AuditEvent.SCOPE_VIOLATION, limit=50
                )
            ]

        assert events, "the denial was not audited"
        assert (OBSERVER.user_id, "DENY") in events

    def test_a_proposal_created_through_the_agent_is_audited_with_the_real_actor(
        self, mcp, monkeypatch, audit_db
    ):
        """The proposer recorded in the audit log is the authenticated caller.

        The proposal is created on the agent's worker thread, so this also
        proves the identity survived the thread boundary.
        """
        from app.auth.audit import AuditEvent
        from app.grafana_tools.dashboard_writing import PROPOSALS
        from app.storage.audit_store import AuditStore
        from app.storage.database import get_db_session

        ir = {
            "operation": "create",
            "name": "Audited dashboard",
            "panels": [
                {
                    "id": "panel-1",
                    "title": "CPU",
                    "metric": "up",
                    "query": "up",
                    "visualizationType": "timeseries",
                    "layout": {"x": 0, "y": 0},
                    "size": {"w": 12, "h": 8},
                    "datasource": {"type": "prometheus", "uid": "ds-prom"},
                }
            ],
            "folderUid": FOLDER_A,
        }

        def _create_proposal() -> str:
            PROPOSALS.create(ir)
            return "created"

        _install_stub_runner(
            monkeypatch,
            [{"name": "_create_proposal", "args": {}}, {"text": "done"}],
            [_create_proposal],
        )

        import app.adk_agent as adk_agent

        with acting_as(MAINTAINER):
            adk_agent.run_adk_agent(
                request="create a dashboard",
                conversation_id="conv-audit-2",
                principal=MAINTAINER,
            )

        with get_db_session() as session:
            actors = [
                event.actor_id
                for event in AuditStore.list_events(
                    session, event_type=AuditEvent.PROPOSAL_CREATED, limit=50
                )
            ]

        assert actors, "the proposal creation was not audited"
        assert MAINTAINER.user_id in actors
        assert ADMIN.user_id not in actors


# ---------------------------------------------------------------------------
# 9. The alert proposal review path.
# ---------------------------------------------------------------------------

class TestAlertProposalReview:
    """Rejecting an alert proposal is an administrative act."""

    def _proposal(self):
        from app.grafana_tools.alerting import ALERT_PROPOSALS

        ir = {
            "type": "alert_rule",
            "title": "CPU alert",
            "metric": "node_cpu_seconds_total",
            "query": "rate(node_cpu_seconds_total[5m]) > 0.9",
            "datasourceUID": "ds-prom",
            "condition": "gt",
            "threshold": 0.9,
            "for_duration": "5m",
            "severity": "critical",
            "ruleGroup": "ai-agent-alerts",
            "folderUID": FOLDER_A,
            "environment": ENV_ALLOWED,
        }
        with acting_as(OPERATOR):
            return ALERT_PROPOSALS.create(ir)

    def test_an_observer_may_not_reject_an_alert_proposal(self):
        from fastapi.testclient import TestClient

        from app.api.main import app
        from app.auth.dependencies import require_principal

        proposal = self._proposal()
        app.dependency_overrides[require_principal] = lambda: OBSERVER
        try:
            with TestClient(app) as client:
                response = client.post(
                    f"/api/alert-proposals/{proposal['proposalId']}/reject",
                    json={"version": proposal["version"]},
                )
        finally:
            app.dependency_overrides.pop(require_principal, None)

        assert response.status_code == 403
        assert "policy denied" in response.json()["detail"].lower()

    def test_a_proposer_may_not_reject_their_own_alert_proposal(self):
        from fastapi.testclient import TestClient

        from app.api.main import app
        from app.auth.dependencies import require_principal

        proposal = self._proposal()
        app.dependency_overrides[require_principal] = lambda: OPERATOR
        try:
            with TestClient(app) as client:
                response = client.post(
                    f"/api/alert-proposals/{proposal['proposalId']}/reject",
                    json={"version": proposal["version"]},
                )
        finally:
            app.dependency_overrides.pop(require_principal, None)

        assert response.status_code == 403
        assert "policy denied" in response.json()["detail"].lower()

    def test_an_operator_may_reject_an_alert_proposal(self):
        from fastapi.testclient import TestClient

        from app.api.main import app
        from app.auth.dependencies import require_principal

        proposal = self._proposal()
        reviewer = make_principal(
            "operator",
            user_id="usr_identity_reviewer",
            folders=(FOLDER_A,),
            environments=(ENV_ALLOWED,),
        )
        app.dependency_overrides[require_principal] = lambda: reviewer
        try:
            with TestClient(app) as client:
                response = client.post(
                    f"/api/alert-proposals/{proposal['proposalId']}/reject",
                    json={"version": proposal["version"]},
                )
        finally:
            app.dependency_overrides.pop(require_principal, None)

        assert response.status_code == 200
        assert response.json()["status"] == "rejected"


# ---------------------------------------------------------------------------
# 10. The live Grafana MCP integration.
#
# These tests run against the real upstream ``mcp-grafana`` stdio server and
# the real Grafana instance. They are skipped when Grafana is unreachable.
# ---------------------------------------------------------------------------

def _grafana_request(path: str) -> Any:
    import urllib.request

    from app.config import get_settings

    settings = get_settings()
    request = urllib.request.Request(
        f"{settings.grafana_url}{path}",
        headers={"Authorization": f"Bearer {settings.grafana_service_account_token}"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def _grafana_is_up() -> bool:
    try:
        _grafana_request("/api/health")
        return True
    except Exception:  # pragma: no cover - the environment may have no Grafana
        return False


class TestLiveGrafanaIntegration:
    def test_a_permitted_operation_reaches_the_real_grafana(self):
        """An administrator's listing really comes from Grafana."""
        if not _grafana_is_up():  # pragma: no cover - depends on the environment
            pytest.skip("Grafana is not reachable.")

        from app.grafana_tools.wrapper import list_dashboards

        remote = _grafana_request("/api/search?type=dash-db")
        assert remote, "the live Grafana instance reports no dashboards"

        with acting_as(ADMIN):
            listing = list_dashboards()

        for dashboard in remote:
            assert dashboard["uid"] in listing, (
                f"dashboard {dashboard['uid']} is missing from the wrapper's listing"
            )

    def test_an_out_of_scope_read_is_rejected_before_the_mcp_content_read(
        self, monkeypatch
    ):
        """The panel content is never fetched for an out-of-scope dashboard."""
        if not _grafana_is_up():  # pragma: no cover - depends on the environment
            pytest.skip("Grafana is not reachable.")

        import app.grafana_tools.dashboard as dashboard_module
        from app.grafana_tools.wrapper import get_dashboard_panels
        from app.mcp.session import mcp_call as real_mcp_call

        remote = _grafana_request("/api/search?type=dash-db")
        if not remote:  # pragma: no cover - depends on the environment
            pytest.skip("the live Grafana instance reports no dashboards")
        target = remote[0]

        tools: list[str] = []

        async def _spy(tool, args, **kwargs):
            tools.append(tool)
            return await real_mcp_call(tool, args, **kwargs)

        monkeypatch.setattr(dashboard_module, "mcp_call", _spy)

        # A maintainer whose scope does not contain the dashboard's folder.
        scoped = make_principal(
            "maintainer",
            user_id="usr_live_scoped",
            folders=("fld-live-not-mine",),
            environments=(ENV_ALLOWED,),
        )
        with acting_as(scoped):
            with pytest.raises(AuthorizationDenied) as excinfo:
                get_dashboard_panels(target["uid"])

        assert excinfo.value.status_code == 403
        assert "get_dashboard_summary" not in tools
        assert "update_dashboard" not in tools

    def test_a_permitted_read_matches_the_real_grafana_state(self, monkeypatch):
        """The wrapper's answer agrees with what Grafana itself reports."""
        if not _grafana_is_up():  # pragma: no cover - depends on the environment
            pytest.skip("Grafana is not reachable.")

        from app.grafana_tools.wrapper import get_dashboard_panels

        remote = _grafana_request("/api/search?type=dash-db")
        if not remote:  # pragma: no cover - depends on the environment
            pytest.skip("the live Grafana instance reports no dashboards")
        target = remote[0]

        dashboard = _grafana_request(f"/api/dashboards/uid/{target['uid']}")
        panels = dashboard.get("dashboard", {}).get("panels") or []
        titles = [
            str(panel.get("title"))
            for panel in panels
            if isinstance(panel, dict) and panel.get("title")
        ]

        with acting_as(ADMIN):
            output = get_dashboard_panels(target["uid"])

        for title in titles[:5]:
            assert title in output, f"panel '{title}' is missing from the wrapper's answer"
