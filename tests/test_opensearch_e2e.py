"""Phase 6: end-to-end tests of the OpenSearch path, for representative questions across all three index
families, plus failure and edge scenarios.

What is real: the Router->Generator->canonicalise->validate pipeline (`run_pipeline`) and `executor.execute_contract`
(the same two calls app/agent.py makes), the registry, the field catalog code, the guard, the normaliser and redaction.
What is mocked: the two LLM calls, and the OpenSearch HTTP client. Mappings are SYNTHETIC fixtures and response
bodies are hand-written, so these tests verify behaviour and schema-handling code paths; they are NOT live-cluster
integration tests and say nothing about the real schemas."""
import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app import executor, llm_client, opensearch_client, pipeline
from app.opensearch_catalog import runtime
from app.opensearch_catalog.field_catalog import FieldCatalogCache

FIX = Path(__file__).resolve().parent / "fixtures" / "opensearch"
REF = {"fabric_health": "references/opensearch-logs/fabric-health.md",
       "controller_log": "references/opensearch-logs/controller-log.md",
       "ilo_events": "references/opensearch-logs/ilo-events.md"}
INDEX = {"fabric_health": "slingshot_crayfabrichealth-*", "controller_log": "controllerlog-*", "ilo_events": "log_iml-*"}
MAPPING_FILE = {v: f"mapping_{k}.json" for k, v in INDEX.items()}
W = {"range": {"@timestamp": {"gte": "now-24h", "lte": "now"}}}
SHARDS_OK = {"total": 3, "successful": 3, "skipped": 0, "failed": 0}


@pytest.fixture()
def live_fields():
    """Field catalog served from the synthetic fixtures (optionally overridden per test)."""
    state = {"override": {}}

    def fetch(index_pattern):
        if index_pattern in state["override"]:
            return state["override"][index_pattern]
        return json.loads((FIX / MAPPING_FILE[index_pattern]).read_text())

    cache = FieldCatalogCache(fetch, ttl_seconds=60, stale_ttl_seconds=60, max_indices=50, failure_backoff_seconds=1)
    runtime.reset()
    with patch.object(runtime, "get_field_cache", return_value=cache):
        yield state
    runtime.reset()


def entry(pid, query, **kw):
    return {"status": "ok", "reference_used": REF[pid], "data_source": "opensearch", "pattern_id": pid,
            "measurement_used": {"type": "raw_metric", "name": f"{pid}_search", "source_metrics": []},
            "query": query, "explanation": "test", **kw}


def ask(skill_index, settings, families, generated, raw=None, search_side_effect=None, question="q"):
    """Run the pipeline then the executor. Returns (executed_contract, search_mock)."""
    router = MagicMock(parsed={"gate_stop": None, "panic_mode": False, "unresolved_topics": [],
                               "matched_references": [{"reference_path": REF[f], "data_source": "opensearch"} for f in families]})
    with patch.object(llm_client, "call_llm_json", side_effect=[router, MagicMock(parsed=generated)] * 3):
        contract = asyncio.run(pipeline.run_pipeline(question, skill_index, settings))
    outcomes = raw if isinstance(raw, list) else [raw]
    effect = search_side_effect or [opensearch_client.ExecutionOutcome(status="success", raw_body=o) if isinstance(o, dict) else o
                                    for o in outcomes]
    with patch.object(opensearch_client, "search", side_effect=effect) as search:
        executed = executor.execute_contract(contract, settings)
    return executed, search


def resp(hits=None, total=None, aggs=None, shards=None, timed_out=False, relation="eq"):
    hits = hits or []
    r = {"took": 5, "timed_out": timed_out, "_shards": shards or SHARDS_OK,
         "hits": {"total": {"value": len(hits) if total is None else total, "relation": relation}, "hits": hits}}
    if aggs is not None:
        r["aggregations"] = aggs
    return r


def terms(name, buckets, other=0, err=0):
    return {name: {"doc_count_error_upper_bound": err, "sum_other_doc_count": other,
                   "buckets": [{"key": k, "doc_count": c, **extra} for k, c, *rest in buckets for extra in [rest[0] if rest else {}]]}}


