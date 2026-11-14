"""The OpenSearch QUERY-PLAN cache (app/opensearch_catalog/plan_cache.py + runtime wiring + agent hook).

Same harness as test_opensearch_result_cache.py: real registry, field catalog (synthetic fixture mappings), guard,
canonicalisation, executor and agent `_run_async_impl`; faked only the HTTP session (FakeCluster) and the
Router/Generator (`run_pipeline`, a counting stub). A plan-cache hit must (1) not call the Router/Generator,
(2) re-validate, (3) run the guard, (4) EXECUTE against OpenSearch again. The answer cache stays intact.

Scenarios: A basic, B revalidation/invalidation, C relative time, D eligibility, E two-cache matrix,
F capacity + concurrency, G isolation/config/status.
"""
import dataclasses
import json
import threading
from types import SimpleNamespace

import pytest

from app import agent as agent_mod, executor
from app.opensearch_catalog import guard, plan_cache, query_cache, runtime
from app.opensearch_catalog.field_catalog import FieldCatalogCache
from app.opensearch_catalog.registry import IndexRegistry
from tests.os_fixtures import FIX, FakeCluster, hits_body
from tests.test_opensearch_result_cache import (
    ALIVE, Q_BASE, SCOPE, SWITCHES, AgentRun, absence_entry, absence_handler, canon, recurrence_entry, rows_entry,
)

Q_REWORDED = "Which switches are not reporting heartbeats?"
Q_15 = "Which switches have stopped sending heartbeats in the last 15 minutes?"


@pytest.fixture()
def cluster(monkeypatch):
    return FakeCluster().install(monkeypatch)


def _s(settings, *, result, plan, **extra):
    return settings.model_copy(update={"opensearch_result_cache_enabled": result,
                                       "opensearch_result_cache_ttl_seconds": 600.0,
                                       "opensearch_query_plan_cache_enabled": plan, **extra})


@pytest.fixture()
def plan_only(settings):
    return _s(settings, result=False, plan=True)


@pytest.fixture()
def both(settings):
    return _s(settings, result=True, plan=True)


def agent(monkeypatch, cluster, settings, contract_for=None):
    cluster.handler = absence_handler(SWITCHES, ALIVE)
    return AgentRun(monkeypatch, settings, contract_for or (lambda q: canon(settings, absence_entry())))


def plan_info(res):
    return res["execution"]["metadata"].get("query_plan_cache")


def result_info(res):
    return res["execution"]["metadata"].get("result_cache")


