"""
app/storage/user_store.py

User & Role Store repository managing users, role assignments, scopes,
and security constraints (last-administrator guard, no self-privilege change).
"""
from __future__ import annotations

import datetime
import secrets
from typing import Any, Mapping

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.storage.audit_store import AuditStore
from app.storage.models import UserModel, UserScopeModel


class AdminSecurityError(ValueError):
    """Raised when an administrative modification violates core security invariants."""


class UserStore:
    """Data access layer for application users and scopes."""

    @staticmethod
    def generate_user_id() -> str:
        return f"usr_{secrets.token_hex(6)}"

    @staticmethod
    def get_user_by_identity(session: Session, issuer: str, subject: str) -> UserModel | None:
        """Lookup a user by the stable (issuer, subject) identity key."""
        stmt = (
            select(UserModel)
            .options(joinedload(UserModel.scopes))
            .where(UserModel.issuer == issuer, UserModel.subject == subject)
        )
        return session.execute(stmt).scalar_one_or_none()

    @staticmethod
    def get_user_by_id(session: Session, user_id: str) -> UserModel | None:
        """Lookup a user by internal user_id."""
        stmt = (
            select(UserModel)
            .options(joinedload(UserModel.scopes))
            .where(UserModel.user_id == user_id)
        )
        return session.execute(stmt).scalar_one_or_none()

    @classmethod
    def create_pending_user(
        cls,
        session: Session,
        *,
        issuer: str,
        subject: str,
        email: str | None = None,
        username: str | None = None,
        display_name: str | None = None,
    ) -> UserModel:
        """Auto-register an unknown authenticated identity as pending with no privileges.
        
        Per Section 4.4 and Section 5.2, an unknown identity receives status 'pending'
        and empty scopes awaiting administrator activation.
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        user_id = cls.generate_user_id()

        user = UserModel(
            user_id=user_id,
            issuer=issuer,
            subject=subject,
            email=email,
            username=username,
            display_name=display_name,
            status="pending",
            role=None,
            authz_version=1,
            created_at=now,
            last_seen_at=now,
        )
        scopes = UserScopeModel(
            user_id=user_id,
            allowed_folder_uids=[],
            allowed_dashboard_uids=[],
            denied_dashboard_uids=[],
            allowed_environments=[],
        )
        user.scopes = scopes

        session.add(user)
        session.flush()

        AuditStore.log_event(
            session,
            event_type="first_sign_in",
            actor_id=user_id,
            actor_issuer=issuer,
            actor_subject=subject,
            target_type="user",
            target_id=user_id,
            decision="ALLOW",
            reason="new_user_registered_as_pending",
            details={"email": email, "username": username},
            timestamp=now,
        )
        return user

    @staticmethod
    def update_last_seen(session: Session, user_id: str) -> None:
        """Touch last_seen_at timestamp on successful authenticated access."""
        user = session.get(UserModel, user_id)
        if user:
            user.last_seen_at = datetime.datetime.now(datetime.timezone.utc)
            session.add(user)

    @staticmethod
    def count_active_admins(session: Session) -> int:
        """Return the count of active administrators."""
        stmt = select(func.count(UserModel.user_id)).where(
            UserModel.status == "active", UserModel.role == "admin"
        )
        return session.execute(stmt).scalar() or 0

    @classmethod
    def update_user_access(
        cls,
        session: Session,
        *,
        admin_user_id: str,
        target_user_id: str,
        role: str | None = None,
        scopes: Mapping[str, Any] | None = None,
        status: str | None = None,
    ) -> UserModel:
        """Update a user's role, status, or scopes with strict administrative guardrails."""
        target = cls.get_user_by_id(session, target_user_id)
        if not target:
            raise KeyError(f"User {target_user_id} not found.")

        # Guardrail 1: No self-service privilege change (Section 5.4)
        if admin_user_id == target_user_id:
            raise AdminSecurityError("Administrators cannot modify their own role, scopes, or status.")

        previous_state = {
            "role": target.role,
            "status": target.status,
            "authz_version": target.authz_version,
            "scopes": {
                "allowed_folder_uids": list(target.scopes.allowed_folder_uids if target.scopes else []),
                "allowed_dashboard_uids": list(target.scopes.allowed_dashboard_uids if target.scopes else []),
                "denied_dashboard_uids": list(target.scopes.denied_dashboard_uids if target.scopes else []),
                "allowed_environments": list(target.scopes.allowed_environments if target.scopes else []),
            },
        }

        # Guardrail 2: Last-Administrator protection (Section 5.4)
        is_target_active_admin = (target.status == "active" and target.role == "admin")
        will_demote_admin = (role is not None and role != "admin")
        will_deactivate_admin = (status is not None and status != "active")

        if is_target_active_admin and (will_demote_admin or will_deactivate_admin):
            if cls.count_active_admins(session) <= 1:
                raise AdminSecurityError("Cannot remove or deactivate the last active Administrator.")

        # Apply updates
        modified = False
        if role is not None and role != target.role:
            target.role = role
            modified = True

        if status is not None and status != target.status:
            target.status = status
            modified = True

        if scopes is not None:
            if target.scopes is None:
                target.scopes = UserScopeModel(user_id=target.user_id)
            if "allowed_folder_uids" in scopes:
                target.scopes.allowed_folder_uids = list(scopes["allowed_folder_uids"])
                modified = True
            if "allowed_dashboard_uids" in scopes:
                target.scopes.allowed_dashboard_uids = list(scopes["allowed_dashboard_uids"])
                modified = True
            if "denied_dashboard_uids" in scopes:
                target.scopes.denied_dashboard_uids = list(scopes["denied_dashboard_uids"])
                modified = True
            if "allowed_environments" in scopes:
                target.scopes.allowed_environments = list(scopes["allowed_environments"])
                modified = True

        if modified:
            target.authz_version += 1
            session.add(target)
            session.flush()

            new_state = {
                "role": target.role,
                "status": target.status,
                "authz_version": target.authz_version,
                "scopes": {
                    "allowed_folder_uids": list(target.scopes.allowed_folder_uids if target.scopes else []),
                    "allowed_dashboard_uids": list(target.scopes.allowed_dashboard_uids if target.scopes else []),
                    "denied_dashboard_uids": list(target.scopes.denied_dashboard_uids if target.scopes else []),
                    "allowed_environments": list(target.scopes.allowed_environments if target.scopes else []),
                },
            }

            AuditStore.log_event(
                session,
                event_type="user_access_modified",
                actor_id=admin_user_id,
                target_type="user",
                target_id=target_user_id,
                decision="ALLOW",
                reason="admin_update",
                previous_state=previous_state,
                new_state=new_state,
            )

        return target

    @staticmethod
    def list_users(session: Session) -> list[UserModel]:
        """List all users with their scopes."""
        stmt = select(UserModel).options(joinedload(UserModel.scopes)).order_by(UserModel.created_at)
        return list(session.execute(stmt).scalars().unique().all())

