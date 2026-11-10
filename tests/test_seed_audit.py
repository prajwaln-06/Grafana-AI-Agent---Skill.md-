"""tests/test_seed_audit.py

Regression tests for the demo RBAC seed data.

The seeded demo accounts name Grafana folders and dashboards by UID. On a
deployment whose Grafana holds different resources those references are
stale, and the correct behaviour is to *report* them -- never to widen a
demo user's scope, never to invent a replacement resource, and never to
overwrite what an administrator has configured.

These tests cover:

* the detection of stale folder and dashboard references;
* an idempotent seed initialization;
* the preservation of manually configured user scopes;
* the absence of any modification to production (non-demo) users;
* the absence of any implicit global permission grant;
* the behaviour when Grafana is unreachable;
* the behaviour when Grafana reports no eligible folder;
* the safe handling of historical test fixtures -- a test resource that is
  not referenced by the seed data is never deleted.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.storage.models import Base, UserModel
from app.storage.seed import (
    DEMO_SEEDED_SCOPES,
    DEMO_USER_IDS,
    is_demo_user,
    seed_dummy_data_if_empty,
)
from app.storage.seed_audit import audit_seed_references, fetch_grafana_inventory


# ---------------------------------------------------------------------------
# An isolated database, so the seeder never touches the live one.
# ---------------------------------------------------------------------------

@pytest.fixture()
def seeded_db():
    """A throwaway SQLite database with the demo data seeded into it."""
    import app.storage.database as db_mod

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSession()
    original_factory = db_mod._SessionFactory
    db_mod._SessionFactory = lambda: session
    try:
        yield session
    finally:
        db_mod._SessionFactory = original_factory
        session.close()


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# A. Seeding behaviour
# ---------------------------------------------------------------------------

class TestSeedingIsIdempotentAndNonDestructive:
    """The seeder creates what is missing and never overwrites what exists."""

    def test_a_first_seed_creates_the_demo_accounts(self, seeded_db):
        assert seed_dummy_data_if_empty(force=True) is True
        from app.storage.user_store import UserStore

        users = UserStore.list_users(seeded_db)
        assert len(users) == len(DEMO_USER_IDS)

    def test_a_second_seed_creates_nothing_new(self, seeded_db):
        assert seed_dummy_data_if_empty(force=True) is True
        assert seed_dummy_data_if_empty(force=True) is False
        assert seed_dummy_data_if_empty(force=True) is False

        from app.storage.user_store import UserStore

        users = UserStore.list_users(seeded_db)
        assert len(users) == len(DEMO_USER_IDS)

    def test_a_second_seed_does_not_duplicate_the_audit_records(self, seeded_db):
        from app.storage.audit_store import AuditStore

        seed_dummy_data_if_empty(force=True)
        first = len(AuditStore.list_events(seeded_db, limit=1000))
        seed_dummy_data_if_empty(force=True)
        second = len(AuditStore.list_events(seeded_db, limit=1000))
        assert first == second

    def test_a_manually_configured_scope_survives_a_re_seed(self, seeded_db):
        """An administrator's configuration of a demo account is preserved."""
        from app.storage.user_store import UserStore

        seed_dummy_data_if_empty(force=True)

        target = UserStore.get_user_by_id(seeded_db, "usr_maint_01")
        target.role = "observer"
        target.status = "active"
        target.scopes.allowed_folder_uids = ["fld-real-one"]
        target.scopes.allowed_dashboard_uids = []
        target.scopes.allowed_environments = ["production"]
        seeded_db.flush()

        seed_dummy_data_if_empty(force=True)

        refreshed = UserStore.get_user_by_id(seeded_db, "usr_maint_01")
        assert refreshed.role == "observer"
        assert list(refreshed.scopes.allowed_folder_uids) == ["fld-real-one"]
        assert list(refreshed.scopes.allowed_environments) == ["production"]

    def test_a_manually_configured_role_survives_a_re_seed(self, seeded_db):
        from app.storage.user_store import UserStore

        seed_dummy_data_if_empty(force=True)
        target = UserStore.get_user_by_id(seeded_db, "usr_obs_01")
        target.role = "maintainer"
        seeded_db.flush()

        seed_dummy_data_if_empty(force=True)

        assert UserStore.get_user_by_id(seeded_db, "usr_obs_01").role == "maintainer"

    def test_a_production_user_is_never_touched(self, seeded_db):
        """A user an administrator created is not the seeder's business."""
        from app.storage.user_store import UserStore

        seed_dummy_data_if_empty(force=True)

        production = UserStore.create_pending_user(
            seeded_db,
            issuer="https://authentik.test",
            subject="sub_real_person",
            email="real@example.com",
            username="real.person",
        )
        production.role = "admin"
        production.status = "active"
        production.scopes.allowed_folder_uids = ["fld-production"]
        production.scopes.allowed_environments = ["production"]
        seeded_db.flush()
        production_id = production.user_id

        seed_dummy_data_if_empty(force=True)

        refreshed = UserStore.get_user_by_id(seeded_db, production_id)
        assert refreshed is not None
        assert refreshed.role == "admin"
        assert list(refreshed.scopes.allowed_folder_uids) == ["fld-production"]
        assert list(refreshed.scopes.allowed_environments) == ["production"]

    def test_the_seeder_grants_no_implicit_global_access(self, seeded_db):
        """No seeded account other than the administrator holds a wildcard."""
        from app.storage.user_store import UserStore

        seed_dummy_data_if_empty(force=True)
        for user in UserStore.list_users(seeded_db):
            if user.role == "admin":
                continue
            folders = list(user.scopes.allowed_folder_uids if user.scopes else [])
            assert "*" not in folders, f"{user.user_id} holds a global folder grant"

    def test_the_seeder_grants_no_implicit_environment_access(self, seeded_db):
        from app.storage.user_store import UserStore

        seed_dummy_data_if_empty(force=True)
        for user in UserStore.list_users(seeded_db):
            environments = list(user.scopes.allowed_environments if user.scopes else [])
            assert "*" not in environments, f"{user.user_id} holds a global environment grant"

    def test_the_seed_defines_its_own_accounts(self, seeded_db):
        seed_dummy_data_if_empty(force=True)
        from app.storage.user_store import UserStore

        for user in UserStore.list_users(seeded_db):
            assert is_demo_user(user)

    def test_a_production_user_is_not_a_demo_account(self, seeded_db):
        from app.storage.user_store import UserStore

        production = UserStore.create_pending_user(
            seeded_db, issuer="https://authentik.test", subject="sub_real",
            email="real@example.com",
        )
        assert is_demo_user(production) is False


