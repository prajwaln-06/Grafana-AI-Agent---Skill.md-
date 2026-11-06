"""Word-form tolerance for free-text search (app/opensearch_catalog/text_forms.py and
the guard's canonicalisation of match/match_phrase).

Regression for: "What timestamps were recorded for iLO System Overheating events on
host X?" returned 8 rows, while "... System Overheated events on host X?" built
match_phrase "System Overheated" and returned an empty result, because the text
field is tokenised but not stemmed. Equivalent wordings must canonicalise to the
SAME query, and the change must be recorded, never silent."""
import copy
from unittest.mock import patch

import pytest

from app import executor, opensearch_client
from app.opensearch_catalog import canonical, guard
from app.opensearch_catalog.context import Limits
from app.opensearch_catalog.text_forms import is_variant_group, phrase_variants, word_forms

W = {"range": {"@timestamp": {"gte": "now-24h", "lte": "now"}}}
HOST = {"term": {"host.keyword": "leif-login03-bmc"}}
OVERHEAT = ("System Overheat", "System Overheated", "System Overheating", "System Overheats")


def ilo_rows(text):
    return {"size": 20, "query": {"bool": {"filter": [W, HOST, {"match_phrase": {"Message": text}}]}}}


def check(os_ctx, pid, query, **kw):
    return guard.check_entry({"pattern_id": pid, "query": query}, os_ctx(pid, **kw))


# ---- the word-form rules ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["System Overheated", "System Overheating", "System Overheat", "System Overheats"])
def test_every_tense_of_the_same_words_gives_the_same_variants(text):
    assert phrase_variants(text) == OVERHEAT


@pytest.mark.parametrize("word,expected", [
    ("disabled", ("disable", "disabled", "disables", "disabling")),
    ("stopped", ("stop", "stopped", "stopping", "stops")),
    ("retried", ("retried", "retries", "retry", "retrying")),
    ("cleared", ("clear", "cleared", "clearing", "clears")),
    ("Failed", ("Fail", "Failed", "Failing", "Fails")),
])
def test_spelling_rules(word, expected):
    assert word_forms(word) == expected


@pytest.mark.parametrize("text", ["IPMI", "RMCP", "LinkStatus", "172.24.0.1", "i2c", "(Temperature", "fan", "status"])
def test_acronyms_camelcase_numbers_punctuation_short_and_stop_words_are_left_as_written(text):
    assert phrase_variants(text) == (text,)


def test_variant_count_is_capped_by_varying_the_inflected_word_only():
    # varying both words would give 16 phrases; only the inflected one is varied
    assert phrase_variants("System Overheated") == OVERHEAT
    assert len(phrase_variants("Fan failed overheated system")) <= 8


def test_every_expanded_list_is_recognised_as_a_group_and_nothing_else_is():
    assert is_variant_group(list(OVERHEAT))
    assert not is_variant_group(["System Overheat", "System Overheated"])          # incomplete
    assert not is_variant_group(["login", "logout"])                               # unrelated phrases
    assert not is_variant_group(list(reversed(OVERHEAT)))                          # not canonical order


# ---- guard canonicalisation ------------------------------------------------------------------------------------------


def test_both_wordings_of_the_reported_question_canonicalise_to_the_identical_query(os_ctx):
    a = check(os_ctx, "ilo_events", ilo_rows("System Overheated"))
    b = check(os_ctx, "ilo_events", ilo_rows("System Overheating"))
    assert a.ok and b.ok, (a.reason, b.reason)
    assert a.query == b.query
    group = a.query["query"]["bool"]["filter"][2]
    assert group == {"bool": {"should": [{"match_phrase": {"Message": t}} for t in OVERHEAT],
                              "minimum_should_match": 1}}


def test_the_expansion_is_recorded_not_silent(os_ctx):
    r = check(os_ctx, "ilo_events", ilo_rows("System Overheated"))
    note = next(a for a in r.applied if "word forms" in a)
    assert "System Overheated" in note and "System Overheating" in note


def test_expanded_query_is_idempotent(os_ctx):
    first = check(os_ctx, "ilo_events", ilo_rows("System Overheated"))
    again = check(os_ctx, "ilo_events", copy.deepcopy(first.query))
    assert again.ok and again.query == first.query
    assert not any("word forms" in a for a in again.applied)


def test_match_keeps_its_operator_in_every_variant(os_ctx):
    q = ilo_rows("x")
    q["query"]["bool"]["filter"][2] = {"match": {"Message": {"query": "System Overheated", "operator": "and"}}}
    r = check(os_ctx, "ilo_events", q)
    assert r.ok, r.reason
    should = r.query["query"]["bool"]["filter"][2]["bool"]["should"]
    assert [s["match"]["Message"] for s in should] == [{"query": t, "operator": "and"} for t in OVERHEAT]
    assert check(os_ctx, "ilo_events", copy.deepcopy(r.query)).query == r.query


