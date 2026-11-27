"""Word-form tolerance for free-text search (app/opensearch_catalog/text_forms.py and
the guard's canonicalisation of match/match_phrase).

Regression for: "What timestamps were recorded for iLO System Overheating events on
host X?" returned 8 rows, while "... System Overheated events on host X?" built
match_phrase "System Overheated" and returned an empty result, because the text
field is tokenised but not stemmed. Equivalent wordings must canonicalise to the
SAME query, and the change must be recorded, never silent."""
import copy
from dataclasses import replace
from unittest.mock import patch

import pytest

from app import executor, opensearch_client
from app.opensearch_catalog import canonical, guard
from app.opensearch_catalog.context import Limits, format_prompt_block
from app.opensearch_catalog.text_forms import is_variant_group, phrase_variants, signal_branch, threshold_keywords, word_forms

W = {"range": {"@timestamp": {"gte": "now-24h", "lte": "now"}}}
HOST = {"term": {"host.keyword": "leif-login03-bmc"}}
OVERHEAT = ("System Overheat", "System Overheated", "System Overheating", "System Overheats")


def ilo_rows(text):
    return {"size": 20, "query": {"bool": {"filter": [W, HOST, {"match_phrase": {"Message": text}}]}}}


def check(os_ctx, pid, query, **kw):
    return guard.check_entry({"pattern_id": pid, "query": query}, os_ctx(pid, **kw))


def canonical_again(os_ctx, result, **kw):
    assert result.ok, result.reason
    again = check(os_ctx, "ilo_events", copy.deepcopy(result.query), **kw)
    assert again.ok and again.query == result.query and not again.applied, again


def text_clause(result):
    return result.query["query"]["bool"]["must"][0]


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
    group = text_clause(a)
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


def test_match_and_preserves_the_complete_original_clause(os_ctx):
    q = ilo_rows("x")
    q["query"]["bool"]["filter"][2] = {"match": {"Message": {"query": "System Overheated", "operator": "and"}}}
    r = check(os_ctx, "ilo_events", q)
    assert r.ok, r.reason
    assert text_clause(r) == q["query"]["bool"]["filter"][2]
    canonical_again(os_ctx, r)


def threshold_rows(text, minimum=2, **options):
    q = ilo_rows("x")
    q["query"]["bool"]["filter"][2] = {"match": {"Message": {
        "query": text, "minimum_should_match": minimum, **options,
    }}}
    return q


def unordered_rows(text, **options):
    q = ilo_rows("x")
    q["query"]["bool"]["filter"][2] = {"match": {"Message": {"query": text, **options} if options else text}}
    return q


@pytest.mark.parametrize("n,minimum", [(2, 2), (3, 2), (4, 3), (5, 3), (6, 4), (7, 5), (8, 6), (9, 6), (10, 7)])
def test_default_policy_and_canonical_form(os_ctx, n, minimum):
    words = "fan off rpm hot ups bus cpu gpu psu dimm".split()
    r = check(os_ctx, "ilo_events", unordered_rows(" ".join(words[:n])))
    assert text_clause(r)["bool"]["minimum_should_match"] == minimum
    assert r.text_search["source"] == "default" and len(r.text_search["keywords"]) == n
    assert any(f"default minimum_should_match {minimum} of {n}" in note for note in r.applied)
    canonical_again(os_ctx, r)


def test_default_can_be_disabled_and_ratio_can_be_changed(os_ctx):
    q = unordered_rows("fan off rpm cpu")
    disabled = replace(Limits(), text_default_msm_enabled=False)
    r = check(os_ctx, "ilo_events", q, limits=disabled)
    assert text_clause(r) == {"match": {"Message": "fan off rpm cpu"}}
    assert r.text_search["source"] == "none"
    canonical_again(os_ctx, r, limits=disabled)
    r = check(os_ctx, "ilo_events", q, limits=replace(Limits(), text_default_msm_ratio=1))
    assert text_clause(r)["bool"]["minimum_should_match"] == 4


@pytest.mark.parametrize("query", ["fan", "the Fan"])
def test_zero_policy_threshold_for_one_keyword(os_ctx, query):
    r = check(os_ctx, "ilo_events", unordered_rows(query))
    assert text_clause(r) == {"match": {"Message": query}}
    canonical_again(os_ctx, r)


