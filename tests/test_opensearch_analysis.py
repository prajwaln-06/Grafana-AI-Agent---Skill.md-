"""The five multi-step analyses (app/opensearch_catalog/analysis.py), in the friend's contract
(`pattern_id`, `raw_metric`) and under her rule: a parameter the model wrote is checked and REJECTED
when it is out of range, never clamped; documented defaults are filled only when a key is absent and
are recorded in `applied`.

What is real: registry, field catalog (built from the SYNTHETIC fixture mappings through the real
opensearch_client code), guard, canonicalisation, validator, executor, results/redaction/severity.
What is faked: only the HTTP session (tests/os_fixtures.FakeCluster)."""
import copy
import json

import pytest

from app import executor, validator
from app.opensearch_catalog import canonical, guard, runtime
from app.opensearch_catalog.registry import RegistryError, load_registry
from tests.os_fixtures import REF, FakeCluster, agg_body, error, hits_body, os_entry, window


@pytest.fixture()
def cluster(monkeypatch):
    return FakeCluster().install(monkeypatch)


def ctx_for(settings, pid):
    return runtime.build_request_context([{"reference_path": REF[pid], "data_source": "opensearch"}], settings)


def prepare(settings, entry):
    """What the pipeline does after the Generator: canonicalise, then validate."""
    ctx = ctx_for(settings, entry["pattern_id"])
    fixed = canonical.canonicalise_contract({"mode": "single", **entry}, ctx)
    res = validator.validate_contract(fixed, known_references={REF[entry["pattern_id"]]},
                                      known_datasources={"opensearch"}, opensearch_context=ctx)
    return fixed, res


def check(settings, entry):
    return guard.check_entry(entry, ctx_for(settings, entry["pattern_id"]))


def run(settings, entry):
    fixed, res = prepare(settings, entry)
    assert res.passed, res.reason
    return fixed, executor.execute_contract(fixed, settings)["execution"]


# =====================================================================================================
# registry: the analyses block is validated at load
# =====================================================================================================

