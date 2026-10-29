"""
app/api/routes_auth.py

The authentication endpoints for the browser UI.

Authentication is OpenID Connect: the browser is sent to Authentik, the user
authenticates there, and Authentik sends the browser back to this service
with an authorization code. This service validates the returned ID token and
establishes an application session (a signed, HttpOnly cookie).

The UI therefore needs five things from the backend:

``GET /api/auth/config``
    Public. What the browser needs in order to render the login screen: where
    sign-in starts, whether sign-up is available and where Authentik's
    enrollment and password-change flows live. It contains no secret, and it
    has to be reachable *before* anybody is signed in.

``GET /api/auth/login``
    Starts the OIDC Authorization Code flow: it redirects the browser to
    Authentik.

``GET /api/auth/callback``
    The redirect URI registered in the Authentik provider. It validates the
    response (state, code, ID token) and establishes the session.

``GET /api/auth/whoami``
    Protected. Returns the verified identity of the caller -- the stable
    ``(issuer, subject)`` pair plus informational profile fields. No group,
    role or entitlement information is exposed, and none is used.

``POST /api/auth/logout``
    Ends the *application* session (the cookie). It is deliberately not a
    global single-sign-on logout: Authentik's own session is only ended when
    the provider's invalidation flow is configured to do so.

This module establishes identity. It assigns no privileges.
"""
from __future__ import annotations

import hmac
import logging
import secrets
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse

from app.auth.dependencies import (
    build_session,
    current_principal,
    require_principal,
    validate_oidc_settings,
)
from app.auth.oidc import (
    OidcClient,
    OidcConfigurationError,
    OidcProtocolError,
    OidcProviderConfig,
)
from app.auth.principal import Principal
from app.auth.session import SessionError, SignedSession
from app.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Authentication"])

#: The endpoints of this module. They are paths on the *application* origin
#: (http://localhost:5173 in the local development setup).
LOGIN_PATH = "/api/auth/login"
CALLBACK_PATH = "/api/auth/callback"
LOGOUT_PATH = "/api/auth/logout"
WHOAMI_PATH = "/api/auth/whoami"
CONFIG_PATH = "/api/auth/config"

#: How long the browser may take to come back from Authentik. The login
#: transaction (state, nonce, PKCE verifier) is only valid for that long.
LOGIN_TRANSACTION_SECONDS = 600


def _flow_url(base_url: str | None, flow_slug: str | None) -> str | None:
    """The URL of an Authentik flow on the Authentik server, or ``None``.

    Authentik serves every flow through the same interface -- ``/if/flow/<slug>/`` --
    so a flow is addressed by its slug. When no base URL or no slug is
    configured, there is no URL to build and the caller gets ``None``: the
    UI then hides the action instead of sending the user to a page that
    does not exist.
    """
    base = (base_url or "").strip().rstrip("/")
    slug = (flow_slug or "").strip().strip("/")
    if not base or not slug:
        return None
    return f"{base}/if/flow/{slug}/"


def enrollment_flow_url(settings: Any) -> str | None:
    """The URL of Authentik's enrollment (sign-up) flow, or ``None``.

    Sign-up is Authentik's job, not ours: the browser is sent to an
    enrollment flow (``designation: enrollment``) on the Authentik server.
    There is no password API in this application and none may be added.

    An explicit ``AUTHENTIK_SIGNUP_URL`` wins; otherwise the URL is built
    from the Authentik base URL and the enrollment flow slug.
    """
    explicit = (getattr(settings, "authentik_signup_url", None) or "").strip()
    if explicit:
        return explicit

    return _flow_url(
        getattr(settings, "authentik_base_url", None),
        getattr(settings, "authentik_enrollment_flow", None),
    )


def password_change_url(settings: Any) -> str | None:
    """The URL of Authentik's password-change flow, or ``None``.

    The password itself is entered on the Authentik server; this
    application never collects or stores it.
    """
    explicit = (getattr(settings, "authentik_password_change_url", None) or "").strip()
    if explicit:
        return explicit

    return _flow_url(
        getattr(settings, "authentik_base_url", None),
        getattr(settings, "authentik_password_change_flow", None),
    )


