"""
Every question in the mentor's catalogue, run through the WHOLE chain with the friend's REAL skill files:

    Router (mock LLM: picks the friend's reference file) -> Generator (mock LLM: writes the contract the model
    would write, in the friend's contract -- pattern_id, raw_metric, never an index)
    -> canonicalise (guard) -> validator -> executor -> real opensearch_client code (fake network)
    -> results.py (redaction, count exactness, severity, metadata)

Ported from the reference implementation's suite and adapted to this branch: exact stored severity case
(controller `error`, fabric `CRITICAL`), group-by on the confirmed `.keyword` paths of the SYNTHETIC fixture
mappings, `{"named_condition": "abnormal_severity"}` instead of hand-written severity lists, and the five
questions one query cannot answer use the new optional `intent` + `analysis` keys. Two questions the friend's
skill files keep `unmapped` ("unusually frequent", "failed logins": no baseline / wording not confirmed) are
asserted to stay unmapped and to send nothing to OpenSearch.

Only the two LLM calls and the network are faked. What this proves is that each question's natural query shape
is accepted, executes, and comes back in the right shape. Whether the real model WRITES these shapes is what
the live smoke test (docs/SMOKE_TEST.md) checks.
"""
import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from app import executor, llm_client, pipeline
from tests.os_fixtures import INDEX, REF, FakeCluster, agg_body, hits_body, os_entry, window

FAB, CTL, ILO = REF["fabric_health"], REF["controller_log"], REF["ilo_events"]


def F(*clauses, gte="now-24h", must_not=()):
    b = {"filter": [window(gte=gte), *clauses]}
    if must_not:
        b["must_not"] = list(must_not)
    return {"bool": b}


ABN = {"named_condition": "abnormal_severity"}


def fake_aggs(aggs):
    out = {}
    for name, spec in aggs.items():
        sub = spec.get("aggs") or {}
        kind = next(k for k in spec if k != "aggs")
        if kind == "terms":
            out[name] = {"doc_count_error_upper_bound": 0, "sum_other_doc_count": 0,
                         "buckets": [{"key": f"k{i}", "doc_count": 5 - i, **fake_aggs(sub)} for i in range(2)]}
        elif kind == "filters":
            out[name] = {"buckets": {k: {"doc_count": 4, **fake_aggs(sub)} for k in spec["filters"]["filters"]}}
        elif kind == "date_histogram":
            out[name] = {"buckets": [{"key_as_string": f"2026-10-28T0{i}:00:00Z", "key": i, "doc_count": 3,
                                      **fake_aggs(sub)} for i in range(3)]}
        else:
            out[name] = {"value": 4}
    return out


DOCS = [{"@timestamp": "2026-10-28T10:00:00Z", "Body": "row one", "Message": "Login by user admin from 10.0.0.9",
         "host": "bmc1", "Severity": "error", "fields": {"Value": "Port disabled", "Port": "j104p1"}},
        {"@timestamp": "2026-10-28T10:01:00Z", "Body": "row two", "Message": "Logout", "host": "bmc1",
         "Severity": "error", "fields": {"Value": "Port disabled", "Port": "j104p1"}}]


def generic(index, dsl):
    if dsl.get("aggs"):
        return agg_body(fake_aggs(dsl["aggs"]), total=10)
    if dsl.get("size") == 0:
        return hits_body([], total=42)
    return hits_body(DOCS, index=index.replace("*", "2026.10.28"))


def ask(skill_index, settings, monkeypatch, question, refs, contract, handler=None):
    cluster = FakeCluster(handler=handler or generic).install(monkeypatch)
    router = MagicMock(parsed={"gate_stop": None, "panic_mode": False, "unresolved_topics": [],
                               "matched_references": [{"reference_path": r, "data_source": "opensearch"}
                                                      for r in (refs if isinstance(refs, list) else [refs])]})
    with patch.object(llm_client, "call_llm_json", side_effect=[router, MagicMock(parsed=contract)]):
        result = asyncio.run(pipeline.run_pipeline(question, skill_index, settings))
    assert result.get("status") != pipeline.INTERNAL_VALIDATION_FAILED_STATUS, result
    return result, executor.execute_contract(result, settings), cluster


def _top(b, n=2):
    assert b["execution_status"] == "success" and b["result_type"] == "buckets" and len(b["buckets"]) == n, b


