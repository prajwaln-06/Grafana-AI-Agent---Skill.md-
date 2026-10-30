"""
tests/test_auth_store_check.py

Integration tests for the scoped FastAPI store check and identity resolution.
Covers bearer tokens, session cookies, database lookups, auto-registration of
pending identities, and account status enforcement (active vs. pending/deactivated).
"""
from __future__ import annotations

import json
from unittest import mock

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth.dependencies import require_principal, resolve_principal_status
from app.auth.principal import Principal
from app.auth.session import SignedSession
from app.storage.database import Base, get_engine, init_db
from app.storage.seed import seed_dummy_data_if_empty, DEFAULT_ISSUER

SESSION_SECRET = "test-session-secret-987654321012345"
COOKIE_NAME = "grafana_ai_session"

test_router = APIRouter()


@test_router.get("/protected/data")
async def protected_endpoint(principal: Principal = Depends(require_principal)):
    return {
        "status": "ok",
        "userId": principal.user_id,
        "role": principal.role,
        "authzVersion": principal.authz_version,
    }


@test_router.get("/api/auth/whoami")
async def whoami_endpoint(principal: Principal = Depends(resolve_principal_status)):
    return principal.to_public_dict()


@pytest.fixture(autouse=True)
def setup_test_environment(monkeypatch, tmp_path):
    """Setup isolated SQLite database and mock settings for each test."""
    db_file = tmp_path / "test_rbac.db"
    db_url = f"sqlite:///{db_file.as_posix()}"

    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("AUTHENTIK_ENABLED", "true")
    monkeypatch.setenv("AUTHENTIK_SESSION_SECRET", SESSION_SECRET)
    monkeypatch.setenv("AUTHENTIK_SESSION_COOKIE_NAME", COOKIE_NAME)
    monkeypatch.setenv("AUTHENTIK_OIDC_DISCOVERY_URL", "http://localhost:9000/application/o/grafana-ai-agent/.well-known/openid-configuration")
    monkeypatch.setenv("AUTHENTIK_OIDC_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("AUTHENTIK_OIDC_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("AUTHENTIK_OIDC_REDIRECT_URI", "http://localhost:5173/api/auth/callback")

    # Reset database engine singleton
    import app.storage.database as db_mod
    db_mod._engine = None
    db_mod._SessionFactory = None

    import app.config as config_mod
    config_mod._settings = None

    init_db(auto_seed=True)
    yield


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(test_router)
    return TestClient(app)


def _make_cookie(subject: str, issuer: str = DEFAULT_ISSUER, email: str = "user@test.com") -> str:
    session = SignedSession(secret=SESSION_SECRET, cookie_name=COOKIE_NAME)
    return session.encode({"iss": issuer, "sub": subject, "email": email})


def test_missing_credentials_returns_401(client):
    """Calling a protected route without a session or bearer token returns 401."""
    response = client.get("/protected/data")
    assert response.status_code == 401
    assert "Authentication required" in response.json()["detail"]


def test_active_user_session_resolves_principal(client):
    """Active user (seeded maintainer) resolves successfully from the database."""
    cookie = _make_cookie(subject="ak_sub_maintainer_01")
    client.cookies.set(COOKIE_NAME, cookie)

    response = client.get("/protected/data")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["userId"] == "usr_maint_01"
    assert data["role"] == "maintainer"
    assert data["authzVersion"] == 2


def test_pending_user_blocked_on_protected_route(client):
    """Pending user is refused with 403 on protected routes."""
    cookie = _make_cookie(subject="ak_sub_new_hire")
    client.cookies.set(COOKIE_NAME, cookie)

    response = client.get("/protected/data")
    assert response.status_code == 403
    assert "pending activation" in response.json()["detail"]


def test_pending_user_allowed_on_whoami(client):
    """Section 4.4: Pending user can access /api/auth/whoami to check their status."""
    cookie = _make_cookie(subject="ak_sub_new_hire", email="newhire@example.com")
    client.cookies.set(COOKIE_NAME, cookie)

    response = client.get("/api/auth/whoami")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "pending"
    assert data["role"] is None
    assert data["userId"] == "usr_pending_01"


def test_deactivated_user_blocked_on_protected_route(client):
    """Deactivated user is refused with 403 on protected routes."""
    cookie = _make_cookie(subject="ak_sub_former_emp")
    client.cookies.set(COOKIE_NAME, cookie)

    response = client.get("/protected/data")
    assert response.status_code == 403
    assert "deactivated" in response.json()["detail"].lower()


def test_unknown_identity_auto_registered_as_pending(client):
    """First sign-in of unknown identity auto-registers in DB as pending and returns 403."""
    brand_new_subject = "ak_sub_brand_new_visitor_123"
    cookie = _make_cookie(subject=brand_new_subject, email="visitor@example.com")
    client.cookies.set(COOKIE_NAME, cookie)

    response = client.get("/protected/data")
    assert response.status_code == 403
    assert "pending activation" in response.json()["detail"]

    # Verify user now exists in DB with pending status via whoami
    whoami_resp = client.get("/api/auth/whoami")
    assert whoami_resp.status_code == 200
    user_info = whoami_resp.json()
    assert user_info["subject"] == brand_new_subject
    assert user_info["status"] == "pending"
    assert user_info["userId"].startswith("usr_")
    assert user_info["role"] is None


def test_bearer_token_verification_flow(client):
    """Bearer access token is verified and mapped to the database user."""
    mock_claims = {
        "iss": DEFAULT_ISSUER,
        "sub": "ak_sub_admin_system",
        "aud": "test-client-id",
        "email": "admin@example.com",
    }

    with mock.patch("app.auth.oidc.OidcClient.verify_bearer_token", return_value=mock_claims):
        response = client.get(
            "/protected/data",
            headers={"Authorization": "Bearer mock-access-token-jwt"},
        )

    assert response.status_code == 200
    assert response.json()["userId"] == "usr_admin_01"
    assert response.json()["role"] == "admin"

