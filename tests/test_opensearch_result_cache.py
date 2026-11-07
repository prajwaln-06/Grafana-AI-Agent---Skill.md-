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
    assert info["hit"] is True and info["match"] == "exact" and info["similarity"] == 1.0
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
    assert info["match"] == "semantic" and info["similarity"] >= 0.85
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


def test_a_semantic_matcher_that_fails_is_a_miss_not_an_error_and_exact_hits_still_work(absence_agent):
    run, cluster = absence_agent
    run.ask(Q_BASE)

    class Boom:
        def similarity(self, a, b):
            raise RuntimeError("embedding service down")

    run.cache.matcher = Boom()
    res, _ = run.ask("Which switches are not reporting?")             # would be a semantic hit
    assert res["execution"]["execution_status"] == "success" and len(run.pipeline_questions) == 2
    assert run.cache.snapshot()["matcher_errors"] == 1
    res, _ = run.ask(Q_BASE)                                          # exact hit needs no matcher
    assert cache_info(res)[0]["match"] == "exact" and len(run.pipeline_questions) == 2


def test_a_matcher_below_the_threshold_is_a_miss(absence_agent):
    run, _ = absence_agent
    run.ask(Q_BASE)

    class Low:
        def similarity(self, a, b):
            return 0.5

    run.cache.matcher = Low()
    run.ask("Which switches are not reporting?")
    assert len(run.pipeline_questions) == 2


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
                {"opensearch_result_cache_max_entries": 0}, {"opensearch_result_cache_similarity_threshold": 0.2},
                {"opensearch_result_cache_similarity_threshold": 1.5},
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


def test_concept_vector_similarity_is_bounded_and_symmetric():
    m = query_cache.ConceptVectorMatcher()
    a, b = SIG(Q_BASE), SIG("Which devices have stopped sending logs?")
    assert m.similarity(a, a) == pytest.approx(1.0)
    assert 0.0 <= m.similarity(a, b) < 1.0 and m.similarity(a, b) == pytest.approx(m.similarity(b, a))
    assert m.similarity(SIG(""), a) == 0.0