# =====================================================================================================================
# Family 1: fabric health
# =====================================================================================================================


def test_fabric_non_healthy_switches(skill_index, settings, live_fields):
    q = {"size": 0, "track_total_hits": True,
         "query": {"bool": {"filter": [W, {"exists": {"field": "fields.PhysicalSubContext"}}],
                            "must_not": [{"term": {"fields.PhysicalSubContext": "HEALTHY"}}]}},
         "aggs": {"by_switch": {"terms": {"field": "fields.Switch.keyword", "size": 20}}}}
    out, search = ask(skill_index, settings, ["fabric_health"], {"mode": "single", **entry("fabric_health", q)},
                      resp(total=42, aggs=terms("by_switch", [("x1000c0r3b0", 30), ("x1000c0r5b0", 12)])))
    sent = search.call_args[0]
    assert sent[1] == INDEX["fabric_health"]
    body = sent[2]
    assert body["size"] == 0 and body["timeout"] and "named_condition" not in json.dumps(body)
    assert "fields.PhysicalSubContext.keyword" in json.dumps(body) and "fields.Switch.keyword" in json.dumps(body)
    ex = out["execution"]
    assert ex["execution_status"] == "success" and ex["result_type"] == "buckets"
    assert [b["key"] for b in ex["buckets"]] == ["x1000c0r3b0", "x1000c0r5b0"]
    assert ex["metadata"]["count"] == {"value": 42, "relation": "eq", "exact": True}
    assert ex["metadata"]["query_window"]["seconds"] == 86400
    assert out["guard_notes"]["applied"]


def test_fabric_distinct_ports_per_switch_is_flagged_approximate(skill_index, settings, live_fields):
    q = {"size": 0, "track_total_hits": True,
         "query": {"bool": {"filter": [W, {"term": {"fields.PhysicalSubContext": "CRITICAL"}}]}},
         "aggs": {"by_switch": {"terms": {"field": "fields.Switch.keyword", "size": 10},
                                "aggs": {"ports": {"cardinality": {"field": "fields.Location.keyword"}}}}}}
    aggs = terms("by_switch", [("x1000c0r3b0", 90, {"ports": {"value": 4}})])
    out, _ = ask(skill_index, settings, ["fabric_health"], {"mode": "single", **entry("fabric_health", q)}, resp(total=90, aggs=aggs))
    ex = out["execution"]
    assert ex["buckets"][0]["metrics"] == {"ports": 4.0}
    agg = ex["metadata"]["aggregation"]
    assert agg["approximate"] and any("estimate" in r for r in agg["approximate_reasons"])


def test_fabric_unhealthy_named_condition_expands_to_not_healthy(skill_index, settings, live_fields):
    q = {"size": 0, "query": {"bool": {"filter": [W, {"named_condition": "abnormal_severity"}]}},
         "aggs": {"by_rack": {"terms": {"field": "fields.Rack.keyword"}}}}
    out, search = ask(skill_index, settings, ["fabric_health"], {"mode": "single", **entry("fabric_health", q)},
                      resp(total=7, aggs=terms("by_rack", [("x1000", 7)])))
    sent = json.dumps(search.call_args[0][2])
    assert "HEALTHY" in sent and "must_not" in sent and "named_condition" not in sent
    assert out["execution"]["execution_status"] == "success"


# =====================================================================================================================
# Family 2: controller log
# =====================================================================================================================


def test_controller_errors_per_host_with_truncated_buckets(skill_index, settings, live_fields):
    q = {"size": 0, "track_total_hits": True,
         "query": {"bool": {"filter": [W, {"named_condition": "abnormal_severity"}]}},
         "aggs": {"by_host": {"terms": {"field": "Resource.host.name.keyword", "size": 2},
                              "aggs": {"by_service": {"terms": {"field": "Resource.service.name.keyword", "size": 3}}}}}}
    aggs = terms("by_host", [("x1c0", 50, {"by_service": {"buckets": [{"key": "hmscd", "doc_count": 50}]}}),
                             ("x2c0", 20, {"by_service": {"buckets": [{"key": "hmpad", "doc_count": 20}]}})], other=130, err=3)
    out, search = ask(skill_index, settings, ["controller_log"], {"mode": "single", **entry("controller_log", q)}, resp(total=200, aggs=aggs))
    sent = json.dumps(search.call_args[0][2])
    assert "warning" not in sent and "error" in sent and "named_condition" not in sent
    ex = out["execution"]
    assert ex["truncated"] is True                               # 130 documents fall outside the 2 buckets
    agg = ex["metadata"]["aggregation"]
    assert agg["sum_other_doc_count"] == 130 and agg["approximate"] and agg["doc_count_error_upper_bound"] == 3
    assert ex["buckets"][0]["sub_buckets"][0]["key"] == "hmscd"


