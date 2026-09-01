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
    gemini_api_key: str = Field(..., description="Google Gemini API key.")
    gemini_model: str = Field(default="gemini-3.5-flash-lite")

    # --- Skill package ---
    skills_root: Path = Field(default=Path("skills"),
                               description="Directory that directly contains SKILL.md.")

    # --- Backends ---
    prometheus_url: str = Field(default="http://localhost:9090")
    opensearch_url: str = Field(default="http://localhost:9600")
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
    api_port: int = Field(default=8000)
    cors_allow_origins: list[str] = Field(default_factory=lambda: ["*"])
    api_key: str | None = Field(default=None,
                                 description="If set, required as the X-API-Key header on every "
                                             "request to this service. Unset (None) means open access "
                                             "-- fine for local dev, set this before deploying anywhere "
                                             "reachable outside your own machine.")

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
