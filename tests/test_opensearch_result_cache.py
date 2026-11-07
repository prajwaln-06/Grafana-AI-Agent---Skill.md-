"""The OpenSearch RESULT cache (app/opensearch_catalog/query_cache.py + runtime wiring + agent hook).

What is real: registry, field catalog (SYNTHETIC fixture mappings through the real opensearch_client code),
guard, canonicalisation, executor, analyses, results/redaction, the agent's `_run_async_impl`, the process-wide
cache in runtime.py. What is faked: only the HTTP session (tests/os_fixtures.FakeCluster) and the Router/Generator
(`run_pipeline`), which is replaced by a counting stub so "no LLM work happened" is observable. Time is injected by
replacing the cache's clock. No Gemini call and no OpenSearch cluster is used anywhere.

Scenarios (see the task): A exact hits, B semantic hits, C negative matches, D freshness, E multi-step analysis,
F failure handling, G isolation + concurrency, H guard/fingerprint safety, I contract shapes, J lifecycle/config.
"""
import asyncio
import copy
import dataclasses
import json
import threading
from types import SimpleNamespace

import pytest
import requests

from app import agent as agent_mod, executor
from app.opensearch_catalog import canonical, query_cache, runtime
from app.opensearch_catalog.field_catalog import FieldCatalogCache
from app.opensearch_catalog.registry import IndexRegistry
from tests.os_fixtures import REF, FakeCluster, agg_body, hits_body, os_entry, window

Q_BASE = "Which switches have stopped sending heartbeats?"
SCOPE = "app:u1"


# ---------------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


@pytest.fixture()
def cluster(monkeypatch):
    return FakeCluster().install(monkeypatch)


@pytest.fixture()
def cache_settings(settings):
    """Result cache on, with a generous TTL so only the tests about time expire anything."""
    return settings.model_copy(update={"opensearch_result_cache_enabled": True,
                                       "opensearch_result_cache_ttl_seconds": 600.0})


def ctx_for(settings, *pids):
    return runtime.build_request_context([{"reference_path": REF[p], "data_source": "opensearch"} for p in pids],
                                         settings)


def canon(settings, *entries):
    """What the pipeline hands the executor: canonical (guard-approved) entries."""
    pids = [e["pattern_id"] for e in entries]
    contract = {"mode": "single", **entries[0]} if len(entries) == 1 else {"mode": "multi", "results": list(entries)}
    return canonical.canonicalise_contract(contract, ctx_for(settings, *pids))


def absence_entry(gte="now-24h", **analysis):
    q = {"size": 0, "query": {"bool": {"filter": [window(gte), {"term": {"fields.PhysicalContext.keyword": "runtime.heartbeat"}}]}},
         "aggs": {"sw": {"terms": {"field": "fields.Switch.keyword"}}}}
    return os_entry("fabric_health", q, intent="absence", analysis={"type": "absence", **analysis})


def recurrence_entry():
    q = {"size": 0, "query": {"bool": {"filter": [window("now-24h"), {"term": {"fields.PhysicalSubContext.keyword": "CRITICAL"}}]}},
         "aggs": {"loc": {"terms": {"field": "fields.Location.keyword"},
                          "aggs": {"per_hour": {"date_histogram": {"field": "@timestamp", "fixed_interval": "1h"}}}}}}
    return os_entry("fabric_health", q, intent="recurrence", analysis={"type": "recurrence"})


def rows_entry(pid="controller_log", gte="now-24h"):
    return os_entry(pid, {"size": 5, "query": {"bool": {"filter": [window(gte)]}}})


def absence_handler(baseline, recent, other=0, recent_gte="now-3m"):
    def handler(index, dsl):
        gte = dsl["query"]["bool"]["filter"][0]["range"]["@timestamp"]["gte"]
        names = recent if gte == recent_gte else baseline
        return agg_body({"sw": {"doc_count_error_upper_bound": 0, "sum_other_doc_count": other,
                                "buckets": [{"key": n, "doc_count": 10} for n in names]}}, total=100)
    return handler


SWITCHES = ["x1000c0r1b0", "x1000c1r2b0", "x1000c2r3b0"]
ALIVE = ["x1000c0r1b1", "x1000c1r2b0"]            # x1000c2r3 is the one that went quiet


def execute(settings, *entries):
    return executor.execute_contract(canon(settings, *entries), settings)


def store(settings, question, executed, scope=SCOPE, skill_index=None):
    return runtime.store_executed_result(question, scope, executed, settings, skill_index)


def lookup(settings, question, scope=SCOPE, skill_index=None):
    return runtime.lookup_cached_result(question, scope, settings, skill_index)


def strip_cache_info(result):
    r = copy.deepcopy(result)
    blocks = r["results"] if r.get("mode") == "multi" else [r]
    for b in blocks:
        b["execution"].get("metadata", {}).pop("result_cache", None)
    return r


def cache_info(result):
    blocks = result["results"] if result.get("mode") == "multi" else [result]
    return [b["execution"]["metadata"]["result_cache"] for b in blocks]


class AgentRun:
    """Drives app.agent exactly as the ADK runner does, with a counting Router/Generator stub."""

    def __init__(self, monkeypatch, settings, contract_for):
        self.settings, self.contract_for = settings, contract_for
        self.pipeline_questions: list[str] = []
        self.skill_index = None
        monkeypatch.setattr(agent_mod, "get_settings", lambda: self.settings)
        monkeypatch.setattr(agent_mod.ObservabilityQueryBuilderAgent, "_get_skill_index_or_reload",
                            lambda _self: self.skill_index)

        async def fake_pipeline(question, skill_index, settings, context=None, catalog=None):
            self.pipeline_questions.append(question)
            return copy.deepcopy(self.contract_for(question))

        monkeypatch.setattr(agent_mod, "run_pipeline", fake_pipeline)

    def ask(self, question, user="u1", state=None):
        ctx = SimpleNamespace(user_content=SimpleNamespace(parts=[SimpleNamespace(text=question)]),
                              session=SimpleNamespace(state=state or {}, user_id=user), invocation_id="inv",
                              app_name="app")

        async def collect():
            return [e async for e in agent_mod.root_agent._run_async_impl(ctx)]

        events = asyncio.run(collect())
        assert len(events) == 1
        payload = json.loads(events[0].content.parts[0].text)
        return payload["result"], events[0]

    @property
    def cache(self):
        return runtime.get_result_cache(self.settings)


@pytest.fixture()
def absence_agent(monkeypatch, cache_settings, cluster):
    cluster.handler = absence_handler(SWITCHES, ALIVE)
    return AgentRun(monkeypatch, cache_settings, lambda q: canon(cache_settings, absence_entry())), cluster


# ---------------------------------------------------------------------------------------------------
# A. exact hits -- the whole pipeline and every OpenSearch search are skipped
# ---------------------------------------------------------------------------------------------------

def test_first_question_runs_the_pipeline_and_the_second_identical_one_does_not(absence_agent):
    run, cluster = absence_agent
    first, ev1 = run.ask(Q_BASE)
    assert run.pipeline_questions == [Q_BASE] and len(cluster.searches) == 2          # baseline + recent
    assert first["execution"]["execution_status"] == "success"
    assert [b["key"] for b in first["execution"]["buckets"]] == ["x1000c2r3b0"]
    assert "result_cache" not in first["execution"]["metadata"]                       # a miss is not annotated

    second, ev2 = run.ask(Q_BASE)
    assert run.pipeline_questions == [Q_BASE], "Router/Generator must not run again"
    assert len(cluster.searches) == 2, "no OpenSearch search for a cache hit"
    info = cache_info(second)[0]
    assert info["hit"] is True and info["match"] == "exact" and "similarity" not in info
    assert strip_cache_info(second) == first                                          # the same completed answer
    assert second["execution"]["fetched_at"] == first["execution"]["fetched_at"]      # honest about when it was fetched
    assert ev2.actions.state_delta.get("previous_question", 1) is None                # same state reset as a normal answer


def test_exact_hit_ignores_case_punctuation_articles_and_tense(absence_agent):
    run, cluster = absence_agent
    run.ask(Q_BASE)
    for q in ("which switches have stopped sending heartbeats", "Which switches have stopped sending the heartbeats??",
              "Which switch has stopped sending heartbeats?"):
        res, _ = run.ask(q)
        assert cache_info(res)[0]["hit"] is True
    assert run.pipeline_questions == [Q_BASE] and len(cluster.searches) == 2


def test_quoted_literal_case_is_never_folded():
    a, b = query_cache.build_signature("Show errors containing 'LinkDown'"), \
        query_cache.build_signature("Show errors containing 'linkdown'")
    assert a.canonical_text != b.canonical_text and a.literals != b.literals


# ---------------------------------------------------------------------------------------------------
# B. semantic hits -- different wording, same operation, scope, concepts, window
# ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("question", [
    "Which switches are no longer reporting?",
    "Show me the switches missing heartbeats.",
    "Which switches are not reporting?",
    "Show me the switches that missed their heartbeats.",
    "Which switches have gone quiet?",
    "List the switches that haven't reported",
])
def test_equivalent_wordings_reuse_the_stored_answer(absence_agent, question):
    run, cluster = absence_agent
    first, _ = run.ask(Q_BASE)
    res, _ = run.ask(question)
    assert run.pipeline_questions == [Q_BASE], f"{question!r} should not have reached the Router"
    assert len(cluster.searches) == 2
    info = cache_info(res)[0]
    assert info["match"] == "semantic" and "similarity" not in info
    assert strip_cache_info(res) == first


def test_nodes_are_down_is_a_miss_unless_the_operator_declared_the_equivalence(monkeypatch, cache_settings, cluster):
    cluster.handler = absence_handler(SWITCHES, ALIVE)
    contract = canon(cache_settings, absence_entry())
    run = AgentRun(monkeypatch, cache_settings, lambda q: contract)
    run.ask(Q_BASE)
    for q in ("Which nodes are down?", "Which nodes have stopped sending heartbeat signals?"):
        run.ask(q)
    assert run.pipeline_questions == [Q_BASE, "Which nodes are down?", "Which nodes have stopped sending heartbeat signals?"]


