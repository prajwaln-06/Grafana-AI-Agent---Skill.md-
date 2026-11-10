"""tests/test_principal_refresh.py

Regression tests for *current*-permission enforcement.

The Principal bound to a request is a snapshot: it carries the role, the
account status and the four resource-scope lists as they were when the
identity was resolved. An administrator can change any of them while a
request -- or a whole agent turn -- is still running.

These tests prove that a revocation, a role downgrade, a deactivation and an
environment withdrawal all take effect on the **next** protected operation
inside an already-running ADK turn, and that two concurrent callers keep
their identities separate.
"""
from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from typing import Any, Iterator

import pytest

from app.auth.principal import (
    CURRENT_PRINCIPAL,
    Principal,
    get_current_principal,
    set_current_principal,
)
from app.auth.resource_scope import AuthorizationDenied

FOLDER_A = "fld-refresh-team-a"
FOLDER_B = "fld-refresh-team-b"
DASH_A = "dash-refresh-a"
DASH_B = "dash-refresh-b"
ENV_ALLOWED = "env-refresh-alpha"
ENV_OTHER = "env-refresh-beta"

ISSUER = "https://authentik.test"


@contextmanager
def acting_as(principal: Principal | None) -> Iterator[None]:
    token = set_current_principal(principal)
    try:
        yield
    finally:
        try:
            CURRENT_PRINCIPAL.reset(token)
        except (ValueError, LookupError):  # pragma: no cover - defensive
            set_current_principal(None)


# ---------------------------------------------------------------------------
# The role store, and the store-backed principals the application builds.
# ---------------------------------------------------------------------------

@pytest.fixture()
def store():
    """The isolated role store, with the tables created."""
    from app.storage.database import init_db

    init_db(auto_seed=False)
    return True


def _create_user(
    *,
    role: str = "maintainer",
    status: str = "active",
    folders: tuple[str, ...] = (FOLDER_A,),
    environments: tuple[str, ...] = (ENV_ALLOWED,),
    subject: str = "sub-refresh-user",
) -> tuple[str, Principal]:
    """Create a user in the store and return its id and a store-backed principal."""
    from app.auth.principal import Principal as StorePrincipal
    from app.storage.database import get_db_session
    from app.storage.user_store import UserStore

    with get_db_session() as session:
        user = UserStore.create_pending_user(
            session, issuer=ISSUER, subject=subject, username=subject
        )
        user.status = status
        user.role = role
        user.scopes.allowed_folder_uids = list(folders)
        user.scopes.allowed_environments = list(environments)
        session.flush()
        principal = StorePrincipal.from_user_model(user)
        return user.user_id, principal


def _resolve(user_id: str) -> Principal:
    """Resolve a principal the way the request dependency does."""
    from app.auth.principal import Principal as StorePrincipal
    from app.storage.database import get_db_session
    from app.storage.user_store import UserStore

    with get_db_session() as session:
        user = UserStore.get_user_by_id(session, user_id)
        assert user is not None
        return StorePrincipal.from_user_model(user)


def _revoke(user_id: str, **changes: Any) -> None:
    """Apply an administrative change to a user, as the admin panel does."""
    from app.storage.database import get_db_session
    from app.storage.user_store import UserStore

    with get_db_session() as session:
        UserStore.update_user_access(
            session,
            admin_user_id="usr_refresh_admin",
            target_user_id=user_id,
            admin_role="admin",
            **changes,
        )


# ---------------------------------------------------------------------------
# A fake upstream Grafana MCP client that records every call.
# ---------------------------------------------------------------------------