def _rows(b):
    assert b["execution_status"] == "success" and b["result_type"] == "hits" and len(b["hits"]) == 2, b


def _sent(c):
    return json.dumps(c.searches[0][1])


# (id, question, pattern_id, intent, query, check(execution, cluster))
CASES = [
    # ---------------- fabric health ----------------
    ("F1", "Which fabric components reported a non-healthy status in the last 24 h?", "fabric_health", "top_n",
     {"size": 0, "query": F(ABN), "aggs": {"sw": {"terms": {"field": "fields.Switch.keyword", "size": 20}}}},
     lambda b, c: (_top(b), "HEALTHY" in _sent(c) and "must_not" in _sent(c) or pytest.fail())),
    ("F2", "How many CRITICAL events occurred per switch, and how many distinct locations?", "fabric_health", "top_n",
     {"size": 0, "query": F({"term": {"fields.PhysicalSubContext": "CRITICAL"}}),
      "aggs": {"sw": {"terms": {"field": "fields.Switch.keyword"},
                      "aggs": {"locs": {"cardinality": {"field": "fields.Location.keyword"}}}}}},
     lambda b, c: (_top(b), b["buckets"][0]["metrics"] == {"locs": 4.0} or pytest.fail(),
                   b["metadata"]["aggregation"]["approximate"] or pytest.fail(),
                   '"fields.PhysicalSubContext.keyword": "CRITICAL"' in _sent(c) or pytest.fail())),
    ("F3", "What health states exist besides HEALTHY?", "fabric_health", "top_n",
     {"size": 0, "query": F(), "aggs": {"states": {"terms": {"field": "fields.PhysicalSubContext.keyword"}}}},
     lambda b, c: _top(b)),
    ("F4", "Which ports were disabled due to excessive flapping?", "fabric_health", "top_n",
     {"size": 0, "query": F({"match_phrase": {"fields.Value": "excessive flapping"}}),
      "aggs": {"ports": {"terms": {"field": "fields.Location.keyword"}}}}, lambda b, c: _top(b)),
    ("F5", "What recommendations were reported for port j104p1?", "fabric_health", "rows",
     {"size": 10, "query": F({"term": {"fields.Port.keyword": "j104p1"}}),
      "_source": ["@timestamp", "fields.Port", "fields.Value"]}, lambda b, c: _rows(b)),
    ("F6", "Which switches reported linkerrors.linkautorestartexhaustederror?", "fabric_health", "top_n",
     {"size": 0, "query": F({"term": {"fields.PhysicalContext.keyword": "linkerrors.linkautorestartexhaustederror"}}),
      "aggs": {"sw": {"terms": {"field": "fields.Switch.keyword"}}}}, lambda b, c: _top(b)),
    ("F7", "Which racks and chassis have the most unhealthy events?", "fabric_health", "top_n",
     {"size": 0, "query": F(ABN), "aggs": {"rack": {"terms": {"field": "fields.Rack.keyword"},
                                                     "aggs": {"chassis": {"terms": {"field": "fields.Chassis.keyword"}}}}}},
     lambda b, c: (_top(b), len(b["buckets"][0]["sub_buckets"]) == 2 or pytest.fail())),
    ("F8", "Show critical events for switch x1000c3r1.", "fabric_health", "rows",
     {"size": 20, "query": F({"prefix": {"fields.Switch.keyword": "x1000c3r1"}},
                             {"term": {"fields.PhysicalSubContext.keyword": "CRITICAL"}})}, lambda b, c: _rows(b)),
    ("F9", "Which ports have recurring faults?", "fabric_health", "recurrence",
     {"size": 0, "query": F({"term": {"fields.PhysicalSubContext.keyword": "CRITICAL"}}, gte="now-7d"),
      "aggs": {"loc": {"terms": {"field": "fields.Location.keyword"},
                       "aggs": {"h": {"date_histogram": {"field": "@timestamp", "fixed_interval": "1h"}}}}}},
     lambda b, c: (_top(b), "active_intervals" in b["buckets"][0]["metrics"] or pytest.fail(),
                   b["analysis"]["type"] == "recurrence" or pytest.fail())),
    ("F10", "Which switches stopped sending heartbeats?", "fabric_health", "absence",
     {"size": 0, "query": F({"term": {"fields.PhysicalContext.keyword": "runtime.heartbeat"}}),
      "aggs": {"sw": {"terms": {"field": "fields.Switch.keyword"}}}},
     lambda b, c: (b["analysis"]["type"] == "absence" or pytest.fail(), b["analysis"]["queries_executed"] == 2 or pytest.fail())),
    # ---------------- controller log ----------------
    ("C1", "Which controllers reported the most errors in the last 24 h?", "controller_log", "top_n",
     {"size": 0, "track_total_hits": True, "query": F(ABN),
      "aggs": {"h": {"terms": {"field": "Resource.host.name.keyword"},
                     "aggs": {"svc": {"terms": {"field": "Resource.service.name.keyword", "size": 5}}}}}},
     lambda b, c: (_top(b), '"error"' in _sent(c) and '"critical"' in _sent(c) or pytest.fail())),
    ("C2", "Which controllers reported I2C failures, and which bus numbers?", "controller_log", "rows",
     {"size": 50, "query": F({"match_phrase": {"Body": "i2c ioctl failed"}})}, lambda b, c: _rows(b)),
    ("C3", "Are the same errors recurring on a controller?", "controller_log", "top_n",
     {"size": 0, "query": F({"term": {"Severity.keyword": "error"}}),
      "aggs": {"h": {"terms": {"field": "Resource.host.name.keyword"},
                     "aggs": {"svc": {"terms": {"field": "Resource.service.name.keyword"}}}}}}, lambda b, c: _top(b)),
    ("C4", 'Which hosts reported "LinkStatus Down"?', "controller_log", "top_n",
     {"size": 0, "query": F({"match_phrase": {"Body": "LinkStatus Down"}}),
      "aggs": {"h": {"terms": {"field": "Resource.host.name.keyword"}}}}, lambda b, c: _top(b)),
    ("C4b", "Which ports are down on x1000c2r3b0?", "controller_log", "rows",
     {"size": 50, "query": F({"term": {"Resource.host.name.keyword": "x1000c2r3b0"}},
                             {"match_phrase": {"Body": "LinkStatus Down"}})}, lambda b, c: _rows(b)),
    ("C7", "Which services generate the most error logs?", "controller_log", "top_n",
     {"size": 0, "query": F(ABN), "aggs": {"s": {"terms": {"field": "Resource.service.name.keyword"}}}},
     lambda b, c: _top(b)),
    ("C8", "Show all hmpad errors for x1000c6s7b1.", "controller_log", "rows",
     {"size": 50, "query": F({"term": {"Resource.service.name.keyword": "hmpad"}},
                             {"term": {"Resource.host.name.keyword": "x1000c6s7b1"}}, ABN)}, lambda b, c: _rows(b)),
    ("C10", "Show controller logs excluding routine CRON messages.", "controller_log", "rows",
     {"size": 50, "query": F(must_not=[{"term": {"Resource.service.name.keyword": "CRON"}}])}, lambda b, c: _rows(b)),
    # ---------------- iLO ----------------
    ("I1", "Who logged into bmc1 in the last 24 h?", "ilo_events", "rows",
     {"size": 50, "query": F({"match_phrase": {"Message": "login"}}, {"term": {"host.keyword": "bmc1"}})},
     lambda b, c: (_rows(b), "admin" not in json.dumps(b["hits"]) or pytest.fail(),
                   b["metadata"]["redaction"]["redacted_usernames"] >= 1 or pytest.fail())),
    ("I2", "Which BMCs were accessed from 172.24.0.1?", "ilo_events", "top_n",
     {"size": 0, "query": F({"match_phrase": {"Message": "172.24.0.1"}}), "aggs": {"h": {"terms": {"field": "host.keyword"}}}},
     lambda b, c: _top(b)),
    ("I3", "Show all login and logout events for root.", "ilo_events", "rows",
     {"size": 50, "query": F({"terms": {"Oem.Hpe.Code": [1017, 1018]}}, {"match_phrase": {"Message": "root"}})},
     lambda b, c: _rows(b)),
    ("I4", "Browser versus IPMI/RMCP login counts.", "ilo_events", "top_n",
     {"size": 0, "query": F(), "aggs": {"kind": {"filters": {"filters": {
         "browser": {"match_phrase": {"Message": "browser"}}, "ipmi": {"match_phrase": {"Message": "RMCP"}}}}}}},
     lambda b, c: (_top(b), {x["key"] for x in b["buckets"]} == {"browser", "ipmi"} or pytest.fail())),
    ("I7", "Security and administration events for bmc1.", "ilo_events", "rows",
     {"size": 50, "query": F({"term": {"Oem.Hpe.Categories.keyword": "Security"}}, {"term": {"host.keyword": "bmc1"}})},
     lambda b, c: _rows(b)),
    ("I8", "Events with a severity other than OK or Informational.", "ilo_events", "rows",
     {"size": 50, "query": F(ABN)},
     lambda b, c: (_rows(b), "Caution" in _sent(c) and "Warning" in _sent(c) or pytest.fail(),
                   b["metadata"]["severity_vocabulary_provisional"] == ["Oem.Hpe.Severity"] or pytest.fail())),
    ("I9", "Warning or critical events marked unrepaired, with recommended actions.", "ilo_events", "rows",
     {"size": 50, "query": F(ABN, {"term": {"Oem.Hpe.Repaired": False}}),
      "_source": ["@timestamp", "host", "Message", "Oem.Hpe.Severity", "Oem.Hpe.RecommendedAction"]}, lambda b, c: _rows(b)),
    ("I10", "Which event codes recur most per host?", "ilo_events", "top_n",
     {"size": 0, "query": F(), "aggs": {"h": {"terms": {"field": "host.keyword"},
                                             "aggs": {"code": {"terms": {"field": "Oem.Hpe.Code"}}}}}}, lambda b, c: _top(b)),
    # ---------------- generic shapes (trend, distinct, count) ----------------
    ("H1", "How did controller errors trend per hour over the last day?", "controller_log", "trend",
     {"size": 0, "query": F(ABN), "aggs": {"t": {"date_histogram": {"field": "@timestamp", "fixed_interval": "1h"}}}},
     lambda b, c: (b["result_type"] == "series" and len(b["series"][0]["points"]) == 3) or pytest.fail()),
    ("D1", "How many distinct hosts reported errors?", "controller_log", "distinct",
     {"size": 0, "query": F(ABN), "aggs": {"u": {"cardinality": {"field": "Resource.host.name.keyword"}}}},
     lambda b, c: b["metadata"]["aggregation"]["approximate"] is True or pytest.fail()),
    ("N1", "How many CRITICAL fabric events happened today?", "fabric_health", "count",
     {"size": 0, "track_total_hits": True, "query": F({"term": {"fields.PhysicalSubContext.keyword": "CRITICAL"}})},
     lambda b, c: (b["execution_status"] == "success" and b["metadata"]["count"]["value"] == 42) or pytest.fail()),
]