def _login_transaction(settings: Any) -> SignedSession | None:
    """The short-lived cookie that carries the login transaction.

    It holds the ``state``, the ``nonce`` and the PKCE verifier between the
    login request and the callback. It is a separate cookie from the
    application session, so a failed login attempt never disturbs a session
    that already exists.

    Returns ``None`` when no session secret is configured -- there is then no
    cookie to write, and no cookie to clear either.
    """
    name = getattr(settings, "authentik_session_cookie_name", "grafana_ai_session")
    try:
        return SignedSession(
            secret=(getattr(settings, "authentik_session_secret", None) or "").strip(),
            max_age_seconds=LOGIN_TRANSACTION_SECONDS,
            cookie_name=f"{name}_txn",
            secure=bool(getattr(settings, "authentik_session_cookie_secure", False)),
            samesite=str(getattr(settings, "authentik_session_cookie_samesite", "lax") or "lax"),
        )
    except SessionError:
        return None


def _session(settings: Any) -> SignedSession | None:
    """The application session, or ``None`` when no secret is configured."""
    try:
        return build_session(settings)
    except SessionError:
        return None


def _safe_next(value: str | None) -> str:
    """A path on this application to return to after the login.

    Only same-origin paths are accepted: an absolute URL would turn the
    callback into an open redirect.
    """
    candidate = (value or "").strip()
    if not candidate.startswith("/") or candidate.startswith("//"):
        return "/"
    return candidate


def _error_redirect(settings: Any, request: Request, code: str, detail: str = "") -> RedirectResponse:
    """Send the browser back to the application with an error marker.

    The login screen reads the marker and explains what went wrong. The
    reason is kept in the query string as a short, non-sensitive code.
    """
    transaction = _login_transaction(settings)
    target = f"/?{urlencode({'auth_error': code})}"
    response = RedirectResponse(target, status_code=status.HTTP_302_FOUND)
    # The login transaction is worthless once it has failed; the identity
    # session (if there is one) is left alone.
    if transaction is not None:
        transaction.clear_cookie(response)
    if detail:
        logger.warning("Authentication failed (%s): %s", code, detail)
    return response


def _redirect_uri(settings: Any, request: Request) -> str:
    """The redirect URI used in the authorization request.

    The configured value always wins: it has to match the URI registered in
    the Authentik provider character for character. Only when nothing is
    configured is it derived from the incoming request.
    """
    configured = (getattr(settings, "authentik_oidc_redirect_uri", None) or "").strip()
    if configured:
        return configured
    return str(request.url_for("auth_callback"))


@router.get(CONFIG_PATH)
async def auth_config(request: Request) -> dict[str, Any]:
    """Public configuration for the login screen and the profile menu.

    Deliberately unauthenticated: the login screen has to render for a
    visitor who has no session yet. Only URLs and flags are exposed here --
    never the client secret, never the session secret, never a token.
    """
    settings = get_settings()
    signup_url = enrollment_flow_url(settings)
    return {
        "providerName": "Authentik",
        "authenticationEnabled": bool(settings.authentik_enabled),
        "loginPath": LOGIN_PATH,
        "callbackPath": CALLBACK_PATH,
        "logoutPath": LOGOUT_PATH,
        "signupEnabled": bool(signup_url),
        "signupUrl": signup_url,
        "passwordChangeUrl": password_change_url(settings),
        "sessionMaxAgeSeconds": int(
            getattr(settings, "authentik_session_max_age_seconds", 3600) or 3600
        ),
    }


@router.get(LOGIN_PATH)
async def login(request: Request, next: str | None = None) -> RedirectResponse:
    """Start the OIDC Authorization Code flow.

    The browser is redirected to Authentik's authorization endpoint with a
    fresh ``state``, a fresh ``nonce`` and a PKCE challenge. The values that
    have to survive the round trip are kept in a short-lived, signed cookie.
    """
    settings = get_settings()
    if not settings.authentik_enabled:
        logger.warning("A login was attempted while authentication is disabled.")
        return _error_redirect(settings, request, "authentication_disabled")

    problems = validate_oidc_settings(settings)
    if problems:
        logger.error("OIDC configuration is invalid: %s", " ".join(problems))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The authentication service is not configured correctly.",
        )

    transaction = _login_transaction(settings)
    if transaction is None:
        # Unreachable while the configuration is valid (the session secret is
        # part of it), but a login without a transaction cookie could never
        # be completed -- so refuse it instead of starting a broken flow.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The authentication service is not configured correctly.",
        )
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(64)

    try:
        with OidcClient(OidcProviderConfig.from_settings(settings)) as client:
            authorization_url = client.authorization_url(
                state=state,
                nonce=nonce,
                code_verifier=code_verifier,
                redirect_uri=_redirect_uri(settings, request),
            )
    except OidcConfigurationError as exc:
        logger.error("The OIDC provider is not usable: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The authentication service is not configured correctly.",
        ) from exc

    response = RedirectResponse(authorization_url, status_code=status.HTTP_302_FOUND)
    transaction.set_cookie(
        response,
        {
            "state": state,
            "nonce": nonce,
            "verifier": code_verifier,
            "next": _safe_next(next),
        },
    )
    return response


