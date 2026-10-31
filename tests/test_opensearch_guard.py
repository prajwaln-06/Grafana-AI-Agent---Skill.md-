"""Deterministic DSL guard (app/opensearch_catalog/guard.py). Uses SYNTHETIC fixture
mappings (see tests/fixtures/opensearch/README.md): these verify the enforcement
code paths, not the live cluster's real schema."""
import copy

import pytest

from app.opensearch_catalog import guard
from app.opensearch_catalog.context import Limits, error_context

W = {"range": {"@timestamp": {"gte": "now-24h", "lte": "now"}}}


def check(os_ctx, pid, query, **extra):
    return guard.check_entry({"pattern_id": pid, "query": query, **extra}, os_ctx(pid))


def hits(*filters, **top):
    return {"size": 5, "query": {"bool": {"filter": [W, *filters]}}, **top}


# ---- accepted shapes ---------------------------------------------------------------------------


def test_valid_hits_query_is_accepted_and_index_comes_from_registry(os_ctx):
    r = check(os_ctx, "controller_log", hits({"term": {"Resource.service.name": "hmscd"}}))
    assert r.ok, r.reason
    assert r.index == "controllerlog-*" and r.pattern_id == "controller_log"


def test_valid_aggregation_gets_documented_defaults_recorded(os_ctx):
    q = {"size": 0, "query": {"bool": {"filter": [W]}},
         "aggs": {"by_host": {"terms": {"field": "Resource.host.name.keyword"}}}}
    r = check(os_ctx, "controller_log", q)
    assert r.ok, r.reason
    assert r.query["aggs"]["by_host"]["terms"]["size"] == 10
    assert r.applied  # the default was recorded, not applied silently


def test_canonicalisation_is_idempotent(os_ctx):
    q = {"size": 0, "query": {"bool": {"filter": [W, {"named_condition": "abnormal_severity"}]}},
         "aggs": {"h": {"terms": {"field": "Resource.host.name.keyword"}}}}
    first = check(os_ctx, "controller_log", q)
    second = check(os_ctx, "controller_log", copy.deepcopy(first.query))
    assert first.ok and second.ok
    assert second.query == first.query and not second.applied


def test_input_query_is_not_mutated(os_ctx):
    q = {"size": 0, "query": {"bool": {"filter": [W, {"named_condition": "abnormal_severity"}]}}}
    before = copy.deepcopy(q)
    check(os_ctx, "controller_log", q)
    assert q == before


# ---- routing boundary ----------------------------------------------------------------------


@pytest.mark.parametrize("pid", [None, "", "nonexistent", "prometheus"])
def test_unknown_or_missing_pattern_is_rejected(os_ctx, pid):
    r = guard.check_entry({"pattern_id": pid, "query": hits()}, os_ctx("controller_log"))
    assert not r.ok and r.code == guard.CODE_UNKNOWN_PATTERN


def test_pattern_not_selected_for_this_request_is_rejected(os_ctx):
    r = guard.check_entry({"pattern_id": "ilo_events", "query": hits()}, os_ctx("controller_log"))
    assert not r.ok and r.code == guard.CODE_UNKNOWN_PATTERN


def test_llm_written_index_that_differs_from_registry_is_rejected(os_ctx):
    r = check(os_ctx, "controller_log", hits(), index="*")
    assert not r.ok and r.code == guard.CODE_INDEX_MISMATCH


def test_matching_index_is_tolerated(os_ctx):
    assert check(os_ctx, "controller_log", hits(), index="controllerlog-*").ok


def test_unavailable_context_fails_closed():
    r = guard.check_entry({"pattern_id": "controller_log", "query": hits()}, error_context("registry broke"))
    assert not r.ok and r.code == guard.CODE_CONTEXT_UNAVAILABLE


def test_none_context_fails_closed():
    assert not guard.check_entry({"pattern_id": "controller_log", "query": hits()}, None).ok


# ---- forbidden constructs and fields --------------------------------------------------------------


@pytest.mark.parametrize("clause", [
    {"wildcard": {"Body": "*x"}}, {"regexp": {"Body": ".*"}}, {"query_string": {"query": "a OR b"}},
    {"script": {"script": {"source": "1"}}}, {"prefix": {"Body": "a"}},
])
def test_forbidden_clauses_are_rejected(os_ctx, clause):
    assert not check(os_ctx, "controller_log", hits(clause)).ok