ANALYSIS_HANDLERS = {
    "absence": lambda i, d: agg_body({"sw": {"buckets": [{"key": "x1000c0r1b0", "doc_count": 5}], "sum_other_doc_count": 0}}),
    "recurrence": lambda i, d: agg_body({"loc": {"sum_other_doc_count": 0, "buckets": [
        {"key": "a", "doc_count": 3, "h": {"buckets": [{"key_as_string": "x", "key": 1, "doc_count": 3}]}},
        {"key": "b", "doc_count": 2, "h": {"buckets": [{"key_as_string": "x", "key": 1, "doc_count": 1},
                                                      {"key_as_string": "y", "key": 2, "doc_count": 1}]}}]}}),
}


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_mentor_question(case, skill_index, settings, monkeypatch):
    qid, question, pid, intent, query, check = case
    extra = {"intent": intent}
    if intent in ANALYSIS_HANDLERS:
        extra["analysis"] = {"type": intent}
    contract = {"mode": "single", **os_entry(pid, query, **extra)}
    result, executed, cluster = ask(skill_index, settings, monkeypatch, question, REF[pid], contract,
                                    ANALYSIS_HANDLERS.get(intent))
    assert result["status"] == "ok" and result["index"] == INDEX[pid], "the index comes from the registry"
    assert "named_condition" not in json.dumps(cluster.searches), "the named condition is always expanded"
    block = executed["execution"]
    assert block["execution_status"] == "success", block
    check(block, cluster)


