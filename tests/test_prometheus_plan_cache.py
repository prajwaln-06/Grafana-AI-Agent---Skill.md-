"""Prometheus query-plan cache: pipeline-level tests (app.agent end to end) plus unit tests.

Only the Router/Generator (counting stub) and Prometheus' HTTP layer (FakePrometheus) are faked; the skill
index, the committed metric catalog, the validator, the executor and label discovery are the real ones.
"""
from __future__ import annotations

import copy
import dataclasses
import threading
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app import agent as agent_mod, label_discovery, pipeline, prometheus_plan_cache as ppc
from app.catalog import runtime as catalog_runtime
from app.catalog.loader import load_catalog
from app.catalog.rules import apply_rules
from app.config import Settings
from app.opensearch_catalog import plan_cache as os_plan
from app.opensearch_catalog import runtime as os_runtime
from app.skill_index import SkillIndex
from tests.prom_plan_fixtures import (Q_CPU, Q_CPU_REWORDED, REAL_CATALOG, REF_CPU, FakePrometheus,
                                      cpu_contract, instant_contract, mem_contract)
from tests.test_opensearch_result_cache import AgentRun

Q_MEM = "Show memory available for all nodes."
Q_LOAD = "Show load average for all nodes."
ALIASES = {"node-exporter": {"usage": "utilization"}}


def _contract_for(q: str) -> dict:
    if "memory" in q.lower():
        return mem_contract()
    if "load" in q.lower():
        return instant_contract()
    return cpu_contract()


class Env:
    def __init__(self, monkeypatch, settings, **over):
        base = {"catalog_path": REAL_CATALOG, "prometheus_query_plan_cache_enabled": True,
                "prometheus_query_plan_cache_aliases": ALIASES}
        base.update(over)
        self.settings = settings.model_copy(update=base)
        self.prom = FakePrometheus().install(monkeypatch)
        self.skill = SkillIndex.load(self.settings.skills_root, self.settings.catalog_path,
                                     catalog_provider=lambda: catalog_runtime.get_runtime(self.settings).catalog())
        self.run = AgentRun(monkeypatch, self.settings, _contract_for)
        self.run.skill_index = self.skill
        self.monkeypatch = monkeypatch
        self.pipeline_kwargs: list[dict] = []
        inner = agent_mod.run_pipeline                     # AgentRun's counting Router/Generator stub

        async def spy(question, skill_index, settings, **kw):
            self.pipeline_kwargs.append(kw)
            return await inner(question, skill_index, settings, **kw)
        monkeypatch.setattr(agent_mod, "run_pipeline", spy)

    def ask(self, q):
        res, _ = self.run.ask(q)
        return res

    def info(self, res):
        return res["execution"]["metadata"].get("query_plan_cache")

    @property
    def pipeline_calls(self):
        return len(self.run.pipeline_questions)

    @property
    def cache(self):
        return ppc.get_cache(self.run.settings)

    def reconfigure(self, **over):
        self.run.settings = self.run.settings.model_copy(update=over)


@pytest.fixture()
def env(monkeypatch, settings):
    return Env(monkeypatch, settings)


@pytest.fixture()
def real_catalog():
    return apply_rules(load_catalog(REAL_CATALOG))


# ---- A. basic behaviour ----------------------------------------------------------------------------------

def test_miss_then_store_then_exact_hit_skips_router_and_generator_but_queries_prometheus_again(env):
    first = env.ask(Q_CPU)
    assert env.info(first) == {"cache": "prometheus", "hit": False, "stored": True, "cache_entry_id": 1,
                               "evicted": False}
    assert env.pipeline_calls == 1 and env.prom.data_queries == 1

    env.prom.value = "99.0"                                   # the data changed between the two questions
    second = env.ask(Q_CPU)
    info = env.info(second)
    assert info["hit"] is True and info["match"] == "exact" and info["revalidated"] is True
    assert info["cache_entry_id"] == 1 and info["age_seconds"] >= 0 and info["cache"] == "prometheus"
    assert env.pipeline_calls == 1, "Router/Generator must not run on a plan-cache hit"
    assert env.prom.data_queries == 2, "Prometheus must be executed again on a hit"
    assert env.prom.queries[0] == env.prom.queries[1]
    assert second["execution"]["execution_status"] == "success"
    assert second["execution"]["series"][0]["points"][0]["value"] == 99.0          # fresh data, not the old 12.5
    assert first["execution"]["series"][0]["points"][0]["value"] == 12.5
    assert "execution" not in ppc.plan_shell(second) and second["query"] == first["query"]
    snap = env.cache.snapshot()
    assert snap["hits_exact"] == 1 and snap["stored"] == 1 and snap["misses"] == 1 and snap["datasource"] == "prometheus"


def test_exact_hit_ignores_ordinary_case_punctuation_and_plurals(env):
    env.ask(Q_CPU)
    res = env.ask("show CPU utilization, for all nodes??")
    assert env.info(res)["hit"] is True and env.pipeline_calls == 1


def test_lowercased_acronym_is_conservatively_not_treated_as_the_same_literal(monkeypatch, settings):
    # inherited from the shared matcher: "cpu" and "CPU" are different literals, so this is a safe miss.
    # The built-in concept vocabulary deliberately supersedes this for CPU/GPU/RAM (they are concept words
    # in this domain; see tests/test_prometheus_concepts.py), so this pins the unchanged behaviour with the
    # vocabulary switched off. The assertion itself is the original one.
    e = Env(monkeypatch, settings, prometheus_query_plan_cache_concepts_enabled=False)
    e.ask(Q_CPU)
    assert e.info(e.ask("show cpu utilization for all nodes"))["hit"] is False


def test_hit_is_scoped_per_user(env):
    env.ask(Q_CPU)
    res, _ = env.run.ask(Q_CPU, user="someone-else")
    assert env.info(res)["hit"] is False and env.pipeline_calls == 2


def test_disabled_cache_leaves_pipeline_and_response_untouched(monkeypatch, settings):
    e = Env(monkeypatch, settings, prometheus_query_plan_cache_enabled=False)
    for _ in range(2):
        res = e.ask(Q_CPU)
        assert "query_plan_cache" not in res["execution"].get("metadata", {})
    assert e.pipeline_calls == 2 and e.prom.data_queries == 2
    assert ppc.get_cache(e.run.settings) is None and ppc.snapshot(e.run.settings) == {"enabled": False}