def test_controller_message_search_returns_redacted_rows_with_assessment(skill_index, settings, live_fields):
    q = {"size": 10, "query": {"bool": {"filter": [W, {"term": {"Resource.service.name": "hmscd"}},
                                                    {"match_phrase": {"Body": "LinkStatus Down"}}]}}}
    docs = [{"_source": {"@timestamp": "2026-10-28T10:05:00Z", "Severity": "error", "Resource": {"host": {"name": "x1c0"}, "service": {"name": "hmscd"}},
                         "Body": "LinkStatus Down port 12 reported by user admin from 10.1.2.3", "Secret": "x", "Timestamp": "Oct 28 10:05:00"}},
            {"_source": {"@timestamp": "2026-10-28T10:01:00Z", "Severity": "info", "Resource": {"host": {"name": "x1c0"}, "service": {"name": "hmscd"}},
                         "Body": "LinkStatus Up"}}]
    out, _ = ask(skill_index, settings, ["controller_log"], {"mode": "single", **entry("controller_log", q)}, resp(docs, total=2))
    ex = out["execution"]
    blob = json.dumps(ex)
    assert "admin" not in blob and "10.1.2.3" not in blob and "Secret" not in blob and "Oct 28" not in blob
    assert [h["severity_assessment"]["state"] for h in ex["hits"]] == ["abnormal", "normal"]
    assert ex["metadata"]["redaction"]["redacted_usernames"] == 1 and ex["metadata"]["redaction"]["redacted_ip_addresses"] == 1
    assert ex["metadata"]["observed_range"]["earliest"] == "2026-10-28T10:01:00Z"
    assert "NOT the full range" in ex["metadata"]["observed_range"]["basis"]


def test_controller_error_volume_over_time(skill_index, settings, live_fields):
    q = {"size": 0, "query": {"bool": {"filter": [W, {"named_condition": "abnormal_severity"}]}},
         "aggs": {"t": {"date_histogram": {"field": "@timestamp", "fixed_interval": "1h"}}}}
    aggs = {"t": {"buckets": [{"key_as_string": f"2026-10-28T{h:02d}:00:00.000Z", "key": h, "doc_count": c}
                              for h, c in ((8, 0), (9, 4), (10, 9))]}}
    out, _ = ask(skill_index, settings, ["controller_log"], {"mode": "single", **entry("controller_log", q)}, resp(total=13, aggs=aggs))
    ex = out["execution"]
    assert ex["result_type"] == "series" and [p["value"] for p in ex["series"][0]["points"]] == [0.0, 4.0, 9.0]
    assert ex["metadata"]["observed_range"]["earliest"].startswith("2026-10-28T09")   # first NON-EMPTY bucket
    assert ex["metadata"]["query_window"]["from_expression"] == "now-24h"


# =====================================================================================================================
# Family 3: iLO events
# =====================================================================================================================


def ilo_doc(ts, sev, vendor, msg="event", code=1017, **extra):
    src = {"@timestamp": ts, "Created": ts, "host": "bmc-1", "Message": msg, "Oem": {"Hpe": {"Code": code, **extra}}}
    if sev is not None:
        src["Severity"] = sev
    if vendor is not None:
        src["Oem"]["Hpe"]["Severity"] = vendor
    return {"_source": src}


