"""Normalisation of registered-family OpenSearch responses (app/opensearch_catalog/results.py).
Responses are hand-built SYNTHETIC examples of the OpenSearch _search response shape."""
import pytest

from app.opensearch_catalog.redaction import RedactionPolicy
from app.opensearch_catalog.results import all_shards_failed, normalize_search_response

POL = RedactionPolicy()
OK_SHARDS = {"total": 3, "successful": 3, "skipped": 0, "failed": 0}


def resp(hits=None, total=0, relation="eq", aggs=None, shards=None, timed_out=False, **extra):
    r = {"took": 7, "timed_out": timed_out, "_shards": shards or OK_SHARDS,
         "hits": {"total": {"value": total, "relation": relation}, "hits": hits or []}, **extra}
    if aggs is not None:
        r["aggregations"] = aggs
    return r


def norm(os_registry, pid, raw, query=None, **kw):
    return normalize_search_response(raw, entry=os_registry.get(pid), query=query or {}, policy=POL,
                                     max_series=kw.get("max_series", 200), max_hits=kw.get("max_hits", 500))


def ctl(i, sev="error", msg="link down"):
    return {"_source": {"@timestamp": f"2026-10-28T10:0{i}:00Z", "Severity": sev, "Body": msg,
                        "Resource": {"host": {"name": f"x{i}c0"}, "service": {"name": "hmscd"}},
                        "Secret": "never-projected", "Timestamp": "Oct 28 10:00:00"}}


# ---- rows ---------------------------------------------------------------------------------------------


def test_rows_contain_only_the_registry_projection(os_registry):
    r = norm(os_registry, "controller_log", resp([ctl(1)], total=1))
    row = r.hits[0].fields
    assert "Secret" not in row and "Timestamp" not in row
    assert row["Body"] == "link down" and row["@timestamp"] == "2026-10-28T10:01:00Z"
    assert "never-projected" not in str(r.to_dict())


def test_row_carries_severity_assessment(os_registry):
    r = norm(os_registry, "controller_log", resp([ctl(1, "error"), ctl(2, "info")], total=2))
    assert r.hits[0].severity_assessment["abnormal"] is True
    assert r.hits[1].severity_assessment["abnormal"] is False


def test_exact_count_is_marked_exact(os_registry):
    m = norm(os_registry, "controller_log", resp([ctl(1)], total=1)).metadata
    assert m["count"] == {"value": 1, "relation": "eq", "exact": True}


def test_lower_bound_count_is_marked_not_exact(os_registry):
    m = norm(os_registry, "controller_log", resp([ctl(1)], total=10000, relation="gte")).metadata
    assert m["count"]["relation"] == "gte" and m["count"]["exact"] is False


def test_more_matches_than_rows_is_reported(os_registry):
    m = norm(os_registry, "controller_log", resp([ctl(1)], total=250)).metadata
    assert m["rows"] == {"returned": 1, "matched": 250, "matched_exact": True, "more_available": True}


def test_hit_cap_truncation_is_reported(os_registry):
    r = norm(os_registry, "controller_log", resp([ctl(i) for i in range(1, 6)], total=5), max_hits=2)
    assert r.truncated and r.original_count == 5 and len(r.hits) == 2


def test_observed_range_comes_from_returned_rows_and_says_so(os_registry):
    m = norm(os_registry, "controller_log", resp([ctl(1), ctl(3)], total=2)).metadata
    assert m["observed_range"]["earliest"] == "2026-10-28T10:01:00Z"
    assert m["observed_range"]["latest"] == "2026-10-28T10:03:00Z"
    assert "returned rows only" in m["observed_range"]["basis"]


def test_free_text_is_redacted_in_rows_and_counted(os_registry):
    r = norm(os_registry, "controller_log", resp([ctl(1, msg="login by user alice from 10.1.2.3")], total=1))
    assert "alice" not in r.hits[0].body and "10.1.2.3" not in r.hits[0].body
    assert r.metadata["redaction"]["redacted_usernames"] == 1
    assert r.metadata["redaction"]["redacted_ip_addresses"] == 1
    assert r.hits[0].fields["@timestamp"] == "2026-10-28T10:01:00Z"  # preserved


