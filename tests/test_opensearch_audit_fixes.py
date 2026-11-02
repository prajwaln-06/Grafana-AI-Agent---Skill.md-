"""Regression tests for the OpenSearch integration audit (2026-11-02), one section per finding.

What is real: registry, field catalog (built from the SYNTHETIC fixture mappings, see
tests/fixtures/opensearch/README.md), guard, canonicalisation, validator, executor, results, and the
real opensearch_client code. What is faked: the two LLM calls and the HTTP session
(tests/os_fixtures.FakeCluster). Nothing here claims compatibility with a live cluster."""
import asyncio
import inspect
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app import executor, llm_client, opensearch_client, pipeline, validator
from app.opensearch_catalog import analysis as os_analysis
from app.opensearch_catalog import canonical, guard, runtime
from app.opensearch_catalog.redaction import RedactionPolicy
from app.opensearch_catalog.results import normalize_search_response
from tests.os_fixtures import REF, FakeCluster, agg_body, hits_body, os_entry, window

CTL = "controller_log"
ILO = "ilo_events"


@pytest.fixture()
def cluster(monkeypatch):
    return FakeCluster().install(monkeypatch)


def ctx_for(settings, *pids):
    return runtime.build_request_context(
        [{"reference_path": REF[p], "data_source": "opensearch"} for p in pids], settings)


def canonical_entry(settings, entry):
    """What the pipeline hands the validator/executor: shape-normalised, then canonicalised."""
    return pipeline._normalize_then_canonicalise({"mode": "single", **entry}, ctx_for(settings, entry["pattern_id"]))


def validate(settings, contract, pid):
    return validator.validate_contract(contract, known_references={REF[pid]}, known_datasources={"opensearch"},
                                       opensearch_context=ctx_for(settings, pid))


def execute(settings, contract):
    return executor.execute_contract(contract, settings)["execution"]


ROWS_Q = {"size": 5, "query": {"bool": {"filter": [window()]}}}


# =====================================================================================================
# F1 -- shape normalisation runs BEFORE canonicalisation
# =====================================================================================================

def _status_in_mode(entry):
    """The documented Generator near-miss: the status value written into `mode`, no `status` key."""
    rest = {k: v for k, v in entry.items() if k != "status"}
    return {"mode": entry["status"], **rest}


def test_f1_near_miss_is_normalised_then_canonicalised(settings, cluster):
    out = pipeline._normalize_then_canonicalise(_status_in_mode(os_entry(CTL, ROWS_Q)), ctx_for(settings, CTL))
    assert out["mode"] == "single" and out["status"] == "ok"
    # canonical form: registry index, documented defaults recorded
    assert out["index"] == "controllerlog-*"
    assert out["query"]["timeout"] == "10s" and out["query"]["sort"] == [{"@timestamp": {"order": "desc"}}]
    assert any("timeout" in a for a in out["guard_notes"]["applied"])
    assert validate(settings, out, CTL).passed


def test_f1_the_old_order_is_what_failed(settings, cluster):
    """Canonicalise-then-normalise (the pre-fix order) leaves the near-miss uncanonical, and the
    validator rejects it -- the defect this ordering fixes. Fail-closed behaviour is unchanged."""
    ctx = ctx_for(settings, CTL)
    old = pipeline._normalize_contract_shape(canonical.canonicalise_contract(_status_in_mode(os_entry(CTL, ROWS_Q)), ctx))
    r = validate(settings, old, CTL)
    assert not r.passed and "canonical" in r.reason


def test_f1_end_to_end_near_miss_validates_and_executes_the_canonical_query(skill_index, settings, cluster):
    cluster.handler = lambda i, d: hits_body([{"@timestamp": "2026-10-28T10:00:00Z", "Severity": "error", "Body": "x"}])
    router = MagicMock(parsed={"gate_stop": None, "panic_mode": False, "unresolved_topics": [],
                               "matched_references": [{"reference_path": REF[CTL], "data_source": "opensearch"}]})
    gen = MagicMock(parsed=_status_in_mode(os_entry(CTL, ROWS_Q)))
    with patch.object(llm_client, "call_llm_json", side_effect=[router, gen]):
        out = asyncio.run(pipeline.run_pipeline("controller logs in the last day", skill_index, settings))
    assert out["mode"] == "single" and out["status"] == "ok", out
    ex = execute(settings, out)
    assert ex["execution_status"] == "success"
    # exactly the validated canonical body went to the cluster
    assert cluster.searches == [("controllerlog-*", out["query"])]


