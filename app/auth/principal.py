"""
app/auth/principal.py

The authenticated and authorized caller identity.

Authentik verifies *who* the user is via the stable identity key (issuer, subject).
The PostgreSQL User and Role Store provides the application-level authorization data:
internal user_id, current role, account status, authz_version, and the four resource
scope lists.
"""
from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from typing import Any, Mapping

CURRENT_PRINCIPAL: contextvars.ContextVar[Principal | None] = contextvars.ContextVar(
    "current_principal", default=None
)


def get_current_principal() -> Principal | None:
    """Return the principal bound to the current context/request, if any."""
    return CURRENT_PRINCIPAL.get()


def set_current_principal(principal: Principal | None) -> contextvars.Token:
    """Bind a principal to the current context/request."""
    return CURRENT_PRINCIPAL.set(principal)


@dataclass(frozen=True)
class Principal:
    """A verified identity resolved against the application User and Role Store."""

    issuer: str
    subject: str
    user_id: str = ""
    authenticated: bool = True
    status: str = "pending"          # pending, active, deactivated
    role: str | None = None          # observer, maintainer, operator, admin
    authz_version: int = 1

    # The four resource scope lists (Section 6.3 & Section 8.2)
    allowed_folder_uids: frozenset[str] = field(default_factory=frozenset)
    allowed_dashboard_uids: frozenset[str] = field(default_factory=frozenset)
    denied_dashboard_uids: frozenset[str] = field(default_factory=frozenset)
    allowed_environments: frozenset[str] = field(default_factory=frozenset)

    username: str | None = None
    email: str | None = None
    name: str | None = None
    app: str | None = None
    claims: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def identity_key(self) -> tuple[str, str]:
        """The stable (issuer, subject) identity key."""
        return (self.issuer, self.subject)

    @property
    def is_active(self) -> bool:
        return self.authenticated and self.status == "active"

    @property
    def display_name(self) -> str | None:
        return self.name

    @classmethod
    def development_anonymous(cls) -> "Principal":
        """Principal used when authentication is explicitly disabled (local dev only)."""
        return cls(
            user_id="usr_dev_anonymous",
            issuer="",
            subject="anonymous",
            authenticated=False,
            status="active",
            role="admin",  # Unrestricted in development-only mode
            allowed_folder_uids=frozenset({"*"}),
            allowed_environments=frozenset({"staging", "production"}),
            username="developer",
            name="Local Developer",
        )

    @classmethod
    def from_oidc_claims(
        cls,
        claims: Mapping[str, Any],
        *,
        issuer: str,
        app: str | None = None,
    ) -> "Principal":
        """Build a principal from the claims of a validated ID token.

        Only the identity and profile fields are read.
        Group, role and entitlement claims are ignored on purpose.
        """

        def _text(name: str) -> str | None:
            value = claims.get(name)
            if value is None:
                return None
            value = str(value).strip()
            return value or None

        return cls(
            issuer=issuer,
            subject=_text("sub") or "",
            authenticated=True,
            username=_text("preferred_username") or _text("nickname"),
            email=_text("email"),
            name=_text("name") or _text("given_name"),
            app=app,
            claims={},
        )

    @classmethod
    def from_session_payload(cls, payload: Mapping[str, Any]) -> "Principal":
        """Rebuild a principal from the application session."""
        app = payload.get("app")
        return cls(
            issuer=str(payload.get("iss") or ""),
            subject=str(payload.get("sub") or ""),
            user_id=str(payload.get("uid") or ""),
            authenticated=True,
            username=payload.get("username"),
            email=payload.get("email"),
            name=payload.get("name"),
            app=app if isinstance(app, str) else None,
            claims={},
        )

    @classmethod
    def from_user_model(
        cls,
        user: Any,
        *,
        app: str | None = None,
        claims: Mapping[str, Any] | None = None,
    ) -> "Principal":
        """Construct a Principal directly from a verified database UserModel record."""
        scopes = getattr(user, "scopes", None)
        return cls(
            user_id=str(getattr(user, "user_id", "")),
            issuer=str(getattr(user, "issuer", "")),
            subject=str(getattr(user, "subject", "")),
            authenticated=True,
            status=str(getattr(user, "status", "pending")),
            role=getattr(user, "role", None),
            authz_version=int(getattr(user, "authz_version", 1)),
            allowed_folder_uids=frozenset(getattr(scopes, "allowed_folder_uids", []) or []),
            allowed_dashboard_uids=frozenset(getattr(scopes, "allowed_dashboard_uids", []) or []),
            denied_dashboard_uids=frozenset(getattr(scopes, "denied_dashboard_uids", []) or []),
            allowed_environments=frozenset(getattr(scopes, "allowed_environments", []) or []),
            email=getattr(user, "email", None),
            username=getattr(user, "username", None),
            name=getattr(user, "display_name", None),
            app=app,
            claims=claims or {},
        )

    def to_public_dict(self) -> dict[str, Any]:
        """A JSON-safe view for the UI and whoami endpoint."""
        return {
            "userId": self.user_id,
            "issuer": self.issuer,
            "subject": self.subject,
            "authenticated": self.authenticated,
            "status": self.status,
            "role": self.role,
            "authzVersion": self.authz_version,
            "scopes": {
                "allowedFolderUids": sorted(list(self.allowed_folder_uids)),
                "allowedDashboardUids": sorted(list(self.allowed_dashboard_uids)),
                "deniedDashboardUids": sorted(list(self.denied_dashboard_uids)),
                "allowedEnvironments": sorted(list(self.allowed_environments)),
            },
            "username": self.username,
            "email": self.email,
            "name": self.name,
            "app": self.app,
        }

    def to_session_payload(self) -> dict[str, Any]:
        """Minimum identity information serialized into the signed session cookie."""
        return {
            "iss": self.issuer,
            "sub": self.subject,
            "uid": self.user_id,
            "email": self.email,
            "name": self.name,
            "username": self.username,
            "app": self.app,
        }


__all__ = [
    "CURRENT_PRINCIPAL",
    "Principal",
    "get_current_principal",
    "set_current_principal",
]
