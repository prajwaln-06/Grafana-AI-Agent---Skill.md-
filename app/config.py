"""
config.py

Typed configuration, loaded once from environment variables (see
.env.example for the full list). Using pydantic's BaseSettings means a
missing/malformed required value fails loudly at startup with a clear
message, instead of surfacing as a confusing error three phases into a
request.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- LLM ---
    llm_provider: str = Field(
        default="gemini_api",
        description="'gemini_api' (Gemini Developer API / AI Studio, GEMINI_API_KEY) or "
                    "'vertex' (Vertex AI; must be selected explicitly).",
    )
    gemini_api_key: str | None = Field(
        default=None, description="Gemini API (AI Studio) key. Required for llm_provider=gemini_api."
    )
    vertex_project: str | None = Field(default=None, description="GCP project for Vertex AI (ADC auth).")
    vertex_location: str | None = Field(default=None, description="Vertex AI location; 'global' if unset.")
    vertex_api_key: str | None = Field(default=None, description="Vertex AI express-mode API key.")
    gemini_model: str = Field(default="gemini-3.5-flash-lite")

    # --- Skill package ---
    skills_root: Path = Field(default=Path("skills"),
                               description="Directory that directly contains SKILL.md.")

    # --- Structured metric catalog ---
    catalog_path: Path = Field(
        default=Path("app/catalog/catalog.json"),
        description="Path to the structured Prometheus metric catalog, separate "
                    "from the dashboard UI catalog at app/catalog.json.",
    )
    catalog_narrow_min_score: float = Field(
        default=2.0,
        description="Minimum catalog search score required to narrow metric candidates.",
    )
    catalog_metric_status_validation_enabled: bool = Field(
        default=False,
        description="When enabled, validate metric approval status against catalog metadata.",
    )

    # --- Backends ---
    prometheus_url: str = Field(default="http://localhost:9090")
    opensearch_url: str = Field(default="http://localhost:9200")
    opensearch_auth_username: str | None = Field(default=None)
    opensearch_auth_password: str | None = Field(default=None)

    prometheus_timeout_seconds: float = Field(default=15.0)
    opensearch_timeout_seconds: float = Field(default=15.0)

    # --- Grafana (alert-rule creation -- SKILL.md Section 12) ---
    alert_rule_creation_enabled: bool = Field(
        default=False,
        description="Feature flag for the alert-rule-creation capability (SKILL.md Section "
                    "12). Defaults to OFF: while False, the Router/Generator prompts are "
                    "built WITHOUT the alert-rule-creation addendum at all (see "
                    "pipeline.py's _build_router_instructions/_build_generator_instructions), "
                    "so an alert-creation request is classified exactly as it was before "
                    "this capability existed (out_of_scope_action) -- there is no partial "
                    "or inconsistent state. Turning this on does not by itself expose "
                    "anything to end users beyond a PROPOSAL that still requires a separate, "
                    "explicit confirmation call (see app/api/routes_alerts.py); it only "
                    "controls whether that proposal step is reachable at all.",
    )
    dependent_query_resolution_enabled: bool = Field(
        default=False,
        description="Feature flag for staged resolution of compound queries whose later "
                    "intent depends on an entity selected by an earlier query. Defaults "
                    "to OFF so ordinary Router/Generator prompts and the legacy flat "
                    "execution path remain byte-for-byte unchanged until a deployment "
                    "has explicitly opted in.",
    )
    grafana_url: str = Field(
        default="http://localhost:3000",
        description="Base URL of the Grafana instance alert rules are created against. "
                    "Required (and must point at a real, reachable Grafana) only if "
                    "alert_rule_creation_enabled is True.",
    )
    grafana_service_account_token: str | None = Field(
        default=None,
        description="Grafana service-account token used to authenticate provisioning-API "
                    "requests (Authorization: Bearer <token>). Required if "
                    "alert_rule_creation_enabled is True; grafana_client.py fails closed "
                    "(returns a clear configuration-error outcome, never a silent no-op) "
                    "if this is unset while the feature is enabled.",
    )
    grafana_default_folder_uid: str | None = Field(
        default=None,
        description="Grafana folder UID that newly-created alert rules are provisioned "
                    "into when the request doesn't (and SKILL.md Section 12.5 says it "
                    "never does) specify one itself. Required if alert_rule_creation_enabled "
                    "is True.",
    )
    grafana_default_datasource_uid: str | None = Field(
        default=None,
        description="Grafana UID of the Prometheus datasource alert rule conditions are "
                    "evaluated against -- this MUST be a datasource already configured in "
                    "Grafana and pointing at the same Prometheus this backend queries via "
                    "prometheus_url, or a proposed rule's condition_query will be evaluated "
                    "against the wrong series entirely. Required if "
                    "alert_rule_creation_enabled is True; resolved at confirmation time only "
                    "(app/grafana_client.py), never by the Router or Generator (Section "
                    "12.5's datasource_uid: null rule).",
    )
    grafana_timeout_seconds: float = Field(default=15.0)

    # --- Response safety limits ---
    max_points_per_series: int = Field(default=11_000,
                                        description="Prometheus's own default sample cap; also applied "
                                                     "as the OpenSearch date_histogram bucket-count guard.")
    max_series_per_result: int = Field(default=200,
                                        description="Hard cap on series/buckets returned per result, "
                                                     "protecting the frontend from an unbounded "
                                                     "by(...)/terms grouping.")
    max_hits_per_result: int = Field(default=500)

    # --- HTTP API ---
    api_host: str = Field(default="0.0.0.0")
    api_port: int = Field(default=8008)
    cors_allow_origins: list[str] = Field(default_factory=lambda: ["*"])
    api_key: str | None = Field(default=None,
                                 description="If set, required as the X-API-Key header on every "
                                             "request to this service. Unset (None) means open access "
                                             "-- fine for local dev, set this before deploying anywhere "
                                             "reachable outside your own machine.")

    # --- Authentik OpenID Connect authentication ---
    # The browser authenticates against Authentik with the OIDC
    # Authorization Code flow. This service is the OIDC *relying party*: it
    # redirects the browser to Authentik, validates the returned ID token and
    # keeps the resulting session in a signed, HttpOnly cookie.
    #
    # The client secret and the session signing secret never leave the
    # backend: they are read from the environment and are never returned to
    # the browser.
    #
    # Authentik establishes identity only. It never assigns application
    # roles, scopes or permissions: those are the responsibility of a later
    # authorization layer, which consumes the verified identity exposed on
    # the request principal.
    authentik_enabled: bool = Field(
        default=False,
        description="Require an authenticated identity on the protected API endpoints. "
                    "When True, a request must carry a valid application-session cookie "
                    "that was established through the OIDC Authorization Code flow. When "
                    "False the service runs in an explicit DEVELOPMENT-ONLY mode with no "
                    "authentication at all -- never use that mode in production.",
    )
    authentik_base_url: str | None = Field(
        default=None,
        description="Browser-visible base URL of the Authentik instance, e.g. "
                    "http://localhost:9000. Used to build the link to Authentik's "
                    "enrollment (sign-up) and password-change flows. Not a secret.",
    )
    authentik_app_slug: str | None = Field(
        default="grafana-ai-agent",
        description="Slug of the Authentik application the OIDC provider belongs to. "
                    "Documented for the operator: the discovery URL embeds it. The "
                    "application itself does not depend on it at runtime.",
    )
    authentik_oidc_discovery_url: str | None = Field(
        default=None,
        description="OIDC discovery document of the Authentik provider, e.g. "
                    "http://localhost:9000/application/o/<application-slug>/"
                    ".well-known/openid-configuration. Not a secret.",
    )
    authentik_oidc_client_id: str | None = Field(
        default=None,
        description="OIDC client id of the Authentik application. Required when "
                    "authentik_enabled is True.",
    )
    authentik_oidc_client_secret: str | None = Field(
        default=None,
        description="OIDC client secret of the Authentik application (a confidential "
                    "client). Required when authentik_enabled is True. This value stays "
                    "on the server and is never sent to the browser.",
    )
    authentik_oidc_redirect_uri: str | None = Field(
        default="http://localhost:5173/api/auth/callback",
        description="Exact redirect URI registered in the Authentik provider. It must "
                    "match the URL the browser is sent back to, character for character, "
                    "including scheme, host, port and path.",
    )
    authentik_oidc_scope: str = Field(
        default="openid profile email",
        description="Scopes requested in the authorization request. 'openid' is "
                    "mandatory; 'email' is needed for the profile menu.",
    )
    authentik_oidc_pkce: bool = Field(
        default=True,
        description="Use PKCE (S256) in the authorization request. The Authentik "
                    "provider supports it; leaving it on is the safer default.",
    )
    authentik_oidc_http_timeout_seconds: float = Field(
        default=10.0,
        description="Timeout for the OIDC discovery, JWKS and token requests.",
    )
    authentik_session_secret: str | None = Field(
        default=None,
        description="Secret used to sign the application-session cookie. Required when "
                    "authentik_enabled is True. Generate one with: openssl rand -hex 32. "
                    "Never send this value to the browser.",
    )
    authentik_session_max_age_seconds: int = Field(
        default=3600,
        description="Lifetime of the application session (the signed, HttpOnly cookie).",
    )
    authentik_session_cookie_name: str = Field(
        default="grafana_ai_session",
        description="Name of the application-session cookie.",
    )
    authentik_session_cookie_secure: bool = Field(
        default=False,
        description="Set the Secure flag on the session cookie. Must be True when the "
                    "application is served over HTTPS; it is False for the local "
                    "http://localhost development setup.",
    )
    authentik_session_cookie_samesite: str = Field(
        default="lax",
        description="SameSite attribute of the session cookie. 'lax' is required for the "
                    "cookie to survive the top-level redirect back from Authentik.",
    )
    authentik_enrollment_flow: str = Field(
        default="default-enrollment-flow",
        description="Slug of the Authentik enrollment flow used for self-service sign-up. "
                    "Authentik ships such a flow as the example blueprint "
                    "'example/flows-enrollment-2-stage.yaml' (designation: enrollment, "
                    "slug: default-enrollment-flow).",
    )
    authentik_signup_url: str | None = Field(
        default=None,
        description="Explicit URL of Authentik's enrollment (sign-up) flow. Overrides the URL "
                    "derived from authentik_base_url and authentik_enrollment_flow. Use this "
                    "when the flow needs query parameters (for example an invitation link). "
                    "Not a secret.",
    )
    authentik_password_change_flow: str = Field(
        default="default-password-change",
        description="Slug of the Authentik flow that lets a signed-in user change their own "
                    "password (it contains the 'Password change' stage). The flow must be "
                    "linked to the identity provider's user-settings flow so that it is "
                    "reachable for an ordinary user.",
    )
    authentik_password_change_url: str | None = Field(
        default=None,
        description="Explicit URL of Authentik's password-change flow. Overrides the URL "
                    "derived from authentik_base_url and authentik_password_change_flow. "
                    "Not a secret.",
    )

    # --- Session store (clarification / multi-turn flow) ---
    session_ttl_seconds: int = Field(default=600)

    # --- Pipeline ---
    label_discovery_lookback_hours: int = Field(default=6)
    pipeline_timeout_seconds: float = Field(default=45.0,
                                             description="Overall budget across all LLM phases for one "
                                                          "request, independent of each call's own retry/"
                                                          "backoff.")

    @property
    def opensearch_auth(self) -> tuple[str, str] | None:
        if self.opensearch_auth_username and self.opensearch_auth_password:
            return (self.opensearch_auth_username, self.opensearch_auth_password)
        return None


_settings: Settings | None = None


def get_settings() -> Settings:
    """Process-wide singleton -- constructed once, reused everywhere. Tests
    that need different settings should construct Settings(...) directly
    rather than mutating this singleton."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