@router.get(CALLBACK_PATH, name="auth_callback")
async def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> RedirectResponse:
    """The redirect URI: validate the provider's answer and sign the user in.

    The checks, in order: the provider did not report an error; the
    ``state`` matches the one issued for this login attempt; the
    authorization code is exchanged for tokens; the returned ID token's
    signature, issuer, audience, expiry and nonce all verify. Only then is
    the session established.
    """
    settings = get_settings()
    if not settings.authentik_enabled:
        return _error_redirect(settings, request, "authentication_disabled")

    if error:
        return _error_redirect(
            settings,
            request,
            "provider_error",
            detail=f"{error}: {error_description or ''}".strip(),
        )

    transaction = _login_transaction(settings)
    try:
        if transaction is None:
            raise SessionError("No session secret is configured.")
        payload = transaction.decode(request.cookies.get(transaction.cookie_name))
    except SessionError as exc:
        return _error_redirect(settings, request, "login_state_missing", detail=str(exc))

    expected_state = str(payload.get("state") or "")
    if not state or not expected_state or not hmac.compare_digest(state, expected_state):
        return _error_redirect(settings, request, "state_mismatch")

    nonce = str(payload.get("nonce") or "")
    code_verifier = str(payload.get("verifier") or "") or None
    if not code:
        return _error_redirect(settings, request, "missing_code")
    if not nonce:
        return _error_redirect(settings, request, "missing_nonce")

    try:
        with OidcClient(OidcProviderConfig.from_settings(settings)) as client:
            tokens = client.exchange_code(
                code=code,
                code_verifier=code_verifier,
                redirect_uri=_redirect_uri(settings, request),
            )

            id_token = tokens.get("id_token")
            if not id_token:
                return _error_redirect(settings, request, "missing_id_token")

            try:
                claims = client.verify_id_token(str(id_token), nonce=nonce)
            except OidcConfigurationError as exc:
                logger.error("The OIDC provider is not usable: %s", exc)
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="The authentication service is not configured correctly.",
                ) from exc
            except OidcProtocolError as exc:
                return _error_redirect(settings, request, "id_token_invalid", detail=str(exc))

            issuer = str(client.metadata().get("issuer") or "")
    except OidcConfigurationError as exc:
        logger.error("The OIDC provider is not usable: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The authentication service is not configured correctly.",
        ) from exc
    except OidcProtocolError as exc:
        return _error_redirect(settings, request, "token_exchange_failed", detail=str(exc))

    principal = Principal.from_oidc_claims(
        claims,
        issuer=issuer,
        app=(getattr(settings, "authentik_app_slug", None) or None),
    )
    if not principal.subject:
        return _error_redirect(settings, request, "missing_subject")

    session = build_session(settings)
    response = RedirectResponse(
        _safe_next(payload.get("next")), status_code=status.HTTP_302_FOUND
    )
    session.set_cookie(response, principal.to_session_payload())
    if transaction is not None:
        transaction.clear_cookie(response)
    logger.info("Established an application session for subject %s.", principal.subject)
    return response


@router.get(WHOAMI_PATH)
async def whoami(
    request: Request, principal: Principal = Depends(require_principal)
) -> dict[str, Any]:
    """The verified identity of the caller."""
    return principal.to_public_dict()


@router.post(LOGOUT_PATH)
async def logout(request: Request) -> dict[str, Any]:
    """End the application session.

    This clears the application's own session cookie. It is **not** a global
    single-sign-on logout: the user stays signed in to Authentik itself --
    and to every other application that uses it -- unless the Authentik
    provider's invalidation flow is configured to end that session too.
    """
    settings = get_settings()
    session = _session(settings)
    transaction = _login_transaction(settings)
    response = JSONResponse(
        content={
            "status": "ok",
            "scope": "application",
            "providerName": "Authentik",
            "globalSsoLogout": False,
        }
    )
    if session is not None:
        session.clear_cookie(response)
    if transaction is not None:
        transaction.clear_cookie(response)
    return response


__all__ = [
    "CALLBACK_PATH",
    "CONFIG_PATH",
    "LOGIN_PATH",
    "LOGOUT_PATH",
    "WHOAMI_PATH",
    "current_principal",
    "enrollment_flow_url",
    "password_change_url",
    "router",
]