def test_prompt_examples_show_default_explicit_and_named_signal_forms(os_ctx):
    prompt = format_prompt_block(os_ctx("ilo_events"))
    assert '{"match":{"Message":"fan off rpm"}}' in prompt
    assert '"minimum_should_match":1' in prompt and 'minimum_should_match: 2' in prompt
    assert 'Percentages, negatives and expressions are forbidden' in prompt
    assert '"match_phrase":{"Message":"link down"}' in prompt and '"minimum_should_match":2' in prompt
    assert 'best matches first, ties newest first' in prompt and 'Never write _score' in prompt
    assert 'best matches first' not in format_prompt_block(os_ctx("ilo_events", limits=Limits(rank_text_matches=False)))


def test_explicit_one_stays_native_and_does_not_pick_up_the_default(os_ctx):
    r = check(os_ctx, "ilo_events", threshold_rows("fan off rpm", 1))
    assert text_clause(r) == {"match": {"Message": {
        "query": "fan off rpm", "operator": "or", "minimum_should_match": 1,
    }}}
    assert r.text_search["source"] == "explicit"
    canonical_again(os_ctx, r)


def test_each_identifier_is_one_signal_even_if_analyzed_as_many_tokens(os_ctx):
    r = check(os_ctx, "ilo_events", unordered_rows("eth0/1 link-down x1000c0s0b0"))
    assert text_clause(r) == {"bool": {"minimum_should_match": 2, "should": [
        {"match_phrase": {"Message": word}} for word in ("eth0/1", "link-down", "x1000c0s0b0")
    ]}}
    canonical_again(os_ctx, r)


@pytest.mark.parametrize("negator", ["not", "no", "never", "without", "cannot"])
def test_negated_words_are_excluded_instead_of_counted(os_ctx, negator):
    r = check(os_ctx, "ilo_events", unordered_rows(f"fan {negator} hot rpm"))
    group = text_clause(r)["bool"]
    assert group["must_not"] == [{"match": {"Message": {
        "query": "hot", "operator": "or", "minimum_should_match": 1,
    }}}]
    assert group["must"][0]["bool"]["minimum_should_match"] == 2
    assert r.text_search["keywords"] == ["fan", "rpm"]
    assert any("excluded hot" in note for note in r.applied)
    canonical_again(os_ctx, r)


def test_unsupported_negation_warns_and_impossible_positive_minimum_asks_for_terms(os_ctx):
    r = check(os_ctx, "ilo_events", unordered_rows("fan rpm without the power"))
    assert "without" not in str(r.query) and any("negation is not supported" in w for w in r.warnings)
    canonical_again(os_ctx, r)
    r = check(os_ctx, "ilo_events", threshold_rows("fan without power", 2))
    assert r.code == guard.CODE_BAD_VALUE and "more distinct search terms" in r.user_message
    r = check(os_ctx, "ilo_events", unordered_rows("the is no"))
    assert r.code == guard.CODE_BAD_VALUE and "minimum 1" in r.user_message


@pytest.mark.parametrize("separator", [", ", " . "])
def test_negation_cannot_skip_over_punctuation_between_signals(os_ctx, separator):
    r = check(os_ctx, "ilo_events", unordered_rows(f"fan rpm without{separator}power"))
    assert r.ok and r.text_search["keywords"] == ["fan", "rpm", "power"]
    assert "must_not" not in text_clause(r)["bool"]
    assert any("negation is not supported" in warning for warning in r.warnings)
    canonical_again(os_ctx, r)


def test_bool_should_is_required_with_or_without_other_clauses(os_ctx):
    for extra in ({}, {"must_not": {"term": {"host.keyword": "bad"}}},
                  {"must": {"term": {"host.keyword": "keep"}}}):
        nested = {"bool": {"should": {"match_phrase": {"Message": "IPMI"}}, **extra}}
        q = {"size": 0, "query": {"bool": {"filter": [W, nested]}}}
        result = check(os_ctx, "ilo_events", q)
        root = result.query["query"]["bool"]
        assert "minimum_should_match" not in root and root["filter"][1]["bool"]["minimum_should_match"] == 1
        assert any("should clause is required" in a for a in result.applied)
        canonical_again(os_ctx, result)


def test_expansion_reserves_clauses_for_later_siblings(os_ctx):
    limits = replace(Limits(), max_clauses=6)
    q = unordered_rows("fan off rpm")
    q["query"]["bool"]["filter"] += [HOST, HOST]
    result = check(os_ctx, "ilo_events", q, limits=limits)
    assert text_clause(result)["match"]["Message"]["minimum_should_match"] == 2
    assert any("query limits" in w for w in result.warnings)
    canonical_again(os_ctx, result, limits=limits)
    q["query"]["bool"]["filter"].append(HOST)
    assert check(os_ctx, "ilo_events", q, limits=limits).code == guard.CODE_LIMIT


