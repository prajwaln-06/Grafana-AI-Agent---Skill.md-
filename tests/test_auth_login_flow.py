"""The login flow, end to end, against a provider that behaves like Authentik.

The stub below reproduces the one behaviour of Authentik that made the
duplicate login possible: an authorization request that carries
``prompt=login`` runs the authentication flow **twice** for a browser that
has no session yet.

Authentik's authorization endpoint only records the "the browser is already
logged in" marker when the request arrives with an existing session. A first
sign-in has none, so the marker is never written, and the request that
returns from the authentication flow to the authorization endpoint starts the
flow all over again -- the user is asked for their email and password twice.

The test therefore drives the *whole* flow (login -> provider -> callback)
and asserts that one login attempt produces exactly one authentication flow,
one callback and one application session.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import pytest
from joserfc import jwt as jose_jwt
from joserfc.jwk import RSAKey

CLIENT_ID = "test-client-id"
CLIENT_SECRET = "test-client-secret"
SESSION_SECRET = "test-session-secret-0123456789"
REDIRECT_URI = "http://localhost:5173/api/auth/callback"
COOKIE_NAME = "grafana_ai_session"


class AuthentikLikeProvider:
    """A provider that behaves like Authentik's authorization endpoint."""

    def __init__(self) -> None:
        self.key = RSAKey.generate_key(2048, parameters={"kid": "stub-key"})
        self.issuer = ""
        self.flow_starts = 0
        self.callbacks = 0
        self.token_requests: list[dict] = []
        self._sessions: dict[str, dict] = {}
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle --------------------------------------------------------

    def start(self) -> "AuthentikLikeProvider":
        provider = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # noqa: ANN002 - silence the server
                return

            def _session(self) -> tuple[str, dict]:
                raw = self.headers.get("Cookie") or ""
                cookies = dict(
                    part.strip().split("=", 1)
                    for part in raw.split(";")
                    if "=" in part
                )
                session_id = cookies.get("stub_session", "")
                return session_id, provider._sessions.setdefault(session_id, {})

            def _send_json(self, payload: dict, status: int = 200) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _redirect(self, location: str, cookie: str | None = None) -> None:
                self.send_response(302)
                self.send_header("Location", location)
                if cookie:
                    self.send_header("Set-Cookie", cookie)
                self.send_header("Content-Length", "0")
                self.end_headers()

            # -- endpoints -------------------------------------------------

            def do_GET(self) -> None:  # noqa: N802 - http.server API
                parsed = urlparse(self.path)
                path = parsed.path
                query = parsed.query

                if path == "/.well-known/openid-configuration":
                    self._send_json(
                        {
                            "issuer": provider.issuer,
                            "authorization_endpoint": f"{provider.issuer}/authorize",
                            "token_endpoint": f"{provider.issuer}/token",
                            "jwks_uri": f"{provider.issuer}/jwks",
                            "end_session_endpoint": f"{provider.issuer}/end-session",
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
                elif path == "/authorize":
                    self._authorize(query)
                elif path == "/flow/login":
                    # The sign-in form. Answering it signs the user in.
                    self._send_json({"component": "ak-stage-identification"})
                elif path == "/flow/login/submit":
                    session_id, session = self._session()
                    session["login_uid"] = f"login-{provider.flow_starts}-{time.time()}"
                    provider._sessions[session_id] = session
                    cookie = f"stub_session={session_id}; Path=/"
                    self._redirect(f"/authorize?{query}", cookie=cookie)
                else:
                    self._send_json({"error": "not_found"}, 404)

            def _authorize(self, query: str) -> None:
                """Authentik's authorization endpoint, including its prompt rule."""
                params = {k: v[0] for k, v in parse_qs(query).items()}
                _, session = self._session()
                prompt = params.get("prompt", "")

                uid = session.get("login_uid")
                if prompt == "login":
                    if uid is None:
                        # No login event yet: the request goes to the
                        # authentication flow *without* recording the marker.
                        provider.flow_starts += 1
                        self._redirect(f"/flow/login?{query}")
                        return
                    marker = session.get("last_login_uid")
                    if marker is None or uid == marker:
                        session["last_login_uid"] = uid
                        provider.flow_starts += 1
                        self._redirect(f"/flow/login?{query}")
                        return
                elif uid is None:
                    provider.flow_starts += 1
                    self._redirect(f"/flow/login?{query}")
                    return

                provider.callbacks += 1
                code = "nonce:" + params.get("nonce", "")
                redirect = params.get("redirect_uri", "")
                self._redirect(f"{redirect}?{urlencode({'code': code, 'state': params.get('state', '')})}")

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                parsed = urlparse(self.path)
                if parsed.path != "/token":
                    self._send_json({"error": "not_found"}, 404)
                    return
                length = int(self.headers.get("Content-Length") or 0)
                form = parse_qs(self.rfile.read(length).decode("utf-8"))
                provider.token_requests.append(
                    {key: value[0] for key, value in form.items()}
                )
                code = form.get("code", [""])[0]
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
    stub = AuthentikLikeProvider().start()
    try:
        yield stub
    finally:
        stub.stop()


def _settings(provider: AuthentikLikeProvider, app_url: str):
    from app.config import Settings

    return Settings(
        gemini_api_key="test-key-not-real",
        authentik_enabled=True,
        authentik_oidc_discovery_url=(
            f"{provider.issuer}/.well-known/openid-configuration"
        ),
        authentik_oidc_client_id=CLIENT_ID,
        authentik_oidc_client_secret=CLIENT_SECRET,
        authentik_oidc_redirect_uri=f"{app_url}/api/auth/callback",
        authentik_session_secret=SESSION_SECRET,
        authentik_session_cookie_name=COOKIE_NAME,
        authentik_base_url=provider.issuer,
        authentik_app_slug="grafana-ai-agent",
        authentik_post_logout_redirect_uri=f"{app_url}/",
        authentik_signup_url=None,
    )


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _AppServer:
    """The application, served over HTTP so that one client can be the
    browser for the application *and* for the provider."""

    def __init__(self, app, port: int) -> None:
        import uvicorn

        self.port = port
        self.url = f"http://localhost:{port}"
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self) -> "_AppServer":
        self._thread.start()
        for _ in range(200):
            if self._server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("the application did not start")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)


