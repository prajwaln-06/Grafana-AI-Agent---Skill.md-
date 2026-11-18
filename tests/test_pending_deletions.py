"""tests/test_pending_deletions.py

Tests for maintainer deletion escalation and administrative Pending Deletions tab endpoints.
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.api.main import app
from app.auth.dependencies import require_admin_principal, require_principal
from app.auth.principal import Principal, set_current_principal
from app.storage.database import get_db_session, init_db
from app.storage.models import ApprovalModel, ProposalModel, UserModel
from app.storage.seed import seed_dummy_data_if_empty


@pytest.fixture(autouse=True)
def setup_seed():
    # Ensure tables exist in the test-isolated SQLite DB before seeding.
    init_db(auto_seed=False)
    seed_dummy_data_if_empty(force=True)


@pytest.fixture(autouse=True)
def cleanup_dependency_overrides():
    """Remove any dependency overrides installed by each test after it finishes."""
    yield
    app.dependency_overrides.pop(require_principal, None)
    app.dependency_overrides.pop(require_admin_principal, None)


alex_rivera = Principal(
    user_id="usr_maint_01",
    issuer="https://authentik.test",
    subject="sub_maint_01",
    authenticated=True,
    status="active",
    role="maintainer",
    allowed_folder_uids=frozenset({"fld-payments", "afvlix95l3wg0d"}),
    allowed_dashboard_uids=frozenset({"dash-checkout-slo", "dash-global-overview"}),
    denied_dashboard_uids=frozenset({"dash-pci-compliance"}),
    allowed_environments=frozenset({"staging", "production"}),
    username="alex.rivera",
    name="Alex Rivera (Maintainer)",
)

admin_user = Principal(
    user_id="usr_admin_01",
    issuer="https://authentik.test",
    subject="sub_admin_01",
    authenticated=True,
    status="active",
    role="admin",
    allowed_folder_uids=frozenset({"*"}),
    allowed_environments=frozenset({"staging", "production"}),
    username="akadmin",
    name="System Administrator",
)


def test_maintainer_delete_folder_escalated():
    """When a maintainer asks to delete a folder in chat, it must be escalated to admin."""
    app.dependency_overrides[require_principal] = lambda: alex_rivera
    set_current_principal(alex_rivera)

    with patch("app.mcp.session.mcp_call", new=AsyncMock(return_value="")):
        client = TestClient(app)
        resp = client.post(
            "/api/chat",
            json={"message": "delete folder fid-payments", "stream": False},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()

        # Chat UI must state that it is escalated to admin
        assert "escalated to an administrator" in data["answer"].lower()
        assert "pending deletions" in data["answer"].lower()

        # Steps must reflect escalation
        step_results = [s.get("result") for s in data.get("steps", [])]
        assert "ESCALATED_TO_ADMIN" in step_results

        # Proposal must be created and pending
        pid = data.get("proposalId")
        assert pid is not None

        with get_db_session() as session:
            prop = session.get(ProposalModel, pid)
            assert prop is not None
            assert prop.impact_class == "destructive_delete"
            assert prop.state == "pending_approval"
            assert prop.requires_second_approver is True


def test_admin_pending_deletions_list_and_approve():
    """Admin can list pending deletions and approve/reject them."""
    # 1. Non-admin (maintainer) cannot access admin pending deletions endpoint
    app.dependency_overrides[require_principal] = lambda: alex_rivera
    app.dependency_overrides.pop(require_admin_principal, None)
    set_current_principal(alex_rivera)

    with patch("app.mcp.session.mcp_call", new=AsyncMock(return_value="")):
        client = TestClient(app)
        resp = client.get("/api/v1/admin/pending-deletions")
        assert resp.status_code == 403

        # 2. Admin can list pending deletions
        app.dependency_overrides[require_principal] = lambda: admin_user
        app.dependency_overrides[require_admin_principal] = lambda: admin_user
        set_current_principal(admin_user)

        resp = client.get("/api/v1/admin/pending-deletions")
        assert resp.status_code == 200
        data = resp.json()
        assert "pending_deletions" in data
        assert data["count"] >= 1

        # Check prop_8822 (seeded destructive deletion)
        prop_ids = [item["proposal_id"] for item in data["pending_deletions"]]
        assert "prop_8822" in prop_ids

        # 3. Admin approves prop_8822
        resp = client.post("/api/v1/admin/pending-deletions/prop_8822/approve")
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"

        with get_db_session() as session:
            prop = session.get(ProposalModel, "prop_8822")
            assert prop.state == "applied"
            appr = session.query(ApprovalModel).filter_by(proposal_id="prop_8822").first()
            assert appr is not None
            assert appr.decision == "approved"

        # 4. Now prop_8822 should no longer be in pending deletions
        resp = client.get("/api/v1/admin/pending-deletions")
        assert resp.status_code == 200
        updated_ids = [item["proposal_id"] for item in resp.json()["pending_deletions"]]
        assert "prop_8822" not in updated_ids