@pytest.mark.parametrize("limits,text", [
    (replace(Limits(), max_bool_depth=1), "fan off rpm"),
    (replace(Limits(), max_text_chars=30), "configuration fan"),
    (Limits(), "one two three four five six seven eight nine ten eleven"),
])
def test_threshold_expansion_degrades_at_depth_text_and_branch_limits(os_ctx, limits, text):
    r = check(os_ctx, "ilo_events", unordered_rows(text), limits=limits)
    assert r.ok and "match" in text_clause(r)
    assert any("simple match" in warning for warning in r.warnings)
    canonical_again(os_ctx, r, limits=limits)


def test_ranked_sort_is_guard_owned_and_yields_to_explicit_sort(os_ctx):
    ranked_sort = [{"_score": "desc"}, {"@timestamp": "desc"}]
    q = unordered_rows("fan off rpm")
    r = check(os_ctx, "ilo_events", q)
    assert r.query["sort"] == ranked_sort and r.text_search["ranked"]
    canonical_again(os_ctx, r)
    q["sort"] = [{"@timestamp": "asc"}]
    r = check(os_ctx, "ilo_events", q)
    assert r.query["sort"] == q["sort"] and not r.text_search["ranked"]
    assert "must" not in r.query["query"]["bool"]
    canonical_again(os_ctx, r)
    q["sort"] = [{"_score": "asc"}]
    assert check(os_ctx, "ilo_events", q).code == guard.CODE_BAD_CLAUSE
    forged = {"status": "ok", "data_source": "opensearch", "pattern_id": "ilo_events",
              "query": {**r.query, "sort": ranked_sort}, "guard_notes": {"text_search": {"source": "default"}}}
    assert canonical.canonicalise_contract(forged, os_ctx("ilo_events"))["status"] == "unmapped"


@pytest.mark.parametrize("agg", [None, {"access": {"filters": {"filters": {"api": {
    "match_phrase": {"Message": "IPMI"},
}}}}}])
def test_counts_and_aggregations_keep_text_in_filter(os_ctx, agg):
    q = unordered_rows("fan off rpm")
    q["size"] = 0
    root = q["query"]["bool"]
    root["must"] = [root["filter"].pop()]
    if agg:
        q["aggs"] = agg
    r = check(os_ctx, "ilo_events", q)
    assert "must" not in r.query["query"]["bool"] and "sort" not in r.query and not r.text_search["ranked"]
    canonical_again(os_ctx, r)


def test_threshold_keywords_drop_standalone_stop_words_and_repetitions():
    assert threshold_keywords("The, is: NOT the! IPMI, IPMI ::1 x1000c3r1 172.24.0.1 link-is-down fan.:.") == (
        "ipmi", "::1", "x1000c3r1", "172.24.0.1", "link-is-down", "fan",
    )


def test_thresholded_match_builds_one_branch_per_distinct_positive_word(os_ctx):
    q = threshold_rows("The System is Overheated, System.", operator="or")
    r = check(os_ctx, "ilo_events", q)
    assert r.ok, r.reason
    group = text_clause(r)["bool"]
    assert group == {"minimum_should_match": 2, "should": [
        signal_branch("Message", "system"), signal_branch("Message", "overheated"),
    ]}
    assert any("normalised search words" in note for note in r.applied)
    canonical_again(os_ctx, r)


@pytest.mark.parametrize("text,minimum", [("the IS, a!", 1), ("the fan FAN: is", 2), ("fan", 2),
                                         ("fan IPMI", True), ("fan IPMI", "2"), ("fan IPMI", 2.0),
                                         ("fan IPMI", 0), ("fan IPMI", 11)])
def test_invalid_term_minimum_is_rejected(os_ctx, text, minimum):
    r = check(os_ctx, "ilo_events", threshold_rows(text, minimum))
    assert not r.ok and r.code == guard.CODE_BAD_VALUE