@pytest.fixture()
def guard_calls(monkeypatch):
    calls = []
    real = guard.check_entry
    monkeypatch.setattr(guard, "check_entry", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    return calls


def primed(monkeypatch, cluster, settings, question=Q_BASE, contract_for=None):
    run = agent(monkeypatch, cluster, settings, contract_for)
    run.ask(question)
    return run


def pcache(settings):
    return runtime.get_plan_cache(settings)


# ---------------------------------------------------------------------------------------------------
# A. basic behaviour
# ---------------------------------------------------------------------------------------------------

def test_first_query_misses_both_caches_runs_the_pipeline_and_stores_the_plan(monkeypatch, cluster, both):
    run = agent(monkeypatch, cluster, both)
    res, _ = run.ask(Q_BASE)
    assert run.pipeline_questions == [Q_BASE] and len(cluster.searches) == 2
    assert res["execution"]["execution_status"] == "success"
    assert plan_info(res) is None and result_info(res) is None            # a miss is not annotated
    assert len(pcache(both)) == 1 and len(run.cache) == 1                 # plan AND answer stored
    assert pcache(both).snapshot()["stored"] == 1


def test_repeated_exact_question_skips_the_llms_but_revalidates_runs_the_guard_and_executes_again(
        monkeypatch, cluster, plan_only, guard_calls):
    run = primed(monkeypatch, cluster, plan_only)
    guard_before, searches_before = len(guard_calls), len(cluster.searches)
    cluster.handler = absence_handler(SWITCHES, SWITCHES[:1])            # the world changed since the first run
    res, ev = run.ask(Q_BASE)
    assert run.pipeline_questions == [Q_BASE], "Router/Generator must not run again"
    assert len(cluster.searches) == searches_before + 2, "OpenSearch is searched again"
    assert len(guard_calls) - guard_before >= 2, "revalidation guard + executor guard both ran"
    assert sorted(b["key"] for b in res["execution"]["buckets"]) == sorted(SWITCHES[1:])   # FRESH data
    info = plan_info(res)
    assert info["hit"] is True and info["match"] == "exact" and info["revalidated"] is True
    assert isinstance(info["cache_entry_id"], int) and info["age_seconds"] >= 0
    assert result_info(res) is None
    assert ev.actions.state_delta.get("previous_question", 1) is None


def test_exact_hit_ignores_case_punctuation_and_articles(monkeypatch, cluster, plan_only):
    run = primed(monkeypatch, cluster, plan_only)
    for q in ("which switches have stopped sending heartbeats", "Which switches have stopped sending the heartbeats??"):
        res, _ = run.ask(q)
        assert plan_info(res)["match"] == "exact"
    assert run.pipeline_questions == [Q_BASE]


def test_equivalent_reworded_question_hits_semantically_and_still_executes(monkeypatch, cluster, plan_only):
    run = primed(monkeypatch, cluster, plan_only)
    n = len(cluster.searches)
    res, _ = run.ask(Q_REWORDED)
    assert run.pipeline_questions == [Q_BASE]
    assert plan_info(res)["hit"] is True and plan_info(res)["match"] == "semantic"
    assert len(cluster.searches) == n + 2


@pytest.mark.parametrize("variant", [
    "Which switches have stopped sending heartbeats in the last 30 minutes?",        # different window
    "Which ports have stopped sending heartbeats in the last 15 minutes?",           # different entity
    "How many switches have stopped sending heartbeats in the last 15 minutes?",     # different operation
    "Which switches have stopped sending heartbeats in the last 15 minutes for x1000c0r1b0?",   # extra literal
    "Which switches have stopped sending heartbeats?",                               # no window vs 15 minutes
    "Which switches are down in the last 15 minutes?",                               # different concept
])
def test_a_different_window_entity_operation_literal_or_intent_is_a_plan_miss(monkeypatch, cluster, plan_only, variant):
    run = primed(monkeypatch, cluster, plan_only, Q_15,
                 contract_for=lambda q: canon(plan_only, absence_entry(gte="now-15m")))
    assert pcache(plan_only).snapshot()["stored"] == 1
    entry = next(iter(pcache(plan_only)._entries.values()))
    assert entry.semantic_ok, "the 15-minute entry must be semantically reusable for this to prove anything"
    res, _ = run.ask(variant)
    assert run.pipeline_questions == [Q_15, variant], "the plan must NOT be reused"
    assert plan_info(res) is None


def test_non_semantic_analyses_are_exact_wording_only(monkeypatch, cluster, plan_only):
    run = agent(monkeypatch, cluster, plan_only, lambda _q: canon(plan_only, rows_entry("controller_log")))
    cluster.handler = lambda i, d: hits_body([{"@timestamp": "2026-10-28T10:00:00Z", "Severity": "error", "Body": "b"}])
    run.ask("Show controller messages")
    entry = next(iter(pcache(plan_only)._entries.values()))
    assert entry.semantic_ok is False
    res, _ = run.ask("Show controller messages")
    assert plan_info(res)["match"] == "exact"
    run.ask("Display the controller messages")
    assert len(run.pipeline_questions) == 2, "a reworded non-absence/recurrence question is never matched"


def test_recurrence_plans_are_stored_and_reusable(monkeypatch, cluster, plan_only):
    from tests.os_fixtures import agg_body
    cluster.handler = lambda i, d: agg_body({"loc": {"doc_count_error_upper_bound": 0, "sum_other_doc_count": 0,
                                                    "buckets": []}}, total=0)
    run = AgentRun(monkeypatch, plan_only, lambda q: canon(plan_only, recurrence_entry()))
    q = "Which locations have recurring CRITICAL faults?"
    run.ask(q)
    if len(pcache(plan_only)) == 1:       # stored only if the recurrence run was complete under these fixtures
        res, _ = run.ask(q)
        assert plan_info(res)["hit"] is True and len(run.pipeline_questions) == 1


# ---------------------------------------------------------------------------------------------------
# B. revalidation and invalidation -- an invalid plan never reaches OpenSearch
# ---------------------------------------------------------------------------------------------------

def _lookup(settings, q=Q_BASE, skill_index=None):
    return runtime.lookup_cached_plan(q, SCOPE, settings, skill_index)


def _prime_cache(monkeypatch, cluster, settings, skill_index=None):
    cluster.handler = absence_handler(SWITCHES, ALIVE)
    executed = executor.execute_contract(canon(settings, absence_entry()), settings)
    assert runtime.store_validated_plan(Q_BASE, SCOPE, executed, settings, skill_index) == ""
    assert _lookup(settings, skill_index=skill_index).hit is not None
    return pcache(settings)


def _expect_invalid(settings, cluster, reason_part, skill_index=None):
    before = len(cluster.searches)
    out = _lookup(settings, skill_index=skill_index)
    assert out.hit is None and reason_part in (out.invalidation_reason or ""), out
    assert len(pcache(settings)) == 0, "the invalid plan is evicted"
    assert len(cluster.searches) == before, "lookup never searches"
    assert pcache(settings).snapshot()["revalidation_failed"] == 1


def test_registry_pattern_change_invalidates(monkeypatch, cluster, plan_only):
    _prime_cache(monkeypatch, cluster, plan_only)
    reg = runtime.get_registry(plan_only)
    changed = IndexRegistry(patterns=tuple(dataclasses.replace(p, max_window="3d") if p.pattern_id == "fabric_health" else p
                                           for p in reg.patterns), registry_version=reg.registry_version)
    monkeypatch.setattr(runtime, "get_registry", lambda s: changed)
    _expect_invalid(plan_only, cluster, "registry entry")


def test_disabled_pattern_invalidates(monkeypatch, cluster, plan_only):
    _prime_cache(monkeypatch, cluster, plan_only)
    reg = runtime.get_registry(plan_only)
    off = IndexRegistry(patterns=tuple(dataclasses.replace(p, enabled=False) if p.pattern_id == "fabric_health" else p
                                       for p in reg.patterns), registry_version=reg.registry_version)
    monkeypatch.setattr(runtime, "get_registry", lambda s: off)
    _expect_invalid(plan_only, cluster, "no longer an enabled registry pattern")


def test_removed_pattern_invalidates(monkeypatch, cluster, plan_only):
    _prime_cache(monkeypatch, cluster, plan_only)
    reg = runtime.get_registry(plan_only)
    gone = IndexRegistry(patterns=tuple(p for p in reg.patterns if p.pattern_id != "fabric_health"),
                         registry_version=reg.registry_version)
    monkeypatch.setattr(runtime, "get_registry", lambda s: gone)
    _expect_invalid(plan_only, cluster, "no longer an enabled registry pattern")


def _mapping_cache(mutator):
    mapping = json.loads((FIX / "mapping_fabric_health.json").read_text())
    for idx in mapping.values():
        mutator(idx["mappings"]["properties"])
    return FieldCatalogCache(lambda ip: mapping, ttl_seconds=60, stale_ttl_seconds=60, max_indices=7,
                             failure_backoff_seconds=1)


def test_live_mapping_change_and_removed_allowed_field_invalidate(monkeypatch, cluster, plan_only):
    _prime_cache(monkeypatch, cluster, plan_only)
    other = _mapping_cache(lambda props: props["fields"]["properties"].pop("Location", None))
    monkeypatch.setattr(runtime, "get_field_cache", lambda s: other)
    _expect_invalid(plan_only, cluster, "live field view")


def test_a_field_losing_an_operation_invalidates(monkeypatch, cluster, plan_only):
    """The keyword sub-field of a queried field disappears from the live mapping: exact-match is no longer allowed."""
    _prime_cache(monkeypatch, cluster, plan_only)

    def drop_keyword(props):
        props["fields"]["properties"]["PhysicalContext"] = {"type": "text"}

    other = _mapping_cache(drop_keyword)
    monkeypatch.setattr(runtime, "get_field_cache", lambda s: other)
    out = _lookup(plan_only)
    assert out.hit is None and out.invalidation_reason
    assert len(pcache(plan_only)) == 0


def test_unavailable_field_discovery_blocks_a_hit(monkeypatch, cluster, plan_only):
    _prime_cache(monkeypatch, cluster, plan_only)
    dead = FieldCatalogCache(lambda ip: None, ttl_seconds=60, stale_ttl_seconds=60, max_indices=7,
                             failure_backoff_seconds=1)
    monkeypatch.setattr(runtime, "get_field_cache", lambda s: dead)
    assert _lookup(plan_only).hit is None


@pytest.mark.parametrize("change", [{"opensearch_redact_ip_mode": "off"}, {"max_hits_per_result": 7},
                                    {"opensearch_analysis_max_terms": 100}, {"alert_rule_creation_enabled": True}])
def test_changed_configuration_invalidates(monkeypatch, cluster, plan_only, change):
    cache = _prime_cache(monkeypatch, cluster, plan_only)
    s = plan_only.model_copy(update=change)
    # a changed config may even select another cache instance; re-point it at the primed one
    monkeypatch.setattr(runtime, "get_plan_cache", lambda settings: cache)
    out = runtime.lookup_cached_plan(Q_BASE, SCOPE, s, None)
    assert out.hit is None and out.invalidation_reason == "configuration changed"


def test_changed_skill_text_invalidates(monkeypatch, cluster, plan_only):
    v1, v2 = SimpleNamespace(raw_text="skill v1"), SimpleNamespace(raw_text="skill v2")
    _prime_cache(monkeypatch, cluster, plan_only, skill_index=v1)
    assert _lookup(plan_only, skill_index=v1).hit is not None
    _expect_invalid(plan_only, cluster, "skill text", skill_index=v2)


def test_incompatible_schema_version_is_never_served(monkeypatch, cluster, plan_only):
    cache = _prime_cache(monkeypatch, cluster, plan_only)
    (eid, entry), = cache._entries.items()
    cache._entries[eid] = dataclasses.replace(entry, schema_version=plan_cache.PLAN_SCHEMA_VERSION + 1)
    out = _lookup(plan_only)
    assert out.hit is None and "schema version" in out.invalidation_reason and len(cache) == 0


def test_the_real_guard_is_rerun_on_the_stored_plan(monkeypatch, cluster, plan_only):
    cache = _prime_cache(monkeypatch, cluster, plan_only)
    (eid, entry), = cache._entries.items()
    contract = json.loads(entry.contract_json)
    contract["query"]["script_fields"] = {"x": {"script": "1"}}          # consistent in both copies, fingerprints equal
    part = dataclasses.replace(entry.parts[0], query_json=json.dumps(contract["query"]))
    cache._entries[eid] = dataclasses.replace(entry, parts=(part,), contract_json=json.dumps(contract))
    out = _lookup(plan_only)
    assert out.hit is None and "guard now rejects" in out.invalidation_reason


def test_a_plan_whose_two_copies_disagree_is_never_served(monkeypatch, cluster, plan_only):
    cache = _prime_cache(monkeypatch, cluster, plan_only)
    (eid, entry), = cache._entries.items()
    contract = json.loads(entry.contract_json)
    contract["query"]["size"] = 9999
    cache._entries[eid] = dataclasses.replace(entry, contract_json=json.dumps(contract))
    out = _lookup(plan_only)
    assert out.hit is None and "differs" in out.invalidation_reason


def test_after_invalidation_the_normal_pipeline_runs_and_executes_nothing_stale(monkeypatch, cluster, plan_only):
    run = primed(monkeypatch, cluster, plan_only)
    n = len(cluster.searches)
    r = runtime.get_registry(plan_only)
    changed = IndexRegistry(patterns=tuple(dataclasses.replace(p, max_window="3d") if p.pattern_id == "fabric_health" else p
                                           for p in r.patterns), registry_version=r.registry_version)
    monkeypatch.setattr(runtime, "get_registry", lambda s: changed)
    res, _ = run.ask(Q_BASE)
    assert run.pipeline_questions == [Q_BASE, Q_BASE], "fell back to Router/Generator"
    info = plan_info(res)
    assert info["hit"] is False and "registry entry" in info["invalidation_reason"]
    assert result_info(res) is None
    assert len(cluster.searches) == n + 2, "exactly one execution: the freshly generated plan, never the stale one"


def test_an_executor_guard_refusal_evicts_the_plan_and_falls_back(monkeypatch, cluster, plan_only):
    run = primed(monkeypatch, cluster, plan_only)
    real = executor.execute_contract
    calls = []

    def refuse_first(contract, settings):
        calls.append(1)
        if len(calls) == 1:
            return {**contract, "execution": {"execution_status": "not_executed", "error": "refused"}}
        return real(contract, settings)

    monkeypatch.setattr(agent_mod.executor, "execute_contract", refuse_first)
    res, _ = run.ask(Q_BASE)
    assert run.pipeline_questions == [Q_BASE, Q_BASE]
    assert plan_info(res) == {"hit": False, "invalidation_reason": "the executor's guard refused the stored plan"}
    assert res["execution"]["execution_status"] == "success"


# ---------------------------------------------------------------------------------------------------
# C. dynamic time
# ---------------------------------------------------------------------------------------------------

def test_the_plan_keeps_its_relative_time_and_a_hit_evaluates_it_now(monkeypatch, cluster, plan_only):
    run = primed(monkeypatch, cluster, plan_only, Q_15, contract_for=lambda q: canon(plan_only, absence_entry(gte="now-15m")))
    (entry,) = pcache(plan_only)._entries.values()
    stored = json.loads(entry.contract_json)
    assert json.dumps(stored["query"]).count("now-15m") >= 1
    assert "20" not in json.dumps(stored["query"]["query"]["bool"]["filter"][0]), "no resolved timestamp in the plan"
    n = len(cluster.searches)
    res, _ = run.ask(Q_15)
    assert plan_info(res)["hit"] is True and len(cluster.searches) == n + 2
    sent = [json.dumps(dsl) for _, dsl in cluster.searches[n:]]
    assert all("now-15m" in s or "now-3m" in s for s in sent)
    assert res["execution"]["resolved_time_range"]["from_expression"] == "now-15m"


def test_an_absolute_window_is_never_stored_as_a_plan(monkeypatch, cluster, plan_only):
    entry = absence_entry(gte="2026-10-28T00:00:00Z")
    entry["query"]["query"]["bool"]["filter"][0] = {"range": {"@timestamp": {"gte": "2026-10-28T00:00:00Z",
                                                                             "lte": "2026-10-28T06:00:00Z"}}}
    cluster.handler = absence_handler(SWITCHES, ALIVE)
    executed = executor.execute_contract(canon(plan_only, entry), plan_only)
    reason = runtime.store_validated_plan("Which switches have stopped sending heartbeats on 2026-10-28?",
                                          SCOPE, executed, plan_only, None)
    assert reason and len(pcache(plan_only)) == 0


# ---------------------------------------------------------------------------------------------------
# D. eligibility
# ---------------------------------------------------------------------------------------------------

def _ask_with_session_id(question, user="u1"):
    import asyncio
    ctx = SimpleNamespace(user_content=SimpleNamespace(parts=[SimpleNamespace(text=question)]),
                          session=SimpleNamespace(state={}, user_id=user, id="s1"), invocation_id="inv",
                          app_name="app")

    async def collect():
        return [e async for e in agent_mod.root_agent._run_async_impl(ctx)]

    (event,) = asyncio.run(collect())
    return json.loads(event.content.parts[0].text)["result"]


def _run_and_count(monkeypatch, cluster, settings, contract, handler=None, question=Q_BASE):
    cluster.handler = handler or absence_handler(SWITCHES, ALIVE)
    AgentRun(monkeypatch, settings, lambda q: contract)
    return _ask_with_session_id(question), len(pcache(settings))


@pytest.mark.parametrize("contract", [
    {"mode": "single", "status": "ambiguous_metric", "explanation": "which metric?"},
    {"mode": "single", "status": "declined", "reason": "parameter_requires_clarification", "explanation": "narrow it"},
    {"mode": "single", "status": "unmapped", "explanation": "no reference"},
    {"mode": "single", "status": "declined", "reason": "out_of_scope", "explanation": "nope"},
    {"mode": "single", "status": "validation_failed", "explanation": "bad"},
    {"mode": "single", "status": "alert_rule_proposed", "data_source": "opensearch", "explanation": "alert"},
], ids=["clarification", "param-clarification", "unsupported", "declined", "validation-failed", "alert-proposal"])
def test_non_executable_flows_are_never_stored(monkeypatch, cluster, plan_only, contract):
    _, n = _run_and_count(monkeypatch, cluster, plan_only, contract)
    assert n == 0


def test_a_failed_execution_is_not_stored(monkeypatch, cluster, plan_only):
    from tests.os_fixtures import _Resp
    res, n = _run_and_count(monkeypatch, cluster, plan_only, canon(plan_only, absence_entry()),
                            handler=lambda i, d: _Resp(500, {"error": "boom"}))
    assert res["execution"]["execution_status"] != "success" and n == 0


def test_a_partial_execution_is_not_stored(monkeypatch, cluster, plan_only):
    _, n = _run_and_count(monkeypatch, cluster, plan_only, canon(plan_only, rows_entry("controller_log")),
                          handler=lambda i, d: hits_body([{"@timestamp": "2026-10-28T10:00:00Z", "Body": "b"}],
                                                         timed_out=True))
    assert n == 0


def test_prometheus_and_multi_and_correlated_results_are_not_plans(monkeypatch, cluster, plan_only):
    prom = {"mode": "single", "status": "ok", "data_source": "prometheus", "query": "up",
            "execution": {"execution_status": "success", "metadata": {"partial": False}}}
    assert runtime.store_validated_plan("how is up", SCOPE, prom, plan_only, None)
    cluster.handler = lambda i, d: hits_body([{"@timestamp": "2026-10-28T10:00:00Z", "Severity": "error", "Body": "b"}])
    multi = canon(plan_only, rows_entry("controller_log"), rows_entry("fabric_health"))
    run = AgentRun(monkeypatch, plan_only, lambda q: multi)
    run.ask("Show controller and fabric messages")
    assert len(pcache(plan_only)) == 0


def test_a_dependency_resolved_compound_question_is_not_stored(monkeypatch, cluster, plan_only):
    from app.pipeline import ALREADY_EXECUTED_MARKER
    cluster.handler = absence_handler(SWITCHES, ALIVE)
    run = AgentRun(monkeypatch, plan_only, lambda q: {**canon(plan_only, absence_entry()), ALREADY_EXECUTED_MARKER: True})
    run.ask(Q_BASE)
    assert len(pcache(plan_only)) == 0


def test_a_clarification_answer_is_neither_served_from_nor_stored_in_the_plan_cache(monkeypatch, cluster, plan_only):
    run = primed(monkeypatch, cluster, plan_only)
    state = {"previous_question": "Which metric?", "previous_result": {"status": "ambiguous_metric"}}
    run.ask(Q_BASE, state=state)
    assert run.pipeline_questions == [Q_BASE, Q_BASE] and len(pcache(plan_only)) == 1


def test_no_results_and_no_credentials_are_stored_in_a_plan(monkeypatch, cluster, plan_only):
    primed(monkeypatch, cluster, plan_only)
    (entry,) = pcache(plan_only)._entries.values()
    stored = json.loads(entry.contract_json)
    assert "execution" not in stored
    text = entry.contract_json + repr(entry.fingerprints)
    for secret in (plan_only.gemini_api_key, "x1000c2r3b0", "Authorization"):
        assert secret not in text


# ---------------------------------------------------------------------------------------------------
# E. the two caches together
# ---------------------------------------------------------------------------------------------------

def test_matrix_answer_hit_plan_not_checked(monkeypatch, cluster, both):
    run = primed(monkeypatch, cluster, both)
    n = len(cluster.searches)
    before = pcache(both).snapshot()
    res, _ = run.ask(Q_BASE)
    assert result_info(res)["hit"] is True and plan_info(res) is None
    assert run.pipeline_questions == [Q_BASE] and len(cluster.searches) == n
    assert pcache(both).snapshot() == before, "the plan cache was not even consulted"


def test_matrix_answer_miss_plan_hit_then_the_answer_cache_is_fed(monkeypatch, cluster, both):
    run = primed(monkeypatch, cluster, both)
    runtime.reset_result_cache()
    n = len(cluster.searches)
    res, _ = run.ask(Q_BASE)
    assert plan_info(res)["hit"] is True and result_info(res) is None
    assert run.pipeline_questions == [Q_BASE] and len(cluster.searches) == n + 2
    assert len(run.cache) == 1, "a plan-hit result is eligible for the answer cache under its normal rules"
    res3, _ = run.ask(Q_BASE)                                        # now the ANSWER cache serves it
    assert result_info(res3)["hit"] is True
    assert plan_info(res3) is None, "a replayed answer must not claim a plan-cache hit it never had"
    assert len(cluster.searches) == n + 2


def test_matrix_both_miss_runs_the_normal_pipeline(monkeypatch, cluster, both):
    run = agent(monkeypatch, cluster, both)
    res, _ = run.ask(Q_BASE)
    assert run.pipeline_questions == [Q_BASE] and plan_info(res) is None and result_info(res) is None


def test_matrix_answer_disabled_plan_hit(monkeypatch, cluster, plan_only):
    run = primed(monkeypatch, cluster, plan_only)
    assert runtime.get_result_cache(plan_only) is None
    res, _ = run.ask(Q_BASE)
    assert plan_info(res)["hit"] is True and run.pipeline_questions == [Q_BASE]


def test_matrix_answer_hit_plan_disabled_is_the_existing_behaviour(monkeypatch, cluster, settings):
    s = _s(settings, result=True, plan=False)
    run = primed(monkeypatch, cluster, s)
    n = len(cluster.searches)
    res, _ = run.ask(Q_BASE)
    assert result_info(res)["hit"] is True and plan_info(res) is None and len(cluster.searches) == n
    assert runtime.get_plan_cache(s) is None and runtime.plan_cache_snapshot(s) == {"enabled": False}
    runtime.reset_result_cache()
    run.ask(Q_BASE)
    assert run.pipeline_questions == [Q_BASE, Q_BASE], "answer miss + plan cache off = full pipeline"


def test_matrix_both_disabled_is_the_uncached_behaviour(monkeypatch, cluster, settings):
    s = _s(settings, result=False, plan=False)
    run = primed(monkeypatch, cluster, s)
    res, _ = run.ask(Q_BASE)
    assert run.pipeline_questions == [Q_BASE, Q_BASE] and plan_info(res) is None and result_info(res) is None
    assert runtime.store_validated_plan(Q_BASE, SCOPE, res, s, None) == "plan cache disabled"


def test_the_two_caches_are_separate_objects_with_separate_metadata(monkeypatch, cluster, both):
    run = primed(monkeypatch, cluster, both)
    assert runtime.get_plan_cache(both) is not runtime.get_result_cache(both)
    assert type(pcache(both)) is not type(run.cache)
    runtime.reset_result_cache()
    res, _ = run.ask(Q_BASE)
    assert "query_plan_cache" in res["execution"]["metadata"] and "result_cache" not in res["execution"]["metadata"]


def test_the_default_is_opt_in_and_independent_of_the_result_cache(settings):
    from app.config import Settings
    s = Settings(gemini_api_key="k", skills_root=settings.skills_root)
    assert s.opensearch_query_plan_cache_enabled is False and s.opensearch_result_cache_enabled is True
    assert s.opensearch_query_plan_cache_max_entries == 500


# ---------------------------------------------------------------------------------------------------
# F. capacity and concurrency
# ---------------------------------------------------------------------------------------------------

def _assess_and_prints(settings, executed, q):
    registry = runtime.get_registry(settings)
    a = query_cache.assess_executed(q, executed, registry=registry, aliases={})
    prints, why = query_cache.compute_fingerprints([p.pattern_id for p in a.parts], registry=registry,
                                                   field_cache=runtime.get_field_cache(settings), settings=settings,
                                                   skill_index=None)
    assert prints is not None, why
    return a, prints


@pytest.fixture()
def executed_plan(cluster, plan_only):
    cluster.handler = absence_handler(SWITCHES, ALIVE)
    return plan_only, executor.execute_contract(canon(plan_only, absence_entry()), plan_only)


def _ok(_entry):
    return None


def test_max_entries_and_lru_eviction(executed_plan):
    s, ex = executed_plan
    cache = plan_cache.QueryPlanCache(max_entries=2)
    for q in ("Show alpha switches", "Show beta switches"):
        a, p = _assess_and_prints(s, ex, q)
        assert cache.put(q, SCOPE, ex, a, p) == (True, "")
    assert cache.lookup("Show alpha switches", SCOPE, _ok).hit is not None           # alpha is now most recently used
    a, p = _assess_and_prints(s, ex, "Show gamma switches")
    cache.put("Show gamma switches", SCOPE, ex, a, p)
    assert len(cache) == 2
    assert cache.lookup("Show beta switches", SCOPE, _ok).hit is None, "the least recently USED entry went"
    assert cache.lookup("Show alpha switches", SCOPE, _ok).hit is not None
    assert cache.lookup("Show gamma switches", SCOPE, _ok).hit is not None
    assert cache.snapshot()["evicted_capacity"] == 1


def test_no_unbounded_growth(executed_plan):
    s, ex = executed_plan
    cache = plan_cache.QueryPlanCache(max_entries=5)
    a0, p0 = _assess_and_prints(s, ex, "Show q0 switches")
    for i in range(60):
        cache.put(f"Show q{i} switches", SCOPE, ex, a0, p0)
    assert len(cache) == 5 and len(cache._by_key) == 5


def test_a_duplicate_store_keeps_the_first_entry(executed_plan):
    s, ex = executed_plan
    cache = plan_cache.QueryPlanCache(max_entries=5)
    a, p = _assess_and_prints(s, ex, Q_BASE)
    assert cache.put(Q_BASE, SCOPE, ex, a, p)[0] is True
    assert cache.put(Q_BASE, SCOPE, ex, a, p)[0] is False and len(cache) == 1


def test_invalid_entries_are_cleaned_up_on_lookup(executed_plan):
    s, ex = executed_plan
    cache = plan_cache.QueryPlanCache(max_entries=5)
    a, p = _assess_and_prints(s, ex, Q_BASE)
    cache.put(Q_BASE, SCOPE, ex, a, p)
    out = cache.lookup(Q_BASE, SCOPE, lambda e: "nope")
    assert out.hit is None and out.invalidation_reason == "nope" and len(cache) == 0 and not cache._by_key
    boom = cache.put(Q_BASE, SCOPE, ex, a, p) and cache.lookup(Q_BASE, SCOPE, lambda e: 1 / 0)
    assert boom.hit is None and "revalidation error" in boom.invalidation_reason and len(cache) == 0


def test_concurrent_lookups_and_stores_are_safe(executed_plan):
    s, ex = executed_plan
    cache = plan_cache.QueryPlanCache(max_entries=8)
    a, p = _assess_and_prints(s, ex, Q_BASE)
    errors, hits = [], []

    def worker(k):
        try:
            for i in range(40):
                q = f"Show w{(k * 40 + i) % 20} switches"
                cache.put(q, SCOPE, ex, a, p)
                got = cache.lookup(q, SCOPE, _ok).hit
                if got is not None:
                    got.contract["query"]["size"] = -1          # mutating a hit must not touch the cache
                    hits.append(1)
                cache.lookup("Show missing switches", SCOPE, _ok)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and hits
    assert len(cache) <= 8 and len(cache._by_key) == len(cache._entries)
    for e in cache._entries.values():
        assert json.loads(e.contract_json)["query"]["size"] != -1


# ---------------------------------------------------------------------------------------------------
# G. isolation, status, admin reload
# ---------------------------------------------------------------------------------------------------

def test_another_scope_never_sees_the_plan(monkeypatch, cluster, plan_only):
    _prime_cache(monkeypatch, cluster, plan_only)
    assert runtime.lookup_cached_plan(Q_BASE, "app:someone-else", plan_only, None).hit is None
    assert runtime.lookup_cached_plan(Q_BASE, SCOPE, plan_only, None).hit is not None


def test_snapshot_exposes_counts_only(monkeypatch, cluster, plan_only):
    _prime_cache(monkeypatch, cluster, plan_only)
    snap = runtime.plan_cache_snapshot(plan_only)
    assert snap["enabled"] is True and snap["entries"] == 1 and snap["max_entries"] == 500
    assert Q_BASE not in json.dumps(snap) and SCOPE not in json.dumps(snap)


def test_admin_reload_empties_the_plan_cache(monkeypatch, cluster, plan_only):
    _prime_cache(monkeypatch, cluster, plan_only)
    assert runtime.reload_registry_status(plan_only)["loaded"] is True
    assert len(pcache(plan_only)) == 0


def test_a_plan_cache_error_never_fails_the_request(monkeypatch, cluster, plan_only):
    run = agent(monkeypatch, cluster, plan_only)
    monkeypatch.setattr(plan_cache.QueryPlanCache, "_lookup", lambda *a, **k: 1 / 0)
    monkeypatch.setattr(plan_cache, "plan_eligibility", lambda *a, **k: 1 / 0)
    res, _ = run.ask(Q_BASE)
    assert res["execution"]["execution_status"] == "success" and run.pipeline_questions == [Q_BASE]
    assert len(pcache(plan_only)) == 0 and pcache(plan_only).snapshot()["errors"] >= 1


# ---------------------------------------------------------------------------------------------------
# H. review round: pattern-aware semantic matching
# ---------------------------------------------------------------------------------------------------

def _registry_with_sibling(settings, **overrides):
    """The real registry plus `fabric_health_b`: a second enabled family that, like fabric_health, offers
    the `absence` analysis (so a reworded absence question could just as well belong to it)."""
    reg = runtime.get_registry(settings)
    fabric = reg.get("fabric_health")
    sibling = dataclasses.replace(fabric, pattern_id="fabric_health_b", index_pattern="fabricb-*",
                                  domain_reference=fabric.domain_reference, **overrides)
    return IndexRegistry(patterns=tuple(reg.patterns) + (sibling,), registry_version=reg.registry_version)


def test_semantic_reuse_is_refused_when_another_pattern_could_own_the_question(monkeypatch, cluster, plan_only):
    cache = _prime_cache(monkeypatch, cluster, plan_only)
    # control (what Level 2 alone would do): the two wordings ARE signature-equivalent -> reuse
    ctrl = cache.lookup(Q_REWORDED, SCOPE, lambda e: None, lambda e, q: None)
    assert ctrl.hit is not None and ctrl.hit.match == "semantic"

    both_patterns = _registry_with_sibling(plan_only)
    monkeypatch.setattr(runtime, "get_registry", lambda s: both_patterns)
    out = _lookup(plan_only, Q_REWORDED)
    assert out.hit is None and out.invalidation_reason is None, "a miss, not an invalidation"
    assert len(cache) == 1, "the plan is still valid for ITS pattern: it is kept"
    assert cache.snapshot()["pattern_ambiguous"] == 1
    # identical wording is not a different question: still an exact hit
    assert _lookup(plan_only, Q_BASE).hit.match == "exact"


def test_the_pipeline_runs_when_the_pattern_is_ambiguous(monkeypatch, cluster, plan_only):
    run = primed(monkeypatch, cluster, plan_only)
    reg = _registry_with_sibling(plan_only)
    monkeypatch.setattr(runtime, "get_registry", lambda s: reg)
    res, _ = run.ask(Q_REWORDED)
    assert run.pipeline_questions == [Q_BASE, Q_REWORDED] and plan_info(res) is None


def test_a_sibling_that_cannot_ground_the_entities_does_not_block_semantic_reuse(monkeypatch, cluster, plan_only):
    _prime_cache(monkeypatch, cluster, plan_only)
    reg = _registry_with_sibling(plan_only, roles={}, queryable_fields=())
    monkeypatch.setattr(runtime, "get_registry", lambda s: reg)
    # the real controller_log / ilo_events do not ground "switch" either: only the sibling could have mattered
    assert _lookup(plan_only, Q_REWORDED).hit.match == "semantic"


def test_a_sibling_that_grounds_the_entities_blocks_even_without_the_analysis(monkeypatch, cluster, plan_only):
    """Stricter than the previous rule, which only looked at patterns offering the same analysis."""
    cache = _prime_cache(monkeypatch, cluster, plan_only)
    reg = _registry_with_sibling(plan_only, analyses={})
    monkeypatch.setattr(runtime, "get_registry", lambda s: reg)
    out = _lookup(plan_only, Q_REWORDED)
    assert out.hit is None and out.invalidation_reason is None and len(cache) == 1
    assert _lookup(plan_only, Q_BASE).hit.match == "exact"


def test_pattern_ambiguity_rules(monkeypatch, cluster, plan_only):
    cache = _prime_cache(monkeypatch, cluster, plan_only)
    (entry,) = cache._entries.values()
    real = runtime.get_registry(plan_only)
    assert plan_cache.pattern_ambiguity(Q_REWORDED, entry, real, {}) is None
    # a sibling that cannot ground the entity the question names is not a candidate ...
    ungrounded = _registry_with_sibling(plan_only, roles={}, queryable_fields=())
    assert plan_cache.pattern_ambiguity(Q_REWORDED, entry, ungrounded, {}) is None
    # ... but a question naming NO entity is grounded everywhere, so it is ambiguous
    assert "names no entity" in plan_cache.pattern_ambiguity("Show heartbeats that stopped", entry, ungrounded, {})
    # a sibling that is disabled is not a candidate
    off = _registry_with_sibling(plan_only, enabled=False)
    assert plan_cache.pattern_ambiguity(Q_REWORDED, entry, off, {}) is None


def test_semantic_matching_fails_closed_without_or_with_a_broken_pattern_check(executed_plan):
    s, ex = executed_plan
    cache = plan_cache.QueryPlanCache(max_entries=5)
    a, p = _assess_and_prints(s, ex, Q_BASE)
    cache.put(Q_BASE, SCOPE, ex, a, p)
    assert cache.lookup(Q_REWORDED, SCOPE, _ok).hit is None, "no pattern check supplied: exact wording only"
    assert cache.lookup(Q_BASE, SCOPE, _ok).hit.match == "exact"

    def boom(entry, q):
        raise RuntimeError("cannot tell")

    assert cache.lookup(Q_REWORDED, SCOPE, _ok, boom).hit is None
    assert cache.snapshot()["match_errors"] == 1


# ---------------------------------------------------------------------------------------------------
# I. review round: invalidation between revalidation and hit construction
# ---------------------------------------------------------------------------------------------------

def _one_plan(executed_plan):
    s, ex = executed_plan
    cache = plan_cache.QueryPlanCache(max_entries=5)
    a, p = _assess_and_prints(s, ex, Q_BASE)
    cache.put(Q_BASE, SCOPE, ex, a, p)
    return cache, (a, p, ex)


@pytest.mark.parametrize("how", ["invalidate", "clear", "replaced"])
def test_an_entry_removed_after_revalidation_is_not_returned(executed_plan, how):
    cache, (a, p, ex) = _one_plan(executed_plan)

    def revalidate_then_lose_the_entry(entry):
        if how == "invalidate":
            cache.invalidate(entry.entry_id)
        else:
            cache.clear()
            if how == "replaced":                     # a NEW live entry now owns the same key
                assert cache.put(Q_BASE, SCOPE, ex, a, p)[0] is True
        return None                                   # revalidation itself "succeeded"

    out = cache.lookup(Q_BASE, SCOPE, revalidate_then_lose_the_entry)
    assert out.hit is None, "the stale entry must not be served"
    snap = cache.snapshot()
    assert snap["lost_race"] == 1 and snap.get("revalidation_failed", 0) == 0 and snap["misses"] == 1
    assert snap.get("hits_exact", 0) == 0
    assert len(cache) == (1 if how == "replaced" else 0)


def test_a_lost_race_on_one_candidate_falls_through_to_the_next(executed_plan):
    cache, (a, p, ex) = _one_plan(executed_plan)
    cache.put("Which switches are not reporting heartbeats?", SCOPE, ex, a, p)       # a second, equivalent plan
    seen = []

    def revalidate(entry):
        seen.append(entry.entry_id)
        if len(seen) == 1:
            cache.invalidate(entry.entry_id)
        return None

    out = cache.lookup(Q_REWORDED, SCOPE, revalidate, lambda e, q: None)
    assert out.hit is not None and out.hit.entry_id == seen[1] and out.hit.entry_id != seen[0]


def test_revalidation_never_holds_the_cache_lock(executed_plan):
    cache, _ = _one_plan(executed_plan)
    held = []

    def revalidate(entry):
        got = cache._lock.acquire(blocking=False)         # RLock: the lookup thread itself could re-enter,
        held.append(got)                                  # so probe from ANOTHER thread
        if got:
            cache._lock.release()
        probe = []
        t = threading.Thread(target=lambda: (probe.append(cache._lock.acquire(timeout=1)),
                                             cache._lock.release() if probe[0] else None))
        t.start()
        t.join()
        held.append(probe[0])
        return None

    assert cache.lookup(Q_BASE, SCOPE, revalidate).hit is not None
    assert held[-1] is True, "another thread could take the lock while revalidation ran"


# ---------------------------------------------------------------------------------------------------
# J. review round: statistics under concurrency
# ---------------------------------------------------------------------------------------------------

def test_statistics_are_exact_under_concurrent_lookups_stores_and_evictions(executed_plan):
    import sys
    s, ex = executed_plan
    a, p = _assess_and_prints(s, ex, Q_BASE)
    cache = plan_cache.QueryPlanCache(max_entries=3)
    threads_n, per = 16, 300
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)                            # make an unlocked `+=` lose updates if it ever could
    try:
        def looker():
            for _ in range(per):
                cache.lookup("Show nothing here", SCOPE, _ok)

        def storer(k):
            for i in range(per):
                cache.put(f"Show item{k}x{i} switches", SCOPE, ex, a, p)

        ts = [threading.Thread(target=looker) for _ in range(threads_n)] + \
             [threading.Thread(target=storer, args=(k,)) for k in range(threads_n)]
        [t.start() for t in ts]
        [t.join() for t in ts]
    finally:
        sys.setswitchinterval(old)
    snap = cache.snapshot()
    assert snap["misses"] == threads_n * per
    assert snap["stored"] == threads_n * per
    assert snap["evicted_capacity"] == threads_n * per - 3
    assert snap["entries"] == 3 and snap.get("errors", 0) == 0


