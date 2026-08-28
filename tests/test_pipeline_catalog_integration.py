"""
tests/test_pipeline_catalog_integration.py

Batch 3 (Phases 7-9) pipeline-level tests:

  - Phase 8 shadow mode: flag-off means catalog is never loaded/searched
    at all; flag-on logs a comparison but never changes the Router prompt,
    Generator prompt, or final response; a broken shadow comparison never
    breaks the real request.
  - Phase 9 assisted routing: flag-off means the Router's Section 4 prompt
    content is byte-for-byte unchanged; flag-on narrows Section 4 for a
    catalog hit while always preserving rows the catalog doesn't cover,
    and falls back to the full Section 4 on a catalog miss.

Every test here mocks llm_client.call_llm_json (same convention as
tests/test_pipeline.py) so no real LLM or Prometheus call happens; the
catalog is loaded from the real, generated app/catalog/catalog.json via a
real Settings object with the relevant flag(s) turned on.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

import inspect

from app import label_discovery, llm_client, pipeline, validator
from app.catalog.generator import generate_catalog
from app.catalog.rules import apply_rules
from app.catalog.schema import Catalog, CatalogEntry
from app.catalog.search import SearchResult
from app.config import Settings


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _reset_catalog_cache():
    """pipeline._get_catalog caches module-globally; make sure one test's
    cached catalog (or cached load-failure) never leaks into another."""
    pipeline.reset_catalog_cache()
    yield
    pipeline.reset_catalog_cache()


@pytest.fixture
def real_catalog(skills_root):
    return apply_rules(generate_catalog(skills_root, generated_at="t"))


def _router_resp(matched_reference_path="references/node-exporter/cpu.md"):
    return MagicMock(parsed={
        "gate_stop": None,
        "matched_references": [{"reference_path": matched_reference_path, "data_source": "prometheus"}],
        "panic_mode": False, "unresolved_topics": [],
    })


def _gen_resp():
    return MagicMock(parsed={
        "mode": "single", "status": "ok",
        "reference_used": "references/node-exporter/cpu.md",
        "measurement_used": {"type": "raw_metric", "name": "node_cpu_seconds_total", "source_metrics": []},
        "data_source": "prometheus", "query": "node_cpu_seconds_total",
        "time_range": {"from": "now-1h", "to": "now", "step": "60s"}, "explanation": "test",
    })


def _settings(skills_root, **overrides):
    return Settings(
        gemini_api_key="test-key-not-real",
        skills_root=skills_root,
        prometheus_url="http://localhost:9090",
        opensearch_url="http://localhost:9600",
        **overrides,
    )


# ---- Phase 8: shadow mode ---------------------------------------------


def test_shadow_mode_off_by_default_never_touches_catalog(skill_index, skills_root):
    settings = _settings(skills_root)  # both flags default False
    assert settings.catalog_shadow_mode_enabled is False

    with patch.object(llm_client, "call_llm_json", side_effect=[_router_resp(), _gen_resp()]), \
         patch.object(pipeline, "_get_catalog") as mock_get_catalog, \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        result = _run(pipeline.run_pipeline("cpu usage on node-1", skill_index, settings))

    mock_get_catalog.assert_not_called()
    assert result["status"] == "ok"


def test_shadow_mode_on_logs_comparison_but_result_is_identical_to_off(skill_index, skills_root, real_catalog):
    router_resp = _router_resp()
    gen_resp = _gen_resp()

    settings_off = _settings(skills_root, catalog_shadow_mode_enabled=False)
    with patch.object(llm_client, "call_llm_json", side_effect=[router_resp, gen_resp]), \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        result_off = _run(pipeline.run_pipeline("cpu usage on node-1", skill_index, settings_off))

    settings_on = _settings(skills_root, catalog_shadow_mode_enabled=True)
    with patch.object(llm_client, "call_llm_json", side_effect=[router_resp, gen_resp]), \
         patch.object(pipeline, "_get_catalog", return_value=real_catalog), \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        result_on = _run(pipeline.run_pipeline("cpu usage on node-1", skill_index, settings_on))

    assert result_off == result_on


def test_shadow_mode_prompt_content_unaffected(skill_index, skills_root, real_catalog):
    """Shadow mode must never alter what the Router/Generator actually see."""
    settings_on = _settings(skills_root, catalog_shadow_mode_enabled=True)
    with patch.object(llm_client, "call_llm_json", side_effect=[_router_resp(), _gen_resp()]) as mock_call, \
         patch.object(pipeline, "_get_catalog", return_value=real_catalog), \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        _run(pipeline.run_pipeline("cpu usage on node-1", skill_index, settings_on))

    router_call_prompt = mock_call.call_args_list[0].kwargs["prompt"]
    assert "## 4. Routing table" in router_call_prompt
    # Full, unnarrowed table must still be present -- shadow mode never
    # narrows anything.
    assert "references/dcgm-exporter/reliability.md" in router_call_prompt


def test_shadow_mode_comparison_failure_does_not_break_the_request(skill_index, skills_root, real_catalog):
    settings_on = _settings(skills_root, catalog_shadow_mode_enabled=True)
    with patch.object(llm_client, "call_llm_json", side_effect=[_router_resp(), _gen_resp()]), \
         patch.object(pipeline, "_get_catalog", return_value=real_catalog), \
         patch.object(pipeline.catalog_search, "search", side_effect=RuntimeError("boom")), \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        result = _run(pipeline.run_pipeline("cpu usage on node-1", skill_index, settings_on))

    assert result["status"] == "ok"


def test_shadow_mode_catalog_load_failure_degrades_silently(skill_index, skills_root, tmp_path):
    settings_on = _settings(
        skills_root, catalog_shadow_mode_enabled=True, catalog_path=tmp_path / "does-not-exist.json",
    )
    with patch.object(llm_client, "call_llm_json", side_effect=[_router_resp(), _gen_resp()]), \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        result = _run(pipeline.run_pipeline("cpu usage on node-1", skill_index, settings_on))

    assert result["status"] == "ok"


# ---- Phase 9: assisted routing -----------------------------------------


def test_assisted_routing_off_by_default_leaves_section4_untouched(skill_index, skills_root, real_catalog):
    settings_off = _settings(skills_root, catalog_assisted_routing_enabled=False)
    full_section4 = skill_index.section("## 4.")

    with patch.object(llm_client, "call_llm_json", side_effect=[_router_resp(), _gen_resp()]) as mock_call, \
         patch.object(pipeline, "_get_catalog", return_value=real_catalog), \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        _run(pipeline.run_pipeline("cpu usage on node-1", skill_index, settings_off, catalog=real_catalog))

    router_call_prompt = mock_call.call_args_list[0].kwargs["prompt"]
    assert full_section4 in router_call_prompt


def test_assisted_routing_on_catalog_hit_narrows_out_unrelated_domain_rows(skill_index, skills_root, real_catalog):
    settings_on = _settings(skills_root, catalog_assisted_routing_enabled=True)

    with patch.object(llm_client, "call_llm_json", side_effect=[_router_resp(), _gen_resp()]) as mock_call, \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        _run(pipeline.run_pipeline("cpu utilization question", skill_index, settings_on, catalog=real_catalog))

    router_call_prompt = mock_call.call_args_list[0].kwargs["prompt"]
    # Relevant row kept.
    assert "references/node-exporter/cpu.md" in router_call_prompt
    # A catalog-covered but unrelated domain row is narrowed out.
    assert "references/dcgm-exporter/reliability.md" not in router_call_prompt


def test_assisted_routing_always_keeps_rows_the_catalog_does_not_cover(skill_index, skills_root, real_catalog):
    """overview.md / *-fundamentals.md / execution-contract.md rows are
    never in any catalog entry's reference_path -- narrowing must never
    drop them, regardless of the question."""
    settings_on = _settings(skills_root, catalog_assisted_routing_enabled=True)

    with patch.object(llm_client, "call_llm_json", side_effect=[_router_resp(), _gen_resp()]) as mock_call, \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        _run(pipeline.run_pipeline("cpu utilization question", skill_index, settings_on, catalog=real_catalog))

    router_call_prompt = mock_call.call_args_list[0].kwargs["prompt"]
    for always_kept in (
        "references/node-exporter/overview.md",
        "references/dcgm-exporter/overview.md",
        "references/prometheus-fundamentals.md",
        "references/opensearch-fundamentals.md",
        "references/execution-contract.md",
    ):
        assert always_kept in router_call_prompt


def test_assisted_routing_catalog_miss_falls_back_to_full_section4(skill_index, skills_root, real_catalog):
    settings_on = _settings(skills_root, catalog_assisted_routing_enabled=True)
    full_section4 = skill_index.section("## 4.")

    with patch.object(llm_client, "call_llm_json", side_effect=[_router_resp(), _gen_resp()]) as mock_call, \
         patch.object(pipeline.catalog_search, "search", return_value=[]), \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        _run(pipeline.run_pipeline("completely unrelated query xyz", skill_index, settings_on, catalog=real_catalog))

    router_call_prompt = mock_call.call_args_list[0].kwargs["prompt"]
    assert full_section4 in router_call_prompt


def test_assisted_routing_relies_on_searchs_own_default_status_filter(skill_index, skills_root, real_catalog):
    """Batch 2 clarification: pending-review metrics must not become
    eligible for query generation via catalog-assisted routing. Rather
    than re-implementing a status filter in pipeline.py (a second source
    of truth), _maybe_narrow_section4 calls catalog_search.search() with
    no `statuses` override at all -- it inherits search.py's own default
    (approved + approved_unavailable, discovered_pending_review excluded)
    automatically, and would continue to if that default ever changes."""
    settings_on = _settings(skills_root, catalog_assisted_routing_enabled=True)

    with patch.object(llm_client, "call_llm_json", side_effect=[_router_resp(), _gen_resp()]), \
         patch.object(pipeline.catalog_search, "search", wraps=pipeline.catalog_search.search) as spy_search, \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        _run(pipeline.run_pipeline("cpu utilization question", skill_index, settings_on, catalog=real_catalog))

    spy_search.assert_called_once()
    _, kwargs = spy_search.call_args
    assert "statuses" not in kwargs


# ---- Phase 10 (Batch 4): uncapped-by-rank search + confidence gate -----------
#
# Batch 4 review finding: "a non-empty catalog search result is not
# necessarily a safe routing result." Two fixes, tested independently:
#   1. Narrowing must never truncate candidates by rank (search.py's
#      ordinary top_n=5 display cutoff is the wrong tool for a decision
#      about which Section 4 rows the Router is even allowed to see).
#   2. Narrowing must not act on a low-confidence top hit -- a non-empty
#      result whose best candidate barely scores anything is treated
#      exactly like a catalog miss (full, unnarrowed Section 4).


def test_assisted_routing_search_is_not_capped_by_default_top_n(skill_index, skills_root, real_catalog):
    """Regression guard for the Batch 4 fix: narrowing must search with an
    effectively unbounded top_n, never search.py's DEFAULT_TOP_N=5 -- a
    correct reference ranked 6th or worse must not be silently excluded
    from the narrowed Section 4 just because of where it landed in a
    ranking meant for user-facing candidate lists, not routing eligibility."""
    settings_on = _settings(skills_root, catalog_assisted_routing_enabled=True)

    with patch.object(llm_client, "call_llm_json", side_effect=[_router_resp(), _gen_resp()]), \
         patch.object(pipeline.catalog_search, "search", wraps=pipeline.catalog_search.search) as spy_search, \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        _run(pipeline.run_pipeline("cpu utilization question", skill_index, settings_on, catalog=real_catalog))

    spy_search.assert_called_once()
    _, kwargs = spy_search.call_args
    assert kwargs.get("top_n", 0) > 43  # larger than the entire real catalog


def test_assisted_routing_low_confidence_hit_falls_back_to_full_section4(skill_index, skills_root, real_catalog):
    """A non-empty search result whose top score never clears
    catalog_narrow_min_score must be treated exactly like a catalog miss --
    the low-confidence half of the Batch 4 fix. Simulated by stubbing
    search() to return one weak, low-scoring candidate rather than relying
    on a real question that happens to score low."""
    settings_on = _settings(skills_root, catalog_assisted_routing_enabled=True, catalog_narrow_min_score=2.0)
    full_section4 = skill_index.section("## 4.")

    weak_entry = CatalogEntry(
        name="node_cpu_seconds_total", type="counter", category="cpu", priority="Medium",
        exporter="node-exporter", status="approved", reference_path="references/node-exporter/cpu.md",
    )
    weak_result = [SearchResult(entry=weak_entry, score=1.0)]  # below the 2.0 floor

    with patch.object(llm_client, "call_llm_json", side_effect=[_router_resp(), _gen_resp()]) as mock_call, \
         patch.object(pipeline.catalog_search, "search", return_value=weak_result), \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        _run(pipeline.run_pipeline("cpu utilization question", skill_index, settings_on, catalog=real_catalog))

    router_call_prompt = mock_call.call_args_list[0].kwargs["prompt"]
    assert full_section4 in router_call_prompt


def test_assisted_routing_high_confidence_hit_still_narrows(skill_index, skills_root, real_catalog):
    """Sanity check that the confidence gate doesn't neuter narrowing
    entirely -- a genuinely strong hit (well above the default floor) must
    still narrow, exactly as Phase 9 always did."""
    settings_on = _settings(skills_root, catalog_assisted_routing_enabled=True, catalog_narrow_min_score=2.0)

    with patch.object(llm_client, "call_llm_json", side_effect=[_router_resp(), _gen_resp()]) as mock_call, \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        _run(pipeline.run_pipeline("cpu utilization question", skill_index, settings_on, catalog=real_catalog))

    router_call_prompt = mock_call.call_args_list[0].kwargs["prompt"]
    assert "references/node-exporter/cpu.md" in router_call_prompt
    assert "references/dcgm-exporter/reliability.md" not in router_call_prompt


def test_catalog_narrowing_candidates_true_miss_vs_low_confidence(real_catalog):
    """Unit-level check on the new pure decision function: both a true miss
    (zero candidates) and a low-confidence hit (candidates exist, none
    clear the floor) come back as confident=False, but are distinguishable
    via top_score for callers (e.g. the eval harness) that need to tell
    them apart."""
    true_miss = pipeline.catalog_narrowing_candidates(real_catalog, "asdkjfhaskjdfh nonsense", min_confident_score=2.0)
    assert true_miss.confident is False
    assert true_miss.top_score is None
    assert true_miss.suggested_paths == frozenset()

    weak_entry = CatalogEntry(
        name="node_cpu_seconds_total", type="counter", category="cpu", priority="Medium",
        exporter="node-exporter", status="approved", reference_path="references/node-exporter/cpu.md",
    )
    with patch.object(pipeline.catalog_search, "search", return_value=[SearchResult(entry=weak_entry, score=1.0)]):
        low_confidence = pipeline.catalog_narrowing_candidates(real_catalog, "cpu", min_confident_score=2.0)
    assert low_confidence.confident is False
    assert low_confidence.top_score == 1.0
    assert low_confidence.suggested_paths == frozenset()


def test_catalog_narrowing_candidates_confident_hit_includes_all_non_capped_candidates(real_catalog):
    """A confident decision's suggested_paths must reflect every candidate
    search finds relevant (bounded only by score, never by rank) -- this is
    what test_assisted_routing_search_is_not_capped_by_default_top_n
    verifies at the call-site level; this checks the decision object
    itself carries the un-truncated set through."""
    decision = pipeline.catalog_narrowing_candidates(real_catalog, "cpu utilization question", min_confident_score=2.0)
    assert decision.confident is True
    uncapped = pipeline.catalog_search.search(real_catalog, "cpu utilization question", top_n=1_000_000)
    expected_paths = frozenset(r.entry.reference_path for r in uncapped if r.entry.reference_path)
    assert decision.suggested_paths == expected_paths


# ---- Batch 4: Phase 11 (Generator integration) ---------------------------------
#
# Central question: does the Router -> Generator flow already mean the
# Generator receives the correct, narrowed reference context, without the
# Generator needing to know anything about the catalog itself? Verified two
# ways: (1) structurally -- nothing in the Generator-context-building code
# path takes or references a Catalog/catalog module at all; (2) behaviorally
# -- the actual prompt text the Generator receives is built purely from
# skill_index.read_reference() for the Router's matched_references, which is
# already exactly what SkillIndex always provided, catalog or not.


def test_generator_context_builder_has_no_catalog_parameter():
    """Structural evidence for 'Phase 11: no code change required'. If a
    Generator change were genuinely necessary, the natural place for it to
    surface would be a new parameter here (e.g. a catalog/status argument)
    -- there isn't one, and GeneratorContext's own fields (below) carry
    nothing catalog-shaped either."""
    params = inspect.signature(pipeline._build_generator_context).parameters
    assert "catalog" not in params
    for name in params:
        assert "catalog" not in name.lower()


def test_generator_context_dataclass_has_no_catalog_shaped_fields():
    field_names = {f for f in pipeline.GeneratorContext.__dataclass_fields__}
    assert "catalog" not in field_names
    for name in field_names:
        assert "catalog" not in name.lower()
    # Confirms the frozen rule is upheld structurally: only name/type/help/
    # unit-shaped things (reference/overview/fundamentals text, discovered
    # metrics, discovered labels) ever reach the Generator -- never
    # keywords/category/priority/status, which live in CatalogEntry, not here.


def test_generator_prompt_content_identical_with_and_without_catalog_assisted_routing(
    skill_index, skills_root, real_catalog,
):
    """Behavioral confirmation: once the Router has settled on
    matched_references (regardless of whether catalog-assisted narrowing
    helped it get there), the Generator's own prompt content is built
    identically -- purely from skill_index.read_reference() for those
    matched references. This is the actual evidence that Router -> Generator
    already carries the correct narrowed context without any Generator-side
    catalog awareness."""
    router_resp = _router_resp()

    settings_narrowing_off = _settings(skills_root, catalog_assisted_routing_enabled=False)
    with patch.object(llm_client, "call_llm_json", side_effect=[router_resp, _gen_resp()]) as mock_off, \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        _run(pipeline.run_pipeline("cpu usage on node-1", skill_index, settings_narrowing_off))

    settings_narrowing_on = _settings(skills_root, catalog_assisted_routing_enabled=True)
    with patch.object(llm_client, "call_llm_json", side_effect=[_router_resp(), _gen_resp()]) as mock_on, \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        _run(pipeline.run_pipeline("cpu usage on node-1", skill_index, settings_narrowing_on, catalog=real_catalog))

    # The Router call's prompt legitimately differs (that's narrowing doing
    # its job) -- but the GENERATOR call (index 1) must be byte-for-byte
    # identical either way, since matched_references is identical (the mock
    # Router response) in both runs.
    generator_prompt_off = mock_off.call_args_list[1].kwargs["prompt"]
    generator_prompt_on = mock_on.call_args_list[1].kwargs["prompt"]
    assert generator_prompt_off == generator_prompt_on


# ---- Batch 4: Phase 12 (Validator / known_metrics integration) -----------------


def test_catalog_status_validation_off_by_default_never_loads_catalog(skill_index, skills_root):
    settings = _settings(skills_root)  # all three catalog flags default False
    assert settings.catalog_metric_status_validation_enabled is False

    with patch.object(llm_client, "call_llm_json", side_effect=[_router_resp(), _gen_resp()]), \
         patch.object(pipeline, "_get_catalog") as mock_get_catalog, \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        result = _run(pipeline.run_pipeline("cpu usage on node-1", skill_index, settings))

    mock_get_catalog.assert_not_called()
    assert result["status"] == "ok"


def test_catalog_status_validation_off_passes_catalog_status_by_metric_none_to_validator(
    skill_index, skills_root, real_catalog,
):
    """Even if a catalog happens to be loaded for another feature's sake
    (e.g. catalog_shadow_mode_enabled), the Validator must not receive
    catalog-status information unless catalog_metric_status_validation_enabled
    ITSELF is True -- each Batch 3/4 catalog feature stays independently
    opt-in."""
    settings = _settings(skills_root, catalog_shadow_mode_enabled=True,
                          catalog_metric_status_validation_enabled=False)
    with patch.object(llm_client, "call_llm_json", side_effect=[_router_resp(), _gen_resp()]), \
         patch.object(pipeline, "_get_catalog", return_value=real_catalog), \
         patch.object(validator, "validate_contract", wraps=validator.validate_contract) as spy_validate, \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        result = _run(pipeline.run_pipeline("cpu usage on node-1", skill_index, settings))

    assert result["status"] == "ok"
    _, kwargs = spy_validate.call_args
    assert kwargs.get("catalog_status_by_metric") is None


def test_catalog_status_validation_on_valid_metric_still_passes(skill_index, skills_root, real_catalog):
    """The real generated catalog's 43 metrics are all 'approved' -- turning
    Phase 12 on must not regress an ordinary successful query."""
    settings = _settings(skills_root, catalog_metric_status_validation_enabled=True)
    with patch.object(llm_client, "call_llm_json", side_effect=[_router_resp(), _gen_resp()]), \
         patch.object(pipeline, "_get_catalog", return_value=real_catalog), \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        result = _run(pipeline.run_pipeline("cpu usage on node-1", skill_index, settings))

    assert result["status"] == "ok"


def test_catalog_status_validation_on_rejects_a_pending_review_metric_end_to_end(skill_index, skills_root):
    """Full pipeline wiring test: a metric that the Generator resolved to
    (and that IS present in the Metric Directory, so known_metrics alone
    would have accepted it) must still be rejected end-to-end when its
    catalog status is discovered_pending_review/rejected and Phase 12 is
    enabled -- this is the actual safety guarantee Phase 12 exists for."""
    pending_catalog = Catalog(
        catalog_version="1.0", generated_at="t",
        metrics=(
            CatalogEntry(
                name="node_cpu_seconds_total", type="counter", category="CPU Utilization",
                priority="Medium", exporter="node-exporter", status="discovered_pending_review",
                reference_path="references/node-exporter/cpu.md",
            ),
        ),
    )
    settings = _settings(skills_root, catalog_metric_status_validation_enabled=True)
    with patch.object(llm_client, "call_llm_json", side_effect=[_router_resp(), _gen_resp()]), \
         patch.object(pipeline, "_get_catalog", return_value=pending_catalog), \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        result = _run(pipeline.run_pipeline("cpu usage on node-1", skill_index, settings))

    assert result["status"] == pipeline.INTERNAL_VALIDATION_FAILED_STATUS
    assert "discovered_pending_review" in result["explanation"]


def test_catalog_status_validation_catalog_load_failure_does_not_weaken_validation(
    skill_index, skills_root, tmp_path,
):
    """Requirement #9 at the pipeline level: a missing/corrupt catalog.json
    with Phase 12 enabled must degrade to known_metrics-only behavior (the
    request still succeeds), never crash and never silently reject."""
    settings = _settings(
        skills_root, catalog_metric_status_validation_enabled=True,
        catalog_path=tmp_path / "does-not-exist.json",
    )
    with patch.object(llm_client, "call_llm_json", side_effect=[_router_resp(), _gen_resp()]), \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        result = _run(pipeline.run_pipeline("cpu usage on node-1", skill_index, settings))

    assert result["status"] == "ok"


# ---- Batch 4: Phase 13 (alert known-metric validation) -------------------------


def _alert_catalog_settings(skills_root, **overrides):
    return _settings(
        skills_root, alert_rule_creation_enabled=True, grafana_url="http://localhost:3000",
        grafana_service_account_token="glsa_test", grafana_default_folder_uid="alerts-folder",
        **overrides,
    )


def _alert_router_resp():
    return MagicMock(parsed={
        "gate_stop": None,
        "matched_references": [{"reference_path": "references/node-exporter/cpu.md", "data_source": "prometheus"}],
        "panic_mode": False, "unresolved_topics": [], "action_intent": "propose_alert_rule",
    })


def _alert_gen_resp(metric_name="node_cpu_seconds_total", condition_query=None):
    condition_query = condition_query or f'100 - (avg(rate({metric_name}{{mode="idle"}}[1m])) * 100)'
    return MagicMock(parsed={
        "mode": "single", "status": "alert_rule_proposed",
        "reference_used": "references/node-exporter/cpu.md",
        "measurement_used": {"type": "raw_metric", "name": metric_name, "source_metrics": []},
        "data_source": "prometheus",
        "alert_rule": {
            "title": "High CPU utilization",
            "condition_query": condition_query,
            "comparison": {"operator": ">", "threshold": 90},
            "for_duration": "5m",
            "folder": None,
            "datasource_uid": None,
        },
        "explanation": "Derived from the verified idle-based CPU utilization expression. Proposal only.",
    })


def test_alert_proposal_for_unknown_metric_is_rejected_end_to_end(skill_index, skills_root):
    """Requirement #12 at the pipeline level -- this is the Phase 13 gap
    fix: previously an alert proposal for a fabricated metric name (not in
    any Metric Directory this request opened) would have passed validation
    entirely, since _validate_alert_rule_proposed never checked
    known_metrics at all."""
    settings = _alert_catalog_settings(skills_root)
    with patch.object(llm_client, "call_llm_json",
                       side_effect=[_alert_router_resp(), _alert_gen_resp(metric_name="totally_made_up_metric",
                                                                           condition_query="totally_made_up_metric > 90")]), \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        result = _run(pipeline.run_pipeline("alert me if made up metric exceeds 90", skill_index, settings))

    assert result["status"] == pipeline.INTERNAL_VALIDATION_FAILED_STATUS
    assert "fabricated" in result["explanation"]


def test_alert_proposal_for_valid_known_metric_still_succeeds_end_to_end(skill_index, skills_root):
    """Regression guard alongside the fix above: a legitimate alert
    proposal for a real, known metric must still reach
    alert_rule_proposed, not be caught by the new known_metrics check."""
    settings = _alert_catalog_settings(skills_root)
    with patch.object(llm_client, "call_llm_json", side_effect=[_alert_router_resp(), _alert_gen_resp()]), \
         patch("app.label_discovery.discover_labels_for_metrics",
               return_value={"node_cpu_seconds_total": []}):
        result = _run(pipeline.run_pipeline("alert me if CPU exceeds 90%", skill_index, settings))

    assert result["status"] == "alert_rule_proposed"


def test_alert_proposal_for_rejected_catalog_metric_is_rejected_end_to_end(skill_index, skills_root):
    """Requirement #14 at the pipeline level: catalog status also gates the
    alert path when Phase 12's flag is enabled alongside alert creation."""
    rejected_catalog = Catalog(
        catalog_version="1.0", generated_at="t",
        metrics=(
            CatalogEntry(
                name="node_cpu_seconds_total", type="counter", category="CPU Utilization",
                priority="Medium", exporter="node-exporter", status="rejected",
                reference_path="references/node-exporter/cpu.md",
            ),
        ),
    )
    settings = _alert_catalog_settings(skills_root, catalog_metric_status_validation_enabled=True)
    with patch.object(llm_client, "call_llm_json", side_effect=[_alert_router_resp(), _alert_gen_resp()]), \
         patch.object(pipeline, "_get_catalog", return_value=rejected_catalog), \
         patch("app.label_discovery.discover_labels_for_metrics",
               return_value={"node_cpu_seconds_total": []}):
        result = _run(pipeline.run_pipeline("alert me if CPU exceeds 90%", skill_index, settings))

    assert result["status"] == pipeline.INTERNAL_VALIDATION_FAILED_STATUS
    assert "rejected" in result["explanation"]


def test_alert_creation_disabled_still_forces_out_of_scope_with_catalog_flags_on(skill_index, skills_root):
    """Requirement #16: catalog integration (any of the three flags) must
    never bypass the existing alert_rule_creation_enabled defense-in-depth
    -- disabled means deterministic out_of_scope_action regardless."""
    settings = _settings(
        skills_root, alert_rule_creation_enabled=False,
        catalog_metric_status_validation_enabled=True, catalog_assisted_routing_enabled=True,
    )
    assert settings.alert_rule_creation_enabled is False

    with patch.object(llm_client, "call_llm_json", return_value=_alert_router_resp()), \
         patch("app.label_discovery.discover_labels_for_metrics", return_value={}):
        result = _run(pipeline.run_pipeline("alert me if CPU exceeds 90%", skill_index, settings))

    assert result["status"] == "out_of_scope_action"