def test_threshold_requires_or_and_defaults_only_for_unordered_standalone_words(os_ctx):
    invalid = check(os_ctx, "ilo_events", threshold_rows("the fan is down", operator="and"))
    assert not invalid.ok and invalid.code == guard.CODE_BAD_VALUE
    phrase = ilo_rows("the fan is off")
    plain = copy.deepcopy(phrase)
    plain["query"]["bool"]["filter"][2] = {"match": {"Message": "the fan is off"}}
    r = check(os_ctx, "ilo_events", phrase)
    assert r.ok and text_clause(r) == {"match_phrase": {"Message": "the fan is off"}}
    r = check(os_ctx, "ilo_events", plain)
    assert r.ok and text_clause(r)["bool"]["minimum_should_match"] == 2
    phrase["query"]["bool"]["filter"][2]["match_phrase"]["Message"] = {
        "query": "the fan is off", "minimum_should_match": 2,
    }
    assert not check(os_ctx, "ilo_events", phrase).ok


def test_threshold_keeps_cleaned_text_when_expansion_exceeds_the_budget(os_ctx):
    limits = Limits(max_clauses=5)
    r = check(os_ctx, "ilo_events", threshold_rows("the System is Overheated"), limits=limits)
    assert r.ok, r.reason
    assert text_clause(r) == {"match": {"Message": {
        "query": "system overheated", "operator": "or", "minimum_should_match": 2,
    }}}
    assert any("simple match" in w for w in r.warnings)
    canonical_again(os_ctx, r, limits=limits)


@pytest.mark.parametrize("minimum,phrases,accepted", [
    (0, [], True), (1, [], False), (1, ["IPMI"], True), (2, ["IPMI", "RMCP"], True),
    (3, ["IPMI", "RMCP"], False), (2, ["IPMI", "IPMI"], False),
    (2, ["System Overheated", "System Overheating"], False),
    (2, ["link down", "link downed"], False),
])
def test_boolean_minimum_is_bounded_by_distinct_explicit_branches(os_ctx, minimum, phrases, accepted):
    q = {"query": {"bool": {"filter": [W], "minimum_should_match": minimum}}}
    if phrases:
        q["query"]["bool"]["should"] = [{"match_phrase": {"Message": phrase}} for phrase in phrases]
    r = check(os_ctx, "ilo_events", q)
    assert r.ok is accepted
    if accepted:
        assert r.query["query"]["bool"]["minimum_should_match"] == minimum
    else:
        assert r.code == guard.CODE_BAD_VALUE


@pytest.mark.parametrize("size", [0, 10])
def test_duplicate_signals_are_detected_before_scoring_context_changes(os_ctx, size):
    first = {"bool": {"filter": [{"match_phrase": {"Message": "fan"}}]}}
    second = {"bool": {"must": [{"match_phrase": {"Message": {"query": "fan"}}}]}}
    q = {"size": size, "query": {"bool": {"filter": [W], "should": [first, second], "minimum_should_match": 2}}}
    assert check(os_ctx, "ilo_events", q).code == guard.CODE_BAD_VALUE
    second["bool"]["must"][0]["match_phrase"]["Message"]["query"] = "rpm"
    canonical_again(os_ctx, check(os_ctx, "ilo_events", q))


def test_phrase_without_variants_is_unchanged(os_ctx):
    r = check(os_ctx, "ilo_events", ilo_rows("IPMI"))
    assert r.ok and text_clause(r) == {"match_phrase": {"Message": "IPMI"}}


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
    assert r.ok and text_clause(r) == q["query"]["bool"]["filter"][2]


def test_a_group_on_a_non_text_field_is_still_rejected(os_ctx):
    q = ilo_rows("x")
    q["query"]["bool"]["filter"][2] = {"bool": {"should": [{"match_phrase": {"host": t}} for t in OVERHEAT],
                                                "minimum_should_match": 1}}
    r = check(os_ctx, "ilo_events", q)
    assert not r.ok and r.code == guard.CODE_OPERATION_NOT_SUPPORTED


def test_at_the_clause_limit_the_phrase_is_searched_as_written_with_a_warning(os_ctx):
    r = check(os_ctx, "ilo_events", ilo_rows("System Overheated"), limits=Limits(max_clauses=6))
    assert r.ok, r.reason
    assert text_clause(r) == {"match_phrase": {"Message": "System Overheated"}}
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


def test_empty_threshold_note_describes_the_cleaned_unordered_search(settings, os_field_cache, os_ctx):
    ex, _ = _run(settings, os_ctx, threshold_rows("the fan is off"), EMPTY)
    note = ex["metadata"]["text_search_note"]
    assert "at least 2 of: fan, off" in note and "minimum of 1" in note
    assert ex["metadata"]["text_search"] == {
        "keywords": ["fan", "off"], "minimum_should_match": 2, "source": "explicit", "ranked": True,
    }
    assert "ordered phrase" not in note