# ---- the questions that need a dedicated flow ----

def test_C5_fewer_switches_available_than_expected(skill_index, settings, monkeypatch):
    q = {"size": 50, "query": F({"match_phrase": {"Body": "switches available"}},
                                {"term": {"Resource.service.name.keyword": "fabric-manager"}})}
    contract = {"mode": "single", **os_entry("controller_log", q, intent="text_ratio_shortfall",
                                             analysis={"type": "text_ratio_shortfall"})}
    handler = lambda i, d: hits_body([  # noqa: E731
        {"@timestamp": "2026-10-28T10:00:00Z", "Body": "Switch Availability: 30/32 switches available"},
        {"@timestamp": "2026-10-28T10:01:00Z", "Body": "Switch Availability: 32/32 switches available"}])
    _, executed, _ = ask(skill_index, settings, monkeypatch, "Fabric-manager messages with fewer switches available "
                         "than expected?", CTL, contract, handler)
    block = executed["execution"]
    assert len(block["hits"]) == 1 and block["analysis"]["rows_flagged"] == 1
    assert block["hits"][0]["analysis"] == {"available": 30, "expected": 32}


def test_C9_hmscd_before_and_after_a_link_down_event(skill_index, settings, monkeypatch):
    q = {"size": 1, "query": F({"match_phrase": {"Body": "LinkStatus Down"}})}
    contract = {"mode": "single", **os_entry("controller_log", q, intent="context_window", analysis={
        "type": "context_window", "before": "5m", "after": "5m",
        "context_filter": {"term": {"Resource.service.name.keyword": "hmscd"}}})}

    def handler(index, dsl):
        if dsl["sort"] == [{"@timestamp": {"order": "desc"}}]:
            return hits_body([{"@timestamp": "2026-10-28T10:00:00Z", "Body": "LinkStatus Down"}], ids=["anchor"])
        return hits_body([{"@timestamp": "2026-10-28T09:59:00Z", "Body": "hmscd before"},
                          {"@timestamp": "2026-10-28T10:01:00Z", "Body": "hmscd after"}], ids=["x", "y"])
    _, executed, cluster = ask(skill_index, settings, monkeypatch, "What did hmscd report before and after the "
                               "link-down event?", CTL, contract, handler)
    block = executed["execution"]
    assert [h["analysis"]["position"] for h in block["hits"]] == ["before", "after"]
    assert cluster.searches[1][1]["sort"] == [{"@timestamp": {"order": "asc"}}]
    assert {"term": {"Resource.service.name.keyword": "hmscd"}} in cluster.searches[1][1]["query"]["bool"]["filter"]