@pytest.mark.parametrize("extra", [{"script_fields": {"a": {}}}, {"runtime_mappings": {}}, {"pit": {"id": "x"}},
                                   {"search_after": [1]}, {"scroll": "1m"}, {"from": 100}])
def test_forbidden_top_level_keys_are_rejected(os_ctx, extra):
    assert not check(os_ctx, "controller_log", hits(**extra)).ok


def test_unknown_field_is_rejected(os_ctx):
    r = check(os_ctx, "controller_log", hits({"term": {"nope": "x"}}))
    assert not r.ok and r.code == guard.CODE_UNKNOWN_FIELD


def test_event_time_field_cannot_be_filtered(os_ctx):
    q = {"size": 5, "query": {"bool": {"filter": [{"range": {"Timestamp": {"gte": "now-1h", "lte": "now"}}}]}}}
    assert not check(os_ctx, "controller_log", q).ok


def test_grouping_on_free_text_is_rejected(os_ctx):
    q = {"size": 0, "query": {"bool": {"filter": [W]}}, "aggs": {"a": {"terms": {"field": "Body.keyword"}}}}
    r = check(os_ctx, "controller_log", q)
    assert not r.ok and r.code == guard.CODE_AGGREGATION


def test_field_not_in_registry_allowlist_is_rejected_even_if_mapped(os_ctx):
    # `Attributes.*` exists in the mapping fixture but only registered keys are permitted
    r = check(os_ctx, "controller_log", hits({"term": {"Attributes.SomethingElse": "x"}}))
    assert not r.ok


# ---- time window ---------------------------------------------------------------------------------------


def test_missing_time_range_is_rejected(os_ctx):
    r = check(os_ctx, "controller_log", {"size": 5, "query": {"match_all": {}}})
    assert not r.ok and r.code == guard.CODE_TIME_RANGE


def test_unbounded_upper_end_is_rejected(os_ctx):
    q = {"size": 5, "query": {"bool": {"filter": [{"range": {"@timestamp": {"gte": "now-1h"}}}]}}}
    assert not check(os_ctx, "controller_log", q).ok


def test_window_at_cap_is_accepted(os_ctx):
    q = {"size": 5, "query": {"bool": {"filter": [{"range": {"@timestamp": {"gte": "now-7d", "lte": "now"}}}]}}}
    assert check(os_ctx, "controller_log", q).ok


def test_window_over_cap_is_rejected_with_explanation_and_never_rewritten(os_ctx):
    q = {"size": 5, "query": {"bool": {"filter": [{"range": {"@timestamp": {"gte": "now-8d", "lte": "now"}}}]}}}
    before = copy.deepcopy(q)
    r = check(os_ctx, "controller_log", q)
    assert not r.ok and r.code == guard.CODE_WINDOW_EXCEEDS_MAX
    assert "7d" in r.user_message and "narrow" in r.user_message.lower()
    assert q == before and r.query is None


def test_two_time_ranges_are_rejected(os_ctx):
    assert not check(os_ctx, "controller_log", hits(W)).ok


def test_time_range_hidden_in_should_does_not_count(os_ctx):
    q = {"size": 5, "query": {"bool": {"should": [W]}}}
    assert not check(os_ctx, "controller_log", q).ok


def test_inverted_window_is_rejected(os_ctx):
    q = {"size": 5, "query": {"bool": {"filter": [{"range": {"@timestamp": {"gte": "now", "lte": "now-1h"}}}]}}}
    assert not check(os_ctx, "controller_log", q).ok


def test_extract_window_reports_the_requested_window():
    w = guard.extract_window(hits(), "@timestamp")
    assert w["from_expression"] == "now-24h" and w["seconds"] == 86400


# ---- bounds --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("size", [-1, 101, 5000, "10", True, 1.5])
def test_hit_size_out_of_bounds_is_rejected(os_ctx, size):
    assert not check(os_ctx, "controller_log", {"size": size, "query": {"bool": {"filter": [W]}}}).ok