def test_f1_over_cap_window_keeps_the_single_mode_envelope(settings, cluster):
    q = {"size": 3, "query": {"bool": {"filter": [window("now-30d")]}}}
    out = pipeline._normalize_then_canonicalise(_status_in_mode(os_entry(CTL, q)), ctx_for(settings, CTL))
    assert out["mode"] == "single" and out["status"] == "declined"
    assert out["reason"] == "parameter_requires_clarification" and "7d" in out["clarification"]


def test_f1_both_generator_call_sites_normalise_before_canonicalising():
    """The root/flat path and the dependent-query path must use the same order, so the canonical
    form the validator recomputes is always the one the pipeline produced."""
    for fn in (pipeline._run_generation_attempt, pipeline._resolve_dependent_entry):
        src = inspect.getsource(fn)
        assert "_normalize_then_canonicalise(" in src and "_normalize_contract_shape(" not in src, fn.__name__
    assert "canonicalise_contract" not in inspect.getsource(pipeline._run_generator)


def test_f1_prometheus_contracts_are_only_shape_normalised():
    prom = {"status": "ok", "data_source": "prometheus", "query": "node_load1",
            "time_range": {"from": "now-1h", "to": "now", "step": "60s"}}
    assert pipeline._normalize_then_canonicalise(prom, None) == {"mode": "single", **prom}
    near = {"mode": "ok", **{k: v for k, v in prom.items() if k != "status"}}
    assert pipeline._normalize_then_canonicalise(near, None) == pipeline._normalize_contract_shape(near)


# =====================================================================================================
# F2 -- sort on a text field uses the mapping-confirmed keyword sub-field, or is rejected
# =====================================================================================================

def _sorted(sort, pid=CTL):
    return os_entry(pid, {"size": 5, "sort": sort, "query": {"bool": {"filter": [window()]}}})


def test_f2_sort_on_text_field_is_applied_to_its_confirmed_keyword_subfield(settings, cluster):
    r = guard.check_entry(_sorted([{"Resource.host.name": "asc"}]), ctx_for(settings, CTL))
    assert r.ok, r.reason
    assert r.query["sort"] == [{"Resource.host.name.keyword": "asc"}]
    assert any("Resource.host.name.keyword" in a and "sort" in a for a in r.applied)


def test_f2_sort_substitution_is_idempotent_and_validates(settings, cluster):
    ctx = ctx_for(settings, CTL)
    first = guard.check_entry(_sorted([{"Resource.host.name": {"order": "desc"}}]), ctx)
    again = guard.check_entry({**_sorted(None), "query": first.query}, ctx)
    assert again.ok and again.query == first.query and not any("sort" in a for a in again.applied)
    fixed = canonical_entry(settings, _sorted([{"Resource.host.name": "desc"}]))
    assert validate(settings, fixed, CTL).passed


def test_f2_keyword_and_date_sorts_are_unchanged(settings, cluster):
    ctx = ctx_for(settings, CTL)
    for sort in ([{"Resource.host.name.keyword": "asc"}], [{"@timestamp": {"order": "asc"}}]):
        r = guard.check_entry(_sorted(sort), ctx)
        assert r.ok and r.query["sort"] == sort


def test_f2_text_field_without_confirmed_subfield_is_rejected_never_invented(settings, cluster):
    r = guard.check_entry(_sorted([{"Attributes.PortName": "asc"}]), ctx_for(settings, CTL))
    assert not r.ok and r.code == guard.CODE_OPERATION_NOT_SUPPORTED
    assert "no confirmed keyword sub-field" in r.reason
    # writing the sub-field the mapping does not have is an unknown field, not a guess that passes
    r = guard.check_entry(_sorted([{"Attributes.PortName.keyword": "asc"}]), ctx_for(settings, CTL))
    assert not r.ok and r.code == guard.CODE_UNKNOWN_FIELD


