"""
app/storage/seed.py

Seeds the database with realistic sample users, scopes, proposals, approvals,
and audit logs representing all application roles, statuses, and resource boundaries.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import logging
from sqlalchemy import select

from app.storage.database import get_db_session
from app.storage.models import (
    ApprovalModel,
    AuditLogModel,
    ProposalModel,
    UserModel,
    UserScopeModel,
)

logger = logging.getLogger(__name__)

DEFAULT_ISSUER = "http://localhost:9000/application/o/grafana-ai-agent/"


def seed_dummy_data_if_empty(force: bool = False) -> bool:
    """Populate the database with realistic initial dummy data if users table is empty."""
    with get_db_session() as session:
        existing_user = session.execute(select(UserModel).limit(1)).scalar_one_or_none()
        if existing_user is not None and not force:
            logger.debug("Database already contains data; skipping seed.")
            return False

        logger.info("Seeding database with realistic RBAC dummy data...")
        now = datetime.datetime.now(datetime.timezone.utc)

        # ----------------------------------------------------------------------
        # 1. Users & Scopes
        # ----------------------------------------------------------------------
        users_data = [
            {
                "user_id": "usr_admin_01",
                "issuer": DEFAULT_ISSUER,
                "subject": "ak_sub_admin_system",
                "email": "admin@example.com",
                "username": "akadmin",
                "display_name": "System Administrator",
                "status": "active",
                "role": "admin",
                "authz_version": 1,
                "created_at": now - datetime.timedelta(days=30),
                "last_seen_at": now,
                "scopes": {
                    "allowed_folder_uids": ["*"],
                    "allowed_dashboard_uids": [],
                    "denied_dashboard_uids": [],
                    "allowed_environments": ["staging", "production"],
                },
            },
            {
                "user_id": "usr_maint_01",
                "issuer": DEFAULT_ISSUER,
                "subject": "ak_sub_maintainer_01",
                "email": "maintainer@example.com",
                "username": "alex.rivera",
                "display_name": "Alex Rivera (Maintainer)",
                "status": "active",
                "role": "maintainer",
                "authz_version": 2,
                "created_at": now - datetime.timedelta(days=20),
                "last_seen_at": now - datetime.timedelta(hours=2),
                "scopes": {
                    "allowed_folder_uids": ["fld-payments", "afvlix95l3wg0d"],
                    "allowed_dashboard_uids": ["dash-checkout-slo", "dash-global-overview"],
                    "denied_dashboard_uids": ["dash-pci-compliance"],
                    "allowed_environments": ["staging", "production"],
                },
            },
            {
                "user_id": "usr_oper_01",
                "issuer": DEFAULT_ISSUER,
                "subject": "ak_sub_operator_01",
                "email": "operator@example.com",
                "username": "sam.chen",
                "display_name": "Sam Chen (On-Call SRE)",
                "status": "active",
                "role": "operator",
                "authz_version": 1,
                "created_at": now - datetime.timedelta(days=15),
                "last_seen_at": now - datetime.timedelta(minutes=30),
                "scopes": {
                    "allowed_folder_uids": ["fld-alerts", "afvlix95l3wg0d"],
                    "allowed_dashboard_uids": [],
                    "denied_dashboard_uids": [],
                    "allowed_environments": ["staging", "production"],
                },
            },
            {
                "user_id": "usr_obs_01",
                "issuer": DEFAULT_ISSUER,
                "subject": "ak_sub_observer_01",
                "email": "observer@example.com",
                "username": "jordan.taylor",
                "display_name": "Jordan Taylor (Analyst)",
                "status": "active",
                "role": "observer",
                "authz_version": 1,
                "created_at": now - datetime.timedelta(days=10),
                "last_seen_at": now - datetime.timedelta(days=1),
                "scopes": {
                    "allowed_folder_uids": ["fld-payments"],
                    "allowed_dashboard_uids": ["dash-checkout-slo"],
                    "denied_dashboard_uids": [],
                    "allowed_environments": ["staging"],
                },
            },
            {
                "user_id": "usr_pending_01",
                "issuer": DEFAULT_ISSUER,
                "subject": "ak_sub_new_hire",
                "email": "newhire@example.com",
                "username": "morgan.lee",
                "display_name": "Morgan Lee (New Hire)",
                "status": "pending",
                "role": None,
                "authz_version": 1,
                "created_at": now - datetime.timedelta(hours=5),
                "last_seen_at": now - datetime.timedelta(hours=5),
                "scopes": {
                    "allowed_folder_uids": [],
                    "allowed_dashboard_uids": [],
                    "denied_dashboard_uids": [],
                    "allowed_environments": [],
                },
            },
            {
                "user_id": "usr_deact_01",
                "issuer": DEFAULT_ISSUER,
                "subject": "ak_sub_former_emp",
                "email": "former@example.com",
                "username": "casey.old",
                "display_name": "Casey Former (Departed)",
                "status": "deactivated",
                "role": "observer",
                "authz_version": 3,
                "created_at": now - datetime.timedelta(days=90),
                "last_seen_at": now - datetime.timedelta(days=45),
                "scopes": {
                    "allowed_folder_uids": [],
                    "allowed_dashboard_uids": [],
                    "denied_dashboard_uids": [],
                    "allowed_environments": [],
                },
            },
        ]

        for u in users_data:
            scopes_info = u.pop("scopes")
            user_row = UserModel(**u)
            scope_row = UserScopeModel(user_id=u["user_id"], **scopes_info)
            user_row.scopes = scope_row
            session.add(user_row)

        session.flush()

        # ----------------------------------------------------------------------
        # 2. Proposals & Approvals
        # ----------------------------------------------------------------------
        # Proposal 1: Routine panel addition on checkout SLO (self-approved maintainer edit)
        ir_1 = {
            "title": "Add p99 Latency to Checkout SLO",
            "operation": "update",
            "dashboardUid": "dash-checkout-slo",
            "folderUid": "fld-payments",
            "panels": [{"title": "p99 Latency (5m)", "type": "timeseries"}],
        }
        digest_1 = hashlib.sha256(json.dumps(ir_1, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

        prop_1 = ProposalModel(
            proposal_id="prop_8821",
            version=1,
            proposer_id="usr_maint_01",
            operation="update",
            impact_class="routine_edit",
            target_resource={"dashboardUid": "dash-checkout-slo", "folderUid": "fld-payments"},
            canonical_content=ir_1,
            digest=digest_1,
            state="approved",
            requires_second_approver=False,
            policy_version="1.0",
            created_at=now - datetime.timedelta(hours=3),
            updated_at=now - datetime.timedelta(hours=2),
        )
        session.add(prop_1)

        appr_1 = ApprovalModel(
            approval_id="appr_501",
            proposal_id="prop_8821",
            proposal_version=1,
            digest=digest_1,
            approver_id="usr_maint_01",
            decision="approved",
            approver_authz_version=2,
            approval_token="appr_tok_8821_demo_token_valid",
            token_expires_at=now + datetime.timedelta(minutes=5),
            token_used=False,
            created_at=now - datetime.timedelta(hours=2),
        )
        session.add(appr_1)

        # Proposal 2: Destructive delete requiring independent second approver
        ir_2 = {
            "title": "Deprecate Checkout SLO Dashboard",
            "operation": "remove",
            "dashboardUid": "dash-checkout-slo",
            "folderUid": "fld-payments",
            "removeDashboard": True,
        }
        digest_2 = hashlib.sha256(json.dumps(ir_2, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

        prop_2 = ProposalModel(
            proposal_id="prop_8822",
            version=1,
            proposer_id="usr_maint_01",
            operation="delete",
            impact_class="destructive_delete",
            target_resource={"dashboardUid": "dash-checkout-slo", "folderUid": "fld-payments"},
            canonical_content=ir_2,
            digest=digest_2,
            state="pending_approval",
            requires_second_approver=True,
            policy_version="1.0",
            created_at=now - datetime.timedelta(minutes=45),
            updated_at=now - datetime.timedelta(minutes=45),
        )
        session.add(prop_2)

        # ----------------------------------------------------------------------
        # 3. Audit Logs
        # ----------------------------------------------------------------------
        audit_records = [
            AuditLogModel(
                event_type="admin_bootstrap",
                actor_id="system",
                target_type="user",
                target_id="usr_admin_01",
                decision="ALLOW",
                reason="initial_administrator_bootstrap",
                details={"email": "admin@example.com"},
                timestamp=now - datetime.timedelta(days=30),
            ),
            AuditLogModel(
                event_type="first_sign_in",
                actor_id="usr_pending_01",
                actor_issuer=DEFAULT_ISSUER,
                actor_subject="ak_sub_new_hire",
                target_type="user",
                target_id="usr_pending_01",
                decision="ALLOW",
                reason="new_user_registered_as_pending",
                details={"email": "newhire@example.com"},
                timestamp=now - datetime.timedelta(hours=5),
            ),
            AuditLogModel(
                event_type="approval_recorded",
                actor_id="usr_maint_01",
                target_type="proposal",
                target_id="prop_8821",
                decision="ALLOW",
                reason="routine_edit_self_approval_permitted",
                details={"version": 1, "digest": digest_1},
                timestamp=now - datetime.timedelta(hours=2),
            ),
        ]
        for a in audit_records:
            session.add(a)

        logger.info("Successfully seeded 6 users, 2 proposals, 1 approval, and 3 audit logs.")
        return True

