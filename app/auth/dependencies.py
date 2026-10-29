"""
app/auth/dependencies.py

The reusable FastAPI authentication dependency for the OIDC session.

How the pieces fit together:

1. The browser asks the application to log in (``GET /api/auth/login``).
2. The application sends the browser to Authentik, which authenticates the
   user and sends it back to the callback URL.
3. The application validates the returned ID token (signature, issuer,
   audience, expiry, nonce) and, on success, establishes an application
   session: a signed, HttpOnly cookie carrying the user's identity.
4. Every protected endpoint then reads that cookie.

Nothing else establishes an identity. In particular, no identity header sent
by the browser is ever believed, and no token is decoded without being
verified first.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException, Request, status

from app.auth.principal import Principal
from app.auth.session import SessionError, SignedSession
from app.config import get_settings

logger = logging.getLogger(__name__)

#: Attribute name used to publish the principal on the request context.
PRINCIPAL_STATE_KEY = "principal"

_UNAUTHENTICATED_HEADERS = {"WWW-Authenticate": 'Bearer realm="grafana-ai-agent"'}


def validate_oidc_settings(settings: Any) -> list[str]:
    """Return the list of problems in the OIDC configuration.

    An empty list means the configuration is usable. When authentication is
    enabled, a non-empty list means the service must refuse to serve
    protected endpoints -- failing closed, never falling back to anonymous
    access.
    """
    problems: list[str] = []
    if not (getattr(settings, "authentik_oidc_discovery_url", None) or "").strip():
        problems.append("AUTHENTIK_OIDC_DISCOVERY_URL is not set.")
    if not (getattr(settings, "authentik_oidc_client_id", None) or "").strip():
        problems.append("AUTHENTIK_OIDC_CLIENT_ID is not set.")
    if not (getattr(settings, "authentik_oidc_client_secret", None) or "").strip():
        problems.append("AUTHENTIK_OIDC_CLIENT_SECRET is not set.")
    if not (getattr(settings, "authentik_oidc_redirect_uri", None) or "").strip():
        problems.append("AUTHENTIK_OIDC_REDIRECT_URI is not set.")
    if not (getattr(settings, "authentik_session_secret", None) or "").strip():
        problems.append("AUTHENTIK_SESSION_SECRET is not set.")
    return problems


def build_session(settings: Any) -> SignedSession:
    """The signed-cookie session store for the current configuration."""
    return SignedSession(
        secret=(getattr(settings, "authentik_session_secret", None) or "").strip(),
        max_age_seconds=int(getattr(settings, "authentik_session_max_age_seconds", 3600) or 3600),
        cookie_name=getattr(settings, "authentik_session_cookie_name", "grafana_ai_session"),
        secure=bool(getattr(settings, "authentik_session_cookie_secure", False)),
        samesite=str(getattr(settings, "authentik_session_cookie_samesite", "lax") or "lax"),
    )


async def require_principal(request: Request) -> Principal:
    """FastAPI dependency: the authenticated principal for this request.

    Attach it to a route (or a whole router) to require an authenticated
    identity::

        router = APIRouter(dependencies=[Depends(require_principal)])
    """
    settings = get_settings()

    if not settings.authentik_enabled:
        # Explicit development mode: authentication is switched off. The
        # principal is deliberately marked as *not* authenticated so that a
        # later authorization layer cannot mistake it for a real user.
        principal = Principal.development_anonymous()
        setattr(request.state, PRINCIPAL_STATE_KEY, principal)
        return principal

    problems = validate_oidc_settings(settings)
    if problems:
        logger.error("OIDC configuration is invalid: %s", " ".join(problems))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The authentication service is not configured correctly.",
        )

    session = build_session(settings)
    try:
        payload = session.decode(request.cookies.get(session.cookie_name))
    except SessionError as exc:
        logger.debug("Rejected request to %s: %s", request.url.path, exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required: the session is missing or has expired.",
            headers=_UNAUTHENTICATED_HEADERS,
        ) from exc

    principal = Principal.from_session_payload(payload)
    if not principal.subject:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required: the session carries no identity.",
            headers=_UNAUTHENTICATED_HEADERS,
        )

    setattr(request.state, PRINCIPAL_STATE_KEY, principal)
    return principal


def current_principal(request: Request) -> Principal | None:
    """The principal published on the request context, if any.

    Provided for the later authorization layer: it reads the identity that
    authentication already established and never re-parses anything.
    """
    return getattr(request.state, PRINCIPAL_STATE_KEY, None)


__all__ = [
    "PRINCIPAL_STATE_KEY",
    "build_session",
    "current_principal",
    "require_principal",
    "validate_oidc_settings",
]