@pytest.mark.parametrize("path", ["Body", "Body.keyword"])
def test_f2_free_text_field_is_never_sortable(settings, cluster, path):
    r = guard.check_entry(_sorted([{path: "asc"}]), ctx_for(settings, CTL))
    assert not r.ok and "free-text" in r.reason


def test_f2_prompt_states_what_the_guard_does_for_text_fields(settings, cluster):
    block = ctx_for(settings, CTL).prompt_block
    assert "sort written on such a text field are applied to its `exact=` path" in block
    assert "group-by and cardinality must name the `exact=` path" in block


def test_f2_executor_sends_the_substituted_sort(settings, cluster):
    cluster.handler = lambda i, d: hits_body([{"@timestamp": "2026-10-28T10:00:00Z", "Body": "x"}])
    ex = execute(settings, canonical_entry(settings, _sorted([{"Resource.host.name": "asc"}])))
    assert ex["execution_status"] == "success"
    assert cluster.searches[0][1]["sort"] == [{"Resource.host.name.keyword": "asc"}]


# =====================================================================================================
# F3 -- histogram vs terms is decided by the query, not by key_as_string
# =====================================================================================================

POLICY = RedactionPolicy()


def _norm(os_registry, pid, query, raw):
    return normalize_search_response(raw, entry=os_registry.get(pid), query=query, policy=POLICY,
                                     max_series=50, max_hits=50)


BOOL_BUCKETS = {"rep": {"doc_count_error_upper_bound": 0, "sum_other_doc_count": 0, "buckets": [
    {"key": 0, "key_as_string": "false", "doc_count": 7}, {"key": 1, "key_as_string": "true", "doc_count": 3}]}}


def test_f3_boolean_terms_aggregation_is_buckets_not_a_time_series(os_registry):
    q = {"size": 0, "aggs": {"rep": {"terms": {"field": "Oem.Hpe.Repaired", "size": 2}}}}
    r = _norm(os_registry, ILO, q, agg_body(BOOL_BUCKETS, total=10))
    assert r.result_type == "buckets" and r.series == []
    assert [(b.key, b.doc_count) for b in r.buckets] == [("false", 7), ("true", 3)]
    assert r.metadata["aggregation"]["type"] == "terms"


def test_f3_boolean_buckets_without_query_structure_are_still_not_a_time_series(os_registry):
    r = _norm(os_registry, ILO, {}, agg_body(BOOL_BUCKETS, total=10))
    assert r.result_type == "buckets" and r.metadata["aggregation"]["type"] == "terms"


def test_f3_date_histogram_is_still_a_series(os_registry):
    q = {"size": 0, "aggs": {"t": {"date_histogram": {"field": "@timestamp", "fixed_interval": "1h"}}}}
    raw = agg_body({"t": {"buckets": [
        {"key": 1761645600000, "key_as_string": "2026-10-28T10:00:00.000Z", "doc_count": 4},
        {"key": 1761649200000, "key_as_string": "2026-10-28T11:00:00.000Z", "doc_count": 0}]}}, total=4)
    r = _norm(os_registry, CTL, q, raw)
    assert r.result_type == "series" and r.metadata["aggregation"]["type"] == "date_histogram"
    assert [(p.timestamp, p.value) for p in r.series[0].points] == [
        ("2026-10-28T10:00:00.000Z", 4.0), ("2026-10-28T11:00:00.000Z", 0.0)]