# ---- partial / shard failures --------------------------------------------------------------------------


def test_clean_response_is_not_partial(os_registry):
    m = norm(os_registry, "controller_log", resp([ctl(1)], total=1)).metadata
    assert m["partial"] is False and m["partial_reasons"] == []


def test_timed_out_is_partial(os_registry):
    m = norm(os_registry, "controller_log", resp([ctl(1)], total=1, timed_out=True)).metadata
    assert m["partial"] and "timed_out" in m["partial_reasons"] and m["timed_out"] is True


def test_shard_failures_are_partial_and_detail_is_redacted_and_bounded(os_registry):
    shards = {"total": 3, "successful": 2, "skipped": 0, "failed": 1, "failures": [
        {"index": "controllerlog-2026.10.28", "shard": 0,
         "reason": {"type": "search_phase_execution_exception", "reason": "failed for user admin at 10.0.0.1 " + "z" * 900}}]}
    m = norm(os_registry, "controller_log", resp([ctl(1)], total=1, shards=shards)).metadata
    assert m["partial"] and "shard_failures" in m["partial_reasons"]
    f = m["shard_failures"][0]
    assert f["type"] == "search_phase_execution_exception"
    assert "admin" not in f["detail"] and "10.0.0.1" not in f["detail"] and len(f["detail"]) < 400
    assert m["shards"]["failed"] == 1


def test_only_a_few_shard_failures_are_listed(os_registry):
    fails = [{"index": f"i{n}", "shard": n, "reason": {"type": "t", "reason": "r"}} for n in range(20)]
    shards = {"total": 20, "successful": 0, "failed": 20, "failures": fails}
    m = norm(os_registry, "controller_log", resp(total=0, shards=shards)).metadata
    assert len(m["shard_failures"]) == 5 and m["shard_failures_reported"] == 5


def test_all_shards_failed_detection():
    assert all_shards_failed({"_shards": {"total": 2, "successful": 0, "failed": 2}})
    assert not all_shards_failed({"_shards": {"total": 2, "successful": 1, "failed": 1}})
    assert not all_shards_failed({"_shards": {"total": 2, "successful": 2, "failed": 0}})
    assert not all_shards_failed({})


# ---- aggregations ----------------------------------------------------------------------------------------------


TERMS_Q = {"aggs": {"by_host": {"terms": {"field": "Resource.host.name.keyword"}}}}


def terms_resp(other=0, err=0, n=3):
    return resp(total=100, aggs={"by_host": {
        "doc_count_error_upper_bound": err, "sum_other_doc_count": other,
        "buckets": [{"key": f"x{i}c0", "doc_count": 10 - i} for i in range(n)]}})


def test_terms_buckets_are_normalised(os_registry):
    r = norm(os_registry, "controller_log", terms_resp(), TERMS_Q)
    assert r.result_type == "buckets" and [b.key for b in r.buckets] == ["x0c0", "x1c0", "x2c0"]
    assert r.metadata["aggregation"]["type"] == "terms"
    assert r.metadata["aggregation"]["approximate"] is False and not r.truncated


def test_buckets_cut_off_by_terms_size_mark_truncation(os_registry):
    r = norm(os_registry, "controller_log", terms_resp(other=40), TERMS_Q)
    assert r.truncated
    a = r.metadata["aggregation"]
    assert a["approximate"] and a["sum_other_doc_count"] == 40
    assert any("40" in x for x in a["approximate_reasons"])


def test_doc_count_error_marks_terms_approximate(os_registry):
    a = norm(os_registry, "controller_log", terms_resp(err=7), TERMS_Q).metadata["aggregation"]
    assert a["approximate"] and a["doc_count_error_upper_bound"] == 7


def test_cardinality_is_flagged_as_estimate(os_registry):
    q = {"aggs": {"c": {"cardinality": {"field": "Resource.host.name.keyword"}}}}
    r = norm(os_registry, "controller_log", resp(total=9, aggs={"c": {"value": 42}}), q)
    assert r.metadata["aggregation"]["approximate"]
    assert any("estimate" in x for x in r.metadata["aggregation"]["approximate_reasons"])
    assert r.series[0].points[0].value == 42.0