def test_declared_family_aliases_make_nodes_are_down_equivalent(monkeypatch, cache_settings, cluster):
    s = cache_settings.model_copy(update={"opensearch_result_cache_aliases": {
        "fabric_health": {"node": "switch", "down": "stopped sending heartbeats"}}})
    cluster.handler = absence_handler(SWITCHES, ALIVE)
    contract = canon(s, absence_entry())
    run = AgentRun(monkeypatch, s, lambda q: contract)
    first, _ = run.ask(Q_BASE)
    for q in ("Which nodes are down?", "Which nodes have stopped sending heartbeat signals?"):
        res, _ = run.ask(q)
        assert cache_info(res)[0]["match"] == "semantic" and strip_cache_info(res) == first
    assert run.pipeline_questions == [Q_BASE]
    # the alias belongs to fabric_health only: it does nothing for a question about a different noun
    run.ask("Which racks are down?")
    assert run.pipeline_questions[-1] == "Which racks are down?"


def test_semantic_matching_can_be_switched_off(monkeypatch, cache_settings, cluster):
    s = cache_settings.model_copy(update={"opensearch_result_cache_semantic_enabled": False})
    cluster.handler = absence_handler(SWITCHES, ALIVE)
    contract = canon(s, absence_entry())
    run = AgentRun(monkeypatch, s, lambda q: contract)
    run.ask(Q_BASE)
    run.ask("Which switches are not reporting?")
    assert len(run.pipeline_questions) == 2
    res, _ = run.ask(Q_BASE)
    assert cache_info(res)[0]["match"] == "exact" and len(run.pipeline_questions) == 2


# ---------------------------------------------------------------------------------------------------
# C. negative matches -- similar wording must never share an answer when the query would differ
# ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("question,why", [
    ("Which switches stopped sending heartbeats in the last hour?", "different time window"),
    ("Which switches stopped sending heartbeats in the last 2 days?", "different time window"),
    ("Which switches are down?", "different operation concept (state, not absent heartbeats)"),
    ("Why are the switches down?", "different operation (explain)"),
    ("How many switches stopped sending heartbeats?", "different operation (count)"),
    ("Which devices have stopped sending heartbeats?", "different entity scope"),
    ("Which switches in rack 5 have stopped sending heartbeats?", "extra filter"),
    ("Which switches have stopped sending logs?", "different signal"),
    ("Which switches have not stopped sending heartbeats?", "negation"),
    ("Which switches have recurring heartbeat problems?", "different analysis type"),
    ("Which switches stopped sending heartbeats and which racks have errors?", "compound question"),
    ("Which switches stopped sending heartbeats today?", "calendar-relative time"),
    ("Which switches have stopped sending heartbeats between 10:00 and 11:00?", "absolute time"),
    ("Which switches have stopped sending heartbeats most often?", "different output requirement"),
])
def test_materially_different_questions_are_misses(absence_agent, question, why):
    run, cluster = absence_agent
    run.ask(Q_BASE)
    run.ask(question)
    assert run.pipeline_questions == [Q_BASE, question], why


def test_a_stored_window_never_serves_another_window(monkeypatch, cache_settings, cluster):
    """A 1-hour answer must not answer a 24-hour question (and vice versa), even though both are 'absence'."""
    cluster.handler = absence_handler(SWITCHES, ALIVE, recent_gte="now-3m")

    def contract_for(q):
        return canon(cache_settings, absence_entry("now-1h" if "last hour" in q else "now-24h"))

    run = AgentRun(monkeypatch, cache_settings, contract_for)
    one_hour = "Which switches stopped sending heartbeats in the last hour?"
    day = "Which switches stopped sending heartbeats in the last 24 hours?"
    run.ask(one_hour)
    run.ask(day)
    assert run.pipeline_questions == [one_hour, day]
    # same window, different words: shared
    res, _ = run.ask("Which switches are no longer reporting in the last 24 hours?")
    assert cache_info(res)[0]["match"] == "semantic" and len(run.pipeline_questions) == 2
    res, _ = run.ask("Which switches have gone quiet in the past 1 day?")
    assert cache_info(res)[0]["match"] == "semantic" and len(run.pipeline_questions) == 2


def test_an_entry_whose_question_disagrees_with_its_query_answers_only_its_own_wording(monkeypatch, cache_settings, cluster):
    """The Generator answered 'in the last hour' with a 24 h query. The cache cannot vouch for that wording, so
    the entry is limited to Level 1 (exact wording)."""
    cluster.handler = absence_handler(SWITCHES, ALIVE)
    contract = canon(cache_settings, absence_entry("now-24h"))
    run = AgentRun(monkeypatch, cache_settings, lambda q: contract)
    q = "Which switches stopped sending heartbeats in the last hour?"
    run.ask(q)
    (entry,) = run.cache._entries.values()
    assert entry.semantic_ok is False
    run.ask("Which switches are not reporting in the last hour?")
    assert len(run.pipeline_questions) == 2
    res, _ = run.ask(q)
    assert cache_info(res)[0]["match"] == "exact" and len(run.pipeline_questions) == 2


def test_an_unfamiliar_noun_is_not_grounded_so_only_exact_wording_matches(monkeypatch, cache_settings, cluster):
    cluster.handler = absence_handler(SWITCHES, ALIVE)
    contract = canon(cache_settings, absence_entry())
    run = AgentRun(monkeypatch, cache_settings, lambda q: contract)
    run.ask("Which nodes have stopped sending heartbeats?")          # 'node' is not a fabric_health role
    (entry,) = run.cache._entries.values()
    assert entry.semantic_ok is False and "not grounded" in run.cache.snapshot().get("x", "not grounded")


# ---------------------------------------------------------------------------------------------------
# D. freshness
# ---------------------------------------------------------------------------------------------------

@pytest.fixture()
def clocked(cache_settings, cluster):
    cluster.handler = absence_handler(SWITCHES, ALIVE)
    clock = Clock()
    s = cache_settings.model_copy(update={"opensearch_result_cache_ttl_seconds": 60.0})
    runtime.get_result_cache(s)._clock = clock
    return s, clock


def test_unexpired_hit_expired_miss_and_a_read_never_extends_the_expiry(clocked):
    s, clock = clocked
    executed = execute(s, absence_entry())                  # recent window 3m -> effective ttl 18 s (10%)
    assert store(s, Q_BASE, executed) == ""
    (entry,) = runtime.get_result_cache(s)._entries.values()
    assert entry.ttl_seconds == pytest.approx(18.0)
    clock.advance(10)
    assert lookup(s, Q_BASE) is not None                      # unexpired: hit
    clock.advance(7)                                          # t = 17 s
    assert lookup(s, Q_BASE) is not None
    clock.advance(2)                                          # t = 19 s: the read at t=10 did NOT push expiry to 28
    assert lookup(s, Q_BASE) is None
    assert len(runtime.get_result_cache(s)) == 0              # expired entries are removed, not kept around


def test_ttl_is_never_longer_than_the_configured_value(clocked):
    s, clock = clocked
    s2 = s.model_copy(update={"opensearch_result_cache_ttl_seconds": 7.0})
    runtime.get_result_cache(s2)._clock = clock
    store(s2, "Show controller messages", execute(s2, rows_entry()))
    (entry,) = runtime.get_result_cache(s2)._entries.values()
    assert entry.ttl_seconds == 7.0                           # 10% of 24h would be 8640 s, so the setting wins


def test_a_narrow_relative_window_shortens_the_lifetime(clocked):
    s, clock = clocked
    store(s, "Show controller messages", execute(s, rows_entry(gte="now-30s")))
    (entry,) = runtime.get_result_cache(s)._entries.values()
    assert entry.ttl_seconds == query_cache.MIN_EFFECTIVE_TTL_SECONDS     # 10% of 30 s = 3 s, floored at 5 s
    clock.advance(4.9)
    assert lookup(s, "Show controller messages") is not None
    clock.advance(0.2)
    assert lookup(s, "Show controller messages") is None


def test_an_absolute_window_is_governed_by_the_configured_ttl_only(clocked):
    s, clock = clocked
    e = absence_entry("2026-10-28T00:00:00Z")
    e["query"]["query"]["bool"]["filter"][0] = window("2026-10-28T00:00:00Z", "2026-10-28T12:00:00Z")
    store(s, Q_BASE, execute(s, e))
    (entry,) = runtime.get_result_cache(s)._entries.values()
    assert entry.ttl_seconds == 60.0 and entry.parts[0].window_relative is False


def test_the_cached_answer_is_exactly_what_was_computed_for_that_window(clocked):
    """A hit reports the ORIGINAL resolved window and fetch time, so a reader can see how old it is."""
    s, clock = clocked
    executed = execute(s, absence_entry())
    store(s, Q_BASE, executed)
    clock.advance(5)
    hit = lookup(s, Q_BASE)
    assert hit["execution"]["resolved_time_range"] == executed["execution"]["resolved_time_range"]
    assert cache_info(hit)[0]["age_seconds"] == pytest.approx(5.0)
    assert cache_info(hit)[0]["expires_in_seconds"] == pytest.approx(13.0)


# ---------------------------------------------------------------------------------------------------
# E. multi-step analysis -- the complete final result, no internal search repeated
# ---------------------------------------------------------------------------------------------------

def test_a_cached_absence_analysis_skips_both_internal_queries_and_keeps_summary_and_warnings(monkeypatch, cache_settings, cluster):
    cluster.handler = absence_handler(["a0", "b0"], [])        # nothing reported recently -> a warning
    contract = canon(cache_settings, absence_entry())
    run = AgentRun(monkeypatch, cache_settings, lambda q: contract)
    first, _ = run.ask(Q_BASE)
    assert len(cluster.searches) == 2
    a1 = first["execution"]["analysis"]
    assert a1["queries_executed"] == 2 and any("no_recent_reports" in w for w in a1["warnings"])

    res, _ = run.ask("Show me the switches missing heartbeats.")          # semantic hit
    assert len(cluster.searches) == 2, "neither the baseline nor the recent-window query may run again"
    assert run.pipeline_questions == [Q_BASE]
    a2 = res["execution"]["analysis"]
    assert a2 == a1 and a2["completed"] is True and a2["missing_count"] == 2
    assert [b["key"] for b in res["execution"]["buckets"]] == [b["key"] for b in first["execution"]["buckets"]]
    assert {b["key"] for b in res["execution"]["buckets"]} == {"a0", "b0"}
    assert res["execution"]["metadata"]["query_window"] == first["execution"]["metadata"]["query_window"]
    assert strip_cache_info(res) == first