def test_f3_keyword_terms_aggregation_is_unchanged(os_registry):
    q = {"size": 0, "aggs": {"h": {"terms": {"field": "Resource.host.name.keyword", "size": 5}}}}
    raw = agg_body({"h": {"doc_count_error_upper_bound": 0, "sum_other_doc_count": 2,
                          "buckets": [{"key": "n1", "doc_count": 5}, {"key": "n2", "doc_count": 1}]}}, total=8)
    r = _norm(os_registry, CTL, q, raw)
    assert r.result_type == "buckets" and [(b.key, b.doc_count) for b in r.buckets] == [("n1", 5), ("n2", 1)]
    assert r.truncated is True and r.metadata["aggregation"]["sum_other_doc_count"] == 2


def test_f3_boolean_grouping_end_to_end(settings, cluster):
    cluster.handler = lambda i, d: agg_body(BOOL_BUCKETS, total=10)
    q = {"size": 0, "query": {"bool": {"filter": [window()]}},
         "aggs": {"rep": {"terms": {"field": "Oem.Hpe.Repaired", "size": 2}}}}
    ex = execute(settings, canonical_entry(settings, os_entry(ILO, q)))
    assert ex["execution_status"] == "success" and ex["result_type"] == "buckets"
    assert [b["key"] for b in ex["buckets"]] == ["false", "true"]


# =====================================================================================================
# F4 -- an absent hits.total is unknown, never zero
# =====================================================================================================

def _no_total(docs):
    body = hits_body(docs)
    del body["hits"]["total"]
    return body


DOCS = [{"@timestamp": "2026-10-28T10:00:00Z", "Severity": "error", "Body": "a"},
        {"@timestamp": "2026-10-28T09:00:00Z", "Severity": "info", "Body": "b"}]


def test_f4_client_keeps_rows_when_total_is_absent(cluster):
    cluster.handler = lambda i, d: _no_total(DOCS)
    out = opensearch_client.search("http://os:9200", "controllerlog-*", {"size": 5})
    assert out.status == "success" and len(out.raw_body["hits"]["hits"]) == 2


def test_f4_client_empty_result_rules_are_preserved(cluster):
    cluster.handler = lambda i, d: _no_total([])
    assert opensearch_client.search("http://os:9200", "controllerlog-*", {"size": 5}).status == "empty_result"
    cluster.handler = lambda i, d: hits_body([], total=0)
    assert opensearch_client.search("http://os:9200", "controllerlog-*", {"size": 5}).status == "empty_result"
    cluster.handler = lambda i, d: hits_body([], total=12)          # count-only with matches
    assert opensearch_client.search("http://os:9200", "controllerlog-*", {"size": 0}).status == "success"


def test_f4_untracked_total_returns_rows_and_reports_the_count_as_unknown(settings, cluster):
    cluster.handler = lambda i, d: _no_total(DOCS)
    q = {"size": 5, "track_total_hits": False, "query": {"bool": {"filter": [window()]}}}
    ex = execute(settings, canonical_entry(settings, os_entry(CTL, q)))
    assert ex["execution_status"] == "success" and len(ex["hits"]) == 2
    assert ex["metadata"]["count"] == {"value": None, "relation": "unknown", "exact": False}
    assert ex["metadata"]["rows"]["returned"] == 2 and ex["metadata"]["rows"]["more_available"] is None


def test_f4_untracked_total_with_no_rows_is_still_an_empty_result(settings, cluster):
    cluster.handler = lambda i, d: _no_total([])
    q = {"size": 5, "track_total_hits": False, "query": {"bool": {"filter": [window()]}}}
    ex = execute(settings, canonical_entry(settings, os_entry(CTL, q)))
    assert ex["execution_status"] == "empty_result"


def test_f4_tracked_totals_are_unchanged(os_registry):
    r = _norm(os_registry, CTL, {}, hits_body(DOCS, total=10000, relation="gte"))
    assert r.metadata["count"] == {"value": 10000, "relation": "gte", "exact": False}
    r = _norm(os_registry, CTL, {}, hits_body(DOCS))
    assert r.metadata["count"] == {"value": 2, "relation": "eq", "exact": True}