def test_nested_cardinality_in_buckets_is_kept_as_bucket_metric(os_registry):
    q = {"aggs": {"by_switch": {"terms": {"field": "x"}, "aggs": {"ports": {"cardinality": {"field": "y"}}}}}}
    raw = resp(total=30, aggs={"by_switch": {"doc_count_error_upper_bound": 0, "sum_other_doc_count": 0, "buckets": [
        {"key": "sw1", "doc_count": 20, "ports": {"value": 3}}]}})
    r = norm(os_registry, "fabric_health", raw, q)
    assert r.buckets[0].metrics == {"ports": 3.0}
    assert r.metadata["aggregation"]["approximate"]


def test_series_cap_marks_truncation(os_registry):
    r = norm(os_registry, "controller_log", terms_resp(n=6), TERMS_Q, max_series=2)
    assert r.truncated and r.original_count == 6 and len(r.buckets) == 2


def test_date_histogram_becomes_a_series_with_observed_range(os_registry):
    q = {"aggs": {"t": {"date_histogram": {"field": "@timestamp", "fixed_interval": "1h"}}}}
    raw = resp(total=5, aggs={"t": {"buckets": [
        {"key_as_string": "2026-10-28T10:00:00.000Z", "key": 1, "doc_count": 0},
        {"key_as_string": "2026-10-28T11:00:00.000Z", "key": 2, "doc_count": 3},
        {"key_as_string": "2026-10-28T12:00:00.000Z", "key": 3, "doc_count": 2}]}})
    r = norm(os_registry, "controller_log", raw, q)
    assert r.result_type == "series" and [p.value for p in r.series[0].points] == [0.0, 3.0, 2.0]
    assert r.metadata["observed_range"]["earliest"].startswith("2026-10-28T11")
    assert r.metadata["aggregation"]["type"] == "date_histogram"


def test_histogram_with_terms_subagg_makes_one_series_per_term(os_registry):
    q = {"aggs": {"t": {"date_histogram": {}, "aggs": {"svc": {"terms": {}}}}}}
    raw = resp(total=4, aggs={"t": {"buckets": [
        {"key_as_string": "2026-10-28T10:00:00.000Z", "key": 1, "doc_count": 2,
         "svc": {"buckets": [{"key": "hmscd", "doc_count": 2}]}},
        {"key_as_string": "2026-10-28T11:00:00.000Z", "key": 2, "doc_count": 2,
         "svc": {"buckets": [{"key": "hmscd", "doc_count": 1}, {"key": "hmpad", "doc_count": 1}]}}]}})
    r = norm(os_registry, "controller_log", raw, q)
    assert {s.labels["svc"] for s in r.series} == {"hmscd", "hmpad"}


def test_filters_aggregation_is_normalised(os_registry):
    q = {"aggs": {"access": {"filters": {"filters": {"browser": {}, "ipmi": {}}}}}}
    raw = resp(total=7, aggs={"access": {"buckets": {"browser": {"doc_count": 5}, "ipmi": {"doc_count": 2}}}})
    r = norm(os_registry, "ilo_events", raw, q)
    assert {b.key: b.doc_count for b in r.buckets} == {"browser": 5, "ipmi": 2}
    assert r.metadata["aggregation"]["type"] == "filters"


def test_bucket_keys_are_redacted(os_registry):
    raw = resp(total=1, aggs={"by_host": {"doc_count_error_upper_bound": 0, "sum_other_doc_count": 0,
                                           "buckets": [{"key": "10.1.2.3", "doc_count": 1}]}})
    r = norm(os_registry, "controller_log", raw, TERMS_Q)
    assert "10.1.2.3" not in r.buckets[0].key


def test_to_dict_includes_metadata_and_legacy_results_do_not():
    from app.normalizer import NormalizedResult
    assert "metadata" not in NormalizedResult(result_type="hits").to_dict()
    assert NormalizedResult(result_type="hits", metadata={"a": 1}).to_dict()["metadata"] == {"a": 1}
