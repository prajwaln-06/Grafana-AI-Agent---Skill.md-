"""Built-in Prometheus concept vocabulary (app/prometheus_concepts.py) and its use by the Prometheus
query-plan cache.

Part 1  vocabulary + signature level   (pure, fast; every pair is a complete, realistic question)
Part 2  isolation                      (the OpenSearch matcher/caches are untouched by Prometheus words)
Part 3  the cache end to end           (real agent pipeline, real catalog, real validator; only the
                                        Router/Generator stub and Prometheus' HTTP layer are faked)

The point of the whole feature is that a synonym may only make two questions ELIGIBLE to match. Every test
in Part 3 that expects a hit also checks that the plan was re-validated and Prometheus queried again, and
every negative test checks that a safeguard other than the vocabulary is what said no.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import label_discovery, pipeline, prometheus_concepts as pc, prometheus_plan_cache as ppc
from app.catalog.loader import load_catalog
from app.catalog.rules import apply_rules
from app.opensearch_catalog import plan_cache as os_plan, query_cache as qc
from tests.prom_plan_fixtures import REAL_CATALOG, REF_CPU, REF_MEM, cpu_contract, instant_contract, mem_contract
from tests.test_prometheus_plan_cache import Env, Q_CPU, _with_metric

REPO = Path(__file__).resolve().parent.parent


def eq(a: str, b: str, aliases=()) -> bool:
    return qc.equivalent(pc.signature(a, aliases), pc.signature(b, aliases))


def sig(q: str):
    return pc.signature(q)


# =====================================================================================================
# Part 1. vocabulary + signature level
# =====================================================================================================

POSITIVE = [
    # (question A, question B): the same measurement, worded differently, same scope and window
    ("Show CPU usage for all nodes.", "Show CPU utilization for all nodes."),
    ("Display processor utilization across all nodes.", "Show CPU utilization for all nodes."),
    ("Show processor usage for all nodes.", "Show CPU utilization for all nodes."),
    ("Show CPU usage for all nodes.", "Display processor utilization across all nodes."),
    ("Show memory consumption across all nodes.", "Show memory usage across all nodes."),
    ("Show RAM utilization across all nodes.", "Show memory usage across all nodes."),
    ("Show RAM usage across all nodes.", "Show memory utilization across all nodes."),
    ("Show RAM consumption across all nodes.", "Show memory utilization across all nodes."),
    ("Show network traffic for all nodes.", "Show network throughput for all nodes."),
    ("Show disk read rate for all nodes.", "Show disk read throughput for all nodes."),
    ("Show disk write rate for all nodes.", "Show disk write throughput for all nodes."),
    ("Show GPU usage for all nodes.", "Show GPU utilization for all nodes."),
    ("Show GPU memory usage for all nodes.", "Show GPU memory consumption for all nodes."),
    ("Show power draw for all nodes.", "Show power consumption for all nodes."),
    ("Show CPU load average for all nodes.", "Show system load for all nodes."),
    ("Show cpu usage for all nodes.", "Show CPU usage for all nodes."),                   # acronym case
    ("What are the CPU usages across all nodes?", "Show CPU utilization for all nodes."),  # plural, phrasing
    ("Show CPU usage for all nodes over the last 1 hour.",
     "Display processor utilization across all nodes over the past 1 hour."),               # same window
    ("Show the CPU utilisation for all nodes.", "Show CPU usage for all nodes."),          # British spelling
]


@pytest.mark.parametrize("a,b", POSITIVE)
def test_reworded_questions_with_the_same_measurement_are_equivalent(a, b):
    assert eq(a, b), (sig(a), sig(b))
    assert eq(b, a)


@pytest.mark.parametrize("a,b", POSITIVE)
def test_the_equivalence_is_made_of_a_shared_concept_not_of_a_missing_one(a, b):
    ca, cb = sig(a).concepts, sig(b).concepts
    assert ca == cb and ca, "both wordings must carry the same, non-empty canonical concept set"
    assert ca <= set(pc.BY_NAME)
    assert sig(a).residual == sig(b).residual, "nothing but the concept differs between the two wordings"


@pytest.mark.parametrize("phrase,concept", [
    ("cpu usage", "cpu_utilization"), ("cpu utilization", "cpu_utilization"),
    ("processor usage", "cpu_utilization"), ("processor utilization", "cpu_utilization"),
    ("memory usage", "memory_utilization"), ("memory utilization", "memory_utilization"),
    ("memory consumption", "memory_utilization"), ("ram usage", "memory_utilization"),
    ("ram consumption", "memory_utilization"), ("network traffic", "network_traffic"),
    ("network throughput", "network_traffic"), ("disk read rate", "disk_read_throughput"),
    ("disk read throughput", "disk_read_throughput"), ("cpu load", "cpu_load"),
    ("load average", "cpu_load"), ("gpu memory usage", "gpu_memory_utilization"),
    ("power consumption", "power_usage"),
])
def test_each_phrase_normalises_to_its_canonical_concept(phrase, concept):
    s = sig(f"Show {phrase} for all nodes.")
    assert s.concepts == {concept}
    assert s.understood


NEGATIVE = [
    ("Show CPU load for all nodes.", "Show CPU utilization for all nodes."),                 # load != utilization
    ("Show load average for all nodes.", "Show CPU usage for all nodes."),
    ("Show memory usage for all nodes.", "Show CPU usage for all nodes."),                   # other resource
    ("Show network traffic for all nodes.", "Show disk read throughput for all nodes."),     # network vs disk
    ("Show CPU utilization for all nodes over the last 1 hour.",
     "Show CPU utilization for all nodes over the last 6 hours."),                            # window
    ("Show CPU usage for all nodes over the last 1 hour.",
     "Show processor utilization for all nodes over the last 6 hours."),                      # window, reworded
    ("Show CPU utilization for node-01.", "Show CPU utilization for node-02."),              # node filter
    ("Show CPU utilization for node-01.", "Show CPU utilization for all nodes."),            # filter vs none
    ("Show CPU utilization for env=prod.", "Show CPU utilization for env=dev."),             # env filter
    ("Show CPU utilization for env PROD.", "Show CPU utilization for env prod."),            # filter case kept
    ("Show CPU utilization for all nodes.", "Show average CPU utilization for all nodes."),  # aggregation
    ("Show CPU utilization for all nodes.", "Show maximum CPU utilization for all nodes."),
    ("Show CPU utilization for all nodes.", "Count the nodes with CPU utilization."),        # operation
    ("Show CPU utilization for all nodes.", "Why is CPU utilization high on all nodes?"),    # operation
    ("Show memory usage for all nodes.", "Show GPU memory usage for all nodes."),            # host vs device
    ("Show disk read throughput for all nodes.", "Show disk write throughput for all nodes."),
    ("Show memory usage for all nodes.", "Show memory available for all nodes."),
    ("Show network traffic for all nodes.", "Show network receive traffic for all nodes."),   # direction
    ("Show network traffic for all nodes.", "Show network utilization for all nodes."),
    ("Show CPU usage for all nodes.", "Show usage for all nodes."),                          # bare word
    ("Show CPU usage for all nodes.", "Show CPU usage per core for all nodes."),             # grouping
    ("Show CPU usage for all nodes.", "Show CPU usage for all nodes in the idle mode."),     # extra filter
    ("Show power usage for all nodes.", "Show CPU usage for all nodes."),
    ("Show GPU usage for all nodes.", "Show CPU usage for all nodes."),
]


@pytest.mark.parametrize("a,b", NEGATIVE)
def test_materially_different_questions_are_never_equivalent(a, b):
    assert not eq(a, b), (sig(a), sig(b))
    assert not eq(b, a)


@pytest.mark.parametrize("a,b,why", pc.DISTINCT_ON_PURPOSE)
def test_the_documented_deliberate_distinctions_hold(a, b, why):
    assert why
    assert not eq(f"Show {a} for all nodes.", f"Show {b} for all nodes.")


def test_a_bare_broad_word_maps_to_no_concept_and_stays_a_difference():
    for word in ("usage", "utilization", "load", "consumption", "traffic", "throughput", "rate"):
        s = sig(f"Show {word} for all nodes.")
        assert not (s.concepts & set(pc.BY_NAME)), word
        assert qc._stem(word) in s.residual, word
    assert not eq("Show usage for all nodes.", "Show utilization for all nodes.")
    assert not eq("Show load for all nodes.", "Show utilization for all nodes.")


def test_unknown_words_stay_in_the_residual_and_block_equivalence():
    a = sig("Show CPU usage for all nodes.")
    b = sig("Show CPU usage for all nodes frobnicated.")
    assert b.residual - a.residual, "the unexplained word is kept, not dropped"
    assert not qc.equivalent(a, b)


def test_a_question_with_two_recognised_concepts_carries_both_and_is_order_insensitive():
    both = sig("Show CPU usage memory usage for all nodes.")
    assert both.concepts == {"cpu_utilization", "memory_utilization"}
    assert eq("Show CPU usage memory usage for all nodes.", "Show memory utilization processor usage for all nodes.")
    assert not eq("Show CPU usage memory usage for all nodes.", "Show CPU usage for all nodes.")
    assert not eq("Show CPU usage memory usage for all nodes.", "Show memory usage for all nodes.")


def test_a_compound_question_is_still_not_understood_so_it_never_matches_semantically():
    s = sig("Show CPU usage and memory usage for all nodes.")
    assert not s.understood and "compound question" in s.reasons
    assert not eq("Show CPU usage and memory usage for all nodes.", "Show CPU usage and memory usage for all nodes.")


def test_the_longest_phrase_wins_so_gpu_memory_is_not_split_into_gpu_plus_host_memory():
    s = sig("Show GPU memory usage for all nodes.")
    assert s.concepts == {"gpu_memory_utilization"} and "gpu" not in s.residual and "memory" not in s.residual
    c = sig("Show CPU load average for all nodes.")
    assert c.concepts == {"cpu_load"} and "average" not in c.residual


def test_time_literal_and_operation_facets_still_participate():
    a = sig("Show CPU usage for all nodes over the last 1 hour.")
    b = sig("Show processor utilization for all nodes over the past 6 hours.")
    assert a.concepts == b.concepts and a.window_seconds == 3600 and b.window_seconds == 21600
    c = sig("Count the nodes with processor usage.")
    assert c.operation == "count" and sig("Show CPU usage for all nodes.").operation == "list"
    assert sig("Show CPU usage for node-01.").literals == {"node-01"}


def test_domain_acronyms_fold_but_other_all_caps_tokens_keep_their_case():
    assert sig("Show CPU usage.").literals == frozenset() and sig("Show RAM usage.").literals == frozenset()
    assert sig("Show GPU usage.").literals == frozenset()
    assert sig("Show CPU usage for env PROD.").literals == {"PROD"}
    assert sig("Show CPU usage for rack A7.").literals == {"A7"}


def test_the_level_1_key_does_not_depend_on_the_vocabulary():
    for q in ("Show CPU usage for all nodes.", "Show RAM utilization for env PROD.", "cpu load, please"):
        assert pc.signature(q).canonical_text == qc.build_signature(q).canonical_text
        assert pc.signature(q, enabled=False).canonical_text == qc.build_signature(q).canonical_text


def test_disabled_vocabulary_is_exactly_the_shared_matcher():
    for q, _ in POSITIVE:
        assert pc.signature(q, enabled=False) == qc.build_signature(q)
    assert not eq_off("Show CPU usage for all nodes.", "Show CPU utilization for all nodes.")
    assert not eq_off("Show processor usage for all nodes.", "Show CPU usage for all nodes.")


def eq_off(a, b):
    return qc.equivalent(pc.signature(a, enabled=False), pc.signature(b, enabled=False))


# ---- aliases: still supported, applied first -------------------------------------------------------------

def test_declared_aliases_still_bridge_words_the_vocabulary_does_not_know():
    # "cores" is not a built-in synonym of anything; the operator declares it
    assert not eq("Show CPU usage for all nodes.", "Show CPU utilization for all nodes in this stack.")
    assert eq("Show proc usage for all nodes.", "Show CPU usage for all nodes.", aliases={"proc": "cpu"})
    assert not eq("Show proc usage for all nodes.", "Show CPU usage for all nodes.")


def test_an_alias_is_applied_before_phrase_matching_so_it_overrides_a_builtin_distinction():
    # without the alias load and utilization are different concepts; the operator declares them the same
    assert not eq("Show CPU load for all nodes.", "Show CPU utilization for all nodes.")
    assert eq("Show CPU load for all nodes.", "Show CPU utilization for all nodes.", aliases={"load": "utilization"})


def test_the_legacy_usage_to_utilization_alias_is_harmless_with_the_vocabulary():
    al = {"usage": "utilization"}
    for a, b in POSITIVE:
        assert eq(a, b, aliases=al) == eq(a, b), (a, b)
    assert not eq("Show memory usage for all nodes.", "Show CPU usage for all nodes.", aliases=al)


# ---- vocabulary integrity ---------------------------------------------------------------------------------

_RESERVED = (qc._TIME_STEMS | qc._COMPOUND_STEMS | qc._LIST_STEMS | qc._EXPLAIN_STEMS | qc._WHEN_STEMS
             | qc._ENTITY_STEMS | qc._FILLER_STEMS | qc._AUX_STEMS)


def test_no_phrase_collides_with_the_builtin_opensearch_lexicon():
    assert not (set(pc._TABLE) & set(qc._PHRASES))


def test_no_phrase_swallows_a_word_the_matcher_treats_specially():
    for key in pc._TABLE:
        assert not (set(key) & _RESERVED), key


def test_every_phrase_is_fully_consumed_into_exactly_its_own_concept():
    for c in pc.CONCEPTS:
        for ph in c.phrases:
            s = pc.signature(ph)
            assert s.concepts == {c.name} and not s.residual and not s.literals, (c.name, ph, s)


def test_each_concept_is_well_formed():
    names = [c.name for c in pc.CONCEPTS]
    assert len(names) == len(set(names)) and set(names) == set(pc.BY_NAME)
    for c in pc.CONCEPTS:
        assert c.phrases and c.metric_hints and c.canonical and c.note
        assert pc.signature(c.canonical).concepts == {c.name}, "the canonical wording names its own concept"


def test_a_phrase_that_names_two_concepts_is_refused_at_build_time(monkeypatch):
    bad = pc.CONCEPTS + (pc.Concept("dup", ("cpu usage",), ("x",), "cpu usage"),)
    monkeypatch.setattr(pc, "CONCEPTS", bad)
    with pytest.raises(ValueError, match="more than one concept"):
        pc._build()


def test_vocabulary_phrase_tables_are_hashable_so_signatures_stay_cacheable():
    hash(pc.VOCABULARY)
    assert pc.signature("Show CPU usage for all nodes.") is pc.signature("Show CPU usage for all nodes.")


# ---- the metric check -------------------------------------------------------------------------------------

BACKED = [
    ("cpu_utilization", "node_cpu_seconds_total"), ("cpu_load", "node_load1"),
    ("memory_utilization", "node_memory_MemAvailable_bytes"), ("network_traffic", "node_network_receive_bytes_total"),
    ("disk_read_throughput", "node_disk_read_bytes_total"), ("disk_write_throughput", "node_disk_written_bytes_total"),
    ("gpu_utilization", "DCGM_FI_DEV_GPU_UTIL"), ("gpu_memory_utilization", "DCGM_FI_DEV_FB_USED"),
    ("power_usage", "DCGM_FI_DEV_POWER_USAGE"),
]


@pytest.mark.parametrize("concept,metric", BACKED)
def test_a_concept_is_backed_by_a_matching_metric(concept, metric):
    s = SimpleNamespace(concepts=frozenset({concept}))
    assert pc.unsupported_concept(s, (metric,)) is None


NOT_BACKED = [          # a hint is a plausibility check, not a proof: these are the clear mismatches
    ("cpu_utilization", "node_memory_MemAvailable_bytes"), ("cpu_utilization", "node_load1"),
    ("memory_utilization", "node_cpu_seconds_total"), ("memory_utilization", "DCGM_FI_DEV_GPU_UTIL"),
    ("network_traffic", "node_cpu_seconds_total"), ("disk_read_throughput", "node_disk_written_bytes_total"),
    ("disk_write_throughput", "node_disk_read_bytes_total"), ("cpu_load", "node_cpu_seconds_total"),
    ("power_usage", "node_memory_MemAvailable_bytes"), ("gpu_utilization", "node_cpu_seconds_total"),
    ("gpu_memory_utilization", "node_memory_MemAvailable_bytes"),
]


@pytest.mark.parametrize("concept,metric", NOT_BACKED)
def test_a_concept_is_not_backed_by_a_metric_of_another_concept(concept, metric):
    s = SimpleNamespace(concepts=frozenset({concept}))
    assert pc.unsupported_concept(s, (metric,)), (concept, metric)


def test_every_concept_in_the_question_must_be_backed():
    s = sig("Show CPU usage memory usage for all nodes.")
    assert pc.unsupported_concept(s, ("node_cpu_seconds_total",)) is not None
    assert pc.unsupported_concept(s, ("node_cpu_seconds_total", "node_memory_MemTotal_bytes")) is None


def test_a_question_without_concepts_has_nothing_to_back():
    assert pc.unsupported_concept(sig("Show something odd for all nodes."), ("node_load1",)) is None


# ---- ownership text -------------------------------------------------------------------------------------

def test_ownership_text_adds_the_canonical_wording_only_when_a_concept_is_recognised():
    q = "Display processor utilization across all nodes."
    assert pc.ownership_text(q) == q + " cpu utilization"
    assert pc.ownership_text(q, enabled=False) == q
    other = "Show node uptime for all nodes."
    assert pc.ownership_text(other) == other
    two = pc.ownership_text("Show CPU usage memory usage for all nodes.")
    assert two.endswith("cpu utilization memory utilization")


# =====================================================================================================
# Part 2. isolation: the OpenSearch matcher and caches do not see the Prometheus vocabulary
# =====================================================================================================

def test_the_shared_matcher_without_a_vocabulary_is_unchanged_for_prometheus_words():
    s = qc.build_signature("Show CPU usage for all nodes.")
    assert s.concepts == frozenset() and s.literals == {"CPU"} and "usage" in s.residual
    assert qc.build_signature("Show CPU usage.") == qc.build_signature("Show CPU usage.", (), None)
    assert not qc.equivalent(qc.build_signature("Show CPU usage for all nodes."),
                             qc.build_signature("Show CPU utilization for all nodes."))
    assert qc.build_signature("Show processor usage.").concepts == frozenset()


def test_opensearch_vocabulary_behaviour_is_unchanged():
    q = "Which switches have stopped sending heartbeat signals right now?"
    plain = qc.build_signature(q)
    assert plain.concepts >= {"ABSENT", "SIGNAL", "NOW"}
    assert pc.signature(q) == plain, "adding the Prometheus vocabulary does not alter an OpenSearch question"


def test_the_builtin_lexicon_always_wins_a_collision():
    v = qc.Vocabulary(phrases=(((qc._stem("down"),), ("PROM_OVERRIDE",)),))
    assert qc.build_signature("Which switches are down?", (), v).concepts == {"DOWN"}


def test_a_vocabulary_alone_never_changes_an_unrelated_question():
    q = "How many switches are down right now?"
    assert qc.build_signature(q, (), pc.VOCABULARY) == qc.build_signature(q)


def test_opensearch_plan_cache_equivalence_does_not_use_the_prometheus_vocabulary():
    cache = os_plan.QueryPlanCache(semantic_enabled=True)
    entry = SimpleNamespace(question="Show CPU utilization for all nodes.", pattern_id="x")
    assert cache._signatures_equal("Show CPU utilization for all nodes!", entry) is True
    assert cache._signatures_equal("Show processor utilization for all nodes.", entry) is False
    assert cache._signatures_equal("Show CPU usage for all nodes.", entry) is False


def test_opensearch_modules_do_not_import_the_prometheus_vocabulary():
    for rel in ("app/opensearch_catalog/query_cache.py", "app/opensearch_catalog/plan_cache.py",
                "app/opensearch_catalog/runtime.py"):
        assert "prometheus_concepts" not in (REPO / rel).read_text(encoding="utf-8"), rel
    out = subprocess.run([sys.executable, "-c",
                          "import sys, app.opensearch_catalog.plan_cache, app.opensearch_catalog.query_cache;"
                          "print('app.prometheus_concepts' in sys.modules)"],
                         cwd=REPO, capture_output=True, text=True, check=True).stdout.strip()
    assert out == "False"


# =====================================================================================================
# Part 3. the Prometheus plan cache, end to end
# =====================================================================================================

Q_MEM_STORED = "Show memory usage across all nodes."


@pytest.fixture()
def e(monkeypatch, settings):
    """No declared aliases at all: every equivalence below comes from the built-in vocabulary."""
    return Env(monkeypatch, settings, prometheus_query_plan_cache_aliases={})


def _semantic_hit(env, res):
    info = env.info(res)
    assert info["hit"] is True and info["match"] == "semantic" and info["revalidated"] is True, info
    return info


@pytest.mark.parametrize("reworded", [
    "Show CPU usage for all nodes.",
    "What is the CPU usage across all nodes?",
    "Display processor utilization across all nodes.",
    "Show processor usage for all nodes.",
    "Show cpu utilisation for all nodes.",
])
def test_cpu_synonyms_reuse_the_cpu_plan_without_any_alias(e, reworded):
    first = e.ask(Q_CPU)
    assert e.info(first)["stored"] is True
    e.prom.value = "77.0"
    res = e.ask(reworded)
    _semantic_hit(e, res)
    assert e.pipeline_calls == 1, "no Router/Generator call on a plan-cache hit"
    assert e.prom.data_queries == 2, "Prometheus is executed AGAIN on a synonym hit"
    assert res["execution"]["series"][0]["points"][0]["value"] == 77.0, "fresh data, never a cached result"
    assert res["query"] == first["query"]
    assert e.cache.snapshot()["hits_semantic"] == 1 and e.cache.snapshot()["concepts_enabled"] is True


@pytest.mark.parametrize("reworded", [
    "Show memory consumption across all nodes.",
    "Show RAM utilization across all nodes.",
    "Show RAM usage across all nodes.",
    "Show memory utilization across all nodes.",
])
def test_memory_synonyms_reuse_the_memory_plan_without_any_alias(e, reworded):
    e.ask(Q_MEM_STORED)
    assert e.pipeline_calls == 1
    res = e.ask(reworded)
    _semantic_hit(e, res)
    assert e.pipeline_calls == 1 and e.prom.data_queries == 2
    assert "node_memory_MemAvailable_bytes" in res["query"]


def test_a_synonym_hit_keeps_the_stored_time_window_and_matches_only_the_same_window(e):
    e.run.contract_for = lambda q: cpu_contract()                 # a 1 h window
    e.ask("Show CPU usage for all nodes over the last 1 hour.")
    hit = e.ask("Display processor utilization across all nodes over the past 1 hour.")
    _semantic_hit(e, hit)
    assert hit["time_range"]["from"] == "now-1h"
    miss = e.ask("Display processor utilization across all nodes over the last 6 hours.")
    assert e.info(miss)["hit"] is False and e.pipeline_calls == 2


@pytest.mark.parametrize("other", [
    "Show memory utilization for all nodes.",                       # other resource: memory plan for a CPU question
    "Show RAM usage for all nodes.",
    "Show CPU load average for all nodes.",                        # load is not utilization
    "Show system load for all nodes.",
    "Show processor utilization for node-01.",                     # scope
    "Show processor utilization per core for all nodes.",          # grouping
    "Show average processor utilization for all nodes.",           # aggregation
    "Show processor utilization for all nodes over the last 6 hours.",
    "Count the nodes by processor utilization.",                   # operation
    "Show processor utilization for all nodes frobnicated.",       # unknown word
])
def test_a_synonym_never_reuses_a_plan_for_a_different_question(e, other):
    e.ask(Q_CPU)
    res = e.ask(other)
    assert e.info(res)["hit"] is False and e.pipeline_calls == 2, other
    assert e.cache.snapshot().get("hits_semantic", 0) == 0


def test_a_cpu_synonym_never_reuses_a_memory_plan_and_vice_versa(e):
    e.ask(Q_MEM_STORED)
    assert e.info(e.ask("Show processor usage for all nodes."))["hit"] is False
    e2_calls = e.pipeline_calls
    assert e2_calls == 2
    assert e.info(e.ask("Show RAM utilization across all nodes."))["hit"] is True      # its own plan still hits
    assert e.pipeline_calls == e2_calls, "the CPU plan stored by the miss is not offered to the RAM question"


def test_load_and_utilization_plans_stay_separate_in_the_same_cache(e):
    e.ask(Q_CPU)
    e.run.contract_for = lambda q: instant_contract()
    e.ask("Show CPU load average for all nodes.")
    assert e.pipeline_calls == 2 and len(e.cache) == 2
    h1 = e.ask("Show processor usage for all nodes.")
    h2 = e.ask("Show system load for all nodes.")
    assert _semantic_hit(e, h1) and _semantic_hit(e, h2)
    assert "node_load1" in h2["query"] and "node_cpu_seconds_total" in h1["query"]
    assert e.pipeline_calls == 2


def test_hits_are_still_scoped_per_user(e):
    e.ask(Q_CPU)
    res, _ = e.run.ask("Show processor usage for all nodes.", user="someone-else")
    assert e.info(res)["hit"] is False and e.pipeline_calls == 2


def test_the_cache_stores_the_plan_only_never_prometheus_data(e):
    e.ask(Q_CPU)
    entry = next(iter(e.cache._entries.values()))
    assert '"execution"' not in entry.contract_json and "12.5" not in entry.contract_json
    e.ask("Show processor usage for all nodes.")
    assert '"execution"' not in next(iter(e.cache._entries.values())).contract_json


# ---- the switches --------------------------------------------------------------------------------------

def test_semantic_matching_disabled_leaves_synonyms_unmatched_but_exact_hits_working(monkeypatch, settings):
    env = Env(monkeypatch, settings, prometheus_query_plan_cache_aliases={},
              prometheus_query_plan_cache_semantic_enabled=False)
    env.ask(Q_CPU)
    assert env.info(env.ask("Show processor usage for all nodes."))["hit"] is False
    assert env.info(env.ask(Q_CPU))["match"] == "exact"


def test_the_vocabulary_switch_off_restores_alias_only_behaviour(monkeypatch, settings):
    env = Env(monkeypatch, settings, prometheus_query_plan_cache_aliases={},
              prometheus_query_plan_cache_concepts_enabled=False)
    env.ask(Q_CPU)
    assert env.cache.snapshot()["concepts_enabled"] is False
    assert env.info(env.ask("Show CPU usage for all nodes."))["hit"] is False
    assert env.info(env.ask("Show processor utilization for all nodes."))["hit"] is False
    assert env.info(env.ask(Q_CPU))["match"] == "exact"


def test_the_vocabulary_switch_is_part_of_the_cache_identity(monkeypatch, settings):
    on = Env(monkeypatch, settings)
    a = ppc.get_cache(on.settings)
    b = ppc.get_cache(on.settings.model_copy(update={"prometheus_query_plan_cache_concepts_enabled": False}))
    assert a is not b and a.concepts_enabled and not b.concepts_enabled


def test_the_vocabulary_is_on_by_default(settings):
    from app.config import Settings
    assert Settings(gemini_api_key="k", skills_root=settings.skills_root).prometheus_query_plan_cache_concepts_enabled


def test_a_declared_alias_still_works_with_the_vocabulary_on(monkeypatch, settings):
    env = Env(monkeypatch, settings, prometheus_query_plan_cache_aliases={"node-exporter": {"proc": "cpu"}})
    env.ask(Q_CPU)
    _semantic_hit(env, env.ask("Show proc usage for all nodes."))          # alias + built-in phrase
    _semantic_hit(env, env.ask("Show processor usage for all nodes."))     # built-in alone


def test_the_legacy_alias_configuration_keeps_working_unchanged(monkeypatch, settings):
    env = Env(monkeypatch, settings)                                       # ALIASES = usage -> utilization
    env.ask(Q_CPU)
    _semantic_hit(env, env.ask("What is the CPU usage across all nodes?"))
    assert env.info(env.ask("Show memory utilization for all nodes."))["hit"] is False


def test_an_alias_declared_for_another_family_does_not_leak(monkeypatch, settings):
    env = Env(monkeypatch, settings, prometheus_query_plan_cache_aliases={"dcgm-exporter": {"load": "utilization"}})
    env.run.contract_for = lambda q: cpu_contract()
    env.ask(Q_CPU)
    assert env.info(env.ask("Show CPU load for all nodes."))["hit"] is False


def test_an_alias_overrides_a_builtin_distinction_only_for_its_own_family(monkeypatch, settings):
    env = Env(monkeypatch, settings, prometheus_query_plan_cache_aliases={"node-exporter": {"load": "utilization"}})
    env.ask(Q_CPU)
    res = env.ask("Show CPU load for all nodes.")
    assert env.info(res)["hit"] is True, "the operator declared load == utilization for node-exporter"


# ---- the existing safeguards still decide -----------------------------------------------------------------

def test_catalog_ownership_rejection_still_blocks_a_synonym_hit(e, monkeypatch):
    e.ask(Q_CPU)
    top = ({"references/node-exporter/cpu.md", "references/node-exporter/memory.md"},
           {REF_CPU: {"node_cpu_seconds_total"}})
    monkeypatch.setattr(ppc, "_top_references", lambda *a, **k: (True, *top))
    res = e.ask("Show processor usage for all nodes.")
    assert e.info(res)["hit"] is False and e.pipeline_calls == 2
    assert e.cache.snapshot().get("pattern_ambiguous") == 1


def test_the_ownership_check_must_still_offer_the_plans_metrics_for_a_synonym(e, monkeypatch):
    e.ask(Q_CPU)
    monkeypatch.setattr(ppc, "_top_references", lambda *a, **k: (True, {REF_CPU}, {REF_CPU: {"node_load1"}}))
    assert e.info(e.ask("Show processor usage for all nodes."))["hit"] is False
    assert e.cache.snapshot().get("pattern_ambiguous") == 1


def test_an_unconfident_catalog_blocks_a_synonym_hit(e, monkeypatch):
    e.ask(Q_CPU)
    monkeypatch.setattr(ppc, "_top_references", lambda *a, **k: (False, set(), {}))
    assert e.info(e.ask("Show processor usage for all nodes."))["hit"] is False


def test_the_ownership_check_is_the_real_one_and_would_reject_the_raw_synonym_wording(e):
    """Why `ownership_text` exists, and proof the check itself was not loosened: asked about the raw
    wording, the unchanged `reference_ambiguity` sends 'processor' to the GPU reference and refuses."""
    e.ask(Q_CPU)
    entry = next(iter(e.cache._entries.values()))
    cat = ppc.pipeline._get_catalog(e.run.settings)
    raw = "Display processor utilization across all nodes."
    assert ppc.reference_ambiguity(raw, entry, settings=e.run.settings, catalog=cat) is not None
    aug = pc.ownership_text(raw)
    assert ppc.reference_ambiguity(aug, entry, settings=e.run.settings, catalog=cat) is None
    # and the augmented text never rescues a plan from the wrong reference
    mem_entry_cpu_question = pc.ownership_text("Show processor usage for all nodes.")
    mem_entry = SimpleNamespace(facts=SimpleNamespace(reference_used=REF_MEM, metrics=("node_memory_MemAvailable_bytes",)))
    assert ppc.reference_ambiguity(mem_entry_cpu_question, mem_entry, settings=e.run.settings, catalog=cat)


def test_a_plan_whose_metrics_do_not_back_the_question_is_exact_only(e):
    # the question says CPU but the plan that actually ran is a memory plan
    e.run.contract_for = lambda q: mem_contract()
    e.ask(Q_CPU)
    entry = next(iter(e.cache._entries.values()))
    assert entry.semantic_ok is False
    assert e.info(e.ask("Show processor usage for all nodes."))["hit"] is False
    assert e.info(e.ask(Q_CPU))["match"] == "exact"


def test_assess_executed_reports_why_an_unbacked_concept_blocks_semantic_reuse():
    executed = {**mem_contract(), "execution": {"execution_status": "success"}}
    a = ppc.assess_executed("Show CPU usage for all nodes.", executed)
    assert a.cacheable and a.semantic_ok is False and "cpu_utilization" in a.semantic_reason
    ok = ppc.assess_executed("Show memory usage for all nodes.", executed)
    assert ok.semantic_ok is True
    off = ppc.assess_executed("Show CPU usage for all nodes.", executed, concepts_enabled=False)
    assert off.semantic_ok is False and "literal 'CPU'" in off.semantic_reason, "previous behaviour, unchanged"


@pytest.mark.parametrize("enabled", [True, False])
def test_an_acronym_the_vocabulary_does_not_consume_must_still_appear_in_the_executed_query(enabled):
    """Turning CPU into a concept WORD must not drop the old rule that a CPU question needs a CPU query:
    'CPU temperature' is not a concept phrase, so 'cpu' stays a leftover word and is still checked."""
    executed = {**mem_contract(), "execution": {"execution_status": "success"}}
    a = ppc.assess_executed("Show CPU temperature for all nodes.", executed, concepts_enabled=enabled)
    assert a.cacheable and a.semantic_ok is False and "'CPU'" in a.semantic_reason
    cpu_exec = {**cpu_contract(), "execution": {"execution_status": "success"}}
    assert ppc.assess_executed("Show CPU temperature for all nodes.", cpu_exec,
                               concepts_enabled=enabled).semantic_ok is True


def test_revalidation_still_runs_on_a_synonym_hit(e, monkeypatch):
    e.ask(Q_CPU)
    seen = []
    real = ppc.revalidate_entry
    monkeypatch.setattr(ppc, "revalidate_entry", lambda entry, **kw: (seen.append(entry.entry_id), real(entry, **kw))[1])
    _semantic_hit(e, e.ask("Show processor usage for all nodes."))
    assert seen == [1]


def test_a_changed_catalog_entry_still_invalidates_a_synonym_hit(e, monkeypatch):
    e.ask(Q_CPU)
    changed = _with_metric(apply_rules(load_catalog(REAL_CATALOG)), help="Something else entirely")
    monkeypatch.setattr(pipeline, "_get_catalog", lambda s: changed)
    res = e.ask("Show processor usage for all nodes.")
    info = e.info(res)
    assert info["hit"] is False and "catalog entry for ['node_cpu_seconds_total'] changed" in info["invalidation_reason"]
    assert e.pipeline_calls == 2, "a rejected plan falls back to Router + Generator, whatever the wording"


def test_a_stale_label_value_is_still_caught_by_the_validator_on_a_synonym_hit(e):
    e.ask(Q_CPU)
    stored_query = e.prom.queries[0][1]
    e.prom.series["node_cpu_seconds_total"] = [
        s for s in e.prom.series["node_cpu_seconds_total"] if s["mode"] != "idle"]
    label_discovery.clear_cache()
    e.run.contract_for = lambda q: cpu_contract('rate(node_cpu_seconds_total{mode="user"}[5m])')
    res = e.ask("Show processor usage for all nodes.")
    info = e.info(res)
    assert info["hit"] is False and "validator now rejects" in info["invalidation_reason"]
    assert [q for _, q in e.prom.queries].count(stored_query) == 1, "the stale plan must not be executed"


def test_every_synonym_hit_is_counted_and_nothing_else_is_swallowed(e):
    e.ask(Q_CPU)
    for q in ("Show CPU usage for all nodes.", "Show processor usage for all nodes."):
        _semantic_hit(e, e.ask(q))
    snap = e.cache.snapshot()
    assert snap["hits_semantic"] == 2 and snap.get("errors", 0) == 0 and snap.get("match_errors", 0) == 0
    assert snap.get("revalidation_failed", 0) == 0


def test_the_opensearch_caches_are_untouched_by_a_prometheus_synonym_hit(e):
    from app.opensearch_catalog import runtime as os_runtime
    e.ask(Q_CPU)
    _semantic_hit(e, e.ask("Show processor usage for all nodes."))
    assert os_runtime.plan_cache_snapshot(e.run.settings).get("entries", 0) == 0
