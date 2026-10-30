"""
app.storage

PostgreSQL-backed User, Role, Scope, Proposal, and Audit Store for application-level RBAC.
"""
from app.storage.database import get_db, init_db, get_engine
from app.storage.models import (
    Base,
    UserModel,
    UserScopeModel,
    ProposalModel,
    ApprovalModel,
    AuditLogModel,
)
from app.storage.user_store import UserStore
from app.storage.audit_store import AuditStore

__all__ = [
    "get_db",
    "init_db",
    "get_engine",
    "Base",
    "UserModel",
    "UserScopeModel",
    "ProposalModel",
    "ApprovalModel",
    "AuditLogModel",
    "UserStore",
    "AuditStore",
]

