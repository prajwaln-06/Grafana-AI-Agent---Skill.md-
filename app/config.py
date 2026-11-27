"""
config.py

Typed configuration, loaded once from environment variables (see
.env.example for the full list). Using pydantic's BaseSettings means a
missing/malformed required value fails loudly at startup with a clear
message, instead of surfacing as a confusing error three phases into a
request.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class GeminiTransportSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    gemini_http_timeout_seconds: float = Field(
        default=60.0, gt=0, description="Default Gemini HTTP timeout per request in seconds.")
    gemini_max_attempts: int = Field(
        default=3, ge=1, description="Maximum Gemini request attempts following transient or JSON parse failures.")


@lru_cache(maxsize=1)
def get_gemini_transport_settings() -> GeminiTransportSettings:
    return GeminiTransportSettings()


class Settings(GeminiTransportSettings):
    # --- LLM ---
    gemini_api_key: str = Field(..., description="Google Gemini API key.")
    gemini_model: str = Field(default="gemini-3.5-flash-lite")
    gemini_router_thinking_budget: int | None = Field(
        default=None, ge=0, description="Optional Gemini thinking budget for the shared Router.")
    gemini_generator_thinking_budget: int | None = Field(
        default=None, ge=0, description="Optional thinking budget for OpenSearch-only read generation.")
    gemini_generator_max_output_tokens: int | None = Field(
        default=8192, ge=1, description="Output token limit for OpenSearch-only read generation.")
    gemini_generator_timeout_seconds: float | None = Field(
        default=None, gt=0, description="Optional HTTP timeout override for OpenSearch-only read generation.")

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
    opensearch_router_fastpath: bool = Field(
        default=False, description="Skip the Router for simple reads naming one registered OpenSearch family when the Prometheus catalog has no candidate.")
    opensearch_field_catalog_ttl_seconds: float = Field(default=600.0, gt=0)
    opensearch_field_catalog_stale_ttl_seconds: float = Field(
        default=3600.0, gt=0,
        description="How long a previously discovered catalog may still be served "
                    "(marked stale) during background refresh or after failure. Must be >= the TTL.")
    opensearch_field_catalog_max_indices: int = Field(
        default=7, ge=1, description="Newest N daily indices merged per pattern.")
    opensearch_field_catalog_failure_backoff_seconds: float = Field(default=30.0, ge=0)

    # --- OpenSearch query limits (enforced in Python by opensearch_catalog.guard) ---
    opensearch_text_default_msm_enabled: bool = Field(
        default=True, description="Require a minimum of distinct meaningful keywords for unordered text searches.")
    opensearch_text_default_msm_ratio: float = Field(
        default=0.75, gt=0, le=1,
        description="Fraction of distinct keywords required by default, rounded down and bounded to 2-10.")
    opensearch_rank_text_matches: bool = Field(
        default=True, description="Rank text-search rows by relevance, then newest, unless a sort was specified.")
    opensearch_default_hits_size: int = Field(default=20, ge=1)
    opensearch_max_hits_size: int = Field(default=100, ge=1)
    opensearch_default_terms_size: int = Field(default=10, ge=1)
    opensearch_max_terms_size: int = Field(default=50, ge=1)
    opensearch_max_histogram_buckets: int = Field(default=200, ge=1)
    opensearch_search_timeout: str = Field(
        default="10s", pattern=r"^[1-9][0-9]*(ms|s)$",
        description="Server-side search timeout added to every OpenSearch query "
                    "(an expired timeout yields a partial result that is flagged).")

    # --- OpenSearch multi-step analyses (opensearch_catalog.analysis) ---
    opensearch_analysis_max_terms: int = Field(
        default=500, ge=1,
        description="Terms-aggregation size allowed ONLY for the 'absence' analysis (who stopped "
                    "reporting), where a truncated entity list would give a wrong answer. Every "
                    "other query keeps opensearch_max_terms_size.")
    opensearch_analysis_anchor_limit: int = Field(
        default=3, ge=1,
        description="At most this many matching events are expanded by a 'context_window' "
                    "(before/after) analysis; each costs one extra bounded query.")
    opensearch_analysis_context_max_minutes: int = Field(
        default=60, ge=1,
        description="Longest span, before or after an event, a 'context_window' analysis may "
                    "ask for. A longer span is rejected, never shortened.")

    # --- OpenSearch result redaction (opensearch_catalog.redaction) ---
    opensearch_redact_usernames: bool = Field(default=True)
    opensearch_redact_ip_mode: str = Field(
        default="mask_all", pattern=r"^(mask_all|keep_internal|off)$",
        description="mask_all | keep_internal (keep private/loopback addresses) | off.")

    # --- OpenSearch RESULT cache (opensearch_catalog.query_cache) ---
    # Not the field-catalog cache above: this one stores completed, redacted ANSWERS so a repeated
    # or reworded question skips the Router/Generator LLM calls and every OpenSearch search.
    opensearch_result_cache_enabled: bool = Field(
        default=True,
        description="Reuse a completed OpenSearch answer for an identical or equivalent question "
                    "within the TTL. Only complete, successful, non-partial answers are ever stored; "
                    "nothing is written to disk. False = every question runs the full pipeline.")
    opensearch_result_cache_ttl_seconds: float = Field(
        default=60.0, gt=0, le=3600,
        description="Fixed lifetime of a stored answer (reads never extend it). An answer about a "
                    "relative window ('now-3m') lives at most 10% of its narrowest span, so it can "
                    "be shorter than this, never longer.")
    opensearch_result_cache_max_entries: int = Field(
        default=200, ge=1, le=10000,
        description="Capacity. When full, expired entries go first, then the oldest.")
    opensearch_result_cache_semantic_enabled: bool = Field(
        default=True,
        description="Allow a DIFFERENTLY WORDED question to reuse an answer (Level 2). This is "
                    "deterministic rule-based matching, not embeddings or a similarity score: the two "
                    "questions must agree exactly in operation, entity scope, concepts, literals, time "
                    "window and every leftover word. False = identical wording only (Level 1). "
                    "ALSO governs the query-plan cache's semantic matching "
                    "(OPENSEARCH_QUERY_PLAN_CACHE_ENABLED); that cache additionally refuses a reworded "
                    "question whenever ANY other enabled pattern could own it: it grounds the entities the "
                    "question names, the question names none, or it declares a conflicting alias.")
    opensearch_result_cache_aliases: dict[str, dict[str, str]] = Field(
        default_factory=dict,
        description="Operator-declared synonyms per index family (pattern_id), e.g. "
                    "{\"fabric_health\": {\"node\": \"switch\", \"down\": \"stopped sending heartbeats\"}} "
                    "to assert that in that family 'nodes are down' means 'switches stopped sending "
                    "heartbeats'. Empty by default: no equivalence is assumed that was not declared. "
                    "Applied only to entries of that single family. The query-plan cache reads the same "
                    "aliases (it has none of its own) and also treats another family's alias for a word "
                    "in the question as a conflict (no semantic reuse).")

    # --- OpenSearch QUERY-PLAN cache (opensearch_catalog.plan_cache) ---
    # Independent of the result cache above: this one stores validated query PLANS (never results), so an
    # identical or equivalent question skips the Router/Generator LLM calls but is re-validated against
    # the current registry / live mapping / config / skill text, re-run through the guard, and EXECUTED
    # AGAIN for fresh data. Matching rules (and the semantic switch / aliases) are the result cache's.
    opensearch_query_plan_cache_enabled: bool = Field(
        default=True,
        description="Reuse a validated OpenSearch query plan for an identical or equivalent question, "
                    "skipping the LLM calls but always re-validating and re-executing it. In memory only. "
                    "False = every result-cache miss runs the full Router/Generator pipeline. Independent of "
                    "OPENSEARCH_RESULT_CACHE_ENABLED. Its semantic matching and aliases are "
                    "OPENSEARCH_RESULT_CACHE_SEMANTIC_ENABLED / OPENSEARCH_RESULT_CACHE_ALIASES, shared "
                    "on purpose: one place declares what counts as an equivalent question.")
    opensearch_query_plan_cache_max_entries: int = Field(
        default=500, ge=1, le=10000,
        description="Capacity of the plan cache. When full the least recently used plan is evicted. "
                    "Plans are valid while their fingerprints and the guard say so, so there is no TTL.")

    # --- Prometheus QUERY-PLAN cache (app/prometheus_plan_cache.py) ---
    # The Prometheus counterpart of the OpenSearch plan cache above. It stores validated PromQL plans
    # (never results): an identical or equivalent question skips the Router/Generator LLM calls but is
    # re-validated against the CURRENT catalog / live labels / config / skill text by the real validator
    # and EXECUTED AGAIN against Prometheus. Entirely independent of the OpenSearch caches: its own
    # instance, its own switches and its own aliases (nothing is shared with OPENSEARCH_*).
    prometheus_query_plan_cache_enabled: bool = Field(
        default=False,
        description="Reuse a validated PromQL plan for an identical or equivalent question, skipping the "
                    "Router/Generator LLM calls but always re-validating and re-executing it (Prometheus "
                    "is queried again on every hit; results are never cached). In memory only. False (the "
                    "default: opt-in, so existing behaviour is unchanged) = every question runs the full "
                    "pipeline and responses carry no query_plan_cache metadata. Independent of "
                    "OPENSEARCH_QUERY_PLAN_CACHE_ENABLED and OPENSEARCH_RESULT_CACHE_ENABLED.")
    prometheus_query_plan_cache_max_entries: int = Field(
        default=500, ge=1, le=10000,
        description="Capacity of the Prometheus plan cache. When full the least recently used plan is "
                    "evicted. Plans stay valid while their fingerprints and the validator say so, so "
                    "there is deliberately no TTL.")
    prometheus_query_plan_cache_semantic_enabled: bool = Field(
        default=True,
        description="Allow a DIFFERENTLY WORDED question to reuse a Prometheus plan. Deterministic "
                    "rule-based matching (the OpenSearch result cache's matcher), not embeddings: the "
                    "two questions must agree in operation, entities, concepts, literals, time window "
                    "and every leftover word, and the catalog must still point the new wording at the "
                    "plan's metrics and reference. False = identical wording only. Separate from "
                    "OPENSEARCH_RESULT_CACHE_SEMANTIC_ENABLED.")
    prometheus_query_plan_cache_aliases: dict[str, dict[str, str]] = Field(
        default_factory=dict,
        description="Operator-declared synonyms per exporter family (the directory name under "
                    "skills/references, e.g. \"node-exporter\"), e.g. "
                    "{\"node-exporter\": {\"usage\": \"utilization\"}} to assert that for that exporter "
                    "'usage' and 'utilization' mean the same thing. Empty by default: no equivalence is "
                    "assumed that was not declared beyond the built-in concept vocabulary "
                    "(PROMETHEUS_QUERY_PLAN_CACHE_CONCEPTS_ENABLED). Aliases are applied first, so they "
                    "override or extend it for that exporter. Applied only to plans of that exporter. "
                    "Separate from OPENSEARCH_RESULT_CACHE_ALIASES (which are keyed by OpenSearch index "
                    "family).")

    prometheus_query_plan_cache_concepts_enabled: bool = Field(
        default=True,
        description="Use the built-in Prometheus concept vocabulary (app/prometheus_concepts.py) so common "
                    "wordings of one measurement are recognised without declaring them as aliases: "
                    "\"cpu usage\" / \"cpu utilization\" / \"processor usage\", \"memory usage\" / "
                    "\"memory consumption\" / \"ram usage\", \"network traffic\" / \"network throughput\", "
                    "\"disk read rate\" / \"disk read throughput\", and similar. Phrase-level and "
                    "domain-aware (\"cpu load\" is NOT \"cpu utilization\"). It only makes a reworded "
                    "question ELIGIBLE: the six-facet signature equality, the catalog ownership check, the "
                    "plan's metric check and full revalidation still decide, and Prometheus is queried "
                    "again on every hit. Needs PROMETHEUS_QUERY_PLAN_CACHE_SEMANTIC_ENABLED. False = only "
                    "PROMETHEUS_QUERY_PLAN_CACHE_ALIASES bridge wordings (the previous behaviour). "
                    "Never affects the OpenSearch caches.")

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
                    "explicit confirmation call (see POST /api/v1/alerts/confirm in run_server.py); it only "
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
                                        description="Prometheus's own default sample cap, applied to "
                                                     "Prometheus range queries only. The OpenSearch "
                                                     "date_histogram bucket limit is "
                                                     "opensearch_max_histogram_buckets.")
    max_series_per_result: int = Field(default=200,
                                        description="Hard cap on series/buckets returned per result, "
                                                     "protecting the frontend from an unbounded "
                                                     "by(...)/terms grouping.")
    max_hits_per_result: int = Field(default=500)

    # --- HTTP API ---
    api_host: str = Field(default="0.0.0.0")
    api_port: int = Field(default=8000)
    cors_allow_origins: list[str] = Field(
        default_factory=lambda: ["*"],
        description="Browser origins allowed to call this API (CORS). Env form is a JSON list, "
                    "e.g. [\"https://ui.example.com\"]. \"*\" is only sensible for local use.")
    api_key: str | None = Field(default=None,
                                 description="If set, required as the X-API-Key header on every "
                                             "request to this service. Unset (None) means open access "
                                             "-- fine for local dev, set this before deploying anywhere "
                                             "reachable outside your own machine.")

    # --- Session store (clarification / multi-turn flow) ---
    session_ttl_seconds: int = Field(default=600)

    # --- Pipeline ---
    label_discovery_lookback_hours: int = Field(default=6)
    label_discovery_cache_ttl_seconds: float = Field(
        default=300.0, ge=0.0,
        description="How long a successfully discovered Prometheus label set per metric is "
                    "reused. Failures are never cached. 0 disables caching.")
    label_discovery_max_workers: int = Field(
        default=8, ge=1, description="Parallel /api/v1/series calls during label discovery.")
    pipeline_timeout_seconds: float = Field(default=120.0,
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

    @field_validator("opensearch_result_cache_aliases", "prometheus_query_plan_cache_aliases")
    @classmethod
    def _check_result_cache_aliases(cls, value: dict) -> dict:
        for pattern_id, words in value.items():
            if not isinstance(pattern_id, str) or not pattern_id.strip():
                raise ValueError("alias family names must be non-empty strings")
            for key, replacement in words.items():
                if not key.strip() or " " in key.strip() or not replacement.strip():
                    raise ValueError(f"alias {key!r} -> {replacement!r}: the key must be one word and the "
                                     f"replacement non-empty")
        return value

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