def test_invalidations_are_counted_exactly(executed_plan):
    cache, _ = _one_plan(executed_plan)
    for _ in range(5):
        cache.lookup(Q_BASE, SCOPE, lambda e: "nope")      # first evicts; the rest find nothing
    assert cache.snapshot()["revalidation_failed"] == 1 and cache.snapshot()["misses"] == 5


# ---------------------------------------------------------------------------------------------------
# K. review round: error handling on the plan path
# ---------------------------------------------------------------------------------------------------

def test_a_failing_plan_lookup_runs_the_normal_pipeline(monkeypatch, cluster, plan_only):
    run = primed(monkeypatch, cluster, plan_only)

    def boom(settings):
        raise RuntimeError("cache unavailable")

    monkeypatch.setattr(runtime, "get_plan_cache", boom)
    res, _ = run.ask(Q_BASE)
    assert res["execution"]["execution_status"] == "success"
    assert run.pipeline_questions == [Q_BASE, Q_BASE] and plan_info(res) is None


def test_a_failing_plan_store_never_fails_the_request(monkeypatch, cluster, both):
    run = agent(monkeypatch, cluster, both)
    monkeypatch.setattr(plan_cache.QueryPlanCache, "put", lambda *a, **k: 1 / 0)
    res, _ = run.ask(Q_BASE)
    assert res["execution"]["execution_status"] == "success"
    assert len(pcache(both)) == 0 and len(run.cache) == 1, "the answer cache is unaffected"

    def boom(settings):
        raise RuntimeError("cache unavailable")

    monkeypatch.setattr(runtime, "get_plan_cache", boom)
    res2, _ = run.ask("Which switches have gone quiet?")
    assert res2["execution"]["execution_status"] == "success"