def test_recurrence_is_cached_under_its_own_concepts(monkeypatch, cache_settings, cluster):
    cluster.handler = lambda i, d: agg_body({"loc": {"doc_count_error_upper_bound": 0, "sum_other_doc_count": 0, "buckets": [
        {"key": "x1c0:p1", "doc_count": 5, "per_hour": {"buckets": [{"key_as_string": "t1", "doc_count": 1},
                                                                    {"key_as_string": "t2", "doc_count": 4}]}}]}}, total=5)
    contract = canon(cache_settings, recurrence_entry())
    run = AgentRun(monkeypatch, cache_settings, lambda q: contract)
    q = "Which locations have recurring faults?"
    first, _ = run.ask(q)
    assert first["execution"]["analysis"]["type"] == "recurrence" and first["execution"]["execution_status"] == "success"
    n = len(cluster.searches)
    res, _ = run.ask("Which locations have recurring faults")
    assert cache_info(res)[0]["hit"] and len(cluster.searches) == n
    # an absence question does not reuse a recurrence answer, nor the other way round
    run.ask("Which locations have stopped sending heartbeats?")
    assert run.pipeline_questions[-1] == "Which locations have stopped sending heartbeats?"


# ---------------------------------------------------------------------------------------------------
# F. failures are never cached
# ---------------------------------------------------------------------------------------------------

def _boom(exc):
    def handler(index, dsl):
        raise exc
    return handler


def _partial(shards_failed=1, timed_out=False):
    def handler(index, dsl):
        body = hits_body([{"@timestamp": "2026-10-28T10:00:00Z", "Severity": "error", "Body": "b"}])
        body["timed_out"] = timed_out
        body["_shards"] = {"total": 3, "successful": 3 - shards_failed, "skipped": 0, "failed": shards_failed,
                           "failures": [{"index": "i", "shard": 0, "reason": {"type": "t", "reason": "r"}}]}
        return body
    return handler


@pytest.mark.parametrize("handler,what", [
    (_boom(requests.exceptions.Timeout()), "timeout"),
    (_boom(requests.exceptions.ConnectionError("refused")), "connection failure"),
    (_partial(shards_failed=1), "failed shard (partial)"),
    (_partial(shards_failed=0, timed_out=True), "server-side timeout (partial)"),
    (_partial(shards_failed=3), "every shard failed"),
])
def test_failed_or_partial_executions_are_not_stored_and_never_replayed(monkeypatch, cache_settings, cluster, handler, what):
    cluster.handler = handler
    contract = canon(cache_settings, rows_entry())
    run = AgentRun(monkeypatch, cache_settings, lambda q: contract)
    q = "Show controller messages"
    first, _ = run.ask(q)
    assert first["execution"]["execution_status"] != "success" or first["execution"]["metadata"]["partial"], what
    assert len(run.cache) == 0, what
    run.ask(q)
    assert run.pipeline_questions == [q, q], f"{what}: the failure must not have been served from the cache"


def test_a_partial_analysis_failure_is_not_stored(monkeypatch, cache_settings, cluster):
    calls = []

    def handler(index, dsl):
        calls.append(1)
        if len(calls) == 2:                                    # the 'recent' query fails
            raise requests.exceptions.Timeout()
        return absence_handler(SWITCHES, ALIVE)(index, dsl)

    cluster.handler = handler
    contract = canon(cache_settings, absence_entry())
    run = AgentRun(monkeypatch, cache_settings, lambda q: contract)
    res, _ = run.ask(Q_BASE)
    assert res["execution"]["execution_status"] == "timeout" and res["execution"]["analysis"]["completed"] is False
    assert len(run.cache) == 0
    run.ask(Q_BASE)
    assert len(run.pipeline_questions) == 2


def test_a_truncated_analysis_is_not_stored(monkeypatch, cache_settings, cluster):
    cluster.handler = absence_handler(["a"], ["a"], other=40)       # more entities exist than the limit
    contract = canon(cache_settings, absence_entry())
    run = AgentRun(monkeypatch, cache_settings, lambda q: contract)
    res, _ = run.ask(Q_BASE)
    assert res["execution"]["analysis"]["complete"] is False and len(run.cache) == 0


def test_rejected_and_unexecuted_results_are_not_stored(cache_settings, cluster):
    cluster.handler = absence_handler(SWITCHES, ALIVE)
    bad = canon(cache_settings, rows_entry())
    bad["query"] = {"size": 5, "query": {"bool": {"filter": [window()]}}, "script_fields": {"x": {}}}
    executed = executor.execute_contract(bad, cache_settings)
    assert executed["execution"]["execution_status"] == "not_executed"
    assert store(cache_settings, "Show controller messages", executed)
    assert len(runtime.get_result_cache(cache_settings)) == 0


def test_prometheus_alert_and_error_shaped_results_are_never_stored(cache_settings, os_registry):
    prom = {"mode": "single", "status": "ok", "data_source": "prometheus", "query": "up", "execution": {
        "execution_status": "success", "metadata": {"partial": False}, "resolved_time_range": {}}}
    alert = {"mode": "single", "status": "alert_rule_proposed", "data_source": "prometheus"}
    unmapped = {"mode": "single", "status": "unmapped", "explanation": "no match"}
    for executed in (prom, alert, unmapped, {}, None, "text", []):
        a = query_cache.assess_executed("Which switches are down?", executed, registry=os_registry)
        assert a.cacheable is False


def test_one_unusable_entry_keeps_the_whole_multi_result_out_of_the_cache(monkeypatch, cache_settings, cluster):
    cluster.handler = lambda i, d: hits_body([{"@timestamp": "2026-10-28T10:00:00Z", "Severity": "error", "Body": "b"}])
    executed = execute(cache_settings, rows_entry(), rows_entry("fabric_health"))
    assert executed["mode"] == "multi" and all(r["execution"]["execution_status"] == "success" for r in executed["results"])
    broken = copy.deepcopy(executed)
    broken["results"][1]["execution"] = {"execution_status": "endpoint_error", "error": "x"}
    assert store(cache_settings, "Show controller and fabric messages", broken) != ""
    assert len(runtime.get_result_cache(cache_settings)) == 0
    partial = copy.deepcopy(executed)
    partial["results"][0]["execution"]["metadata"]["partial"] = True
    assert store(cache_settings, "Show controller and fabric messages", partial) != ""
    assert len(runtime.get_result_cache(cache_settings)) == 0
    assert store(cache_settings, "Show controller and fabric messages", executed) == ""     # all good: stored whole


def test_a_failure_in_the_question_matching_is_a_miss_not_an_error_and_exact_hits_still_work(absence_agent, monkeypatch):
    run, cluster = absence_agent
    run.ask(Q_BASE)

    def boom(a, b):
        raise RuntimeError("matching broke")

    monkeypatch.setattr(query_cache, "equivalent", boom)
    res, _ = run.ask("Which switches are not reporting?")             # would be a semantic hit
    assert res["execution"]["execution_status"] == "success" and len(run.pipeline_questions) == 2
    assert run.cache.snapshot()["match_errors"] == 1
    res, _ = run.ask(Q_BASE)                                          # an exact hit needs no matching
    assert cache_info(res)[0]["match"] == "exact" and len(run.pipeline_questions) == 2


def test_no_similarity_score_or_threshold_exists_anywhere(absence_agent, cache_settings):
    """Level 2 is rule-based facet equality only; nothing may suggest embedding similarity is active."""
    from app.config import Settings
    run, _ = absence_agent
    run.ask(Q_BASE)
    assert not any("similarity" in f or "threshold" in f for f in Settings.model_fields if "result_cache" in f)
    assert not hasattr(query_cache, "ConceptVectorMatcher") and not hasattr(query_cache, "SemanticMatcher")
    assert "similarity_threshold" not in run.cache.snapshot() and not hasattr(run.cache, "matcher")
    res, _ = run.ask("Which switches are not reporting?")
    assert cache_info(res)[0]["match"] == "semantic" and "similarity" not in cache_info(res)[0]


def test_capacity_is_bounded_oldest_first_and_an_evicted_entry_is_a_miss(cache_settings, cluster):
    cluster.handler = absence_handler(SWITCHES, ALIVE)
    s = cache_settings.model_copy(update={"opensearch_result_cache_max_entries": 2})
    clock = Clock()
    cache = runtime.get_result_cache(s)
    cache._clock = clock
    executed = execute(s, absence_entry())
    for i, q in enumerate(("Which switches are missing?", "Which switches are down?", "Which switches are silent?")):
        clock.advance(1)
        assert store(s, q, executed) == ""
        assert len(cache) <= 2
    assert lookup(s, "Which switches are missing?") is None            # oldest was evicted
    assert lookup(s, "Which switches are down?") is not None
    assert lookup(s, "Which switches are silent?") is not None
    assert cache.snapshot()["evicted_capacity"] == 1


def test_an_oversized_result_is_not_stored(cache_settings, cluster, monkeypatch):
    cluster.handler = absence_handler(SWITCHES, ALIVE)
    monkeypatch.setattr(query_cache, "MAX_ENTRY_BYTES", 100)
    assert store(cache_settings, Q_BASE, execute(cache_settings, absence_entry())) == "result too large to cache"


# ---------------------------------------------------------------------------------------------------
# G. isolation and concurrency
# ---------------------------------------------------------------------------------------------------