def test_I11_how_long_after_creation_were_events_ingested(skill_index, settings, monkeypatch):
    contract = {"mode": "single", **os_entry("ilo_events", {"size": 100, "query": F()}, intent="ingest_lag",
                                             analysis={"type": "ingest_lag"})}
    handler = lambda i, d: hits_body([{"@timestamp": "2026-10-28T10:30:00Z", "Created": "2026-10-28T10:00:00Z",  # noqa: E731
                                       "host": "h", "Message": "m"}], index="log_iml-2026.10.26")
    _, executed, _ = ask(skill_index, settings, monkeypatch, "How long after creation were events ingested?", ILO,
                         contract, handler)
    assert executed["execution"]["analysis"]["max_seconds"] == 1800.0


def test_C6_link_down_near_availability_problems_runs_both_halves_proximity_is_next_phase(skill_index, settings, monkeypatch):
    e1 = os_entry("controller_log", {"size": 20, "query": F({"match_phrase": {"Body": "LinkStatus Down"}})}, intent="rows")
    e2 = os_entry("controller_log", {"size": 50, "query": F({"match_phrase": {"Body": "switches available"}})},
                  intent="text_ratio_shortfall", analysis={"type": "text_ratio_shortfall"})
    contract = {"mode": "multi", "results": [e1, e2], "synthesis": None}
    handler = lambda i, d: hits_body([{"@timestamp": "2026-10-28T10:00:00Z", "Body": "LinkStatus Down 3/4 switches available"}])  # noqa: E731
    _, executed, _ = ask(skill_index, settings, monkeypatch, "Did link-down events occur near fabric availability "
                         "problems?", CTL, contract, handler)
    assert [r["execution"]["execution_status"] for r in executed["results"]] == ["success", "success"]
    assert executed["synthesis"] is None and "proximity" not in json.dumps(executed)   # no pretend correlation