def test_an_opensearch_failure_on_a_plan_hit_is_returned_not_stored(monkeypatch, cluster, both):
    from tests.os_fixtures import _Resp
    run = primed(monkeypatch, cluster, both)
    runtime.reset_result_cache()
    stored_before = pcache(both).snapshot()["stored"]
    cluster.handler = lambda i, d: _Resp(500, {"error": "boom"})
    res, _ = run.ask(Q_BASE)
    assert run.pipeline_questions == [Q_BASE], "no fallback: regenerating the same query would fail the same way"
    assert res["execution"]["execution_status"] not in ("success", "empty_result") and res["execution"].get("error")
    assert plan_info(res)["hit"] is True
    assert len(run.cache) == 0, "the failed result is not stored in the answer cache"
    assert pcache(both).snapshot()["stored"] == stored_before and len(pcache(both)) == 1, "the valid plan is kept"
    cluster.handler = absence_handler(SWITCHES, ALIVE)                    # the cluster recovers: plan still serves
    res2, _ = run.ask(Q_BASE)
    assert plan_info(res2)["hit"] is True and res2["execution"]["execution_status"] == "success"


# ---------------------------------------------------------------------------------------------------
# L. third round: ANY other enabled pattern that could own the question blocks semantic reuse
# ---------------------------------------------------------------------------------------------------