# ---------------------------------------------------------------------------
# B. Stale-reference detection
# ---------------------------------------------------------------------------

def _accounts(**overrides) -> list[dict]:
    base = {
        "user_id": "usr_maint_01",
        "username": "alex.rivera",
        "role": "maintainer",
        "status": "active",
        "authz_version": 2,
        "scopes": {
            "allowed_folder_uids": ["fld-payments", "afvlix95l3wg0d"],
            "allowed_dashboard_uids": ["dash-checkout-slo", "dash-global-overview"],
            "denied_dashboard_uids": ["dash-pci-compliance"],
            "allowed_environments": ["staging", "production"],
        },
        "seed_state": "unmodified",
    }
    base.update(overrides)
    return [base]


class TestStaleReferenceDetection:
    """The audit reports the references that do not exist."""

    def test_a_missing_folder_is_reported(self):
        inventory = {
            "available": True,
            "folders": ["", "fld-real"],
            "dashboards": ["dash-real"],
            "error": None,
        }
        report = _run(audit_seed_references(accounts=_accounts(), inventory=inventory))
        assert report["ok"] is False
        assert report["stale_folder_references"]["usr_maint_01"] == ["fld-payments", "afvlix95l3wg0d"]
        # Two stale folders plus the three stale dashboards.
        assert report["stale_reference_count"] == 5

    def test_a_missing_dashboard_is_reported(self):
        inventory = {
            "available": True,
            "folders": ["", "fld-payments", "afvlix95l3wg0d"],
            "dashboards": ["dash-global-overview"],
            "error": None,
        }
        report = _run(audit_seed_references(accounts=_accounts(), inventory=inventory))
        # The denied dashboard is a reference too: a deny for a dashboard that
        # does not exist is not protecting anything.
        assert report["stale_dashboard_references"]["usr_maint_01"] == [
            "dash-checkout-slo",
            "dash-pci-compliance",
        ]
        assert report["stale_folder_references"] == {}

    def test_consistent_references_report_no_problem(self):
        inventory = {
            "available": True,
            "folders": ["", "fld-payments", "afvlix95l3wg0d"],
            "dashboards": ["dash-checkout-slo", "dash-global-overview", "dash-pci-compliance"],
            "error": None,
        }
        report = _run(audit_seed_references(accounts=_accounts(), inventory=inventory))
        assert report["ok"] is True
        assert report["stale_reference_count"] == 0
        assert report["remediation"] is None

    def test_a_wildcard_folder_is_never_reported_as_stale(self):
        accounts = _accounts(scopes={
            "allowed_folder_uids": ["*"],
            "allowed_dashboard_uids": [],
            "denied_dashboard_uids": [],
            "allowed_environments": [],
        })
        inventory = {"available": True, "folders": [""], "dashboards": [], "error": None}
        report = _run(audit_seed_references(accounts=accounts, inventory=inventory))
        assert report["stale_reference_count"] == 0

    def test_grafana_being_unavailable_is_reported_not_raised(self):
        inventory = {
            "available": False,
            "folders": [],
            "dashboards": [],
            "error": "Grafana inventory could not be retrieved.",
        }
        report = _run(audit_seed_references(accounts=_accounts(), inventory=inventory))
        assert report["grafana_available"] is False
        assert report["ok"] is False
        assert report["grafana_error"]
        assert "could not be validated" in report["remediation"]

    def test_grafana_with_no_eligible_folder_is_reported(self):
        inventory = {"available": True, "folders": [""], "dashboards": [], "error": None}
        report = _run(audit_seed_references(accounts=_accounts(), inventory=inventory))
        assert report["grafana_available"] is True
        assert report["grafana_folder_count"] == 1
        assert report["stale_reference_count"] > 0
        assert report["remediation"]

    def test_the_remediation_never_asks_for_a_broader_grant(self):
        inventory = {"available": True, "folders": [""], "dashboards": [], "error": None}
        report = _run(audit_seed_references(accounts=_accounts(), inventory=inventory))
        remediation = report["remediation"].lower()
        # The remediation must explain what to do -- and must warn that the
        # reference is deliberately not repaired by widening a scope.
        assert "assign the real folder and dashboard uids" in remediation
        assert "arbitrary existing folder" in remediation
        assert "never meant to see" in remediation

    def test_the_report_never_changes_a_scope(self, seeded_db):
        """The audit is read-only: it does not repair a stale reference."""
        from app.storage.user_store import UserStore

        seed_dummy_data_if_empty(force=True)
        before = list(
            UserStore.get_user_by_id(seeded_db, "usr_maint_01").scopes.allowed_folder_uids
        )

        inventory = {"available": True, "folders": [""], "dashboards": [], "error": None}
        _run(audit_seed_references(inventory=inventory))

        after = list(
            UserStore.get_user_by_id(seeded_db, "usr_maint_01").scopes.allowed_folder_uids
        )
        assert after == before


