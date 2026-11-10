"""
app/storage/seed.py

Seeds the database with realistic sample users, scopes, proposals, approvals,
and audit logs representing all application roles, statuses, and resource
boundaries.

The seeder is deliberately conservative:

* It only ever *creates* records. A record that already exists is never
  modified, so an administrator's manual configuration of a demo user's
  role, status or scopes is preserved across restarts and re-seeds.
* It is idempotent: running it twice produces the same state, with no
  duplicate users, proposals, approvals or audit records.
* It only manages the records it owns -- the demo accounts listed in
  :data:`DEMO_USER_IDS`. A user that an administrator created is never
  touched, not even in ``force`` mode.
* The demo accounts reference Grafana folders and dashboards by UID. Those
  UIDs belong to a particular Grafana instance, so they can be stale on a
  deployment that has different resources. The seeder does not silently
  repair a stale reference (that would hand a demo user access to an
  unrelated resource); it reports the stale references instead -- see
  :mod:`app.storage.seed_audit`.
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

#: The subject prefix the demo accounts use. The identity resolver treats a
#: pre-seeded record with this prefix as a demo account that may be linked
#: to a real identity on first sign-in.
DEMO_SUBJECT_PREFIX = "ak_sub_"


def _demo_users(now: datetime.datetime) -> list[dict]:
    """The demo accounts the seeder owns."""
    return [
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
            "user_id": "usr_test_maurya_01",
            "issuer": DEFAULT_ISSUER,
            "subject": "ak_sub_maurya_test",
            "email": "maurya@example.com",
            "username": "maurya",
            "display_name": "Maurya Test",
            "status": "pending",
            "role": None,
            "authz_version": 1,
            "created_at": now - datetime.timedelta(hours=2),
            "last_seen_at": now - datetime.timedelta(hours=2),
            "scopes": {
                "allowed_folder_uids": [],
                "allowed_dashboard_uids": [],
                "denied_dashboard_uids": [],
                "allowed_environments": [],
            },
        },
        {
            "user_id": "usr_test_jaidev_01",
            "issuer": DEFAULT_ISSUER,
            "subject": "ak_sub_jaidev_test",
            "email": "jaidev@example.com",
            "username": "jaidev",
            "display_name": "Jaidev Test",
            "status": "pending",
            "role": None,
            "authz_version": 1,
            "created_at": now - datetime.timedelta(hours=1),
            "last_seen_at": now - datetime.timedelta(hours=1),
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


#: The user IDs of the demo accounts the seeder owns.
DEMO_USER_IDS: tuple[str, ...] = tuple(
    user["user_id"] for user in _demo_users(datetime.datetime.now(datetime.timezone.utc))
)

#: The scope values the seeder creates, used to tell an untouched demo record
#: apart from one an administrator has reconfigured.
DEMO_SEEDED_SCOPES: dict[str, dict[str, list[str]]] = {
    user["user_id"]: dict(user["scopes"])
    for user in _demo_users(datetime.datetime.now(datetime.timezone.utc))
}


def is_demo_user(user: UserModel | None) -> bool:
    """Is this record one of the demo accounts the seeder owns?

    A record counts as demo data when it carries one of the seeder's user
    IDs or when its subject carries the demo subject prefix. Real users
    provisioned by an administrator never match either.
    """
    if user is None:
        return False
    if user.user_id in DEMO_USER_IDS:
        return True
    return str(user.subject or "").startswith(DEMO_SUBJECT_PREFIX)


def seed_dummy_data_if_empty(force: bool = False) -> bool:
    """Populate the database with the demo data if it is not there yet.

    Returns ``True`` when at least one record was created.

    The function is idempotent and never overwrites anything: a demo account
    that already exists is left exactly as it is, so an administrator's
    configuration of it survives both a restart and an explicit re-seed. In
    ``force`` mode the seeder re-creates only the demo records that are
    *missing*; it still never modifies an existing one.
    """
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
        created_users = 0
        for u in _demo_users(now):
            if session.get(UserModel, u["user_id"]) is not None:
                # The account already exists. Its role, status and scopes may
                # have been configured by an administrator -- leave them alone.
                logger.debug("Demo account %s already exists; leaving it untouched.", u["user_id"])
                continue
            scopes_info = u["scopes"]
            user_fields = {k: v for k, v in u.items() if k != "scopes"}
            user_row = UserModel(**user_fields)
            scope_row = UserScopeModel(user_id=u["user_id"], **scopes_info)
            user_row.scopes = scope_row
            session.add(user_row)
            created_users += 1

        session.flush()

        # ----------------------------------------------------------------------
        # 2. Proposals & Approvals
        # ----------------------------------------------------------------------
        created_proposals = 0
        created_approvals = 0

        # Proposal 1: Routine panel addition on checkout SLO (self-approved maintainer edit)
        ir_1 = {
            "title": "Add p99 Latency to Checkout SLO",
            "operation": "update",
            "dashboardUid": "dash-checkout-slo",
            "folderUid": "fld-payments",
            "panels": [{"title": "p99 Latency (5m)", "type": "timeseries"}],
        }
        digest_1 = hashlib.sha256(json.dumps(ir_1, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

        if session.get(ProposalModel, "prop_8821") is None:
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
            created_proposals += 1

        if session.query(ApprovalModel).filter_by(approval_id="appr_501").first() is None:
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
            created_approvals += 1

        # Proposal 2: Destructive delete requiring independent second approver
        ir_2 = {
            "title": "Deprecate Checkout SLO Dashboard",
            "operation": "remove",
            "dashboardUid": "dash-checkout-slo",
            "folderUid": "fld-payments",
            "removeDashboard": True,
        }
        digest_2 = hashlib.sha256(json.dumps(ir_2, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

        if session.get(ProposalModel, "prop_8822") is None:
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
            created_proposals += 1

        # ----------------------------------------------------------------------
        # 3. Audit Logs
        # ----------------------------------------------------------------------
        existing_event_types = {
            row[0] for row in session.execute(
                select(AuditLogModel.event_type).where(
                    AuditLogModel.event_type.in_(
                        ("admin_bootstrap", "first_sign_in", "approval_recorded")
                    )
                )
            ).all()
        }
        audit_records = []
        if "admin_bootstrap" not in existing_event_types:
            audit_records.append(
                AuditLogModel(
                    event_type="admin_bootstrap",
                    actor_id="system",
                    target_type="user",
                    target_id="usr_admin_01",
                    decision="ALLOW",
                    reason="initial_administrator_bootstrap",
                    details={"email": "admin@example.com"},
                    timestamp=now - datetime.timedelta(days=30),
                )
            )
        if "first_sign_in" not in existing_event_types:
            audit_records.append(
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
                )
            )
        if "approval_recorded" not in existing_event_types:
            audit_records.append(
                AuditLogModel(
                    event_type="approval_recorded",
                    actor_id="usr_maint_01",
                    target_type="proposal",
                    target_id="prop_8821",
                    decision="ALLOW",
                    reason="routine_edit_self_approval_permitted",
                    details={"version": 1, "digest": digest_1},
                    timestamp=now - datetime.timedelta(hours=2),
                )
            )
        for a in audit_records:
            session.add(a)

        created_records = created_users + len(audit_records) + created_proposals + created_approvals
        if created_records == 0:
            logger.info("Seed data is already present; nothing to create.")
            return False

        logger.info(
            "Seeded %d demo user(s), %d proposal(s), %d approval(s) and %d audit record(s); "
            "existing records were left untouched.",
            created_users,
            created_proposals,
            created_approvals,
            len(audit_records),
        )
        return True