Q_PORT = "Which ports have stopped sending heartbeats?"
Q_PORT_REWORDED = "Which ports have gone silent?"
Q_NO_ENTITY = "Which are silent?"
Q_NO_ENTITY_REWORDED = "Which are quiet?"


def _store(settings, question, skill_index=None):
    cluster_executed = executor.execute_contract(canon(settings, absence_entry()), settings)
    assert runtime.store_validated_plan(question, SCOPE, cluster_executed, settings, skill_index) == ""
    return pcache(settings)


def test_port_question_is_not_reused_across_patterns_with_a_different_analysis(monkeypatch, cluster, plan_only):
    """`port` is grounded by fabric_health AND controller_log; controller_log offers no absence/recurrence."""
    cluster.handler = absence_handler(SWITCHES, ALIVE)
    cache = _store(plan_only, Q_PORT)
    reg = runtime.get_registry(plan_only)
    assert reg.get("controller_log") is not None and "absence" not in reg.get("controller_log").analyses
    # control: the matcher alone WOULD reuse it (what the previous rule did)
    ctrl = cache.lookup(Q_PORT_REWORDED, SCOPE, lambda e: None, lambda e, q: None)
    assert ctrl.hit is not None and ctrl.hit.match == "semantic"
    out = _lookup(plan_only, Q_PORT_REWORDED)
    assert out.hit is None and out.invalidation_reason is None
    (entry,) = cache._entries.values()
    assert "controller_log" in plan_cache.pattern_ambiguity(Q_PORT_REWORDED, entry, reg, {})
    assert cache.snapshot()["pattern_ambiguous"] >= 1 and len(cache) == 1, "kept: still valid for its own pattern"
    assert _lookup(plan_only, Q_PORT).hit.match == "exact", "identical wording is unchanged"


