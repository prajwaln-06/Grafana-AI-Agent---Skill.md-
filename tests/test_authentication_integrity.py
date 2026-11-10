"""tests/test_authentication_integrity.py

Regression tests for the integrity of the authenticated test setup.

The `api_principal` fixture injects an authenticated identity into the API
under test. That is the right way to test authenticated behaviour -- but it
must not be able to hide a broken authentication or authorization boundary.

These tests assert that:

* the fixture installs a realistic, *authenticated*, active principal --
  not the development-anonymous principal that means "authentication is
  switched off";
* the fixture reaches the endpoint, the request context and the ADK tool
  wrappers, so the production authorization path is what runs;
* the fixture does **not** bypass the administrative role check: an
  authenticated non-administrator still gets HTTP 403;
* the fixture does **not** bypass the account-status check: an inactive
  account still gets HTTP 403;
* the application's own authentication enforcement is untouched: an
  unauthenticated request still gets HTTP 401;
* the tests that assert HTTP 401 do not use the fixture.

The module also documents, one by one, what each of the tests that the
P0 implementation had to re-authenticate actually verifies.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.auth.principal import Principal


# ---------------------------------------------------------------------------
# A. The fixture installs a realistic identity
# ---------------------------------------------------------------------------

class TestFixtureInstallsARealisticPrincipal:
    """The injected identity looks like a real authenticated account."""

    def test_the_default_principal_is_authenticated_and_active(self, api_principal):
        assert api_principal.authenticated is True
        assert api_principal.status == "active"
        assert api_principal.is_active is True

    def test_the_default_principal_is_not_the_development_anonymous_one(self, api_principal):
        """The development principal means "authentication is off".

        Using it as the stand-in for an authenticated caller would let a
        test pass for the wrong reason, so the fixture must not use it.
        """
        assert api_principal.development_mode is False
        assert api_principal.subject != "anonymous"
        assert api_principal.user_id != "usr_dev_anonymous"

    def test_the_default_principal_has_an_explicit_role(self, api_principal):
        assert api_principal.role in {"admin", "maintainer", "operator", "observer"}

    def test_a_parametrized_principal_is_used_verbatim(self, make_test_principal):
        principal = make_test_principal(
            "maintainer", user_id="usr_param", folders=("fld-x",), dashboards=("dash-y",)
        )
        assert principal.role == "maintainer"
        assert principal.allowed_folder_uids == frozenset({"fld-x"})
        assert principal.allowed_dashboard_uids == frozenset({"dash-y"})

    def test_the_factory_produces_an_authenticated_active_principal(self, make_test_principal):
        principal = make_test_principal("observer")
        assert principal.authenticated is True
        assert principal.status == "active"
        assert principal.development_mode is False

    def test_the_factory_can_produce_an_inactive_principal(self, make_test_principal):
        principal = make_test_principal("observer", status="deactivated")
        assert principal.is_active is False


# ---------------------------------------------------------------------------
# B. The fixture reaches every layer that makes an authorization decision
# ---------------------------------------------------------------------------

class TestFixtureReachesEveryAuthorizationLayer:
    """The identity reaches the endpoint, the context and the tool wrappers."""

    def test_the_endpoint_sees_the_injected_identity(self, api_principal):
        from app.api.main import app
        from app.auth.principal import get_current_principal

        with TestClient(app) as client:
            assert client.get("/api/catalog").status_code == 200
            assert get_current_principal().user_id == api_principal.user_id

    def test_the_tool_wrapper_sees_the_injected_identity(self, api_principal):
        """The ADK tool wrappers read the identity from the request context.

        If they did not, the whole scope enforcement of the agent tools
        would be running against an anonymous caller.
        """
        import app.grafana_tools.wrapper as wrapper

        seen: dict[str, object] = {}

        async def fake_list(refresh: bool = False):
            seen["principal"] = wrapper.current_principal()
            return []

        original = wrapper.fetch_dashboard_list
        wrapper.fetch_dashboard_list = fake_list
        try:
            wrapper.list_dashboards()
        finally:
            wrapper.fetch_dashboard_list = original

        assert seen["principal"] is not None
        assert seen["principal"].user_id == api_principal.user_id
        assert seen["principal"].role == api_principal.role


# ---------------------------------------------------------------------------
# C. The fixture does not bypass the application's own checks
# ---------------------------------------------------------------------------

class TestFixtureDoesNotBypassTheAuthorizationChecks:
    """The override replaces identity resolution -- nothing else."""

    def test_an_authenticated_non_administrator_still_gets_403(self, make_test_principal):
        from app.api.main import app
        from app.auth.dependencies import require_principal

        principal = make_test_principal("maintainer")
        app.dependency_overrides[require_principal] = lambda: principal
        try:
            with TestClient(app) as client:
                response = client.get("/api/v1/admin/users")
        finally:
            app.dependency_overrides.pop(require_principal, None)

        assert response.status_code == 403

    def test_an_administrator_is_allowed(self, make_test_principal):
        from app.api.main import app
        from app.auth.dependencies import require_principal

        principal = make_test_principal("admin", folders=("*",))
        app.dependency_overrides[require_principal] = lambda: principal
        try:
            with TestClient(app) as client:
                response = client.get("/api/v1/admin/users")
        finally:
            app.dependency_overrides.pop(require_principal, None)

        assert response.status_code == 200

    def test_a_pending_account_still_gets_403(self, make_test_principal):
        """The status check is part of the resolver, not of the override.

        The fixture replaces the resolver wholesale, so a pending account
        injected this way is *not* refused by the resolver -- but it is
        still refused by every authorization decision, because every one of
        them starts with "is this principal active?".
        """
        from app.auth.policy import authorize_dashboard_read, authorize_promql

        principal = make_test_principal("admin", status="pending", folders=("*",))
        assert principal.is_active is False
        assert authorize_dashboard_read(principal, "dash-x", "fld-x").allowed is False
        assert authorize_promql(principal, environment="staging").allowed is False

    def test_the_scope_of_the_injected_identity_is_honoured(self, make_test_principal):
        """An observer injected with a narrow scope sees only that scope."""
        from app.auth.policy import authorize_dashboard_read

        principal = make_test_principal(
            "observer", folders=("fld-mine",), dashboards=("dash-mine",)
        )
        assert authorize_dashboard_read(principal, "dash-mine", "fld-mine").allowed is True
        assert authorize_dashboard_read(principal, "dash-other", "fld-other").allowed is False


# ---------------------------------------------------------------------------
# D. The application's own authentication enforcement is untouched
# ---------------------------------------------------------------------------

class TestApplicationAuthenticationIsStillEnforced:
    """The real endpoints still refuse an unauthenticated caller."""

    def test_an_unauthenticated_protected_endpoint_returns_401(self):
        """No ``api_principal`` here: this is the application's own check."""
        from app.api.main import app

        with TestClient(app) as client:
            assert client.get("/api/catalog").status_code == 401
            assert client.get("/api/v1/capabilities").status_code == 401
            assert client.get("/api/v1/admin/users").status_code == 401
            assert client.get("/api/v1/admin/folders").status_code == 401
            assert client.get("/api/v1/admin/audit-log").status_code == 401
            assert client.get("/api/v1/admin/seed-audit").status_code == 401

    def test_an_unauthenticated_chat_request_returns_401(self):
        from app.api.main import app

        with TestClient(app) as client:
            response = client.post("/api/chat", json={"message": "hello"})
        assert response.status_code == 401

    def test_the_whoami_endpoint_still_requires_a_session(self):
        from app.api.main import app

        with TestClient(app) as client:
            assert client.get("/api/auth/whoami").status_code == 401

    def test_the_401_tests_do_not_use_the_authenticated_fixture(self):
        """A 401 test that installs a principal would assert nothing."""
        import inspect

        import tests.test_auth_oidc as oidc
        import tests.test_auth_store_check as store_check

        for module in (oidc, store_check):
            for name, func in vars(module).items():
                if not name.startswith("test_"):
                    continue
                if not inspect.isfunction(func):
                    continue
                params = inspect.signature(func).parameters
                assert "api_principal" not in params, (
                    f"{module.__name__}.{name} asserts on an unauthenticated response "
                    "but installs an authenticated principal"
                )