def test_ilo_abnormal_events_or_semantics_and_provisional_flag(skill_index, settings, live_fields):
    q = {"size": 20, "sort": [{"@timestamp": "desc"}],
         "query": {"bool": {"filter": [W, {"named_condition": "abnormal_severity"}, {"term": {"Oem.Hpe.Repaired": False}}]}}}
    docs = [ilo_doc("2026-10-28T10:03:00Z", "OK", "Caution", "Fan degraded", 1031, RecommendedAction="Replace fan"),
            ilo_doc("2026-10-28T10:02:00Z", "Critical", "Informational", "PSU failure", 1040),
            ilo_doc("2026-10-28T10:01:00Z", "OK", None, "Event with one severity field missing", 1050)]
    out, search = ask(skill_index, settings, ["ilo_events"], {"mode": "single", **entry("ilo_events", q)}, resp(docs, total=3))
    sent = json.dumps(search.call_args[0][2])
    assert "Oem.Hpe.Severity" in sent and '"should"' in sent and "must_not" not in sent and "named_condition" not in sent
    ex = out["execution"]
    states = [(h["severity_assessment"]["state"], h["severity_assessment"]["conflict"]) for h in ex["hits"]]
    assert states == [("abnormal", True), ("abnormal", True), ("unknown", False)]        # missing field is NOT healthy
    assert ex["hits"][0]["severity_assessment"]["provisional_basis"] == ["Oem.Hpe.Severity"]
    assert ex["hits"][1]["severity_assessment"]["provisional_basis"] == []
    assert ex["metadata"]["severity_vocabulary_provisional"] == ["Oem.Hpe.Severity"]


def test_ilo_browser_versus_ipmi_access_counts(skill_index, settings, live_fields):
    q = {"size": 0, "track_total_hits": True, "query": {"bool": {"filter": [W]}},
         "aggs": {"access": {"filters": {"filters": {"browser": {"match_phrase": {"Message": "browser"}},
                                                     "ipmi": {"match_phrase": {"Message": "IPMI"}}}}}}}
    aggs = {"access": {"buckets": {"browser": {"doc_count": 120}, "ipmi": {"doc_count": 8}}}}
    out, _ = ask(skill_index, settings, ["ilo_events"], {"mode": "single", **entry("ilo_events", q)}, resp(total=140, aggs=aggs))
    ex = out["execution"]
    assert {b["key"]: b["doc_count"] for b in ex["buckets"]} == {"browser": 120, "ipmi": 8}
    assert ex["metadata"]["aggregation"]["type"] == "filters"


def test_ilo_top_event_codes_per_bmc(skill_index, settings, live_fields):
    q = {"size": 0, "query": {"bool": {"filter": [W]}},
         "aggs": {"by_host": {"terms": {"field": "host.keyword", "size": 5}, "aggs": {"by_code": {"terms": {"field": "Oem.Hpe.Code", "size": 3}}}}}}
    aggs = terms("by_host", [("bmc-1", 10, {"by_code": {"buckets": [{"key": 1017, "doc_count": 6}]}})])
    out, _ = ask(skill_index, settings, ["ilo_events"], {"mode": "single", **entry("ilo_events", q)}, resp(total=10, aggs=aggs))
    assert out["execution"]["buckets"][0]["sub_buckets"][0]["key"] == "1017"


# =====================================================================================================================
# Multi-family, safety boundaries and failure modes
# =====================================================================================================================


def test_multi_result_across_families_isolates_a_failing_entry(skill_index, settings, live_fields):
    ctl = entry("controller_log", {"size": 3, "query": {"bool": {"filter": [W]}}})
    ilo = entry("ilo_events", {"size": 3, "query": {"bool": {"filter": [W]}}})
    gen = {"mode": "multi", "results": [ctl, ilo], "synthesis": None}
    ok = resp([{"_source": {"@timestamp": "2026-10-28T10:00:00Z", "Severity": "info", "Body": "b"}}], total=1)
    out, search = ask(skill_index, settings, ["controller_log", "ilo_events"], gen,
                      search_side_effect=[opensearch_client.ExecutionOutcome(status="success", raw_body=ok),
                                          opensearch_client.ExecutionOutcome(status="timeout", error="Request exceeded 10s timeout.")])
    assert [r["execution"]["execution_status"] for r in out["results"]] == ["success", "timeout"]
    assert [c[0][1] for c in search.call_args_list] == [INDEX["controller_log"], INDEX["ilo_events"]]


