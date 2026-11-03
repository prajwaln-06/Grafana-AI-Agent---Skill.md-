"""Tests for the Authentik OpenID Connect authentication.

The tests below exercise the whole Authorization Code flow against a *stub*
provider that runs on localhost: it publishes a discovery document, a JWKS
and a token endpoint, and signs ID tokens with a throw-away key. Nothing
outside this process is contacted, and no Authentik instance is needed.

What is covered:

* the login redirect carries a state, a nonce and a PKCE challenge,
* the callback validates the state and the ID token before it establishes a
  session,
* a tampered, expired or forged session is rejected with 401,
* identity headers sent by the browser are never believed,
* the session cookie is HttpOnly and holds no token,
* logging out ends the application session,
* a misconfigured deployment fails closed.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from joserfc import jwt as jose_jwt
from joserfc.jwk import RSAKey

from app.auth.session import SessionError, SignedSession

CLIENT_ID = "test-client-id"
CLIENT_SECRET = "test-client-secret"
SESSION_SECRET = "test-session-secret-0123456789"
REDIRECT_URI = "http://localhost:5173/api/auth/callback"
COOKIE_NAME = "grafana_ai_session"
TXN_COOKIE_NAME = f"{COOKIE_NAME}_txn"


# --------------------------------------------------------------------------
# A stub OIDC provider
# --------------------------------------------------------------------------

class StubProvider:
    """A minimal OIDC provider on localhost."""

    def __init__(self) -> None:
        self.key = RSAKey.generate_key(2048, parameters={"kid": "stub-key"})
        self.issuer = ""
        self.token_requests: list[dict] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle --------------------------------------------------------

    def start(self) -> "StubProvider":
        provider = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # noqa: ANN002 - silence the server
                return

            def _send_json(self, payload: dict, status: int = 200) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802 - http.server API
                path = urlparse(self.path).path
                if path == "/.well-known/openid-configuration":
                    self._send_json(
                        {
                            "issuer": provider.issuer,
                            "authorization_endpoint": f"{provider.issuer}/authorize",
                            "token_endpoint": f"{provider.issuer}/token",
                            "jwks_uri": f"{provider.issuer}/jwks",
                            "end_session_endpoint": (
                                f"{provider.issuer}/application/o/test-app/end-session/"
                            ),
                            "response_types_supported": ["code"],
                            "subject_types_supported": ["public"],
                            "id_token_signing_alg_values_supported": ["RS256"],
                            "code_challenge_methods_supported": ["S256"],
                            "scopes_supported": ["openid", "profile", "email"],
                            "grant_types_supported": ["authorization_code"],
                        }
                    )
                elif path == "/jwks":
                    self._send_json({"keys": [provider.key.as_dict(private=False)]})
                else:
                    self._send_json({"error": "not_found"}, 404)

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                path = urlparse(self.path).path
                length = int(self.headers.get("Content-Length") or 0)
                form = parse_qs(self.rfile.read(length).decode("utf-8"))
                if path != "/token":
                    self._send_json({"error": "not_found"}, 404)
                    return

                provider.token_requests.append(
                    {
                        "form": {key: value[0] for key, value in form.items()},
                        "authorization": self.headers.get("Authorization", ""),
                    }
                )
                code = form.get("code", [""])[0]
                # The code carries the nonce the caller expects, so that the
                # stub can echo it back in the ID token.
                nonce = code[len("nonce:"):] if code.startswith("nonce:") else ""
                id_token = jose_jwt.encode(
                    {"alg": "RS256", "kid": "stub-key"},
                    {
                        "iss": provider.issuer,
                        "aud": CLIENT_ID,
                        "sub": "8f3b1c0e-user-identifier",
                        "exp": int(time.time()) + 300,
                        "iat": int(time.time()),
                        "nonce": nonce,
                        "email": "jaidev@example.com",
                        "preferred_username": "jaidev",
                        "name": "Jaidev Test",
                    },
                    provider.key,
                )
                self._send_json(
                    {
                        "access_token": "access-token-not-used-by-the-app",
                        "token_type": "Bearer",
                        "expires_in": 300,
                        "id_token": id_token,
                    }
                )

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.issuer = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


@pytest.fixture()
def provider():
    stub = StubProvider().start()
    try:
        yield stub
    finally:
        stub.stop()


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

def _settings(provider: StubProvider, **overrides):
    from app.config import Settings

    values = {
        "gemini_api_key": "test-key-not-real",
        "authentik_enabled": True,
        "authentik_oidc_discovery_url": (
            f"{provider.issuer}/.well-known/openid-configuration"
        ),
        "authentik_oidc_client_id": CLIENT_ID,
        "authentik_oidc_client_secret": CLIENT_SECRET,
        "authentik_oidc_redirect_uri": REDIRECT_URI,
        "authentik_session_secret": SESSION_SECRET,
        "authentik_session_cookie_name": COOKIE_NAME,
        "authentik_base_url": "http://localhost:9000",
        "authentik_app_slug": "grafana-ai-agent",
        "authentik_post_logout_redirect_uri": "http://localhost:5173/",
        "authentik_signup_url": None,
    }
    values.update(overrides)
    return Settings(**values)


def _patch_settings(monkeypatch, settings):
    from app.api import main as api_main
    from app.api import routes_auth
    from app.auth import dependencies as auth_dependencies

    for module in (api_main, routes_auth, auth_dependencies):
        monkeypatch.setattr(module, "get_settings", lambda: settings)


@pytest.fixture()
def client(monkeypatch, provider):
    from app.api.main import app

    _patch_settings(monkeypatch, _settings(provider))
    with TestClient(app) as test_client:
        yield test_client


def _start_login(client: TestClient) -> dict:
    """Run the login step and return the transaction's state and nonce."""
    response = client.get("/api/auth/login", follow_redirects=False)
    assert response.status_code == 302
    transaction = SignedSession(
        secret=SESSION_SECRET,
        max_age_seconds=600,
        cookie_name=TXN_COOKIE_NAME,
    )
    payload = transaction.decode(client.cookies.get(TXN_COOKIE_NAME))
    return {"response": response, "state": payload["state"], "nonce": payload["nonce"]}