def test_default_is_disabled_and_independent_of_opensearch_flags(settings):
    s = Settings(gemini_api_key="k", skills_root=settings.skills_root)
    assert s.prometheus_query_plan_cache_enabled is False
    assert s.prometheus_query_plan_cache_max_entries == 500 and s.prometheus_query_plan_cache_semantic_enabled is True
    on_os = s.model_copy(update={"opensearch_query_plan_cache_enabled": True})
    assert not ppc.enabled(on_os)
    on_prom = s.model_copy(update={"prometheus_query_plan_cache_enabled": True})
    assert ppc.enabled(on_prom) and not on_prom.opensearch_query_plan_cache_enabled


def test_invalid_configuration_is_rejected(settings):
    for bad in ({"prometheus_query_plan_cache_max_entries": 0}, {"prometheus_query_plan_cache_max_entries": 10001},
                {"prometheus_query_plan_cache_aliases": {"node-exporter": {"two words": "x"}}},
                {"prometheus_query_plan_cache_aliases": {" ": {"a": "b"}}}):
        with pytest.raises(ValidationError):
            Settings(gemini_api_key="k", skills_root=settings.skills_root, **bad)


def test_prometheus_and_opensearch_plan_caches_are_separate_instances(env):
    env.ask(Q_CPU)
    assert isinstance(env.cache, ppc.PrometheusPlanCache) and isinstance(env.cache, os_plan.QueryPlanCache)
    assert env.cache.snapshot()["entries"] == 1
    assert os_runtime.plan_cache_snapshot(env.run.settings).get("enabled", False) is False


def test_opensearch_plan_cache_enabled_alone_adds_no_prometheus_metadata(monkeypatch, settings):
    e = Env(monkeypatch, settings, prometheus_query_plan_cache_enabled=False, opensearch_query_plan_cache_enabled=True)
    res = e.ask(Q_CPU)
    assert "query_plan_cache" not in res["execution"].get("metadata", {})


# ---- B. semantic matching ---------------------------------------------------------------------------------

def test_reworded_question_is_a_semantic_hit_when_the_alias_is_declared(env):
    env.ask(Q_CPU)
    res = env.ask(Q_CPU_REWORDED)
    info = env.info(res)
    assert info["hit"] is True and info["match"] == "semantic" and info["revalidated"] is True
    assert env.pipeline_calls == 1 and env.prom.data_queries == 2
    assert env.cache.snapshot()["hits_semantic"] == 1


def test_without_a_declared_alias_usage_and_utilization_are_not_assumed_equal(monkeypatch, settings):
    # With the built-in concept vocabulary OFF nothing but a declared alias bridges wordings (the original
    # contract of this test, unchanged). With it ON (the default) "cpu usage" is a recognised wording of
    # "cpu utilization": see tests/test_prometheus_concepts.py.
    e = Env(monkeypatch, settings, prometheus_query_plan_cache_aliases={},
            prometheus_query_plan_cache_concepts_enabled=False)
    e.ask(Q_CPU)
    res = e.ask(Q_CPU_REWORDED)
    assert e.info(res)["hit"] is False and e.pipeline_calls == 2


def test_semantic_matching_can_be_switched_off(monkeypatch, settings):
    e = Env(monkeypatch, settings, prometheus_query_plan_cache_semantic_enabled=False)
    e.ask(Q_CPU)
    assert e.info(e.ask(Q_CPU_REWORDED))["hit"] is False
    assert e.info(e.ask(Q_CPU))["hit"] is True                                          # exact still works


@pytest.mark.parametrize("other", [
    "Show memory utilization for all nodes.",                    # different pattern / reference
    "Show CPU utilization for node-01.",                         # different label filter / entity scope
    "Show CPU utilization per core for all nodes.",              # different grouping
    "Show CPU utilization for all nodes over the last 6 hours.", # different time window
    "Show average CPU utilization for all nodes.",               # different aggregation
    "Show CPU load average for all nodes.",                      # different metric
    "Show CPU utilization for all nodes in the idle mode.",      # extra filter word
])
def test_materially_different_questions_never_reuse_the_cpu_plan(env, other):
    env.ask(Q_CPU)
    res = env.ask(other)
    assert env.info(res)["hit"] is False and env.pipeline_calls == 2, other


def test_reference_ambiguity_blocks_a_semantic_hit(env, monkeypatch):
    env.ask(Q_CPU)
    top = ({"references/node-exporter/cpu.md", "references/node-exporter/memory.md"},
           {REF_CPU: {"node_cpu_seconds_total"}})
    monkeypatch.setattr(ppc, "_top_references", lambda *a, **k: (True, *top))
    res = env.ask(Q_CPU_REWORDED)
    assert env.info(res)["hit"] is False and env.pipeline_calls == 2
    assert env.cache.snapshot()["entries"] >= 1


def test_semantic_hit_needs_the_catalog_to_still_offer_the_plans_metrics(env, monkeypatch):
    env.ask(Q_CPU)
    monkeypatch.setattr(ppc, "_top_references", lambda *a, **k: (True, {REF_CPU}, {REF_CPU: {"node_load1"}}))
    res = env.ask(Q_CPU_REWORDED)
    assert env.info(res)["hit"] is False and env.pipeline_calls == 2
    snap = env.cache.snapshot()
    assert snap.get("pattern_ambiguous") == 1, "a deliberate ownership rejection"
    assert snap.get("match_errors", 0) == 0 and snap.get("errors", 0) == 0, "not a swallowed error"


def test_semantic_hit_needs_a_confident_catalog(env, monkeypatch):
    env.ask(Q_CPU)
    monkeypatch.setattr(ppc, "_top_references", lambda *a, **k: (False, set(), {}))
    assert env.info(env.ask(Q_CPU_REWORDED))["hit"] is False


