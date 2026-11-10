"""tests/test_audit_logging.py

Regression tests for the completeness and the integrity of the security
audit log.

The audit log is the record of who did what. These tests assert that:

* every security-relevant mutation produces a record;
* the actor in the record is the authenticated principal, never a
  client-supplied identity;
* no credential ever reaches the log;
* an audit failure cannot turn a denial into an allow;
* one operation produces one record, not two.
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.auth.principal import Principal, set_current_principal
from app.storage.audit_store import REDACTED, AuditStore, redact_sensitive
from app.storage.database import get_db_session


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def events(event_type: str | None = None) -> list[dict[str, Any]]:
    """Every audit record currently in the log, newest first.

    The records are read into plain dictionaries inside the session, so they
    stay readable after the session closes.
    """
    with get_db_session() as session:
        records = AuditStore.list_events(session, limit=1000, event_type=event_type)
        return [
            {
                "id": record.id,
                "event_type": record.event_type,
                "actor_id": record.actor_id,
                "actor_issuer": record.actor_issuer,
                "actor_subject": record.actor_subject,
                "target_type": record.target_type,
                "target_id": record.target_id,
                "decision": record.decision,
                "reason": record.reason,
                "previous_state": record.previous_state,
                "new_state": record.new_state,
                "details": record.details,
                "timestamp": record.timestamp,
            }
            for record in records
        ]


def make_principal(role: str, *, user_id: str, role_scope: tuple[str, ...] = ("*",)) -> Principal:
    return Principal(
        user_id=user_id,
        issuer="https://authentik.test",
        subject=f"sub_{user_id}",
        authenticated=True,
        status="active",
        role=role,
        allowed_folder_uids=frozenset(role_scope),
        allowed_environments=frozenset({"staging"}),
        username=user_id,
    )


ADMIN = make_principal("admin", user_id="usr_audit_admin")
MAINTAINER = make_principal("maintainer", user_id="usr_audit_maintainer")


@pytest.fixture(scope="module", autouse=True)
def _audit_schema(isolated_database):
    """Make sure the audit table exists in the isolated test database."""
    from app.storage.database import init_db

    init_db(auto_seed=False)
    return True


@pytest.fixture()
def clean_log():
    """Start every test from an empty audit log."""
    with get_db_session() as session:
        for record in AuditStore.list_events(session, limit=1000):
            session.delete(record)
    return True


# ---------------------------------------------------------------------------
# A. Redaction -- credentials must never be stored
# ---------------------------------------------------------------------------

class TestAuditRedaction:
    """No credential, token or secret ever reaches the audit log."""

    def test_secret_bearing_keys_are_redacted(self):
        payload = {
            "password": "hunter2",
            "access_token": "abc.def.ghi",
            "refreshToken": "rt-123",
            "id_token": "eyJhbGciOi",
            "cookie": "grafana_ai_session=xyz",
            "Authorization": "Bearer eyJhbGciOi",
            "client_secret": "s3cr3t",
            "apiKey": "key-123",
            "safe": "keep-me",
        }
        redacted = redact_sensitive(payload)
        assert redacted["safe"] == "keep-me"
        for key in ("password", "access_token", "refreshToken", "id_token",
                    "cookie", "Authorization", "client_secret", "apiKey"):
            assert redacted[key] == REDACTED, key

    def test_redaction_is_recursive(self):
        payload = {"outer": {"inner": {"password": "hunter2"}}}
        redacted = redact_sensitive(payload)
        assert redacted["outer"]["inner"]["password"] == REDACTED

    def test_token_shaped_values_are_redacted_even_under_a_safe_key(self):
        redacted = redact_sensitive({"note": "Bearer eyJhbGciOiJIUzI1NiJ9.e30.sig"})
        assert redacted["note"] == REDACTED

    def test_a_record_written_with_a_credential_stores_the_placeholder(self, clean_log):
        from app.auth.audit import record_event

        record_event(
            "test_secret",
            principal=ADMIN,
            details={"password": "hunter2", "user": "someone"},
        )
        stored = events("test_secret")
        assert len(stored) == 1
        assert stored[0]["details"]["password"] == REDACTED
        assert stored[0]["details"]["user"] == "someone"
        assert "hunter2" not in json.dumps(stored[0]["details"])


# ---------------------------------------------------------------------------
# B. Actor attribution
# ---------------------------------------------------------------------------

class TestActorAttribution:
    """The actor of a record is the server-side principal, never the client."""

    def test_the_actor_is_the_authenticated_principal(self, clean_log):
        from app.auth.audit import record_event

        record_event("test_actor", principal=MAINTAINER)
        stored = events("test_actor")
        assert len(stored) == 1
        assert stored[0]["actor_id"] == MAINTAINER.user_id
        assert stored[0]["actor_issuer"] == MAINTAINER.issuer
        assert stored[0]["actor_subject"] == MAINTAINER.subject
        assert stored[0]["details"]["actor_role"] == "maintainer"

    def test_the_actor_role_is_recorded(self, clean_log):
        from app.auth.audit import record_event

        record_event("test_actor_role", principal=ADMIN)
        stored = events("test_actor_role")
        assert stored[0]["details"]["actor_role"] == "admin"

    def test_a_client_supplied_actor_is_ignored(self, clean_log):
        """A details mapping cannot make the actor someone else."""
        from app.auth.audit import record_event

        record_event(
            "test_spoof",
            principal=MAINTAINER,
            details={"actor_id": "usr_someone_else", "userId": "usr_someone_else"},
        )
        stored = events("test_spoof")
        assert len(stored) == 1
        # The record's actor is the authenticated principal, whatever the
        # payload claimed.
        assert stored[0]["actor_id"] == MAINTAINER.user_id
        assert stored[0]["actor_subject"] == MAINTAINER.subject
        # The spoofed value survives only as an inert detail -- it is never
        # read as the actor.
        assert stored[0]["details"]["actor_id"] == "usr_someone_else"


# ---------------------------------------------------------------------------
# C. Administrative events
# ---------------------------------------------------------------------------

class TestAdministrativeAuditEvents:
    """The administrative surface produces a record for every change."""

    def _admin_client(self, principal: Principal) -> TestClient:
        from app.api.main import app
        from app.auth.dependencies import require_principal

        app.dependency_overrides[require_principal] = lambda: principal
        set_current_principal(principal)
        return TestClient(app)

    def test_a_role_change_is_audited(self, clean_log):
        from app.storage.user_store import UserStore

        with get_db_session() as session:
            target = UserStore.create_pending_user(
                session, issuer="iss", subject="sub-role", email="role@example.com"
            )
            target_id = target.user_id
            UserStore.update_user_access(
                session,
                admin_user_id=ADMIN.user_id,
                target_user_id=target_id,
                role="maintainer",
                admin_role="admin",
            )

        role_events = events("user_role_changed")
        assert len(role_events) == 1
        assert role_events[0]["actor_id"] == ADMIN.user_id
        assert role_events[0]["target_id"] == target_id
        assert role_events[0]["previous_state"]["role"] is None
        assert role_events[0]["new_state"]["role"] == "maintainer"

    def test_an_activation_is_audited(self, clean_log):
        from app.storage.user_store import UserStore

        with get_db_session() as session:
            target = UserStore.create_pending_user(
                session, issuer="iss", subject="sub-activate", email="act@example.com"
            )
            target_id = target.user_id
            UserStore.update_user_access(
                session,
                admin_user_id=ADMIN.user_id,
                target_user_id=target_id,
                status="active",
                admin_role="admin",
            )

        status_events = events("user_status_changed")
        assert len(status_events) == 1
        assert status_events[0]["new_state"]["status"] == "active"

    def test_a_deactivation_is_audited(self, clean_log):
        from app.storage.user_store import UserStore

        with get_db_session() as session:
            target = UserStore.create_pending_user(
                session, issuer="iss", subject="sub-deactivate", email="deact@example.com"
            )
            target.status = "active"
            target.role = "observer"
            session.flush()
            target_id = target.user_id
            UserStore.update_user_access(
                session,
                admin_user_id=ADMIN.user_id,
                target_user_id=target_id,
                status="deactivated",
                admin_role="admin",
            )

        status_events = events("user_status_changed")
        assert len(status_events) == 1
        assert status_events[0]["new_state"]["status"] == "deactivated"

    @pytest.mark.parametrize(
        "scope_key,value",
        [
            ("allowed_folder_uids", ["fld-audit"]),
            ("allowed_dashboard_uids", ["dash-audit"]),
            ("allowed_environments", ["staging"]),
            ("denied_dashboard_uids", ["dash-forbidden"]),
        ],
    )
    def test_each_scope_change_is_audited(self, clean_log, scope_key, value):
        from app.storage.user_store import UserStore

        with get_db_session() as session:
            target = UserStore.create_pending_user(
                session, issuer="iss", subject=f"sub-scope-{scope_key}", email="sc@example.com"
            )
            target_id = target.user_id
            UserStore.update_user_access(
                session,
                admin_user_id=ADMIN.user_id,
                target_user_id=target_id,
                scopes={scope_key: value},
                admin_role="admin",
            )

        scope_events = events("user_scope_changed")
        assert len(scope_events) == 1
        assert scope_events[0]["details"]["scopeType"] == scope_key
        assert scope_events[0]["new_state"][scope_key] == value

    def test_an_admin_read_is_audited(self, clean_log):
        client = self._admin_client(ADMIN)
        try:
            response = client.get("/api/v1/admin/users")
            assert response.status_code == 200
        finally:
            from app.api.main import app
            from app.auth.dependencies import require_principal

            app.dependency_overrides.pop(require_principal, None)
            set_current_principal(None)

        listed = events("admin_users_listed")
        assert len(listed) == 1
        assert listed[0]["actor_id"] == ADMIN.user_id

    def test_a_non_admin_reaching_an_admin_endpoint_is_audited_as_a_denial(self, clean_log):
        client = self._admin_client(MAINTAINER)
        try:
            response = client.get("/api/v1/admin/users")
            assert response.status_code == 403
        finally:
            from app.api.main import app
            from app.auth.dependencies import require_principal

            app.dependency_overrides.pop(require_principal, None)
            set_current_principal(None)

        denials = events("admin_access_denied")
        assert len(denials) == 1
        assert denials[0]["decision"] == "DENY"
        assert denials[0]["actor_id"] == MAINTAINER.user_id

    def test_the_audit_log_has_no_write_or_delete_api(self):
        """The audit log is append-only: no application API can rewrite it."""
        from app.api.main import app

        paths = {getattr(route, "path", "") for route in app.routes}
        methods = {
            (getattr(route, "path", ""), method)
            for route in app.routes
            for method in getattr(route, "methods", set())
        }
        for path, method in methods:
            if "audit-log" in path:
                assert method in {"GET"}, f"{method} {path} would allow rewriting history"
        for path in paths:
            assert not path.endswith("/audit-log/delete")


# ---------------------------------------------------------------------------
# D. Failure policy
# ---------------------------------------------------------------------------

class TestAuditFailurePolicy:
    """An audit failure never changes the outcome of the operation."""

    def test_a_failing_audit_does_not_turn_a_denial_into_an_allow(self, monkeypatch, clean_log):
        """The denial is raised even when the audit record cannot be written."""
        from app.api.main import app
        from app.auth.dependencies import require_principal
        from app.storage import audit_store

        def boom(*args, **kwargs):
            raise RuntimeError("the audit sink is down")

        monkeypatch.setattr(audit_store.AuditStore, "log_event", staticmethod(boom))

        app.dependency_overrides[require_principal] = lambda: MAINTAINER
        set_current_principal(MAINTAINER)
        try:
            with TestClient(app) as client:
                response = client.get("/api/v1/admin/users")
        finally:
            app.dependency_overrides.pop(require_principal, None)
            set_current_principal(None)

        assert response.status_code == 403

    def test_a_required_audit_failure_rolls_the_mutation_back(self, monkeypatch, clean_log):
        """A mutation whose audit record cannot be written does not happen."""
        from app.storage import audit_store
        from app.storage.user_store import UserStore

        def boom(*args, **kwargs):
            raise RuntimeError("the audit sink is down")

        # The account is created first, so the audit sink only fails for the
        # mutation under test.
        with get_db_session() as session:
            target = UserStore.create_pending_user(
                session, issuer="iss", subject="sub-rollback", email="rb@example.com"
            )
            target_id = target.user_id

        monkeypatch.setattr(audit_store.AuditStore, "log_event", staticmethod(boom))

        with pytest.raises(RuntimeError):
            with get_db_session() as session:
                UserStore.update_user_access(
                    session,
                    admin_user_id=ADMIN.user_id,
                    target_user_id=target_id,
                    role="maintainer",
                    admin_role="admin",
                )

        with get_db_session() as session:
            from app.storage.models import UserModel

            assert session.get(UserModel, target_id).role is None

    def test_an_event_written_in_the_callers_transaction_fails_loudly(self, monkeypatch):
        """A caller-supplied session means the write is part of the mutation.

        The exception always propagates there, so the surrounding transaction
        rolls back and the audited mutation cannot silently complete without
        its record.
        """
        from app.auth.audit import record_event
        from app.storage import audit_store

        def boom(*args, **kwargs):
            raise RuntimeError("the audit sink is down")

        monkeypatch.setattr(audit_store.AuditStore, "log_event", staticmethod(boom))

        with pytest.raises(RuntimeError):
            with get_db_session() as session:
                record_event(
                    "test_in_transaction",
                    principal=ADMIN,
                    session=session,
                    required=False,
                )


# ---------------------------------------------------------------------------
# E. No duplicate records
# ---------------------------------------------------------------------------

class TestNoDuplicateAuditRecords:
    """One operation produces one record."""

    def test_a_scope_change_that_changes_nothing_produces_no_record(self, clean_log):
        from app.storage.user_store import UserStore

        with get_db_session() as session:
            target = UserStore.create_pending_user(
                session, issuer="iss", subject="sub-noop", email="noop@example.com"
            )
            target_id = target.user_id
            UserStore.update_user_access(
                session,
                admin_user_id=ADMIN.user_id,
                target_user_id=target_id,
                scopes={"allowed_folder_uids": ["fld-x"]},
                admin_role="admin",
            )
        assert len(events("user_scope_changed")) == 1

        # The identical scope again changes nothing.
        with get_db_session() as session:
            UserStore.update_user_access(
                session,
                admin_user_id=ADMIN.user_id,
                target_user_id=target_id,
                scopes={"allowed_folder_uids": ["fld-x"]},
                admin_role="admin",
            )
        assert len(events("user_scope_changed")) == 1
        assert len(events("user_access_modified")) == 1

    def test_a_proposal_creation_is_recorded_once(self, clean_log):
        from app.grafana_tools.dashboard_writing import PROPOSALS

        ir = {
            "operation": "create",
            "name": "Audit Dashboard",
            "folderUid": "fld-audit",
            "panels": [{
                "title": "Panel",
                "datasource": {"type": "prometheus", "uid": "prom-uid"},
                "visualizationType": "timeseries",
                "query": "node_load1",
            }],
        }
        set_current_principal(MAINTAINER)
        try:
            proposal = PROPOSALS.create(ir)
        finally:
            set_current_principal(None)

        created = events("proposal_created")
        assert len(created) == 1
        assert created[0]["target_id"] == proposal["proposalId"]
        assert created[0]["actor_id"] == MAINTAINER.user_id
