"""
app/auth/dependencies.py

FastAPI authentication and identity resolution dependencies for OIDC session
cookies and Bearer access tokens, backed by the PostgreSQL User and Role Store.

The validation sequence:
1. Extract Bearer token or signed session cookie (HTTP 401 if missing).
2. Validate token signature against Authentik JWKS or verify signed cookie.
3. Extract stable identity key (issuer, subject).
4. Query PostgreSQL User and Role Store:
   - If unknown: Auto-register user with status 'pending', no role, and empty scopes.
     Log a 'first_sign_in' audit record. Return HTTP 403 (pending activation).
   - If existing: Update last_seen_at.
   - If status is 'pending' or 'deactivated': Return HTTP 403 Forbidden.
   - If status is 'active': Build server-side Principal (role, authz_version, 4 scope lists).
5. Attach verified Principal to request.state.principal.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException, Request, status

from app.auth.oidc import (
    OidcClient,
    OidcConfigurationError,
    OidcProtocolError,
    OidcProviderConfig,
)
from app.auth.principal import Principal, set_current_principal
from app.auth.session import SessionError, SignedSession
from app.config import get_settings
from app.storage.database import get_db_session
from app.storage.models import UserModel
from app.storage.user_store import UserStore
from sqlalchemy import select

logger = logging.getLogger(__name__)

#: Attribute name used to publish the principal on the request context.
PRINCIPAL_STATE_KEY = "principal"

_UNAUTHENTICATED_HEADERS = {"WWW-Authenticate": 'Bearer realm="grafana-ai-agent"'}


def validate_oidc_settings(settings: Any) -> list[str]:
    """Return the list of problems in the OIDC configuration."""
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


def _extract_caller_credentials(
    request: Request, settings: Any
) -> tuple[str, str, dict[str, Any]]:
    """Extract and cryptographically verify (issuer, subject, profile) from Bearer token or Cookie."""
    auth_header = request.headers.get("Authorization") or ""
    bearer_token: str | None = None
    if auth_header.lower().startswith("bearer "):
        bearer_token = auth_header[7:].strip()

    if bearer_token:
        # Validate OAuth 2.0 Bearer JWT against Authentik JWKS
        try:
            with OidcClient(OidcProviderConfig.from_settings(settings)) as client:
                claims = client.verify_bearer_token(bearer_token)
                issuer = str(claims.get("iss") or client.metadata().get("issuer") or "")
                subject = str(claims.get("sub") or "")
                if not subject:
                    raise OidcProtocolError("Bearer token carries no subject.")
                return (
                    issuer,
                    subject,
                    {
                        "email": claims.get("email"),
                        "username": claims.get("preferred_username") or claims.get("username"),
                        "display_name": claims.get("name"),
                        "claims": claims,
                    },
                )
        except OidcConfigurationError as exc:
            logger.error("OIDC provider is unavailable: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="The authentication service is not configured correctly.",
            ) from exc
        except OidcProtocolError as exc:
            logger.debug("Rejected Bearer token: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Invalid bearer token: {exc}",
                headers=_UNAUTHENTICATED_HEADERS,
            ) from exc

    # Fallback to Web UI signed session cookie
    session = build_session(settings)
    raw_cookie = request.cookies.get(session.cookie_name)
    try:
        payload = session.decode(raw_cookie)
    except SessionError as exc:
        logger.debug("Rejected session cookie on %s: %s", request.url.path, exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required: the session is missing or has expired.",
            headers=_UNAUTHENTICATED_HEADERS,
        ) from exc

    issuer = str(payload.get("iss") or "")
    subject = str(payload.get("sub") or "")
    if not subject:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required: the session carries no identity.",
            headers=_UNAUTHENTICATED_HEADERS,
        )

    return (
        issuer,
        subject,
        {
            "email": payload.get("email"),
            "username": payload.get("username"),
            "display_name": payload.get("name"),
            "claims": {},
        },
    )


async def _resolve_principal_from_store(
    request: Request, *, allow_inactive: bool = False
) -> Principal:
    """Core identity verification and database lookup logic."""
    settings = get_settings()

    if not settings.authentik_enabled:
        principal = Principal.development_anonymous()
        setattr(request.state, PRINCIPAL_STATE_KEY, principal)
        set_current_principal(principal)
        return principal

    problems = validate_oidc_settings(settings)
    if problems:
        logger.error("OIDC configuration is invalid: %s", " ".join(problems))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The authentication service is not configured correctly.",
        )

    issuer, subject, profile = _extract_caller_credentials(request, settings)

    with get_db_session() as db:
        user = UserStore.get_user_by_identity(db, issuer, subject)
        if user is None:
            # If an existing pre-seeded record exists with placeholder subject, link it
            email = profile.get("email")
            username = profile.get("username")
            stmt = select(UserModel).where(
                (UserModel.email == email) | (UserModel.username == username)
            )
            candidate = db.execute(stmt).scalars().first()
            if candidate is not None and str(candidate.subject).startswith("ak_sub_"):
                candidate.issuer = issuer
                candidate.subject = subject
                user = candidate
                db.add(user)
                db.flush()
                logger.info("Linked pre-seeded record %s to identity (iss=%r, sub=%r)", user.user_id, issuer, subject)
            else:
                # First sign-in: Auto-register user as pending with empty scopes
                logger.info("Auto-registering new authenticated identity (iss=%r, sub=%r) as pending", issuer, subject)
                user = UserStore.create_pending_user(
                    db,
                    issuer=issuer,
                    subject=subject,
                    email=profile.get("email"),
                    username=profile.get("username"),
                    display_name=profile.get("display_name"),
                )
        else:
            UserStore.update_last_seen(db, user.user_id)

        principal = Principal.from_user_model(
            user,
            app=getattr(settings, "authentik_app_slug", None),
            claims=profile.get("claims"),
        )

    setattr(request.state, PRINCIPAL_STATE_KEY, principal)
    set_current_principal(principal)

    # Status check enforcement (Section 4.4 & Section 5.2)
    if not allow_inactive:
        if principal.status == "pending":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Account pending activation by an administrator.",
            )
        if principal.status == "deactivated":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Account deactivated.",
            )

    return principal


async def require_principal(request: Request) -> Principal:
    """Standard FastAPI dependency: requires an authenticated and active user."""
    return await _resolve_principal_from_store(request, allow_inactive=False)


async def resolve_principal_status(request: Request) -> Principal:
    """FastAPI dependency for status endpoints like /api/auth/whoami.
    
    Allows pending or deactivated users to read their own identity and status.
    """
    return await _resolve_principal_from_store(request, allow_inactive=True)


def current_principal(request: Request) -> Principal | None:
    """The principal published on request.state, if already resolved."""
    return getattr(request.state, PRINCIPAL_STATE_KEY, None)


__all__ = [
    "PRINCIPAL_STATE_KEY",
    "build_session",
    "current_principal",
    "require_principal",
    "resolve_principal_status",
    "validate_oidc_settings",
]