def test_entry_whose_own_question_disagrees_with_its_plan_is_exact_only(env):
    # the question names a 6h window but the executed plan covers 1h: not offered to reworded questions
    env.run.contract_for = lambda q: cpu_contract()
    env.ask("Show CPU utilization for all nodes over the last 6 hours.")
    entry = next(iter(env.cache._entries.values()))
    assert entry.semantic_ok is False
    assert env.info(env.ask("Show CPU usage for all nodes over the last 6 hours.")) ["hit"] is False


# ---- C. revalidation ------------------------------------------------------------------------------------

def _with_metric(catalog, **over):
    return dataclasses.replace(catalog, metrics=tuple(
        dataclasses.replace(m, **over) if m.name == "node_cpu_seconds_total" else m for m in catalog.metrics))


def _rejected(env, reason_part, *, again=True):
    res = env.ask(Q_CPU)
    info = env.info(res)
    assert info["hit"] is False and reason_part in info["invalidation_reason"], info
    assert env.pipeline_calls == 2, "a rejected plan falls back to Router + Generator"
    assert env.cache.snapshot()["revalidation_failed"] >= 1
    return res


def test_catalog_metadata_change_rejects_the_plan(env, monkeypatch, real_catalog):
    env.ask(Q_CPU)
    changed = _with_metric(real_catalog, help="Something else entirely")
    monkeypatch.setattr(pipeline, "_get_catalog", lambda s: changed)
    _rejected(env, "catalog entry for ['node_cpu_seconds_total'] changed")


def test_metric_status_change_rejects_the_plan(env, monkeypatch, real_catalog):
    env.ask(Q_CPU)
    changed = _with_metric(real_catalog, status="rejected")
    monkeypatch.setattr(pipeline, "_get_catalog", lambda s: changed)
    _rejected(env, "catalog entry")


def test_catalog_becoming_unavailable_rejects_the_plan(env, monkeypatch):
    env.ask(Q_CPU)
    monkeypatch.setattr(pipeline, "_get_catalog", lambda s: None)
    _rejected(env, "catalog unavailable")


def test_metric_leaving_the_directory_rejects_the_plan(env, monkeypatch):
    env.ask(Q_CPU)
    monkeypatch.setattr(env.skill, "metric_directory", lambda path: {})
    _rejected(env, "no metric directory")


def test_label_key_change_rejects_the_plan(env):
    env.ask(Q_CPU)
    env.prom.series["node_cpu_seconds_total"] = [
        {k: v for k, v in s.items() if k != "cpu"} for s in env.prom.series["node_cpu_seconds_total"]]
    label_discovery.clear_cache()
    _rejected(env, "label keys of ['node_cpu_seconds_total'] changed")


def test_label_value_change_is_caught_by_the_validator_and_the_stale_plan_is_never_executed(env):
    env.ask(Q_CPU)
    stored_query = env.prom.queries[0][1]
    env.prom.series["node_cpu_seconds_total"] = [
        s for s in env.prom.series["node_cpu_seconds_total"] if s["mode"] != "idle"]
    label_discovery.clear_cache()
    env.run.contract_for = lambda q: cpu_contract('rate(node_cpu_seconds_total{mode="user"}[5m])')
    res = _rejected(env, "validator now rejects")
    assert [q for _, q in env.prom.queries].count(stored_query) == 1, "the invalid plan must not be executed"
    assert res["query"] == 'rate(node_cpu_seconds_total{mode="user"}[5m])'


def test_label_discovery_outage_rejects_the_plan(env):
    env.ask(Q_CPU)
    env.prom.fail_labels = True
    label_discovery.clear_cache()
    _rejected(env, "label discovery is unavailable")


def test_configuration_change_rejects_the_plan(env):
    env.ask(Q_CPU)
    env.reconfigure(prometheus_url="http://other-prometheus:9090")
    _rejected(env, "configuration changed")


def test_skill_text_change_rejects_the_plan(env):
    env.ask(Q_CPU)
    env.skill.raw_text += "\n- new routing rule\n"
    _rejected(env, "skill text changed")


def test_reference_text_change_rejects_the_plan(env, monkeypatch):
    env.ask(Q_CPU)
    real = env.skill.read_reference
    monkeypatch.setattr(env.skill, "read_reference", lambda p: real(p) + "\nedited")
    _rejected(env, "skill text changed")


def test_vanished_reference_rejects_the_plan(env, monkeypatch):
    env.ask(Q_CPU)

    def gone(path):
        raise FileNotFoundError(path)
    monkeypatch.setattr(env.skill, "read_reference", gone)
    _rejected(env, "skill text is unavailable")


def test_after_rejection_the_fresh_plan_is_stored_and_hits_again(env):
    env.ask(Q_CPU)
    env.reconfigure(prometheus_url="http://other-prometheus:9090")
    res = env.ask(Q_CPU)
    assert env.info(res)["stored"] is True and env.info(res)["hit"] is False
    assert env.info(env.ask(Q_CPU))["hit"] is True and env.pipeline_calls == 2


def test_executor_refusal_drops_the_plan_and_runs_the_pipeline(env, monkeypatch):
    env.ask(Q_CPU)
    from app import executor
    real = executor.execute_contract
    calls = {"n": 0}

    def refuse_once(contract, settings):
        calls["n"] += 1
        if calls["n"] == 1:
            return {**contract, "execution": {"execution_status": "not_executed"}}
        return real(contract, settings)
    monkeypatch.setattr(executor, "execute_contract", refuse_once)
    res = env.ask(Q_CPU)
    assert env.info(res)["hit"] is False and "executor did not run" in env.info(res)["invalidation_reason"]
    assert env.pipeline_calls == 2


# ---- D. eligibility --------------------------------------------------------------------------------------

def _ok_exec(**over):
    c = cpu_contract(**over)
    c["execution"] = {"execution_status": "success"}
    return c