def test_f4_count_only_query_may_not_disable_the_total(settings, cluster):
    ctx = ctx_for(settings, CTL)
    q = {"size": 0, "track_total_hits": False, "query": {"bool": {"filter": [window()]}}}
    r = guard.check_entry(os_entry(CTL, q), ctx)
    assert not r.ok and r.code == guard.CODE_BAD_VALUE and "count-only" in r.reason
    assert guard.check_entry(os_entry(CTL, {**q, "track_total_hits": True}), ctx).ok


def test_f4_analysis_total_helper_reports_unknown():
    assert os_analysis._total({"hits": {"hits": []}}) == (None, "unknown")
    assert os_analysis._total({"hits": {"total": {"value": 3, "relation": "eq"}}}) == (3, "eq")


# =====================================================================================================
# F5 -- the executor independently runs the full guard and sends only the canonical query
# =====================================================================================================

def _raw_entry(query, pid=CTL, **kw):
    """An entry that SKIPPED the pipeline (never validated, never canonicalised)."""
    return {"mode": "single", **os_entry(pid, query, **kw)}


W_ = window()
UNSAFE = {
    # every one of these passes guard.structural_check() (the executor's only check before the fix)
    "scripted_metric": {"size": 0, "query": {"bool": {"filter": [W_]}},
                        "aggs": {"x": {"scripted_metric": {"init_script": "s", "map_script": "s",
                                                           "combine_script": "s", "reduce_script": "s"}}}},
    "top_hits_10000": {"size": 0, "query": {"bool": {"filter": [W_]}}, "aggs": {"x": {"top_hits": {"size": 10000}}}},
    "terms_size_10m": {"size": 0, "query": {"bool": {"filter": [W_]}},
                       "aggs": {"x": {"terms": {"field": "Resource.host.name.keyword", "size": 10_000_000}}}},
    "multi_match_star": {"size": 5, "query": {"bool": {"filter": [W_, {"multi_match": {"query": "x", "fields": ["*"]}}]}}},
    "terms_lookup": {"size": 5, "query": {"bool": {"filter": [W_, {"terms": {"Resource.host.name.keyword": {
        "index": "other-index", "id": "1", "path": "hosts"}}}]}}},
    "match_all": {"size": 5, "query": {"match_all": {}}},
    "range_in_should": {"size": 5, "query": {"bool": {"should": [W_], "filter": [{"exists": {"field": "Body"}}]}}},
    "range_in_must_not": {"size": 5, "query": {"bool": {"must_not": [W_], "filter": [{"exists": {"field": "Body"}}]}}},
    "field_not_in_registry": {"size": 5, "query": {"bool": {"filter": [W_, {"term": {"Resource.process_id": 1}}]}}},
}


@pytest.mark.parametrize("name", sorted(UNSAFE))
def test_f5_structurally_valid_but_unsafe_queries_never_reach_the_cluster(settings, cluster, name):
    assert guard.structural_check(UNSAFE[name], guard.Limits()) is None   # the old executor check passed it
    ex = execute(settings, _raw_entry(UNSAFE[name]))
    assert ex["execution_status"] == "not_executed", ex
    assert "rejected before execution" in ex["error"]
    assert cluster.searches == []


def test_f5_a_valid_but_uncanonical_query_is_not_run(settings, cluster):
    ex = execute(settings, _raw_entry(ROWS_Q))          # valid, but defaults were never filled in
    assert ex["execution_status"] == "not_executed" and "canonical" in ex["error"]
    assert cluster.searches == []


def test_f5_a_canonical_query_changed_after_validation_is_not_run(settings, cluster):
    fixed = canonical_entry(settings, os_entry(CTL, ROWS_Q))
    assert validate(settings, fixed, CTL).passed
    tampered = json.loads(json.dumps(fixed))
    del tampered["query"]["_source"]                   # rows would no longer be projection-limited
    ex = execute(settings, tampered)
    assert ex["execution_status"] == "not_executed" and cluster.searches == []


def test_f5_the_executed_body_equals_the_validated_canonical_query(settings, cluster):
    cluster.handler = lambda i, d: hits_body(DOCS)
    fixed = canonical_entry(settings, os_entry(CTL, ROWS_Q))
    assert validate(settings, fixed, CTL).passed
    ex = execute(settings, fixed)
    assert ex["execution_status"] == "success"
    assert cluster.searches == [("controllerlog-*", fixed["query"])]
    assert ex["resolved_time_range"]["seconds"] == 86400 and ex["resolved_time_range"]["max_window"] == "7d"