def test_window_over_cap_asks_to_narrow_and_never_queries(skill_index, settings, live_fields):
    q = {"size": 3, "query": {"bool": {"filter": [{"range": {"@timestamp": {"gte": "now-30d", "lte": "now"}}}]}}}
    out, search = ask(skill_index, settings, ["controller_log"], {"mode": "single", **entry("controller_log", q)}, resp())
    assert out["status"] == "declined" and out["reason"] == "parameter_requires_clarification"
    assert "7d" in out["clarification"] and "execution" not in out and not search.called


@pytest.mark.parametrize("bad_query", [
    {"size": 3, "query": {"bool": {"filter": [W, {"wildcard": {"Body": "*"}}]}}},
    {"size": 3, "query": {"bool": {"filter": [W, {"term": {"Not.A.Field": "x"}}]}}},
    {"size": 5000, "query": {"bool": {"filter": [W]}}},
    {"size": 3, "query": {"match_all": {}}},
    {"size": 3, "query": {"bool": {"filter": [W]}}, "script_fields": {"a": {}}},
    {"size": 0, "query": {"bool": {"filter": [W]}}, "aggs": {"a": {"terms": {"field": "Body"}}}},
])
def test_invalid_queries_never_reach_the_cluster(skill_index, settings, live_fields, bad_query):
    out, search = ask(skill_index, settings, ["controller_log"], {"mode": "single", **entry("controller_log", bad_query)}, resp())
    assert out["status"] == "validation_failed" and "execution" not in out and not search.called


def test_llm_written_index_is_rejected_and_never_queried(skill_index, settings, live_fields):
    out, search = ask(skill_index, settings, ["controller_log"],
                      {"mode": "single", **entry("controller_log", {"size": 3, "query": {"bool": {"filter": [W]}}}, index="*")}, resp())
    assert out["status"] == "validation_failed" and not search.called


def test_field_missing_from_live_mapping_is_rejected_with_a_clear_reason(skill_index, settings, live_fields):
    mapping = json.loads((FIX / "mapping_controller_log.json").read_text())
    for idx in mapping.values():
        del idx["mappings"]["properties"]["Resource"]["properties"]["service"]
    live_fields["override"][INDEX["controller_log"]] = mapping
    q = {"size": 3, "query": {"bool": {"filter": [W, {"term": {"Resource.service.name": "hmscd"}}]}}}
    out, search = ask(skill_index, settings, ["controller_log"], {"mode": "single", **entry("controller_log", q)}, resp())
    assert out["status"] == "validation_failed" and not search.called
    assert "Resource.service.name" in out["explanation"]


def test_discovery_failure_fails_closed(skill_index, settings, live_fields):
    live_fields["override"][INDEX["controller_log"]] = None
    out, search = ask(skill_index, settings, ["controller_log"],
                      {"mode": "single", **entry("controller_log", {"size": 3, "query": {"bool": {"filter": [W]}}})}, resp())
    assert out["status"] != "ok" and not search.called


@pytest.mark.parametrize("status", ["timeout", "endpoint_unreachable", "endpoint_error"])
def test_backend_failures_become_typed_errors_without_leaking(skill_index, settings, live_fields, status):
    err = opensearch_client.ExecutionOutcome(status=status, error="boom: user alice at 10.1.2.3 " + "x" * 800)
    out, _ = ask(skill_index, settings, ["controller_log"], {"mode": "single", **entry("controller_log", {"size": 3, "query": {"bool": {"filter": [W]}}})},
                 search_side_effect=[err])
    ex = out["execution"]
    assert ex["execution_status"] == status and "alice" not in ex["error"] and "10.1.2.3" not in ex["error"] and len(ex["error"]) < 500


