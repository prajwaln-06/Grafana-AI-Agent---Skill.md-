"""
app.auth

Authentik-backed authentication for the Grafana AI Agent API.

Authentication is OpenID Connect: this service is the relying party of the
Authentik provider. It sends the browser to Authentik, validates the ID
token that comes back and keeps the resulting session in a signed, HttpOnly
cookie.

Authentik establishes **identity only**. This package deliberately contains
no role, permission or scope logic: the verified identity is published on the
request context for a later authorization layer to consume.
"""
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

__all__ = [
    "OidcClient",
    "OidcConfigurationError",
    "OidcProtocolError",
    "OidcProviderConfig",
    "Principal",
    "SessionError",
    "SignedSession",
    "build_session",
    "current_principal",
    "require_principal",
    "validate_oidc_settings",
]