# ---------------------------------------------------------------------------
# C. The Grafana inventory
# ---------------------------------------------------------------------------

class TestGrafanaInventory:
    """The inventory is read defensively from Grafana."""

    def test_an_unreachable_grafana_yields_an_unavailable_inventory(self, monkeypatch):
        import app.mcp.session as session_module

        async def boom(*args, **kwargs):
            raise RuntimeError("the Grafana MCP bridge is down")

        monkeypatch.setattr(session_module, "mcp_call", boom)
        inventory = _run(fetch_grafana_inventory())
        assert inventory["available"] is False
        assert inventory["error"]

    def test_an_empty_response_yields_an_unavailable_inventory(self, monkeypatch):
        import app.mcp.session as session_module

        async def empty(*args, **kwargs):
            return ""

        monkeypatch.setattr(session_module, "mcp_call", empty)
        inventory = _run(fetch_grafana_inventory())
        assert inventory["available"] is False
        assert inventory["error"]

    def test_a_healthy_grafana_yields_the_folder_and_dashboard_uids(self, monkeypatch):
        import app.mcp.session as session_module

        async def fake(tool, args, **kwargs):
            if args.get("endpoint") == "/api/folders":
                return json.dumps([{"uid": "fld-a", "title": "A"}])
            return json.dumps([{"uid": "dash-a", "title": "D", "folderUid": "fld-a"}])

        monkeypatch.setattr(session_module, "mcp_call", fake)
        inventory = _run(fetch_grafana_inventory())
        assert inventory["available"] is True
        assert "fld-a" in inventory["folders"]
        # Grafana's General folder is addressed by the empty UID.
        assert "" in inventory["folders"]
        assert "dash-a" in inventory["dashboards"]

    def test_the_mcp_response_envelope_is_unwrapped(self, monkeypatch):
        """``grafana_api_request`` wraps the payload in ``data``.

        Reading the envelope as if it were the payload would report an
        empty inventory -- and every dashboard reference would then be
        wrongly reported as stale.
        """
        import app.mcp.session as session_module

        async def fake(tool, args, **kwargs):
            if args.get("endpoint") == "/api/folders":
                return json.dumps({
                    "status": 200,
                    "headers": {"Content-Type": "application/json"},
                    "data": [{"uid": "fld-a", "title": "A"}],
                })
            return json.dumps({
                "status": 200,
                "headers": {"Content-Type": "application/json"},
                "data": [{"uid": "dash-a", "title": "D", "folderUid": "fld-a"}],
            })

        monkeypatch.setattr(session_module, "mcp_call", fake)
        inventory = _run(fetch_grafana_inventory())
        assert inventory["available"] is True
        assert "fld-a" in inventory["folders"]
        assert "dash-a" in inventory["dashboards"]
        assert inventory["dashboards"] != []