class RecordingMcp:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    @property
    def tools(self) -> list[str]:
        with self._lock:
            return [call["tool"] for call in self.calls]

    @property
    def count(self) -> int:
        with self._lock:
            return len(self.calls)

    async def mcp_call(self, tool: str, args: dict[str, Any] | None = None, **kwargs) -> str:
        args = dict(args or {})
        with self._lock:
            self.calls.append({"tool": tool, "args": args})
        if tool == "grafana_api_request":
            endpoint = str(args.get("endpoint") or "")
            if endpoint.startswith("/api/search"):
                import json

                return json.dumps(
                    [
                        {
                            "uid": DASH_A,
                            "title": "Team A dashboard",
                            "tags": [],
                            "folderUid": FOLDER_A,
                        },
                        {
                            "uid": DASH_B,
                            "title": "Team B dashboard",
                            "tags": [],
                            "folderUid": FOLDER_B,
                        },
                    ]
                )
            if endpoint.startswith("/api/dashboards/uid/"):
                import json

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
            return "{}"
        if tool == "get_dashboard_by_uid":
            import json

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
            import json

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
        return "{}"


@pytest.fixture()
def mcp(monkeypatch) -> Iterator[RecordingMcp]:
    import importlib

    recorder = RecordingMcp()
    for module_name in (
        "app.grafana_tools.dashboard",
        "app.grafana_tools.prometheus",
        "app.grafana_tools.opensearch",
        "app.grafana_tools.datasource",
        "app.grafana_tools.dashboard_writing",
        "app.grafana_tools.alerting",
    ):
        module = importlib.import_module(module_name)
        if hasattr(module, "mcp_call"):
            monkeypatch.setattr(module, "mcp_call", recorder.mcp_call)
    return recorder


@pytest.fixture(autouse=True)
def _reset_caches():
    import app.grafana_tools.dashboard as dashboard_module
    import app.grafana_tools.datasource as datasource_module
    import app.grafana_tools.discovery as discovery_module

    dashboard_module._dashboard_list = None
    dashboard_module._dashboard_details = {}
    datasource_module._datasource_registry = None
    discovery_module._panel_index = None
    yield
    dashboard_module._dashboard_list = None
    dashboard_module._dashboard_details = {}
    datasource_module._datasource_registry = None
    discovery_module._panel_index = None


# ---------------------------------------------------------------------------
# A stub ADK model that replays a scripted sequence of tool calls.
# ---------------------------------------------------------------------------

def _install_stub_runner(monkeypatch, script: list[dict[str, Any]], tools: list[Any]):
    from google.adk.agents import LlmAgent
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types as genai_types

    import app.adk_agent as adk_agent

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

    agent = LlmAgent(
        name="refresh_test_agent",
        model=_Stub(model="stub-model", script=script, turn=0, lock=threading.Lock()),
        description="Test agent.",
        instruction="Call the tool.",
        tools=tools,
    )
    runner = Runner(
        app_name="refresh_test",
        agent=agent,
        session_service=InMemorySessionService(),
        auto_create_session=True,
    )
    monkeypatch.setattr(adk_agent, "_agent_instance", agent)
    monkeypatch.setattr(adk_agent, "_runner_instance", runner)
    return agent, runner


# ---------------------------------------------------------------------------
# 1. Revocation during an ADK turn.
# ---------------------------------------------------------------------------