# --------------------------------------------------------------------------
# The login redirect
# --------------------------------------------------------------------------

def test_login_redirects_to_the_provider_with_state_nonce_and_pkce(client):
    response = client.get("/api/auth/login", follow_redirects=False)
    assert response.status_code == 302

    location = urlparse(response.headers["location"])
    query = {key: value[0] for key, value in parse_qs(location.query).items()}

    assert location.path == "/authorize"
    assert query["response_type"] == "code"
    assert query["client_id"] == CLIENT_ID
    assert query["redirect_uri"] == REDIRECT_URI
    assert query["scope"].split() == ["openid", "profile", "email"]
    assert query["state"]
    assert query["nonce"]
    assert query["code_challenge"]
    assert query["code_challenge_method"] == "S256"


def test_login_transaction_cookie_is_short_lived_and_signed(client):
    client.get("/api/auth/login", follow_redirects=False)
    value = client.cookies.get(TXN_COOKIE_NAME)
    assert value

    transaction = SignedSession(
        secret=SESSION_SECRET, max_age_seconds=600, cookie_name=TXN_COOKIE_NAME
    )
    payload = transaction.decode(value)
    assert payload["state"] and payload["nonce"] and payload["verifier"]

    # A transaction cookie signed with another secret is not accepted.
    other = SignedSession(
        secret="another-secret", max_age_seconds=600, cookie_name=TXN_COOKIE_NAME
    )
    with pytest.raises(SessionError):
        other.decode(value)


# --------------------------------------------------------------------------
# The callback
# --------------------------------------------------------------------------