def test_f5_window_over_cap_is_rejected_by_the_executor_not_shrunk(settings, cluster):
    q = {"size": 5, "query": {"bool": {"filter": [window("now-8d")]}}}
    ex = execute(settings, _raw_entry(q))
    assert ex["execution_status"] == "not_executed" and "window_exceeds_max" in ex["error"]
    assert cluster.searches == []


def test_f5_executor_fails_closed_when_field_discovery_is_unavailable(settings, monkeypatch):
    fake = FakeCluster(mapping_status=503).install(monkeypatch)
    fixed = {"mode": "single", **os_entry(CTL, ROWS_Q)}
    ex = execute(settings, fixed)
    assert ex["execution_status"] == "not_executed" and "field discovery is unavailable" in ex["error"]
    assert fake.searches == []


def test_f5_index_rules_are_not_weakened(settings, cluster):
    fixed = canonical_entry(settings, os_entry(CTL, ROWS_Q))
    for bad in ("*", "_all", "controllerlog-*,log_iml-*", "remote:controllerlog-*"):
        ex = execute(settings, {**fixed, "index": bad})
        assert ex["execution_status"] == "not_executed", bad
    assert cluster.searches == []


# =====================================================================================================
# F6 -- aggregation and filter-bucket NAMES are not matched against the construct denylist
# =====================================================================================================

def _agg_q(aggs):
    return {"size": 0, "query": {"bool": {"filter": [window()]}}, "aggs": aggs}


@pytest.mark.parametrize("name", ["stats", "routing", "indices", "ext", "profile", "explain", "preference"])
def test_f6_aggregation_named_like_a_denylisted_key_is_accepted(settings, cluster, name):
    r = guard.check_entry(os_entry(CTL, _agg_q({name: {"terms": {"field": "Resource.host.name.keyword"}}})),
                          ctx_for(settings, CTL))
    assert r.ok, r.reason
    assert name in r.query["aggs"]


def test_f6_sub_aggregation_and_filter_bucket_names_are_accepted(settings, cluster):
    aggs = {"by_host": {"terms": {"field": "Resource.host.name.keyword"}, "aggs": {
        "stats": {"filters": {"filters": {"profile": {"term": {"Severity": "error"}},
                                          "routing": {"term": {"Severity": "info"}}}}}}}}
    r = guard.check_entry(os_entry(CTL, _agg_q(aggs)), ctx_for(settings, CTL))
    assert r.ok, r.reason


@pytest.mark.parametrize("aggs,needle", [
    ({"stats": {"stats": {"field": "Oem.Hpe.Code"}}}, "stats"),                          # a stats AGGREGATION type
    ({"stats": {"terms": {"field": "Resource.host.name.keyword", "script": "x"}}}, "script"),
    ({"ok": {"terms": {"field": "Resource.host.name.keyword"}, "aggs": {"x": {"terms": {"script": "x"}}}}}, "script"),
    ({"f": {"filters": {"filters": {"stats": {"script": {"script": "x"}}}}}}, "script"),
])
def test_f6_constructs_under_any_name_are_still_rejected(settings, cluster, aggs, needle):
    r = guard.check_entry(os_entry(CTL, _agg_q(aggs)), ctx_for(settings, CTL))
    assert not r.ok and needle in r.reason


def test_f6_query_clauses_are_still_fully_scanned():
    body = {"size": 5, "query": {"bool": {"filter": [window(), {"aggs": {"script": {"script": "x"}}}]}}}
    assert "script" in (guard.structural_check(body, guard.Limits()) or "")
    body = {"size": 5, "query": {"bool": {"filter": [window(), {"wildcard": {"Body": "x*"}}]}}}
    assert "wildcard" in (guard.structural_check(body, guard.Limits()) or "")
    assert "profile" in (guard.structural_check({"profile": True, "query": {}}, guard.Limits()) or "")