def test_partial_response_is_success_with_partial_metadata(skill_index, settings, live_fields):
    shards = {"total": 4, "successful": 3, "skipped": 0, "failed": 1,
              "failures": [{"index": "controllerlog-2026.10.28", "shard": 2, "reason": {"type": "es_rejected_execution_exception", "reason": "queue full"}}]}
    raw = resp([{"_source": {"@timestamp": "2026-10-28T10:00:00Z", "Severity": "error", "Body": "b"}}], total=1, shards=shards, timed_out=True)
    out, _ = ask(skill_index, settings, ["controller_log"], {"mode": "single", **entry("controller_log", {"size": 3, "query": {"bool": {"filter": [W]}}})}, raw)
    md = out["execution"]["metadata"]
    assert out["execution"]["execution_status"] == "success" and md["partial"]
    assert set(md["partial_reasons"]) == {"timed_out", "shard_failures"}
    assert md["shard_failures"][0]["type"] == "es_rejected_execution_exception" and md["shards"]["failed"] == 1


def test_all_shards_failed_is_an_error_not_an_empty_answer(skill_index, settings, live_fields):
    shards = {"total": 2, "successful": 0, "failed": 2, "failures": [{"index": "i", "shard": 0, "reason": {"type": "t", "reason": "r"}}]}
    out, _ = ask(skill_index, settings, ["controller_log"], {"mode": "single", **entry("controller_log", {"size": 3, "query": {"bool": {"filter": [W]}}})},
                 resp(total=0, shards=shards))
    assert out["execution"]["execution_status"] == "endpoint_error" and out["execution"]["metadata"]["shards"]["failed"] == 2


def test_empty_result_is_explicit_and_carries_the_window(skill_index, settings, live_fields):
    out, _ = ask(skill_index, settings, ["ilo_events"], {"mode": "single", **entry("ilo_events", {"size": 3, "query": {"bool": {"filter": [W]}}})}, resp(total=0))
    ex = out["execution"]
    assert ex["execution_status"] == "empty_result" and ex["metadata"]["count"]["exact"] is True
    assert ex["metadata"]["query_window"]["seconds"] == 86400 and not ex["metadata"]["partial"]


def test_lower_bound_total_is_never_presented_as_exact(skill_index, settings, live_fields):
    docs = [{"_source": {"@timestamp": "2026-10-28T10:00:00Z", "Severity": "info", "Body": "b"}}]
    out, _ = ask(skill_index, settings, ["controller_log"], {"mode": "single", **entry("controller_log", {"size": 1, "query": {"bool": {"filter": [W]}}})},
                 resp(docs, total=10000, relation="gte"))
    md = out["execution"]["metadata"]
    assert md["count"] == {"value": 10000, "relation": "gte", "exact": False} and md["rows"]["more_available"]


@pytest.mark.parametrize("pid", ["fabric_health", "controller_log", "ilo_events"])
def test_count_only_question_succeeds_with_the_total_in_metadata(skill_index, settings, live_fields, pid):
    """"How many ... in the last day?" -> size 0, no aggregation, no rows. Not an empty result."""
    q = {"size": 0, "track_total_hits": True, "query": {"bool": {"filter": [W, {"named_condition": "abnormal_severity"}]}}}
    out, search = ask(skill_index, settings, [pid], {"mode": "single", **entry(pid, q)}, resp(total=37))
    ex = out["execution"]
    assert search.call_args[0][2]["size"] == 0
    assert ex["execution_status"] == "success" and ex["metadata"]["count"] == {"value": 37, "relation": "eq", "exact": True}
    assert ex["metadata"]["rows"]["returned"] == 0 and ex["metadata"]["rows"]["more_available"]


def test_count_only_question_with_no_matches_is_an_empty_result(skill_index, settings, live_fields):
    q = {"size": 0, "track_total_hits": True, "query": {"bool": {"filter": [W]}}}
    out, _ = ask(skill_index, settings, ["controller_log"], {"mode": "single", **entry("controller_log", q)}, resp(total=0))
    assert out["execution"]["execution_status"] == "empty_result" and out["execution"]["metadata"]["count"]["value"] == 0


