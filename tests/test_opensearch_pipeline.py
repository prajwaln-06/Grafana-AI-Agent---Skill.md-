"""Pipeline integration for OpenSearch: request context, canonicalisation hook, validator, and the
guarantee that Prometheus-only requests are untouched. LLM calls and the OpenSearch cluster are
mocked; mappings are SYNTHETIC fixtures (no live-cluster claims)."""
import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app import llm_client, pipeline, validator
from app.opensearch_catalog import canonical, runtime
from app.opensearch_catalog.context import error_context
from app.opensearch_catalog.field_catalog import FieldCatalogCache

FIX = Path(__file__).resolve().parent / "fixtures" / "opensearch"
CTL_REF = "references/opensearch-logs/controller-log.md"
W = {"range": {"@timestamp": {"gte": "now-24h", "lte": "now"}}}


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def fixture_cache():
    by_index = {"slingshot_crayfabrichealth-*": "mapping_fabric_health.json", "controllerlog-*": "mapping_controller_log.json",
                "log_iml-*": "mapping_ilo_events.json"}
    cache = FieldCatalogCache(lambda ip: json.loads((FIX / by_index[ip]).read_text()), ttl_seconds=60,
                              stale_ttl_seconds=60, max_indices=50, failure_backoff_seconds=1)
    runtime.reset()
    with patch.object(runtime, "get_field_cache", return_value=cache):
        yield cache
    runtime.reset()


def entry(query, **kw):
    return {"status": "ok", "reference_used": CTL_REF, "data_source": "opensearch", "pattern_id": "controller_log",
            "measurement_used": {"type": "raw_metric", "name": "controller_log_messages", "source_metrics": []},
            "query": query, "explanation": "test", **kw}


# ---- request context --------------------------------------------------------------------------------


def test_context_is_built_from_registry_and_runtime_fields(skill_index, settings, fixture_cache):
    ctx = pipeline._build_generator_context([{"reference_path": CTL_REF, "data_source": "opensearch"}],
                                            skill_index, settings, question="q")
    assert ctx.opensearch is not None and set(ctx.opensearch.patterns) == {"controller_log"}
    assert ctx.opensearch.patterns["controller_log"].usable
    assert "controller_log" in ctx.opensearch_block


def test_prompt_block_names_pattern_id_and_forbids_writing_an_index(skill_index, settings, fixture_cache):
    ctx = pipeline._build_generator_context([{"reference_path": CTL_REF, "data_source": "opensearch"}],
                                            skill_index, settings, question="q")
    block = ctx.opensearch_block
    assert "pattern_id" in block and "abnormal_severity" in block and "7d" in block


def test_prompt_block_does_not_leak_other_families(skill_index, settings, fixture_cache):
    ctx = pipeline._build_generator_context([{"reference_path": CTL_REF, "data_source": "opensearch"}],
                                            skill_index, settings, question="q")
    assert "ilo_events" not in ctx.opensearch_block and "fabric_health" not in ctx.opensearch_block


def test_unreachable_cluster_gives_unusable_context_not_a_guess(skill_index, settings):
    runtime.reset()
    dead = FieldCatalogCache(lambda ip: None, ttl_seconds=60, stale_ttl_seconds=60, max_indices=50,
                             failure_backoff_seconds=1)
    with patch.object(runtime, "get_field_cache", return_value=dead):
        ctx = pipeline._build_generator_context([{"reference_path": CTL_REF, "data_source": "opensearch"}],
                                                skill_index, settings, question="q")
    runtime.reset()
    assert not ctx.opensearch.usable


def test_context_build_failure_is_contained(settings):
    with patch.object(runtime, "get_registry", side_effect=RuntimeError("x")):
        ctx = runtime.build_request_context([{"reference_path": CTL_REF, "data_source": "opensearch"}], settings)
    assert ctx.error and not ctx.usable


def test_prometheus_only_request_has_no_opensearch_context(skill_index, settings):
    ctx = pipeline._build_generator_context([{"reference_path": "references/node-exporter/cpu.md",
                                              "data_source": "prometheus"}], skill_index, settings, question="load")
    assert ctx.opensearch is None and ctx.opensearch_block == ""


# ---- canonicalisation hook ----------------------------------------------------------------------------------


def test_canonicalise_sets_index_expands_condition_and_records_notes(os_ctx):
    q = {"size": 0, "query": {"bool": {"filter": [W, {"named_condition": "abnormal_severity"}]}},
         "aggs": {"h": {"terms": {"field": "Resource.host.name.keyword"}}}}
    out = canonical.canonicalise_contract(entry(q), os_ctx("controller_log"))
    assert out["index"] == "controllerlog-*" and "named_condition" not in json.dumps(out["query"])
    assert out["guard_notes"]["applied"]


def test_canonicalise_handles_multi_mode(os_ctx):
    contract = {"mode": "multi", "results": [entry({"size": 3, "query": {"bool": {"filter": [W]}}}),
                                              {"status": "unmapped", "explanation": "x"}], "synthesis": None}
    out = canonical.canonicalise_contract(contract, os_ctx("controller_log"))
    assert out["results"][0]["index"] == "controllerlog-*" and out["results"][1] == contract["results"][1]