def test_scopes_never_share_entries(monkeypatch, cache_settings, cluster):
    cluster.handler = absence_handler(SWITCHES, ALIVE)
    contract = canon(cache_settings, absence_entry())
    run = AgentRun(monkeypatch, cache_settings, lambda q: contract)
    run.ask(Q_BASE, user="alice")
    run.ask(Q_BASE, user="bob")
    assert len(run.pipeline_questions) == 2, "bob must not be answered from alice's entry"
    run.ask(Q_BASE, user="alice")
    run.ask("Which switches are not reporting?", user="bob")           # semantic hit, but inside bob's own scope
    assert len(run.pipeline_questions) == 2 and len(run.cache) == 2


def test_mutating_a_returned_result_cannot_change_what_is_stored(cache_settings, cluster):
    cluster.handler = absence_handler(SWITCHES, ALIVE)
    executed = execute(cache_settings, absence_entry())
    pristine = copy.deepcopy(executed)
    assert store(cache_settings, Q_BASE, executed) == ""
    executed["execution"]["buckets"].clear()                           # caller keeps mutating its own object
    executed["execution"]["analysis"]["warnings"].append("tampered")
    hit1 = lookup(cache_settings, Q_BASE)
    assert strip_cache_info(hit1) == pristine
    hit1["execution"]["buckets"].append({"key": "evil"})
    hit1["execution"]["analysis"]["missing_count"] = 99
    hit1["execution"]["metadata"]["count"]["value"] = -1
    hit2 = lookup(cache_settings, Q_BASE)
    assert strip_cache_info(hit2) == pristine and hit2 is not hit1