def test_terms_size_over_max_is_rejected_not_clamped(os_ctx):
    q = {"size": 0, "query": {"bool": {"filter": [W]}},
         "aggs": {"a": {"terms": {"field": "Resource.host.name.keyword", "size": 5000}}}}
    assert not check(os_ctx, "controller_log", q).ok


def test_aggregation_nesting_over_limit_is_rejected(os_ctx):
    inner = {"terms": {"field": "Resource.host.name.keyword", "size": 2}}
    q = {"size": 0, "query": {"bool": {"filter": [W]}},
         "aggs": {"a": {**inner, "aggs": {"b": {**inner, "aggs": {"c": {**inner, "aggs": {"d": inner}}}}}}}}
    assert not check(os_ctx, "controller_log", q).ok


def test_bucket_explosion_is_rejected(os_ctx):
    t = lambda s: {"terms": {"field": "Resource.host.name.keyword", "size": s}}  # noqa: E731
    q = {"size": 0, "query": {"bool": {"filter": [W]}},
         "aggs": {"a": {**t(50), "aggs": {"b": {**t(50), "aggs": {"c": t(50)}}}}}}
    assert not check(os_ctx, "controller_log", q, ).ok


def test_date_histogram_too_fine_for_window_is_rejected(os_ctx):
    q = {"size": 0, "query": {"bool": {"filter": [{"range": {"@timestamp": {"gte": "now-7d", "lte": "now"}}}]}},
         "aggs": {"t": {"date_histogram": {"field": "@timestamp", "fixed_interval": "1s"}}}}
    assert not check(os_ctx, "controller_log", q).ok


def test_date_histogram_reasonable_interval_is_accepted(os_ctx):
    q = {"size": 0, "query": {"bool": {"filter": [W]}},
         "aggs": {"t": {"date_histogram": {"field": "@timestamp", "fixed_interval": "1h"}}}}
    assert check(os_ctx, "controller_log", q).ok


def test_oversized_query_is_rejected(os_ctx):
    assert not check(os_ctx, "controller_log", hits({"terms": {"Resource.service.name": ["x" * 50] * 5000}})).ok


def test_cardinality_is_flagged_approximate(os_ctx):
    q = {"size": 0, "query": {"bool": {"filter": [W]}},
         "aggs": {"c": {"cardinality": {"field": "Resource.host.name.keyword"}}}}
    r = check(os_ctx, "controller_log", q)
    assert r.ok and r.approximate_aggregations


def test_sort_only_on_permitted_fields(os_ctx):
    assert check(os_ctx, "controller_log", hits(sort=[{"@timestamp": "desc"}])).ok
    assert not check(os_ctx, "controller_log", hits(sort=[{"Body.keyword": "asc"}])).ok


def test_source_filter_cannot_exceed_projection(os_ctx):
    assert not check(os_ctx, "controller_log", hits(_source=["Attributes", "Anything.else"])).ok


# ---- severity semantics ------------------------------------------------------------------------------------


def test_named_condition_expands_deterministically(os_ctx):
    r = check(os_ctx, "controller_log", hits({"named_condition": "abnormal_severity"}))
    assert r.ok
    assert "named_condition" not in str(r.query)
    assert "error" in str(r.query) and "critical" in str(r.query)
    assert "warning" not in str(r.query)  # warning is not an error unless asked


def test_unknown_named_condition_is_rejected(os_ctx):
    assert not check(os_ctx, "controller_log", hits({"named_condition": "wibble"})).ok


def test_must_not_on_positive_severity_field_is_rejected(os_ctx):
    q = {"size": 5, "query": {"bool": {"filter": [W], "must_not": [{"term": {"Severity.keyword": "info"}}]}}}
    r = check(os_ctx, "controller_log", q)
    assert not r.ok and r.code == guard.CODE_SEVERITY


def test_severity_value_case_mismatch_is_rejected(os_ctx):
    r = check(os_ctx, "controller_log", hits({"term": {"Severity.keyword": "ERROR"}}))
    assert not r.ok and r.code == guard.CODE_SEVERITY


def test_unrecognised_severity_value_warns_but_is_not_silently_changed(os_ctx):
    r = check(os_ctx, "controller_log", hits({"term": {"Severity.keyword": "bizarre"}}))
    if r.ok:
        assert r.warnings and "bizarre" in str(r.query)


