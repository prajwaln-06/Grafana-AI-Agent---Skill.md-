"""
app/auth/principal.py

The authenticated identity, and nothing else.

Authentik verifies *who* the user is; this class carries that answer. The
identity key is the pair ``(issuer, subject)``: ``subject`` is the ``sub``
claim of the validated ID token -- a stable, opaque identifier that does not
change when the user renames themselves or changes their email address.

Every other piece of information Authentik can supply -- groups,
entitlements, roles -- is deliberately **not** read here. Authentik verifies
identity; the application decides permissions, and that decision belongs to a
later authorization layer built on the stable identity exposed by this
class.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class Principal:
    """A verified identity.

    Attributes:
        issuer: The OIDC issuer the identity was established by.
        subject: The stable user identifier (the ID token's ``sub`` claim).
            Together with ``issuer`` this is the application's identity key.
        authenticated: ``False`` only in the explicit development mode where
            authentication is disabled. A later authorization layer must treat
            such a principal as "no identity", never as a privileged one.
    """

    issuer: str
    subject: str
    authenticated: bool = True
    username: str | None = None
    email: str | None = None
    name: str | None = None
    app: str | None = None
    claims: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def identity_key(self) -> tuple[str, str]:
        """The stable ``(issuer, subject)`` identity key."""
        return (self.issuer, self.subject)

    @classmethod
    def development_anonymous(cls) -> "Principal":
        """Principal used when authentication is explicitly disabled.

        This is a development convenience only. It carries no identity and
        must never be treated as an authenticated user.
        """
        return cls(issuer="", subject="anonymous", authenticated=False)

    @classmethod
    def from_oidc_claims(
        cls,
        claims: Mapping[str, Any],
        *,
        issuer: str,
        app: str | None = None,
    ) -> "Principal":
        """Build a principal from the claims of a validated ID token.

        Only the identity and the profile fields the UI displays are read.
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

    def to_public_dict(self) -> dict[str, Any]:
        """A JSON-safe view for the browser.

        Only identity and profile information is exposed. No group, role,
        entitlement or permission information is copied, even when the
        provider supplied one.
        """
        return {
            "issuer": self.issuer,
            "subject": self.subject,
            "authenticated": self.authenticated,
            "username": self.username,
            "email": self.email,
            "name": self.name,
            "app": self.app,
        }

    def to_session_payload(self) -> dict[str, Any]:
        """The minimum identity information kept in the application session.

        No token of any kind is stored: the tokens were used once, to verify
        the identity, and are then discarded.
        """
        return {
            "iss": self.issuer,
            "sub": self.subject,
            "email": self.email,
            "name": self.name,
            "username": self.username,
            "app": self.app,
        }

    @classmethod
    def from_session_payload(cls, payload: Mapping[str, Any]) -> "Principal":
        """Rebuild a principal from the application session."""
        app = payload.get("app")
        return cls(
            issuer=str(payload.get("iss") or ""),
            subject=str(payload.get("sub") or ""),
            authenticated=True,
            username=payload.get("username"),
            email=payload.get("email"),
            name=payload.get("name"),
            app=app if isinstance(app, str) else None,
            claims={},
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"Principal(issuer={self.issuer!r}, subject={self.subject!r}, "
            f"authenticated={self.authenticated})"
        )


__all__ = ["Principal"]