class TestRevocationDuringATurn:
    def test_a_scope_revoked_mid_turn_stops_the_next_tool_call(self, mcp, store, monkeypatch):
        """The second tool call in the turn sees the revoked scope."""
        from app.adk_agent import scope_guarded
        from app.grafana_tools.wrapper import list_dashboards

        user_id, principal = _create_user(subject="sub-refresh-revoke")
        seen: dict[str, str] = {}

        def _revoke_in_turn() -> str:
            _revoke(user_id, scopes={"allowed_folder_uids": [FOLDER_B]})
            return "revoked"

        def _list_after_revocation() -> str:
            seen["listing"] = list_dashboards()
            return seen["listing"]

        _install_stub_runner(
            monkeypatch,
            [
                {"name": "_revoke_in_turn", "args": {}},
                {"name": "_list_after_revocation", "args": {}},
                {"text": "done"},
            ],
            [scope_guarded(_revoke_in_turn), scope_guarded(_list_after_revocation)],
        )

        import app.adk_agent as adk_agent

        with acting_as(principal):
            adk_agent.run_adk_agent(
                request="list the dashboards",
                conversation_id="conv-refresh-1",
                principal=principal,
            )

        # The listing was scoped with the *revoked* permissions: the folder
        # the turn started with is gone, the new one is there.
        assert "Team A dashboard" not in seen["listing"]
        assert "Team B dashboard" in seen["listing"]

    def test_a_revoked_scope_denies_the_next_read(self, mcp, store):
        """A read after the revocation is refused, with no content returned."""
        from app.grafana_tools.wrapper import get_dashboard_panels

        user_id, principal = _create_user(subject="sub-refresh-read")
        _revoke(user_id, scopes={"allowed_folder_uids": []})

        with acting_as(principal):
            with pytest.raises(AuthorizationDenied) as excinfo:
                get_dashboard_panels(DASH_A)

        assert excinfo.value.status_code == 403
        assert "get_dashboard_summary" not in mcp.tools

    def test_a_role_downgrade_mid_turn_stops_the_next_call(self, mcp, store, monkeypatch):
        """A maintainer downgraded to observer loses the query privilege."""
        from app.adk_agent import scope_guarded
        from app.grafana_tools.wrapper import query_prometheus_metric

        user_id, principal = _create_user(subject="sub-refresh-downgrade")
        seen: dict[str, str] = {}

        def _downgrade_in_turn() -> str:
            _revoke(user_id, role="observer")
            return "downgraded"

        def _query_after_downgrade() -> str:
            seen["answer"] = query_prometheus_metric(expr="up")
            return seen["answer"]

        _install_stub_runner(
            monkeypatch,
            [
                {"name": "_downgrade_in_turn", "args": {}},
                {"name": "_query_after_downgrade", "args": {}},
                {"text": "done"},
            ],
            [scope_guarded(_downgrade_in_turn), scope_guarded(_query_after_downgrade)],
        )

        import app.adk_agent as adk_agent

        with acting_as(principal):
            adk_agent.run_adk_agent(
                request="what is the cpu",
                conversation_id="conv-refresh-2",
                principal=principal,
            )

        # An observer may not execute PromQL, so the query never reached
        # Prometheus and the model was told so.
        assert "query_prometheus" not in mcp.tools
        assert "RBAC Access Denied" in seen["answer"]

    def test_a_deactivated_account_stops_the_next_call(self, mcp, store, monkeypatch):
        """A deactivated account is refused on the next protected call."""
        from app.adk_agent import scope_guarded
        from app.grafana_tools.wrapper import list_dashboards

        user_id, principal = _create_user(subject="sub-refresh-deactivated")

        def _deactivate_in_turn() -> str:
            _revoke(user_id, status="deactivated")
            return "deactivated"

        _install_stub_runner(
            monkeypatch,
            [
                {"name": "_deactivate_in_turn", "args": {}},
                {"name": "list_dashboards", "args": {}},
                {"text": "done"},
            ],
            [scope_guarded(_deactivate_in_turn), scope_guarded(list_dashboards)],
        )

        import app.adk_agent as adk_agent

        with acting_as(principal):
            adk_agent.run_adk_agent(
                request="list the dashboards",
                conversation_id="conv-refresh-3",
                principal=principal,
            )

        # The deactivated account reached no Grafana read at all.
        assert mcp.count == 0

    def test_a_deactivated_account_is_refused_outright(self, mcp, store):
        from app.grafana_tools.wrapper import list_dashboards

        user_id, principal = _create_user(subject="sub-refresh-deactivated-2")
        _revoke(user_id, status="deactivated")

        with acting_as(principal):
            with pytest.raises(AuthorizationDenied) as excinfo:
                list_dashboards()

        assert excinfo.value.status_code == 403
        assert mcp.count == 0

    def test_an_environment_withdrawn_mid_turn_stops_the_next_query(
        self, mcp, store, monkeypatch
    ):
        """Environment permissions are re-read as well."""
        from app.adk_agent import scope_guarded
        from app.grafana_tools.wrapper import query_prometheus_metric

        user_id, principal = _create_user(
            subject="sub-refresh-environment",
            environments=(ENV_ALLOWED, ENV_OTHER),
        )

        def _withdraw_environment() -> str:
            _revoke(user_id, scopes={"allowed_environments": [ENV_OTHER]})
            return "withdrawn"

        _install_stub_runner(
            monkeypatch,
            [
                {"name": "_withdraw_environment", "args": {}},
                {
                    "name": "query_prometheus_metric",
                    "args": {"expr": "up", "environment": ENV_ALLOWED},
                },
                {"text": "done"},
            ],
            [scope_guarded(_withdraw_environment), scope_guarded(query_prometheus_metric)],
        )

        import app.adk_agent as adk_agent

        with acting_as(principal):
            adk_agent.run_adk_agent(
                request="what is the cpu in {0}".format(ENV_ALLOWED),
                conversation_id="conv-refresh-4",
                principal=principal,
            )

        assert "query_prometheus" not in mcp.tools

    def test_a_deleted_account_is_refused(self, mcp, store):
        """An identity that is no longer in the store is refused."""
        from app.grafana_tools.wrapper import list_dashboards

        user_id, principal = _create_user(subject="sub-refresh-deleted")

        from app.storage.database import get_db_session
        from app.storage.models import UserModel

        with get_db_session() as session:
            session.query(UserModel).filter(UserModel.user_id == user_id).delete()
            session.flush()

        with acting_as(principal):
            with pytest.raises(AuthorizationDenied) as excinfo:
                list_dashboards()

        assert excinfo.value.status_code == 403
        assert mcp.count == 0

    def test_a_store_backed_principal_keeps_its_authenticated_identity(
        self, mcp, store
    ):
        """The refresh re-reads the authorization data, not the identity."""
        from app.grafana_tools.wrapper import list_dashboards

        user_id, principal = _create_user(subject="sub-refresh-identity")
        assert principal.store_backed is True
        assert principal.issuer == ISSUER
        assert principal.subject == "sub-refresh-identity"

        with acting_as(principal):
            list_dashboards()
            refreshed = get_current_principal()

        assert refreshed is not None
        assert refreshed.issuer == ISSUER
        assert refreshed.subject == "sub-refresh-identity"
        assert refreshed.user_id == user_id

    def test_an_in_memory_principal_is_not_refreshed(self, mcp, store):
        """A principal that has no store row is used as it is."""
        from app.grafana_tools.wrapper import list_dashboards

        principal = Principal(
            user_id="usr_not_store_backed",
            issuer=ISSUER,
            subject="sub-not-store-backed",
            authenticated=True,
            status="active",
            role="admin",
            allowed_folder_uids=frozenset({"*"}),
        )
        assert principal.store_backed is False

        with acting_as(principal):
            list_dashboards()
            assert get_current_principal() is principal

    def test_the_execution_path_revalidates_the_current_permissions(self, mcp, store):
        """A revoked executor cannot execute, and nothing is written."""
        from app.grafana_tools.dashboard_writing import PROPOSALS, execute_approved_mutation

        user_id, principal = _create_user(subject="sub-refresh-executor")
        ir = {
            "operation": "create",
            "name": "Revoked execution",
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
        with acting_as(principal):
            proposal = PROPOSALS.create(ir)
            approved = PROPOSALS.approve(
                proposal["proposalId"],
                proposal["version"],
                approver_id=principal.user_id,
                approver_role="maintainer",
            )

        _revoke(user_id, scopes={"allowed_folder_uids": []})

        with acting_as(principal):
            with pytest.raises((AuthorizationDenied, PermissionError)):
                asyncio.run(
                    execute_approved_mutation(
                        proposal["proposalId"],
                        proposal["version"],
                        approved["approvalToken"],
                    )
                )

        assert "update_dashboard" not in mcp.tools


# ---------------------------------------------------------------------------
# 3. The store is read once per protected operation -- and no longer.
# ---------------------------------------------------------------------------

class TestStoreReadAccounting:
    @pytest.fixture()
    def reads(self, monkeypatch) -> list[int]:
        """Count the reads of the role store's identity lookup."""
        from app.storage.user_store import UserStore

        counter: list[int] = []
        original = UserStore.get_user_by_identity

        def _counting(session, issuer, subject):
            counter.append(1)
            return original(session, issuer, subject)

        monkeypatch.setattr(UserStore, "get_user_by_identity", staticmethod(_counting))
        return counter

    def test_one_operation_reads_the_store_once(self, mcp, store, reads, monkeypatch):
        """The internal steps of one operation share a single read."""
        from app.adk_agent import scope_guarded
        from app.grafana_tools.dashboard_writing import propose_dashboard

        user_id, principal = _create_user(subject="sub-refresh-reads")

        def _proposal_operation() -> str:
            # ``propose_dashboard`` establishes the authorization data and
            # then builds the proposal, which asks for it again.
            propose_dashboard("Create a dashboard called Somewhere")
            return "done"

        _install_stub_runner(
            monkeypatch,
            [{"name": "_proposal_operation", "args": {}}, {"text": "done"}],
            [scope_guarded(_proposal_operation)],
        )

        import app.adk_agent as adk_agent

        with acting_as(principal):
            adk_agent.run_adk_agent(
                request="do something",
                conversation_id="conv-refresh-reads",
                principal=principal,
            )

        # One protected operation: the store was read once for it, not once
        # per internal step.
        assert len(reads) == 1

    def test_the_operation_scope_never_outlives_the_operation(self, mcp, store, reads):
        """Nothing survives the operation scope."""
        from app.auth.refresh import authorization_operation
        from app.auth.resource_scope import require_active_principal

        user_id, principal = _create_user(subject="sub-refresh-reads-4")

        with acting_as(principal):
            with authorization_operation():
                require_active_principal()
                require_active_principal()
            assert len(reads) == 1

            # Outside the scope the store is read again.
            require_active_principal()
            assert len(reads) == 2

    def test_two_operations_read_the_store_twice(self, mcp, store, reads, monkeypatch):
        """A revocation that lands between two operations is always seen."""
        from app.adk_agent import scope_guarded
        from app.grafana_tools.wrapper import list_dashboards

        user_id, principal = _create_user(subject="sub-refresh-reads-2")

        def _first_operation() -> str:
            list_dashboards()
            return "done"

        def _second_operation() -> str:
            list_dashboards()
            return "done"

        _install_stub_runner(
            monkeypatch,
            [
                {"name": "_first_operation", "args": {}},
                {"name": "_second_operation", "args": {}},
                {"text": "done"},
            ],
            [scope_guarded(_first_operation), scope_guarded(_second_operation)],
        )

        import app.adk_agent as adk_agent

        with acting_as(principal):
            adk_agent.run_adk_agent(
                request="do something",
                conversation_id="conv-refresh-reads-2",
                principal=principal,
            )

        # Two protected operations, two reads.
        assert len(reads) == 2

    def test_a_direct_call_outside_an_operation_always_reads(self, mcp, store, reads):
        """A call that is not inside an operation scope reads the store."""
        from app.grafana_tools.wrapper import list_dashboards

        user_id, principal = _create_user(subject="sub-refresh-reads-3")

        with acting_as(principal):
            list_dashboards()
            list_dashboards()

        assert len(reads) == 2


# ---------------------------------------------------------------------------
# 2. Concurrency: two callers keep their own identity.
# ---------------------------------------------------------------------------

class TestConcurrentIsolation:
    def test_two_users_keep_separate_identities_across_a_refresh(self, mcp, store):
        """A refresh rebinds only the calling context."""
        from app.grafana_tools.wrapper import list_dashboards

        user_a, principal_a = _create_user(
            subject="sub-refresh-user-a", folders=(FOLDER_A,)
        )
        user_b, principal_b = _create_user(
            subject="sub-refresh-user-b", folders=(FOLDER_B,)
        )

        observed: dict[str, str] = {}
        barrier = threading.Barrier(2)

        def _worker(principal: Principal) -> None:
            with acting_as(principal):
                barrier.wait(timeout=15)
                list_dashboards()
                current = get_current_principal()
                observed[principal.user_id] = current.user_id if current else ""

        threads = [
            threading.Thread(target=_worker, args=(principal_a,)),
            threading.Thread(target=_worker, args=(principal_b,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert observed == {user_a: user_a, user_b: user_b}
