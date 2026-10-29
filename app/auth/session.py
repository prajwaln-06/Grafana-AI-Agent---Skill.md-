"""
app/auth/session.py

The application session: a signed, HttpOnly cookie that carries the minimum
identity information the application needs between requests.

The session is *not* an OAuth token store. Once the OIDC Authorization Code
flow has been completed and the ID token has been validated, the tokens
themselves are dropped: only the stable identity (``sub``, ``iss``) and the
profile fields the UI displays survive, inside a cookie the browser script
cannot read.

The cookie is signed with HMAC-SHA256 using a server-side secret. It is
integrity-protected, not encrypted, so it must never contain anything beyond
the identity the user is entitled to see anyway (their own subject id and
email address).

Why not a server-side session store? A local development setup should not
need a Redis or a database to log in, and the identity is small enough to
carry in the cookie. The trade-off is deliberate and documented: the session
cannot be revoked centrally before it expires -- it is invalidated by signing
out or by rotating the session secret.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Mapping

logger = logging.getLogger(__name__)


class SessionError(Exception):
    """Raised when a session cookie cannot be trusted (bad signature, expired,
    malformed). It is never surfaced to the browser as-is: the caller answers
    with a plain 401."""


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


class SignedSession:
    """A signed-cookie session.

    Attributes:
        secret: The signing secret. Must be long and random; it never leaves
            the server.
        max_age_seconds: How long a session stays valid.
        cookie_name: The name of the cookie.
        secure: Whether to set the ``Secure`` attribute.
        samesite: The ``SameSite`` attribute.
    """

    def __init__(
        self,
        *,
        secret: str,
        max_age_seconds: int = 3600,
        cookie_name: str = "grafana_ai_session",
        secure: bool = False,
        samesite: str = "lax",
    ) -> None:
        if not secret:
            raise SessionError("A session signing secret is required.")
        self._secret = secret.encode("utf-8")
        self.max_age_seconds = int(max_age_seconds)
        self.cookie_name = cookie_name
        self.secure = bool(secure)
        self.samesite = samesite

    # -- encoding ---------------------------------------------------------

    def _sign(self, payload: str) -> str:
        signature = hmac.new(self._secret, payload.encode("ascii"), hashlib.sha256).digest()
        return f"{payload}.{_b64encode(signature)}"

    def encode(self, data: Mapping[str, Any], *, now: float | None = None) -> str:
        """Sign ``data`` into a cookie value."""
        issued_at = int(now if now is not None else time.time())
        payload = dict(data)
        payload["iat"] = issued_at
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return self._sign(_b64encode(raw))

    def decode(self, value: str | None, *, now: float | None = None) -> dict[str, Any]:
        """Verify and return the payload of a cookie value.

        Raises:
            SessionError: The value is missing, malformed, tampered with or
                expired.
        """
        if not value:
            raise SessionError("No session cookie was presented.")
        payload, _, signature = value.partition(".")
        if not payload or not signature:
            raise SessionError("The session cookie is malformed.")
        expected = hmac.new(
            self._secret, payload.encode("ascii"), hashlib.sha256
        ).digest()
        try:
            presented = _b64decode(signature)
        except Exception as exc:  # noqa: BLE001 - any decoding problem is a bad cookie
            raise SessionError("The session cookie is malformed.") from exc
        if not hmac.compare_digest(presented, expected):
            raise SessionError("The session cookie signature does not match.")

        try:
            data = json.loads(_b64decode(payload))
        except Exception as exc:  # noqa: BLE001
            raise SessionError("The session cookie is malformed.") from exc
        if not isinstance(data, dict):
            raise SessionError("The session cookie is malformed.")

        issued_at = data.get("iat")
        current = now if now is not None else time.time()
        if not isinstance(issued_at, int):
            raise SessionError("The session cookie carries no issue time.")
        if current - issued_at > self.max_age_seconds:
            raise SessionError("The session has expired.")
        if issued_at - current > 60:
            # A cookie issued "in the future" is either a clock problem or a
            # forged cookie. Either way it is not trustworthy.
            raise SessionError("The session cookie is not valid yet.")
        return data

    # -- cookies ----------------------------------------------------------

    def set_cookie(self, response: Any, data: Mapping[str, Any]) -> None:
        """Attach a freshly signed session cookie to a response."""
        response.set_cookie(
            key=self.cookie_name,
            value=self.encode(data),
            max_age=self.max_age_seconds,
            httponly=True,
            secure=self.secure,
            samesite=self.samesite,
            path="/",
        )

    def clear_cookie(self, response: Any) -> None:
        """Remove the session cookie from the browser."""
        response.delete_cookie(
            key=self.cookie_name,
            path="/",
            httponly=True,
            secure=self.secure,
            samesite=self.samesite,
        )


__all__ = ["SessionError", "SignedSession"]