def test_phrase_without_variants_is_unchanged(os_ctx):
    r = check(os_ctx, "ilo_events", ilo_rows("IPMI"))
    assert r.ok and r.query["query"]["bool"]["filter"][2] == {"match_phrase": {"Message": "IPMI"}}


def test_filters_aggregation_buckets_are_expanded_and_stay_valid(os_ctx):
    q = {"size": 0, "query": {"bool": {"filter": [W]}},
         "aggs": {"access": {"filters": {"filters": {"browser": {"match_phrase": {"Message": "browser"}},
                                                     "ipmi": {"match_phrase": {"Message": "IPMI"}}}}}}}
    r = check(os_ctx, "ilo_events", q)
    assert r.ok, r.reason
    buckets = r.query["aggs"]["access"]["filters"]["filters"]
    assert "should" in buckets["browser"]["bool"] and buckets["ipmi"] == {"match_phrase": {"Message": "IPMI"}}
    assert check(os_ctx, "ilo_events", copy.deepcopy(r.query)).query == r.query


def test_a_hand_written_should_of_unrelated_phrases_is_an_ordinary_bool(os_ctx):
    q = ilo_rows("x")
    q["query"]["bool"]["filter"][2] = {"bool": {"should": [{"match_phrase": {"Message": "IPMI"}},
                                                           {"match_phrase": {"Message": "RMCP"}}],
                                                "minimum_should_match": 1}}
    r = check(os_ctx, "ilo_events", q)
    assert r.ok and r.query["query"]["bool"]["filter"][2] == q["query"]["bool"]["filter"][2]


def test_a_group_on_a_non_text_field_is_still_rejected(os_ctx):
    q = ilo_rows("x")
    q["query"]["bool"]["filter"][2] = {"bool": {"should": [{"match_phrase": {"host": t}} for t in OVERHEAT],
                                                "minimum_should_match": 1}}
    r = check(os_ctx, "ilo_events", q)
    assert not r.ok and r.code == guard.CODE_OPERATION_NOT_SUPPORTED


def test_at_the_clause_limit_the_phrase_is_searched_as_written_with_a_warning(os_ctx):
    r = check(os_ctx, "ilo_events", ilo_rows("System Overheated"), limits=Limits(max_clauses=6))
    assert r.ok, r.reason
    assert r.query["query"]["bool"]["filter"][2] == {"match_phrase": {"Message": "System Overheated"}}
    assert any("searched exactly as written" in w for w in r.warnings)
    again = check(os_ctx, "ilo_events", copy.deepcopy(r.query), limits=Limits(max_clauses=6))
    assert again.ok and again.query == r.query


def test_canonical_contract_carries_the_note_for_the_frontend(os_ctx):
    out = canonical.canonicalise_contract(
        {"mode": "single", "status": "ok", "data_source": "opensearch", "pattern_id": "ilo_events",
         "query": ilo_rows("System Overheated")}, os_ctx("ilo_events"))
    assert any("word forms" in a for a in out["guard_notes"]["applied"])


# ---- executor: an empty text search is flagged, not presented as "nothing happened" ----------------------------------


def _run(settings, os_ctx, query, raw):
    entry = canonical.canonicalise_contract(
        {"status": "ok", "data_source": "opensearch", "pattern_id": "ilo_events", "query": query},
        os_ctx("ilo_events"))
    with patch.object(opensearch_client, "search",
                      return_value=opensearch_client.ExecutionOutcome(status="empty_result", raw_body=raw)) as m:
        res = executor.execute_contract(entry, settings)
    return res["execution"], m


EMPTY = {"took": 1, "timed_out": False, "_shards": {"total": 1, "successful": 1, "failed": 0},
         "hits": {"total": {"value": 0, "relation": "eq"}, "hits": []}}


def test_empty_text_search_carries_a_note_naming_what_was_searched(settings, os_field_cache, os_ctx):
    ex, m = _run(settings, os_ctx, ilo_rows("System Overheated"), EMPTY)
    assert m.called and ex["execution_status"] == "empty_result"
    note = ex["metadata"]["text_search_note"]
    assert "'System Overheat'" in note and "Overheating'" not in note  # one representative per group
    assert "different words" in note


def test_empty_result_without_text_search_has_no_note(settings, os_field_cache, os_ctx):
    q = {"size": 20, "query": {"bool": {"filter": [W, HOST]}}}
    ex, _ = _run(settings, os_ctx, q, EMPTY)
    assert ex["execution_status"] == "empty_result" and "text_search_note" not in ex["metadata"]