def _registry_with(tmp_path, mutate):
    from app.opensearch_catalog.registry import DEFAULT_REGISTRY_PATH
    data = json.loads(DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8"))
    mutate({p["pattern_id"]: p for p in data["patterns"]})
    path = tmp_path / "reg.json"
    path.write_text(json.dumps(data))
    return path


def test_packaged_registry_analyses_load_and_are_all_unverified(os_registry):
    got = {p.pattern_id: sorted(p.analyses) for p in os_registry.patterns}
    assert got == {"fabric_health": ["absence", "recurrence"],
                   "controller_log": ["context_window", "text_ratio_shortfall"],
                   "ilo_events": ["ingest_lag"]}
    assert all(cfg["verified"] is False for p in os_registry.patterns for cfg in p.analyses.values())


@pytest.mark.parametrize("mutate,needle", [
    (lambda p: p["fabric_health"]["analyses"].update({"teleport": {"verified": False}}), "unknown analysis type"),
    (lambda p: p["fabric_health"]["analyses"]["absence"].pop("verified"), "'verified' is required"),
    (lambda p: p["fabric_health"]["analyses"]["absence"].update({"entity_role": "nope"}), "must name a role"),
    (lambda p: p["fabric_health"]["analyses"]["absence"].update({"entity_role": "text"}), "not groupable"),
    (lambda p: p["fabric_health"]["analyses"]["absence"].update({"heartbeat_interval_seconds": 0}), "positive integer"),
    (lambda p: p["fabric_health"]["analyses"]["absence"].update({"normalise_strip_suffix_regex": "b(\\d+$"}), "does not compile"),
    (lambda p: p["fabric_health"]["analyses"]["absence"].update({"surprise": 1}), "unknown parameter"),
    (lambda p: p["fabric_health"]["analyses"]["recurrence"].update({"interval": "1hour"}), "not a duration"),
    (lambda p: p["controller_log"]["analyses"]["text_ratio_shortfall"].update({"regex": "(\\d+) switches"}), "exactly two capture groups"),
    (lambda p: p["controller_log"]["analyses"]["text_ratio_shortfall"].update({"compare": "lte"}), "must be one of"),
    (lambda p: p["ilo_events"]["analyses"]["ingest_lag"].update({"event_time_fields": ["Severity"]}), "display-only time fields"),
    (lambda p: p["controller_log"]["analyses"]["context_window"].update({"default_before": "5 minutes"}), "not a duration"),
])
def test_bad_analysis_configuration_stops_the_registry_from_loading(tmp_path, mutate, needle):
    with pytest.raises(RegistryError, match=needle):
        load_registry(_registry_with(tmp_path, mutate))


# =====================================================================================================
# guard: intent / analysis keys -- reject, never repair
# =====================================================================================================

def absence_entry(**analysis):
    q = {"size": 0, "query": {"bool": {"filter": [window("now-24h"),
                                                  {"term": {"fields.PhysicalContext.keyword": "runtime.heartbeat"}}]}},
         "aggs": {"sw": {"terms": {"field": "fields.Switch.keyword"}}}}
    return os_entry("fabric_health", q, intent="absence", analysis={"type": "absence", **analysis})


def test_simple_questions_without_the_new_keys_are_unchanged(settings, cluster):
    q = {"size": 5, "query": {"bool": {"filter": [window()]}}}
    r = check(settings, os_entry("controller_log", q))
    assert r.ok and r.analysis is None and r.intent is None


@pytest.mark.parametrize("intent", ["rows", "count", "top_n", "trend", "distinct"])
def test_simple_intents_are_descriptive_only(settings, cluster, intent):
    q = {"size": 5, "query": {"bool": {"filter": [window()]}}}
    r = check(settings, os_entry("controller_log", q, intent=intent))
    assert r.ok and r.intent == intent and r.analysis is None


def test_unknown_intent_is_rejected(settings, cluster):
    r = check(settings, os_entry("controller_log", {"query": {"bool": {"filter": [window()]}}}, intent="guess"))
    assert not r.ok and r.code == "intent_invalid"


@pytest.mark.parametrize("extra,needle", [
    ({"intent": "absence"}, "needs an `analysis` object"),
    ({"analysis": "absence"}, "must be an object"),
    ({"analysis": {"type": "telepathy"}}, "is not one of"),
    ({"intent": "rows", "analysis": {"type": "absence"}}, "contradicts"),
])
def test_malformed_analysis_keys_are_rejected(settings, cluster, extra, needle):
    e = absence_entry()
    e.pop("intent"), e.pop("analysis")
    e.update(extra)
    r = check(settings, e)
    assert not r.ok and r.code == "analysis_invalid" and needle in r.reason


def test_an_analysis_the_family_does_not_configure_is_rejected_with_unmapped_advice(settings, cluster):
    q = {"size": 0, "query": {"bool": {"filter": [window()]}},
         "aggs": {"h": {"terms": {"field": "Resource.host.name.keyword"}}}}
    r = check(settings, os_entry("controller_log", q, analysis={"type": "absence"}))
    assert not r.ok and r.code == "analysis_invalid" and "not available for 'controller_log'" in r.reason
    assert "unmapped" in r.reason


def test_absence_defaults_come_from_the_registry_and_are_recorded(settings, cluster):
    r = check(settings, absence_entry())
    assert r.ok and r.analysis == {"type": "absence", "recent_window": "3m"}         # 60 s x 3 missed
    assert any("recent_window defaulted to 3m" in a and "verified=False" in a for a in r.applied)
    assert r.query["aggs"]["sw"]["terms"]["size"] == 500                               # analysis maximum, recorded
    assert any("terms size defaulted to 500" in a for a in r.applied)


def test_absence_written_values_are_kept_or_rejected_never_adjusted(settings, cluster):
    r = check(settings, absence_entry(recent_window="10m"))
    assert r.ok and r.analysis["recent_window"] == "10m"
    for value, needle in [("30s", "shorter than one heartbeat"), ("48h", "must be shorter than the query's time window"),
                          ("soon", "not a duration"), ("3 minutes", "not a duration")]:
        r = check(settings, absence_entry(recent_window=value))
        assert not r.ok and r.code == "analysis_invalid" and needle in r.reason, value
    e = absence_entry()
    e["analysis"]["surprise"] = 1
    r = check(settings, e)
    assert not r.ok and "unknown parameter" in r.reason


def test_absence_query_shape_is_enforced(settings, cluster):
    e = absence_entry()
    e["query"]["aggs"] = {"sw": {"terms": {"field": "fields.Rack.keyword"}}}
    r = check(settings, e)
    assert not r.ok and r.code == "analysis_invalid" and "must group by" in r.reason
    e = absence_entry()
    e["query"]["aggs"]["sw"]["aggs"] = {"n": {"cardinality": {"field": "fields.Port.keyword"}}}
    assert "sub-aggregations" in check(settings, e).reason
    e = absence_entry()
    e["query"]["aggs"]["sw"]["terms"]["size"] = 501
    r = check(settings, e)
    assert not r.ok and r.code == "limit_exceeded"                                    # above the analysis maximum


def test_the_large_terms_size_is_only_for_absence(settings, cluster):
    q = {"size": 0, "query": {"bool": {"filter": [window()]}},
         "aggs": {"sw": {"terms": {"field": "fields.Switch.keyword", "size": 500}}}}
    r = check(settings, os_entry("fabric_health", q))
    assert not r.ok and r.code == "limit_exceeded" and "1 to 50" in r.reason


def context_entry(**analysis):
    q = {"query": {"bool": {"filter": [window("now-24h"), {"match_phrase": {"Body": "LinkStatus Down"}}]}}}
    return os_entry("controller_log", q, intent="context_window", analysis={"type": "context_window", **analysis})


def test_context_window_defaults_and_limits(settings, cluster):
    r = check(settings, context_entry())
    assert r.ok and r.analysis == {"type": "context_window", "before": "5m", "after": "5m"}
    assert r.query["size"] == 3 and any("events to expand" in a for a in r.applied)
    r = check(settings, context_entry(before="9h"))
    assert not r.ok and r.code == "analysis_invalid" and "longer than the maximum of 60m" in r.reason
    assert "has not been shortened" in r.reason and r.user_message
    e = context_entry()
    e["query"]["size"] = 50
    r = check(settings, e)
    assert not r.ok and "at most 3 events" in r.reason
    e = context_entry()
    e["query"]["aggs"] = {"h": {"terms": {"field": "Resource.host.name.keyword"}}}
    e["query"]["size"] = 0
    assert "rows query without aggregations" in check(settings, e).reason


def test_context_filter_is_checked_like_any_clause_and_may_not_carry_time(settings, cluster):
    cf = {"term": {"Resource.service.name": "hmscd"}}
    r = check(settings, context_entry(context_filter=cf))
    assert r.ok and r.analysis["context_filter"] == {"term": {"Resource.service.name.keyword": "hmscd"}}
    assert any("analysis.context_filter" in a for a in r.applied)                   # the friend's recorded substitution
    again = check(settings, {**os_entry("controller_log", r.query), "analysis": r.analysis})
    assert again.ok and again.analysis == r.analysis and not any("context_filter" in a for a in again.applied)
    for bad, code_or_needle in [({"range": {"@timestamp": {"gte": "now-1h", "lte": "now"}}}, "must not contain a time range"),
                                ({"term": {"Nope": "x"}}, "unknown_field"),
                                ({"wildcard": {"Body": "*x*"}}, "not allowed")]:
        r = check(settings, context_entry(context_filter=bad))
        assert not r.ok and (code_or_needle in r.reason or r.code == code_or_needle), bad


def test_rows_analyses_require_the_fields_they_read_and_never_add_them(settings, cluster):
    q = {"size": 20, "query": {"bool": {"filter": [window()]}}, "_source": ["host", "Message"]}
    r = check(settings, os_entry("ilo_events", q, analysis={"type": "ingest_lag"}))
    assert not r.ok and "must include ['@timestamp', 'Created']" in r.reason
    q["_source"] = ["@timestamp", "Created", "host", "Message"]
    assert check(settings, os_entry("ilo_events", q, analysis={"type": "ingest_lag"})).ok
    q = {"size": 0, "query": {"bool": {"filter": [window()]}}}
    r = check(settings, os_entry("ilo_events", q, analysis={"type": "ingest_lag"}))
    assert not r.ok and "rows query" in r.reason
    q = {"size": 20, "query": {"bool": {"filter": [window()]}}, "_source": ["@timestamp", "Severity"]}
    r = check(settings, os_entry("controller_log", q, analysis={"type": "text_ratio_shortfall"}))
    assert not r.ok and "message field" in r.reason


def recurrence_entry(interval="1h", field="fields.Location.keyword"):
    q = {"size": 0, "query": {"bool": {"filter": [window("now-7d"), {"term": {"fields.PhysicalSubContext": "CRITICAL"}}]}},
         "aggs": {"loc": {"terms": {"field": field, "size": 10},
                          "aggs": {"per": {"date_histogram": {"field": "@timestamp", "fixed_interval": interval}}}}}}
    return os_entry("fabric_health", q, analysis={"type": "recurrence"})


def test_recurrence_interval_must_be_the_registry_definition(settings, cluster):
    assert check(settings, recurrence_entry()).ok
    r = check(settings, recurrence_entry(interval="2h"))
    assert not r.ok and "must be '1h'" in r.reason
    r = check(settings, recurrence_entry(field="fields.Rack.keyword"))
    assert not r.ok and "must group by" in r.reason


def test_guard_is_idempotent_for_every_analysis(settings, cluster):
    entries = [absence_entry(), context_entry(), context_entry(context_filter={"term": {"Resource.service.name": "hmscd"}}),
               recurrence_entry(),
               os_entry("ilo_events", {"size": 10, "query": {"bool": {"filter": [window()]}}}, analysis={"type": "ingest_lag"}),
               os_entry("controller_log", {"query": {"bool": {"filter": [window(), {"match_phrase": {"Body": "switches available"}}]}}},
                        analysis={"type": "text_ratio_shortfall"})]
    for e in entries:
        first = check(settings, e)
        assert first.ok, (e["analysis"], first.reason)
        second = check(settings, {**e, "query": first.query, "analysis": first.analysis})
        assert second.ok and second.query == first.query and second.analysis == first.analysis
        assert not [a for a in second.applied if "defaulted" in a]


def test_validator_refuses_an_analysis_that_skipped_canonicalisation(settings, cluster):
    fixed, res = prepare(settings, absence_entry())
    assert res.passed
    tampered = copy.deepcopy(fixed)
    tampered["analysis"]["recent_window"] = "30s"            # e.g. edited after canonicalisation
    ctx = ctx_for(settings, "fabric_health")
    res = validator.validate_contract(tampered, known_references={REF["fabric_health"]}, known_datasources={"opensearch"},
                                      opensearch_context=ctx)
    assert not res.passed and "analysis_invalid" in res.reason
    tampered = copy.deepcopy(fixed)
    tampered["analysis"].pop("recent_window")                 # default not filled in -> not canonical
    res = validator.validate_contract(tampered, known_references={REF["fabric_health"]}, known_datasources={"opensearch"},
                                      opensearch_context=ctx)
    assert not res.passed and "canonical" in res.reason


# =====================================================================================================
# execution: absence
# =====================================================================================================

def absence_handler(baseline, recent, other=0):
    def handler(index, dsl):
        gte = dsl["query"]["bool"]["filter"][0]["range"]["@timestamp"]["gte"]
        names = baseline if gte == "now-24h" else recent
        return agg_body({"sw": {"doc_count_error_upper_bound": 0, "sum_other_doc_count": other,
                                "buckets": [{"key": n, "doc_count": 10} for n in names]}}, total=100)
    return handler


def test_absence_finds_switches_that_stopped_and_ignores_the_asic_suffix(settings, cluster):
    cluster.handler = absence_handler(["x1000c0r1b0", "x1000c1r2b0", "x1000c2r3b0"], ["x1000c0r1b1", "x1000c1r2b0"])
    _, block = run(settings, absence_entry())
    assert block["execution_status"] == "success" and block["result_type"] == "buckets"
    assert [b["key"] for b in block["buckets"]] == ["x1000c2r3b0"]           # x1000c0r1 is alive as ...b1
    a = block["analysis"]
    assert (a["type"], a["baseline_entities"], a["recent_entities"], a["missing_count"]) == ("absence", 3, 2, 1)
    assert a["recent_window"] == "3m" and a["threshold_verified"] is False and a["complete"] is True
    assert a["queries_executed"] == 2 and len(cluster.searches) == 2
    recent_q = cluster.searches[1][1]
    assert recent_q["query"]["bool"]["filter"][0] == {"range": {"@timestamp": {"gte": "now-3m", "lte": "now"}}}
    assert recent_q["query"]["bool"]["filter"][1] == {"term": {"fields.PhysicalContext.keyword": "runtime.heartbeat"}}
    assert recent_q["timeout"] and recent_q["aggs"]["sw"]["terms"]["size"] == 500


def test_absence_with_nobody_missing_is_a_real_answer(settings, cluster):
    cluster.handler = absence_handler(["a"], ["a"])
    _, block = run(settings, absence_entry())
    assert block["execution_status"] == "success" and block["buckets"] == [] and block["analysis"]["missing_count"] == 0


def test_absence_warns_on_total_outage_and_on_a_truncated_entity_list(settings, cluster):
    cluster.handler = absence_handler(["a", "b"], [])
    _, block = run(settings, absence_entry())
    assert any("no_recent_reports" in w for w in block["analysis"]["warnings"])
    cluster.handler = absence_handler(["a"], ["a"], other=40)
    _, block = run(settings, absence_entry())
    assert block["analysis"]["complete"] is False and any("incomplete_entity_list" in w for w in block["analysis"]["warnings"])


def test_absence_with_an_absolute_window_narrows_from_its_end(settings, cluster):
    e = absence_entry()
    e["query"]["query"]["bool"]["filter"][0] = window("2026-10-28T00:00:00Z", "2026-10-28T12:00:00Z")
    cluster.handler = lambda i, d: agg_body({"sw": {"buckets": [{"key": "a", "doc_count": 1}]}})
    run(settings, e)
    assert cluster.searches[1][1]["query"]["bool"]["filter"][0]["range"]["@timestamp"] == \
        {"gte": "2026-10-28T11:57:00Z", "lte": "2026-10-28T12:00:00Z"}


def test_a_failing_follow_up_becomes_an_error_block_with_the_reason(settings, cluster):
    calls = []

    def handler(index, dsl):
        calls.append(1)
        return absence_handler(["a"], [])(index, dsl) if len(calls) == 1 else error(500, "shard down")
    cluster.handler = handler
    _, block = run(settings, absence_entry())
    assert block["execution_status"] == "endpoint_error" and "shard down" in block["error"]
    assert block["analysis"]["completed"] is False and block["analysis"]["queries_executed"] == 2


def test_a_partial_follow_up_is_reported_as_partial(settings, cluster):
    def handler(index, dsl):
        body = absence_handler(["a"], ["a"])(index, dsl)
        if dsl["query"]["bool"]["filter"][0]["range"]["@timestamp"]["gte"] != "now-24h":
            body["timed_out"] = True
        return body
    cluster.handler = handler
    _, block = run(settings, absence_entry())
    assert block["metadata"]["partial"] is True and "timed_out" in block["metadata"]["partial_reasons"]


# =====================================================================================================
# execution: context window
# =====================================================================================================

ANCHOR = {"@timestamp": "2026-10-28T10:00:00Z", "Body": "LinkStatus Down on j104p1", "Severity": "error",
          "Resource": {"host": {"name": "x1000c0r1b0"}, "service": {"name": "hmscd"}}}


def context_handler(anchor=ANCHOR):
    def handler(index, dsl):
        if dsl["sort"] == [{"@timestamp": {"order": "desc"}}]:                # the anchor query (newest first)
            return hits_body([anchor], ids=["A"])
        docs = [{"@timestamp": "2026-10-28T09:58:00Z", "Body": "before it"},
                {**anchor},
                {"@timestamp": "2026-10-28T10:02:00Z", "Body": "after it, login by alice from 10.1.2.3"}]
        return hits_body(docs, ids=["b", "A", "c"])
    return handler


def test_context_window_scopes_to_the_same_host_and_labels_rows(settings, cluster):
    cluster.handler = context_handler()
    _, block = run(settings, context_entry())
    assert block["result_type"] == "hits" and block["execution_status"] == "success"
    assert [h["analysis"]["position"] for h in block["hits"]] == ["before", "anchor", "after"]
    assert [h["analysis"]["offset_seconds"] for h in block["hits"]] == [-120.0, 0.0, 120.0]
    follow = cluster.searches[1][1]
    flt = follow["query"]["bool"]["filter"]
    assert flt[0] == {"range": {"@timestamp": {"gte": "2026-10-28T09:55:00Z", "lte": "2026-10-28T10:05:00Z"}}}
    assert flt[1] == {"term": {"Resource.host.name.keyword": "x1000c0r1b0"}}      # a scalar copied from the event
    assert follow["sort"] == [{"@timestamp": {"order": "asc"}}] and follow["size"] == 100
    a = block["analysis"]
    assert a["anchors_found"] == 1 and a["anchors"][0]["rows_before"] == 1 and a["anchors"][0]["rows_after"] == 1


def test_context_rows_go_through_the_friends_redaction_and_severity(settings, cluster):
    cluster.handler = context_handler()
    _, block = run(settings, context_entry())
    after = block["hits"][2]
    assert "alice" not in json.dumps(after) and "10.1.2.3" not in json.dumps(after)
    assert block["metadata"]["redaction"]["redacted_usernames"] >= 1
    assert block["hits"][1]["severity_assessment"]["state"] == "abnormal"          # 'error' in controller_log


def test_context_window_with_a_context_filter(settings, cluster):
    cluster.handler = context_handler()
    _, block = run(settings, context_entry(context_filter={"term": {"Resource.service.name": "hmscd"}}, before="2m"))
    flt = cluster.searches[1][1]["query"]["bool"]["filter"]
    assert flt[1] == {"term": {"Resource.service.name.keyword": "hmscd"}}
    assert flt[0]["range"]["@timestamp"]["gte"] == "2026-10-28T09:58:00Z"
    assert block["analysis"]["scope"] == "context_filter"


def test_context_window_never_copies_a_non_scalar_document_value_into_a_query(settings, cluster):
    bad = {**ANCHOR, "Resource": {"host": {"name": {"$ne": "x"}}}}
    cluster.handler = context_handler(anchor=bad)
    _, block = run(settings, context_entry())
    assert len(cluster.searches) == 1                                              # no follow-up was sent
    assert any("no plain" in w for w in block["analysis"]["warnings"])


def test_context_window_without_any_matching_event(settings, cluster):
    cluster.handler = lambda i, d: hits_body([])
    _, block = run(settings, context_entry())
    assert block["execution_status"] == "empty_result" and len(cluster.searches) == 1
    assert any("no_anchor_event" in w for w in block["analysis"]["warnings"])


def test_context_window_flags_a_truncated_window(settings, cluster):
    def handler(index, dsl):
        if dsl["sort"] == [{"@timestamp": {"order": "desc"}}]:
            return hits_body([ANCHOR], ids=["A"])
        return hits_body([{"@timestamp": "2026-10-28T09:59:00Z", "Body": "x"}], total=250)
    cluster.handler = handler
    _, block = run(settings, context_entry())
    assert block["analysis"]["anchors"][0]["truncated"] is True
    assert block["metadata"]["count"]["relation"] == "gte" and block["metadata"]["count"]["exact"] is False


# =====================================================================================================
# execution: ingest lag, n/m shortfall, recurrence
# =====================================================================================================

def test_ingest_lag_statistics_on_the_returned_rows(settings, cluster):
    cluster.handler = lambda i, d: hits_body([
        {"@timestamp": "2026-10-28T10:05:00Z", "Created": "2026-10-28T10:00:00Z", "host": "bmc1", "Message": "a"},
        {"@timestamp": "2026-10-28T10:02:00Z", "Created": "2026-10-28T10:00:00Z", "host": "bmc1", "Message": "b"},
        {"@timestamp": "2026-10-28T10:01:00Z", "host": "bmc1", "Message": "no event time"}], index="log_iml-2026.10.26")
    e = os_entry("ilo_events", {"size": 20, "query": {"bool": {"filter": [window(), {"term": {"host": "bmc1"}}]}}},
                 analysis={"type": "ingest_lag"})
    fixed, block = run(settings, e)
    assert {"@timestamp", "Created"} <= set(fixed["query"]["_source"])            # the friend's default projection
    a = block["analysis"]
    assert (a["rows_returned"], a["rows_measured"], a["min_seconds"], a["median_seconds"], a["max_seconds"]) == \
        (3, 2, 120.0, 210.0, 300.0)
    assert [h.get("analysis") for h in block["hits"]] == [{"lag_seconds": 300.0}, {"lag_seconds": 120.0}, None]
    assert a["event_time_verified"] is False and "sample" in a["explanation"]


def test_ingest_lag_with_nothing_measurable_says_so(settings, cluster):
    cluster.handler = lambda i, d: hits_body([{"@timestamp": "2026-10-28T10:05:00Z", "host": "h"}], index="log_iml-x")
    _, block = run(settings, os_entry("ilo_events", {"size": 5, "query": {"bool": {"filter": [window()]}}},
                                      analysis={"type": "ingest_lag"}))
    assert block["analysis"]["rows_measured"] == 0 and any("no_lag_measurable" in w for w in block["analysis"]["warnings"])


def shortfall_entry(size=50):
    q = {"size": size, "query": {"bool": {"filter": [window(), {"match_phrase": {"Body": "switches available"}}]}}}
    return os_entry("controller_log", q, intent="text_ratio_shortfall", analysis={"type": "text_ratio_shortfall"})


def test_shortfall_keeps_only_rows_below_expected(settings, cluster):
    cluster.handler = lambda i, d: hits_body([
        {"@timestamp": "2026-10-28T10:00:00Z", "Body": "Switch Availability: 30/32 switches available"},
        {"@timestamp": "2026-10-28T10:01:00Z", "Body": "Switch Availability: 32/32 Switches Available"},
        {"@timestamp": "2026-10-28T10:02:00Z", "Body": "something else entirely"}])
    _, block = run(settings, shortfall_entry())
    assert len(block["hits"]) == 1 and block["hits"][0]["analysis"] == {"available": 30, "expected": 32}
    a = block["analysis"]
    assert (a["rows_scanned"], a["rows_matching_pattern"], a["rows_flagged"]) == (3, 2, 1)
    assert a["scan_complete"] is True and a["pattern_verified"] is False
    assert block["metadata"]["count"] == {"value": 1, "relation": "eq", "exact": True}


def test_shortfall_scan_limit_is_reported_and_the_count_is_a_lower_bound(settings, cluster):
    cluster.handler = lambda i, d: hits_body([{"@timestamp": "2026-10-28T10:00:00Z", "Body": "1/2 switches available"}],
                                             total=10000, relation="gte")
    _, block = run(settings, shortfall_entry(size=1))
    assert block["analysis"]["scan_complete"] is False and any("scan_limited" in w for w in block["analysis"]["warnings"])
    assert block["metadata"]["count"]["relation"] == "gte"


def test_shortfall_with_no_short_rows_is_a_real_answer(settings, cluster):
    cluster.handler = lambda i, d: hits_body([{"@timestamp": "2026-10-28T10:00:00Z", "Body": "32/32 switches available"}])
    _, block = run(settings, shortfall_entry())
    assert block["execution_status"] == "success" and block["hits"] == [] and block["analysis"]["rows_flagged"] == 0


def test_recurrence_ranks_by_separate_intervals_not_by_volume(settings, cluster):
    cluster.handler = lambda i, d: agg_body({"loc": {"doc_count_error_upper_bound": 0, "sum_other_doc_count": 0, "buckets": [
        {"key": "x1c0r1j1p1", "doc_count": 500, "per": {"buckets": [{"key_as_string": "a", "key": 1, "doc_count": 500}]}},
        {"key": "x1c0r1j9p2", "doc_count": 30, "per": {"buckets": [
            {"key_as_string": "a", "key": 1, "doc_count": 10}, {"key_as_string": "b", "key": 2, "doc_count": 0},
            {"key_as_string": "c", "key": 3, "doc_count": 10}, {"key_as_string": "d", "key": 4, "doc_count": 10}]}}]}},
        total=530)
    _, block = run(settings, recurrence_entry())
    got = [(b["key"], b["doc_count"], b["metrics"]["active_intervals"]) for b in block["buckets"]]
    assert got == [("x1c0r1j9p2", 30, 3.0), ("x1c0r1j1p1", 500, 1.0)]
    a = block["analysis"]
    assert a["interval"] == "1h" and a["intervals_in_window"] == 168 and a["interval_verified"] is False


# =====================================================================================================
# the follow-up queries are checked by the guard again before they run
# =====================================================================================================

def test_every_follow_up_is_re_checked_by_the_guard(settings, cluster, monkeypatch):
    seen = []
    real = guard.check_entry

    def spy(entry, ctx, **kw):
        seen.append(kw.get("followup_of"))
        return real(entry, ctx, **kw)
    monkeypatch.setattr(guard, "check_entry", spy)
    cluster.handler = absence_handler(["a"], ["a"])
    fixed, _ = prepare(settings, absence_entry())
    seen.clear()
    executor.execute_contract(fixed, settings)
    # one re-validation of the whole entry, then one check per query that was sent
    assert seen == [None, "absence", "absence"]


def test_a_rejected_follow_up_is_never_sent(settings, cluster, monkeypatch):
    cluster.handler = context_handler()
    fixed, _ = prepare(settings, context_entry())
    real = guard.check_entry
    calls = []

    def reject_follow_ups(entry, ctx, **kw):
        calls.append(1)
        if kw.get("followup_of") and len(calls) > 2:
            return guard.GuardResult(ok=False, code="bad_clause", reason="test rejection")
        return real(entry, ctx, **kw)
    monkeypatch.setattr(guard, "check_entry", reject_follow_ups)
    block = executor.execute_contract(fixed, settings)["execution"]
    assert block["execution_status"] == "not_executed" and "test rejection" in block["error"]
    assert len(cluster.searches) == 1                                              # only the anchor query went out


def test_executor_refuses_an_analysis_entry_that_was_never_validated(settings, cluster):
    raw = absence_entry(recent_window="30s")                                       # invalid, and not canonicalised
    raw["index"] = "slingshot_crayfabrichealth-*"
    block = executor.execute_contract({"mode": "single", **raw}, settings)["execution"]
    assert block["execution_status"] == "not_executed" and "Analysis rejected" in block["error"]
    assert cluster.searches == []


def test_analysis_needs_live_field_discovery(settings, monkeypatch):
    FakeCluster(mapping_status=503).install(monkeypatch)
    fixed, res = prepare(settings, absence_entry())
    assert not res.passed                                                          # fails closed before execution
