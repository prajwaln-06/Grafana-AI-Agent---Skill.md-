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
    opensearch_url: str = Field(default="http://localhost:9200",
                                description="OpenSearch REST endpoint (port 9200; 9600 is the "
                                            "Performance Analyzer port, not the search API).")
    opensearch_auth_username: str | None = Field(default=None)
    opensearch_auth_password: str | None = Field(default=None)

    # --- OpenSearch registry / runtime field catalog (app/opensearch_catalog) ---
    opensearch_registry_path: Path | None = Field(
        default=None,
        description="Index registry file. None = the packaged "
                    "app/opensearch_catalog/index_registry.json.")
    opensearch_field_catalog_ttl_seconds: float = Field(default=600.0, gt=0)
    opensearch_field_catalog_stale_ttl_seconds: float = Field(
        default=3600.0, gt=0,
        description="How long a previously discovered catalog may still be served "
                    "(marked stale) when a refresh fails. Must be >= the TTL.")
    opensearch_field_catalog_max_indices: int = Field(
        default=7, ge=1, description="Newest N daily indices merged per pattern.")
    opensearch_field_catalog_failure_backoff_seconds: float = Field(default=30.0, ge=0)

    # --- OpenSearch query limits (enforced in Python by opensearch_catalog.guard) ---
    opensearch_default_hits_size: int = Field(default=20, ge=1)
    opensearch_max_hits_size: int = Field(default=100, ge=1)
    opensearch_default_terms_size: int = Field(default=10, ge=1)
    opensearch_max_terms_size: int = Field(default=50, ge=1)
    opensearch_max_histogram_buckets: int = Field(default=200, ge=1)
    opensearch_search_timeout: str = Field(
        default="10s", pattern=r"^[1-9][0-9]*(ms|s)$",
        description="Server-side search timeout added to every OpenSearch query "
                    "(an expired timeout yields a partial result that is flagged).")

    # --- OpenSearch result redaction (opensearch_catalog.redaction) ---
    opensearch_redact_usernames: bool = Field(default=True)
    opensearch_redact_ip_mode: str = Field(
        default="mask_all", pattern=r"^(mask_all|keep_internal|off)$",
        description="mask_all | keep_internal (keep private/loopback addresses) | off.")

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

    # --- Metric catalog integration (final architecture: Batches 1-3, now
    # mandatory as of Batch 4 Phase 9) ---
    # Catalog participation is a permanent part of the normal query
    # pipeline, not an opt-in feature: `_get_catalog` is now called
    # unconditionally in run_pipeline (see its own docstring), Router-level
    # narrowing (_maybe_narrow_section4) and Generator-level candidate
    # construction (_build_metric_metadata, Level-1/2/3) both run whenever
    # a catalog successfully loads, with no separate on/off switch. A
    # missing/corrupt catalog.json still degrades every one of these to
    # exactly their pre-catalog behavior (full Section 4, no candidate
    # block, no Level-1/2/3) rather than crashing or blocking a request --
    # "catalog integration is additive" remains true, but it is no longer
    # conditioned on a deployment opting in.
    #
    # Two migration-only switches that used to gate this behavior were
    # removed once Batches 1-3 were validated and approved:
    #   - `catalog_shadow_mode_enabled` (Phase 8): a purely observational
    #     comparison of the catalog's opinion vs. the Router's actual
    #     choice, whose only purpose was validating catalog-assisted
    #     routing DURING migration. It never affected any response, and
    #     had nothing left to compare once assisted routing became
    #     unconditional. See app/pipeline.py's module docstring for the
    #     historical note.
    #   - `catalog_assisted_routing_enabled` / `catalog_generator_
    #     candidates_enabled`: the "is the new Catalog-based architecture
    #     turned on for this deployment yet" switches. Both are now
    #     permanent, unconditional behavior; keeping them around after
    #     Batch 3's approval would have meant a permanent, dead
    #     `if catalog_enabled: new architecture else: old architecture`
    #     branch in the normal query pipeline, which the final
    #     architecture explicitly does not have.
    catalog_path: Path = Field(
        default=Path("app/catalog/catalog.json"),
        description="Path to the generated metric catalog (produced by "
                    "scripts/generate_production_catalog.py from the vendor metric document; "
                    "see app/catalog/vendor_docx.py), loaded lazily on first use and cached for "
                    "the life of the process for the normal query path (Router-level "
                    "narrowing and Generator-level candidate construction; see "
                    "app/pipeline.py's `_get_catalog`). A missing or invalid file at this path "
                    "never crashes the pipeline -- the whole catalog-narrowing/candidate/"
                    "fallback architecture silently disables itself (logged once) and every "
                    "request behaves exactly as it would with no catalog at all: full "
                    "unnarrowed Section 4, no Generator candidate block, no Level-1/2/3.",
    )
    catalog_narrow_min_score: float = Field(
        default=2.0,
        description="Minimum score the TOP catalog-search candidate must clear before "
                    "Router-level Section 4 narrowing or Generator-level Level-1 candidate "
                    "construction will act on it at all; a non-empty search result below this "
                    "floor is treated exactly like a catalog miss (full, unnarrowed Section 4; "
                    "no Level-1 candidate block). The review finding this exists for was that "
                    "'a non-empty catalog search result is not necessarily a safe routing "
                    "result' -- this is the conservative half of that fix (the other half is "
                    "that narrowing/candidate search itself does not truncate by rank; see "
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
        description="When True, app/validator.py's deterministic checks additionally look up "
                    "each Prometheus-backed query/alert metric's catalog status and reject the "
                    "metric outright if that status is 'discovered_pending_review' or "
                    "'rejected' -- even if the same metric name also happens to appear in a "
                    "Metric Directory this request opened (the existing `known_metrics` "
                    "check). This is strictly additive/supplementary: it can only make an "
                    "already-known metric MORE restricted, never less -- it never overrides a "
                    "`known_metrics` rejection, and it never makes a fabricated metric name "
                    "valid. Deliberately kept as a genuine, independent opt-in capability (NOT "
                    "removed alongside the migration flags above): unlike Router narrowing or "
                    "Generator candidates, this is not 'old vs new pipeline shape' -- it is an "
                    "optional stricter enforcement rule a deployment may reasonably not want on "
                    "immediately (e.g. one whose catalog.json currently has many entries still "
                    "sitting in `discovered_pending_review`). While False (the default), "
                    "validator.py's known_metrics behavior is byte-for-byte what it was before "
                    "this flag existed. If the catalog fails to load, or this deployment has no "
                    "catalog at all, this degrades to 'no catalog-status information available' "
                    "(validation proceeds on known_metrics alone) rather than rejecting every "
                    "metric or crashing the request -- consistent with every other catalog "
                    "setting's 'catalog integration is additive, never a new single point of "
                    "failure' convention.",
    )

    catalog_reviews_path: Path = Field(
        default=Path("app/catalog/reviews.json"),
        description="Path to the human-review store (explicit approve/reject decisions, "
                    "see app/catalog/reviews.py). Separate from catalog_path: the vendor "
                    "catalog is never edited by reviews. A MISSING file means 'no decisions "
                    "yet'; a file that exists but is malformed or self-conflicting makes the "
                    "runtime refresh fail closed (the previous snapshot stays in place). In a "
                    "container this must live on a mounted volume shared with whoever runs "
                    "scripts/review_catalog.py, or decisions are lost on redeploy.",
    )

    catalog_runtime_reconciliation_enabled: bool = Field(
        default=False,
        description="When True, the server starts a background thread (app/catalog/runtime.py) "
                    "that discovers metrics from prometheus_url at startup and every "
                    "catalog_runtime_refresh_seconds, reconciles them with the vendor catalog and "
                    "review decisions, and publishes the result as the catalog the query pipeline "
                    "uses (statuses approved / approved_unavailable / discovered_pending_review / "
                    "rejected). Also turns on catalog-status validation (see "
                    "catalog_metric_status_validation_enabled), because the statuses it enforces "
                    "only exist once this is on. Discovery never runs inside a user request, and a "
                    "failed discovery keeps the last good snapshot. Defaults to OFF: the pipeline "
                    "then uses the vendor catalog plus review decisions, with no availability data.",
    )
    catalog_runtime_refresh_seconds: float = Field(
        default=300.0,
        ge=1.0,
        description="Seconds between runtime reconciliation refreshes (only used when "
                    "catalog_runtime_reconciliation_enabled is True). Each refresh makes two "
                    "Prometheus API calls (label values + metadata).",
    )

    # --- Runtime metric discovery scope (app/catalog/reconciler.py) ---
    catalog_discovery_include_patterns: list[str] = Field(
        default_factory=list,
        description="Shell-style globs (case-sensitive), e.g. [\"node_*\", \"DCGM_*\"]. When "
                    "non-empty, only runtime-discovered metrics matching at least one pattern "
                    "are queued as 'discovered_pending_review'. Empty (default) = no include "
                    "restriction. Env var form is a JSON list. Affects ONLY which new, unknown "
                    "metrics enter the review queue -- never the availability of vendor/"
                    "approved metrics, and never a human rejection.",
    )
    catalog_discovery_exclude_patterns: list[str] = Field(
        default_factory=list,
        description="Shell-style globs for runtime-discovered metrics that must never be "
                    "queued for review, e.g. [\"go_*\", \"process_*\", \"scrape_*\"] to keep "
                    "Prometheus's own and generic client-library metrics out of the review "
                    "queue. Exclusion wins over inclusion. Empty (default) = nothing excluded. "
                    "Nothing is excluded by default because which metrics are noise depends on "
                    "the deployment.",
    )

    # --- Dependency-aware compound query resolution ---
    dependent_query_resolution_enabled: bool = Field(
        default=False,
        description="Feature flag for the opt-in path (see app/pipeline.py's module "
                    "docstring, 'Dependency-aware compound query resolution') that lets a "
                    "compound question's later query reuse the resolved result of an "
                    "earlier query in the SAME question -- e.g. 'which node has the "
                    "highest CPU utilization, and how much memory is available on that "
                    "node?'. Defaults to OFF: while False, the Router prompt, the "
                    "Generator prompt, the existing one-shot construction flow, and the "
                    "flat (single-executor-pass) execution flow are all kept byte-for-"
                    "byte unchanged -- there is zero prompt or control-flow change for a "
                    "deployment that hasn't opted in. When True, the Router may annotate "
                    "each matched reference with `intent_id`/`depends_on`; a genuine, "
                    "validated dependency plan is resolved in stages (root intents, then "
                    "each dependent intent generated/validated/executed once its parent's "
                    "data is available) instead of a single flat Generator call, and the "
                    "pipeline executes the result itself before returning (see "
                    "`pipeline.ALREADY_EXECUTED_MARKER`, checked by app/agent.py so it "
                    "never re-executes the same queries a second time). Invalid or absent "
                    "dependency metadata for a given question -- incomplete tagging, an "
                    "unknown `depends_on` id, a self-dependency, a cycle, or simply no "
                    "question that declares one -- always degrades to exactly the "
                    "existing flat path for that request, never a crash or a blocked "
                    "request.",
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