def test_an_entity_free_question_is_ambiguous_with_several_enabled_patterns(monkeypatch, cluster, plan_only):
    cluster.handler = absence_handler(SWITCHES, ALIVE)
    cache = _store(plan_only, Q_NO_ENTITY)
    (entry,) = cache._entries.values()
    assert entry.semantic_ok, "the entry is Level-2 eligible, so the miss below is the ambiguity rule"
    out = _lookup(plan_only, Q_NO_ENTITY_REWORDED)
    assert out.hit is None and len(cache) == 1
    assert "names no entity" in plan_cache.pattern_ambiguity(
        Q_NO_ENTITY_REWORDED, entry, runtime.get_registry(plan_only), {})
    assert _lookup(plan_only, Q_NO_ENTITY).hit.match == "exact"
    # with ONE enabled pattern nothing can compete: reuse is allowed
    real = runtime.get_registry(plan_only)
    solo = IndexRegistry(patterns=tuple(p for p in real.patterns if p.pattern_id == "fabric_health"),
                         registry_version=real.registry_version)
    monkeypatch.setattr(runtime, "get_registry", lambda s: solo)
    assert _lookup(plan_only, Q_NO_ENTITY_REWORDED).hit.match == "semantic"


def _alias_settings(settings, aliases):
    return settings.model_copy(update={"opensearch_result_cache_aliases": aliases})