# =====================================================================================================
# F7 -- date-math rounding is accounted for in the window cap (documented OpenSearch semantics;
#       NOT verified against a live cluster)
# =====================================================================================================

NOW = datetime(2026, 11, 2, 15, 30, tzinfo=timezone.utc)


def _window_result(settings, bounds):
    q = {"size": 5, "query": {"bool": {"filter": [{"range": {"@timestamp": bounds}}]}}}
    return guard.check_entry(os_entry(CTL, q), ctx_for(settings, CTL), now=NOW)


@pytest.mark.parametrize("bounds,ok,seconds", [
    ({"gte": "now-7d", "lte": "now"}, True, 7 * 86400),                                    # exactly the cap
    ({"gte": "now-169h", "lte": "now"}, False, 169 * 3600),                                # one hour over
    ({"gte": "now-7d/d", "lt": "now/d"}, True, 7 * 86400),                                 # whole past days
    ({"gte": "now-6d/d", "lte": "now/d"}, True, 7 * 86400 - 0.001),                        # lte rounds UP
    ({"gte": "now-7d/d", "lte": "now/d"}, False, 8 * 86400 - 0.001),                       # the audit's case
    ({"gt": "now-8d/d", "lt": "now/d"}, True, 7 * 86400),                                  # gt rounds UP
    ({"gte": "2026-10-26", "lte": "2026-11-01"}, True, 7 * 86400 - 0.001),                 # date-only lte
    ({"gte": "2026-10-26", "lte": "2026-11-02"}, False, 8 * 86400 - 0.001),
    ({"gte": "2026-10-26T00:00:00Z", "lte": "2026-11-02T00:00:00Z"}, True, 7 * 86400),     # full timestamps
])
def test_f7_window_boundaries(settings, cluster, bounds, ok, seconds):
    r = _window_result(settings, bounds)
    assert r.ok is ok, r.reason
    if ok:
        assert r.window["seconds"] == pytest.approx(seconds, abs=1e-6)
    else:
        assert r.code == guard.CODE_WINDOW_EXCEEDS_MAX and "have not changed it" in r.user_message


def test_f7_rounded_upper_bound_reports_the_end_of_day():
    w = guard.extract_window({"query": {"bool": {"filter": [{"range": {"@timestamp": {
        "gte": "now-1d/d", "lte": "now/d"}}}]}}}, "@timestamp", now=NOW)
    assert w["start"] == "2026-11-01T00:00:00Z" and w["end"] == "2026-11-02T23:59:59.999000Z"


def test_f7_prometheus_time_parsing_is_unchanged():
    from app import time_utils
    assert time_utils.resolve_relative_time("now/d", reference=NOW) == datetime(2026, 11, 2, tzinfo=timezone.utc)
    assert time_utils.resolve_relative_time("now-1d/d", reference=NOW) == datetime(2026, 11, 1, tzinfo=timezone.utc)


# =====================================================================================================
# Phase 3 -- dependent-query datasources, recurrence truncation, registry reload
# =====================================================================================================

def _matched(ds_parent, ds_child):
    return [{"reference_path": "references/node-exporter/memory.md", "data_source": ds_parent, "intent_id": "a"},
            {"reference_path": REF[CTL], "data_source": ds_child, "intent_id": "b", "depends_on": ["a"]}]


def test_dependency_across_datasources_falls_back_to_the_flat_path():
    assert pipeline._extract_dependency_plan(_matched("prometheus", "opensearch")) is None
    assert pipeline._extract_dependency_plan(_matched("opensearch", "prometheus")) is None


def test_dependency_within_one_datasource_is_still_planned():
    m = [{"reference_path": "references/node-exporter/memory.md", "data_source": "prometheus", "intent_id": "a"},
         {"reference_path": "references/node-exporter/cpu.md", "data_source": "prometheus", "intent_id": "b",
          "depends_on": ["a"]}]
    plan = pipeline._extract_dependency_plan(m)
    assert plan is not None and [d["intent_id"] for d in plan.dependent_order] == ["b"]


