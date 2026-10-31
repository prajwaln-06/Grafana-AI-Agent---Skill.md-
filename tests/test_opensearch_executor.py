"""Executor OpenSearch branch: registry-resolved index, defence-in-depth re-checks, partial-result
handling, redacted diagnostics. The OpenSearch HTTP client is always mocked (no live cluster)."""
from unittest.mock import patch

from app import executor, opensearch_client

W = {"range": {"@timestamp": {"gte": "now-24h", "lte": "now"}}}
Q = {"size": 5, "query": {"bool": {"filter": [W]}}}


def entry(**kw):
    return {"status": "ok", "data_source": "opensearch", "pattern_id": "controller_log", "query": Q, **kw}


def outcome(raw, status="success", error=None):
    return opensearch_client.ExecutionOutcome(status=status, raw_body=raw, error=error)


def raw_hits(n=1, total=None, shards=None, timed_out=False, relation="eq"):
    return {"took": 3, "timed_out": timed_out, "_shards": shards or {"total": 1, "successful": 1, "failed": 0},
            "hits": {"total": {"value": n if total is None else total, "relation": relation},
                     "hits": [{"_source": {"@timestamp": "2026-10-28T10:00:00Z", "Severity": "error", "Body": "b"}}] * n}}


def run(settings, e, out):
    with patch.object(opensearch_client, "search", return_value=out) as m:
        res = executor.execute_contract(e, settings)
    return res["execution"], m


def test_index_comes_from_registry_not_from_the_entry(settings):
    ex, m = run(settings, entry(index="controllerlog-*"), outcome(raw_hits()))
    assert m.call_args[0][1] == "controllerlog-*" and ex["execution_status"] == "success"


def test_index_that_disagrees_with_pattern_is_not_executed(settings):
    ex, m = run(settings, entry(index="*"), outcome(raw_hits()))
    assert ex["execution_status"] == "not_executed" and not m.called


def test_unknown_pattern_is_not_executed(settings):
    ex, m = run(settings, entry(pattern_id="nope"), outcome(raw_hits()))
    assert ex["execution_status"] == "not_executed" and not m.called


def test_legacy_index_that_equals_a_registered_pattern_is_accepted(settings):
    e = entry(index="controllerlog-*")
    e.pop("pattern_id")
    ex, m = run(settings, e, outcome(raw_hits()))
    assert ex["execution_status"] == "success" and m.call_args[0][1] == "controllerlog-*"


def test_wildcard_everything_index_is_not_executed(settings):
    e = entry(index="*")
    e.pop("pattern_id")
    ex, m = run(settings, e, outcome(raw_hits()))
    assert ex["execution_status"] == "not_executed" and not m.called


def test_forbidden_body_is_not_executed_even_if_it_slipped_past_validation(settings):
    bad = {"size": 5, "script_fields": {"x": {}}, "query": {"bool": {"filter": [W]}}}
    ex, m = run(settings, entry(query=bad), outcome(raw_hits()))
    assert ex["execution_status"] == "not_executed" and not m.called


def test_window_over_cap_is_not_executed_and_not_rewritten(settings):
    q = {"size": 5, "query": {"bool": {"filter": [{"range": {"@timestamp": {"gte": "now-30d", "lte": "now"}}}]}}}
    ex, m = run(settings, entry(query=q), outcome(raw_hits()))
    assert ex["execution_status"] == "not_executed" and not m.called
    assert "maximum" in ex["error"]


def test_missing_time_range_is_not_executed(settings):
    ex, m = run(settings, entry(query={"size": 5, "query": {"match_all": {}}}), outcome(raw_hits()))
    assert ex["execution_status"] == "not_executed" and not m.called


def test_oversized_hit_request_is_not_executed(settings):
    ex, m = run(settings, entry(query={**Q, "size": 10000}), outcome(raw_hits()))
    assert ex["execution_status"] == "not_executed" and not m.called