def test_identical_concurrent_misses_store_exactly_one_entry(cache_settings, cluster):
    cluster.handler = absence_handler(SWITCHES, ALIVE)
    executed = execute(cache_settings, absence_entry())
    results, barrier = [], threading.Barrier(16)

    def worker():
        barrier.wait()
        results.append(store(cache_settings, Q_BASE, copy.deepcopy(executed)))

    threads = [threading.Thread(target=worker) for _ in range(16)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    cache = runtime.get_result_cache(cache_settings)
    assert len(cache) == 1 and results.count("") == 1 and len(results) == 16
    assert cache.snapshot()["stored"] == 1 and cache.snapshot()["duplicate_not_stored"] == 15


def test_different_questions_never_overwrite_each_other_under_concurrency(cache_settings, cluster):
    cluster.handler = lambda i, d: hits_body([{"@timestamp": "2026-10-28T10:00:00Z", "Severity": "error", "Body": "b"}])
    ctl = execute(cache_settings, rows_entry("controller_log"))
    fab = execute(cache_settings, rows_entry("fabric_health"))
    q_ctl, q_fab = "Show controller messages", "Show fabric messages"
    barrier, wrong = threading.Barrier(8), []

    def writer(q, ex):
        barrier.wait()
        store(cache_settings, q, copy.deepcopy(ex))

    def reader():
        barrier.wait()
        for _ in range(100):
            for q, pid in ((q_ctl, "controller_log"), (q_fab, "fabric_health")):
                hit = lookup(cache_settings, q)
                if hit is not None and hit["pattern_id"] != pid:
                    wrong.append((q, hit["pattern_id"]))

    threads = [threading.Thread(target=writer, args=(q_ctl, ctl)), threading.Thread(target=writer, args=(q_fab, fab))]
    threads += [threading.Thread(target=reader) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not wrong
    assert lookup(cache_settings, q_ctl)["pattern_id"] == "controller_log"
    assert lookup(cache_settings, q_fab)["pattern_id"] == "fabric_health"


def test_a_stored_answer_keeps_the_existing_redaction(cache_settings, cluster):
    body = "Failed login for user admin from 10.1.2.3"
    cluster.handler = lambda i, d: hits_body([{"@timestamp": "2026-10-28T10:00:00Z", "Severity": "error", "Body": body}])
    executed = execute(cache_settings, rows_entry())
    assert "10.1.2.3" not in json.dumps(executed), "precondition: the executor already masked the address"
    assert store(cache_settings, "Show controller messages", executed) == ""
    cache = runtime.get_result_cache(cache_settings)
    # "_source" legitimately appears once per entry as the registry-projected field list in the stored DSL;
    # what must never be stored is a raw document body or the unmasked address.
    for e in cache._entries.values():
        stored = json.loads(e.result_json)
        assert "10.1.2.3" not in e.result_json
        assert "_source" not in json.dumps(stored["execution"]), "no raw _source document in the execution block"
    assert strip_cache_info(lookup(cache_settings, "Show controller messages")) == executed
    snap = json.dumps(cache.snapshot())
    assert "controller" not in snap and "10.1.2.3" not in snap, "the status snapshot must not expose questions/results"


# ---------------------------------------------------------------------------------------------------
# H. a hit can never bypass the guard or the current configuration
# ---------------------------------------------------------------------------------------------------

def _stored(cache_settings, cluster, skill_index=None):
    cluster.handler = absence_handler(SWITCHES, ALIVE)
    assert store(cache_settings, Q_BASE, execute(cache_settings, absence_entry()), skill_index=skill_index) == ""
    return runtime.get_result_cache(cache_settings)


def test_a_registry_change_invalidates_the_stored_answer(monkeypatch, cache_settings, cluster):
    cache = _stored(cache_settings, cluster)
    assert lookup(cache_settings, Q_BASE) is not None
    reg = runtime.get_registry(cache_settings)
    changed = IndexRegistry(patterns=tuple(dataclasses.replace(p, max_window="3d") if p.pattern_id == "fabric_health" else p
                                           for p in reg.patterns), registry_version=reg.registry_version)
    monkeypatch.setattr(runtime, "get_registry", lambda s: changed)
    assert lookup(cache_settings, Q_BASE) is None
    assert len(cache) == 0 and cache.snapshot()["revalidation_failed"] == 1


def test_a_pattern_that_is_disabled_or_gone_is_never_replayed(monkeypatch, cache_settings, cluster):
    _stored(cache_settings, cluster)
    reg = runtime.get_registry(cache_settings)
    off = IndexRegistry(patterns=tuple(dataclasses.replace(p, enabled=False) if p.pattern_id == "fabric_health" else p
                                       for p in reg.patterns), registry_version=reg.registry_version)
    monkeypatch.setattr(runtime, "get_registry", lambda s: off)
    assert lookup(cache_settings, Q_BASE) is None


def test_unavailable_field_discovery_blocks_a_hit(monkeypatch, cache_settings, cluster):
    _stored(cache_settings, cluster)
    dead = FieldCatalogCache(lambda ip: None, ttl_seconds=60, stale_ttl_seconds=60, max_indices=7,
                             failure_backoff_seconds=1)
    monkeypatch.setattr(runtime, "get_field_cache", lambda s: dead)
    assert lookup(cache_settings, Q_BASE) is None


def test_a_changed_live_mapping_invalidates_the_stored_answer(monkeypatch, cache_settings, cluster):
    _stored(cache_settings, cluster)
    from tests.os_fixtures import FIX, INDEX
    mapping = json.loads((FIX / "mapping_fabric_health.json").read_text())
    for idx in mapping.values():                                       # the cluster stops indexing a permitted field
        idx["mappings"]["properties"]["fields"]["properties"].pop("Location", None)
    other = FieldCatalogCache(lambda ip: mapping, ttl_seconds=60, stale_ttl_seconds=60, max_indices=7,
                              failure_backoff_seconds=1)
    monkeypatch.setattr(runtime, "get_field_cache", lambda s: other)
    assert lookup(cache_settings, Q_BASE) is None


def test_changed_security_or_output_configuration_invalidates_the_stored_answer(cache_settings, cluster):
    _stored(cache_settings, cluster)
    for change in ({"opensearch_redact_ip_mode": "off"}, {"opensearch_redact_usernames": False},
                   {"max_hits_per_result": 7}, {"opensearch_analysis_max_terms": 100}, {"alert_rule_creation_enabled": True}):
        s = cache_settings.model_copy(update=change)
        assert lookup(s, Q_BASE) is None, change
        cluster.handler = absence_handler(SWITCHES, ALIVE)
        assert store(cache_settings, Q_BASE, execute(cache_settings, absence_entry())) == ""    # re-prime for the next change


def test_changed_skill_text_invalidates_the_stored_answer(cache_settings, cluster):
    v1, v2 = SimpleNamespace(raw_text="skill v1"), SimpleNamespace(raw_text="skill v2")
    _stored(cache_settings, cluster, skill_index=v1)
    assert lookup(cache_settings, Q_BASE, skill_index=v1) is not None
    assert lookup(cache_settings, Q_BASE, skill_index=v2) is None


def test_reuse_reruns_the_real_guard_on_the_stored_query(cache_settings, cluster):
    """Even with every fingerprint matching, a stored query the guard would now refuse is not replayed."""
    cache = _stored(cache_settings, cluster)
    (entry,) = cache._entries.values()
    kwargs = dict(settings=cache_settings, registry=runtime.get_registry(cache_settings),
                  field_cache=runtime.get_field_cache(cache_settings), skill_index=None)
    assert query_cache.revalidate_entry(entry, **kwargs) is None
    q = json.loads(entry.parts[0].query_json)
    q["script_fields"] = {"x": {"script": "1"}}
    evil = dataclasses.replace(entry, parts=(dataclasses.replace(entry.parts[0], query_json=json.dumps(q)),))
    assert "guard now rejects" in query_cache.revalidate_entry(evil, **kwargs)
    q = json.loads(entry.parts[0].query_json)
    q.pop("timeout")                                                    # the guard re-adds it: not the canonical form
    odd = dataclasses.replace(entry, parts=(dataclasses.replace(entry.parts[0], query_json=json.dumps(q)),))
    assert "canonical" in query_cache.revalidate_entry(odd, **kwargs)


def test_a_hit_never_touches_opensearch_or_names_an_index(absence_agent, monkeypatch):
    run, cluster = absence_agent
    run.ask(Q_BASE)
    calls = []
    monkeypatch.setattr("app.opensearch_client.search", lambda *a, **k: calls.append((a, k)))
    run.ask(Q_BASE)
    run.ask("Which switches are not reporting?")
    assert calls == []


# ---------------------------------------------------------------------------------------------------
# I. contract shapes
# ---------------------------------------------------------------------------------------------------

def test_multi_mode_results_are_cached_whole_and_keep_their_shape(monkeypatch, cache_settings, cluster):
    cluster.handler = lambda i, d: hits_body([{"@timestamp": "2026-10-28T10:00:00Z", "Severity": "error", "Body": "b"}])
    contract = canon(cache_settings, rows_entry("controller_log"), rows_entry("fabric_health"))
    run = AgentRun(monkeypatch, cache_settings, lambda q: contract)
    q = "Show controller and fabric messages"
    first, _ = run.ask(q)
    n = len(cluster.searches)
    assert first["mode"] == "multi" and len(first["results"]) == 2 and n == 2
    res, _ = run.ask(q)
    assert res["mode"] == "multi" and len(cluster.searches) == n and run.pipeline_questions == [q]
    assert [i["hit"] for i in cache_info(res)] == [True, True]
    assert strip_cache_info(res) == first
    # a multi-entry answer is matched by its exact wording only
    run.ask("Show the fabric and controller messages")
    assert len(run.pipeline_questions) == 2


def test_an_empty_answer_is_a_complete_answer_and_is_cached(monkeypatch, cache_settings, cluster):
    cluster.handler = lambda i, d: hits_body([])
    contract = canon(cache_settings, rows_entry())
    run = AgentRun(monkeypatch, cache_settings, lambda q: contract)
    first, _ = run.ask("Show controller messages")
    assert first["execution"]["execution_status"] == "empty_result"
    res, _ = run.ask("Show controller messages")
    assert res["execution"]["execution_status"] == "empty_result" and len(run.pipeline_questions) == 1


# ---------------------------------------------------------------------------------------------------
# J. lifecycle, configuration, other flows
# ---------------------------------------------------------------------------------------------------

def test_one_cache_instance_per_configuration_and_reset_clears_it(cache_settings, cluster):
    a, b = runtime.get_result_cache(cache_settings), runtime.get_result_cache(cache_settings)
    assert a is b
    _stored(cache_settings, cluster)
    assert len(a) == 1
    runtime.reset()
    assert runtime.get_result_cache(cache_settings) is not a and len(runtime.get_result_cache(cache_settings)) == 0


def test_admin_reload_empties_the_result_cache(cache_settings, cluster):
    cache = _stored(cache_settings, cluster)
    assert len(cache) == 1
    assert runtime.reload_registry_status(cache_settings)["loaded"] is True
    assert len(cache) == 0 and lookup(cache_settings, Q_BASE) is None


def test_a_disabled_cache_changes_nothing(monkeypatch, cache_settings, cluster):
    s = cache_settings.model_copy(update={"opensearch_result_cache_enabled": False})
    cluster.handler = absence_handler(SWITCHES, ALIVE)
    contract = canon(s, absence_entry())
    run = AgentRun(monkeypatch, s, lambda q: contract)
    run.ask(Q_BASE)
    res, _ = run.ask(Q_BASE)
    assert len(run.pipeline_questions) == 2 and "result_cache" not in res["execution"]["metadata"]
    assert runtime.get_result_cache(s) is None and runtime.result_cache_snapshot(s) == {"enabled": False}
    assert store(s, Q_BASE, res) == "result cache disabled" and lookup(s, Q_BASE) is None


def test_an_answer_to_a_clarification_is_neither_served_from_nor_stored_in_the_cache(absence_agent):
    run, cluster = absence_agent
    run.ask(Q_BASE)
    state = {"previous_question": "Which metric?", "previous_result": {"status": "ambiguous_metric"}}
    run.ask(Q_BASE, state=state)                                       # a clarification turn: the pipeline must run
    assert run.pipeline_questions == [Q_BASE, Q_BASE]
    assert len(run.cache) == 1


def test_a_cache_error_never_fails_the_request(monkeypatch, absence_agent):
    run, _ = absence_agent
    run.ask(Q_BASE)
    monkeypatch.setattr(query_cache, "revalidate_entry", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    res, _ = run.ask(Q_BASE)                                           # revalidation blows up -> miss, pipeline answers
    assert res["execution"]["execution_status"] == "success" and len(run.pipeline_questions) == 2
    monkeypatch.setattr(runtime, "get_registry", lambda s: (_ for _ in ()).throw(RuntimeError("no registry")))
    res, _ = run.ask(Q_BASE)
    assert len(run.pipeline_questions) == 3                            # lookup miss, and the store failure is swallowed


def test_settings_validation(cache_settings):
    from pydantic import ValidationError
    from app.config import Settings
    base = cache_settings.model_dump()
    for bad in ({"opensearch_result_cache_ttl_seconds": 0}, {"opensearch_result_cache_ttl_seconds": 99999},
                {"opensearch_result_cache_max_entries": 0},
                {"opensearch_result_cache_aliases": {"fabric_health": {"two words": "x"}}},
                {"opensearch_result_cache_aliases": {"fabric_health": {"node": " "}}}):
        with pytest.raises(ValidationError):
            Settings(**{**base, **bad})
    s = Settings(**base)
    assert s.opensearch_result_cache_enabled and s.opensearch_result_cache_ttl_seconds == 600.0
    assert Settings.model_fields["opensearch_result_cache_ttl_seconds"].default == 60.0
    assert Settings.model_fields["opensearch_result_cache_aliases"].default_factory() == {}


# ---------------------------------------------------------------------------------------------------
# signature unit tests: the equivalence contract itself
# ---------------------------------------------------------------------------------------------------

SIG = query_cache.build_signature


def test_equivalent_phrasings_share_every_facet():
    base = SIG(Q_BASE)
    assert base.understood and base.operation == "list" and base.entities == {"switch"}
    assert base.concepts == {"ABSENT", "SIGNAL"} and base.residual == frozenset() and base.window_seconds is None
    for q in ("Which switches are no longer reporting?", "Show me the switches missing heartbeats.",
              "Which switches have gone quiet?", "Which switches haven't reported?"):
        assert query_cache.equivalent(base, SIG(q)), q


@pytest.mark.parametrize("a,b", [
    ("Which switches went down in the last hour?", "Which switches went down in the last 24 hours?"),
    ("Which switches are down?", "Why are the switches down?"),
    ("Which switches are down?", "Which devices are down?"),
    ("Which switches have errors?", "Which switches have warnings?"),
    ("Which switches reported errors in rack 5?", "Which switches reported errors in rack 7?"),
    ("Show errors containing 'LinkDown'", "Show errors containing 'linkdown'"),
    ("Which switches are down?", "Which switches are not down?"),
])
def test_material_differences_break_equivalence(a, b):
    assert not query_cache.equivalent(SIG(a), SIG(b))


def test_window_expressions_compare_by_seconds_not_wording():
    assert SIG("switches down in the last 24 hours").window_seconds == 86400 == SIG("switches down in the past 1 day").window_seconds
    assert SIG("switches down in the last hour").window_seconds == 3600
    assert SIG("switches down in the last 30 minutes").window_seconds == 1800
    assert not SIG("switches down in the last 3 and 4 hours").understood or SIG("x in the last 3 hours and the last 4 hours").window_seconds is None
    assert not SIG("switches down today").understood and not SIG("switches down since 10:15").understood


def test_signatures_are_deterministic_and_never_raise():
    for q in ("", "   ", "???", "x" * 5000, "Which é ​ switches?", None, 42):
        assert SIG(q) == SIG(q)
    assert SIG(Q_BASE) == SIG(Q_BASE.title())
    # SHOUTING words are treated as case-sensitive literals (they might be an identifier): never equal -> a safe miss
    assert SIG(Q_BASE) != SIG(Q_BASE.upper())


def test_equivalence_is_reflexive_symmetric_and_broken_by_any_single_facet():
    a = SIG(Q_BASE)
    assert query_cache.equivalent(a, a)
    b = SIG("Which switches are no longer reporting?")
    assert query_cache.equivalent(a, b) and query_cache.equivalent(b, a)
    for other in ("Which devices have stopped sending heartbeats?",                  # entity
                  "Why have the switches stopped sending heartbeats?",               # operation
                  "How many switches have stopped sending heartbeats?",              # operation
                  "Which switches have stopped sending heartbeats in the last hour?",  # window
                  "Which switches have stopped sending heartbeats on x1000c0r1b0?",  # literal
                  "Which switches have stopped sending heartbeats repeatedly?",      # concept
                  "Which switches have stopped sending heartbeats critically?"):     # unexplained word
        o = SIG(other)
        assert not query_cache.equivalent(a, o) and not query_cache.equivalent(o, a), other
    assert not query_cache.equivalent(SIG(""), SIG(""))                              # nothing understood => never


# ---------------------------------------------------------------------------------------------------
# I. an incomplete analysis is never cached; informational caveats do not block caching
#
# analysis.py signals a cut-short answer in three different ways: a summary flag (absence:
# `complete`, text_ratio_shortfall: `scan_complete`), or ONLY a warning (context_window:
# `anchor_<i>_truncated`, recurrence: `ranking_limited`). The cache classifies warnings by code.
# ---------------------------------------------------------------------------------------------------

ANCHOR = {"@timestamp": "2026-10-28T10:00:00Z", "Body": "LinkStatus Down on j104p1", "Severity": "error",
          "Resource": {"host": {"name": "x1000c0r1b0"}, "service": {"name": "hmscd"}}}
CTX_Q = "Show the log lines around the LinkStatus Down events"


def context_entry(**analysis):
    q = {"query": {"bool": {"filter": [window("now-24h"), {"match_phrase": {"Body": "LinkStatus Down"}}]}}}
    return os_entry("controller_log", q, intent="context_window", analysis={"type": "context_window", **analysis})


def context_handler(anchor=ANCHOR, follow_total=None, follow_relation="eq", no_anchor=False):
    def handler(index, dsl):
        if dsl.get("sort") == [{"@timestamp": {"order": "desc"}}]:                    # the anchor query
            return hits_body([] if no_anchor else [anchor], ids=["A"])
        if dsl.get("sort") == [{"@timestamp": {"order": "asc"}}]:                     # a per-event follow-up
            docs = [{"@timestamp": "2026-10-28T09:58:00Z", "Body": "before it"}, {**anchor},
                    {"@timestamp": "2026-10-28T10:02:00Z", "Body": "after it"}]
            return hits_body(docs, ids=["b", "A", "c"], total=follow_total, relation=follow_relation)
        return hits_body([{"@timestamp": "2026-10-28T10:00:00Z", "Severity": "error", "Body": "row"}])
    return handler


def recurrence_handler(other=0):
    return lambda i, d: agg_body({"loc": {"doc_count_error_upper_bound": 0, "sum_other_doc_count": other, "buckets": [
        {"key": "x1c0:p1", "doc_count": 5, "per_hour": {"buckets": [{"key_as_string": "t1", "doc_count": 1},
                                                                    {"key_as_string": "t2", "doc_count": 4}]}}]}}, total=5 + other)


def shortfall_entry(size=50):
    q = {"size": size, "query": {"bool": {"filter": [window(), {"match_phrase": {"Body": "switches available"}}]}}}
    return os_entry("controller_log", q, intent="text_ratio_shortfall", analysis={"type": "text_ratio_shortfall"})


def lag_entry():
    return os_entry("ilo_events", {"size": 20, "query": {"bool": {"filter": [window()]}}}, analysis={"type": "ingest_lag"})


def _cache_len(settings):
    return len(runtime.get_result_cache(settings))


def _warning_codes(executed):
    return [w.split(":")[0] for w in executed["execution"]["analysis"]["warnings"]]


# ---- context_window ----

def test_a_truncated_context_window_is_not_cached(cache_settings, cluster):
    cluster.handler = context_handler(follow_total=500)               # 500 rows exist around the event, 3 returned
    executed = execute(cache_settings, context_entry())
    a = executed["execution"]["analysis"]
    assert a["completed"] is True and executed["execution"]["execution_status"] == "success"
    assert _warning_codes(executed) == ["anchor_0_truncated"]
    # the reason the old checks missed it: this analysis sets neither flag the cache used to look at
    assert "complete" not in a and "scan_complete" not in a
    reason = store(cache_settings, CTX_Q, executed)
    assert "anchor_0_truncated" in reason and _cache_len(cache_settings) == 0
    assert lookup(cache_settings, CTX_Q) is None


def test_a_context_window_whose_row_count_is_only_a_lower_bound_is_not_cached(cache_settings, cluster):
    cluster.handler = context_handler(follow_total=3, follow_relation="gte")      # relation gte => more may exist
    executed = execute(cache_settings, context_entry())
    assert _warning_codes(executed) == ["anchor_0_truncated"]
    assert store(cache_settings, CTX_Q, executed) != "" and _cache_len(cache_settings) == 0


def test_a_context_window_with_a_skipped_event_is_not_cached(cache_settings, cluster):
    no_time = {k: v for k, v in ANCHOR.items() if k != "@timestamp"}                # event found, cannot be expanded
    cluster.handler = context_handler(anchor=no_time)
    executed = execute(cache_settings, context_entry())
    assert _warning_codes(executed) == ["anchor_0_skipped"]
    a = executed["execution"]["analysis"]
    assert a["anchors_found"] == 1 and a["anchors_expanded"] == 0
    reason = store(cache_settings, CTX_Q, executed)
    assert "anchor_0_skipped" in reason and _cache_len(cache_settings) == 0


def test_a_complete_context_window_is_cached_and_replayed_without_any_search(cache_settings, cluster):
    cluster.handler = context_handler()
    executed = execute(cache_settings, context_entry())
    a = executed["execution"]["analysis"]
    assert _warning_codes(executed) == [] and a["anchors"][0]["truncated"] is False
    assert store(cache_settings, CTX_Q, executed) == "" and _cache_len(cache_settings) == 1
    n = len(cluster.searches)
    hit = lookup(cache_settings, CTX_Q)
    assert hit is not None and len(cluster.searches) == n
    assert strip_cache_info(hit) == executed


def test_a_context_window_that_found_no_event_is_a_complete_answer_and_is_cached(cache_settings, cluster):
    cluster.handler = context_handler(no_anchor=True)
    executed = execute(cache_settings, context_entry())
    assert _warning_codes(executed) == ["no_anchor_event"]                         # informational: nothing was cut short
    assert executed["execution"]["execution_status"] == "empty_result"
    assert store(cache_settings, CTX_Q, executed) == "" and _cache_len(cache_settings) == 1


def test_a_truncated_context_window_runs_the_pipeline_again_through_the_agent(monkeypatch, cache_settings, cluster):
    cluster.handler = context_handler(follow_total=500)
    contract = canon(cache_settings, context_entry())
    run = AgentRun(monkeypatch, cache_settings, lambda q: contract)
    run.ask(CTX_Q)
    n = len(cluster.searches)
    run.ask(CTX_Q)
    assert run.pipeline_questions == [CTX_Q, CTX_Q] and len(cluster.searches) > n
    assert _cache_len(cache_settings) == 0


# ---- recurrence ----

def test_a_ranking_limited_recurrence_is_not_cached(cache_settings, cluster):
    cluster.handler = recurrence_handler(other=40)                       # 40 documents belong to unranked entities
    executed = execute(cache_settings, recurrence_entry())
    a = executed["execution"]["analysis"]
    assert a["completed"] is True and executed["execution"]["execution_status"] == "success"
    assert _warning_codes(executed) == ["ranking_limited"]
    assert "complete" not in a and "scan_complete" not in a                  # again: only a warning says so
    reason = store(cache_settings, "Which locations have recurring faults?", executed)
    assert "ranking_limited" in reason and _cache_len(cache_settings) == 0


def test_a_ranking_limited_recurrence_runs_the_pipeline_again_through_the_agent(monkeypatch, cache_settings, cluster):
    cluster.handler = recurrence_handler(other=40)
    contract = canon(cache_settings, recurrence_entry())
    run = AgentRun(monkeypatch, cache_settings, lambda q: contract)
    q = "Which locations have recurring faults?"
    run.ask(q)
    run.ask(q)
    assert run.pipeline_questions == [q, q] and _cache_len(cache_settings) == 0


def test_a_complete_recurrence_is_cached_and_replayed_without_any_search(cache_settings, cluster):
    cluster.handler = recurrence_handler(other=0)
    executed = execute(cache_settings, recurrence_entry())
    assert _warning_codes(executed) == []
    q = "Which locations have recurring faults?"
    assert store(cache_settings, q, executed) == "" and _cache_len(cache_settings) == 1
    n = len(cluster.searches)
    hit = lookup(cache_settings, q)
    assert hit is not None and len(cluster.searches) == n and strip_cache_info(hit) == executed


# ---- the other analysis types ----

def test_an_absence_with_more_entities_than_the_limit_is_not_cached(cache_settings, cluster):
    cluster.handler = absence_handler(SWITCHES, ALIVE, other=7)
    executed = execute(cache_settings, absence_entry())
    assert "incomplete_entity_list" in _warning_codes(executed) and executed["execution"]["analysis"]["complete"] is False
    assert store(cache_settings, Q_BASE, executed) != "" and _cache_len(cache_settings) == 0


def test_an_absence_whose_only_warning_is_informational_is_still_cached(cache_settings, cluster):
    cluster.handler = absence_handler(SWITCHES, [])                       # nothing reported recently: a reading aid only
    executed = execute(cache_settings, absence_entry())
    assert _warning_codes(executed) == ["no_recent_reports"]
    assert store(cache_settings, Q_BASE, executed) == "" and _cache_len(cache_settings) == 1


def test_a_scan_limited_shortfall_is_not_cached_but_a_complete_one_is(cache_settings, cluster):
    cluster.handler = lambda i, d: hits_body([{"@timestamp": "2026-10-28T10:00:00Z", "Body": "1/2 switches available"}],
                                             total=10000, relation="gte")
    limited = execute(cache_settings, shortfall_entry(size=1))
    assert _warning_codes(limited) == ["scan_limited"]
    assert store(cache_settings, "Which fabric messages report fewer switches available?", limited) != ""
    assert _cache_len(cache_settings) == 0
    cluster.handler = lambda i, d: hits_body([{"@timestamp": "2026-10-28T10:00:00Z", "Body": "1/2 switches available"}])
    complete = execute(cache_settings, shortfall_entry())
    assert _warning_codes(complete) == []
    assert store(cache_settings, "Which fabric messages report fewer switches available?", complete) == ""


def test_ingest_lag_informational_warnings_do_not_block_caching(cache_settings, cluster):
    rows = [{"@timestamp": "2026-10-28T10:00:00Z", "Created": "2026-10-28T10:05:00Z", "host": "bmc1", "Message": "a"}]
    cluster.handler = lambda i, d: hits_body(rows, index="log_iml-2026.10.26")
    negative = execute(cache_settings, lag_entry())
    assert _warning_codes(negative) == ["negative_lag"]                      # clock skew: data complete, caveat only
    assert store(cache_settings, "What is the ingest lag of the iLO events?", negative) == ""
    runtime.reset_result_cache()
    cluster.handler = lambda i, d: hits_body([{"@timestamp": "2026-10-28T10:05:00Z", "host": "h"}], index="log_iml-x")
    unmeasurable = execute(cache_settings, lag_entry())
    assert _warning_codes(unmeasurable) == ["no_lag_measurable"]
    assert store(cache_settings, "What is the ingest lag of the iLO events?", unmeasurable) == ""


# ---- all-or-nothing, fail-closed and independent checks ----

def test_one_truncated_analysis_keeps_a_whole_multi_result_out_of_the_cache(cache_settings, cluster):
    cluster.handler = context_handler(follow_total=500)
    executed = execute(cache_settings, rows_entry("fabric_health"), context_entry())
    assert executed["mode"] == "multi" and len(executed["results"]) == 2
    reason = store(cache_settings, "Show fabric messages and the LinkStatus Down context", executed)
    assert "entry 1" in reason and "anchor_0_truncated" in reason and _cache_len(cache_settings) == 0


@pytest.fixture()
def complete_absence(cache_settings, cluster):
    cluster.handler = absence_handler(SWITCHES, ALIVE)
    executed = execute(cache_settings, absence_entry())
    assert store(cache_settings, Q_BASE, copy.deepcopy(executed)) == ""
    runtime.reset_result_cache()
    return executed


@pytest.mark.parametrize("mutate, expect", [
    (lambda a: a["warnings"].append("brand_new_warning: some future caveat"), "unclassified warning"),
    (lambda a: a["warnings"].append("incomplete_entity_list: x"), "incomplete_entity_list"),
    (lambda a: a["warnings"].append("no_recent_reports: ok"), None),                           # informational only
    (lambda a: a["warnings"].append(""), "unclassified warning"),
    (lambda a: a["warnings"].append(None), "unclassified warning"),
    (lambda a: a["warnings"].append("anchor_x_truncated: not a real index"), "unclassified warning"),
    (lambda a: a.pop("warnings"), "warnings are missing"),
    (lambda a: a.update(warnings="none"), "warnings are missing"),
    (lambda a: a.update(complete=False), "truncated"),
    (lambda a: a.update(scan_complete=False), "truncated"),
    (lambda a: a.update(completed=False), "did not complete"),
])
def test_warning_classification_is_fail_closed(cache_settings, complete_absence, mutate, expect):
    executed = copy.deepcopy(complete_absence)
    mutate(executed["execution"]["analysis"])
    reason = store(cache_settings, Q_BASE, executed)
    if expect is None:
        assert reason == "" and _cache_len(cache_settings) == 1
    else:
        assert expect in reason and _cache_len(cache_settings) == 0


def test_each_truncation_signal_blocks_caching_on_its_own(cache_settings, cluster):
    """Even with the warnings list emptied, the structured flags still refuse the entry."""
    cluster.handler = context_handler()
    base = execute(cache_settings, context_entry())
    assert _warning_codes(base) == []

    def flag_truncated(ex):
        ex["analysis"]["anchors"][0]["truncated"] = True

    def flag_unexpanded(ex):
        ex["analysis"]["anchors_found"] = ex["analysis"]["anchors_expanded"] + 1

    def flag_result_truncated(ex):
        ex["truncated"] = True

    for mutate, expect in ((flag_truncated, "surrounding rows were truncated"), (flag_unexpanded, "was not expanded"),
                           (flag_result_truncated, "truncated by the result limits")):
        executed = copy.deepcopy(base)
        mutate(executed["execution"])
        reason = store(cache_settings, CTX_Q, executed)
        assert expect in reason and _cache_len(cache_settings) == 0, expect
    assert store(cache_settings, CTX_Q, copy.deepcopy(base)) == ""                       # untouched: stored


def test_classify_warning_codes():
    c = query_cache.classify_warning
    for w in ("incomplete_entity_list: more", "ranking_limited: x", "scan_limited: x", "anchor_0_truncated: x",
              "anchor_12_truncated: x", "anchor_2_skipped: x"):
        assert c(w) == "incomplete", w
    for w in ("no_recent_reports: x", "no_anchor_event: x", "negative_lag: x", "no_lag_measurable: x"):
        assert c(w) == "informational", w
    for w in ("something_new: x", "", None, 5, "anchor_x_truncated: x", "anchor_1_truncated_again: x"):
        assert c(w) == "unknown", w


def test_every_warning_code_in_analysis_py_is_classified():
    """If analysis.py gains a warning, this fails until it is classified as incomplete or informational."""
    import ast
    from pathlib import Path
    from app.opensearch_catalog import analysis as analysis_mod

    tree = ast.parse(Path(analysis_mod.__file__).read_text(encoding="utf-8"))

    def text_of(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):                                   # f"anchor_{idx}_truncated: ..."
            return "".join(v.value if isinstance(v, ast.Constant) else "0" for v in node.values)
        return None

    found: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "append"
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "warnings" and node.args):
            t = text_of(node.args[0])
            if t:
                found.add(t.split(":", 1)[0])
        if (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "warnings" for t in node.targets)
                and isinstance(node.value, ast.IfExp) and isinstance(node.value.orelse, ast.List)):
            found.update(t.split(":", 1)[0] for t in map(text_of, node.value.orelse.elts) if t)
    assert {"incomplete_entity_list", "no_recent_reports", "anchor_0_truncated", "anchor_0_skipped", "no_anchor_event",
            "negative_lag", "no_lag_measurable", "scan_limited", "ranking_limited"} <= found, found    # extractor works
    unclassified = {c for c in found if query_cache.classify_warning(c + ": x") == "unknown"}
    assert not unclassified, f"classify these analysis warnings in query_cache.py: {sorted(unclassified)}"


# ---------------------------------------------------------------------------------------------------
# J. context_window: the anchor query's own total must prove that EVERY matching event was expanded
#
# The anchor query returns at most `size` (<= the anchor limit) events, but more may match. analysis.py
# now keeps the anchor query's total in the summary as `anchors_matched: {value, relation}` (relation
# "eq" exact | "gte" lower bound | "unknown" = OpenSearch sent no total). Only an exact total equal to
# the events returned and expanded is proof of completeness. These tests drive the REAL executor, with
# a fake cluster that reports totals the way OpenSearch does (see anchor_corpus_handler).
# ---------------------------------------------------------------------------------------------------

DESC = [{"@timestamp": {"order": "desc"}}]
ASC = [{"@timestamp": {"order": "asc"}}]


def anchor_corpus_handler(corpus_total):
    """`corpus_total` events match the anchor query. The response holds at most `size` of them and
    reports hits.total the way OpenSearch does: exact up to the tracking bound (default 10 000), a
    lower bound (relation gte) beyond it, nothing at all when track_total_hits is false."""
    def handler(index, dsl):
        if "controllerlog" not in index:
            return hits_body([{"@timestamp": "2026-10-28T10:00:00Z", "Severity": "error", "Body": "row"}])
        if dsl.get("sort") == ASC:                                                    # a per-event expansion
            docs = [{"@timestamp": "2026-10-28T09:58:00Z", "Body": "before it"},
                    {"@timestamp": "2026-10-28T10:02:00Z", "Body": "after it"}]
            return hits_body(docs, ids=["b", "c"])
        n = min(corpus_total, dsl["size"])
        docs = [{**ANCHOR, "@timestamp": f"2026-10-28T10:0{i}:00Z"} for i in range(n)]
        body = hits_body(docs, ids=[f"A{i}" for i in range(n)])
        track = dsl.get("track_total_hits", 10000)
        if track is False:
            body["hits"].pop("total")                                                  # no total at all
        elif track is True or corpus_total <= track:
            body["hits"]["total"] = {"value": corpus_total, "relation": "eq"}
        else:
            body["hits"]["total"] = {"value": track, "relation": "gte"}
        return body
    return handler


def ctx_entry(size=None, track=None, **analysis):
    q = {"query": {"bool": {"filter": [window("now-24h"), {"match_phrase": {"Body": "LinkStatus Down"}}]}}}
    if size is not None:
        q["size"] = size
    if track is not None:
        q["track_total_hits"] = track
    return os_entry("controller_log", q, intent="context_window", analysis={"type": "context_window", **analysis})


def _matched(executed):
    return executed["execution"]["analysis"]["anchors_matched"]


@pytest.mark.parametrize("corpus, track, size, relation, value, cacheable, why", [
    (1, None, None, "eq", 1, True, ""),                                       # one event, exact: complete
    (3, None, None, "eq", 3, True, ""),                                       # exactly the anchor limit, exact: complete
    (3, True, None, "eq", 3, True, ""),
    (2, 2, None, "eq", 2, True, ""),                                          # integer bound not exceeded: still exact
    (1, None, 1, "eq", 1, True, ""),
    (5, None, None, "eq", 5, False, "5 events matched but only 3 were expanded"),   # exact total above the limit
    (2, None, 1, "eq", 2, False, "2 events matched but only 1 were expanded"),      # exact total above the query size
    (25000, None, None, "gte", 10000, False, "lower bound"),                  # beyond the default tracking bound
    (5, 3, None, "gte", 3, False, "lower bound"),                             # lower bound whose number equals the count returned
    (3, False, None, "unknown", None, False, "did not report how many"),      # track_total_hits: false
])
def test_the_anchor_total_decides_whether_a_context_window_is_cached(
        cache_settings, cluster, corpus, track, size, relation, value, cacheable, why):
    cluster.handler = anchor_corpus_handler(corpus)
    executed = execute(cache_settings, ctx_entry(size=size, track=track))
    a = executed["execution"]["analysis"]
    assert a["completed"] is True and _warning_codes(executed) == []                 # nothing else flags these
    assert a["anchors_matched"] == {"value": value, "relation": relation}            # propagated, never faked
    assert a["anchors_found"] == a["anchors_expanded"] == min(corpus, size or 3)     # every RETURNED event was expanded
    reason = store(cache_settings, CTX_Q, executed)
    if cacheable:
        assert reason == "" and _cache_len(cache_settings) == 1
        assert lookup(cache_settings, CTX_Q) is not None
    else:
        assert why in reason and _cache_len(cache_settings) == 0
        assert lookup(cache_settings, CTX_Q) is None


def test_an_exact_total_above_a_configured_anchor_limit_is_never_cached(cache_settings, cluster):
    """Every returned event is expanded, yet more events matched than the limit lets the analysis expand."""
    s = cache_settings.model_copy(update={"opensearch_analysis_anchor_limit": 2})
    cluster.handler = anchor_corpus_handler(3)
    executed = execute(s, ctx_entry())
    a = executed["execution"]["analysis"]
    assert (a["anchors_found"], a["anchors_expanded"]) == (2, 2) and a["anchors_matched"] == {"value": 3, "relation": "eq"}
    assert all(not x["truncated"] for x in a["anchors"]) and _warning_codes(executed) == []
    assert "3 events matched but only 2 were expanded" in store(s, CTX_Q, executed) and _cache_len(s) == 0
    cluster.handler = anchor_corpus_handler(2)                                       # exactly the limit: complete
    assert store(s, CTX_Q, execute(s, ctx_entry())) == "" and _cache_len(s) == 1


def test_no_matching_event_is_cacheable_only_with_an_exact_zero_total(cache_settings, cluster):
    cluster.handler = anchor_corpus_handler(0)
    executed = execute(cache_settings, ctx_entry())
    assert executed["execution"]["execution_status"] == "empty_result" and _warning_codes(executed) == ["no_anchor_event"]
    assert _matched(executed) == {"value": 0, "relation": "eq"}
    assert store(cache_settings, CTX_Q, executed) == "" and _cache_len(cache_settings) == 1
    runtime.reset_result_cache()
    cluster.handler = anchor_corpus_handler(0)
    unknown = execute(cache_settings, ctx_entry(track=False))                         # no events returned, no total either
    assert _matched(unknown) == {"value": None, "relation": "unknown"}
    assert "did not report" in store(cache_settings, CTX_Q, unknown) and _cache_len(cache_settings) == 0
    lower = copy.deepcopy(executed)                                                   # an exact zero is required
    lower["execution"]["analysis"]["anchors_matched"] = {"value": 0, "relation": "gte"}
    assert "lower bound" in store(cache_settings, CTX_Q, lower) and _cache_len(cache_settings) == 0


@pytest.mark.parametrize("total, expect", [
    ({"value": "3", "relation": "eq"}, "did not report how many"),            # value of the wrong type -> unknown
    ({"value": 3, "relation": "weird"}, "malformed"),                         # a relation OpenSearch never sends
    ({"value": -1, "relation": "eq"}, "malformed"),
    ({"value": 1, "relation": "eq"}, ""),                                     # control: a proper total
    (None, "did not report how many"),                                        # hits.total missing entirely
])
def test_a_malformed_total_from_the_cluster_fails_closed(cache_settings, cluster, total, expect):
    base = anchor_corpus_handler(1)

    def handler(index, dsl):
        body = base(index, dsl)
        if "controllerlog" in index and dsl.get("sort") == DESC:
            if total is None:
                body["hits"].pop("total")
            else:
                body["hits"]["total"] = total
        return body

    cluster.handler = handler
    executed = execute(cache_settings, ctx_entry())
    reason = store(cache_settings, CTX_Q, executed)
    if expect:
        assert expect in reason and _cache_len(cache_settings) == 0
    else:
        assert reason == "" and _cache_len(cache_settings) == 1


def test_a_total_that_contradicts_the_returned_events_is_not_cached(cache_settings, cluster):
    base = anchor_corpus_handler(3)

    def handler(index, dsl):
        body = base(index, dsl)
        if "controllerlog" in index and dsl.get("sort") == DESC:
            body["hits"]["total"] = {"value": 1, "relation": "eq"}                    # 1 matched, yet 3 were returned
        return body

    cluster.handler = handler
    executed = execute(cache_settings, ctx_entry())
    assert executed["execution"]["analysis"]["anchors_found"] == 3
    assert "contradicts the events returned" in store(cache_settings, CTX_Q, executed) and _cache_len(cache_settings) == 0


@pytest.fixture()
def complete_context(cache_settings, cluster):
    cluster.handler = anchor_corpus_handler(1)
    executed = execute(cache_settings, ctx_entry())
    assert store(cache_settings, CTX_Q, copy.deepcopy(executed)) == ""
    runtime.reset_result_cache()
    return executed


@pytest.mark.parametrize("mutate, expect", [
    (lambda a: a.pop("anchors_matched"), "not reported"),
    (lambda a: a.update(anchors_matched="1"), "not reported"),
    (lambda a: a.update(anchors_matched=[1]), "not reported"),
    (lambda a: a.update(anchors_matched=None), "not reported"),
    (lambda a: a.update(anchors_matched={}), "did not report"),
    (lambda a: a.update(anchors_matched={"value": None, "relation": "unknown"}), "did not report"),
    (lambda a: a.update(anchors_matched={"value": 1}), "malformed"),
    (lambda a: a.update(anchors_matched={"value": 1, "relation": "lt"}), "malformed"),
    (lambda a: a.update(anchors_matched={"value": True, "relation": "eq"}), "malformed"),
    (lambda a: a.update(anchors_matched={"value": "1", "relation": "eq"}), "malformed"),
    (lambda a: a.update(anchors_matched={"value": 1.0, "relation": "eq"}), "malformed"),
    (lambda a: a.update(anchors_matched={"value": -1, "relation": "eq"}), "malformed"),
    (lambda a: a.update(anchors_matched={"value": 1, "relation": "gte"}), "lower bound"),
    (lambda a: a.update(anchors_matched={"value": 2, "relation": "eq"}), "2 events matched but only 1"),
    (lambda a: a.update(anchors_matched={"value": 0, "relation": "eq"}), "contradicts the events returned"),
    (lambda a: a.update(anchors_found=2), "was not expanded"),
    (lambda a: a.update(anchors_expanded=2), "contradict each other"),
    (lambda a: a["anchors"].pop(), "contradict each other"),
    (lambda a: a.pop("anchors"), "missing or malformed"),
    (lambda a: a.pop("anchors_found"), "missing or malformed"),
    (lambda a: a.update(anchors_found="1"), "missing or malformed"),
    (lambda a: a["anchors"][0].update(truncated=True), "truncated"),                  # existing per-anchor check
    (lambda a: a["warnings"].append("anchor_0_skipped: x"), "anchor_0_skipped"),       # existing warning classification
    (lambda a: a.update(anchors_matched={"value": 1, "relation": "eq"}), None),       # control: proper total is accepted
])
def test_the_context_window_completeness_contract_fails_closed(cache_settings, complete_context, mutate, expect):
    executed = copy.deepcopy(complete_context)
    mutate(executed["execution"]["analysis"])
    reason = store(cache_settings, CTX_Q, executed)
    if expect is None:
        assert reason == "" and _cache_len(cache_settings) == 1
    else:
        assert expect in reason and _cache_len(cache_settings) == 0


def test_a_complete_context_window_is_replayed_with_no_router_generator_or_opensearch_call(
        monkeypatch, cache_settings, cluster):
    cluster.handler = anchor_corpus_handler(3)                                         # 3 events, exact, all expanded
    contract = canon(cache_settings, ctx_entry())
    run = AgentRun(monkeypatch, cache_settings, lambda q: contract)
    first, _ = run.ask(CTX_Q)
    assert len(cluster.searches) == 4 and run.pipeline_questions == [CTX_Q]            # 1 anchor query + 3 expansions
    assert first["execution"]["analysis"]["anchors_matched"] == {"value": 3, "relation": "eq"}
    second, _ = run.ask(CTX_Q)
    assert run.pipeline_questions == [CTX_Q], "Router/Generator must not run again"
    assert len(cluster.searches) == 4, "no OpenSearch search at all for the hit"
    assert cache_info(second)[0]["hit"] is True and strip_cache_info(second) == first


def test_an_incomplete_anchor_query_is_not_stored_and_the_next_identical_question_runs_the_pipeline_again(
        monkeypatch, cache_settings, cluster):
    cluster.handler = anchor_corpus_handler(5)                                         # 5 match, 3 can be expanded
    contract = canon(cache_settings, ctx_entry())
    run = AgentRun(monkeypatch, cache_settings, lambda q: contract)
    first, _ = run.ask(CTX_Q)
    assert first["execution"]["analysis"]["anchors_matched"] == {"value": 5, "relation": "eq"}
    assert first["execution"]["analysis"]["anchors_found"] == 3 and len(cluster.searches) == 4
    assert _cache_len(cache_settings) == 0
    second, _ = run.ask(CTX_Q)
    assert run.pipeline_questions == [CTX_Q, CTX_Q] and len(cluster.searches) == 8
    assert "result_cache" not in second["execution"]["metadata"] and _cache_len(cache_settings) == 0


def test_all_or_nothing_for_a_multi_result_with_an_incomplete_anchor_query(cache_settings, cluster):
    cluster.handler = anchor_corpus_handler(5)
    executed = execute(cache_settings, rows_entry("fabric_health"), ctx_entry())
    assert executed["mode"] == "multi"
    reason = store(cache_settings, "Show fabric messages and the LinkStatus Down context", executed)
    assert "entry 1" in reason and "5 events matched but only 3" in reason and _cache_len(cache_settings) == 0
    cluster.handler = anchor_corpus_handler(2)                                          # complete: stored whole
    executed = execute(cache_settings, rows_entry("fabric_health"), ctx_entry())
    assert store(cache_settings, "Show fabric messages and the LinkStatus Down context", executed) == ""


def test_other_analyses_do_not_carry_or_need_the_anchor_total(cache_settings, cluster):
    cluster.handler = absence_handler(SWITCHES, ALIVE)
    absence = execute(cache_settings, absence_entry())
    cluster.handler = recurrence_handler(other=0)
    recurrence = execute(cache_settings, recurrence_entry())
    cluster.handler = lambda i, d: hits_body([{"@timestamp": "2026-10-28T10:00:00Z", "Body": "1/2 switches available"}])
    shortfall = execute(cache_settings, shortfall_entry())
    cluster.handler = lambda i, d: hits_body([{"@timestamp": "2026-10-28T10:00:00Z", "Created": "2026-10-28T09:55:00Z",
                                               "host": "bmc1", "Message": "a"}], index="log_iml-2026.10.26")
    lag = execute(cache_settings, lag_entry())
    for n, (q, ex) in enumerate((("Which switches have stopped sending heartbeats?", absence),
                                 ("Which locations have recurring faults?", recurrence),
                                 ("Which fabric messages report fewer switches available?", shortfall),
                                 ("What is the ingest lag of the iLO events?", lag)), start=1):
        assert "anchors_matched" not in ex["execution"]["analysis"], q
        assert store(cache_settings, q, ex) == "" and _cache_len(cache_settings) == n, q
