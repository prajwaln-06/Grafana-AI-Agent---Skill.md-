"""
tests/test_cli_users_add.py

Focused tests for ``agent-cli admin users add`` -- the command line client
that creates a new account in the User & Role Store.

The tests run against the suite's throwaway SQLite database (the session
fixture in ``tests/conftest.py`` points ``DATABASE_URL`` at it), and they
assert against the application's own store: the row that is written, the
scopes that are stored with it, the ``authz_version`` counter and the audit
records.
"""
from __future__ import annotations

import datetime

import pytest

from app.storage.database import get_db_session, init_db
from app.storage.models import AuditLogModel, UserModel, UserScopeModel
from app.storage.user_store import UserStore
from cli.main import main
from cli.users import DuplicateUserError, UserInputError, add_user

ISSUER = "http://localhost:9000/application/o/grafana-ai-agent/"
ADMIN_ID = "usr_cli_test_admin"


@pytest.fixture(scope="module", autouse=True)
def _schema():
    """Create the tables in the throwaway database."""
    init_db(auto_seed=False)


@pytest.fixture()
def admin_user():
    """An active administrator, the actor a role or scope change is audited against."""
    now = datetime.datetime.now(datetime.timezone.utc)
    with get_db_session() as session:
        existing = UserStore.get_user_by_id(session, ADMIN_ID)
        if existing is None:
            admin = UserModel(
                user_id=ADMIN_ID,
                issuer=ISSUER,
                subject="ak_sub_cli_test_admin",
                email="cli-admin@example.com",
                username="cli-admin",
                display_name="CLI Test Admin",
                status="active",
                role="admin",
                authz_version=1,
                created_at=now,
                last_seen_at=now,
            )
            admin.scopes = UserScopeModel(
                user_id=ADMIN_ID,
                allowed_folder_uids=["*"],
                allowed_dashboard_uids=[],
                denied_dashboard_uids=[],
                allowed_environments=["staging", "production"],
                role_grants=[],
            )
            session.add(admin)
    yield ADMIN_ID


@pytest.fixture()
def clean_users():
    """Remove the accounts the tests created."""
    yield
    with get_db_session() as session:
        for user in session.query(UserModel).filter(
            UserModel.subject.like("ak_sub_cli_test_%")
        ).all():
            session.delete(user)


def _stored(subject: str, issuer: str = ISSUER) -> dict | None:
    """A snapshot of the stored account (the ORM row is detached afterwards)."""
    with get_db_session() as session:
        user = UserStore.get_user_by_identity(session, issuer, subject)
        if user is None:
            return None
        return {
            "user_id": user.user_id,
            "status": user.status,
            "role": user.role,
            "username": user.username,
            "display_name": user.display_name,
            "email": user.email,
            "authz_version": user.authz_version,
            "allowed_folder_uids": list(user.scopes.allowed_folder_uids) if user.scopes else [],
            "allowed_dashboard_uids": list(user.scopes.allowed_dashboard_uids) if user.scopes else [],
            "denied_dashboard_uids": list(user.scopes.denied_dashboard_uids) if user.scopes else [],
            "allowed_environments": list(user.scopes.allowed_environments) if user.scopes else [],
        }


def _audit_events(subject: str) -> list[str]:
    """The audit events recorded for the account with this identity."""
    with get_db_session() as session:
        user = UserStore.get_user_by_identity(session, ISSUER, subject)
        if user is None:
            return []
        rows = (
            session.query(AuditLogModel)
            .filter(AuditLogModel.target_id == user.user_id)
            .all()
        )
        return [row.event_type for row in rows]


# ---------------------------------------------------------------------------
# Creation and persistence
# ---------------------------------------------------------------------------

def test_add_user_creates_a_pending_account(clean_users):
    """A new account is created pending, without a role and without scopes."""
    summary = add_user(
        subject="ak_sub_cli_test_new",
        username="cli.new",
        display_name="CLI Test New",
        email="cli.new@example.com",
    )

    assert summary["status"] == "pending"
    assert summary["role"] is None
    assert summary["username"] == "cli.new"
    assert summary["display_name"] == "CLI Test New"
    assert summary["email"] == "cli.new@example.com"

    stored = _stored("ak_sub_cli_test_new")
    assert stored is not None, "the account must be persisted in the database"
    assert stored["user_id"] == summary["user_id"]
    assert stored["status"] == "pending"
    assert stored["role"] is None
    assert stored["allowed_folder_uids"] == []
    assert stored["allowed_dashboard_uids"] == []
    assert stored["allowed_environments"] == []


def test_add_user_activates_and_scopes_an_account(admin_user, clean_users):
    """A role, a status and the four scope lists are stored as given."""
    summary = add_user(
        subject="ak_sub_cli_test_scoped",
        username="cli.scoped",
        display_name="CLI Test Scoped",
        email="cli.scoped@example.com",
        role="maintainer",
        admin_user_id=admin_user,
        allowed_folder_uids=["fld-payments"],
        allowed_dashboard_uids=["dash-checkout-slo"],
        denied_dashboard_uids=["dash-pci-compliance"],
        allowed_environments=["staging"],
    )

    assert summary["status"] == "active"
    assert summary["role"] == "maintainer"

    stored = _stored("ak_sub_cli_test_scoped")
    assert stored is not None
    assert stored["status"] == "active"
    assert stored["role"] == "maintainer"
    assert stored["allowed_folder_uids"] == ["fld-payments"]
    assert stored["allowed_dashboard_uids"] == ["dash-checkout-slo"]
    assert stored["denied_dashboard_uids"] == ["dash-pci-compliance"]
    assert stored["allowed_environments"] == ["staging"]
    assert stored["authz_version"] >= 2, "an access change bumps authz_version"

    events = _audit_events("ak_sub_cli_test_scoped")
    assert "first_sign_in" in events
    assert "user_access_modified" in events
    assert "user_scope_changed" in events