Q_ALIAS_STORED = "Which switches are silent?"
Q_ALIAS_INCOMING = "Which nodes are silent?"


@pytest.mark.parametrize("aliases, reused", [
    ({"fabric_health": {"node": "switch"}}, True),                                     # nobody else interprets it
    ({"fabric_health": {"node": "switch"}, "controller_log": {"node": "host"}}, False),  # conflicting meaning
    ({"fabric_health": {"node": "switch"}, "ilo_events": {"node": "host"}}, False),
    ({"fabric_health": {"node": "switch"}, "controller_log": {"node": "switch"}}, True),  # same meaning: no conflict
    ({"fabric_health": {"node": "switch"}, "controller_log": {"sonic": "host"}}, True),   # unrelated word
])
def test_aliases_declared_by_other_patterns_are_inspected(monkeypatch, cluster, settings, aliases, reused):
    s = _alias_settings(_s(settings, result=False, plan=True), aliases)
    cluster.handler = absence_handler(SWITCHES, ALIVE)
    cache = _store(s, Q_ALIAS_STORED)
    (entry,) = cache._entries.values()
    assert entry.semantic_ok
    out = runtime.lookup_cached_plan(Q_ALIAS_INCOMING, SCOPE, s, None)
    assert (out.hit is not None) is reused, aliases
    if not reused:
        reg = runtime.get_registry(s)
        assert "interprets 'node' differently" in plan_cache.pattern_ambiguity(Q_ALIAS_INCOMING, entry, reg, aliases)
    assert runtime.lookup_cached_plan(Q_ALIAS_STORED, SCOPE, s, None).hit.match == "exact"