def test_ilo_abnormal_is_or_over_both_severity_fields(os_ctx):
    r = check(os_ctx, "ilo_events", hits({"named_condition": "abnormal_severity"}))
    assert r.ok, r.reason
    flat = str(r.query)
    assert "Severity" in flat and "Oem.Hpe.Severity" in flat
    assert "Warning" in flat and "Critical" in flat
    assert "'should'" in flat  # an OR, not an AND


def test_ilo_abnormal_expansion_lists_only_recognised_abnormal_values(os_ctx):
    r = check(os_ctx, "ilo_events", hits({"named_condition": "abnormal_severity"}))
    flat = str(r.query)
    assert "must_not" not in flat
    for normal in ("'OK'", "'Informational'"):
        assert normal not in flat  # normal values are never part of the abnormal set
    assert "Caution" in flat


def test_fabric_health_unhealthy_is_not_healthy(os_ctx):
    r = check(os_ctx, "fabric_health", hits({"named_condition": "abnormal_severity"}))
    assert r.ok, r.reason
    flat = str(r.query)
    assert "HEALTHY" in flat and "exists" in flat


# ---- every example query shipped in the reference files passes the guard ------------------------------------


def test_reference_examples_are_accepted_by_the_guard(os_ctx, skills_root):
    import json, re
    for pid, doc in (("fabric_health", "fabric-health.md"), ("controller_log", "controller-log.md"),
                     ("ilo_events", "ilo-events.md")):
        text = (skills_root / "references" / "opensearch-logs" / doc).read_text()
        blocks = re.findall(r"```json\n(.*?)```", text, re.S)
        assert blocks, doc
        for b in blocks:
            r = check(os_ctx, pid, json.loads(b))
            assert r.ok, f"{doc}: {r.code} {r.reason}"


# ---- exact match on analysed text fields -----------------------------------------------------------------------


def test_term_on_text_field_is_applied_to_its_keyword_subfield_and_recorded(os_ctx):
    r = check(os_ctx, "controller_log", hits({"term": {"Resource.host.name": "x1c0"}}))
    assert r.ok, r.reason
    assert {"Resource.host.name.keyword": "x1c0"} in [c.get("term") for c in r.query["query"]["bool"]["filter"]]
    assert any("keyword sub-field" in a for a in r.applied)


def test_terms_and_prefix_on_text_field_use_the_keyword_subfield(os_ctx):
    r = check(os_ctx, "controller_log", hits({"terms": {"Resource.service.name": ["hmscd", "hmpad"]}},
                                              {"prefix": {"Resource.host.name": "x1"}}))
    assert r.ok, r.reason
    flat = str(r.query)
    assert "Resource.service.name.keyword" in flat and "Resource.host.name.keyword" in flat
    assert "'Resource.host.name': " not in flat


def test_exact_substitution_is_idempotent(os_ctx):
    first = check(os_ctx, "controller_log", hits({"term": {"Resource.host.name": "x"}}))
    again = check(os_ctx, "controller_log", copy.deepcopy(first.query))
    assert again.ok and again.query == first.query
    assert not any("keyword sub-field" in a for a in again.applied)


def test_severity_value_check_still_applies_after_substitution(os_ctx):
    r = check(os_ctx, "controller_log", hits({"term": {"Severity": "ERROR"}}))
    assert not r.ok and r.code == guard.CODE_SEVERITY


def test_free_text_field_is_search_only(os_ctx):
    for clause in ({"term": {"Body.keyword": "x"}}, {"term": {"Body": "x"}}, {"terms": {"Body": ["x"]}},
                   {"prefix": {"Body": "a"}}):
        r = check(os_ctx, "controller_log", hits(clause))
        assert not r.ok and r.code == guard.CODE_OPERATION_NOT_SUPPORTED, clause
    assert check(os_ctx, "controller_log", hits({"match_phrase": {"Body": "link down"}})).ok


def test_prompt_advertises_only_what_the_guard_accepts(os_ctx):
    from app.opensearch_catalog.context import format_prompt_block
    block = format_prompt_block(os_ctx("controller_log"))
    body_line = next(l for l in block.splitlines() if l.strip().startswith("- Body --"))
    assert "group-by" not in body_line and "sort" not in body_line and "exact" not in body_line
    assert "text-search" in body_line