@pytest.mark.parametrize("mutate, why", [
    (lambda c: c.update(mode="multi"), "single"),
    (lambda c: c.update(results=[]), "single"),
    (lambda c: c.update(correlation={"x": 1}), "correlated"),
    (lambda c: c.update(status="panic_mode_best_effort"), "status"),
    (lambda c: c.update(status="alert_proposal"), "status"),
    (lambda c: c.update(status="ambiguous_metric"), "status"),
    (lambda c: c.update(data_source="opensearch"), "not a Prometheus"),
    (lambda c: c["execution"].update(execution_status="empty_result"), "successful"),
    (lambda c: c["execution"].update(execution_status="endpoint_error"), "successful"),
    (lambda c: c.pop("execution"), "successful"),
    (lambda c: c.update(query=" "), "no PromQL"),
    (lambda c: c.update(reference_used=""), "no reference"),
    (lambda c: c.update(measurement_used={"type": "raw_metric", "name": None, "source_metrics": []}), "no metric"),
    (lambda c: c.update(time_range={"from": "2025-01-01T00:00:00Z", "to": "now", "step": "60s"}), "relative"),
    (lambda c: c.update(time_range={"from": "now-1h", "to": "2025-01-02T00:00:00Z", "step": "60s"}), "relative"),
    (lambda c: c.update(time_range={}), "relative"),
    (lambda c: c.update(query_type="instant", time_range={"time": "2025-01-01T00:00:00Z"}), "absolute"),
    (lambda c: c.update(query_type="weird"), "not stored"),
])
def test_ineligible_plans_are_refused(mutate, why):
    c = _ok_exec()
    mutate(c)
    assert why in ppc.plan_eligibility(c)


@pytest.mark.parametrize("c", [
    _ok_exec(), _ok_exec(window_from="now/d"),
    {**instant_contract(), "execution": {"execution_status": "success"}},
    {**instant_contract(time_range={"time": "now-5m"}), "execution": {"execution_status": "success"}},
])
def test_eligible_plans_are_accepted(c):
    assert ppc.plan_eligibility(c) == ""


def test_empty_result_is_not_stored_and_the_next_question_runs_the_pipeline_again(env):
    env.prom.mode = "empty"
    res = env.ask(Q_CPU)
    info = env.info(res)
    assert info["hit"] is False and info["stored"] is False and "successful" in info["not_stored_reason"]
    assert env.cache.snapshot()["entries"] == 0
    env.prom.mode = "ok"
    assert env.info(env.ask(Q_CPU))["hit"] is False and env.pipeline_calls == 2


def test_failed_execution_is_not_stored(env):
    env.prom.mode = "error"
    res = env.ask(Q_CPU)
    assert env.info(res)["stored"] is False and env.cache.snapshot()["entries"] == 0


def test_instant_query_plan_is_cached_and_hits(env):
    env.ask(Q_LOAD)
    res = env.ask(Q_LOAD)
    assert env.info(res)["hit"] is True and env.pipeline_calls == 1
    assert [p for p, _ in env.prom.queries] == ["/api/v1/query", "/api/v1/query"]


def test_multi_response_is_not_stored_or_annotated(env):
    env.run.contract_for = lambda q: {"mode": "multi", "synthesis": None,
                                      "results": [cpu_contract(), mem_contract()]}
    res = env.ask("Show CPU and memory for all nodes.")
    assert env.cache.snapshot()["entries"] == 0 and "query_plan_cache" not in str(res.get("metadata", ""))
    assert env.cache.snapshot()["not_stored"] == 1


def test_plan_naming_a_metric_missing_from_the_directory_is_not_stored(env):
    env.run.contract_for = lambda q: cpu_contract("node_nonexistent_total", "node_nonexistent_total")
    res = env.ask(Q_CPU)
    info = env.info(res) or {}
    assert not info.get("stored") and env.cache.snapshot()["entries"] == 0


def test_plan_that_does_not_validate_under_the_cache_context_is_not_stored(env):
    env.run.contract_for = lambda q: cpu_contract('rate(node_cpu_seconds_total{mode="bogus"}[5m])')
    env.ask(Q_CPU)
    assert env.cache.snapshot()["entries"] == 0


def test_label_discovery_outage_at_store_time_means_not_stored(env):
    env.prom.fail_labels = True
    res = env.ask(Q_CPU)
    assert env.info(res)["stored"] is False and "label discovery" in env.info(res)["not_stored_reason"]


def test_follow_up_turn_neither_reads_nor_writes_the_cache(env):
    env.ask(Q_CPU)                                                       # seeds an entry
    before = env.cache.snapshot()
    res, _ = env.run.ask(Q_CPU, state={"previous_question": "earlier", "previous_result": {"status": "ok"}})
    after = env.cache.snapshot()
    assert env.pipeline_calls == 2 and after["entries"] == before["entries"] == 1
    assert after.get("hits_exact", 0) == 0 and "query_plan_cache" not in res["execution"].get("metadata", {})


# ---- E. cache operations ---------------------------------------------------------------------------------

def test_capacity_and_lru_eviction(monkeypatch, settings):
    e = Env(monkeypatch, settings, prometheus_query_plan_cache_max_entries=2)
    e.ask(Q_CPU)
    e.ask(Q_MEM)
    assert e.info(e.ask(Q_CPU))["hit"] is True                      # Q_CPU is now the most recently used
    res = e.ask(Q_LOAD)                                              # evicts the least recently used: Q_MEM
    assert e.info(res)["evicted"] is True
    snap = e.cache.snapshot()
    assert snap["entries"] == 2 and snap["evicted_capacity"] == 1
    assert e.info(e.ask(Q_CPU))["hit"] is True
    assert e.info(e.ask(Q_MEM))["hit"] is False                     # was evicted


def test_malformed_entry_is_rejected_evicted_and_falls_back(env):
    env.ask(Q_CPU)
    eid, entry = next(iter(env.cache._entries.items()))
    env.cache._entries[eid] = dataclasses.replace(entry, contract_json="{not json")
    res = env.ask(Q_CPU)
    assert env.info(res)["hit"] is False and "malformed" in env.info(res)["invalidation_reason"]
    assert env.pipeline_calls == 2


def test_entry_with_executed_output_or_changed_query_is_rejected(env):
    env.ask(Q_CPU)
    eid, entry = next(iter(env.cache._entries.items()))
    forged = json_with(entry.contract_json, query="node_load1")
    env.cache._entries[eid] = dataclasses.replace(entry, contract_json=forged)
    res = env.ask(Q_CPU)
    assert "differs from the plan that was checked" in env.info(res)["invalidation_reason"]