@pytest.fixture()
def app_url(provider, monkeypatch):
    from app.api import main as api_main
    from app.api import routes_auth
    from app.auth import dependencies as auth_dependencies

    port = _free_port()
    settings = _settings(provider, f"http://localhost:{port}")
    for module in (api_main, routes_auth, auth_dependencies):
        monkeypatch.setattr(module, "get_settings", lambda: settings)

    server = _AppServer(api_main.app, port).start()
    try:
        yield server.url
    finally:
        server.stop()


def _client() -> httpx.Client:
    """A browser."""
    return httpx.Client(follow_redirects=False, timeout=20)


def _submit(url: httpx.URL) -> httpx.URL:
    """The URL of the sign-in form's submit action."""
    return url.copy_with(path="/flow/login/submit")


def _sign_in(client: httpx.Client, provider: AuthentikLikeProvider, app_url: str) -> list[str]:
    """Drive the whole flow; return the URLs the browser was sent to."""
    visited: list[str] = []

    response = client.get(f"{app_url}/api/auth/login")
    assert response.status_code == 302
    url = response.headers["location"]
    visited.append(url)

    for _ in range(12):
        response = client.get(url)
        if response.status_code in (301, 302, 303, 307, 308):
            url = httpx.URL(url).join(response.headers["location"])
            visited.append(str(url))
            if url.path == "/flow/login":
                # The user submits the sign-in form.
                response = client.get(_submit(url))
                assert response.status_code == 302
                url = httpx.URL(url).join(response.headers["location"])
                visited.append(str(url))
                continue
            if url.path == "/api/auth/callback":
                # The browser follows the callback: the session is established.
                response = client.get(url)
                assert response.status_code == 302
                visited.append(str(url))
                return visited
            continue
        raise AssertionError(f"unexpected answer {response.status_code} for {url}")

    raise AssertionError("the flow did not finish")


def test_one_login_attempt_means_one_authentication_flow(provider, app_url):
    with _client() as client:
        _sign_in(client, provider, app_url)

    assert provider.flow_starts == 1
    assert provider.callbacks == 1


def test_the_application_session_is_established(provider, app_url):
    with _client() as client:
        _sign_in(client, provider, app_url)
        whoami = client.get(f"{app_url}/api/auth/whoami")

    assert whoami.status_code == 200
    assert whoami.json()["subject"] == "8f3b1c0e-user-identifier"
    assert client.cookies.get(COOKIE_NAME)


def test_the_application_never_asks_the_provider_to_re_authenticate(provider, app_url):
    """The authorization request must not carry ``prompt=login``.

    With it, Authentik runs the authentication flow twice -- see
    ``AuthentikLikeProvider._authorize``.
    """
    with _client() as client:
        response = client.get(f"{app_url}/api/auth/login")
        location = response.headers["location"]

    assert "prompt=login" not in location


def test_prompt_login_would_run_the_flow_twice(provider):
    """The reason the application must not send ``prompt=login``.

    This drives the provider directly -- without the application -- to show
    what Authentik does with it.
    """
    with httpx.Client(follow_redirects=False, timeout=20) as client:
        url = (
            f"{provider.issuer}/authorize?"
            + urlencode(
                {
                    "response_type": "code",
                    "client_id": CLIENT_ID,
                    "redirect_uri": REDIRECT_URI,
                    "state": "s",
                    "nonce": "n",
                    "prompt": "login",
                }
            )
        )
        for _ in range(6):
            response = client.get(url)
            assert response.status_code == 302
            url = httpx.URL(url).join(response.headers["location"])
            if url.path == "/flow/login":
                response = client.get(_submit(url))
                url = httpx.URL(url).join(response.headers["location"])
                continue
            break

    assert provider.flow_starts == 2
    assert provider.callbacks == 1