def test_add_user_rejects_a_duplicate_identity(clean_users):
    """The same (issuer, subject) is never created twice."""
    add_user(subject="ak_sub_cli_test_dup", username="cli.dup")

    with pytest.raises(DuplicateUserError) as excinfo:
        add_user(subject="ak_sub_cli_test_dup", username="cli.dup2")
    assert "already exists" in str(excinfo.value)

    with get_db_session() as session:
        matching = [
            user
            for user in UserStore.list_users(session)
            if user.subject == "ak_sub_cli_test_dup"
        ]
    assert len(matching) == 1, "the duplicate must not create a second account"


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"subject": ""}, "--subject is required."),
        ({"subject": "has space"}, "whitespace"),
        ({"role": "wizard"}, "--role must be one of"),
        ({"status": "sleeping"}, "--status must be one of"),
        ({"email": "not-an-email"}, "--email is not a valid email address."),
        ({"allowed_folder_uids": ["bad uid"]}, "Grafana UIDs only"),
        ({"allowed_folder_uids": ["a,b"]}, "contains a comma"),
        ({"allowed_environments": ["not an env"]}, "environment names only"),
    ],
)
def test_add_user_rejects_invalid_input(kwargs, message, clean_users):
    """Invalid attributes never reach the store."""
    with pytest.raises(UserInputError) as excinfo:
        add_user(**{"subject": "ak_sub_cli_test_invalid", **kwargs})
    assert message in str(excinfo.value)


def test_add_user_requires_an_administrator_for_grants(clean_users):
    """A role or a scope can only be granted on behalf of an administrator."""
    with pytest.raises(UserInputError) as excinfo:
        add_user(subject="ak_sub_cli_test_noadmin", role="maintainer")
    assert "--admin is required" in str(excinfo.value)

    with pytest.raises(UserInputError) as excinfo:
        add_user(subject="ak_sub_cli_test_noadmin", allowed_folder_uids=["fld-payments"])
    assert "--admin is required" in str(excinfo.value)


def test_add_user_rejects_an_unknown_administrator(clean_users):
    """The administrator the change is audited against must exist."""
    with pytest.raises(UserInputError) as excinfo:
        add_user(
            subject="ak_sub_cli_test_badadmin",
            role="observer",
            admin_user_id="usr_cli_test_missing",
        )
    assert "not found" in str(excinfo.value)


def test_add_user_rejects_a_non_administrator_actor(admin_user, clean_users):
    """Only an active administrator may act for the store."""
    with get_db_session() as session:
        user = UserStore.create_pending_user(
            session,
            issuer=ISSUER,
            subject="ak_sub_cli_test_plain",
            email="plain@example.com",
            username="cli.plain",
        )
        plain_id = user.user_id

    with pytest.raises(UserInputError) as excinfo:
        add_user(
            subject="ak_sub_cli_test_needsadmin",
            role="observer",
            admin_user_id=plain_id,
        )
    assert "not an active administrator" in str(excinfo.value)


# ---------------------------------------------------------------------------
# The command line itself
# ---------------------------------------------------------------------------

def test_cli_creates_a_user_and_reports_it(capsys, clean_users):
    """``agent-cli admin users add`` writes the account and prints the result."""
    exit_code = main(
        [
            "admin",
            "users",
            "add",
            "--subject",
            "ak_sub_cli_test_cli",
            "--username",
            "cli.cli",
            "--name",
            "CLI Test Command Line",
            "--email",
            "cli.cli@example.com",
        ]
    )
    assert exit_code == 0

    out = capsys.readouterr().out
    assert "[200 OK]" in out
    assert "Created user" in out

    stored = _stored("ak_sub_cli_test_cli")
    assert stored is not None
    assert stored["username"] == "cli.cli"
    assert stored["display_name"] == "CLI Test Command Line"


def test_cli_reports_a_duplicate_and_exits_non_zero(capsys, clean_users):
    """A second attempt on the same identity fails with the duplicate code."""
    assert main(["admin", "users", "add", "--subject", "ak_sub_cli_test_cli2"]) == 0
    capsys.readouterr()

    assert main(["admin", "users", "add", "--subject", "ak_sub_cli_test_cli2"]) == 3
    assert "already exists" in capsys.readouterr().err


def test_cli_help_lists_the_command(capsys):
    """The help output shows the command and a usage example."""
    with pytest.raises(SystemExit) as excinfo:
        main(["admin", "users", "add", "--help"])
    assert excinfo.value.code == 0

    out = capsys.readouterr().out
    assert "--subject" in out
    assert "--allow-folder" in out
    assert "example:" in out