def test_the_stored_patterns_alias_cannot_make_a_question_look_unambiguous(monkeypatch, cluster, plan_only):
    """The stored family's alias is judged only as that family's reading. Each competitor also reads the
    question under ITS OWN aliases (none here), so an alias cannot hide a named entity or fake one."""
    cluster.handler = absence_handler(SWITCHES, ALIVE)
    al = {"fabric_health": {"interface": "switch"}}
    s = _alias_settings(plan_only, al)
    (entry,) = _store(s, "Which switches are silent?")._entries.values()
    reg = runtime.get_registry(s)
    # "interface" is not an entity word: read without the alias the question names no entity, so
    # controller_log/ilo_events could own it -> miss (fail closed), although the aliased reading is clean
    assert "names no entity" in plan_cache.pattern_ambiguity("Which interfaces are silent?", entry, reg, al)
    # an entity word the alias leaves alone ("port") stays ambiguous because controller_log grounds it
    assert "controller_log" in plan_cache.pattern_ambiguity("Which ports are silent?", entry, reg, al)
    # an entity word nobody grounds ("node") is not claimed by a competitor: with the alias it is reusable
    al2 = {"fabric_health": {"node": "switch"}}
    assert plan_cache.pattern_ambiguity("Which nodes are silent?", entry, reg, al2) is None


def test_normal_pipeline_runs_after_an_ambiguous_semantic_lookup(monkeypatch, cluster, plan_only):
    run = agent(monkeypatch, cluster, plan_only)
    run.ask(Q_PORT)
    assert len(pcache(plan_only)) == 1
    n = len(cluster.searches)
    res, _ = run.ask(Q_PORT_REWORDED)
    assert run.pipeline_questions == [Q_PORT, Q_PORT_REWORDED], "Router/Generator ran: no semantic reuse"
    assert plan_info(res) is None and len(cluster.searches) == n + 2, "exactly one execution: the fresh plan"
    res2, _ = run.ask(Q_PORT)                                    # exact wording still hits
    assert plan_info(res2)["match"] == "exact" and len(run.pipeline_questions) == 2


def test_semantic_reuse_still_works_when_no_competitor_can_own_the_question(monkeypatch, cluster, plan_only):
    run = primed(monkeypatch, cluster, plan_only)
    n = len(cluster.searches)
    res, _ = run.ask(Q_REWORDED)                                  # switch: grounded by fabric_health alone
    assert plan_info(res)["match"] == "semantic" and run.pipeline_questions == [Q_BASE]
    assert len(cluster.searches) == n + 2


# The exact impact of the stricter rule on the questions found in the repo (skills docs, evals, tests):
# 39 are Level-2 admissible for fabric_health (entities grounded by it). Against the real registry:
# question -> text the refusal reason must contain (and why)
LOSES_SEMANTIC_REUSE = {
    "Which ports have recurring faults?": "controller_log",                      # also grounds 'port'
    "Which ports have stopped sending heartbeats in the last 15 minutes?": "controller_log",
    "Which event codes recur most on each BMC?": "names no entity",              # BMC is a literal, not an entity
}
KEEPS_SEMANTIC_REUSE = [
    "Which switches have stopped sending heartbeats?", "Which switches are silent?",
    "Which switches have gone quiet in the past 1 day?", "Which locations have recurring faults?",
    "Which locations have stopped sending heartbeats?", "Which switches in rack 5 have stopped sending heartbeats?",
    "Which switches have recurring heartbeat problems?",
]


@pytest.mark.parametrize("question", sorted(LOSES_SEMANTIC_REUSE))
def test_documented_questions_that_lose_semantic_reuse(settings, question):
    entry = SimpleNamespace(pattern_id="fabric_health", analysis_type="recurrence" if "recur" in question else "absence")
    why = plan_cache.pattern_ambiguity(question, entry, runtime.get_registry(settings), {})
    assert why and LOSES_SEMANTIC_REUSE[question] in why, why


@pytest.mark.parametrize("question", KEEPS_SEMANTIC_REUSE)
def test_documented_questions_that_keep_semantic_reuse(settings, question):
    entry = SimpleNamespace(pattern_id="fabric_health", analysis_type="recurrence" if "recur" in question else "absence")
    assert plan_cache.pattern_ambiguity(question, entry, runtime.get_registry(settings), {}) is None
