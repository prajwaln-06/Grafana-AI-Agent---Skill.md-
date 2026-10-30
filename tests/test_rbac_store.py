"""
tests/test_rbac_store.py

Unit tests for the User & Role Store, dummy data seeding, auto-registration,
and administrative security guardrails.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.storage.audit_store import AuditStore
from app.storage.models import Base, UserModel
from app.storage.seed import seed_dummy_data_if_empty, DEFAULT_ISSUER
from app.storage.user_store import AdminSecurityError, UserStore


@pytest.fixture
def test_db_session():
    """In-memory SQLite database session isolated for testing."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()


def test_seed_dummy_data(test_db_session):
    """Confirm seeding creates the 6 dummy users representing all roles and statuses."""
    # Seed directly into test session
    import app.storage.database as db_mod
    original_factory = db_mod._SessionFactory
    db_mod._SessionFactory = lambda: test_db_session

    try:
        seeded = seed_dummy_data_if_empty(force=True)
        assert seeded is True

        users = UserStore.list_users(test_db_session)
        assert len(users) == 6

        # Check Admin
        admin = UserStore.get_user_by_id(test_db_session, "usr_admin_01")
        assert admin is not None
        assert admin.role == "admin"
        assert admin.status == "active"
        assert admin.scopes.allowed_folder_uids == ["*"]

        # Check Maintainer
        maint = UserStore.get_user_by_id(test_db_session, "usr_maint_01")
        assert maint is not None
        assert maint.role == "maintainer"
        assert "dash-pci-compliance" in maint.scopes.denied_dashboard_uids
        assert "fld-payments" in maint.scopes.allowed_folder_uids

        # Check Pending & Deactivated
        pending = UserStore.get_user_by_id(test_db_session, "usr_pending_01")
        assert pending.status == "pending"
        assert pending.role is None

        deact = UserStore.get_user_by_id(test_db_session, "usr_deact_01")
        assert deact.status == "deactivated"

        # Check Audit log
        events = AuditStore.list_events(test_db_session)
        assert len(events) >= 3
    finally:
        db_mod._SessionFactory = original_factory


def test_auto_register_pending_user(test_db_session):
    """Unknown authenticated identity is auto-registered as pending with empty scopes."""
    issuer = "http://localhost:9000/application/o/test/"
    subject = "sub_brand_new_user"

    user = UserStore.create_pending_user(
        test_db_session,
        issuer=issuer,
        subject=subject,
        email="newuser@example.com",
        username="new.user",
        display_name="New User",
    )

    assert user.user_id.startswith("usr_")
    assert user.status == "pending"
    assert user.role is None
    assert user.authz_version == 1
    assert user.scopes.allowed_folder_uids == []
    assert user.scopes.allowed_dashboard_uids == []

    # Audit log entry created
    events = AuditStore.list_events(test_db_session, event_type="first_sign_in")
    assert len(events) == 1
    assert events[0].actor_subject == subject
    assert events[0].decision == "ALLOW"


def test_admin_cannot_edit_own_privileges(test_db_session):
    """Section 5.4: Administrator cannot modify their own role, scopes, or status."""
    admin = UserStore.create_pending_user(
        test_db_session,
        issuer="test_iss",
        subject="test_admin_sub",
        email="admin@test.com",
    )
    admin.status = "active"
    admin.role = "admin"
    test_db_session.flush()

    with pytest.raises(AdminSecurityError, match="Administrators cannot modify their own"):
        UserStore.update_user_access(
            test_db_session,
            admin_user_id=admin.user_id,
            target_user_id=admin.user_id,
            role="maintainer",
        )


def test_last_administrator_protection(test_db_session):
    """Section 5.4: Cannot remove or deactivate the last active administrator."""
    admin1 = UserStore.create_pending_user(test_db_session, issuer="test_iss", subject="admin1")
    admin1.status = "active"
    admin1.role = "admin"

    other_admin = UserStore.create_pending_user(test_db_session, issuer="test_iss", subject="admin2")
    other_admin.status = "active"
    other_admin.role = "maintainer"  # only admin1 is active admin
    test_db_session.flush()

    assert UserStore.count_active_admins(test_db_session) == 1

    # Demoting admin1 by another user must fail
    with pytest.raises(AdminSecurityError, match="Cannot remove or deactivate the last active Administrator"):
        UserStore.update_user_access(
            test_db_session,
            admin_user_id=other_admin.user_id,
            target_user_id=admin1.user_id,
            role="maintainer",
        )

    # Deactivating admin1 must fail
    with pytest.raises(AdminSecurityError, match="Cannot remove or deactivate the last active Administrator"):
        UserStore.update_user_access(
            test_db_session,
            admin_user_id=other_admin.user_id,
            target_user_id=admin1.user_id,
            status="deactivated",
        )