def test_row_cap_truncation_is_reported(skill_index, settings, live_fields):
    settings.max_hits_per_result = 2
    docs = [{"_source": {"@timestamp": f"2026-10-28T10:0{i}:00Z", "Severity": "info", "Body": "b"}} for i in range(5)]
    out, _ = ask(skill_index, settings, ["controller_log"], {"mode": "single", **entry("controller_log", {"size": 5, "query": {"bool": {"filter": [W]}}})},
                 resp(docs, total=5))
    ex = out["execution"]
    assert ex["truncated"] is True and ex["original_count"] == 5 and len(ex["hits"]) == 2


@pytest.mark.parametrize("pid,agg,aggs,status,metric_type", [
    ("controller_log", {"terms": {"field": "Resource.host.name.keyword"}}, terms("a", []), "empty_result", "terms"),   # no buckets
    ("ilo_events", {"max": {"field": "Oem.Hpe.Code"}}, {"a": {"value": None}}, "empty_result", "metric"),             # no value over zero docs
    ("ilo_events", {"value_count": {"field": "Oem.Hpe.Code"}}, {"a": {"value": 0}}, "success", "metric"),             # a real zero
])
def test_aggregation_edge_cases(skill_index, settings, live_fields, pid, agg, aggs, status, metric_type):
    q = {"size": 0, "query": {"bool": {"filter": [W]}}, "aggs": {"a": agg}}
    out, _ = ask(skill_index, settings, [pid], {"mode": "single", **entry(pid, q)}, resp(total=0, aggs=aggs))
    ex = out["execution"]
    assert ex["execution_status"] == status and ex["metadata"]["aggregation"]["type"] == metric_type
    if status == "success":
        assert ex["series"][0]["points"][0]["value"] == 0.0     # zero is a value, not an absence


def test_histogram_with_only_empty_buckets_has_no_observed_range(skill_index, settings, live_fields):
    q = {"size": 0, "query": {"bool": {"filter": [W]}}, "aggs": {"t": {"date_histogram": {"field": "@timestamp", "fixed_interval": "1h"}}}}
    aggs = {"t": {"buckets": [{"key_as_string": "2026-10-28T10:00:00.000Z", "key": 1, "doc_count": 0}]}}
    out, _ = ask(skill_index, settings, ["controller_log"], {"mode": "single", **entry("controller_log", q)}, resp(total=0, aggs=aggs))
    assert out["execution"]["metadata"]["observed_range"] is None


def test_prometheus_results_are_unchanged_next_to_opensearch_code(skill_index, settings):
    """Regression guard: a Prometheus-only question never touches the OpenSearch machinery and its result carries no
    OpenSearch metadata."""
    from app import label_discovery, prometheus_client
    router = MagicMock(parsed={"gate_stop": None, "panic_mode": False, "unresolved_topics": [],
                               "matched_references": [{"reference_path": "references/node-exporter/cpu.md", "data_source": "prometheus"}]})
    gen = {"mode": "single", "status": "ok", "reference_used": "references/node-exporter/cpu.md",
           "measurement_used": {"type": "raw_metric", "name": "node_load1", "source_metrics": []}, "data_source": "prometheus",
           "query": "node_load1", "time_range": {"from": "now-1h", "to": "now", "step": "60s"}, "explanation": "t"}
    fake = prometheus_client.ExecutionOutcome(status="success", raw_data={"resultType": "matrix", "result": [{"metric": {"instance": "n"}, "values": [[1735689600, "1.2"]]}]})
    with patch.object(llm_client, "call_llm_json", side_effect=[router, MagicMock(parsed=gen)]), \
         patch.object(label_discovery, "discover_label_details_for_metrics", return_value={"node_load1": label_discovery.LabelDetails(keys=[], values={})}), \
         patch.object(runtime, "build_request_context") as os_ctx, \
         patch.object(prometheus_client, "query_range", return_value=fake), \
         patch.object(opensearch_client, "search") as search:
        contract = asyncio.run(pipeline.run_pipeline("load", skill_index, settings))
        out = executor.execute_contract(contract, settings)
    assert out["execution"]["execution_status"] == "success" and out["execution"]["result_type"] == "series"
    assert "metadata" not in out["execution"] and "guard_notes" not in out
    assert not os_ctx.called and not search.called