@pytest.mark.parametrize("qid,question,why", [
    ("I5", "Which BMCs have unusually frequent logins?", "no baseline for 'unusually frequent' is defined"),
    ("I6", "Any failed login attempts?", "the failed-login wording has not been confirmed"),
])
def test_questions_the_friends_skill_keeps_unmapped_stay_unmapped(skill_index, settings, monkeypatch, qid, question, why):
    contract = {"mode": "single", "status": "unmapped", "explanation": f"Not supported by a plain query: {why}."}
    result, executed, cluster = ask(skill_index, settings, monkeypatch, question, ILO, contract)
    assert result["status"] == "unmapped" and "execution" not in executed and cluster.searches == []


def test_must_not_on_a_severity_field_is_refused_in_favour_of_the_named_condition(skill_index, settings, monkeypatch):
    bad = {"mode": "single", **os_entry("ilo_events", {"size": 50, "query": F(must_not=[
        {"terms": {"Oem.Hpe.Severity.keyword": ["OK", "Informational"]}}])})}
    FakeCluster(handler=generic).install(monkeypatch)
    router = MagicMock(parsed={"gate_stop": None, "panic_mode": False, "unresolved_topics": [],
                               "matched_references": [{"reference_path": ILO, "data_source": "opensearch"}]})
    with patch.object(llm_client, "call_llm_json", side_effect=[router, MagicMock(parsed=bad), MagicMock(parsed=bad)]):
        result = asyncio.run(pipeline.run_pipeline("severity other than OK", skill_index, settings))
    assert result["status"] == pipeline.INTERNAL_VALIDATION_FAILED_STATUS
    assert "severity_invalid" in json.dumps(result)


def test_wrong_severity_case_is_rejected_not_corrected(skill_index, settings, monkeypatch):
    bad = {"mode": "single", **os_entry("controller_log", {"size": 5, "query": F({"term": {"Severity.keyword": "ERROR"}})})}
    FakeCluster(handler=generic).install(monkeypatch)
    router = MagicMock(parsed={"gate_stop": None, "panic_mode": False, "unresolved_topics": [],
                               "matched_references": [{"reference_path": CTL, "data_source": "opensearch"}]})
    with patch.object(llm_client, "call_llm_json", side_effect=[router, MagicMock(parsed=bad), MagicMock(parsed=bad)]):
        result = asyncio.run(pipeline.run_pipeline("controller errors", skill_index, settings))
    assert result["status"] == pipeline.INTERNAL_VALIDATION_FAILED_STATUS
    assert "differs only in case" in json.dumps(result)


def test_an_analysis_parameter_over_the_limit_is_rejected_not_clamped(skill_index, settings, monkeypatch):
    q = {"size": 1, "query": F({"match_phrase": {"Body": "LinkStatus Down"}})}
    bad = {"mode": "single", **os_entry("controller_log", q, analysis={"type": "context_window", "before": "9h"})}
    FakeCluster(handler=generic).install(monkeypatch)
    router = MagicMock(parsed={"gate_stop": None, "panic_mode": False, "unresolved_topics": [],
                               "matched_references": [{"reference_path": CTL, "data_source": "opensearch"}]})
    with patch.object(llm_client, "call_llm_json", side_effect=[router, MagicMock(parsed=bad), MagicMock(parsed=bad)]):
        result = asyncio.run(pipeline.run_pipeline("before and after", skill_index, settings))
    assert result["status"] == pipeline.INTERNAL_VALIDATION_FAILED_STATUS and "analysis_invalid" in json.dumps(result)


def test_generator_prompt_lists_the_analyses_of_the_selected_family_only(skill_index, settings, monkeypatch):
    FakeCluster(handler=generic).install(monkeypatch)
    prompts = []

    def fake(prompt, system_instruction, api_key, model):
        prompts.append(prompt)
        if len(prompts) == 1:
            return MagicMock(parsed={"gate_stop": None, "panic_mode": False, "unresolved_topics": [],
                                     "matched_references": [{"reference_path": FAB, "data_source": "opensearch"}]})
        return MagicMock(parsed={"mode": "single", **os_entry("fabric_health", {"size": 5, "query": F()})})
    with patch.object(llm_client, "call_llm_json", side_effect=fake):
        asyncio.run(pipeline.run_pipeline("fabric events", skill_index, settings))
    gen = prompts[1]
    block = gen[gen.index("OpenSearch query context (authoritative"):]      # the system-produced block only
    assert "PATTERN `fabric_health`" in block and "- absence:" in block and "- recurrence:" in block
    assert "fields.Switch.keyword" in block and "UNVERIFIED placeholder" in block
    assert "- ingest_lag:" not in block and "PATTERN `controller_log`" not in block