def test_success_reports_query_window_separately_from_observed_range(settings):
    ex, _ = run(settings, entry(), outcome(raw_hits()))
    md = ex["metadata"]
    assert md["query_window"]["from_expression"] == "now-24h" and md["query_window"]["seconds"] == 86400
    assert md["observed_range"]["basis"].startswith("returned rows only")
    assert ex["resolved_time_range"]["to_expression"] == "now"


def test_partial_results_are_surfaced_not_hidden(settings):
    shards = {"total": 3, "successful": 2, "failed": 1, "failures": [
        {"index": "controllerlog-x", "shard": 1, "reason": {"type": "oom", "reason": "boom"}}]}
    ex, _ = run(settings, entry(), outcome(raw_hits(shards=shards)))
    assert ex["execution_status"] == "success" and ex["metadata"]["partial"] is True


def test_all_shards_failed_is_an_endpoint_error_with_metadata(settings):
    shards = {"total": 2, "successful": 0, "failed": 2, "failures": [
        {"index": "i", "shard": 0, "reason": {"type": "t", "reason": "r"}}]}
    ex, _ = run(settings, entry(), outcome(raw_hits(0, shards=shards)))
    assert ex["execution_status"] == "endpoint_error" and ex["metadata"]["shards"]["failed"] == 2


def test_empty_result_still_carries_metadata_and_window(settings):
    raw = raw_hits(0)
    ex, _ = run(settings, entry(), outcome(raw, status="empty_result"))
    assert ex["execution_status"] == "empty_result"
    assert ex["metadata"]["count"] == {"value": 0, "relation": "eq", "exact": True}
    assert ex["metadata"]["query_window"]["seconds"] == 86400


def test_empty_result_that_was_partial_says_so(settings):
    raw = raw_hits(0, timed_out=True)
    ex, _ = run(settings, entry(), outcome(raw, status="empty_result"))
    assert ex["metadata"]["partial"] is True


def test_endpoint_errors_are_redacted_and_bounded(settings):
    err = "failed for user admin at 10.0.0.1 body={'secret': 'x'} " + "y" * 2000
    ex, _ = run(settings, entry(), outcome(None, status="endpoint_error", error=err))
    assert ex["execution_status"] == "endpoint_error"
    assert "admin" not in ex["error"] and "10.0.0.1" not in ex["error"] and len(ex["error"]) < 500


def test_redaction_policy_comes_from_settings(settings):
    settings.opensearch_redact_ip_mode = "off"
    raw = raw_hits()
    raw["hits"]["hits"][0]["_source"]["Body"] = "from 10.1.2.3"
    ex, _ = run(settings, entry(), outcome(raw))
    assert "10.1.2.3" in ex["hits"][0]["body"]
    assert ex["metadata"]["redaction"]["policy"]["ip_mode"] == "off"


def test_whole_document_is_never_returned(settings):
    raw = raw_hits()
    raw["hits"]["hits"][0]["_source"]["Password"] = "hunter2"
    ex, _ = run(settings, entry(), outcome(raw))
    assert "hunter2" not in str(ex)


def test_registry_load_failure_is_an_error_not_an_unchecked_query(settings, tmp_path):
    settings.opensearch_registry_path = tmp_path / "missing.json"
    from app.opensearch_catalog import runtime
    runtime.reset()
    try:
        ex, m = run(settings, entry(), outcome(raw_hits()))
    finally:
        runtime.reset()
    assert ex["execution_status"] == "endpoint_error" and not m.called


def test_prometheus_entry_is_unaffected_by_the_opensearch_changes(settings):
    from app import prometheus_client
    e = {"status": "ok", "data_source": "prometheus", "query": "up", "time_range": {"from": "now-1h", "to": "now", "step": "60s"}}
    fake = prometheus_client.ExecutionOutcome(status="success", raw_data={"resultType": "matrix", "result": []})
    with patch.object(prometheus_client, "query_range", return_value=fake), \
         patch.object(opensearch_client, "search") as m:
        res = executor.execute_contract(e, settings)
    assert "metadata" not in res["execution"] and not m.called
