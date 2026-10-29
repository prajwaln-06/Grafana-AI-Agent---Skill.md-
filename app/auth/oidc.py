"""
app/auth/oidc.py

The OpenID Connect client: the Authorization Code flow against Authentik.

This module is the *relying party*. It

  1. reads the provider's discovery document,
  2. builds the authorization request (state, nonce, PKCE),
  3. exchanges the authorization code for tokens, and
  4. validates the ID token: signature, issuer, audience, expiry and nonce.

The heavy lifting is done by established libraries, never by hand-rolled JWT
decoding:

  * **authlib** builds the authorization URL (including the S256 PKCE
    challenge) and performs the token request against the provider's token
    endpoint, including the client authentication.
  * **joserfc** (authlib's own successor for JOSE) verifies the ID token's
    signature against the provider's JWKS and validates the registered
    claims.

The tokens themselves never leave this module: the caller is handed the
*validated claims* and nothing else. No access token, refresh token or ID
token is ever sent to the browser, and none of them is stored in the
application session.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping

import httpx
from authlib.integrations.httpx_client import OAuth2Client
from joserfc import jwt as jose_jwt
from joserfc import jwk as jose_jwk
from joserfc.jwt import JWTClaimsRegistry

logger = logging.getLogger(__name__)


class OidcError(Exception):
    """Base class for every OIDC failure."""


class OidcConfigurationError(OidcError):
    """The provider or the client is not configured (or not reachable).

    This is an operator problem, not a user problem: it is reported as a
    ``503`` and never as an authentication failure.
    """


class OidcProtocolError(OidcError):
    """The provider's answer was not acceptable.

    A mismatching ``state``, a wrong ``nonce``, a rejected token, a
    signature that does not verify: all of these mean the login attempt did
    not establish an identity.
    """


#: Signature algorithms this client is willing to verify an ID token with.
#: Symmetric (``HS*``) and ``none`` are deliberately absent: the provider
#: signs with its own private key and publishes the public key in its JWKS.
ALLOWED_ID_TOKEN_ALGORITHMS = frozenset(
    {
        "RS256", "RS384", "RS512",
        "PS256", "PS384", "PS512",
        "ES256", "ES384", "ES512",
    }
)

#: How long a fetched discovery document / JWKS is reused.
METADATA_CACHE_SECONDS = 300


@dataclass(frozen=True)
class OidcProviderConfig:
    """Everything the client needs to talk to one OIDC provider."""

    discovery_url: str
    client_id: str
    client_secret: str
    redirect_uri: str
    scope: str = "openid profile email"
    pkce: bool = True
    timeout_seconds: float = 10.0

    @classmethod
    def from_settings(cls, settings: Any) -> "OidcProviderConfig":
        return cls(
            discovery_url=(getattr(settings, "authentik_oidc_discovery_url", None) or "").strip(),
            client_id=(getattr(settings, "authentik_oidc_client_id", None) or "").strip(),
            client_secret=(getattr(settings, "authentik_oidc_client_secret", None) or "").strip(),
            redirect_uri=(getattr(settings, "authentik_oidc_redirect_uri", None) or "").strip(),
            scope=(getattr(settings, "authentik_oidc_scope", None) or "openid profile email").strip(),
            pkce=bool(getattr(settings, "authentik_oidc_pkce", True)),
            timeout_seconds=float(
                getattr(settings, "authentik_oidc_http_timeout_seconds", 10.0) or 10.0
            ),
        )


# --------------------------------------------------------------------------
# Small TTL caches for the discovery document and the JWKS
# --------------------------------------------------------------------------

_cache_lock = threading.Lock()
_metadata_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_jwks_cache: dict[str, tuple[float, jose_jwk.KeySet]] = {}


def _cached(cache: dict, key: str, now: float):
    entry = cache.get(key)
    if entry and (now - entry[0]) < METADATA_CACHE_SECONDS:
        return entry[1]
    return None


def _store(cache: dict, key: str, now: float, value: Any) -> None:
    cache[key] = (now, value)


class OidcClient:
    """A minimal OIDC relying party for one provider."""

    def __init__(
        self,
        config: OidcProviderConfig,
        *,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.config = config
        self._http = http_client
        self._owned_http = http_client is None

    # -- HTTP -------------------------------------------------------------

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=self.config.timeout_seconds, follow_redirects=False)
        return self._http

    def close(self) -> None:
        if self._http is not None and self._owned_http:
            self._http.close()
            self._http = None

    def __enter__(self) -> "OidcClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # -- discovery --------------------------------------------------------

    def metadata(self) -> dict[str, Any]:
        """The provider's discovery document (cached for a few minutes)."""
        if not self.config.discovery_url:
            raise OidcConfigurationError(
                "AUTHENTIK_OIDC_DISCOVERY_URL is not configured."
            )

        now = time.time()
        with _cache_lock:
            cached = _cached(_metadata_cache, self.config.discovery_url, now)
        if cached is not None:
            return cached

        try:
            response = self.http.get(self.config.discovery_url)
            response.raise_for_status()
            document = response.json()
        except httpx.HTTPError as exc:
            raise OidcConfigurationError(
                f"The OIDC discovery document could not be fetched from "
                f"{self.config.discovery_url}: {exc}"
            ) from exc
        except ValueError as exc:
            raise OidcConfigurationError(
                f"The OIDC discovery document at {self.config.discovery_url} is not valid JSON."
            ) from exc

        if not isinstance(document, dict):
            raise OidcConfigurationError(
                f"The OIDC discovery document at {self.config.discovery_url} is not an object."
            )

        missing = [
            key
            for key in ("issuer", "authorization_endpoint", "token_endpoint", "jwks_uri")
            if not document.get(key)
        ]
        if missing:
            raise OidcConfigurationError(
                "The OIDC discovery document is missing: " + ", ".join(missing) + "."
            )

        with _cache_lock:
            _store(_metadata_cache, self.config.discovery_url, now, document)
        return document

    def jwks(self) -> jose_jwk.KeySet:
        """The provider's JSON Web Key Set (cached for a few minutes)."""
        jwks_uri = self.metadata()["jwks_uri"]
        now = time.time()
        with _cache_lock:
            cached = _cached(_jwks_cache, jwks_uri, now)
        if cached is not None:
            return cached

        try:
            response = self.http.get(jwks_uri)
            response.raise_for_status()
            document = response.json()
        except httpx.HTTPError as exc:
            raise OidcConfigurationError(
                f"The provider's JWKS could not be fetched from {jwks_uri}: {exc}"
            ) from exc
        except ValueError as exc:
            raise OidcConfigurationError(
                f"The provider's JWKS at {jwks_uri} is not valid JSON."
            ) from exc

        try:
            key_set = jose_jwk.KeySet.import_key_set(document)
        except Exception as exc:  # noqa: BLE001 - any import problem is a bad JWKS
            raise OidcConfigurationError(
                f"The provider's JWKS at {jwks_uri} could not be read: {exc}"
            ) from exc

        with _cache_lock:
            _store(_jwks_cache, jwks_uri, now, key_set)
        return key_set

    # -- the flow ---------------------------------------------------------

    def _oauth_client(self) -> OAuth2Client:
        metadata = self.metadata()
        return OAuth2Client(
            client_id=self.config.client_id,
            client_secret=self.config.client_secret,
            redirect_uri=self.config.redirect_uri,
            scope=self.config.scope,
            code_challenge_method="S256" if self.config.pkce else None,
            server_metadata=metadata,
        )

    def authorization_url(
        self,
        *,
        state: str,
        nonce: str,
        code_verifier: str | None = None,
        redirect_uri: str | None = None,
    ) -> str:
        """The URL the browser is sent to in order to authenticate.

        ``state`` protects against cross-site request forgery, ``nonce``
        binds the returned ID token to this request, and PKCE (``code_verifier``)
        binds the authorization code to this client.
        """
        metadata = self.metadata()
        client = self._oauth_client()
        kwargs: dict[str, Any] = {"nonce": nonce}
        if redirect_uri:
            kwargs["redirect_uri"] = redirect_uri
        url, _ = client.create_authorization_url(
            metadata["authorization_endpoint"],
            state=state,
            code_verifier=code_verifier if self.config.pkce else None,
            **kwargs,
        )
        return url

    def exchange_code(
        self,
        *,
        code: str,
        code_verifier: str | None = None,
        redirect_uri: str | None = None,
    ) -> dict[str, Any]:
        """Exchange the authorization code for the provider's tokens."""
        metadata = self.metadata()
        client = self._oauth_client()
        kwargs: dict[str, Any] = {"code": code}
        if code_verifier and self.config.pkce:
            kwargs["code_verifier"] = code_verifier
        if redirect_uri:
            kwargs["redirect_uri"] = redirect_uri
        try:
            token = client.fetch_token(metadata["token_endpoint"], **kwargs)
        except Exception as exc:  # noqa: BLE001 - authlib raises several error types
            raise OidcProtocolError(
                f"The authorization code was rejected by the provider: {exc}"
            ) from exc
        return dict(token)

    def verify_id_token(self, id_token: str, *, nonce: str) -> dict[str, Any]:
        """Verify the ID token and return its claims.

        The signature is checked against the provider's JWKS; the issuer,
        audience, expiry, subject and nonce are checked against the
        expectations of this client. A token that fails any of these checks
        establishes no identity.
        """
        metadata = self.metadata()
        key_set = self.jwks()
        algorithms = self._allowed_algorithms(metadata)

        try:
            token = jose_jwt.decode(id_token, key_set, algorithms=algorithms)
        except Exception as exc:  # noqa: BLE001 - joserfc raises several error types
            raise OidcProtocolError(
                f"The ID token could not be verified: {exc}"
            ) from exc

        claims = dict(token.claims)
        registry = JWTClaimsRegistry(
            iss={"essential": True, "value": metadata["issuer"]},
            aud={"essential": True, "value": self.config.client_id},
            sub={"essential": True},
            exp={"essential": True},
            nonce={"essential": True, "value": nonce},
        )
        try:
            registry.validate(claims)
        except Exception as exc:  # noqa: BLE001 - joserfc raises several error types
            raise OidcProtocolError(
                f"The ID token failed validation: {exc}"
            ) from exc

        # With more than one audience the authorized party must be named.
        audience = claims.get("aud")
        if isinstance(audience, list) and len(audience) > 1:
            if claims.get("azp") != self.config.client_id:
                raise OidcProtocolError(
                    "The ID token lists several audiences but does not name this client "
                    "as the authorized party."
                )

        return claims

    @staticmethod
    def _allowed_algorithms(metadata: Mapping[str, Any]) -> list[str]:
        """The signature algorithms this client accepts for ID tokens."""
        supported = metadata.get("id_token_signing_alg_values_supported") or []
        allowed = [
            algorithm
            for algorithm in supported
            if algorithm in ALLOWED_ID_TOKEN_ALGORITHMS
        ]
        if not allowed:
            # The provider did not advertise its algorithms. RS256 is what
            # Authentik uses; refusing everything else is the safe default.
            allowed = ["RS256"]
        return allowed


__all__ = [
    "ALLOWED_ID_TOKEN_ALGORITHMS",
    "OidcClient",
    "OidcConfigurationError",
    "OidcError",
    "OidcProtocolError",
    "OidcProviderConfig",
]