def json_with(contract_json, **over):
    import json
    c = json.loads(contract_json)
    c.update(over)
    return json.dumps(c)


def test_unsupported_schema_version_is_rejected(env):
    env.ask(Q_CPU)
    eid, entry = next(iter(env.cache._entries.items()))
    env.cache._entries[eid] = dataclasses.replace(entry, schema_version=99)
    assert "schema version" in env.info(env.ask(Q_CPU))["invalidation_reason"]


def test_revalidation_crash_is_a_miss_not_a_failed_request(env, monkeypatch):
    env.ask(Q_CPU)

    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(ppc, "revalidate_entry", boom)
    res = env.ask(Q_CPU)
    assert res["execution"]["execution_status"] == "success"
    assert "revalidation error" in env.info(res)["invalidation_reason"] and env.pipeline_calls == 2


def test_store_crash_never_fails_the_request(env, monkeypatch):
    monkeypatch.setattr(ppc, "store_validated_plan", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    res = env.ask(Q_CPU)
    assert res["execution"]["execution_status"] == "success"


def test_lookup_crash_is_a_miss_not_a_failed_request(env, monkeypatch):
    env.ask(Q_CPU)
    monkeypatch.setattr(ppc.PrometheusPlanCache, "lookup", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    res = env.ask(Q_CPU)
    assert res["execution"]["execution_status"] == "success" and env.pipeline_calls == 2
    assert env.cache.snapshot()["errors"] == 1


def test_concurrent_identical_stores_keep_exactly_one_entry(env):
    s, sk = env.run.settings, env.skill
    executed = _ok_exec()
    baseline = ppc.capture_baseline(s)
    errors = []

    def work():
        try:
            ppc.store_validated_plan(Q_CPU, "u1", copy.deepcopy(executed), s, sk, baseline)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
    threads = [threading.Thread(target=work) for _ in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    snap = env.cache.snapshot()
    assert not errors and snap["entries"] == 1 and snap["stored"] == 1 and snap["duplicate_not_stored"] == 11


def test_concurrent_lookups_and_inserts_are_consistent(monkeypatch, settings):
    e = Env(monkeypatch, settings, prometheus_query_plan_cache_max_entries=3)
    s, sk = e.run.settings, e.skill
    questions = [Q_CPU, Q_MEM, Q_LOAD, "Show CPU utilization per core for all nodes.", "Show memory usage per node."]
    execs = {q: {**_contract_for(q), "execution": {"execution_status": "success"}} for q in questions}
    baseline = ppc.capture_baseline(s)
    errors = []

    def work(i):
        try:
            for q in questions * 2:
                ppc.store_validated_plan(q, "u1", copy.deepcopy(execs[q]), s, sk, baseline)
                look = ppc.lookup(q, "u1", s, sk)
                assert look.hit is None or look.hit.contract["query"] == execs[q]["query"]
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    snap = e.cache.snapshot()
    assert not errors and 0 < snap["entries"] <= 3 and snap["stored"] >= 3


def test_reset_empties_the_cache_and_snapshot_reports_state(env):
    env.ask(Q_CPU)
    assert ppc.snapshot(env.run.settings)["entries"] == 1
    ppc.reset()
    assert ppc.get_cache(env.run.settings).snapshot()["entries"] == 0


def test_entry_contract_with_the_base_class_is_pinned():
    needed = {"entry_id", "scope", "schema_version", "question", "canonical_text", "semantic_ok", "pattern_id",
              "contract_json", "created_at"}
    assert needed <= {f.name for f in dataclasses.fields(ppc.PromPlanEntry)}


def test_stored_contract_contains_no_execution_output(env):
    env.ask(Q_CPU)
    entry = next(iter(env.cache._entries.values()))
    assert "execution" not in entry.contract_json and "series" not in entry.contract_json


def test_opensearch_cache_state_is_unaffected(env):
    env.ask(Q_CPU)
    env.ask(Q_CPU)
    assert os_runtime.plan_cache_snapshot(env.run.settings).get("entries", 0) == 0


# ---- F. catalog routing revalidation (exact hits included) ------------------------------------------------

def _runtime_catalog(env):
    return catalog_runtime.get_runtime(env.run.settings).catalog()


def _publish(env, monkeypatch, catalog):
    """Swap the catalog snapshot the pipeline AND the skill index read (exactly what a background refresh
    or an admin reload publishes), restored automatically at test end."""
    rt = catalog_runtime.get_runtime(env.run.settings)
    monkeypatch.setattr(rt, "_snapshot", dataclasses.replace(rt.snapshot(), catalog=catalog))


def _entry(catalog, name):
    return next(m for m in catalog.metrics if m.name == name)


def _competitor(catalog, **over):
    base = dataclasses.replace(_entry(catalog, "node_cpu_seconds_total"), name="node_cpu_competitor_total", **over)
    return dataclasses.replace(catalog, metrics=catalog.metrics + (base,))


def test_competing_catalog_entry_rejects_an_exact_hit_without_touching_the_plans_own_entry(env, monkeypatch):
    env.ask(Q_CPU)
    stored_query = env.prom.queries[0][1]
    catalog = _runtime_catalog(env)
    competing = _competitor(catalog, reference_path="references/node-exporter/memory.md")
    assert _entry(competing, "node_cpu_seconds_total") == _entry(catalog, "node_cpu_seconds_total")
    _publish(env, monkeypatch, competing)
    assert env.cache.snapshot()["entries"] == 1, "a catalog republish does not clear the plan cache by itself"
    eid = next(iter(env.cache._entries))
    env.run.contract_for = lambda q: cpu_contract('rate(node_cpu_seconds_total{mode="user"}[5m])')

    res = env.ask(Q_CPU)                                           # the EXACT same question
    info = env.info(res)
    assert info["hit"] is False and "catalog routing for the question changed" in info["invalidation_reason"]
    assert env.pipeline_calls == 2, "the pipeline (Router + Generator) must decide again"
    assert [q for _, q in env.prom.queries].count(stored_query) == 1, "the stale plan must not be executed again"
    assert eid not in env.cache._entries and env.cache.snapshot()["revalidation_failed"] == 1


def test_competing_entry_also_blocks_a_semantic_hit(env, monkeypatch):
    env.ask(Q_CPU)
    _publish(env, monkeypatch, _competitor(_runtime_catalog(env), reference_path="references/node-exporter/memory.md"))
    assert env.info(env.ask(Q_CPU_REWORDED))["hit"] is False and env.pipeline_calls == 2


def test_competing_entry_in_the_same_reference_is_also_a_routing_change(env, monkeypatch):
    env.ask(Q_CPU)
    _publish(env, monkeypatch, _competitor(_runtime_catalog(env)))
    assert "catalog routing" in env.info(env.ask(Q_CPU))["invalidation_reason"]


def test_after_a_routing_change_the_new_plan_is_stored_against_the_new_catalog(env, monkeypatch):
    env.ask(Q_CPU)
    _publish(env, monkeypatch, _competitor(_runtime_catalog(env)))
    assert env.info(env.ask(Q_CPU))["stored"] is True
    assert env.info(env.ask(Q_CPU))["hit"] is True and env.pipeline_calls == 2


def test_the_competing_entry_would_have_been_reused_without_the_routing_check(env, monkeypatch):
    """Guards the guard: the plan's own catalog entry, the skill text, the labels and the validator all
    still pass, so only the routing fingerprint stops the stale exact hit."""
    monkeypatch.setattr(ppc, "fingerprint_routing", lambda catalog, question: "constant")
    env.ask(Q_CPU)
    _publish(env, monkeypatch, _competitor(_runtime_catalog(env), reference_path="references/node-exporter/memory.md"))
    assert env.info(env.ask(Q_CPU))["hit"] is True and env.pipeline_calls == 1


def test_unrelated_catalog_addition_does_not_cause_needless_misses(env, monkeypatch):
    env.ask(Q_CPU)
    catalog = _runtime_catalog(env)
    unrelated = dataclasses.replace(_entry(catalog, "node_cpu_seconds_total"), name="zzqq_unrelated", exporter="zzqq",
                                    category="zzqq", keywords=("zzqq",), help="zzqq")
    _publish(env, monkeypatch, dataclasses.replace(catalog, metrics=catalog.metrics + (unrelated,)))
    assert env.info(env.ask(Q_CPU))["hit"] is True and env.pipeline_calls == 1


def test_routing_fingerprint_unit(real_catalog):
    base = ppc.fingerprint_routing(real_catalog, Q_CPU)
    assert base == ppc.fingerprint_routing(real_catalog, Q_CPU)
    assert base != ppc.fingerprint_routing(_competitor(real_catalog), Q_CPU)
    assert base != ppc.fingerprint_routing(None, Q_CPU)
    assert ppc.fingerprint_routing(None, Q_CPU) == ppc.fingerprint_routing(None, Q_MEM)


# ---- G. time-range consistency ----------------------------------------------------------------------------

def _tamper(env, **contract_over):
    eid, entry = next(iter(env.cache._entries.items()))
    env.cache._entries[eid] = dataclasses.replace(entry, contract_json=json_with(entry.contract_json, **contract_over))
    return eid


def test_tampered_time_range_is_rejected_even_though_it_is_independently_valid(env):
    env.ask(Q_CPU)
    eid = _tamper(env, time_range={"from": "now-6h", "to": "now", "step": "60s"})
    res = env.ask(Q_CPU)
    info = env.info(res)
    assert info["hit"] is False and "time range differs" in info["invalidation_reason"]
    assert env.pipeline_calls == 2 and eid not in env.cache._entries
    assert res["time_range"]["from"] == "now-1h", "the executed plan is the freshly generated one"


def test_the_tampered_range_really_would_have_been_accepted_without_the_new_check(env, monkeypatch):
    """Guards the guard: every other check passes for now-6h, so only the time-range check stops it."""
    env.ask(Q_CPU)
    _tamper(env, time_range={"from": "now-6h", "to": "now", "step": "60s"})
    monkeypatch.setattr(ppc, "check_consistency", lambda entry: None)
    res = env.ask(Q_CPU)
    assert env.info(res)["hit"] is True and res["time_range"]["from"] == "now-6h"


@pytest.mark.parametrize("tr", [
    {"from": "now-1h", "to": "now-5m", "step": "60s"},        # end moved
    {"from": "now-1h", "to": "now", "step": "5m"},            # step changed
    {"from": "now-1h", "to": "now"},                          # step dropped
    {"from": "now-1h"},                                       # end dropped
    None,
])
def test_any_other_time_range_difference_is_rejected(env, tr):
    env.ask(Q_CPU)
    _tamper(env, time_range=tr)
    assert "time range differs" in env.info(env.ask(Q_CPU))["invalidation_reason"] and env.pipeline_calls == 2


def test_tampered_instant_time_is_rejected(env):
    env.ask(Q_LOAD)
    eid = _tamper(env, time_range={"time": "now-5m"})
    info = env.info(env.ask(Q_LOAD))
    assert info["hit"] is False and "time range differs" in info["invalidation_reason"]
    assert eid not in env.cache._entries and env.pipeline_calls == 2


def test_query_type_switch_between_range_and_instant_is_rejected(env):
    env.ask(Q_CPU)
    _tamper(env, query_type="instant", time_range={"time": "now"})
    assert env.info(env.ask(Q_CPU))["hit"] is False and env.pipeline_calls == 2


def test_time_expr_of_handles_both_shapes():
    assert ppc.time_expr_of(cpu_contract()) == ("range", "now-1h", "now", "60s")
    assert ppc.time_expr_of(instant_contract()) == ("instant", "now")
    assert ppc.time_expr_of({"query_type": "instant", "time": "now-1m"}) == ("instant", "now-1m")
    assert ppc.time_expr_of({"time_range": "bogus"}) == ("range", None, None, None)


# ---- H. reference ownership is judged per reference ---------------------------------------------------------

def _owner_check(settings, catalog, question=Q_CPU_REWORDED):
    facts = ppc.PlanFacts(reference_used=REF_CPU, family="node-exporter", metrics=("node_cpu_seconds_total",),
                          query=CPU_QUERY_FOR_TESTS, query_type="range", window_seconds=3600.0,
                          time_expr=("range", "now-1h", "now", "60s"))
    return ppc.reference_ambiguity(question, SimpleNamespace(facts=facts), settings=settings, catalog=catalog)


CPU_QUERY_FOR_TESTS = cpu_contract()["query"]


def test_plan_metric_offered_only_by_another_reference_is_not_confirmed(settings, real_catalog):
    a = _entry(real_catalog, "node_cpu_seconds_total")
    moved = dataclasses.replace(a, reference_path="references/node-exporter/memory.md", priority="Review")
    boosted = dataclasses.replace(a, name="node_cpu_guest_seconds_total", priority="High")
    catalog = dataclasses.replace(real_catalog, metrics=tuple(moved if m.name == a.name else m
                                                              for m in real_catalog.metrics) + (boosted,))
    confident, refs, by_ref = ppc._top_references(catalog, Q_CPU_REWORDED, settings.catalog_narrow_min_score)
    assert confident and refs == {REF_CPU}, "cpu.md owns the question alone"
    assert "node_cpu_seconds_total" in set().union(*by_ref.values()), "the old flat check would have passed"
    assert "node_cpu_seconds_total" not in by_ref[REF_CPU]
    assert "no longer offers ['node_cpu_seconds_total']" in _owner_check(settings, catalog)


def test_catalog_schema_forbids_duplicate_metric_names_so_competing_references_is_the_real_case(real_catalog):
    """The schema allows exactly one entry per metric name, hence a metric can only ever be offered by ONE
    reference. If this ever relaxes, per-reference grouping in _top_references already handles it, but the
    duplicate-name scenario then needs its own test: this one is the tripwire."""
    from app.catalog.schema import CatalogSchemaError
    a = _entry(real_catalog, "node_cpu_seconds_total")
    dup = dataclasses.replace(a, reference_path="references/node-exporter/memory.md")
    with pytest.raises(CatalogSchemaError, match="Duplicate metric name"):
        dataclasses.replace(real_catalog, metrics=real_catalog.metrics + (dup,))


def test_ownership_is_confirmed_on_the_untouched_catalog(settings, real_catalog):
    assert _owner_check(settings, real_catalog) is None


def test_top_references_groups_metric_names_by_reference(settings, real_catalog):
    confident, refs, by_ref = ppc._top_references(real_catalog, Q_CPU, settings.catalog_narrow_min_score)
    assert confident and refs == {REF_CPU} and "node_cpu_seconds_total" in by_ref[REF_CPU]
    assert all(isinstance(v, set) and v for v in by_ref.values())
    assert ppc._top_references(real_catalog, "zzqq qqzz", settings.catalog_narrow_min_score) == (False, set(), {})


# ---- I. catalog snapshot between generation and insertion; fail-closed catalog handling ------------------------

def _publish_during_generation(env, monkeypatch, catalog, then=None):
    """A catalog refresh that lands WHILE the Router/Generator are generating the plan."""
    def generate(q):
        _publish(env, monkeypatch, catalog)
        return (then or cpu_contract)()
    env.run.contract_for = generate


def test_pipeline_runs_on_the_snapshot_captured_before_it_started(env):
    before = _runtime_catalog(env)
    env.ask(Q_CPU)
    assert len(env.pipeline_kwargs) == 1 and env.pipeline_kwargs[0]["catalog"] is before


def test_disabled_cache_calls_the_pipeline_exactly_as_before(monkeypatch, settings):
    e = Env(monkeypatch, settings, prometheus_query_plan_cache_enabled=False)
    e.ask(Q_CPU)
    assert "catalog" not in e.pipeline_kwargs[0] and set(e.pipeline_kwargs[0]) == {"context"}


def test_catalog_republished_between_generation_and_insertion_is_not_stored(env, monkeypatch):
    original = _runtime_catalog(env)
    competing = _competitor(original, reference_path="references/node-exporter/memory.md")
    _publish_during_generation(env, monkeypatch, competing)

    res = env.ask(Q_CPU)
    info = env.info(res)
    assert res["execution"]["execution_status"] == "success" and res["query"] == cpu_contract()["query"]
    assert info["hit"] is False and info["stored"] is False
    assert "routing for the question changed between generation and insertion" in info["not_stored_reason"]
    snap = env.cache.snapshot()
    assert snap["entries"] == 0 and snap["catalog_changed_not_stored"] == 1 and snap["not_stored"] == 1
    assert env.pipeline_kwargs[0]["catalog"] is original, "the pipeline ran on the snapshot taken before it"

    # the old plan is NOT in the cache, so it cannot be served under the new catalog; the next identical
    # question generates a plan under the NEW catalog and that one is stored and reused
    env.run.contract_for = lambda q: cpu_contract()
    again = env.ask(Q_CPU)
    assert env.info(again)["hit"] is False and env.info(again)["stored"] is True and env.pipeline_calls == 2
    entry = next(iter(env.cache._entries.values()))
    assert entry.fingerprints.routing == ppc.fingerprint_routing(competing, Q_CPU)
    assert env.info(env.ask(Q_CPU))["hit"] is True and env.pipeline_calls == 2


def test_plan_metric_entry_changed_between_generation_and_insertion_is_not_stored(env, monkeypatch):
    # `unit` is not a search field, so the routing view is unchanged and only the entry check can notice
    changed = _with_metric(_runtime_catalog(env), unit="changed_while_generating")
    _publish_during_generation(env, monkeypatch, changed)
    info = env.info(env.ask(Q_CPU))
    assert info["stored"] is False and "entry of a plan metric changed" in info["not_stored_reason"]
    assert env.cache.snapshot()["entries"] == 0


def test_republish_of_identical_catalog_content_does_not_block_storing(env, monkeypatch):
    _publish_during_generation(env, monkeypatch, dataclasses.replace(_runtime_catalog(env)))
    assert env.info(env.ask(Q_CPU))["stored"] is True


def test_unrelated_catalog_change_during_generation_does_not_block_storing(env, monkeypatch):
    catalog = _runtime_catalog(env)
    unrelated = dataclasses.replace(_entry(catalog, "node_cpu_seconds_total"), name="zzqq_unrelated", exporter="zzqq",
                                    category="zzqq", keywords=("zzqq",), help="zzqq")
    _publish_during_generation(env, monkeypatch, dataclasses.replace(catalog, metrics=catalog.metrics + (unrelated,)))
    assert env.info(env.ask(Q_CPU))["stored"] is True


def test_two_layers_a_plan_stored_despite_the_check_is_still_rejected_on_reuse(env, monkeypatch):
    """Guards the guard, and shows defence in depth: fingerprints are computed from the GENERATION snapshot,
    so even if the insertion-time comparison were bypassed the plan could not be reused under the new
    catalog."""
    competing = _competitor(_runtime_catalog(env), reference_path="references/node-exporter/memory.md")
    _publish_during_generation(env, monkeypatch, competing)
    monkeypatch.setattr(ppc, "catalog_changed_since", lambda *a, **k: None)
    assert env.info(env.ask(Q_CPU))["stored"] is True                    # only the new check kept it out
    env.run.contract_for = lambda q: cpu_contract()
    again = env.ask(Q_CPU)
    assert env.info(again)["hit"] is False and "catalog routing" in env.info(again)["invalidation_reason"]


def test_no_baseline_means_nothing_is_stored(env):
    out = ppc.store_validated_plan(Q_CPU, "u1", _ok_exec(), env.run.settings, env.skill)
    assert out.stored is False and "no catalog baseline" in out.reason
    assert env.cache.snapshot()["entries"] == 0


def test_catalog_unavailable_when_the_pipeline_starts_stores_nothing(env, monkeypatch):
    monkeypatch.setattr(pipeline, "_get_catalog", lambda s: None)
    res = env.ask(Q_CPU)
    info = env.info(res)
    assert res["execution"]["execution_status"] == "success"
    assert info["stored"] is False and "catalog unavailable when the pipeline started" in info["not_stored_reason"]
    assert "catalog" not in env.pipeline_kwargs[0], "no snapshot to pass: the pipeline resolves its own, as before"
    assert env.cache.snapshot()["entries"] == 0


def test_catalog_vanishing_between_generation_and_insertion_stores_nothing(env, monkeypatch):
    def generate(q):
        monkeypatch.setattr(pipeline, "_get_catalog", lambda s: None)
        return cpu_contract()
    env.run.contract_for = generate
    info = env.info(env.ask(Q_CPU))
    assert info["stored"] is False and "catalog unavailable at insertion" in info["not_stored_reason"]


def test_catalog_unavailable_blocks_an_exact_hit(env, monkeypatch):
    env.ask(Q_CPU)
    monkeypatch.setattr(pipeline, "_get_catalog", lambda s: None)
    res = env.ask(Q_CPU)
    assert env.info(res)["hit"] is False and "catalog unavailable" in env.info(res)["invalidation_reason"]
    assert env.pipeline_calls == 2


def test_catalog_unavailable_blocks_a_semantic_hit(env, monkeypatch):
    env.ask(Q_CPU)
    monkeypatch.setattr(pipeline, "_get_catalog", lambda s: None)
    assert env.info(env.ask(Q_CPU_REWORDED))["hit"] is False and env.pipeline_calls == 2


def test_status_validation_never_silently_skipped_without_a_catalog(monkeypatch, settings):
    e = Env(monkeypatch, settings, catalog_metric_status_validation_enabled=True)
    e.ask(Q_CPU)
    assert e.cache.snapshot()["entries"] == 1
    monkeypatch.setattr(pipeline, "_get_catalog", lambda s: None)
    res = e.ask(Q_CPU)
    assert e.info(res)["hit"] is False and "catalog unavailable" in e.info(res)["invalidation_reason"]
    assert e.pipeline_calls == 2


def test_validator_context_refuses_to_run_without_a_catalog_when_status_validation_is_active(settings):
    facts = None                                           # not used by validator_kwargs
    active = settings.model_copy(update={"catalog_metric_status_validation_enabled": True})
    with pytest.raises(ppc.CatalogUnavailable):
        ppc.validator_kwargs(facts, settings=active, catalog=None, known_metrics=set(), known_references=set(),
                             details={})
    inactive = settings.model_copy(update={"catalog_metric_status_validation_enabled": False,
                                           "catalog_runtime_reconciliation_enabled": False})
    assert ppc.validator_kwargs(facts, settings=inactive, catalog=None, known_metrics=set(), known_references=set(),
                                details={})["catalog_status_by_metric"] is None


def test_compute_fingerprints_refuses_a_missing_catalog(env):
    facts = ppc.assess_executed(Q_CPU, _ok_exec()).facts
    prints, why = ppc.compute_fingerprints(facts, cpu_contract(), settings=env.run.settings, skill_index=env.skill,
                                           catalog=None, question=Q_CPU)
    assert prints is None and "catalog unavailable" in why


def test_catalog_changed_since_unit(real_catalog):
    assert ppc.catalog_changed_since(real_catalog, real_catalog, Q_CPU, ("node_cpu_seconds_total",)) is None
    assert ppc.catalog_changed_since(dataclasses.replace(real_catalog), real_catalog, Q_CPU,
                                     ("node_cpu_seconds_total",)) is None
    assert "unavailable when the pipeline started" in ppc.catalog_changed_since(None, real_catalog, Q_CPU, ())
    assert "unavailable at insertion" in ppc.catalog_changed_since(real_catalog, None, Q_CPU, ())
    assert "routing" in ppc.catalog_changed_since(real_catalog, _competitor(real_catalog), Q_CPU,
                                                  ("node_cpu_seconds_total",))
    assert "plan metric" in ppc.catalog_changed_since(real_catalog, _with_metric(real_catalog, unit="x"), Q_CPU,
                                                      ("node_cpu_seconds_total",))
    # a help-text edit moves the question's catalog score, so it is (correctly) caught as a routing change
    assert "routing" in ppc.catalog_changed_since(real_catalog, _with_metric(real_catalog, help="x"), Q_CPU,
                                                  ("node_cpu_seconds_total",))