# ---------------------------------------------------------------------------
# E. The narrowest practical Gemini boundary
# ---------------------------------------------------------------------------

class TestGeminiStubBoundary:
    """The language-model dependency is stubbed narrowly, not wholesale."""

    def test_the_alert_hand_off_test_stubs_only_the_pipeline_call(self):
        """``test_awaiting_metric_for_alert_resolution`` stubs one function.

        The hand-off logic under test -- recognising the pending action,
        rewriting the request, clearing the pending state -- is the
        application's own code and must still run. Only the language-model
        call is replaced.
        """
        import inspect

        import tests.test_concept_catalog as catalog

        source = inspect.getsource(catalog.test_awaiting_metric_for_alert_resolution)
        assert 'patch("app.api.routes_chat.run_pipeline"' in source
        # The application code itself is not patched away.
        assert "app.api.routes_chat.SESSION_STORE" not in source
        # The hand-off assertions survive.
        assert "pending_action is None" in source

    def test_the_provider_error_tests_are_not_stubbed_away(self):
        """A provider misconfiguration must still surface as HTTP 503."""
        import inspect

        import tests.test_chat_provider_error as provider_error

        source = inspect.getsource(provider_error)
        assert "LLMProviderConfigError" in source
        assert "503" in source

    def test_the_provider_error_test_keeps_its_real_error_assertion(self):
        import inspect

        import tests.test_chat_provider_error as provider_error

        for name in (
            "test_chat_reports_a_provider_misconfiguration_as_503",
            "test_chat_reports_an_adk_provider_misconfiguration_as_503",
        ):
            source = inspect.getsource(getattr(provider_error, name))
            assert "503" in source
            assert "GEMINI_API_KEY" in source or "status_code == 503" in source