def _recurrence_entry():
    q = {"size": 0, "query": {"bool": {"filter": [window("now-7d"), {"term": {"fields.PhysicalSubContext": "CRITICAL"}}]}},
         "aggs": {"loc": {"terms": {"field": "fields.Location.keyword", "size": 2},
                          "aggs": {"per": {"date_histogram": {"field": "@timestamp", "fixed_interval": "1h"}}}}}}
    return os_entry("fabric_health", q, analysis={"type": "recurrence"})


def _recurrence_response(sum_other):
    return agg_body({"loc": {"doc_count_error_upper_bound": 0, "sum_other_doc_count": sum_other, "buckets": [
        {"key": "p1", "doc_count": 500, "per": {"buckets": [{"key_as_string": "a", "key": 1, "doc_count": 500}]}},
        {"key": "p2", "doc_count": 30, "per": {"buckets": [{"key_as_string": "a", "key": 1, "doc_count": 10},
                                                           {"key_as_string": "b", "key": 2, "doc_count": 20}]}}]}},
        total=530 + sum_other)


def test_recurrence_says_when_the_ranked_set_was_truncated(settings, cluster):
    cluster.handler = lambda i, d: _recurrence_response(sum_other=40)
    ex = execute(settings, canonical_entry(settings, _recurrence_entry()))
    assert ex["execution_status"] == "success"
    assert any(w.startswith("ranking_limited") for w in ex["analysis"]["warnings"])
    assert ex["metadata"]["aggregation"]["approximate"] is True


def test_recurrence_without_truncation_has_no_warning(settings, cluster):
    cluster.handler = lambda i, d: _recurrence_response(sum_other=0)
    ex = execute(settings, canonical_entry(settings, _recurrence_entry()))
    assert ex["execution_status"] == "success" and ex["analysis"]["warnings"] == []


def test_registry_reload_picks_up_an_edit_and_reports_a_broken_one(settings, tmp_path):
    from app.opensearch_catalog.registry import DEFAULT_REGISTRY_PATH
    path = tmp_path / "index_registry.json"
    good = DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8")
    path.write_text(good, encoding="utf-8")
    settings.opensearch_registry_path = path
    assert "controller_log" in [p.pattern_id for p in runtime.get_registry(settings).enabled_patterns]

    data = json.loads(good)
    pats = data["patterns"] if isinstance(data, dict) else data
    next(p for p in pats if p["pattern_id"] == "ilo_events")["enabled"] = False
    path.write_text(json.dumps(data), encoding="utf-8")
    # still cached until the reload
    assert "ilo_events" in [p.pattern_id for p in runtime.get_registry(settings).enabled_patterns]
    status = runtime.reload_registry_status(settings)
    assert status == {"loaded": True, "patterns": ["controller_log", "fabric_health"]}

    path.write_text("{ not json", encoding="utf-8")
    status = runtime.reload_registry_status(settings)
    assert status["loaded"] is False and status["error"]
    # nothing is cached after a failed load: every OpenSearch request fails closed
    ctx = runtime.build_request_context([{"reference_path": REF[CTL], "data_source": "opensearch"}], settings)
    assert ctx.error and not ctx.usable


def test_admin_reload_endpoint_reports_the_registry(monkeypatch, settings):
    from fastapi.testclient import TestClient

    from run_server import app
    # Patch both, as tests/test_api.py does: the skill reload reads app.agent's settings, and the
    # real ones would load the real catalog into process-wide state other tests then see.
    monkeypatch.setattr("run_server.get_settings", lambda: settings)
    monkeypatch.setattr("app.agent.get_settings", lambda: settings)
    with patch.object(runtime, "reset_registry", wraps=runtime.reset_registry) as spy, TestClient(app) as client:
        r = client.post("/api/v1/admin/reload-skill")
    assert r.status_code == 200 and spy.called
    assert r.json()["opensearch_registry"] == {"loaded": True,
                                               "patterns": ["controller_log", "fabric_health", "ilo_events"]}
