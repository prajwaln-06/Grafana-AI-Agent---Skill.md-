"""
app/storage/models.py

SQLAlchemy ORM models for the User and Role Store, resource scopes,
persistent proposals, approvals, and the append-only audit log.
"""
from __future__ import annotations

import datetime
from typing import Any, List

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class UserModel(Base):
    """Stores internal application users and their current RBAC status."""
    __tablename__ = "users"

    user_id = Column(String(64), primary_key=True)
    issuer = Column(String(512), nullable=False)
    subject = Column(String(255), nullable=False)

    email = Column(String(255), nullable=True)
    username = Column(String(255), nullable=True)
    display_name = Column(String(255), nullable=True)

    status = Column(String(32), nullable=False, default="pending")  # pending, active, deactivated
    role = Column(String(32), nullable=True)                         # observer, maintainer, operator, admin
    authz_version = Column(Integer, nullable=False, default=1)

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )
    last_seen_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )

    scopes = relationship("UserScopeModel", uselist=False, back_populates="user", cascade="all, delete-orphan")
    proposals = relationship("ProposalModel", back_populates="proposer", cascade="all, delete-orphan")
    approvals = relationship("ApprovalModel", back_populates="approver", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("issuer", "subject", name="uq_users_issuer_subject"),
        Index("idx_users_issuer_subject", "issuer", "subject"),
    )


class UserScopeModel(Base):
    """The four selective resource scope lists for an application user."""
    __tablename__ = "user_scopes"

    user_id = Column(String(64), ForeignKey("users.user_id", ondelete="CASCADE"), primary_key=True)

    allowed_folder_uids = Column(JSON, nullable=False, default=list)
    allowed_dashboard_uids = Column(JSON, nullable=False, default=list)
    denied_dashboard_uids = Column(JSON, nullable=False, default=list)
    allowed_environments = Column(JSON, nullable=False, default=list)

    user = relationship("UserModel", back_populates="scopes")


class ProposalModel(Base):
    """Persistent proposal records replacing the legacy in-memory store."""
    __tablename__ = "proposals"

    proposal_id = Column(String(64), primary_key=True)
    version = Column(Integer, nullable=False, default=1)
    proposer_id = Column(String(64), ForeignKey("users.user_id"), nullable=False)

    operation = Column(String(64), nullable=False)
    impact_class = Column(String(64), nullable=False)
    target_resource = Column(JSON, nullable=False, default=dict)
    canonical_content = Column(JSON, nullable=False, default=dict)
    digest = Column(String(64), nullable=False)

    state = Column(String(32), nullable=False, default="pending_approval")
    requires_second_approver = Column(Boolean, nullable=False, default=False)
    policy_version = Column(String(32), nullable=False, default="1.0")

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )

    proposer = relationship("UserModel", back_populates="proposals")
    approvals = relationship("ApprovalModel", back_populates="proposal", cascade="all, delete-orphan")


class ApprovalModel(Base):
    """Approval records bound to an authenticated approver, proposal version, and digest."""
    __tablename__ = "approvals"

    approval_id = Column(String(64), primary_key=True)
    proposal_id = Column(String(64), ForeignKey("proposals.proposal_id", ondelete="CASCADE"), nullable=False)
    proposal_version = Column(Integer, nullable=False)
    digest = Column(String(64), nullable=False)
    approver_id = Column(String(64), ForeignKey("users.user_id"), nullable=False)

    decision = Column(String(32), nullable=False, default="approved")
    approver_authz_version = Column(Integer, nullable=False)

    approval_token = Column(String(128), unique=True, nullable=True)
    token_expires_at = Column(DateTime(timezone=True), nullable=True)
    token_used = Column(Boolean, nullable=False, default=False)
    token_used_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )

    proposal = relationship("ProposalModel", back_populates="approvals")
    approver = relationship("UserModel", back_populates="approvals")


class AuditLogModel(Base):
    """Append-only security and authorization audit log."""
    __tablename__ = "audit_logs"

    id = Column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    event_type = Column(String(64), nullable=False)

    actor_id = Column(String(64), nullable=True)
    actor_issuer = Column(String(512), nullable=True)
    actor_subject = Column(String(255), nullable=True)

    target_type = Column(String(64), nullable=True)
    target_id = Column(String(255), nullable=True)

    decision = Column(String(32), nullable=True)
    reason = Column(String(255), nullable=True)

    previous_state = Column(JSON, nullable=True)
    new_state = Column(JSON, nullable=True)
    details = Column(JSON, nullable=True)

    timestamp = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.datetime.now(datetime.timezone.utc),
    )

    __table_args__ = (
        Index("idx_audit_logs_timestamp", "timestamp"),
        Index("idx_audit_logs_actor_id", "actor_id"),
        Index("idx_audit_logs_event_type", "event_type"),
    )