def test_over_cap_window_becomes_a_clarification_and_is_not_shortened(os_ctx):
    q = {"size": 3, "query": {"bool": {"filter": [{"range": {"@timestamp": {"gte": "now-30d", "lte": "now"}}}]}}}
    out = canonical.canonicalise_contract(entry(q), os_ctx("controller_log"))
    assert out["status"] == "declined" and out["reason"] == "parameter_requires_clarification"
    assert "7d" in out["clarification"] and "query" not in out  # nothing was rewritten or kept
    assert validator.validate_contract({"mode": "single", **out}, known_datasources={"opensearch"}).passed


def test_other_rejections_are_left_for_the_validator(os_ctx):
    e = entry({"size": 3, "query": {"bool": {"filter": [W, {"wildcard": {"Body": "*"}}]}}})
    assert canonical.canonicalise_contract(e, os_ctx("controller_log")) == e


def test_non_opensearch_and_non_runnable_entries_are_untouched(os_ctx):
    prom = {"status": "ok", "data_source": "prometheus", "query": "up"}
    unm = {"status": "unmapped", "explanation": "x"}
    ctx = os_ctx("controller_log")
    assert canonical.canonicalise_contract(prom, ctx) == prom and canonical.canonicalise_contract(unm, ctx) == unm


def test_missing_context_leaves_contract_alone():
    e = entry({"size": 1})
    assert canonical.canonicalise_contract(e, None) is e


def test_garbage_contract_never_raises(os_ctx):
    for junk in (None, [], "x", {"results": "nope"}):
        canonical.canonicalise_contract(junk, os_ctx("controller_log"))


# ---- validator ------------------------------------------------------------------------------------------------------


def validate(e, ctx):
    return validator.validate_contract({"mode": "single", **e}, known_references={CTL_REF},
                                       known_datasources={"opensearch"}, opensearch_context=ctx)


def test_validator_accepts_canonical_entry(os_ctx):
    ctx = os_ctx("controller_log")
    e = canonical.canonicalise_contract(entry({"size": 3, "query": {"bool": {"filter": [W]}}}), ctx)
    assert validate(e, ctx).passed


def test_validator_rejects_uncanonicalised_entry(os_ctx):
    ctx = os_ctx("controller_log")
    q = {"size": 0, "query": {"bool": {"filter": [W, {"named_condition": "abnormal_severity"}]}},
         "aggs": {"h": {"terms": {"field": "Resource.host.name.keyword"}}}}
    r = validate(entry(q), ctx)
    assert not r.passed and "canonical" in r.reason


def test_validator_rejects_guard_violations(os_ctx):
    ctx = os_ctx("controller_log")
    r = validate(entry({"size": 3, "query": {"bool": {"filter": [W, {"term": {"bogus": "x"}}]}}}), ctx)
    assert not r.passed and "unknown_field" in r.reason


def test_validator_without_context_only_structural_and_says_so(os_ctx):
    r = validator.validate_contract({"mode": "single", **entry({"size": 3})}, known_references={CTL_REF},
                                    known_datasources={"opensearch"})
    assert r.passed and any("structural" in w for w in r.warnings)


def test_validator_fails_closed_when_context_unavailable():
    r = validate(entry({"size": 3, "query": {"bool": {"filter": [W]}}}), error_context("down"))
    assert not r.passed and "context_unavailable" in r.reason


# ---- end to end through run_pipeline (LLM mocked) ------------------------------------------------------------------


def _pipeline(skill_index, settings, generator_contract):
    router = MagicMock(parsed={"gate_stop": None, "panic_mode": False, "unresolved_topics": [],
                               "matched_references": [{"reference_path": CTL_REF, "data_source": "opensearch"}]})
    with patch.object(llm_client, "call_llm_json", side_effect=[router, MagicMock(parsed=generator_contract)]):
        return _run(pipeline.run_pipeline("controller errors in the last day", skill_index, settings))


def test_end_to_end_valid_query_is_canonicalised(skill_index, settings, fixture_cache):
    q = {"size": 0, "query": {"bool": {"filter": [W, {"named_condition": "abnormal_severity"}]}},
         "aggs": {"h": {"terms": {"field": "Resource.host.name.keyword"}}}}
    out = _pipeline(skill_index, settings, {"mode": "single", **entry(q)})
    assert out["status"] == "ok", out
    assert out["index"] == "controllerlog-*" and "named_condition" not in json.dumps(out["query"])


def test_end_to_end_llm_written_index_is_rejected(skill_index, settings, fixture_cache):
    out = _pipeline(skill_index, settings, {"mode": "single", **entry({"size": 3, "query": {"bool": {"filter": [W]}}}, index="*")})
    assert out["status"] != "ok"


def test_end_to_end_over_cap_window_asks_to_narrow(skill_index, settings, fixture_cache):
    q = {"size": 3, "query": {"bool": {"filter": [{"range": {"@timestamp": {"gte": "now-30d", "lte": "now"}}}]}}}
    out = _pipeline(skill_index, settings, {"mode": "single", **entry(q)})
    assert out["status"] == "declined" and out["reason"] == "parameter_requires_clarification"
    assert "7d" in out["clarification"]


def test_end_to_end_unreachable_cluster_never_yields_an_ok_query(skill_index, settings):
    runtime.reset()
    dead = FieldCatalogCache(lambda ip: None, ttl_seconds=60, stale_ttl_seconds=60, max_indices=50, failure_backoff_seconds=1)
    with patch.object(runtime, "get_field_cache", return_value=dead):
        out = _pipeline(skill_index, settings, {"mode": "single", **entry({"size": 3, "query": {"bool": {"filter": [W]}}})})
    runtime.reset()
    assert out["status"] != "ok"
