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

    # --- Metric catalog integration (Batch 3, Phases 7-9) ---
    # Both flags follow the exact same "flag off = zero behavior change"
    # convention alert_rule_creation_enabled already established above:
    # default False, and while False the corresponding pipeline.py code
    # path is never exercised at all (no catalog load, no extra search
    # call, no prompt-content change) -- not merely "exercised but
    # defaulting to a no-op."
    catalog_path: Path = Field(
        default=Path("app/catalog/catalog.json"),
        description="Path to the generated metric catalog (see app/catalog/generator.py). "
                    "Only read at all if catalog_shadow_mode_enabled or "
                    "catalog_assisted_routing_enabled is True. A missing or invalid file at "
                    "this path never crashes the pipeline -- both features silently disable "
                    "themselves (logged once) if the catalog can't be loaded.",
    )
    catalog_shadow_mode_enabled: bool = Field(
        default=False,
        description="Phase 8. When True, every non-gate_stop request also runs catalog "
                    "search (app/catalog/search.py) alongside the real Router call and logs "
                    "a comparison between the Router's actual matched_references and what "
                    "the catalog would have suggested. Purely observational: this NEVER "
                    "changes the Router's prompt, the Generator's prompt, or the response "
                    "returned to the caller, and a failure inside the shadow comparison "
                    "itself is caught and logged, never allowed to affect the real request.",
    )
    catalog_assisted_routing_enabled: bool = Field(
        default=False,
        description="Phase 9. When True, the Router's SKILL.md Section 4 (routing table) "
                    "prompt content is narrowed, per-question, to catalog-suggested domain "
                    "rows -- see pipeline.py's _maybe_narrow_section4. Narrowing only ever "
                    "applies to routing rows whose reference_path is one the catalog "
                    "actually has an opinion about (today: the 8 node-exporter/dcgm-exporter "
                    "domain files); every other row (overview.md rows, *-fundamentals.md "
                    "rows, execution-contract.md) is always kept regardless of catalog "
                    "results, and a catalog miss OR a low-confidence hit (see "
                    "catalog_narrow_min_score below) always falls back to the full, "
                    "unnarrowed Section 4 -- never treated as 'nothing routes here'. "
                    "Independent of catalog_shadow_mode_enabled.",
    )
    catalog_narrow_min_score: float = Field(
        default=2.0,
        description="Phase 10 (Batch 4). Minimum score the TOP catalog-search candidate must "
                    "clear before catalog_assisted_routing_enabled will narrow Section 4 at "
                    "all; a non-empty search result below this floor is treated exactly like "
                    "a catalog miss (full, unnarrowed Section 4). Batch 4's review finding was "
                    "that 'a non-empty catalog search result is not necessarily a safe routing "
                    "result' -- this is the conservative half of that fix (the other half is "
                    "that narrowing itself no longer truncates by rank; see "
                    "pipeline.py's _NARROW_SEARCH_TOP_N). The default (2.0) is set relative to "
                    "search.py's own field weights: it rules out a candidate whose ENTIRE score "
                    "comes from a single incidental 'help' or 'exporter' token match (weight "
                    "1.0 each, search.py's own docstring calls free-prose help text 'most "
                    "likely to contain incidental word overlap'), while still passing any "
                    "single category match (weight 2.0) or stronger. It intentionally does NOT "
                    "attempt to fix moderate-scoring false positives from genuine name/keyword "
                    "overlap on the wrong metric (e.g. a query containing the word 'total' "
                    "matching an unrelated *_TOTAL metric name) -- that is a scoring-precision "
                    "problem for a future weight-tuning phase, not something a single global "
                    "threshold can safely resolve without also cutting correct matches that "
                    "score just as low. See scripts/evaluate_catalog_retrieval.py's "
                    "reference-level report for the measured breakdown.",
    )
    catalog_metric_status_validation_enabled: bool = Field(
        default=False,
        description="Phase 12 (Batch 4). When True, app/validator.py's deterministic checks "
                    "additionally look up each Prometheus-backed query/alert metric's catalog "
                    "status and reject the metric outright if that status is "
                    "'discovered_pending_review' or 'rejected' -- even if the same metric name "
                    "also happens to appear in a Metric Directory this request opened (the "
                    "existing `known_metrics` check). This is strictly additive/supplementary: "
                    "it can only make an already-known metric MORE restricted, never less -- it "
                    "never overrides a `known_metrics` rejection, and it never makes a fabricated "
                    "metric name valid. While False (the default), the catalog is not even loaded "
                    "for this purpose (unless catalog_shadow_mode_enabled or "
                    "catalog_assisted_routing_enabled is also True for its own reasons) and "
                    "validator.py's known_metrics behavior is byte-for-byte what it was before "
                    "this flag existed. If the catalog fails to load, or this deployment has no "
                    "catalog at all, this degrades to 'no catalog-status information available' "
                    "(validation proceeds on known_metrics alone) rather than rejecting every "
                    "metric or crashing the request -- consistent with every other catalog "
                    "setting's 'catalog integration is additive, never a new single point of "
                    "failure' convention.",
    )

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