def test_callback_establishes_a_session(client):
    login = _start_login(client)

    response = client.get(
        "/api/auth/callback",
        params={"code": f"nonce:{login['nonce']}", "state": login["state"]},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["location"] == "/"

    session_cookie = client.cookies.get(COOKIE_NAME)
    assert session_cookie

    session = SignedSession(secret=SESSION_SECRET, cookie_name=COOKIE_NAME)
    payload = session.decode(session_cookie)
    assert payload["sub"] == "8f3b1c0e-user-identifier"
    assert payload["email"] == "jaidev@example.com"
    # No token of any kind is kept in the session.
    assert "id_token" not in payload
    assert "access_token" not in payload


def test_callback_sends_the_code_verifier_and_the_client_secret(client, provider):
    login = _start_login(client)
    client.get(
        "/api/auth/callback",
        params={"code": f"nonce:{login['nonce']}", "state": login["state"]},
        follow_redirects=False,
    )

    request = provider.token_requests[-1]
    assert request["form"]["grant_type"] == "authorization_code"
    assert request["form"]["code_verifier"]
    assert request["form"]["redirect_uri"] == REDIRECT_URI
    # Confidential client: the secret is presented to the token endpoint.
    assert request["authorization"].startswith("Basic ")


def test_callback_rejects_a_mismatching_state(client):
    login = _start_login(client)

    response = client.get(
        "/api/auth/callback",
        params={"code": f"nonce:{login['nonce']}", "state": "not-the-state"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert "auth_error=state_mismatch" in response.headers["location"]
    assert not client.cookies.get(COOKIE_NAME)


def test_callback_rejects_a_mismatching_nonce(client):
    login = _start_login(client)

    response = client.get(
        "/api/auth/callback",
        params={"code": "nonce:the-wrong-nonce", "state": login["state"]},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert "auth_error=id_token_invalid" in response.headers["location"]
    assert not client.cookies.get(COOKIE_NAME)


def test_callback_without_a_login_transaction_is_rejected(client):
    response = client.get(
        "/api/auth/callback", params={"code": "abc", "state": "xyz"}, follow_redirects=False
    )
    assert response.status_code == 302
    assert "auth_error=login_state_missing" in response.headers["location"]
    assert not client.cookies.get(COOKIE_NAME)


def test_callback_reports_a_provider_error(client):
    response = client.get(
        "/api/auth/callback",
        params={"error": "access_denied", "error_description": "nope"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert "auth_error=provider_error" in response.headers["location"]


# --------------------------------------------------------------------------
# The session
# --------------------------------------------------------------------------

def test_session_cookie_is_httponly_and_lax(client):
    login = _start_login(client)
    response = client.get(
        "/api/auth/callback",
        params={"code": f"nonce:{login['nonce']}", "state": login["state"]},
        follow_redirects=False,
    )
    cookie_header = response.headers.get("set-cookie", "")
    assert "HttpOnly" in cookie_header
    assert "SameSite=lax" in cookie_header
    assert f"{COOKIE_NAME}=" in cookie_header


def test_whoami_returns_the_verified_identity(client):
    login = _start_login(client)
    client.get(
        "/api/auth/callback",
        params={"code": f"nonce:{login['nonce']}", "state": login["state"]},
        follow_redirects=False,
    )

    body = client.get("/api/auth/whoami").json()
    assert body["authenticated"] is True
    assert body["subject"] == "8f3b1c0e-user-identifier"
    assert body["email"] == "jaidev@example.com"
    assert body["username"] == "jaidev"
    assert body["app"] == "grafana-ai-agent"
    assert body["issuer"].startswith("http://127.0.0.1:")


def test_whoami_requires_a_session(client):
    assert client.get("/api/auth/whoami").status_code == 401


def test_protected_endpoints_require_a_session(client):
    assert client.get("/api/catalog").status_code == 401
    assert client.get("/api/v1/capabilities").status_code == 401


def test_identity_headers_from_the_browser_are_never_believed(client):
    """A client cannot become someone else by sending headers."""
    response = client.get(
        "/api/catalog",
        headers={
            "X-authentik-uid": "900347b8a29876b45ca6f75722635ecfedf0e931c6022e3a29a8aa13fb5516fb",
            "X-authentik-email": "attacker@example.com",
            "X-authentik-username": "attacker",
        },
    )
    assert response.status_code == 401


def test_a_forged_session_cookie_is_rejected(client):
    session = SignedSession(secret="not-the-session-secret", cookie_name=COOKIE_NAME)
    forged = session.encode({"iss": "http://x", "sub": "attacker", "email": "a@b.c"})
    client.cookies.set(COOKIE_NAME, forged)
    assert client.get("/api/auth/whoami").status_code == 401


def test_an_expired_session_is_rejected(client):
    session = SignedSession(secret=SESSION_SECRET, cookie_name=COOKIE_NAME, max_age_seconds=1)
    expired = session.encode(
        {"iss": "http://x", "sub": "u", "email": "a@b.c"}, now=time.time() - 3600
    )
    client.cookies.set(COOKIE_NAME, expired)
    assert client.get("/api/auth/whoami").status_code == 401


def test_logout_ends_the_application_session(client):
    login = _start_login(client)
    client.get(
        "/api/auth/callback",
        params={"code": f"nonce:{login['nonce']}", "state": login["state"]},
        follow_redirects=False,
    )
    assert client.get("/api/auth/whoami").status_code == 200

    response = client.post("/api/auth/logout")
    assert response.status_code == 200
    body = response.json()
    # The logout ends the application session *and* the provider's session.
    assert body["scope"] == "application+provider"
    assert body["globalSsoLogout"] is True
    assert body["endSessionUrl"]
    assert client.get("/api/auth/whoami").status_code == 401


# --------------------------------------------------------------------------
# One login attempt, one authentication flow
# --------------------------------------------------------------------------

def _authorization_query(response) -> dict:
    location = urlparse(response.headers["location"])
    return {key: value[0] for key, value in parse_qs(location.query).items()}


def test_login_does_not_ask_the_provider_to_authenticate_again(client):
    """One login attempt must start exactly one authentication flow.

    Authentik re-runs its authentication flow when the authorization request
    carries ``prompt=login`` and the browser has no session yet -- which is
    every first sign-in. The user would then be asked for their email and
    password twice for a single login attempt.
    """
    response = client.get("/api/auth/login", follow_redirects=False)
    assert response.status_code == 302
    assert "prompt" not in _authorization_query(response)


def test_login_never_forwards_prompt_login(client):
    response = client.get(
        "/api/auth/login", params={"prompt": "login"}, follow_redirects=False
    )
    assert "prompt" not in _authorization_query(response)


def test_login_ignores_an_unsupported_prompt(client):
    response = client.get(
        "/api/auth/login", params={"prompt": "select_account"}, follow_redirects=False
    )
    assert "prompt" not in _authorization_query(response)


def test_login_forwards_a_supported_prompt(client):
    response = client.get(
        "/api/auth/login", params={"prompt": "none"}, follow_redirects=False
    )
    assert _authorization_query(response)["prompt"] == "none"


def test_a_login_transaction_cannot_be_replayed(client):
    """One login attempt, one callback: the transaction is single-use."""
    login = _start_login(client)

    first = client.get(
        "/api/auth/callback",
        params={"code": f"nonce:{login['nonce']}", "state": login["state"]},
        follow_redirects=False,
    )
    assert first.status_code == 302
    assert first.headers["location"] == "/"

    second = client.get(
        "/api/auth/callback",
        params={"code": f"nonce:{login['nonce']}", "state": login["state"]},
        follow_redirects=False,
    )
    assert second.status_code == 302
    assert "auth_error=login_state_missing" in second.headers["location"]


def test_the_session_persists_across_requests(client):
    login = _start_login(client)
    client.get(
        "/api/auth/callback",
        params={"code": f"nonce:{login['nonce']}", "state": login["state"]},
        follow_redirects=False,
    )

    first = client.get("/api/auth/whoami").json()
    second = client.get("/api/auth/whoami").json()
    assert first["subject"] == second["subject"]
    assert first["email"] == "jaidev@example.com"


# --------------------------------------------------------------------------
# The ID token never reaches the browser
# --------------------------------------------------------------------------

def test_the_id_token_is_kept_on_the_server_not_in_the_cookie(client):
    from app.auth.id_token_store import get_id_token_store

    login = _start_login(client)
    client.get(
        "/api/auth/callback",
        params={"code": f"nonce:{login['nonce']}", "state": login["state"]},
        follow_redirects=False,
    )

    session = SignedSession(secret=SESSION_SECRET, cookie_name=COOKIE_NAME)
    payload = session.decode(client.cookies.get(COOKIE_NAME))
    # The cookie carries an opaque handle, never a token.
    assert "id_token" not in payload
    assert "access_token" not in payload
    session_id = payload.get("sid")
    assert session_id

    stored = get_id_token_store().get(session_id)
    assert stored
    assert stored.count(".") == 2  # a JWT


# --------------------------------------------------------------------------
# The RP-initiated logout
# --------------------------------------------------------------------------

def _signed_in(client: TestClient) -> dict:
    """Sign in and return the payload of the established session."""
    login = _start_login(client)
    client.get(
        "/api/auth/callback",
        params={"code": f"nonce:{login['nonce']}", "state": login["state"]},
        follow_redirects=False,
    )
    session = SignedSession(secret=SESSION_SECRET, cookie_name=COOKIE_NAME)
    return session.decode(client.cookies.get(COOKIE_NAME))


def test_logout_returns_the_providers_end_session_url(client):
    _signed_in(client)

    body = client.post("/api/auth/logout").json()
    url = body["endSessionUrl"]
    parsed = urlparse(url)

    # The endpoint comes from the provider's discovery document.
    assert parsed.path.endswith("/application/o/test-app/end-session/")

    params = {key: value[0] for key, value in parse_qs(parsed.query).items()}
    assert params["client_id"] == CLIENT_ID
    # The post-logout redirect URI is the one registered in the provider.
    assert params["post_logout_redirect_uri"] == "http://localhost:5173/"
    # The ID token is presented as the hint, so the provider accepts it.
    assert params["id_token_hint"].count(".") == 2


def test_logout_encodes_the_post_logout_redirect_uri(client):
    _signed_in(client)

    url = client.post("/api/auth/logout").json()["endSessionUrl"]
    assert "post_logout_redirect_uri=http%3A%2F%2Flocalhost%3A5173%2F" in url
    assert "client_id=test-client-id" in url


def test_logout_uses_the_configured_post_logout_redirect_uri(monkeypatch, provider):
    from app.api.main import app

    _patch_settings(
        monkeypatch,
        _settings(
            provider,
            authentik_post_logout_redirect_uri="https://grafana.example.com/logged-out",
        ),
    )
    with TestClient(app) as test_client:
        _signed_in(test_client)
        url = test_client.post("/api/auth/logout").json()["endSessionUrl"]

    params = {
        key: value[0] for key, value in parse_qs(urlparse(url).query).items()
    }
    assert params["post_logout_redirect_uri"] == "https://grafana.example.com/logged-out"


def test_logout_forgets_the_retained_id_token(client):
    from app.auth.id_token_store import get_id_token_store

    payload = _signed_in(client)
    session_id = payload["sid"]
    store = get_id_token_store()
    assert store.get(session_id)

    client.post("/api/auth/logout")
    assert store.get(session_id) is None


def test_get_logout_redirects_to_the_end_session_endpoint(client):
    _signed_in(client)

    response = client.get("/api/auth/logout", follow_redirects=False)
    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("http://127.0.0.1:")
    assert "/application/o/test-app/end-session/" in location
    # The application session is cleared on the way out.
    assert client.get("/api/auth/whoami").status_code == 401


def test_logout_without_a_session_still_ends_the_provider_session(client):
    """A logout without an application session has no ID token to present.

    The provider refuses a post-logout redirect without a valid
    ``id_token_hint``, so the redirect is left out -- the SSO session is
    still ended.
    """
    body = client.post("/api/auth/logout").json()
    params = {
        key: value[0]
        for key, value in parse_qs(urlparse(body["endSessionUrl"]).query).items()
    }
    assert params["client_id"] == CLIENT_ID
    assert "post_logout_redirect_uri" not in params
    assert "id_token_hint" not in params


def test_logout_falls_back_to_the_configured_base_url(monkeypatch, provider):
    """The end-session URL is still built when the provider is unreachable."""
    from app.api.main import app

    _patch_settings(
        monkeypatch,
        _settings(
            provider,
            authentik_oidc_discovery_url="http://127.0.0.1:1/.well-known/openid-configuration",
        ),
    )
    with TestClient(app) as test_client:
        body = test_client.post("/api/auth/logout").json()

    assert body["endSessionUrl"].startswith(
        "http://localhost:9000/application/o/grafana-ai-agent/end-session/"
    )


def test_logout_in_development_mode_stays_in_the_application(monkeypatch, provider):
    """Without authentication there is no provider session to end."""
    from app.api.main import app

    _patch_settings(monkeypatch, _settings(provider, authentik_enabled=False))
    with TestClient(app) as test_client:
        body = test_client.post("/api/auth/logout").json()

    assert body["endSessionUrl"] == "/"


# --------------------------------------------------------------------------
# The public login configuration
# --------------------------------------------------------------------------

def test_login_config_is_public(client):
    response = client.get("/api/auth/config")
    assert response.status_code == 200
    body = response.json()
    assert body["loginPath"] == "/api/auth/login"
    assert body["callbackPath"] == "/api/auth/callback"
    assert body["logoutPath"] == "/api/auth/logout"
    assert body["signupEnabled"] is True
    assert body["signupUrl"] == "http://localhost:9000/if/flow/default-enrollment-flow/"


def test_login_config_never_exposes_a_secret(client):
    text = client.get("/api/auth/config").text
    assert CLIENT_SECRET not in text
    assert SESSION_SECRET not in text


def test_login_config_without_an_enrollment_flow_has_no_signup_link(monkeypatch, provider):
    from app.api.main import app

    _patch_settings(
        monkeypatch, _settings(provider, authentik_base_url=None, authentik_enrollment_flow="")
    )
    with TestClient(app) as test_client:
        body = test_client.get("/api/auth/config").json()
    assert body["signupEnabled"] is False
    assert body["signupUrl"] is None


# --------------------------------------------------------------------------
# Fail closed
# --------------------------------------------------------------------------

def test_misconfiguration_fails_closed(monkeypatch, provider):
    from app.api.main import app

    _patch_settings(monkeypatch, _settings(provider, authentik_oidc_client_secret=None))
    # No context manager: the lifespan would refuse to start the service at
    # all (see the next test). What matters here is that the endpoints
    # themselves refuse to serve anonymous traffic.
    test_client = TestClient(app)
    assert test_client.get("/api/catalog").status_code == 503
    assert test_client.get("/healthz").status_code == 200


def test_startup_refuses_to_run_with_an_invalid_configuration(monkeypatch, provider):
    from app.api import main as api_main
    from app.api.main import app

    _patch_settings(monkeypatch, _settings(provider, authentik_session_secret=None))
    with pytest.raises(RuntimeError, match="misconfigured"):
        with TestClient(app):
            pass


def test_development_mode_leaves_the_api_open(monkeypatch, provider):
    """With authentication explicitly disabled the local workflow keeps
    working. This is a development convenience, not a security control."""
    from app.api.main import app

    _patch_settings(monkeypatch, _settings(provider, authentik_enabled=False))
    with TestClient(app) as test_client:
        assert test_client.get("/api/catalog").status_code == 200
        body = test_client.get("/api/auth/whoami").json()
        assert body["authenticated"] is False
        assert body["subject"] == "anonymous"


# --------------------------------------------------------------------------
# The signed session itself
# --------------------------------------------------------------------------

def test_signed_session_round_trip():
    session = SignedSession(secret="a-secret", cookie_name="c")
    value = session.encode({"sub": "u1"})
    assert session.decode(value)["sub"] == "u1"


def test_signed_session_rejects_a_tampered_value():
    session = SignedSession(secret="a-secret", cookie_name="c")
    value = session.encode({"sub": "u1"})
    payload, _, signature = value.partition(".")
    with pytest.raises(SessionError):
        session.decode(f"{payload}x.{signature}")