# ---------------------------------------------------------------------------
# D. Historical test fixtures are handled safely
# ---------------------------------------------------------------------------

class TestHistoricalTestFixtures:
    """A resource that the seed data does not reference is left alone."""

    def test_a_historical_fixture_folder_is_not_reported_as_a_seed_problem(self):
        """A leftover test folder is not a *reference* problem.

        The audit reports references the demo accounts hold that do not
        resolve. A folder that merely exists in Grafana and is referenced by
        nobody is not the seeder's to report, and -- above all -- is never
        deleted.
        """
        accounts = _accounts(scopes={
            "allowed_folder_uids": ["fld-payments"],
            "allowed_dashboard_uids": [],
            "denied_dashboard_uids": [],
            "allowed_environments": [],
        })
        inventory = {
            "available": True,
            # The historical fixture folder is present in Grafana.
            "folders": ["", "fld-payments", "fld-qa-unassigned", "fld-alerts"],
            "dashboards": [],
            "error": None,
        }
        report = _run(audit_seed_references(accounts=accounts, inventory=inventory))
        assert report["stale_reference_count"] == 0

    def test_the_audit_performs_no_deletion(self, monkeypatch, seeded_db):
        """Nothing in the audit path deletes a Grafana resource."""
        import app.mcp.session as session_module

        calls: list[tuple[str, dict]] = []

        async def recorder(tool, args, **kwargs):
            calls.append((tool, dict(args)))
            if args.get("endpoint") == "/api/folders":
                return json.dumps([{"uid": "fld-a", "title": "A"}])
            return json.dumps([])

        monkeypatch.setattr(session_module, "mcp_call", recorder)

        inventory = {
            "available": True,
            "folders": ["", "fld-qa-unassigned"],
            "dashboards": [],
            "error": None,
        }
        _run(audit_seed_references(inventory=inventory))

        for tool, args in calls:
            assert str(args.get("method", "GET")).upper() == "GET", (tool, args)
            assert "DELETE" not in json.dumps(args).upper()
